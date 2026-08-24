#!/usr/bin/env python3
"""The release gates — armed thresholds over the three instruments, one verdict, one exit code.

  * retrieval golden — `final` R@k vs `bars.BAR_RECALL` (`evals/run_retrieval.py`)
  * QA golden        — honesty and groundedness vs their bars; refutation REPORTED, not gated
  * adversarial suite — the injection, ACL-leakage and forged-frontmatter categories, collected by
                        name; the collection floor is itself a CI test, so `-k` cannot fail open.

The noise rule: a real model over a real corpus is not deterministic, so an instrument whose bar
fails is re-run ONCE — but only when every failing bar sits within ONE question's weight of passing
(the report's own denominators set the weight). A wider miss is a regression and fails on the first
attempt; a runner exiting non-zero is an infra failure and fails immediately.

Both golden halves are REAL measurements, so this needs docker Postgres and OPENROUTER_API_KEY and
never runs in CI — it is the operator's release gate, `make gates`.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:                                    # run as a script (evals/ is sys.path[0])
    from bars import BAR_GROUNDEDNESS, BAR_HONESTY, BAR_RECALL
except ModuleNotFoundError:             # imported as `evals.run_gates` (the unit tests)
    from evals.bars import BAR_GROUNDEDNESS, BAR_HONESTY, BAR_RECALL

ADVERSARIAL_K = "adversarial_cat1 or adversarial_cat2 or adversarial_cat7"


def judge_retrieval(report: dict) -> list[str]:
    """Bar failures (empty = pass) from a run_retrieval report."""
    recall = report["summary"]["final"]["recall_at_k"]
    return [] if recall >= BAR_RECALL else [
        f"final R@{report['k']} {recall:.3f} < {BAR_RECALL:.2f}"]


def judge_qa(report: dict) -> list[str]:
    """Bar failures (empty = pass) from a run_qa report. Refutation is reported, never gated."""
    failures = []
    if report["honesty"] < BAR_HONESTY:
        failures.append(f"honesty {report['honesty']:.3f} < {BAR_HONESTY:.2f}")
    if report["groundedness"] < BAR_GROUNDEDNESS:
        failures.append(f"groundedness {report['groundedness']:.3f} < {BAR_GROUNDEDNESS:.2f}")
    return failures


_EPS = 1e-9


def within_noise_band(report: dict, kind: str) -> bool:
    """True iff EVERY failing bar is within one question's weight of its bar — the only shape the
    re-run is for. An unconditional re-run would let an intermittent regression through."""
    if kind == "retrieval":
        weight = 1.0 / max(1, report.get("questions", 1))
        return report["summary"]["final"]["recall_at_k"] >= BAR_RECALL - weight - _EPS
    counts = report.get("counts") or {}
    ok = True
    if report["honesty"] < BAR_HONESTY:
        weight = 1.0 / max(1, counts.get("unanswerable", 1))
        ok = ok and report["honesty"] >= BAR_HONESTY - weight - _EPS
    if report["groundedness"] < BAR_GROUNDEDNESS:
        weight = 1.0 / max(1, counts.get("answerable", 1))
        ok = ok and report["groundedness"] >= BAR_GROUNDEDNESS - weight - _EPS
    return ok


def _run(cmd: list[str]) -> int:
    print("gate>", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.call([str(c) for c in cmd], cwd=ROOT)


def _instrument(name, cmd, report_path, judge, kind) -> tuple[bool, list[str]]:
    """Run one golden instrument, judge its report; on a WITHIN-NOISE-BAND bar failure re-run
    ONCE (the constrained noise rule). An infra failure (runner non-zero) and a beyond-band miss
    both fail on the first attempt. Returns (passed, failures-of-last-run)."""
    failures: list[str] = ["never ran"]
    for attempt in (1, 2):
        if _run(cmd) != 0:
            print(f"{name}: runner exited non-zero — an infra failure fails the gate immediately")
            return False, ["runner failed"]
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        failures = judge(report)
        if not failures:
            return True, []
        if attempt == 1:
            if not within_noise_band(report, kind):
                print(f"{name}: {'; '.join(failures)} — beyond one case's weight: a regression, "
                      f"no re-run")
                return False, failures
            print(f"{name}: {'; '.join(failures)} — within one case's weight; the noise rule "
                  f"grants ONE re-run")
    return False, failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=str(ROOT / "evals" / "corpus"),
                    help="knowledge-repo checkout to rebuild the index from "
                         "(defaults to the frozen reference corpus)")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--skip-adversarial", action="store_true",
                    help="golden bars only (the adversarial half already ran, e.g. in CI)")
    args = ap.parse_args()

    out = ROOT / "evals" / "out" / "gates"
    out.mkdir(parents=True, exist_ok=True)
    verdicts = {}

    if not args.skip_adversarial:
        code = _run([args.python, "-m", "pytest", "-q", "-k", ADVERSARIAL_K, "-p",
                     "no:cacheprovider", "--no-cov"])
        verdicts["adversarial"] = (code == 0, [] if code == 0 else ["suite failed"])

    retrieval_report = out / "retrieval.json"
    verdicts["retrieval"] = _instrument(
        "retrieval", [args.python, "evals/run_retrieval.py",
                      "--golden", "evals/retrieval_golden.json", "--embedder", "openrouter",
                      "--rebuild", "--repo", args.repo, "--report", retrieval_report],
        retrieval_report, judge_retrieval, "retrieval")

    qa_report = out / "qa.json"
    verdicts["qa"] = _instrument(
        "qa", [args.python, "evals/run_qa.py",
               "--golden", "evals/qa_golden.json", "--embedder", "openrouter",
               "--llm", "openrouter", "--rebuild", "--repo", args.repo, "--report", qa_report],
        qa_report, judge_qa, "qa")

    print("\n# release gates")
    for name, (passed, failures) in verdicts.items():
        print(f"  {name:<12} {'PASS' if passed else 'FAIL'}"
              + (f"  ({'; '.join(failures)})" if failures else ""))
    return 0 if all(passed for passed, _ in verdicts.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
