"""Calibrate capture routing against the real classifier (spec audience-layers §3.6.4).

Each case in `routing_golden.json` is an invented capture and the layers its sender may publish
to. The bar: an unambiguous case lands in its expected layer, and a deliberately ambiguous one in
the sender's default. A classifier failure never counts as a pass. The run is paid, needs `--live`
and `--max-usd`, and stops once the OpenRouter usage counter has moved by the cap.

    python evals/run_routing.py --live --max-usd 0.05 --report evals/out/routing.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import spend  # noqa: E402

from stigmergy.capture import schema  # noqa: E402
from stigmergy.kernel import layers  # noqa: E402
from stigmergy.server import routing  # noqa: E402
from stigmergy.server.identity import Principal  # noqa: E402

GOLDEN = ROOT / "evals" / "routing_golden.json"
DEFAULT_REPORT = ROOT / "evals" / "out" / "routing.json"


def _sender(case: dict) -> Principal:
    return Principal(subject="calibration@example.com", display_name="Calibration",
                     groups=tuple(case["candidates"]), default_audience=(layers.PUBLIC,))


def score(case: dict, routed: routing.Routed) -> dict:
    probabilities = routed.decision.probabilities
    layer_odds = {k: v for k, v in (probabilities or {}).items() if k != routing.NO_SINGLE_LAYER}
    if probabilities is None:
        outcome = "classifier_failed"
    elif case["expect"] == "default":
        outcome = "pass" if routed.decision.decision == schema.DEFAULT else "fail"
    else:
        landed = layers.layer_of_acl(routed.acl)
        outcome = "pass" if routed.decision.decision == schema.CLASSIFIED and landed == case["expect"] else "fail"
    return {"id": case["id"], "expect": case["expect"], "candidates": case["candidates"],
            "layer": layers.layer_of_acl(routed.acl), "decision": routed.decision.decision,
            "probabilities": probabilities, "top": max(layer_odds.values()) if layer_odds else None,
            "outcome": outcome}


def run(golden: dict, *, spent=lambda: 0.0, max_usd: float = float("inf")) -> dict:
    results = []
    for case in golden["cases"]:
        if spent() >= max_usd:
            break
        routed = routing.route(
            _sender(case),
            None,
            descriptions=lambda: golden["layers"],
            state=lambda case=case: routing.capture_state(
                title=case.get("title"), files=case.get("files", ()), text=case.get("text", "")),
        )
        results.append(score(case, routed))
    clear = [r for r in results if r["expect"] != "default"]
    ambiguous = [r for r in results if r["expect"] == "default"]
    complete = len(results) == len(golden["cases"])
    return {
        "threshold": routing.THRESHOLD,
        "cases": len(results),
        "complete": complete,
        "unambiguous_passed": sum(r["outcome"] == "pass" for r in clear),
        "unambiguous": len(clear),
        "ambiguous_passed": sum(r["outcome"] == "pass" for r in ambiguous),
        "ambiguous": len(ambiguous),
        "classifier_failures": sum(r["outcome"] == "classifier_failed" for r in results),
        "lowest_top_unambiguous": min((r["top"] for r in clear if r["top"] is not None), default=None),
        "highest_top_ambiguous": max((r["top"] for r in ambiguous if r["top"] is not None), default=None),
        "bar_passed": complete and all(r["outcome"] == "pass" for r in results),
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
    summary = {key: value for key, value in report.items() if key != "results"}
    print(json.dumps(summary, indent=2))
    for result in report["results"]:
        if result["outcome"] != "pass":
            print(f"{result['outcome']}: {result['id']} expected {result['expect']}, "
                  f"got {result['layer']} ({result['decision']}) {result['probabilities']}")
    return 0 if report["bar_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
