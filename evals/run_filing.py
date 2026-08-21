#!/usr/bin/env python3
"""Golden filing runner — on-demand, keyed instrument (not wired into CI).

Drives `evals/filing/`'s golden captures through the REAL filing path — `worker.process_next` over
real Postgres, a real bare remote, a real `git worktree`, the nine gates and the knowledge repo's
own contract linter — against the frozen mini repo at `evals/filing/repo/`, and scores each outcome
per facet, each facet with its own denominator:

  - status     the terminal queue status (`filed` / `rejected` / `failed`)
  - reason     a refusal's own `reason_code`, for the states that carry one
  - type       the `type:` of the page that landed, read back out of git
  - folder     where it landed
  - anchor     the page's server-stamped `entity:` — resolved registry ids; `[]` is the
               company-wide answer, distinguished from a wrong entity
  - edits      which OTHER pages the commit changed, scored by containment (`_edits_match`)
  - proposals  for a name the registry does not know: the identity the filing PROPOSED for it
  - decisions  for a meeting: one decision page per decision, each with its OWN anchor

Every capture is ONE scored phase (ADR 041). Nothing parks, nothing is asked, nothing is re-filed:
a name the registry does not know is proposed as an entity in the same commit as the page, and a
steward confirms it afterwards from the inbox — which the instrument never reaches.

ATTEMPTS, BOUNCES and COST are cost axes: reported, no bar, never folded into a quality facet.
Scoring is deterministic — no judge — except the two title matchers, which are word-subset.

  # keyless plumbing self-check (offline double — appends no history row)
  python evals/run_filing.py --backend double

  # the real measurement (needs the model provider's key)
  python evals/run_filing.py --backend pydantic \
      --model anthropic:claude-sonnet-5 --report evals/out/filing-sonnet-5.json

`--kinds` scores a subset: denominators are recomputed from it instead of held against
`EXPECTED_DENOMINATORS`, and the history row records `kinds`.

Both backends need the local Postgres (`make db-up`) and `gitleaks` on PATH. `--backend double` has
no NLP — it files one `note` per capture anchored to the first registry entity, and proposes an
identity only when a `DOUBLE:propose=` directive says to — so it scores 1.00 on the facets code
decides and low on every facet judgment decides.
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
# Self-pinned to THIS checkout: `src/` for the package under measurement, ROOT for
# `tests.librarian.support`'s bare-remote rig. Never point this script at another checkout.
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

try:                                    # run as a script: evals/ is sys.path[0], sibling import
    import bars  # noqa: E402
    import eval_history  # noqa: E402
except ModuleNotFoundError:             # imported as `evals.run_filing` (the scorer unit tests)
    from evals import bars, eval_history  # noqa: E402

FIXTURE = ROOT / "evals" / "filing"

# Every facet this instrument knows, in the order the table prints them: quality first, then the
# two cost counters, which carry no bar.
QUALITY_FACETS = ("status", "reason", "type", "folder", "anchor", "edits",
                  "proposals", "decisions")
COST_FACETS = ("attempts", "bounces")
FACETS = QUALITY_FACETS + COST_FACETS

# The per-facet denominators the shipped golden set produces, pinned rather than derived: a facet
# that quietly loses one is a score that ROSE because a capture stopped being counted. Adding a
# capture fails `_check_set` first, on purpose — update this in the same commit.
#
# MOVED for ADR 041, which retired the park. Four numbers changed and each one is a fact about the
# redesign rather than an edit to a yardstick: `status` fell 16 -> 14 because the two ask-back
# captures stopped being two phases each; `anchor` fell 11 -> 10 because F02 no longer asserts an
# id it would have to invent (the id of a PROPOSED entity is slugified from the name the agent
# chose, so `proposals` scores that judgment tolerantly instead); `park_question` and `reuse` are
# gone with the states they measured, and `proposals` takes the former's denominator of 2.
# Scores before and after are comparable per FACET and not per run — see evals/README.md's growth
# protocol, and say so in the history row's own commit.
EXPECTED_DENOMINATORS = {"status": 14, "reason": 1, "type": 13, "folder": 13, "anchor": 10,
                         "edits": 1, "proposals": 2, "decisions": 2,
                         "attempts": 12, "bounces": 12}

# Words carried by nearly every title in this domain; matching on them would let any two titles
# match each other. Deliberately tiny — a long stop list is a second yardstick nobody reviews.
_STOPWORDS = frozenset({"a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "at",
                        "by", "with", "is", "are", "be", "md", "wiki", "decisions", "notes",
                        "concepts", "meetings", "sources"})
_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")


# ── pure scoring: no Postgres, no git, no framework, no model ──────────────────────────────────
# Everything below is a function of data, so a keyless test builds `observed` by hand and gets
# identical scores. Keep the heavy imports inside `_run`.

def _words(text: str) -> frozenset:
    """Significant words of a title or path, lowercased and stopword-stripped. No morphology
    folding — the one fold happens at the COMPARISON, in `_same_word`."""
    return frozenset(w for w in _WORD_SPLIT_RE.split((text or "").lower())
                     if w and w not in _STOPWORDS)


def _same_word(want: str, got: str) -> bool:
    """One token against one token: equal, or the same word in the other grammatical NUMBER.

    Exactly one fold — a trailing `s`, either direction — because grammatical number is measured
    run-to-run noise on the same model. Not a stemmer and not to become one: `es` plurals and every
    other inflection are still two distinct words, so an expectation must be written in uninflected
    content words.
    """
    return want == got or want == f"{got}s" or got == f"{want}s"


def title_matches(expected: str, candidate: str) -> bool:
    """Does `candidate` (a page path, or a title) carry the title `expected` names?

    Word-subset, not a literal `in`: every significant word of the expectation must appear in the
    candidate, in either grammatical number (`_same_word`). Generous in one direction only — a
    candidate may say MORE, never less — so an expectation is written in the uninflected content
    words a paraphrase cannot drop. The same rule is stated in
    `evals/filing/expected/expectations.json`'s `_looseness` note, where expectations are written.
    """
    want = _words(expected)
    got = _words(candidate)
    return bool(want) and all(any(_same_word(w, g) for g in got) for w in want)


def _anchor_matches(expected: dict, observed: dict) -> bool:
    """An anchor is `{kind, ids}` and both halves have to agree.

    `ids` is the load-bearing comparison: the resolved registry ids stamped on the page. `kind` is
    derived from them (`_anchor_from_page`) and earns its place only at the edges, where
    `_page_anchor` answers `unreadable` — a kind no expectation names — so an instrument fault can
    never score as a company-wide filing.
    """
    return (str(expected.get("kind", "")) == str(observed.get("kind", ""))
            and sorted(expected.get("ids") or []) == sorted(observed.get("ids") or []))


def _edits_match(expected: list, observed: list) -> bool:
    """Every edit the expectation names was performed. Observed EXTRAS do not fail the facet.

    Containment, not equality: an extra edit is additive and already gate-checked, and captures file
    one after another into ONE growing repo, so a later capture may legitimately backlink a page an
    earlier one created — under equality that would score run order.

    Consequence when writing an expectation: `edits: []` is vacuously true and still fills the
    denominator. "This capture owes no edit" is said by naming no `edits` key at all.
    """
    return set(expected) <= set(observed)


def _decisions_match(expected: list, observed: list) -> bool:
    """One decision page per expected decision, each matched loosely by title and exactly by
    anchor, with no page left over on either side.

    The count is part of the check: five decision pages where two were expected is a granularity
    failure, not over-delivery. Matching is greedy and one-to-one.

    An entry MAY omit `title` and then pairs on its ANCHOR alone — a decision's aboutness is a fact
    with one spelling, its title is prose. A title-less entry is the WEAKEST matcher and must be
    written LAST, or greedy pairing lets it take a titled sibling's page; `_check_set` refuses a set
    ordered the other way.
    """
    if len(expected) != len(observed):
        return False
    remaining = list(observed)
    for want in expected:
        hit = None
        for candidate in remaining:
            # An ABSENT title is what makes an entry anchor-only; a present-but-empty one still has
            # to match, and `title_matches("")` is False by design.
            if "title" in want and not title_matches(want["title"],
                                                     candidate.get("path", "")):
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


def _proposals_match(expected: list, observed: list) -> bool:
    """Every identity the expectation says should have been proposed was proposed.

    Matched loosely against the joined names, the same predicate the retired `park_question` facet
    used and for the same reason: what is being scored is WHICH unregistered thing the filing gave
    an identity to, not the spelling the agent chose for it. A proposal named `Halcyon Grid pilot`
    is the same judgment as one named `Halcyon Grid`, and the id — `slugify(name)` — differs
    between them, which is exactly why this facet is here and the `anchor` facet is not asserted on
    a proposing capture.

    Generous about extras on purpose: a filing that proposed the name AND a second identity beside
    it still recognised the one the expectation names, and the extra is already fenced by
    `identity`'s three honesty checks (named in the material, no registered collision, not a name a
    steward declined).
    """
    joined = " ".join(str(name) for name in (observed or []))
    return bool(expected) and all(title_matches(name, joined) for name in expected)


def score_phase(expect: dict, observed: dict) -> dict:
    """`{facet: bool}` for exactly the facets `expect` NAMES — nothing else.

    A facet the expectation is silent about is absent from the result: never `False`, never counted,
    never folded into another facet's denominator.
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
    if "proposals" in expect:
        out["proposals"] = _proposals_match(expect["proposals"], observed.get("proposals") or [])
    if "decisions" in expect:
        out["decisions"] = _decisions_match(expect["decisions"], observed.get("decisions") or [])
    if "attempts" in expect:
        out["attempts"] = observed.get("attempts") == expect["attempts"]
    if "bounces" in expect:
        out["bounces"] = observed.get("bounces") == expect["bounces"]
    return out


