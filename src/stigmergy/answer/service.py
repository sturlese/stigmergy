"""AnswerService — the answering loop over `BrainService`, plus the strict verdict gate.

The loop: an agent gathers evidence, a deterministic verifier judges figures and citations,
exactly one corrective retry is allowed, and the verdict travels with the answer. On top of that
sits the piece that is NOT in `verify()` (ADR 007):

    the STRICT GATE — after the single retry, ANY untraced figure in a channel that would SHIP
    (the answer or a citation quote) suppresses it: an answer with an untraced figure becomes an
    honest refusal carrying the verifier's findings. No untraced figure ever leaves in a
    human-readable channel — it surfaces ONLY inside `verdict`. A citation-only problem (no
    figures) still ships labeled `partial`; a `failed` verdict (2+ problems) never ships.

`ask` runs under the server process identity — there is no per-call identity parameter, which a
client could spoof. The response is transport-agnostic JSON; the MCP adapter is a thin skin over it.

**A refusal's shipped `reason` is composed ENTIRELY by this module, from structured facts recorded
during the run — never from anything the model wrote.** When `out.reason` was the model's own
free-text explanation, `_shape_refusal` merely scanned it for a smuggled figure before shipping it
or swapping in a neutral template. That produced a false explanation in practice: a CORRECT
refusal ("that entity's ARR doesn't answer the question") justified by a WRONG claim about the
corpus ("only a quarterly value exists, not monthly") that nobody had verified. `verdict: verified`
on a refusal has only ever meant no untraced FIGURE escaped, never that the explanation was TRUE.

`run_facts_reason` (below) is the one composer for all five refusal shapes, built from two facts
the server itself recorded this run — which queries ran (`ctx.searched`) and which pages the tools
actually returned (`ctx.read_paths_order`, or `out.citations` for a suppressed drafted answer) —
and nothing else. It cannot assert what the brain does or does not contain, because it is never
handed anything about the brain as a whole to assert from. Semantic verification is deliberately
out of scope: an LLM judge over it was tried and retired.
"""
import logging

from stigmergy.answer.brain import AnswerBrain
from stigmergy.answer.numbers import unverified_figures
from stigmergy.answer.synthesize import SynthesisContext, answer_limits, build_synthesizer
from stigmergy.answer.verify_answer import feedback, verify
from stigmergy.index import store
from stigmergy.server.service import neutralize_fence

log = logging.getLogger(__name__)

_RANK = {"verified": 0, "partial": 1, "failed": 2}


def _dedup(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))          # order-preserving unique


def _reverdict(figs: list[str], citation_problems: list[str]) -> dict:
    """Recompute the verdict from the FULL set of shipped-channel problems (answer + citation-quote
    figures + citation problems) — the same verified/partial/failed thresholds `verify()` uses,
    applied after the gate widened the figure scan to every shipped channel. A `failed` here always
    suppresses, so it only ever appears on a refusal;
    a single untraced figure reads `partial` yet still suppresses (`suppressed`/`refused` mark it)."""
    n = len(figs) + len(citation_problems)
    label = "verified" if n == 0 else ("partial" if n == 1 else "failed")
    return {"verdict": label, "unverified_figures": figs, "citation_problems": citation_problems}


# ── the refusal's shipped prose — composed from server-recorded facts, never the model ─────────
# Cap at 3 named items, "and N more" past that — the same shape `report.needs_input()` already uses
# for a long candidate list, not a second convention.
_NAMED_ITEMS_CAP = 3


def _list_clause(verb: str, items: list[str]) -> str:
    """`{verb} "a" and "b"` / `{verb} a, b and c` / `{verb} a, b, c and N more` — the ONE joining
    shape both `searched` (quoted query text) and `surfaced` (bare page titles) use, so the two
    clauses read as one voice rather than two hand-written formats that happen to look similar."""
    n = len(items)
    if n == 0:
        return ""
    if n <= _NAMED_ITEMS_CAP:
        head, last = items[:-1], items[-1]
        body = (", ".join(head) + " and " + last) if head else last
        return f"{verb} {body}"
    head = ", ".join(items[:_NAMED_ITEMS_CAP])
    return f"{verb} {head} and {n - _NAMED_ITEMS_CAP} more"


