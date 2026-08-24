#!/usr/bin/env python3
"""Golden QA runner — on-demand, keyed instrument (not wired into CI).

Drives `evals/qa_golden.json` through the full answering loop over the Postgres index and reports
three quality axes, each with its own denominator:

  - HONESTY      refusal rate over genuinely unanswerable questions (`kind: refusal` alone). The
                 armed bar is `bars.BAR_HONESTY`.
  - GROUNDEDNESS fraction of answerable questions answered with the expected figure/citation and a
                 verdict that is not `failed`.
  - REFUTATION   fraction of corrective questions — false-premise (`refute`) and mixed-entity
                 (`disambiguate`) — handled by EITHER an honest refusal OR a cited
                 correction/disambiguation carrying the corpus's real figure.

RETRY RATE and SECONDS/QUESTION are latency axes: no bar, gate nothing. A retried ask almost always
still ends `verified`, so the quality axes are blind to that cost by construction.

`_score` is not literal: a figure matches any numerically equivalent spelling, an ISO date any
rendering of the same day, and `cites` accepts a chain where any one page is valid. The corpus is
`evals/corpus/`, committed and frozen so the series stays comparable.

  # keyless self-check (plumbing only — appends no history row)
  python evals/run_qa.py --embedder fake --llm fake --rebuild --repo evals/corpus

  # the real measurement (needs OPENROUTER_API_KEY)
  python evals/run_qa.py --embedder openrouter --llm openrouter \
      --rebuild --repo evals/corpus --report evals/out/qa-glm.json
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
from stigmergy.kernel.llm import ANSWER_MODEL  # noqa: E402
from stigmergy.server import entity_aliases  # noqa: E402
from stigmergy.server.identity import resolve_audiences  # noqa: E402
from stigmergy.server.service import BrainService  # noqa: E402
from stigmergy.server.settings import Settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", default=str(ROOT / "evals" / "qa_golden.json"))
    ap.add_argument("--identities", default=str(ROOT / "evals" / "qa_identities.json"))
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--embedder", choices=["openrouter", "fake"], default=None,
                    help="query embedder (default: match the built index's model)")
    ap.add_argument("--llm", choices=["openrouter", "fake"], default="openrouter",
                    help="the synthesizer backend (default: openrouter)")
    ap.add_argument("--rebuild", metavar="", nargs="?", const=True, default=False,
                    help="rebuild the index first (requires --repo)")
    ap.add_argument("--repo", default=None, help="knowledge-repo checkout (with --rebuild)")
    # Without this, `Settings` measures a server with entity-first resolution silently OFF while
    # the deployed one has it on. Same `--repo` convention as `Settings.from_args`; the loader
    # fails open, so a corpus shipping no `ops/` scores identically either way.
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
    inspectable. `entity_registry_path` is the FALLBACK source, exactly as on a real server: where
    the measured database carries a registry snapshot in `ops_file_snapshot`, the service answers from that and
    this path is never read (`BrainService._registry_source`). It must still be set for a database
    that has none, or entity-first resolution is off under measurement while live on the deployed
    server."""
    return Settings(identity=identity_name, identities_path=args.identities,
                    entity_registry_path=(args.entity_registry
                                          or entity_aliases.default_path(args.repo)),
                    llm=args.llm, model=ANSWER_MODEL)


