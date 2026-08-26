"""AnswerService — the answering loop over `BrainService`, plus the strict verdict gate.

An agent gathers evidence, a deterministic verifier judges figures and citations, exactly one
corrective retry is allowed, and the verdict travels with the answer. The STRICT GATE sits after
verification, outside `verify()`: any untraced figure in a channel that would ship (the answer or
a citation quote) suppresses the WHOLE answer into a refusal carrying the findings; a
citation-only problem ships labeled `partial`; a `failed` verdict (2+ problems) never ships.

A refusal's shipped `reason` is composed ENTIRELY by this module (`run_facts_reason`) from facts
the server recorded this run, never from model text. It can never assert what the brain contains,
because it is handed nothing about the brain to assert from. `ask` runs under the server process
identity — no per-call identity parameter a client could spoof.
"""
import asyncio
import logging

from psycopg.errors import QueryCanceled

from stigmergy.answer.brain import AnswerBrain
from stigmergy.answer.numbers import unverified_figures
from stigmergy.answer.synthesize import (
    SynthesisContext,
    answer_limits,
    build_evidence_synthesizer,
    build_synthesizer,
)
from stigmergy.answer.verify_answer import feedback, verify
from stigmergy.index import store
from stigmergy.server.service import neutralize_fence

log = logging.getLogger(__name__)

_RANK = {"verified": 0, "partial": 1, "failed": 2}
_EVIDENCE_PAGE_CAP = 3
ANSWER_PHASE_TIMEOUT_S = 90
TOTAL_ANSWER_TIMEOUT_S = ANSWER_PHASE_TIMEOUT_S * 2


def _dedup(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))          # order-preserving unique


def _phase_timeout(deadline: float) -> float:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError
    return min(ANSWER_PHASE_TIMEOUT_S, remaining)


def _reverdict(figs: list[str], citation_problems: list[str]) -> dict:
    """Re-verdict over the FULL shipped-channel problem set, same thresholds as `verify()`."""
    n = len(figs) + len(citation_problems)
    label = "verified" if n == 0 else ("partial" if n == 1 else "failed")
    return {"verdict": label, "unverified_figures": figs, "citation_problems": citation_problems}


def strict_gate_findings(out, verdict: dict, evidence: str) -> tuple[list, dict]:
    """The strict gate's arithmetic, in ONE place for its two readers — `_shape` (what ships) and
    `ask` (whether to retry). Scans every human-readable channel that would ship, citation quotes
    included, and re-verdicts over the full figure set."""
    quote_figs = unverified_figures(" ".join(c.quote for c in out.citations), evidence)
    figs = _dedup(verdict["unverified_figures"] + quote_figs)
    return figs, _reverdict(figs, verdict["citation_problems"])


_SUPPRESSES = 2   # `_ship_rank`'s refusal tier — the only tier the corrective retry is spent on


def _ship_rank(figs: list, gated: dict) -> int:
    """The gate's outcome as an order: 0 ships clean, 1 ships `partial`, 2 refuses (any untraced
    figure, or a `failed` verdict). The retry trigger, the retry-wins comparison and `_shape` all
    read this, so they cannot disagree about what would ship."""
    if figs or gated["verdict"] == "failed":
        return _SUPPRESSES
    return _RANK[gated["verdict"]]


def _usage_facts(u) -> dict:
    """Token COUNTS only: the audit column this feeds carries no transcript by contract."""
    return {"requests": int(getattr(u, "requests", 0) or 0),
            "input_tokens": int(getattr(u, "input_tokens", 0) or 0),
            "cache_read_tokens": int(getattr(u, "cache_read_tokens", 0) or 0),
            "output_tokens": int(getattr(u, "output_tokens", 0) or 0)}


def _add_usage(facts: dict, u) -> dict:
    """The retry's spend, folded in whether or not the retry wins."""
    more = _usage_facts(u)
    return {k: facts[k] + more[k] for k in facts}


# ── the refusal's shipped prose — composed from server-recorded facts, never the model ─────────
_NAMED_ITEMS_CAP = 3   # name up to 3 items, "and N more" past that


def _list_clause(verb: str, items: list[str]) -> str:
    """The ONE joining shape both `searched` (quoted queries) and `surfaced` (titles) use."""
    n = len(items)
    if n == 0:
        return ""
    if n <= _NAMED_ITEMS_CAP:
        head, last = items[:-1], items[-1]
        body = (", ".join(head) + " and " + last) if head else last
        return f"{verb} {body}"
    head = ", ".join(items[:_NAMED_ITEMS_CAP])
    return f"{verb} {head} and {n - _NAMED_ITEMS_CAP} more"