# A length cap for whatever query text DOES ship — belt-and-suspenders alongside the substring
# check below, the same doctrine `Citation.quote`'s own <=200 cap takes.
_QUERY_CAP = 200


def _shippable_queries(question: str, searched: list[str]) -> list[str]:
    """Which of `searched`'s recorded queries may be quoted VERBATIM into a refusal: only the ones
    that are themselves a verbatim (case-insensitive) substring of the asker's OWN `question` —
    the one check that makes "the asker's own words" a true claim rather than a merely asserted
    one. `ctx.searched` is populated from the AGENT's own tool-call arguments, and the agent is
    steerable by hostile page content (the threat this codebase's fencing exists for): a steered
    agent can search for a literal string it wants shipped verbatim into the refusal prose, and
    neutralizing a fence token in that string does not stop it from reading as a persuasive
    corpus-characterizing sentence (a fenced token is inert as a DELIMITER, not as prose). Every
    query that fails this check is dropped here and folded into a count clause by the caller
    instead — never merely fenced, never shipped at all.

    Neutralized and length-capped even though a substring of the question is, definitionally, no
    worse than the question itself — the same belt-and-suspenders posture `_compose_reason`'s own
    defensive backstop takes."""
    q_norm = (question or "").lower()
    return [neutralize_fence(q)[:_QUERY_CAP] for q in searched if q and q.lower() in q_norm]


def _searched_clause(question: str, searched: list[str]) -> str:
    """`searched "a" and "b"` for queries that are the asker's own words (a substring of
    `question`), `and N other search(es)` folded in for every recorded query that is NOT — never
    quoted, whatever it says."""
    shipped = _shippable_queries(question, searched)
    other = len(searched) - len(shipped)
    lead = _list_clause("searched", [f'"{q}"' for q in shipped])
    if other:
        note = f"{other} other search" + ("" if other == 1 else "es")
        lead = f"{lead} and {note}" if lead else f"searched {note}"
    return lead


def _surfaced_clause(surfaced: list[str]) -> str:
    return _list_clause("surfaced", surfaced)


def _them_it(n: int) -> str:
    """"it" for exactly one cited page, "them" for more than one, `""` for ZERO — a suppressed
    refusal can genuinely cite nothing at all (an answer with a
    fabricated figure and no citations at all; `verify_answer.check_citations`'s own "answer
    carries no citations" is exactly this case for `suppressed_citations`), and `""` signals the
    caller to drop the pronoun clause entirely rather than render "used them"/"quoted them"
    referring to nothing."""
    if n == 0:
        return ""
    return "it" if n == 1 else "them"


def _join_lead(*clauses: str) -> str:
    """Join non-empty lead clauses with ', ' — an empty clause contributes nothing, including no
    stray separator. Without this, a case with neither a recorded query nor a named page (should
    be rare, but `read_page` alone records no query) would read ", — a drafted answer used…" with
    an orphaned comma and dash rather than dropping the lead entirely."""
    return ", ".join(c for c in clauses if c)