def _run(args, golden) -> int:
    default_identity = golden.get("default_identity", "steward")
    with store.connect(args.dsn) as conn:
        if args.rebuild:
            stats = build.rebuild(conn, args.repo, build_embedder(args.embedder or "openrouter"))
            print(f"rebuilt: {stats['pages']} pages · model={stats['model']}")
        meta = store.read_meta(conn)
        if meta is None:
            sys.exit("the index is empty — pass --rebuild --repo <dir> or build it first")
        if args.entity_registry and store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) is not None:
            # A flag that silently does nothing is worse than no flag: this database carries a
            # registry snapshot, and the service prefers it, so the run measures the registry the
            # INDEX was built from — not the file named here. `--rebuild --repo <dir>` is what
            # makes the two the same.
            print(f"note: --entity-registry {args.entity_registry} is IGNORED — this index carries "
                  f"a registry snapshot and the service answers from it", file=sys.stderr)
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
            # Progress on stderr; stdout stays the report.
            print(f"[{n:2d}/{total}] {case['id']:24s} ", end="", flush=True, file=sys.stderr)
            # Wall time measured from OUTSIDE `ask` — what a Slack user actually waits through.
            started = time.perf_counter()
            res = asyncio.run(svc.ask(case["q"]))
            scored = _score(case, res)
            scored["seconds"] = round(time.perf_counter() - started, 2)
            print(("ok" if scored["ok"] else f"MISS ({scored['verdict']}"
                   f"{', refused' if scored['refused'] else ''})")
                  + f"  {scored['seconds']:.1f}s{' retried' if scored['retried'] else ''}",
                  file=sys.stderr)
            results.append(scored)

    report = _aggregate(golden, results, "fake" if args.llm == "fake" else ANSWER_MODEL)
    print(_render(report))
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"report -> {args.report}")
    # Only a real-instrument run appends to the durable series; the keyless `--llm fake` self-check
    # has no quality number worth keeping. Never fails the run.
    if args.llm == "openrouter":
        eval_history.append_run(
            suite="qa", git_sha=eval_history.resolve_git_sha(ROOT),
            metrics={"honesty": report["honesty"], "groundedness": report["groundedness"],
                    "refutation": report["refutation"], "model": report["model"],
                    "counts": report["counts"],
                    "retry_rate": report["retry_rate"], "seconds": report["seconds"],
                    **eval_history.corpus_provenance(args.repo)})
    return 0


# A figure expectation must be the WHOLE expectation. `"routing v2"`, `"Q3"` and `"2026-02-10"`
# deliberately do NOT match: they carry digits but are prose, and numeric equivalence loose on them
# would score any answer containing a 2 as a hit.
_PURE_FIGURE = re.compile(r"^\s*\d[\d.,]*\s?(?:bn|[kKmMbB])?\s?[%xX]?\s*$")
_SMALL_INTEGERS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


# Dimension-BLIND on purpose: this asks "is the right number present", so it must not demand `40%`
# over `40 per cent`. Both sides strip the dimension before the subset test.
def _figures(text: str) -> set[str]:
    return {canon.removesuffix("%") for canon in numbers.number_pool(text)}


def _small_integer_matches(expected: str, answer: str) -> bool:
    stripped = expected.strip()
    if not stripped.isascii() or not stripped.isdecimal():
        return False
    value = int(stripped)
    if value < len(_SMALL_INTEGERS):
        forms = (_SMALL_INTEGERS[value],)
    elif value < 100:
        tens, ones = divmod(value, 10)
        phrase = _TENS[tens] if not ones else f"{_TENS[tens]} {_SMALL_INTEGERS[ones]}"
        forms = (phrase, phrase.replace(" ", "-"))
    else:
        return False
    return any(re.search(rf"(?<![A-Za-z]){re.escape(form)}(?![A-Za-z])", answer, re.I) for form in forms)


# An ISO date in an expectation must match the date HOWEVER the answer writes it — equivalence,
# never a wider literal match.
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")


def _date_renderings(iso: str) -> list[str]:
    """Every spelling of one calendar date this scorer accepts: ISO, both long forms and the
    numeric variants, with and without the leading zero. Day-first and month-first both, because
    English writes both and the expectation has already fixed the day."""
    year, month, day = (int(part) for part in iso.split("-"))
    name = _MONTHS[month - 1]
    return [iso,
            f"{day} {name} {year}", f"{day:02d} {name} {year}",
            f"{name} {day}, {year}", f"{name} {day:02d}, {year}",
            f"{day}/{month}/{year}", f"{day:02d}/{month:02d}/{year}",
            f"{month}/{day}/{year}", f"{month:02d}/{day:02d}/{year}",
            f"{day}-{month}-{year}", f"{day:02d}-{month:02d}-{year}"]


