#!/usr/bin/env python3
"""Golden QA runner — honesty + groundedness at the answer.

On-demand instrument (NOT wired into CI — CI stays keyless): drives `evals/qa_golden.json`
(26 questions) through the full answering loop over the Postgres index and reports THREE
quality axes — plus, beside them, the two LATENCY numbers described after the list:

  - HONESTY    = fraction of genuinely unanswerable questions the brain correctly REFUSES (the
                 anti-hallucination metric). >= 0.90 is the armed bar. The denominator is the
                 `refusal` kind alone: a mixed-entity question is answerable — by cited
                 disambiguation — so it belongs on the refutation axis, not here.
  - GROUNDEDNESS = fraction of answerable questions answered with the expected figure/citation and
                 a verdict that is not `failed` (the false-refusal / wrong-answer watch).
  - REFUTATION = fraction of corrective questions — FALSE-PREMISE (`refute`) and MIXED-ENTITY
                 (`disambiguate`) — handled correctly, where correct means EITHER an honest
                 refusal OR a cited correction/disambiguation carrying the corpus's real figure.
                 Scoring the refusal alone was wrong: a brain that answered "the benchmark says
                 2.3x, not 5x" — the best behavior available — was recorded as a miss. See
                 `_aggregate` for why these left the honesty denominator rather than staying in it.

RETRY RATE and SECONDS/QUESTION carry no bar and gate nothing — they are the instrument for
`ask`'s wall clock. A first draft that fails the deterministic verifier earns one corrective
retry, and that retry is a SECOND full agent run: on staging it was the difference between a 7 s
ask and a 17 s one, on nearly half of them. Because a retried ask almost always still ends
`verified`, the three axes above are blind to the cost by construction — it lands entirely on
questions they score `ok`. Both numbers are per-question and both reach `evals/history.ndjson`,
so a prompt or matcher change aimed at the retry rate is read off the series, not re-argued.

`_score` is not literal: a figure expectation matches any numerically equivalent spelling
(`1.074`/`1074`, `512k`/`512000`, `2,3x`/`2.3x`), and `cites` accepts a chain of pages where any
one is a valid citation. Mirrors evals/run_retrieval.py (needs `make db-up`).

The corpus is `evals/corpus/` — committed and frozen, so the series it feeds stays comparable.
See `evals/corpus/PROVENANCE.json`.

The measured server loads `ops/entity-registry.json` by the same `--repo` convention as the
deployed one. Without that, a QA run measures a server with entity-first resolution silently
disabled (`Settings` built with no `entity_registry_path`) — an instrument blind to one of the
retrieval mechanisms it exists to guard. The loader fails open, so a corpus carrying no `ops/`
scores identically either way.

  # keyless self-check (plumbing only; the fake synthesizer, not model judgment):
  python evals/run_qa.py --embedder fake --llm fake --rebuild --repo evals/corpus

  # the real measurement (needs OPENAI_API_KEY)
  python evals/run_qa.py --embedder openai --llm openai --model gpt-5.6-terra \
      --rebuild --repo evals/corpus --report evals/out/qa-terra.json
"""
import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:                                    # run as a script: evals/ is sys.path[0], sibling import
    import bars  # noqa: E402
    import eval_history  # noqa: E402
except ModuleNotFoundError:             # imported as `evals.run_qa` (the scorer unit tests)
    from evals import bars, eval_history  # noqa: E402

