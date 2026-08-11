#!/usr/bin/env python3
"""Golden filing runner — quality at the ONE writer of the knowledge repo.

On-demand instrument (NOT wired into CI — CI stays keyless): drives `evals/filing/`'s ten golden
captures through the REAL filing path — `worker.process_next` over real Postgres, a real bare
remote, a real `git worktree`, the real eight gates and the knowledge repo's own contract linter —
against the frozen mini knowledge repo at `evals/filing/repo/`, and scores each outcome against an
expectation that names its facets one by one.

**Why it exists.** The repo already measured retrieval and the answer. It measured nothing about
FILING, which is the one place a model's judgment becomes a permanent commit in somebody's
knowledge repo. "Is backend X as good as Sonnet at filing?" was answered by reading pages by hand
and watching gate bounce-rates — enough to catch a disaster, blind to the gradual kind.

**Per-facet, never one number.** Each capture's expectation names the facets it is scored on, and
each facet keeps its own denominator (the same rule `run_qa._aggregate` follows for its three
axes): a backend that starts filing everything as a note must not be able to hide that behind a
rising anchor score. The facets:

  - status        the terminal queue status (`filed` / `needs_input` / `triage` / `rejected`)
  - reason        a refusal's own `reason_code`, for the states that carry one
  - type          the `type:` of the page that ACTUALLY landed, read back out of git
  - folder        where it landed
  - anchor        the page's server-stamped `entity:` — resolved REGISTRY IDS, never the agent's
                  spelling of a name, and `[]` (which the contract calls company-wide) is a real
                  answer this scorer distinguishes from a wrong entity
  - edits         which OTHER pages the commit changed — what `edits.apply_declared` performed
                  from the agent's declaration, scored by CONTAINMENT: the edit an existing page
                  was OWED has to be there, and a further one does not fail it (`_edits_match`)
  - park_question for a park: the unresolved name the question actually captured
  - decisions     for a meeting: one decision page per decision, each with its OWN anchor
  - reuse         for a meeting re-filed after a park: did the capture LOSE a decision on the way
                  back

ATTEMPTS, BOUNCES and COST carry no bar and gate nothing — they are this instrument's cost axes,
the same posture `run_qa.py` takes with retry rate and seconds/question. A backend that reaches
the same page in two agent passes instead of one is not worse at filing; it is more expensive at
it, and a quality axis that absorbed that would be measuring two things at once.

**Scoring is deterministic and there is no judge.** Every facet is a functional fact with one
spelling — a status, a registry id, a folder, a count — except the two title matchers, which are
deliberately loose (normalized word-subset, never a literal `in`). `evals/README.md` records what
a literal-word expectation cost the QA golden; this set does not repeat it.

  # keyless plumbing self-check (the offline double, NOT a measurement — appends no history row)
  python evals/run_filing.py --backend double

  # the real measurement (needs ANTHROPIC_API_KEY and the `claude` CLI on PATH)
  python evals/run_filing.py --backend sdk --model claude-sonnet-5 \
      --report evals/out/filing-sonnet-5.json

  # the structured flow, every capture, on the pydantic-ai backend (ADR 033)
  python evals/run_filing.py --backend pydantic \
      --model anthropic:claude-sonnet-5 --report evals/out/filing-structured.json

**A subset is a different measurement, and says so.** `--kinds` scores only the captures of the
named kinds, and everything downstream is recomputed from that subset rather than from the shipped
set: `_check_set` derives the per-facet denominators instead of holding them against
`EXPECTED_DENOMINATORS` (which pins the WHOLE set and only the whole set), and the history row
records `kinds` so a subset score can never be read later as a full-set one. Every backend runs
every kind now (ADR 033 lifted the meeting-only restriction on `--backend pydantic`), so a subset
is an ablation somebody chose rather than a limitation they worked around.

Both need the local Postgres (`make db-up`) and `gitleaks` on PATH — the secrets gate shells out
to the real scanner, and a filing eval that skipped it would be measuring a shorter pipeline than
the one that runs in production. Both are asked for BEFORE the first capture is claimed: `_run`
calls `worker.startup_checks`, the worker's own pre-flight, so a missing scanner, a missing Claude
credential or a malformed fixture is one loud line instead of a full table of `failed` rows with
the real cause buried under attempts-exhausted noise.

**What this runner deletes from its own environment before it starts.** `make` hands every target
the operator's gitignored env file (`-include .env` + `export`), and this is the first one that
drives `processing._file`. Two families of variable in that file silently redirect such a run at
something a person is using: the librarian App credential, which makes `githubapp.configured()`
true so that every filed capture mints a REAL installation token and pushes to
`github.com/<slug>` instead of this run's throwaway bare remote; and the LLM backend, which a
filed meeting's view regeneration would spend real money through. `_run` deletes the five App
variables and pins `$CLEAN_LLM` to the fake backend unconditionally, for BOTH backends — the same
structural defence `tests/conftest.py` applies to the whole suite and `scripts/e2e_isolate.sh` to
every e2e script, in the third place that needs it.

**One thing a filed meeting does that is not filing, and is not scored.** It triggers view
regeneration (`processing._file_meeting`, best-effort) against the fake backend pinned above: the
flow is exercised, nothing is spent, and it may push a SECOND commit on top of the meeting's. That
commit cannot move a score — every page here is read back at the sha in `result_ref`, which names
the meeting's OWN commit and is captured before the view step runs (`librarian/index.md` records
that contract, and reading the branch tip instead has bitten a test once).

**What the double legitimately misses.** `librarian/double.py` has no NLP: it files one
well-formed page per capture, always as a `note`, always anchored to the FIRST entity in the
registry, and it parks only on an explicit `DOUBLE:` directive — which this golden set contains
none of, on purpose. So `--backend double` scores 1.00 on the facets that are code's (status for
the deterministic refusals, the duplicate's `reason`) and well below it on every facet that is
judgment's (anchor, type, folder, edits, park_question, decisions). That is the correct reading of
the table, not a defect in it: those rows are the instrument reporting that its plumbing works and
that the thing it measures was not present.
"""
import argparse
import collections
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Self-pinned, like `run_qa.py` and `run_retrieval.py`: `src/` for the package under measurement,
# and the checkout ROOT because this runner drives the real filing path through
# `tests.librarian.support` — the same bare-remote-plus-clone rig every librarian integration test
# uses, imported rather than rewritten (`scripts/walk_*.py` already establish that reach). Running
# two checkouts side by side means running each one's OWN copy of this script, never this one with
# a different PYTHONPATH.
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

try:                                    # run as a script: evals/ is sys.path[0], sibling import
    import bars  # noqa: E402
    import eval_history  # noqa: E402
except ModuleNotFoundError:             # imported as `evals.run_filing` (the scorer unit tests)
    from evals import bars, eval_history  # noqa: E402

FIXTURE = ROOT / "evals" / "filing"

# Every facet this instrument knows, in the order the table prints them. Quality first, then the
# two cost counters — which are recorded per capture and reported, and carry no bar (see the module
# docstring).
QUALITY_FACETS = ("status", "reason", "type", "folder", "anchor", "edits",
                  "park_question", "decisions", "reuse")
COST_FACETS = ("attempts", "bounces")
FACETS = QUALITY_FACETS + COST_FACETS

