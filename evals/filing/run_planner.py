#!/usr/bin/env python3
"""Run one source through PydanticPlanner and score its FilingPlan.

The runner is planner-only: it reads a source and the versioned case, sends that source to the
configured OpenRouter librarian model in one request by default, and prints the plan plus
semantic score. That request can cost money. It never opens a database or a Git worktree for
writing. Higher turn counts are an explicit diagnostic opt-in.

Example:
  python evals/filing/run_planner.py \
    --live \
    --source /path/to/harness-engineering.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

try:
    from planner_eval import load_case, score
    from worktree import apply_and_gate, prepared
except ModuleNotFoundError:
    from evals.filing.planner_eval import load_case, score
    from evals.filing.worktree import apply_and_gate, prepared

from stigmergy.knowledge.planner import PydanticPlanner  # noqa: E402
from stigmergy.librarian.config import Settings  # noqa: E402

DEFAULT_CASE = ROOT / "evals" / "filing" / "cases" / "harness_engineering.json"
DEFAULT_WORKTREE = ROOT / "evals" / "filing" / "repo"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="path to readable source text")
    parser.add_argument(
        "--live",
        action="store_true",
        help="acknowledge that source text is sent to configured OpenRouter and can cost money",
    )
    parser.add_argument("--case", default=str(DEFAULT_CASE), help="versioned semantic case")
    parser.add_argument(
        "--worktree",
        default=str(DEFAULT_WORKTREE),
        help="read-only worktree containing the librarian skill",
    )
    parser.add_argument("--timeout-s", type=int, default=300)
    parser.add_argument("--max-turns", type=int, default=1)
    args = parser.parse_args(argv)

    if not args.live:
        parser.error(
            "--live is required: this sends source text to configured OpenRouter and can cost money"
        )

    source_path = Path(args.source)
    if not source_path.is_file():
        parser.error(f"source is not a file: {source_path}")
    case = load_case(args.case)
    source_text = source_path.read_text(encoding="utf-8")
    with prepared(case, source_text, template=str(args.worktree)) as evaluation:
        settings = Settings(
            repo=evaluation.root,
            timeout_s=args.timeout_s,
            max_turns=args.max_turns,
        )
        run = PydanticPlanner(settings).plan(
            worktree=evaluation.root,
            envelope=evaluation.envelope,
            source_path=evaluation.source_path,
            source_text=evaluation.source_text,
            context=evaluation.context,
        )
        semantic = score(run.plan, case, source_text=source_text)
        gates = apply_and_gate(evaluation, run.plan)
        semantic["passed"] = semantic["passed"] and gates["passed"]
        result = {
            "case": case["name"],
            "model_requests": run.model_requests,
            "plan": run.plan.model_dump(mode="json"),
            "score": semantic,
            "gates": gates,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["score"]["passed"] else 1
if __name__ == "__main__":
    sys.exit(main())
