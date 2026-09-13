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
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

try:
    from planner_eval import load_case, score
except ModuleNotFoundError:
    from evals.filing.planner_eval import load_case, score

from stigmergy.capture import schema  # noqa: E402
from stigmergy.knowledge.planner import PydanticPlanner  # noqa: E402
from stigmergy.librarian.config import Settings  # noqa: E402

EMPTY_CONTEXT = json.dumps(
    {
        "candidates": [],
        "entities": [],
        "namespaces": [],
        "source_evidence": [],
        "truncated": False,
    },
    sort_keys=True,
)
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
    settings = Settings(
        repo=str(args.worktree),
        timeout_s=args.timeout_s,
        max_turns=args.max_turns,
    )
    run = PydanticPlanner(settings).plan(
        worktree=str(args.worktree),
        envelope=_envelope(source_text, title=case["source_title"]),
        source_path=case["source_path"],
        source_text=source_text,
        context=EMPTY_CONTEXT,
    )
    result = {
        "case": case["name"],
        "model_requests": run.model_requests,
        "plan": run.plan.model_dump(mode="json"),
        "score": score(run.plan, case, source_text=source_text),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["score"]["passed"] else 1


def _envelope(source_text: str, *, title: str) -> schema.CaptureEnvelope:
    raw = source_text.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return schema.CaptureEnvelope(
        idempotency_key="filing-eval",
        actor=schema.Actor(subject="filing-eval", display_name="Filing evaluation"),
        audience=None,
        origin=schema.Origin(
            adapter="mcp",
            captured_at=dt.datetime(2026, 9, 12, 14, 36, 31, tzinfo=dt.UTC),
            title=title,
        ),
        artifacts=(
            schema.ArtifactRef(
                blob_ref=schema.content_ref(digest),
                sha256=digest,
                bytes=len(raw),
                media_type=schema.MEDIA_MARKDOWN,
            ),
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