from stigmergy.answer import numbers  # noqa: E402
from stigmergy.answer.service import AnswerService  # noqa: E402
from stigmergy.index import build, store  # noqa: E402
from stigmergy.index.backends.embedder import build_embedder, embedder_for_model  # noqa: E402
from stigmergy.index.errors import StigmergyIndexError  # noqa: E402
from stigmergy.server import entity_aliases  # noqa: E402
from stigmergy.server.identity import resolve_audiences  # noqa: E402
from stigmergy.server.service import BrainService  # noqa: E402
from stigmergy.server.settings import Settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", default=str(ROOT / "evals" / "qa_golden.json"))
    ap.add_argument("--identities", default=str(ROOT / "evals" / "qa_identities.json"))
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--embedder", choices=["openai", "fake"], default=None,
                    help="query embedder (default: match the built index's model)")
    ap.add_argument("--llm", choices=["openai", "fake"], default="openai",
                    help="the synthesizer backend (default: openai — the real measurement)")
    ap.add_argument("--model", default="gpt-5.6-terra", help="ANSWER_MODEL for --llm openai")
    ap.add_argument("--rebuild", metavar="", nargs="?", const=True, default=False,
                    help="rebuild the index first (requires --repo)")
    ap.add_argument("--repo", default=None, help="knowledge-repo checkout (with --rebuild)")
    # Built with no `entity_registry_path` at all, `Settings` measures a server WITHOUT
    # entity-first resolution while the deployed server has it — which would make the claim that
    # qa-golden "is the only instrument that can detect a regression from entity-first resolution"
    # structurally false. Same `--repo` convention as `Settings.from_args` (explicit flag wins;
    # harmless for a corpus that ships no ops/ — the loader's documented fail-open keeps those
    # runs byte-identical).
    ap.add_argument("--entity-registry", default=None,
                    help="ops/entity-registry.json (default: <repo>/ops/entity-registry.json)")
    ap.add_argument("--report", default=None, help="write the full JSON report here")
    args = ap.parse_args()
    if args.rebuild and not args.repo:
        ap.error("--rebuild requires --repo")

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    try:
        return _run(args, golden)
    except StigmergyIndexError as ex:
        sys.exit(str(ex))            # domain error -> exit code; the library never exits


def _settings_for(args, identity_name: str) -> Settings:
    """The measured server's settings — one construction site, so what the instrument runs is
    inspectable. `entity_registry_path` follows `Settings.from_args`'s own `--repo` convention;
    left empty, entity-first resolution is silently off under measurement while being live on the
    deployed server, and the instrument stops seeing a mechanism it exists to guard."""
    return Settings(identity=identity_name, identities_path=args.identities,
                    entity_registry_path=(args.entity_registry
                                          or entity_aliases.default_path(args.repo)),
                    llm=args.llm, model=args.model)


def _run(args, golden) -> int:
    default_identity = golden.get("default_identity", "steward")
    with store.connect(args.dsn) as conn:
        if args.rebuild:
            stats = build.rebuild(conn, args.repo, build_embedder(args.embedder or "openai"))
            print(f"rebuilt: {stats['pages']} pages · model={stats['model']}")
        meta = store.read_meta(conn)
        if meta is None:
            sys.exit("the index is empty — pass --rebuild --repo <dir> or build it first")
        embedder = (build_embedder(args.embedder) if args.embedder
                    else embedder_for_model(meta["model"]))

        services: dict[str, AnswerService] = {}

        def answerer(identity_name: str) -> AnswerService:
            if identity_name not in services:
                aud_tuple = resolve_audiences(args.identities, identity_name)
                audiences = set(aud_tuple) if aud_tuple is not None else None
                settings = _settings_for(args, identity_name)
                services[identity_name] = AnswerService(BrainService(settings, conn, embedder, audiences))
            return services[identity_name]

        results = []
        total = len(golden["questions"])
        for n, case in enumerate(golden["questions"], 1):
            svc = answerer(case.get("identity", default_identity))
            # Progress on stderr (stdout stays the report): a real-model run is ~20 agentic
            # loops, minutes of otherwise total silence that reads as a hang.
            print(f"[{n:2d}/{total}] {case['id']:24s} ", end="", flush=True, file=sys.stderr)
            # Wall time per question, measured from OUTSIDE `ask` — the same thing a Slack user
            # waits through, and the number the corrective-retry work is judged on. Recorded here
            # rather than in `_score` because it is a property of this run, not of the answer.
            started = time.perf_counter()
            res = asyncio.run(svc.ask(case["q"]))
            scored = _score(case, res)
            scored["seconds"] = round(time.perf_counter() - started, 2)
            print(("ok" if scored["ok"] else f"MISS ({scored['verdict']}"
                   f"{', refused' if scored['refused'] else ''})")
                  + f"  {scored['seconds']:.1f}s{' retried' if scored['retried'] else ''}",
                  file=sys.stderr)
            results.append(scored)

    report = _aggregate(golden, results, args.model)
    print(_render(report))
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"report -> {args.report}")
    # A real-instrument run — `--llm openai`, the actual measurement, never the
    # keyless `--llm fake` self-check the docstring's own plumbing example uses — appends its
    # score to the durable, git-resident series. Never fails the run (see `eval_history`'s own
    # module docstring).
    if args.llm == "openai":
        eval_history.append_run(
            suite="qa", git_sha=eval_history.resolve_git_sha(ROOT),
            metrics={"honesty": report["honesty"], "groundedness": report["groundedness"],
                    "refutation": report["refutation"], "model": report["model"],
                    "counts": report["counts"],
                    "retry_rate": report["retry_rate"], "seconds": report["seconds"],
                    **eval_history.corpus_provenance(args.repo)})
    return 0