def run_facts_reason(case: str, question: str, searched: list[str], surfaced: list[str]) -> str:
    """THE composer for every refusal's shipped `reason` — one function, five cases, no model
    input. `searched`/`surfaced` are already deduped, in first-tried/first-surfaced
    order (`SynthesisContext.note_query`/`note_page`).

    `case` is one of:
    - `"no_surface"` — a genuine refusal, nothing came back from any tool this run.
    - `"no_match"` — a genuine refusal, pages surfaced but none carried what was asked.
    - `"budget_exceeded"` — a genuine refusal: the agent's run hit its tool budget
      (`synthesize.answer_limits`) before an `AnswerOutput` ever existed, so there is nothing to
      verify.
    - `"suppressed_figures"` — a drafted answer existed and was withheld for an untraced figure.
    - `"suppressed_citations"` — a drafted answer existed and was withheld for an unconfirmable
      citation (no figures involved).

    Every sentence states only what ran and what came back THIS turn — never what the brain does
    or does not contain, which is a semantic claim no deterministic verifier can check.

    `question` is the asker's OWN question text — the one thing
    `_searched_clause` trusts to decide which recorded queries may be quoted verbatim. Without it,
    a steered agent's own search queries shipped verbatim by construction (`ctx.searched` is
    agent-authored, not asker-authored); see `_shippable_queries` for the full reasoning.
    """
    lead = _join_lead(_searched_clause(question, searched), _surfaced_clause(surfaced))

    if case == "no_surface":
        if not searched:
            # Should not happen given the agent's own instructions (it always searches first),
            # but a composer must not crash if it does.
            return "nothing came back this run — no tool call found anything to work with."
        return f"{lead} — nothing came back this run."

    if case == "no_match":
        ending = {1: "it doesn't answer that.",
                 2: "neither answers that."}.get(len(surfaced), "none of them answer that.")
        prefix = f"{lead} — " if lead else ""
        return f"{prefix}{ending}"

    if case == "budget_exceeded":
        prefix = f"{lead} — " if lead else ""
        return f"{prefix}the answer could not be completed within the tool budget."

    them_it = _them_it(len(surfaced))
    prefix = f"{lead} — " if lead else ""
    if case == "suppressed_figures":
        if them_it:
            return (f"{prefix}a drafted answer used {them_it}, but it carried a figure none of "
                    f"that evidence could confirm, so it was withheld. No unverified number "
                    f"leaves the brain.")
        # zero cited pages — nothing for "used them" to refer to.
        return (f"{prefix}a drafted answer carried a figure none of that evidence could confirm, "
                f"so it was withheld. No unverified number leaves the brain.")
    if case == "suppressed_citations":
        if them_it:
            return (f"{prefix}a drafted answer quoted {them_it} in a way the verifier couldn't "
                    f"confirm word-for-word, so it was withheld.")
        # zero cited pages (e.g. "answer carries no citations") — nothing was quoted at all.
        return (f"{prefix}a drafted answer carried no citation the verifier could confirm, "
                f"so it was withheld.")

    raise ValueError(f"unknown refusal_case {case!r}")   # exhaustive; a new case is a code change


def _titles_for(get_page, paths: list[str]) -> list[str]:
    """Page titles for a list of paths, one lookup each (`AnswerBrain.get_page`, already
    ACL-scoped — the tools that populate `read_paths`/`out.citations` never surface anything
    outside the asker's audience, so no new scoping logic is needed here).

    **Never falls back to the path.** A page contract requires a title, so a missing one is a data
    problem, not a rendering one — logged as such, with a generic placeholder standing in rather
    than the path (which could name something outside the asker's own view, e.g. a zone/type
    segment) or a blank slot a reader would misread as a formatting bug.

    **Neutralized and length-capped, once, here.** `search_text` already neutralizes the SAME
    frontmatter titles before an agent ever sees them — but an MCP consumer reads this composer's
    `reason`/`surfaced` fields as SERVER prose, raw (Slack's own mrkdwn escaping is not a defense
    every consumer gets), so a hostile title reaching either field with no fence-neutralization and
    no cap is one surface behind the discipline `search_text` already gives the exact same text.
    Done in this ONE function so both `reason` (via `_surfaced_clause`) and the structured
    `surfaced` field inherit it, rather than at each of `_shape`/`_shape_refusal`'s two call
    sites."""
    titles = []
    for path in paths:
        page = get_page(path)
        title = (page or {}).get("title") if page else ""
        if not title:
            log.warning("refusal composer: page %r has no title — a page contract violation, "
                       "not a rendering choice", path)
        titles.append(neutralize_fence(title)[:_QUERY_CAP] if title else "a page")
    return titles