# The per-facet denominators the shipped golden set produces — 10 captures, 12 scored phases.
# Pinned rather than derived, and checked by `_check_set` before a single model call is spent: a
# facet that quietly lost a denominator is a score that ROSE because a capture stopped being
# counted, which is the one failure the per-facet design cannot survive and the one nothing in a
# rendered table would show. Adding a capture therefore fails here first, on purpose — the new
# denominator is a decision to record in the same commit (`evals/README.md`'s growth protocol),
# not a number to discover later in a diff.
#
# The two cost facets sit at 8 rather than 12 because the two parking captures name no
# attempts/bounces expectation, while `agent_passes` in the report still sums the passes of all
# twelve phases. Those are different questions and deliberately different denominators.
#
# `edits` sits at 1 because exactly one capture is OWED an edit it can be held to (F03). The base
# case used to pin `edits: []` as well, which scored an assumption about how one backend would
# file rather than anything the brief requires — see `_edits_match` and F01's `why`.
EXPECTED_DENOMINATORS = {"status": 12, "reason": 1, "type": 9, "folder": 9, "anchor": 7,
                         "edits": 1, "park_question": 2, "decisions": 2, "reuse": 1,
                         "attempts": 8, "bounces": 8}

# Words carried by nearly every title in this domain; matching on them would let any two titles
# match each other. Deliberately tiny — a long stop list is a second yardstick nobody reviews.
_STOPWORDS = frozenset({"a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "at",
                        "by", "with", "is", "are", "be", "md", "wiki", "decisions", "notes",
                        "concepts", "meetings", "sources"})
_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")


# ── pure scoring: no Postgres, no git, no SDK, no model ────────────────────────────────────────
# Everything below this line is a function of data. `_run` builds the `observed` dicts from a real
# run and hands them here; a keyless test builds the same dicts by hand and gets identical scores.
# That is the whole seam, and it is why the heavy imports live inside `_run` rather than at module
# scope: `from evals import run_filing` costs nothing but the standard library.

def _words(text: str) -> frozenset:
    """The significant words of a title or a path, normalized for comparison.

    A pure tokenizer: it lowercases, splits and drops stopwords, and it does NOT fold morphology.
    The one fold this instrument makes lives in `_same_word` below, at the COMPARISON, so a token
    is still the token the title actually carries — a `_words` that rewrote its own output would be
    a stemmer wearing a tokenizer's name.
    """
    return frozenset(w for w in _WORD_SPLIT_RE.split((text or "").lower())
                     if w and w not in _STOPWORDS)


def _same_word(want: str, got: str) -> bool:
    """One token against one token: equal, or the same word in the other grammatical NUMBER.

    Exactly one fold and deliberately no more — a trailing `s`, in either direction. Not a stemmer,
    not a table, nothing that could be extended without somebody noticing: `tracked` and `tracking`
    are still two words here, and the yardstick's obligation to name uninflected content words is
    unchanged for every inflection except this one.

    **It is here because grammatical number turned out to be run-to-run NOISE, measured.** Three
    independent runs of the same code on the same model titled F08's review decision three ways —
    two singular ("…-for-review-…", scored PASS) and one plural ("…-reviews-…", scored FAIL) — with
    the anchors right every time. A 2-denominator facet flipping on whether a model wrote "review"
    or "reviews" is the instrument measuring the model's grammar, which is the same defect
    `globex-meeting-budget` records one series over, arrived at from the other direction: there an
    expectation demanded a literal word, here it demanded a literal ENDING.

    **What the narrowness does and does not buy, stated exactly** — the first draft of this comment
    overclaimed and the claim is the part that matters. It folds ONE suffix: a plural written `es`
    is not folded (`process`/`processes` is still a miss), and neither is any other inflection. What
    it cannot do is what a stem table does — reach `ed`, `ing`, `ies` or an irregular form, or grow
    a new entry without somebody editing this line.

    What it CAN do, and the reason this is a two-line rule rather than a one-line one: `a == b + "s"`
    also fires when a token happens to be another token plus `s` (`bus` and `bu`). That is only a
    false match if BOTH strings are words a title would carry, and a two-letter fragment is not —
    every expectation here is written in proper nouns and stable nouns. It is a residual, not an
    impossibility, and if one ever bites, the fix is the expectation rather than a longer rule.
    """
    return want == got or want == f"{got}s" or got == f"{want}s"


def title_matches(expected: str, candidate: str) -> bool:
    """Does `candidate` (a page path, or a title) carry the title `expected` names?

    **Word-subset, not a literal `in`, and that is a lesson this repo already paid for.**
    `evals/README.md` records `globex-meeting-budget`, a QA expectation demanding the literal word
    "budget" from an answer free to paraphrase around it — a yardstick defect that has distorted
    every entry in that series since. A decision the agent titles "Second wave sequencing for
    Northwind" is the same decision as one titled "Northwind second wave", and an instrument that
    scored the first a miss would be measuring the model's word order.

    So an expectation matches when every significant word it names appears in the candidate, in
    either grammatical number (`_same_word`). It is deliberately generous in one direction only: a
    candidate may say MORE than the expectation, and never less.

    **Beyond that one fold, `_words` does no stemming and will not grow any.** "tracked" and
    "tracking" are two words here, so the obligation sits on the yardstick instead: an expectation
    is written in the UNINFLECTED content words a paraphrase cannot drop — a proper noun plus a
    stable noun — and never in the verb form one particular run happened to produce. A stemmer
    would move that judgment into a table nobody reviews and would silently start matching words
    the expectation never meant. `evals/filing/expected/expectations.json`'s `_looseness` note
    states the same rule where the expectations are written, which is where it has to be obeyed.

    **This loosening is ONE-DIRECTIONAL, which is what keeps the series comparable** — a recorded
    score stays valid rather than needing a re-run to mean anything. The predicate is strictly
    weaker than the one it replaces, so every pairing that matched before matches now: a recorded
    PASS cannot become a FAIL, and only a FAIL can flip.

    **With one caveat, stated because it is real rather than hidden**: `_decisions_match` pairs
    expectations to pages GREEDILY and one-to-one, so a weaker predicate can in principle change
    which page a title claims and starve a later expectation — a PASS→FAIL the loosening itself
    cannot cause but the pairing can. That is precisely what
    `tests/evals/test_filing_golden_fixture.py::test_no_expected_decision_title_can_swallow_a_later`
    `_ones_page` exists to prevent, and it calls THIS function — so widening the match widened that
    guard's net in the same commit. The property is preserved by the guard, not by the fold alone.
    """
    want = _words(expected)
    got = _words(candidate)
    return bool(want) and all(any(_same_word(w, g) for g in got) for w in want)


def _anchor_matches(expected: dict, observed: dict) -> bool:
    """An anchor is `{kind, ids}` and both halves have to agree.

    What each half actually carries, since the names invite a stronger reading than the code
    supports. `ids` is the load-bearing comparison: the resolved registry ids stamped on the page.
    `kind` is DERIVED from those ids for a page that was read back (`_anchor_from_page`) — ids means
    `entity`, no ids means `company` — so on the filed road it discriminates nothing `ids` does not.
    It earns its place at the two edges instead: `gate_anchoring` upstream is what makes an empty
    `entity:` on a committed page mean the company-wide outcome rather than a failed resolution,
    and `_page_anchor` answers `unreadable` — a kind no expectation names — for a page that could
    not be read back at all, so an instrument fault can never score as a company-wide filing.
    """
    return (str(expected.get("kind", "")) == str(observed.get("kind", ""))
            and sorted(expected.get("ids") or []) == sorted(observed.get("ids") or []))