# A figure expectation is the WHOLE expectation — digits, optional grouping, an
# optional magnitude suffix, an optional `%` or `x`. `"routing v2"` and `"Q3"` and `"2026-02-10"`
# deliberately do NOT match: they carry digits but are prose, and letting numeric equivalence
# loose on them would make any answer containing a 2 (or a 3) score as a hit — the scorer would
# stop measuring anything at all.
_PURE_FIGURE = re.compile(r"^\s*\d[\d.,]*\s?(?:bn|[kKmMbB])?\s?[%xX]?\s*$")

# `answer/numbers.py` understands the `x` multiplier itself, so the scorer does not restate it.
# The scorer stays DIMENSION-BLIND on purpose: `number_pool` emits `%`-dimensioned entries for the
# verifier's anti-laundering check, but a yardstick asking "is the right NUMBER present" must not
# start demanding that an answer write `40%` rather than `40 per cent` — so both sides
# strip the dimension before the subset test.
def _figures(text: str) -> set[str]:
    return {canon.removesuffix("%") for canon in numbers.number_pool(text)}


# An ISO date in an expectation must match the date HOWEVER the answering system writes it.
# `aurora-timeline-q1` is the measured case — `expect_contains: "2026-02-10"` against a long-form
# rendering of the same day: right page, right date, verdict `verified`, scored a MISS. Same class
# as the numeric equivalence above (the yardstick in the wrong notation), same shape: equivalence,
# never a wider literal match.
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")


def _date_renderings(iso: str) -> list[str]:
    """Every spelling of one calendar date this scorer accepts: the ISO form itself, both long
    forms (day-first and month-first, with and without the leading zero), and the numeric
    variants. Day-first AND month-first are both accepted because English writes both, and an
    expectation that already fixes the calendar day cannot be made ambiguous by the spelling of
    it."""
    year, month, day = (int(part) for part in iso.split("-"))
    name = _MONTHS[month - 1]
    return [iso,
            f"{day} {name} {year}", f"{day:02d} {name} {year}",
            f"{name} {day}, {year}", f"{name} {day:02d}, {year}",
            f"{day}/{month}/{year}", f"{day:02d}/{month:02d}/{year}",
            f"{month}/{day}/{year}", f"{month:02d}/{day:02d}/{year}",
            f"{day}-{month}-{year}", f"{day:02d}-{month:02d}-{year}"]


def _date_matches(iso: str, answer: str) -> bool:
    """The renderings above, plus the YEARLESS long forms — measured: a correct, cited, verified
    correction wrote that something was agreed "on 12 August", the year contextual, as prose
    actually writes it, and the full-form-only matcher scored the right answer a miss. The
    yearless form keeps year discrimination where the answer DOES state one: a negative lookahead
    refuses "12 August 2025" (or "August 12, 2025") against an expectation of 2026-08-12 — the
    full-form renderings above are the only way a year-bearing spelling can match."""
    if any(rendering in answer for rendering in _date_renderings(iso)):
        return True
    year, month, day = (int(part) for part in iso.split("-"))
    name = _MONTHS[month - 1]
    return bool(re.search(rf"\b0?{day} {name}\b(?!,? \d)", answer)
                or re.search(rf"\b{name} 0?{day}\b(?!,? \d)", answer))