def aggregate(phases: list, *, backend: str, model: str, wall_s: float,
              kinds: list | None = None) -> dict:
    """Per-facet hits and denominators over every scored phase, plus the cost axes.

    `phases` is `[{id, phase, expect, observed, facets}, ...]` — one entry per scored moment, and
    since ADR 041 that is exactly one per capture. `kinds` rides into the report and the history
    row: a per-facet score is only comparable against one over the same set.
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
        # A bar of None means REPORT, DO NOT JUDGE — a facet whose baseline is not yet recorded
        # must never read as a pass.
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
    # This table is what gets screenshotted and quoted, so the caption must not name a model the
    # double never called, nor let a subset score read as the shipped set's.
    identity = (f"backend `{report['backend']}` (no model)" if report["backend"] == "double" else
                f"backend `{report['backend']}` · model `{report['model']}`")
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
    """The `metrics` dict a real-instrument run hands `eval_history.append_run`.

    Pure — the caller resolves `eval_history.corpus_provenance` and hands the result in — so a typo
    in a key name is a keyless test's problem rather than a defect in the durable history file.
    """
    return {"backend": report["backend"], "model": report["model"],
            "kinds": report.get("kinds") or [],
            "facets": {name: row["score"] for name, row in report["facets"].items()},
            "counts": {name: {"hits": row["hits"], "of": row["of"]}
                       for name, row in report["facets"].items()},
            # What the phases ENDED as: a row whose facets look ordinary but whose statuses say
            # `failed: 9` is a run to distrust, and the report may be long gone.
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

    The page's `entity:` carries the registry's own resolved ids, never the agent's spelling of a
    name. Only ever called with frontmatter that was genuinely read (`_page_anchor` answers the
    unreadable case), which is what makes the empty-list branch a company-wide claim rather than an
    absence of evidence.
    """
    raw = frontmatter.get("entity")
    ids = [raw] if isinstance(raw, str) and raw else list(raw or [])
    ids = [str(i) for i in ids if str(i)]
    return {"kind": ("entity" if ids else "company"), "ids": sorted(ids)}