_QUERY_CAP = 200   # cap on shipped query text/titles — defense in depth beside the substring check


def _shippable_queries(question: str, searched: list[str]) -> list[str]:
    """Which recorded queries may be quoted VERBATIM into a refusal: only those that are a
    case-insensitive substring of the asker's OWN `question`. `ctx.searched` is agent-authored and
    the agent is steerable by hostile page content, and neutralizing a fence token leaves a
    planted sentence persuasive as PROSE. Everything else is folded into a count by the caller."""
    q_norm = (question or "").lower()
    return [neutralize_fence(q)[:_QUERY_CAP] for q in searched if q and q.lower() in q_norm]


def _searched_clause(question: str, searched: list[str]) -> str:
    """`searched "a" and "b"` for the asker's own words; the rest become a bare count."""
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
    """"it" for one cited page, "them" for several, `""` for ZERO — a suppressed refusal can
    genuinely cite nothing, and `""` tells the caller to drop the pronoun clause."""
    if n == 0:
        return ""
    return "it" if n == 1 else "them"


def _join_lead(*clauses: str) -> str:
    """Join non-empty lead clauses with ', '; an all-empty lead is reachable."""
    return ", ".join(c for c in clauses if c)


def run_facts_reason(case: str, question: str, searched: list[str], surfaced: list[str]) -> str:
    """THE composer for every refusal's shipped `reason` — one function, five cases, no model
    input. Every sentence states only what ran and came back THIS turn, never what the brain
    contains: a semantic claim no deterministic verifier can check.

    `case`: `"no_surface"` (nothing came back from any tool), `"no_match"` (pages surfaced, none
    answered), `"budget_exceeded"` (tool budget hit before any `AnswerOutput` existed),
    `"suppressed_figures"` and `"suppressed_citations"` (a draft withheld by the strict gate).
    """
    lead = _join_lead(_searched_clause(question, searched), _surfaced_clause(surfaced))

    if case == "no_surface":
        if not searched:
            # the agent always searches first, but a composer must not crash
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
    """Page titles via `AnswerBrain.get_page` (already ACL-scoped). Never falls back to the path,
    which could name something outside the asker's view — a placeholder stands in. Neutralized and
    capped in this ONE function, so `reason` and the structured `surfaced` field both inherit it:
    MCP consumers read these as raw SERVER prose."""
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
        """`settings` carries the answer model policy, defaulting to the service's own."""
        self.brain = AnswerBrain(service)
        self.settings = settings if settings is not None else service.settings

    async def ask(self, question: str) -> dict:
        # `UsageLimitExceeded` is the ONE pydantic_ai exception caught here; imported inside the
        # method so a monkeypatched double can raise it whichever backend is configured.
        from pydantic_ai.exceptions import UsageLimitExceeded
        agent = build_synthesizer(self.settings)
        # the fake path never needs (nor constructs) pydantic_ai's UsageLimits
        limits = None if self.settings.llm == "fake" else answer_limits()
        ctx = SynthesisContext(service=self.brain)
        deadline = asyncio.get_running_loop().time() + TOTAL_ANSWER_TIMEOUT_S
        try:
            timeout = _phase_timeout(deadline)
            result = await asyncio.wait_for(
                agent.run(question, deps=ctx, usage_limits=limits),
                timeout=timeout,
            )
        # `QueryCanceled` joins the two budget exceptions because a serving connection's statement
        # deadline is the same KIND of event: the run died before an `AnswerOutput` existed. It
        # reaches here from a tool call the agent made, so `ctx` still holds whatever earlier tools
        # returned, and the recovery below closes over exactly that.
        except (UsageLimitExceeded, TimeoutError, QueryCanceled):
            completed = await self._complete_budget_exhaustion(question, ctx, deadline)
            if completed is not None:
                return completed
            shaped = self._shape_budget_refusal(question, ctx)
            shaped["usage"] = None   # the run died mid-flight; there is no usage object to read
            return shaped
        out = result.output
        usage = _usage_facts(result.usage)
        evidence = ctx.evidence_text()
        verdict = verify(out, evidence, self.brain.get_page, ctx.read_paths)
        # The FIRST draft's verdict, kept whatever happens next — nothing else records what a
        # retry was for once `verdict` is rebound to the retry's.
        first_verdict = verdict

        # The corrective retry runs ONLY when the strict gate would suppress the draft as it
        # stands: an untraced figure (citation-quote scan included) or a `failed` verdict. A lone
        # citation problem ships `partial` either way, so no second run is spent on it. Reading
        # the GATE's scan rather than the raw verdict keeps the trigger and the gate agreeing on
        # "would suppress", and names a quote-fabricated figure in the corrective brief.
        retried = False
        figs, gated = ((), verdict) if out.refused else \
            strict_gate_findings(out, verdict, evidence)
        # `not out.refused` is STRUCTURAL, not redundant: a refusal is an answer, never a defect
        # to repair — the guard holds even if a future verifier learns to fail one.
        if not out.refused and _ship_rank(figs, gated) == _SUPPRESSES:
            retried = True
            try:
                # The retry carries the first run's MESSAGE HISTORY so the model redrafts from
                # evidence already in context. `deps=ctx` stays the SAME object: evidence and
                # surfaced paths accumulate across both runs. `gated`, not the raw verdict — a
                # quote-fabricated figure is invisible to the raw one.
                timeout = _phase_timeout(deadline)
                result2 = await asyncio.wait_for(
                    agent.run(
                        feedback(question, out, gated),
                        deps=ctx,
                        usage_limits=limits,
                        message_history=result.all_messages(),
                    ),
                    timeout=timeout,
                )
            except (UsageLimitExceeded, TimeoutError, QueryCanceled):
                # Keep the first run's outcome. The killed retry's spend is unrecoverable (the
                # exception carries no usage object), so `usage` undercounts exactly this case.
                pass
            else:
                usage = _add_usage(usage, result2.usage)   # spent whether or not the retry wins
                out2 = result2.output
                retry_evidence = ctx.evidence_text()
                v2 = verify(out2, retry_evidence, self.brain.get_page, ctx.read_paths)
                figs2, gated2 = ((), v2) if out2.refused else \
                    strict_gate_findings(out2, v2, retry_evidence)
                # The retry wins only if it improves WHAT WOULD SHIP — the gate's rank, never the
                # raw verdicts', so no draft can win here and then lose at the gate.
                if _ship_rank(figs2, gated2) < _ship_rank(figs, gated):
                    out, verdict = out2, v2

        shaped = self._shape(question, out, verdict, retried, ctx.evidence_text(), ctx,
                             first_verdict=first_verdict)
        shaped["usage"] = usage
        return shaped

    async def _complete_budget_exhaustion(
        self, question: str, ctx: SynthesisContext, deadline: float
    ) -> dict | None:
        """Close one budget-exhausted run over a fixed, reader-scoped evidence set."""
        from pydantic_ai.exceptions import AgentRunError

        mark = ctx.mark()
        try:
            # A run whose model never reached a tool leaves nothing to close over; ONE scoped
            # search, run by the server rather than the model, is what makes this path possible.
            if not ctx.read_paths_order:
                ctx.record(self.brain.search_text(question, ctx))
            paths = tuple(ctx.read_paths_order[:_EVIDENCE_PAGE_CAP])
            if not paths:
                return None
            for path in paths:
                ctx.record(self.brain.page_text(path, ctx))
        except QueryCanceled:
            # The statement deadline cut the recovery itself, leaving a HALF-gathered ledger. The
            # refusal composed next may only report what the primary run established, so the
            # partial gathering is rewound rather than shipped as `searched`/`surfaced` facts.
            ctx.rewind(mark)
            log.warning("evidence recovery cancelled by the database statement deadline")
            return None
        try:
            timeout = _phase_timeout(deadline)
            result = await asyncio.wait_for(
                build_evidence_synthesizer(self.settings).run(
                    question,
                    evidence=ctx.evidence_text(),
                ),
                timeout=timeout,
            )
        except (AgentRunError, TimeoutError) as error:
            log.warning("evidence-only answer completion failed (%s)", error.__class__.__name__)
            return None
        out = result.output
        if out.refused:
            return None
        evidence = ctx.evidence_text()
        verdict = verify(out, evidence, self.brain.get_page, ctx.read_paths)
        figures, gated = strict_gate_findings(out, verdict, evidence)
        if figures or gated["verdict"] != "verified":
            return None
        shaped = self._shape(
            question,
            out,
            verdict,
            True,
            evidence,
            ctx,
            first_verdict=None,
        )
        shaped["usage"] = None
        return shaped

    # ── response shaping + the strict gate ──────────────────────────────────
    def _shape(self, question: str, out, verdict: dict, retried: bool, evidence: str,
              ctx: SynthesisContext, *, first_verdict: dict | None) -> dict:
        if out.refused:
            return self._shape_refusal(question, out, verdict, retried, ctx,
                                       first_verdict=first_verdict)

        figs, v = strict_gate_findings(out, verdict, evidence)
        # suppress on ANY untraced figure, or on a `failed` verdict (2+ problems); exactly one
        # citation-only problem ships, labeled `partial`
        if figs or v["verdict"] == "failed":
            # Case selection is mechanical, never asked of the model. Cited paths, not the full
            # `read_paths`: what the draft RELIED ON is the more precise fact.
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
        """A genuine model refusal; nothing of the model's ships."""
        case = "no_surface" if not ctx.read_paths_order else "no_match"
        surfaced = _titles_for(self.brain.get_page, list(ctx.read_paths_order))
        reason = self._compose_reason(case, question, ctx.searched, surfaced)
        return self._refusal(question, retried, verdict, suppressed=False, reason=reason,
                             refusal_case=case,
                             searched=_shippable_queries(question, ctx.searched),
                             surfaced=surfaced, first_verdict=first_verdict)

    def _shape_budget_refusal(self, question: str, ctx: SynthesisContext) -> dict:
        """Shape a primary-budget refusal when evidence-only completion cannot ship."""
        verdict = _reverdict([], [])   # vacuously verified: no drafted answer existed to distrust
        surfaced = _titles_for(self.brain.get_page, list(ctx.read_paths_order))
        reason = self._compose_reason("budget_exceeded", question, ctx.searched, surfaced)
        return self._refusal(question, False, verdict, suppressed=False, reason=reason,
                             refusal_case="budget_exceeded",
                             searched=_shippable_queries(question, ctx.searched),
                             surfaced=surfaced,
                             # None, never a synthesized `verified` reading as "first attempt clean"
                             first_verdict=None)

    def _compose_reason(self, case: str, question: str, searched: list[str],
                        surfaced: list[str]) -> str:
        """`run_facts_reason` plus a backstop over the structured facts it may ship.

        The composer names only question substrings, server-recorded counts, and ACL-filtered
        titles. Those facts are not model-authored answer figures, so the defensive scan must
        verify them against the structured facts rather than the question alone.
        """
        reason = run_facts_reason(case, question, searched, surfaced)
        refusal_facts = "\n".join((question, *surfaced, str(len(searched)), str(len(surfaced))))
        if unverified_figures(reason, refusal_facts):
            log.warning("refusal composer: composed reason failed its own defensive figure scan "
                       "(case=%s) — falling back to the generic no-surface sentence", case)
            return run_facts_reason("no_surface", question, [], [])
        return reason

    def _refusal(self, question, retried, verdict, *, suppressed, reason,
                refusal_case: str = "", searched: list[str] | None = None,
                surfaced: list[str] | None = None, first_verdict: dict | None = None) -> dict:
        """The refusal shape; `first_verdict` is `None` only on the budget path."""
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
    """`verdict` reaches `audit_log.result` as COUNTS, never the problem STRINGS: those are
    drafted-answer text by construction, and the column's contract is no transcript."""
    return {
        "verdict": verdict["verdict"],
        "unverified_figures": len(verdict.get("unverified_figures") or ()),
        "citation_problems": len(verdict.get("citation_problems") or ()),
    }


def audit_summary(result: dict) -> dict:
    """`ask`'s `audit_log.result` summary — an outcome shape, never a transcript. The ONE
    definition both transports share, so they cannot describe the same outcome differently.

    Everything free-text is reduced to a count: `citations` because a citation `path` is
    MODEL-authored, `verdict`/`first_verdict` through `_verdict_shape`. `first_verdict` is the
    only field that can say what a retry was FOR, and is `None` when no draft existed. `usage`
    sums both runs' token counts (`None` for the budget refusal).
    """
    first = result["first_verdict"]
    return {
        "refused": result["refused"],
        "suppressed": result["suppressed"],
        "verdict": _verdict_shape(result["verdict"]),
        "first_verdict": _verdict_shape(first) if first else None,
        "citations": len(result.get("citations") or []),
        "retried": result["retried"],
        "usage": result.get("usage"),
    }