class AnswerService:
    def __init__(self, service, settings=None):
        """`service` is a `stigmergy.server.service.BrainService`; `settings` carries the answer
        model policy (llm/model/reasoning_effort) — defaults to the service's own settings."""
        self.brain = AnswerBrain(service)
        self.settings = settings if settings is not None else service.settings

    async def ask(self, question: str) -> dict:
        # `UsageLimitExceeded` is the ONE pydantic_ai exception class this method catches. The
        # other SDK exception classes stay uncaught, deliberately: a model outage is a different
        # question, not answered here. Imported inside the method
        # rather than gated behind `self.settings.llm` like `answer_limits()` below: a monkeypatched
        # double injected under `build_synthesizer` (this suite's own established way to drive
        # `ask()` through a controlled agent) can raise this regardless of `settings.llm`, and this
        # method must catch it whichever backend is nominally configured.
        from pydantic_ai.exceptions import UsageLimitExceeded
        agent = build_synthesizer(self.settings)
        # the fake path never needs (nor constructs) pydantic_ai's UsageLimits
        limits = None if self.settings.llm == "fake" else answer_limits()
        ctx = SynthesisContext(service=self.brain)
        try:
            result = await agent.run(question, deps=ctx, usage_limits=limits)
        except UsageLimitExceeded:
            # No `AnswerOutput` ever existed to verify — a genuine refusal, never the corrective
            # retry (a second full run would double the very budget spend the limit exists to
            # bound; the retry exists for verifier feedback on a drafted answer, and there is none).
            return self._shape_budget_refusal(question, ctx)
        out = result.output
        verdict = verify(out, ctx.evidence_text(), self.brain.get_page, ctx.read_paths)
        # The FIRST draft's verdict, kept whatever happens next — nothing else records it. `verdict`
        # is rebound the moment the retry improves on it, and what ships is re-derived a third time
        # by the strict gate, so production could see THAT a question paid a second full agent run
        # (`retried`) and never what it paid for. That distinction is the entire economics of the
        # retry, because the two cases are worth opposite amounts: a first draft carrying an
        # untraced FIGURE would be SUPPRESSED without the retry, so the retry buys the answer
        # itself; one carrying a single citation problem ships as `partial` either way, so the
        # retry buys a label and an accurate quote. Measured on staging 2026-08: ~41 % of asks
        # retry, each costing ~6.8 s against a comparable answered ask — and not one ask has ever
        # been suppressed, which is a hint about which case dominates and no more than a hint.
        first_verdict = verdict

        retried = False
        if verdict["verdict"] != "verified" and not out.refused:
            retried = True
            try:
                # The retry carries the first run's MESSAGE HISTORY, so the model redrafts from
                # evidence already in its context instead of re-gathering it. Without it the
                # corrective prompt held only the question, the previous draft and the verifier's
                # findings — the retry re-searched and re-read the very pages it had just read, and
                # a corrective pass cost about as much as a first one (measured on staging: 7.1 s
                # median when the first draft verified, 17.3 s when it did not, on ~47% of asks).
                # `deps=ctx` stays the SAME object on purpose: evidence and surfaced paths
                # accumulate across both runs, so the verifier judges the retry against everything
                # the question gathered, not only what the second run happened to touch.
                result2 = await agent.run(feedback(question, out, verdict), deps=ctx,
                                          usage_limits=limits,
                                          message_history=result.all_messages())
            except UsageLimitExceeded:
                pass   # keep the first run's shipped outcome — same as "the retry did not improve"
            else:
                out2 = result2.output
                v2 = verify(out2, ctx.evidence_text(), self.brain.get_page, ctx.read_paths)
                if _RANK[v2["verdict"]] < _RANK[verdict["verdict"]]:  # the retry wins only if it improves
                    out, verdict = out2, v2

        return self._shape(question, out, verdict, retried, ctx.evidence_text(), ctx,
                           first_verdict=first_verdict)

    # ── response shaping + the strict gate ──────────────────────────────────
    def _shape(self, question: str, out, verdict: dict, retried: bool, evidence: str,
              ctx: SynthesisContext, *, first_verdict: dict) -> dict:
        if out.refused:
            return self._shape_refusal(question, out, verdict, retried, ctx,
                                       first_verdict=first_verdict)

        # Every human-readable channel that would ship is scanned, not just answer_markdown: a
        # figure fabricated inside a citation quote must be caught too.
        quote_figs = unverified_figures(" ".join(c.quote for c in out.citations), evidence)
        figs = _dedup(verdict["unverified_figures"] + quote_figs)
        v = _reverdict(figs, verdict["citation_problems"])
        # suppress on ANY untraced figure, or on a `failed` verdict (2+ problems — a citation-only
        # `failed` refuses too; only exactly-one citation-only problem ships, labeled `partial`).
        if figs or v["verdict"] == "failed":
            # Case selection is mechanical: which branch fired, never asked of the
            # model. `cited` — not the full `read_paths` — because the drafted answer may have
            # searched more broadly than it ultimately cited; naming exactly what it RELIED ON is
            # the more precise, more actionable fact.
            case = "suppressed_figures" if figs else "suppressed_citations"
            cited_paths = _dedup([c.path for c in out.citations])
            cited_titles = _titles_for(self.brain.get_page, cited_paths)
            reason = self._compose_reason(case, question, ctx.searched, cited_titles)
            return self._refusal(question, retried, v, suppressed=True, reason=reason,
                                 refusal_case=case,
                                 searched=_shippable_queries(question, ctx.searched),
                                 surfaced=cited_titles, first_verdict=first_verdict)
        return {
            "question": question,
            "refused": False,
            "answer_markdown": out.answer_markdown,
            "reason": "",
            "citations": [{"path": c.path, "quote": c.quote} for c in out.citations],
            "confidence": out.confidence,
            "verdict": v,
            "first_verdict": first_verdict,
            "retried": retried,
            "suppressed": False,
            "built_at": self._built_at(),
        }

    def _shape_refusal(self, question: str, out, verdict: dict, retried: bool,
                       ctx: SynthesisContext, *, first_verdict: dict) -> dict:
        """A genuine model refusal (`out.refused`). The model's own explanation does not ship at
        all, which makes the older defense (scan `out.reason` for a smuggled figure, replace with a
        neutral template if found) architecturally unreachable: there is nothing of the model's
        left to scan, because `reason` is composed here from `ctx.searched`/`ctx.read_paths_order`
        alone, which the ACL-scoped tools populated, never from the model."""
        case = "no_surface" if not ctx.read_paths_order else "no_match"
        surfaced = _titles_for(self.brain.get_page, list(ctx.read_paths_order))
        reason = self._compose_reason(case, question, ctx.searched, surfaced)
        return self._refusal(question, retried, verdict, suppressed=False, reason=reason,
                             refusal_case=case,
                             searched=_shippable_queries(question, ctx.searched),
                             surfaced=surfaced, first_verdict=first_verdict)

    def _shape_budget_refusal(self, question: str, ctx: SynthesisContext) -> dict:
        """`UsageLimitExceeded` on the agent's FIRST `agent.run()` call: no `AnswerOutput` ever
        existed, so there is nothing for `verify_answer.verify` to judge —
        this is a genuine refusal, same shape and same composer as every other one
        (`run_facts_reason`'s `budget_exceeded` case), built from `ctx`'s own recorded facts
        (whatever the run searched/read before it ran out of budget), never from the model,
        because the model never returned anything this run.

        `retried` is always False: `ask` calls this before it ever sets that flag True, because
        the corrective retry is never spent on a run that already exhausted the budget."""
        verdict = _reverdict([], [])   # vacuously verified — no drafted answer ever existed to distrust
        surfaced = _titles_for(self.brain.get_page, list(ctx.read_paths_order))
        reason = self._compose_reason("budget_exceeded", question, ctx.searched, surfaced)
        return self._refusal(question, False, verdict, suppressed=False, reason=reason,
                             refusal_case="budget_exceeded",
                             searched=_shippable_queries(question, ctx.searched),
                             surfaced=surfaced,
                             # No draft ever existed to judge, so there is no first verdict —
                             # `None`, never a synthesized `verified`, which would read in the
                             # column as "the first attempt was clean" for a run that never
                             # produced an attempt at all.
                             first_verdict=None)

    def _compose_reason(self, case: str, question: str, searched: list[str],
                        surfaced: list[str]) -> str:
        """`run_facts_reason` plus a defensive backstop, belt-and-suspenders: the composed
        sentence's only variables are query text (the asker's own words, and ONLY those —
        `run_facts_reason` never quotes a recorded query that isn't itself a substring of
        `question`) and page titles
        (frontmatter facts), neither of which should ever be a numeral the verifier would flag —
        but a title that happens to contain one is a cheap-to-imagine edge case not worth trusting
        blindly. If the scan ever fires, fall back to the safest, most generic honest sentence
        rather than shipping a hand-built exception message.

        **Scanned against `question`, never `evidence`.** A composed reason may only contain
        figures that were either already in the asker's question (safe, checked) or came from a
        page title (still caught here). Scanning the evidence text instead would widen that to
        anything any tool returned this run, which is a far weaker claim for a sentence the server
        asserts in its own voice.

        This used to be load-bearing for a second reason: the tool renderers echoed their own
        argument on the absence path, so a figure in a model-chosen query entered evidence and
        would have been "traced" by construction. That channel is closed at the source now
        (`answer/brain.py`'s NO_RESULTS/UNKNOWN_PAGE/UNKNOWN_ENTITY carry no argument), so this
        is defense in depth rather than the only thing standing between a steered query and a
        verified figure. Do not reopen either half on the grounds that the other exists."""
        reason = run_facts_reason(case, question, searched, surfaced)
        if unverified_figures(reason, question):
            log.warning("refusal composer: composed reason failed its own defensive figure scan "
                       "(case=%s) — falling back to the generic no-surface sentence", case)
            return run_facts_reason("no_surface", question, [], [])
        return reason

    def _refusal(self, question, retried, verdict, *, suppressed, reason,
                refusal_case: str = "", searched: list[str] | None = None,
                surfaced: list[str] | None = None, first_verdict: dict | None = None) -> dict:
        """The refusal shape. `refusal_case`/`searched`/`surfaced` are ADDITIVE: the
        sentence (`reason`) is for a person, these three are the same facts for a client that wants
        them structurally rather than parsed out of prose — the same posture this codebase already
        takes for `reason_code` beside a rejection sentence.

        `first_verdict` is additive on the same terms, and carries the same content as `verdict`
        (the problem STRINGS) — `audit_summary` is what reduces both to counts before either
        reaches a log column. `None` only on the budget-refusal path, where no draft existed."""
        return {
            "question": question,
            "refused": True,
            "answer_markdown": "",
            "reason": reason,
            "citations": [],
            "confidence": "low",
            "verdict": verdict,
            "first_verdict": first_verdict,
            "retried": retried,
            "suppressed": suppressed,
            "refusal_case": refusal_case,
            "searched": searched or [],
            "surfaced": surfaced or [],
            "built_at": self._built_at(),
        }

    def _built_at(self):
        meta = store.read_meta(self.brain.service.conn) or {}
        return meta.get("built_at")