def _edits_match(expected: list, observed: list) -> bool:
    """Every edit the expectation names was performed. Observed EXTRAS do not fail the facet.

    **Containment, not equality**, and the asymmetry is the whole design of this facet.

    *An extra edit cannot damage a page, by construction.* `edits.validate` confines a declared edit
    to a page that already exists, sits in one of the fast lane's folders and was not created by
    this capture; what is then written is a `related:` link or a callout — additive by construction
    (`page.with_related_link`, `page.with_callout`) — and `gate_zone` and `gate_body_rewrite` judge
    the resulting diff exactly as they judge the new page. A backend that cross-links more generously
    than the yardstick anticipated has therefore done something already proved harmless, and marking
    it down would score the yardstick's imagination. What this facet asserts is the other direction:
    the edit the material OWED an existing page actually happened.

    *The corpus grows while the run happens.* The captures are filed one after another into ONE
    throwaway repo, the way a real knowledge repo evolves, so a later capture may legitimately
    backlink a page an earlier capture in the same run created — a path no expectation written
    against the frozen fixture could name. Under equality that reads as a miss, and the facet would
    be scoring run order. Both directions were measured on the first Sonnet-5 baseline, before any
    number was recorded; F01's and F03's `why` notes in `expected/expectations.json` record it.

    One consequence to hold on to when writing an expectation: under containment `edits: []` is
    vacuously TRUE for every backend and still fills the denominator, which is a facet that reads as
    measured and measures nothing. "This capture owes no edit" is said by NAMING NO `edits` KEY —
    silence is not scored, which is the rule `score_phase` keeps for every facet.
    """
    return set(expected) <= set(observed)


def _decisions_match(expected: list, observed: list) -> bool:
    """One decision page per expected decision, each matched loosely by title and exactly by
    anchor, with no page left over on either side.

    The count is part of the check. A meeting that produced five decision pages where two were
    expected has not over-delivered — it has split one decision into fragments that each anchor
    separately, which is the granularity failure the meeting brief spends a whole section on.
    Matching is greedy and one-to-one, so two expected titles cannot both be satisfied by the same
    page.
    """
    if len(expected) != len(observed):
        return False
    remaining = list(observed)
    for want in expected:
        hit = None
        for candidate in remaining:
            if not title_matches(want.get("title", ""), candidate.get("path", "")):
                continue
            if "anchor" in want and not _anchor_matches(want["anchor"],
                                                        candidate.get("anchor") or {}):
                continue
            hit = candidate
            break
        if hit is None:
            return False
        remaining.remove(hit)
    return True


def _park_question_matches(expected: list, observed: list) -> bool:
    """Every name the expectation says should have been asked about was asked about.

    Matched loosely against the whole question, because the report renders one sentence naming all
    of them and a scorer that demanded the list shape would be scoring `report.py`'s wording rather
    than whether the right thing was unresolved.
    """
    joined = " ".join(str(name) for name in (observed or []))
    return bool(expected) and all(title_matches(name, joined) for name in expected)


def score_phase(expect: dict, observed: dict) -> dict:
    """`{facet: bool}` for exactly the facets `expect` NAMES — nothing else.

    A facet the expectation is silent about is absent from the result: never `False`, never
    counted, and never quietly folded into some other facet's denominator. That is what makes the
    denominators in the table mean what they say, and it is why a capture can be added to the
    golden set to probe one facet without diluting every other one.
    """
    out: dict = {}
    if "status" in expect:
        out["status"] = observed.get("status") == expect["status"]
    if "reason" in expect:
        out["reason"] = observed.get("reason") == expect["reason"]
    if "type" in expect:
        out["type"] = observed.get("type") == expect["type"]
    if "folder" in expect:
        out["folder"] = observed.get("folder") == expect["folder"]
    if "anchor" in expect:
        out["anchor"] = _anchor_matches(expect["anchor"], observed.get("anchor") or {})
    if "edits" in expect:
        out["edits"] = _edits_match(expect["edits"], observed.get("edits") or [])
    if "park_question" in expect:
        out["park_question"] = _park_question_matches(expect["park_question"],
                                                      observed.get("park_question") or [])
    if "decisions" in expect:
        out["decisions"] = _decisions_match(expect["decisions"], observed.get("decisions") or [])
    if "reuse" in expect:
        # The scored half is "did the capture keep what it had distilled". Whether the re-file
        # SPENT a model pass to keep it rides along in `observed` and is reported, never scored —
        # see `_observe_reuse` for why that asymmetry is the honest one.
        want = bool(expect["reuse"].get("decisions_preserved", True))
        out["reuse"] = bool((observed.get("reuse") or {}).get("preserved")) == want
    if "attempts" in expect:
        out["attempts"] = observed.get("attempts") == expect["attempts"]
    if "bounces" in expect:
        out["bounces"] = observed.get("bounces") == expect["bounces"]
    return out


def aggregate(phases: list, *, backend: str, model: str, wall_s: float,
              kinds: list | None = None) -> dict:
    """Per-facet hits and denominators over every scored phase, plus the cost axes.

    `phases` is `[{id, phase, expect, observed, facets}, ...]` — one entry per scored moment, so a
    parking capture contributes TWO (the park, and the re-file after the reply) and its facets are
    counted where they actually happened.

    `kinds` is which capture kinds this run actually measured. It rides in the report — and from
    there into the history row — because a per-facet score is only comparable against another score
    over the SAME set: a `--kinds meeting` run scores three phases where the shipped set scores
    twelve, and two rows that look alike and measured different sets is the one failure a series
    read years later cannot recover from.
    """
    facets: dict = {}
    for entry in phases:
        for name, ok in entry["facets"].items():
            row = facets.setdefault(name, {"hits": 0, "of": 0})
            row["of"] += 1
            row["hits"] += bool(ok)
    for name, row in facets.items():
        row["score"] = row["hits"] / row["of"] if row["of"] else 0.0
        bar = bars.FILING_BARS.get(name)
        row["bar"] = bar
        # A bar of None means REPORT, DO NOT JUDGE. It is the honest state for a facet whose
        # baseline has not been recorded yet, and it must never read as a pass — an instrument
        # that answers "fine" before it has ever been calibrated is worse than one that says
        # nothing (`evals/README.md`, how baselines are fixed).
        row["pass"] = None if bar is None else row["score"] >= bar
    costs = [p["observed"].get("cost_usd", 0.0) for p in phases]
    attempts = [p["observed"].get("attempts", 0) for p in phases]
    return {
        "backend": backend,
        "model": model,
        "kinds": sorted(kinds or []),
        "facets": facets,
        "total_cost_usd": round(sum(costs), 6),
        "agent_passes": sum(attempts),
        "wall_s": round(wall_s, 2),
        "counts": {"captures": len({p["id"] for p in phases}), "phases": len(phases)},
        "phases": phases,
    }