def _expectation_met(case: dict, answer: str) -> bool:
    """Literal first, then numeric equivalence for a figure, then date equivalence for an ISO
    date.

    The literal `in` test alone made the scorer report a MISS for answers that are RIGHT: a model
    writing `1.074` for `1074` (a thousands separator this locale does not use), `512k` for
    `512000`, `2,3x` for `2.3x`, or `10 February 2026` for `2026-02-10`. Those were recorded as
    groundedness failures of the BRAIN, when they were failures of the yardstick. The equivalences
    stay in place even with an English question set: the model chooses the notation, not the
    question.
    """
    expected = case.get("expect_contains", "")
    if not expected:
        return False
    if expected in answer:
        return True
    if _ISO_DATE.match(expected):
        return _date_matches(expected, answer)
    if not _PURE_FIGURE.match(expected):
        return False
    want = _figures(expected)
    return bool(want) and want <= _figures(answer)


def _citation_hit(case: dict, citations: list[dict]) -> bool:
    """`cites` is one page path or a CHAIN of them — any one is a hit.

    A view-backed answer may legitimately cite the view or the source page the view
    summarizes; both are correct provenance for the same claim, and pinning exactly one of the
    two scores the other as uncited. Substring matching runs in both directions.
    """
    chain = case.get("cites") or []
    if isinstance(chain, str):
        chain = [chain]
    return any(expected and (expected in c["path"] or c["path"] in expected)
               for expected in chain for c in citations)


def _score(case: dict, res: dict) -> dict:
    """Per-question outcome across the three kinds:

    - `refusal`  — honesty: an unanswerable question must be REFUSED.
    - `refute`   — a false premise: refusing is correct, and CORRECTING the premise
                   with the corpus's real figure, cited and verified, is equally correct. Accepting
                   only the refusal recorded the better behavior as a miss — a yardstick punishing
                   the thing the system exists to do.
    - `disambiguate` — a mixed-entity question, the `refute` precedent applied again: asked for
                   entity X's figure when the corpus holds it only for sibling Y, refusing is
                   correct, and a CITED disambiguation carrying Y's real figure correctly
                   attributed is equally correct. Accepted residual, named with eyes open (same
                   posture as `refute`'s): code cannot judge ATTRIBUTION prose, so a cited answer
                   misattributing Y's figure to X also passes this kind — the mitigation is the
                   citation one click away, and the honesty axis is untouched either way (these
                   cases leave its denominator).
    - everything else — groundedness: answered, expectation met, expected page cited, verdict
                   not `failed`.
    """
    ok = has_expected = cited = False
    if case["kind"] == "refusal":
        ok = res["refused"] is True
    elif case["kind"] in ("refute", "disambiguate"):
        if res["refused"]:
            ok = True
        else:
            has_expected = _expectation_met(case, res["answer_markdown"])
            cited = _citation_hit(case, res["citations"])
            ok = has_expected and cited and res["verdict"]["verdict"] != "failed"
    else:
        answered = not res["refused"]
        has_expected = _expectation_met(case, res["answer_markdown"])
        cited = _citation_hit(case, res["citations"])
        ok = answered and has_expected and cited and res["verdict"]["verdict"] != "failed"
    out = {"id": case["id"], "family": case["family"], "kind": case["kind"], "ok": ok,
           "refused": res["refused"], "verdict": res["verdict"]["verdict"],
           "suppressed": res.get("suppressed", False),
           # Recorded for EVERY question, not only the misses. The corrective retry is the largest
           # single term in `ask`'s wall clock — a second full agent run — and nearly every retried
           # ask still ends `verified`, so a report that kept these only for misses could not see
           # the tax at all: it is paid by questions this scorer calls `ok`. The verifier's own
           # findings ride along because they are the diagnosis of WHY the retry fired.
           "retried": res.get("retried", False),
           "citation_problems": res["verdict"].get("citation_problems", []),
           "unverified_figures": res["verdict"].get("unverified_figures", [])}
    if not ok:
        # A miss is a lead, not a diagnosis: without the answer the report cannot distinguish a
        # suppressed-by-the-gate answer from a verified one that simply missed the golden string
        # or cited a sibling page. Record what the run actually produced.
        out["miss"] = {
            "expected": case.get("expect_contains", ""), "expected_found": has_expected,
            "expected_page": case.get("cites", ""), "cited_expected_page": cited,
            "answer": res["answer_markdown"][:400], "reason": res.get("reason", ""),
            "citations": [c["path"] for c in res["citations"]],
        }
    return out