class CountingAgent:
    """Wraps the agent under measurement and counts the passes ONE capture spends.

    One capture is one phase now, so the count a phase reports is the whole cost of that capture:
    the drafting pass, plus the corrective retry if a gate or an identity honesty check bounced it.

    This stands where `processing` expects a `filing_port.FilingAgent`, so it must forward EVERY
    declared member of that port, not only its methods: `structured_ordinary` and `wants_gathered`
    each change the shape of the run, and swallowing one would silently change what is measured.
    Copied with no default, so a backend missing one fails at construction.
    """

    def __init__(self, inner):
        self.inner = inner
        self.structured_ordinary = inner.structured_ordinary
        self.wants_gathered = inner.wants_gathered
        self.calls = 0

    def reset(self) -> None:
        self.calls = 0

    def run(self, **kwargs):
        self.calls += 1
        return self.inner.run(**kwargs)

    def run_meeting(self, **kwargs):
        self.calls += 1
        return self.inner.run_meeting(**kwargs)


# The backend that calls a model; `double` is the keyless plumbing self-check. Named here rather
# than imported so `--help` costs nothing. The VALUE must not change: history rows key on it.
REAL_BACKEND = "pydantic"


def build_parser() -> argparse.ArgumentParser:
    """The command line, as a value, so the flags can be asserted without driving a measurement."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=str(FIXTURE / "repo"),
                    help="the frozen mini knowledge repo to file into (default: evals/filing/repo)")
    ap.add_argument("--manifest", default=str(FIXTURE / "captures" / "manifest.json"))
    ap.add_argument("--expectations", default=str(FIXTURE / "expected" / "expectations.json"))
    ap.add_argument("--backend", choices=[REAL_BACKEND, "double"],
                    default=REAL_BACKEND,
                    help=f"the agent backend (default: {REAL_BACKEND} — the real "
                         f"measurement; 'double' is the keyless plumbing self-check)")
    ap.add_argument("--model", default=None,
                    help="the librarian model (default: librarian Settings' own default). The "
                         f"{REAL_BACKEND!r} backend needs a provider-prefixed id, e.g. "
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
    # A `--kinds` naming every kind the set carries is the WHOLE set and still owes the pinned
    # denominators — decide from what was SELECTED, never from whether the flag was typed.
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

    An unknown kind is refused by name: a typo that produced an empty run would print a table of
    zero denominators, which reads exactly like a backend that files nothing.
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


# The rule for any future backend that cannot serve every kind: refuse the run before the queue is
# touched, never let an unmeasurable capture score as a filing failure.


def _subset(manifest: dict, expectations: dict, kinds: list) -> tuple:
    """The manifest and the expectations narrowed to `kinds`, keeping every other key of both.

    Narrowed by the manifest's own ids: the yardstick file records no kind, and giving it one would
    be a second place for the two halves to disagree about what a capture is.
    """
    captures = [capture for capture in manifest["captures"] if capture.get("kind") in kinds]
    ids = {capture["id"] for capture in captures}
    return ({**manifest, "captures": captures},
            {**expectations, "expectations": [entry for entry in expectations["expectations"]
                                              if entry["id"] in ids]})


# The two keys an ask-back expectation used to carry. Named rather than forgotten: an entry that
# grows one again would be scored on its `expect` block alone and its second half would vanish
# without a word, which is the failure `_check_set`'s third refusal exists to make loud.
RETIRED_ENTRY_KEYS = ("reply", "after_reply")


def _expect_blocks(entry: dict) -> list:
    """Every scored moment one entry declares — exactly one since ADR 041 retired the park."""
    return [entry["expect"]]


def _denominators(expectations: dict) -> dict:
    """The per-facet denominators the file on disk would produce, without spending a run."""
    counts: collections.Counter = collections.Counter()
    for entry in expectations["expectations"]:
        for block in _expect_blocks(entry):
            counts.update(name for name in block if name in FACETS)
    return dict(counts)


def _check_set(manifest: dict, expectations: dict, *, whole_set: bool = True) -> None:
    """Refusals against a drifted golden set, all BEFORE a model call is spent. Each would otherwise
    look like a backend result rather than a set defect:

    1. the two halves name the same captures, COUNTED rather than set-compared (a duplicate in one
       half and an omission in the other cancel out under `set()`);
    2. every expectation key is a facet the scorer knows — `score_phase` ignores what it does not
       recognize, so `achor:` would silently leave the anchor denominator;
    3. no entry carries the retired ask-back keys (`RETIRED_ENTRY_KEYS`);
    4. the denominators are the pinned ones;
    5. a decision entry asserts a `title` or an `anchor`, and title-less entries come LAST.

    `whole_set=False` is a PROPER subset: only check 4 changes, to "this subset scores something".
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

    parked = [entry["id"] for entry in expectations["expectations"]
              if any(key in entry for key in RETIRED_ENTRY_KEYS)]
    if parked:
        sys.exit(f"these expectations carry {list(RETIRED_ENTRY_KEYS)}, which nothing scores any "
                 f"more: a capture never waits on a person (ADR 041), so there is no reply to send "
                 f"and no second phase to score. The block would be read by nobody and the capture "
                 f"would quietly be measured on its `expect` half alone: {parked}")

    # An entry asserting neither title nor anchor matches whatever page is left; an anchor-only
    # entry written before a titled sibling takes that sibling's page and scores a correct page set
    # a miss. Neither failure would point at the expectation file, so both are refused here.
    empty, misordered = [], []
    for entry in expectations["expectations"]:
        for block in _expect_blocks(entry):
            decisions = block.get("decisions") or []
            if any("title" not in d and "anchor" not in d for d in decisions):
                empty.append(entry["id"])
            seen_titleless = False
            for decided in decisions:
                if "title" not in decided:
                    seen_titleless = True
                elif seen_titleless:
                    misordered.append(entry["id"])
                    break
    if empty:
        sys.exit(f"these expectations name a decision with neither a `title` nor an `anchor`, so it "
                 f"matches whatever page is left and measures nothing: {sorted(set(empty))}")
    if misordered:
        sys.exit(f"these expectations put a title-less decision BEFORE a titled one; matching is "
                 f"greedy in file order and an anchor-only entry is the weakest matcher, so it "
                 f"would take the titled entry's page and score a correct page set a miss. Write "
                 f"the titled entries first: {sorted(set(misordered))}")

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
    """One measurement, end to end. `kinds` is `main`'s to pass — that is where a subset is
    resolved and refused."""
    # Deferred so the scorer stays importable without any of this installed.
    import pytest

    from stigmergy.capture import schema
    from stigmergy.kernel.frontmatter import split_frontmatter
    from stigmergy.librarian import config as librarian_config
    from stigmergy.librarian import githubapp, worker
    from stigmergy.librarian.agent import build_agent
    from stigmergy.librarian.errors import LibrarianConfigError
    from tests import testdb
    from tests.librarian import support

    # ── nothing a measurement does may touch state a human is using ────────────────────────────
    # `make` exports the operator's own `.env` here. With the App configured, `_file` mints a REAL
    # installation token and pushes to github.com instead of this run's throwaway bare remote. The
    # names are imported, never retyped, so a renamed variable breaks the import instead of
    # escaping a list written from memory. Unconditional, both backends, no escape hatch.
    for name in (githubapp.APP_ID_ENV, githubapp.INSTALLATION_ID_ENV, githubapp.PRIVATE_KEY_ENV,
                 githubapp.PRIVATE_KEY_FILE_ENV, githubapp.APP_LOGIN_ENV):
        os.environ.pop(name, None)
    # Same doctrine, one credential over: a filed meeting triggers `_file_meeting`'s best-effort
    # view regeneration, which would otherwise spend real money this instrument does not price. No
    # score depends on it — scored pages are read back at `result_ref`'s sha, the filing's own commit.
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
            # The eval owns the queue for a run. `tests.testdb` has already refused any DSN but the
            # test database's, so this can only wipe the suite's own.
            cur.execute("DELETE FROM capture_queue")
        with tempfile.TemporaryDirectory() as tmp:
            env = support.build_repo(os.path.join(tmp, "git"), source=args.repo)
            settings = support.build_settings(
                env, worktree_root=os.path.join(tmp, "worktrees"),
                backend=args.backend, model=model)
            # The worker's own pre-flight, before a capture is claimed: without it a missing
            # `gitleaks` or credential produces a table of zeros with the cause buried under
            # attempts-exhausted noise.
            try:
                worker.startup_checks(settings)
            except LibrarianConfigError as ex:
                sys.exit(f"the filing golden cannot run against this configuration: {ex}")
            counting = CountingAgent(build_agent(settings))
            deps = support.build_deps(env, settings, agent=counting)

            total = len(manifest["captures"])
            for n, capture in enumerate(manifest["captures"], 1):
                entry = by_id[capture["id"]]
                # Progress on stderr; stdout stays the report.
                print(f"[{n:2d}/{total}] {capture['id']:34s} ", end="", flush=True,
                      file=sys.stderr)
                phases += _drive(conn, deps, counting, env, capture, entry,
                                 materials=materials, schema=schema, worker=worker,
                                 support=support, split_frontmatter=split_frontmatter)
                print("", file=sys.stderr)

    report = aggregate(phases, backend=args.backend, model=model,
                       wall_s=time.perf_counter() - started, kinds=kinds)
    print(render(report))
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False,
                                                default=str), encoding="utf-8")
        print(f"report -> {args.report}")

    # Only a REAL-instrument run appends to the durable series, and only one that MEASURED
    # something (`_withheld_reason`): a row from a run that died on its configuration is
    # indistinguishable there from a backend that files badly.
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

    Two conditions, each a table produced without measuring a backend: any phase ended `failed` (the
    instrument breaking, not the backend), or no phase ever called the agent. A single capture may
    legitimately spend zero passes; a whole run at zero is plumbing. The exit code is untouched
    either way.
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
           schema, worker, support, split_frontmatter) -> list:
    """One golden capture, submitted and drained through the real path. Returns its scored phase.

    ONE phase, always — a capture never waits on a person (ADR 041), so there is no reply to send
    and nothing to re-file. The list return is kept because `aggregate` reads a flat list of phases
    and a caller that has to remember whether this returns one or many is how a phase gets dropped.

    Each capture is still submitted and drained on its own, so exactly one row is claimable at any
    moment and `process_next` cannot pick up a different one — which is what lets a
    duplicate-refusal capture depend on an earlier one having filed.
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
    del item                            # submitted; the row is found again by draining the queue
    result = _drain_one(conn, deps, worker, capture_id=capture["id"], what="its own capture")
    observed = _observe(result, counting.calls, env=env, support=support,
                        split_frontmatter=split_frontmatter)
    print(f"{result.status:12s}", end="", flush=True, file=sys.stderr)
    return [_phase(capture["id"], "only", entry["expect"], observed)]


def _drain_one(conn, deps, worker, *, capture_id: str, what: str):
    """`worker.process_next`, with the empty queue named for what it is.

    `None` is legitimate for a polling service and a contradiction here — exactly one row is
    claimable at this moment. Unpacking it blindly gives a tuple-unpacking traceback whose real
    cause (a submit that never landed, a stale lease, another worker on this database) is nowhere
    in it.
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

    Pages are read back out of git at the commit that was pushed (`support.read_filed_page`), never
    from a worktree or from the agent's own account of what it wrote — what landed in the commit is
    all a reader of the knowledge repo will ever see.
    """
    report = result.report or {}
    observed = {
        "status": result.status,
        "reason": report.get("reason_code", ""),
        "attempts": attempts,
        # A corrective retry is the last pass an item may spend, so bounces is passes minus one —
        # except on a refusal decided before the agent ran, which spends none at all.
        "bounces": max(0, attempts - 1),
        "cost_usd": float(report.get("cost_usd", 0.0) or 0.0),
        "edits": list(report.get("pages_edited") or []),
        # The identities this filing CREATED unconfirmed, by the NAME the account chose — the id
        # is `slugify(name)` and would score the spelling, which `_proposals_match` exists not to.
        "proposals": [str(e.get("name", "")) for e in (report.get("entities_proposed") or [])
                      if isinstance(e, dict) and e.get("name")],
        # Reported, never scored. A filing that recognised the name as a registered entity's
        # SPELLING proposed an alias instead of an identity, and that is the near miss a reader of
        # a red `proposals` cell needs in front of them.
        "proposed_aliases": [f"{a.get('entity', '')}: {a.get('alias', '')}"
                             for a in (report.get("aliases_proposed") or [])
                             if isinstance(a, dict) and a.get("alias")],
        # Diagnostics for a miss, chosen so two reports from the same backend diff to nothing but
        # `wall_s` and `cost_usd`. Do not add the report's summary sentence: it embeds the commit
        # sha, which differs on every run.
        "page_path": report.get("page_path", ""),
        "anchored_to": report.get("anchored_to", ""),
    }
    if "@" not in (result.result_ref or ""):
        return observed                 # a refusal: nothing was committed to read back
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
        return observed

    page_path = report.get("page_path", "")
    observed["folder"] = _folder_of(page_path)
    observed["type"] = _page_type(env, sha, page_path, support, split_frontmatter)
    observed["anchor"] = _page_anchor(env, sha, page_path, support, split_frontmatter)
    return observed


def _frontmatter(env, sha: str, page_path: str, support, split_frontmatter) -> dict | None:
    """The page's frontmatter at `sha`, or `None` when the page could not be read back at all.

    `None` rather than `{}`: an unreadable page and a page with no fields are different
    observations, and only one may be allowed to look like an anchoring outcome. Never a crash —
    a run that died mid-way would lose every capture behind it, each of which cost an agent pass.
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

    `entity: []` on a COMMITTED page means company-wide — `gate_anchoring` never lets an unresolved
    entity reach a commit. That holds only for a page actually read back, so a missing page answers
    `unreadable`, a kind no expectation names: otherwise it would score as a lucky company-wide hit.
    """
    frontmatter = _frontmatter(env, sha, page_path, support, split_frontmatter)
    if frontmatter is None:
        return {"kind": "unreadable", "ids": []}
    return _anchor_from_page(frontmatter)


if __name__ == "__main__":
    sys.exit(main())