def render(report: dict) -> str:
    """The per-facet table. One row per facet, its own denominator beside it, and every miss named
    — a score with no misses listed is a number nobody can act on."""
    # `--backend double` calls no model at all, so naming one in the caption would put a model's
    # name on a table it never touched — and a table is exactly the thing somebody screenshots and
    # quotes later as "what Sonnet scored". The JSON report and the history row keep `model` as the
    # setting it was (the double appends no row at all); only the human-facing line changes.
    identity = (f"backend `{report['backend']}` (no model)" if report["backend"] == "double" else
                f"backend `{report['backend']}` · model `{report['model']}`")
    # Which kinds were measured, IN THE CAPTION, for the same reason the history row carries them:
    # this table is the thing somebody screenshots, and a per-facet score over three phases must
    # never be quotable as the shipped set's.
    kinds = report.get("kinds") or []
    measured = f" · kinds `{', '.join(kinds)}`" if kinds else ""
    lines = [f"# golden filing — {identity}{measured}", ""]
    width = max((len(name) for name in report["facets"]), default=6)
    for name in FACETS:
        row = report["facets"].get(name)
        if row is None:
            continue
        verdict = ("      " if row["bar"] is None else
                   ("PASS" if row["pass"] else "FAIL"))
        bar = "  (no bar — baseline not yet fixed)" if row["bar"] is None else \
              f"  ({verdict} vs {row['bar']:.2f} bar)"
        kind = "cost" if name in COST_FACETS else "    "
        lines.append(f"  {kind} {name:<{width}}  {row['score']:.2f}  "
                     f"[{row['hits']}/{row['of']}]{bar}")
    lines += ["",
              f"  total cost   ${report['total_cost_usd']:.4f} over {report['agent_passes']} "
              f"agent pass(es)",
              f"  wall clock   {report['wall_s']:.1f}s "
              f"({report['counts']['captures']} captures, {report['counts']['phases']} scored "
              f"phases)"]
    misses = [(p, name) for p in report["phases"]
              for name, ok in p["facets"].items() if not ok]
    if misses:
        lines += ["", "misses:"]
        for phase, name in misses:
            expected = phase["expect"].get(name)
            got = phase["observed"].get(name)
            lines.append(f"  {phase['id']} [{phase['phase']}] {name}: "
                         f"expected {json.dumps(expected, ensure_ascii=False)}, "
                         f"got {json.dumps(got, ensure_ascii=False, default=str)}")
    return "\n".join(lines)


def _history_metrics(report: dict, phases: list, provenance: dict) -> dict:
    """The `metrics` dict a real-instrument run hands `eval_history.append_run` — pulled out of
    the append site so a typo in a key name is a keyless test's problem, never something that
    first shows up in the durable `evals/history.ndjson` artifact a paid SDK run writes to.

    A pure function of what the caller already has in hand: `report` (`aggregate`'s own output,
    for `backend`/`model`/`total_cost_usd`/`agent_passes`/`wall_s`/`facets`), `phases` (for
    `_status_counts`), and `provenance` — the caller resolves `eval_history.corpus_provenance`
    itself and hands in the result, since THAT does read `PROVENANCE.json` and shell out to git.
    No I/O and no environ in here, so `from evals import run_filing` keeps costing nothing but the
    standard library.
    """
    return {"backend": report["backend"], "model": report["model"],
            # Which capture kinds this row measured. Present on every row from now on, so an older
            # row without it is visibly older rather than ambiguously a subset — and so a
            # `--kinds meeting` score can never be read as the shipped set's.
            "kinds": report.get("kinds") or [],
            "facets": {name: row["score"] for name, row in report["facets"].items()},
            "counts": {name: {"hits": row["hits"], "of": row["of"]}
                       for name, row in report["facets"].items()},
            # What the twelve phases actually ENDED as. A row whose facets look
            # ordinary but whose statuses say `triage: 9` is a run to distrust, and
            # without this the reader would have to still have the report to know.
            "statuses": _status_counts(phases),
            "total_cost_usd": report["total_cost_usd"],
            "agent_passes": report["agent_passes"],
            "wall_s": report["wall_s"],
            **provenance}


# ── observation: turning one real Result into the flat dict the scorer reads ───────────────────
def _folder_of(page_path: str) -> str:
    return os.path.dirname(page_path or "")


def _anchor_from_page(frontmatter: dict) -> dict:
    """`{kind, ids}` read off the FILED page rather than off the report's prose.

    The page's `entity:` is what `processing._stamp` wrote from `gates.resolve_entity_ids` — the
    same resolution `gate_anchoring` verified — so it is the registry's own ids and cannot carry
    the agent's spelling of a name. `report["anchored_to"]` is a rendered sentence for a human and
    would make this scorer depend on `report.py`'s wording.

    Only ever called with frontmatter that was genuinely READ (`_page_anchor` answers for the
    unreadable case itself), which is what makes the empty-list branch a company-wide claim rather
    than an absence of evidence.
    """
    raw = frontmatter.get("entity")
    ids = [raw] if isinstance(raw, str) and raw else list(raw or [])
    ids = [str(i) for i in ids if str(i)]
    return {"kind": ("entity" if ids else "company"), "ids": sorted(ids)}


def _observe_reuse(report: dict, attempts: int) -> dict:
    """What a meeting's re-file after a park did to the distillation the park had made.

    Three observables, and only the first is SCORED:

    * `preserved` — no decision the parked pass had distilled went missing. This is the property
      the whole reuse mechanism exists to protect ("a park must not cost knowledge"), and it is
      true or false on both roads back from a park.
    * `reused` — the parked distillation was re-filed verbatim, with no model call.
    * `redistilled` — this pass spent agent passes.

    The last two are reported and not scored, deliberately. Whether a park stores a reusable
    distillation at all is decided by HOW it parked: `processing._with_park_outcome` keeps one only
    when the agent decided to FILE and the gates then vetoed the anchor. An agent following the
    meeting brief correctly parks with `decision: "triage"` instead — storing nothing, so its
    re-file legitimately re-reads the transcript. Scoring `reused` would therefore mark the
    brief-following backend down for following the brief, which is exactly the over-fitting this
    golden set's own known-risks section warns about. What must not happen either way is losing a
    decision, and that is what `preserved` measures.

    Whether there was anything to lose in the first place is recorded beside this block by
    `_reuse_at_risk`, which is what keeps a `reuse` column of 1.00 readable.
    """
    reuse = report.get("distillation_reuse") or {}
    if not reuse:
        # No stored distillation was involved at all — nothing was at risk, so nothing was lost.
        return {"preserved": True, "reused": False, "redistilled": attempts > 0, "dropped": []}
    dropped = list(reuse.get("dropped") or [])
    return {"preserved": not dropped, "reused": bool(reuse.get("reused")),
            "redistilled": attempts > 0, "dropped": dropped}