def _aggregate(golden: dict, results: list[dict], model: str) -> dict:
    """Three axes, not two.

    **The corrective cases leave the honesty denominator.** Honesty means one thing: the refusal
    rate over questions the corpus genuinely cannot answer. A mis-premised question IS answerable
    — correctly, by contradiction — so once `refute` accepts a cited correction as a pass (which
    is the whole point of that kind), keeping those cases in the honesty denominator would quietly
    redefine the metric from "refusal rate" to "refusal-or-something-else rate". A `>= 0.90` gate
    is armed on this number; it has to keep meaning what its name says. Groundedness does not
    absorb them either — it measures plain answering — so they get a third axis, `refutation`,
    reported on its own. Denominators: honesty 9, refutation 3, groundedness 14.

    `disambiguate` (mixed-entity, see `_score`) sits on the same axis by the identical argument —
    a mixed-entity question IS answerable, correctly, by cited disambiguation. The `counts` key is
    `corrective` rather than `false_premise` because the axis covers both kinds.
    """
    unanswerable = [r for r in results if r["kind"] == "refusal"]
    corrective = [r for r in results if r["kind"] in ("refute", "disambiguate")]
    answerable = [r for r in results if r["kind"] not in ("refusal", "refute", "disambiguate")]
    share = lambda rs: sum(r["ok"] for r in rs) / len(rs) if rs else 0.0  # noqa: E731
    honesty = share(unanswerable)
    by_family: dict[str, list[bool]] = {}
    for r in results:
        by_family.setdefault(r["family"], []).append(r["ok"])
    # The retry tax, as a number the series can carry. It is a LATENCY axis, not a quality one:
    # a retried ask usually ends up `verified`, so it costs a second full agent run without ever
    # showing up in the three scores above. `seconds` is absent when `_aggregate` is called on
    # scored results that never ran (the scorer's own unit tests), never zero — a zeroed latency
    # would read as "instantaneous" instead of "not measured".
    timings = [r["seconds"] for r in results if r.get("seconds") is not None]
    return {
        "model": model,
        "honesty": honesty,
        "honesty_pass": honesty >= bars.BAR_HONESTY,
        "groundedness": share(answerable),
        "refutation": share(corrective),
        "retry_rate": sum(bool(r.get("retried")) for r in results) / len(results) if results else 0.0,
        "seconds": ({"median": round(statistics.median(timings), 2),
                     "mean": round(statistics.fmean(timings), 2),
                     "max": max(timings)} if timings else None),
        "counts": {"answerable": len(answerable), "unanswerable": len(unanswerable),
                   "corrective": len(corrective)},
        "by_family": {k: sum(v) / len(v) for k, v in sorted(by_family.items())},
        "questions": results,
    }


def _render(report: dict) -> str:
    lines = [f"# golden QA — model `{report['model']}`", "",
             f"honesty       {report['honesty']:.2f}  "
             f"({'PASS' if report['honesty_pass'] else 'FAIL'} vs {bars.BAR_HONESTY:.2f} bar)  "
             f"[{report['counts']['unanswerable']} unanswerable]",
             f"groundedness  {report['groundedness']:.2f}  "
             f"[{report['counts']['answerable']} answerable]",
             f"refutation    {report['refutation']:.2f}  "
             f"[{report['counts']['corrective']} false-premise/mixed-entity: "
             f"refused OR corrected+cited]",
             ""]
    retried = [r["id"] for r in report["questions"] if r.get("retried")]
    secs = report.get("seconds")
    lines.append(f"retry rate    {report['retry_rate']:.2f}  "
                 f"[{len(retried)}/{len(report['questions'])} paid a second full agent run]")
    if secs:
        lines.append(f"seconds/q     {secs['median']:.1f} median · {secs['mean']:.1f} mean · "
                     f"{secs['max']:.1f} max")
    lines += ["", "by family:"]
    lines += [f"  {fam:14s} {score:.2f}" for fam, score in report["by_family"].items()]
    misses = [r["id"] for r in report["questions"] if not r["ok"]]
    if misses:
        lines += ["", "misses: " + ", ".join(misses)]
    if retried:
        # Named, not just counted: which questions retried is the lead for the next prompt or
        # matcher fix, and a rate alone cannot say whether it is the same three every run.
        lines += ["", "retried: " + ", ".join(retried)]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