def _verdict_shape(verdict: dict) -> dict:
    """`verdict` travels into `audit_log.result` as SHAPE, never as the lists of problem STRINGS
    `verify_answer.verify`/`_reverdict` compute for the answering loop's own internal use (the
    retry feedback, the strict gate's figure scan). Those strings are drafted-answer text by
    construction — `check_citations` embeds up to 80 characters of the drafted citation quote
    itself (`f"citation quote not found in {path}: {quote[:80]!r}"`), and `unverified_figures` is
    the list of untraced figures verbatim. Writing `result["verdict"]` wholesale lands that text in
    the JSONB column on the ROUTINE partial/suppressed path — no attacker needed, and invisible to
    a test that greps for a distinctive QUESTION string, because the leak is ANSWER text.

    The summary wants a verdict; the column must carry no drafted-answer content. Counts satisfy
    both, and they are everything the pilot report needs (`answer_shape` reads
    `refused`/`citations` for its percentages, never the verdict's own internals), so
    `unverified_figures`/`citation_problems` ship as COUNTS."""
    return {
        "verdict": verdict["verdict"],
        "unverified_figures": len(verdict.get("unverified_figures") or ()),
        "citation_problems": len(verdict.get("citation_problems") or ()),
    }


def audit_summary(result: dict) -> dict:
    """`ask`'s `audit_log.result` summary: `{refused, suppressed, verdict, citations, retried}` —
    the per-tool outcome SHAPE the pilot report reads (% answered-with-citation vs honest refusal),
    never a transcript.

    The ONE definition both callers of `service.call_async("ask", ..., summarize=audit_summary)`
    share (`mcp_server.py`'s `ask` tool and `slack.mention._run_ask`), so the two transports cannot
    describe the same outcome differently.

    `citations` is a COUNT, and it used to be the list of paths. The argument for the list was
    that "a path is the same identifying fact `verdict` and `surfaced` already carry — no new
    disclosure", and it was wrong in the way this whole column is designed against: a citation
    `path` is MODEL-authored, not corpus-authored. Nothing here checks that the value is a path
    the run actually read — an unresolvable citation is a citation *problem*, not a dropped
    citation, so it is logged either way — which made up to `MAX_CITATIONS` free-text fields a
    steered model could write a transcript into, in the one column whose whole contract is that it
    carries none. `Citation.path`'s own `max_length` bounds each one, but a bound is a narrower
    channel, not a closed one.

    A count closes it, and costs nothing that is read: `pilot_report.answer_shape` tests
    `r.get("citations")` for TRUTH — "did this answer cite anything" — and `0`/`n` answers that
    exactly as `[]`/`[…]` did. It is the same reduction `_verdict_shape` makes one field over, for
    the same reason, and this module had already made it once without applying it here.

    `verdict` is `_verdict_shape`'s COUNT-only rendering, not `result["verdict"]` verbatim — see
    that function's docstring for why the verbatim dict is itself a transcript leak.

    `first_verdict` is the same rendering of the FIRST draft's verdict, and it is the only column
    that can say what a retry was FOR. `verdict` alone cannot: it is the shipped one, so a retried
    ask that ended clean and one that never needed a retry are indistinguishable in it. The two
    first-draft cases are worth opposite amounts — an untraced figure means the strict gate would
    have suppressed the answer entirely (the retry bought the answer), while a single citation
    problem ships as `partial` regardless (the retry bought a label and an accurate quote) — and at
    ~41 % of asks and ~6.8 s each, which one dominates decides whether the retry path deserves more
    work or less. `None` when no draft existed (the budget refusal).
    """
    first = result["first_verdict"]
    return {
        "refused": result["refused"],
        "suppressed": result["suppressed"],
        "verdict": _verdict_shape(result["verdict"]),
        "first_verdict": _verdict_shape(first) if first else None,
        "citations": len(result.get("citations") or []),
        "retried": result["retried"],
    }
