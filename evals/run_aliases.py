"""Calibrate the filing-time alias decision against the real classifier (spec
retrieval-and-curation §3.5, §7 Aliases).

Each case in `alias_golden.json` is an invented entity, a variant a page links it by, and the
sentence around the link. The bar: no non-alias is accepted at `aliases.THRESHOLD`; recall over
the true aliases is reported. A classifier failure never counts as a pass. The run is paid, needs
`--live` and `--max-usd`, and stops once the OpenRouter usage counter has moved by the cap.

    python evals/run_aliases.py --live --max-usd 0.05 --report evals/out/aliases.json
"""
from __future__ import annotations

import argparse
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import spend  # noqa: E402

from stigmergy.kernel import llm  # noqa: E402
from stigmergy.knowledge import aliases  # noqa: E402

GOLDEN = ROOT / "evals" / "alias_golden.json"
DEFAULT_REPORT = ROOT / "evals" / "out" / "aliases.json"
THRESHOLDS = (0.75, 0.8, 0.85, 0.9, 0.95)


def decide(case: dict) -> float | None:
    entity = case["entity"]
    page = types.SimpleNamespace(title=entity["title"], entity_type=entity["type"], body=entity["summary"],
                                 aliases=tuple(entity["aliases"]))
    try:
        return aliases.decide(aliases.Candidate("wiki/entities/x.md", case["candidate"], case["sentence"]), page)
    except llm.DecisionFailed:
        return None


def run(golden: dict, *, spent=lambda: 0.0, max_usd: float = float("inf")) -> dict:
    results = []
    for case in golden["cases"]:
        if spent() >= max_usd:
            break
        results.append({"id": case["id"], "expect": case["expect"], "probability": decide(case)})
    true = [r for r in results if r["expect"] == "alias"]
    other = [r for r in results if r["expect"] == "other"]

    def accepted(rows, threshold):
        return [r["id"] for r in rows if r["probability"] is not None and r["probability"] >= threshold]

    by_threshold = {str(t): {"non_aliases_accepted": accepted(other, t),
                             "recall": round(len(accepted(true, t)) / len(true), 4) if true else None}
                    for t in THRESHOLDS}
    chosen = by_threshold[str(aliases.THRESHOLD)]
    complete = len(results) == len(golden["cases"])
    failures = [r["id"] for r in results if r["probability"] is None]
    return {
        "threshold": aliases.THRESHOLD, "cases": len(results), "complete": complete,
        "classifier_failures": failures,
        "lowest_true_alias": min((r["probability"] for r in true if r["probability"] is not None), default=None),
        "highest_non_alias": max((r["probability"] for r in other if r["probability"] is not None), default=None),
        "by_threshold": by_threshold,
        "bar_passed": complete and not failures and not chosen["non_aliases_accepted"],
        "results": results,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="call the real classifier (paid)")
    parser.add_argument("--max-usd", type=float, required=True)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("this runner only measures the real classifier: pass --live")
    start = spend.provider_usage()
    if start is None:
        parser.error("the OpenRouter usage counter is unreadable, so the spend cap cannot be enforced")

    def spent() -> float:
        now = spend.provider_usage()
        return float("inf") if now is None else now - start

    report = run(json.loads(GOLDEN.read_text(encoding="utf-8")), spent=spent, max_usd=args.max_usd)
    report["provider_delta_usd"] = round(spent(), 6)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, indent=2))
    for result in report["results"]:
        print(f"  {result['expect']:5s} {result['probability']!s:8s} {result['id']}")
    return 0 if report["bar_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