def _date_matches(iso: str, answer: str) -> bool:
    """The renderings above, plus the YEARLESS long forms prose actually writes ("on 12 August").
    Year discrimination survives: a negative lookahead refuses a year-bearing spelling with the
    wrong year, so the full-form renderings are the only way one can match."""
    if any(rendering in answer for rendering in _date_renderings(iso)):
        return True
    year, month, day = (int(part) for part in iso.split("-"))
    name = _MONTHS[month - 1]
    return bool(re.search(rf"\b0?{day} {name}\b(?!,? \d)", answer)
                or re.search(rf"\b{name} 0?{day}\b(?!,? \d)", answer))


def _expectation_met(case: dict, answer: str) -> bool:
    """Literal first, then numeric equivalence for a figure, then date equivalence for an ISO date.

    A literal `in` alone scores a right answer a miss whenever the model picks another notation
    (`1.074` for `1074`, `512k` for `512000`, `10 February 2026` for `2026-02-10`) — a yardstick
    failure recorded as a groundedness failure of the brain.
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
    if _small_integer_matches(expected, answer):
        return True
    want = _figures(expected)
    return bool(want) and want <= _figures(answer)


def _citation_hit(case: dict, citations: list[dict]) -> bool:
    """`cites` is one page path or a CHAIN of them — any one is a hit.

    A claim that several documents establish may legitimately be cited to any one of them — a
    chain is "these are all correct provenance for this claim", not "cite all of these". Substring
    matching runs in both directions.
    """
    chain = case.get("cites") or []
    if isinstance(chain, str):
        chain = [chain]
    return any(expected and (expected in c["path"] or c["path"] in expected)
               for expected in chain for c in citations)


def _score(case: dict, res: dict) -> dict:
    """Per-question outcome across the three kinds:

    - `refusal`      an unanswerable question must be REFUSED.
    - `refute`       a false premise: refusing is correct, and so is CORRECTING it with the
                     corpus's real figure, cited and verified.
    - `disambiguate` a mixed-entity question: refusing is correct, and so is a cited
                     disambiguation carrying the sibling's real figure. Accepted residual, named
                     with eyes open — code cannot judge attribution prose, so a cited answer that
                     misattributes also passes; the citation is the mitigation.
    - everything else — groundedness: answered, expectation met, expected page cited, verdict not
                     `failed`.
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
           # Recorded for EVERY question, not only the misses: nearly every retried ask still ends
           # `verified`, so the retry tax is paid entirely by questions scored `ok`. The verifier's
           # findings ride along as the diagnosis of why the retry fired.
           "retried": res.get("retried", False),
           "citation_problems": res["verdict"].get("citation_problems", []),
           "unverified_figures": res["verdict"].get("unverified_figures", [])}
    if not ok:
        # Without the answer itself the report cannot tell a gate-suppressed answer from a verified
        # one that missed the golden string or cited a sibling page.
        out["miss"] = {
            "expected": case.get("expect_contains", ""), "expected_found": has_expected,
            "expected_page": case.get("cites", ""), "cited_expected_page": cited,
            "answer": res["answer_markdown"][:400], "reason": res.get("reason", ""),
            "citations": [c["path"] for c in res["citations"]],
        }
    return out


def _aggregate(golden: dict, results: list[dict], model: str) -> dict:
    """Three axes, each with its own denominator.

    The corrective kinds (`refute`, `disambiguate`) sit on `refutation` and NOT in the honesty
    denominator: both are answerable — by contradiction, by cited disambiguation — and a bar is
    armed on honesty, which has to keep meaning "refusal rate". Groundedness measures plain
    answering, so it does not absorb them either.
    """
    unanswerable = [r for r in results if r["kind"] == "refusal"]
    corrective = [r for r in results if r["kind"] in ("refute", "disambiguate")]
    answerable = [r for r in results if r["kind"] not in ("refusal", "refute", "disambiguate")]
    share = lambda rs: sum(r["ok"] for r in rs) / len(rs) if rs else 0.0  # noqa: E731
    honesty = share(unanswerable)
    by_family: dict[str, list[bool]] = {}
    for r in results:
        by_family.setdefault(r["family"], []).append(r["ok"])
    # `seconds` is ABSENT, never zero, when nothing was timed (the scorer's unit tests): a zeroed
    # latency would read as "instantaneous" rather than "not measured".
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
        # Named, not just counted: a rate alone cannot say whether it is the same questions
        # retrying every run.
        lines += ["", "retried: " + ", ".join(retried)]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