def _reuse_at_risk(report: dict) -> bool:
    """Was a stored distillation actually present to preserve?

    `preserved` is True by construction when nothing was stored — an agent that parks the way the
    meeting brief asks (`decision: "triage"`) leaves nothing behind — so a `reuse` score of 1.00
    means EITHER "the capture kept what it had distilled" OR "there was never anything to lose".
    Those are different claims about a backend, and this is the flag that tells them apart when
    somebody reads the row six months from now. Reported, never scored, for the same reason
    `reused` is not: how a backend parks is the brief's business, not this facet's.

    Recorded beside the `reuse` block rather than inside it. That block is the observation contract
    between `processing._reuse_note` and this instrument and is pinned key for key by
    `tests/evals/test_filing_observation_contract.py`; this flag is the instrument's own reading of
    whether the block was there at all, not a field the production report grew.
    """
    return bool(report.get("distillation_reuse"))


class CountingAgent:
    """Wraps the agent under measurement and counts the passes ONE capture spends.

    This is the instrument's own seam, and it is why no production code changed to build this
    eval: `processing.Deps.agent` is injected, so the number of times the model was actually called
    is observable from outside without any report having to carry it. `report.filed` does not
    report agent attempts (only `failed_system` does), and adding the field would have been a
    production change to serve a measurement.

    It also gives the meeting reuse its honest observable: the reuse attempt runs no agent pass at
    all, so a re-file that reused a parked distillation is the one that arrives here with
    `calls == 0`.

    **A wrapper of a PORT owes every declared member of it, not only the methods** (ADR 033). This
    class stands where `processing` expects a `filing_port.FilingAgent`, and that port declares
    `structured_ordinary` — the attribute that decides whether the gatherer runs, which half of the
    outcome envelope is owed, and whether CODE writes the page. Swallowing it made every backend
    look EXPLORING from behind this wrapper: the paid golden on `--backend pydantic` would have run
    the structured backend down the exploring branch, refused every ordinary capture for carrying
    no `page_path`, and reported that as a filing-quality score. An instrument that changes the
    thing it measures is worse than no instrument.

    Copied by plain attribute access with NO default, so a backend that forgot to declare it fails
    loudly HERE, at construction, before a single capture is submitted — rather than one delivery
    at a time inside a measurement.
    """

    def __init__(self, inner):
        self.inner = inner
        self.structured_ordinary = inner.structured_ordinary
        self.calls = 0

    def reset(self) -> None:
        self.calls = 0

    def run(self, **kwargs):
        self.calls += 1
        return self.inner.run(**kwargs)

    def run_meeting(self, **kwargs):
        self.calls += 1
        return self.inner.run_meeting(**kwargs)


# The structured backend (ADR 033: every flow, no tools, code writes the page). Named here rather
# than imported so `--help` costs nothing but the standard library, like everything else at this
# module's scope.
#
# **It used to be `MEETING_ONLY_BACKEND`, guarded by `_require_measurable_subset`**, which refused
# any `--kinds` but `meeting` for it because M1's backend would have scored a column of refusals on
# the ordinary captures. Both the constant's name and that guard are gone with the restriction they
# described; the `--kinds` flag itself stays, because an ablation somebody chooses is a different
# thing from a limitation they work around.
STRUCTURED_BACKEND = "pydantic"


def build_parser() -> argparse.ArgumentParser:
    """The command line, as a value — `librarian/cli.build_parser`'s own shape, and for its reason:
    a parser that can only be reached by running `main()` can only be asserted by driving a whole
    measurement, so the flags an operator is promised (by a refusal, by a doc, by this file's own
    docstring) had no seam anybody could check them through."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=str(FIXTURE / "repo"),
                    help="the frozen mini knowledge repo to file into (default: evals/filing/repo)")
    ap.add_argument("--manifest", default=str(FIXTURE / "captures" / "manifest.json"))
    ap.add_argument("--expectations", default=str(FIXTURE / "expected" / "expectations.json"))
    ap.add_argument("--backend", choices=["sdk", "double", STRUCTURED_BACKEND], default="sdk",
                    help="the agent backend (default: sdk — the real measurement)")
    ap.add_argument("--model", default=None,
                    help="the librarian model (default: librarian Settings' own default). The "
                         f"{STRUCTURED_BACKEND!r} backend needs a provider-prefixed id, e.g. "
                         "anthropic:claude-sonnet-5")
    ap.add_argument("--kinds", default="",
                    help="comma-separated capture kinds to measure (default: all of them). A "
                         "subset recomputes its own denominators and records the kinds in the "
                         "history row, so its score is never mistaken for the whole set's")
    ap.add_argument("--report", default=None, help="write the full JSON report here")
    return ap


def main() -> int:
    args = build_parser().parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    expectations = json.loads(Path(args.expectations).read_text(encoding="utf-8"))
    selected = _select_kinds(manifest, args.kinds)
    # **A `--kinds` that names every kind the set carries is the WHOLE set**, and it still owes the
    # pinned denominators. Reading the flag's presence instead of what it selected would let
    # `--kinds meeting,raw` skip the drift check on the full golden set — the one check that makes
    # two full-set scores comparable — by spelling out what it was already going to run.
    whole = selected is None or set(selected) == set(_manifest_kinds(manifest))
    if selected is not None:
        manifest, expectations = _subset(manifest, expectations, selected)
    _check_set(manifest, expectations, whole_set=whole)
    return _run(args, manifest, expectations, kinds=_manifest_kinds(manifest))


def _manifest_kinds(manifest: dict) -> list[str]:
    """Every capture kind the manifest carries, sorted — what a run over it MEASURES."""
    return sorted({str(capture.get("kind", "")) for capture in manifest["captures"]})


def _select_kinds(manifest: dict, raw: str) -> list[str] | None:
    """`--kinds`, resolved against the set on disk. `None` means the whole set.

    An unknown kind is refused by name rather than silently scoring nothing: a typo that produced
    an empty run would print a table of zero denominators, which reads exactly like a backend that
    files nothing.
    """
    if not (raw or "").strip():
        return None
    wanted = sorted({kind.strip() for kind in raw.split(",") if kind.strip()})
    available = _manifest_kinds(manifest)
    unknown = [kind for kind in wanted if kind not in available]
    if unknown:
        sys.exit(f"--kinds names {unknown}, which the golden set does not contain. It carries: "
                 f"{available}")
    return wanted


# REMOVED with the meeting-only restriction (ADR 033): `_require_measurable_subset`, which refused
# `--backend pydantic` unless it was given `--kinds meeting` and nothing else. Its argument was
# sound for M1 — an ordinary capture would have been refused by the backend and scored as a filing
# failure, which measures nothing about it — and it is simply not true any more: that backend now
# serves every kind the golden set carries, which is what this milestone is for. Recorded here
# rather than deleted in silence, because the RULE it enforced still stands for the next
# backend/subset pairing somebody adds: a run that cannot measure a capture must be refused before
# the queue is touched, not scored as a failure.


def _subset(manifest: dict, expectations: dict, kinds: list) -> tuple:
    """The manifest and the expectations narrowed to `kinds`, keeping every other key of both.

    The expectations are narrowed by the manifest's own ids rather than by a kind of their own —
    the yardstick file records no kind, and inventing one there would be a second place for the two
    halves to disagree about what a capture is (`_check_set`'s first refusal exists because they
    can).
    """
    captures = [capture for capture in manifest["captures"] if capture.get("kind") in kinds]
    ids = {capture["id"] for capture in captures}
    return ({**manifest, "captures": captures},
            {**expectations, "expectations": [entry for entry in expectations["expectations"]
                                              if entry["id"] in ids]})


def _expect_blocks(entry: dict) -> list:
    """Every scored moment one entry declares: the first pass, and the re-file after a reply."""
    return [entry["expect"]] + ([entry["after_reply"]] if "after_reply" in entry else [])


def _denominators(expectations: dict) -> dict:
    """The per-facet denominators the file on disk would produce — the same arithmetic `aggregate`
    performs over a complete run, done here without spending one."""
    counts: collections.Counter = collections.Counter()
    for entry in expectations["expectations"]:
        for block in _expect_blocks(entry):
            counts.update(name for name in block if name in FACETS)
    return dict(counts)


def _check_set(manifest: dict, expectations: dict, *, whole_set: bool = True) -> None:
    """Four refusals, all BEFORE a single model call is spent.

    A golden set that has drifted does not fail; it silently scores a smaller or a different set
    than the one it claims, which is the failure mode `tests/evals/test_golden_corpus_fixture.py`
    exists to prevent for the other corpus. Here it costs real money to discover late, and every
    one of these four has a way of looking like a backend result instead of a set defect:

    1. **The halves name the same captures**, counted rather than set-compared — a duplicated id in
       one half and a missing one in the other cancel out under `set()` and score that capture
       twice.
    2. **Every key of every expectation block is a facet the scorer knows.** `score_phase` ignores
       what it does not recognize, so `achor:` removes that capture from the anchor denominator in
       silence.
    3. **`reply` and `after_reply` travel together.** Half an ask-back case either scores a park
       and stops, or names an `after_reply` phase the runner will never reach.
    4. **The denominators are the pinned ones** (`EXPECTED_DENOMINATORS`), which is what makes the
       three checks above add up to the set this instrument's series was calibrated on.

    **`whole_set=False` is a PROPER subset, and only the FOURTH check changes.** The pin describes
    the shipped set and nothing else, so holding a proper subset against it would fail every subset
    by construction — but a `--kinds` that names every kind the set carries is not a subset at all
    and still owes the pin (`main` decides which it is from what was SELECTED, never from whether
    the flag was typed). What a proper subset owes instead is that it scores SOMETHING — a filter
    whose captures name no facet at all produces a table of empty denominators, which reads like a
    backend that files nothing. The first three checks are properties of a set of any size and
    still run.
    """
    captures = collections.Counter(c["id"] for c in manifest["captures"])
    expected = collections.Counter(e["id"] for e in expectations["expectations"])
    if captures != expected:
        sys.exit("the golden set is inconsistent — captures/manifest.json and "
                 "expected/expectations.json do not describe the same set:\n"
                 f"  only in captures/manifest.json:     "
                 f"{sorted((captures - expected).elements())}\n"
                 f"  only in expected/expectations.json: "
                 f"{sorted((expected - captures).elements())}")

    unknown = {entry["id"]: sorted(set(block) - set(FACETS))
               for entry in expectations["expectations"]
               for block in _expect_blocks(entry) if set(block) - set(FACETS)}
    if unknown:
        named = "; ".join(f"{capture_id}: {names}" for capture_id, names in sorted(unknown.items()))
        sys.exit("expected/expectations.json names keys the scorer does not score, so those "
                 f"captures would silently leave their facets' denominators: {named}")

    half = [entry["id"] for entry in expectations["expectations"]
            if ("reply" in entry) != ("after_reply" in entry)]
    if half:
        sys.exit(f"these expectations carry a `reply` without an `after_reply` block or the other "
                 f"way round — an ask-back case is only measured when both are present: {half}")

    denominators = _denominators(expectations)
    if not whole_set:
        if not denominators:
            sys.exit("this subset scores no facet at all — every capture in it names none, so the "
                     "run would print a table of empty denominators rather than measure anything")
        return
    if denominators != EXPECTED_DENOMINATORS:
        names = sorted(set(denominators) | set(EXPECTED_DENOMINATORS))
        diff = [f"{name}: pinned {EXPECTED_DENOMINATORS.get(name, 0)}, "
                f"file has {denominators.get(name, 0)}"
                for name in names
                if denominators.get(name, 0) != EXPECTED_DENOMINATORS.get(name, 0)]
        sys.exit("the golden set no longer produces the denominators run_filing pins — scores "
                 "recorded before and after this change are not comparable per run. Update "
                 "EXPECTED_DENOMINATORS in the same commit, on purpose:\n  " + "\n  ".join(diff))


def _run(args, manifest: dict, expectations: dict, *, kinds: list | None = None) -> int:
    """One measurement, end to end.

    `kinds` is `main`'s to pass, and only `main`'s: it is where the subset was resolved and
    refused. It defaults to the unfiltered, every-flow run a direct caller means.
    """
    # Deferred on purpose — see the pure-scoring banner above. Nothing heavier than the standard
    # library is imported until a run actually happens, so the scorer stays importable keylessly.
    import pytest

    from stigmergy.capture import schema
    from stigmergy.kernel.frontmatter import split_frontmatter
    from stigmergy.librarian import config as librarian_config
    from stigmergy.librarian import githubapp, worker
    from stigmergy.librarian.agent import build_agent
    from stigmergy.librarian.errors import LibrarianConfigError
    from stigmergy.server.service import BrainService
    from stigmergy.server.settings import Settings as ServerSettings
    from tests import testdb
    from tests.librarian import support

    # ── nothing a measurement does may touch state a human is using ────────────────────────────
    # `make filing-golden` hands this script the operator's own gitignored env file (`-include
    # .env` + `export` in the Makefile), and this is the first make target that drives
    # `processing._file`. With the App configured, `_file` asks `githubapp.configured()` and mints
    # a REAL installation token — a network call signed with the App private key — then pushes to
    # `github.com/<slug>` instead of this run's throwaway bare remote, whose "slug" here is a local
    # path. Every capture fails, the table reads as a backend that cannot file, and the fault is
    # somewhere nobody would look. `tests/conftest.py::no_real_github_app_anywhere` closes exactly
    # this at the root of the suite and `scripts/e2e_isolate.sh` for every e2e script; this is the
    # third road that reaches `_file`, and it had neither.
    #
    # The names are imported, never retyped: a sixth variable, or a rename, has to break the import
    # rather than silently escape a list written from memory. Deleted unconditionally, for BOTH
    # backends and with no escape hatch, for the reason `e2e_isolate.sh` records — an escape hatch
    # is a rule somebody has to remember, and remembering is what failed.
    for name in (githubapp.APP_ID_ENV, githubapp.INSTALLATION_ID_ENV, githubapp.PRIVATE_KEY_ENV,
                 githubapp.PRIVATE_KEY_FILE_ENV, githubapp.APP_LOGIN_ENV):
        os.environ.pop(name, None)
    # Same doctrine, one credential over (`tests/conftest.py::no_real_llm_anywhere`, which pins the
    # identical value for the identical reason). A filed meeting triggers `_file_meeting`'s
    # best-effort view regeneration, and `kernel.settings.resolve_backend` reads `$CLEAN_LLM` at
    # call time, defaulting to the REAL provider: with the operator's key exported that step spends
    # OpenAI money this instrument does not price, and without one it logs an alarming traceback
    # for a step no score depends on. The fake keeps the flow exercised at zero spend; the scored
    # pages are read back at `result_ref`'s sha either way, which names the filing's own commit.
    os.environ["CLEAN_LLM"] = "fake"

    model = args.model or librarian_config.DEFAULT_MODEL
    by_id = {e["id"]: e for e in expectations["expectations"]}
    materials = Path(args.manifest).parent

    try:
        conn = testdb.connect_or_skip("filing-golden")
    except (pytest.skip.Exception, pytest.fail.Exception) as ex:
        sys.exit(f"the filing golden needs the local test Postgres: {ex}")

    started = time.perf_counter()
    phases: list = []
    with conn:
        schema.ensure_capture_schema(conn)
        with conn.cursor() as cur:
            # The eval owns the queue for the duration of a run, exactly like the walk scripts —
            # `tests.testdb` has already refused any DSN but the test database's, so this can only
            # ever wipe the suite's own.
            cur.execute("DELETE FROM capture_queue")
        with tempfile.TemporaryDirectory() as tmp:
            env = support.build_repo(os.path.join(tmp, "git"), source=args.repo)
            settings = support.build_settings(
                env, worktree_root=os.path.join(tmp, "worktrees"),
                backend=args.backend, model=model)
            # The worker's OWN pre-flight, run against the run's own settings before a capture is
            # claimed — the whole point of `startup_checks` being fail-closed and loud. Without it,
            # a missing `gitleaks`, an absent Claude credential or a fixture whose linter or skill
            # is not at `base` produces ten identical `failed` rows and a table of zeros, and the
            # real cause is buried under attempts-exhausted noise. It is safe for this fixture by
            # construction: the lease arithmetic passes on `Settings`' defaults, the linter and
            # both skills are committed at `base`, and `_check_push_identity` returns early because
            # this run's origin is a local bare path rather than github.com.
            try:
                worker.startup_checks(settings)
            except LibrarianConfigError as ex:
                sys.exit(f"the filing golden cannot run against this configuration: {ex}")
            counting = CountingAgent(build_agent(settings))
            deps = support.build_deps(env, settings, agent=counting)

            total = len(manifest["captures"])
            for n, capture in enumerate(manifest["captures"], 1):
                entry = by_id[capture["id"]]
                # Progress on stderr (stdout stays the report): a real-model run is ten agent
                # loops over a real git worktree, minutes of silence that read as a hang.
                print(f"[{n:2d}/{total}] {capture['id']:34s} ", end="", flush=True,
                      file=sys.stderr)
                phases += _drive(conn, deps, counting, env, capture, entry,
                                 materials=materials, schema=schema, worker=worker,
                                 support=support, split_frontmatter=split_frontmatter,
                                 brain_service=BrainService, server_settings=ServerSettings)
                print("", file=sys.stderr)

    report = aggregate(phases, backend=args.backend, model=model,
                       wall_s=time.perf_counter() - started, kinds=kinds)
    print(render(report))
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False,
                                                default=str), encoding="utf-8")
        print(f"report -> {args.report}")

    # A REAL-instrument run — never the keyless `--backend double` self-check — appends its score
    # to the durable, git-resident series. Same rule, and the same reason, as `run_qa.py`'s
    # `--llm openai` guard: a plumbing check has no quality number worth keeping.
    #
    # And only a run that MEASURED something (`_withheld_reason`). The series is the one durable
    # record, read years later by somebody comparing two backends; a row written by a run that
    # died on its configuration is indistinguishable there from a backend that files badly, and it
    # drags every trend line it sits in.
    if args.backend != "double":
        withheld = _withheld_reason(phases, failed_status=schema.FAILED)
        if withheld:
            print(f"\n!! NO HISTORY ROW WRITTEN — {withheld}.\n"
                  f"!! {eval_history.HISTORY_PATH.name} is unchanged. The table above still says "
                  f"what happened; fix the cause and run it again.", file=sys.stderr)
        else:
            eval_history.append_run(
                suite="filing", git_sha=eval_history.resolve_git_sha(ROOT),
                metrics=_history_metrics(report, phases,
                                         eval_history.corpus_provenance(args.repo)))
    return 0


def _status_counts(phases: list) -> dict:
    """`{status: how many phases ended there}`, sorted so two reports of the same run diff to
    nothing."""
    counts = collections.Counter(str(p["observed"].get("status", "")) for p in phases)
    return dict(sorted(counts.items()))


def _withheld_reason(phases: list, *, failed_status: str) -> str:
    """Why this run must NOT leave a row in the series — `""` when it may.

    Two conditions, and each is a run that produced a table without measuring a backend:

    * **any phase ended `failed`.** `worker.process_next` writes that status when processing raised
      — a config fault, a git fault, a fixture fault. It is the instrument breaking, and recorded
      as a score it reads as a backend that cannot file.
    * **no phase ever called the agent.** Every capture refused before the model ran (a credential
      that was accepted and then rejected, a queue that handed back nothing) leaves a full set of
      facets scored at whatever the deterministic path produced. F04 legitimately spends zero
      passes; a whole RUN at zero is plumbing.

    The run's own exit code is untouched either way: the table above is what happened, and this
    only decides whether the number is durable enough to compare against later ones.
    """
    failed = sorted({p["id"] for p in phases if p["observed"].get("status") == failed_status})
    if failed:
        return (f"{len(failed)} phase(s) ended `{failed_status}` — the instrument, not the "
                f"backend: {failed}")
    if not any(p["observed"].get("attempts", 0) > 0 for p in phases):
        return ("the agent was never called in any of the "
                f"{len(phases)} scored phase(s), so nothing about a backend was measured")
    return ""


def _drive(conn, deps, counting, env, capture: dict, entry: dict, *, materials: Path,
           schema, worker, support, split_frontmatter, brain_service, server_settings) -> list:
    """One golden capture, submitted and drained through the real path. Returns its scored phases.

    A parking capture yields TWO phases — the park, and the re-file after the stored reply travels
    back through the REAL answer channel (`BrainService.reply`, the same object
    `tests/librarian/test_human_loop_pg.py` drives, never a hand-written UPDATE).

    **Each capture is submitted and drained on its own**, rather than submitting the whole set and
    then draining it. That keeps exactly one row claimable at any moment, so `worker.process_next`
    cannot pick up a different capture than the one being scored — and it is what lets F04's
    duplicate refusal depend on F01 having genuinely filed first.
    """
    material = (materials / capture["material"]).read_text(encoding="utf-8")
    counting.reset()
    if capture["kind"] == schema.MEETING:
        hints = capture.get("hints") or {}
        item = support.submit_meeting(conn, deps, material,
                                      submitted_by=capture["submitted_by"],
                                      title=hints.get("title", ""),
                                      meeting_date=hints.get("meeting_date", ""),
                                      attendees=hints.get("attendees", ""))
    else:
        item = support.submit(conn, deps, material, submitted_by=capture["submitted_by"],
                              hints=capture.get("hints") or None)
    result = _drain_one(conn, deps, worker, capture_id=capture["id"], what="its own capture")
    observed = _observe(result, counting.calls, env=env, support=support,
                        split_frontmatter=split_frontmatter)
    out = [_phase(capture["id"], "only" if "reply" not in entry else "park",
                  entry["expect"], observed)]
    print(f"{result.status:12s}", end="", flush=True, file=sys.stderr)

    if "reply" not in entry:
        return out

    if result.status != schema.NEEDS_INPUT:
        # The backend did not park, so there is nothing to reply to. The `after_reply` facets are
        # recorded as missed rather than skipped: a phase that silently vanished would shrink its
        # facets' denominators and quietly raise the score of a backend that never asked.
        out.append(_phase(capture["id"], "after_reply", entry["after_reply"],
                          {"status": "", "note": "never parked, so no reply was possible"}))
        return out

    settings = server_settings(identity=capture["submitted_by"], identities_path="x")
    service = brain_service(settings, conn, embedder=None, audiences=set(),
                            identity=capture["submitted_by"])
    service.reply(item["id"], entry["reply"])
    counting.reset()
    refiled = _drain_one(conn, deps, worker, capture_id=capture["id"],
                         what="the capture it had just replied to")
    print(f" -> {refiled.status}", end="", flush=True, file=sys.stderr)
    out.append(_phase(capture["id"], "after_reply", entry["after_reply"],
                      _observe(refiled, counting.calls, env=env, support=support,
                               split_frontmatter=split_frontmatter)))
    return out


def _drain_one(conn, deps, worker, *, capture_id: str, what: str):
    """`worker.process_next`, with the empty queue named for what it is.

    The worker answers `None` when it claimed nothing, which is a legitimate answer for a service
    that polls and a contradiction for this runner: each capture is submitted and drained on its
    own, so exactly one row is claimable at this moment and it belongs to `capture_id`. Unpacking
    the `None` used to raise `TypeError: cannot unpack non-sequence`, a traceback whose top frame
    names tuple unpacking and whose cause is somewhere else entirely — a reply that never reached
    the row, a lease still held from an earlier run, another worker draining this database.
    """
    claimed = worker.process_next(conn, deps)
    if claimed is None:
        sys.exit(f"{capture_id}: the queue was empty when the runner went to drain {what}. Nothing "
                 f"was claimable, so either the submit did not land, the row is still leased by an "
                 f"earlier run, or something else is draining this database — the run is stopped "
                 f"here rather than scoring a capture that never ran.")
    _, result = claimed
    return result


def _phase(capture_id: str, phase: str, expect: dict, observed: dict) -> dict:
    return {"id": capture_id, "phase": phase, "expect": expect, "observed": observed,
            "facets": score_phase(expect, observed)}


def _observe(result, attempts: int, *, env, support, split_frontmatter) -> dict:
    """One `processing.Result` as the flat, JSON-shaped dict the scorer reads.

    Pages are read back **out of git at the commit that was pushed** (`support.read_filed_page`),
    never off a worktree that no longer exists and never from the agent's own account of what it
    wrote. What landed in the commit is the only thing a reader of the knowledge repo will ever
    see, so it is the only thing worth scoring.
    """
    report = result.report or {}
    observed = {
        "status": result.status,
        "reason": report.get("reason_code", ""),
        "attempts": attempts,
        # A corrective retry is the second and last agent pass an item may spend, so the bounce
        # count is one less than the passes — except on a reuse, which spends none at all.
        "bounces": max(0, attempts - 1),
        "cost_usd": float(report.get("cost_usd", 0.0) or 0.0),
        "edits": list(report.get("pages_edited") or []),
        "park_question": [name for name in
                          ([report.get("unresolved_name")] if report.get("unresolved_name")
                           else list(report.get("unresolved_names") or [])) if name],
        # Diagnostics for a miss, chosen so the whole report is REPRODUCIBLE: the report's own
        # summary sentence would be the obvious thing to keep, and it embeds the commit sha —
        # which differs on every run, because each run files into its own throwaway repo. Two
        # reports from the same backend now diff to nothing but `wall_s` and `cost_usd`, which is
        # what makes "did this change move anything?" answerable with `diff` instead of by eye.
        "page_path": report.get("page_path", ""),
        "anchored_to": report.get("anchored_to", ""),
    }
    if "@" not in (result.result_ref or ""):
        return observed                 # a park or a refusal: nothing was committed to read back
    sha = result.result_ref.rsplit("@", 1)[1]

    meeting = report.get("filed_meeting")
    if meeting:
        meeting_page = meeting.get("meeting_page", "")
        observed["folder"] = _folder_of(meeting_page)
        observed["type"] = _page_type(env, sha, meeting_page, support, split_frontmatter)
        observed["decisions"] = [
            {"path": d.get("path", ""),
             "anchor": _page_anchor(env, sha, d.get("path", ""), support, split_frontmatter)}
            for d in meeting.get("decisions") or []]
        observed["source_pages"] = list(meeting.get("source_pages") or [])
        observed["reuse"] = _observe_reuse(report, attempts)
        observed["reuse_at_risk"] = _reuse_at_risk(report)
        return observed

    page_path = report.get("page_path", "")
    observed["folder"] = _folder_of(page_path)
    observed["type"] = _page_type(env, sha, page_path, support, split_frontmatter)
    observed["anchor"] = _page_anchor(env, sha, page_path, support, split_frontmatter)
    return observed


def _frontmatter(env, sha: str, page_path: str, support, split_frontmatter) -> dict | None:
    """The page's frontmatter at `sha`, or `None` when the page could not be read back at all.

    `None` rather than `{}`, and that distinction is the whole point: a page that could not be read
    and a page carrying no fields are different observations, and only one of them may be allowed
    to look like an anchoring outcome. An unreadable page is still an observation and not a crash —
    a run that died on phase three would lose the captures behind it, and each of those costs a
    real agent pass.
    """
    try:
        text = support.read_filed_page(env.repo, sha, page_path)
    except Exception:  # noqa: BLE001 — an unreadable page is an observation, not a crash
        return None
    return split_frontmatter(text)[0] or {}


def _page_type(env, sha: str, page_path: str, support, split_frontmatter) -> str:
    return str((_frontmatter(env, sha, page_path, support, split_frontmatter) or {}).get("type",
                                                                                         ""))


def _page_anchor(env, sha: str, page_path: str, support, split_frontmatter) -> dict:
    """The page's own anchor, and one answer for the page that is not there.

    `entity: []` on a committed page means the anchoring outcome was company-wide: `gate_anchoring`
    refuses to let a page whose declared entity did not resolve reach a commit at all, so an empty
    list on something that FILED can only be the company-wide outcome (`processing._stamp`'s own
    defence-in-depth records the same reasoning).

    That argument holds only for a page this function actually read. A page named by the result and
    absent from the commit is an instrument or a filing fault, and reporting it as `company` would
    hand a wrong-but-lucky score to F05 — the one capture whose correct answer IS company-wide.
    `unreadable` is a kind no expectation names, so it scores as a miss against every anchor
    expectation there is, which is the honest reading of "the page could not be found".
    """
    frontmatter = _frontmatter(env, sha, page_path, support, split_frontmatter)
    if frontmatter is None:
        return {"kind": "unreadable", "ids": []}
    return _anchor_from_page(frontmatter)


if __name__ == "__main__":
    sys.exit(main())
