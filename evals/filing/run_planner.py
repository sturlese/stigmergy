#!/usr/bin/env python3
"""Run one source through the production-equivalent filing evaluation path.

The default runs one coherent planner request, the real temporary-worktree writer gates, and at most
one bounded replacement-plan request after a rejection, without opening a database or writing Git.
``planner-only`` is a diagnostic mode that skips the bounded correction.
Every result is a single immutable, case-level evidence record suitable for
embedding in the parity artifact. The request can cost money.

Example:
  python evals/filing/run_planner.py \
    --live \
    --source /path/to/harness-engineering.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

try:
    from constants import (
        PLANNER_ONLY_MODE,
        PRODUCTION_EQUIVALENT_MODE,
        PRODUCTION_MAX_TURNS,
        PRODUCTION_REASONING_LEVEL,
    )
    from planner_eval import load_case, score
    from worktree import (
        apply_and_gate,
        apply_with_production_repair,
        effective_plan,
        initial_graph_manifest,
        prepared,
    )
except ModuleNotFoundError:
    from evals.filing.constants import (
        PLANNER_ONLY_MODE,
        PRODUCTION_EQUIVALENT_MODE,
        PRODUCTION_MAX_TURNS,
        PRODUCTION_REASONING_LEVEL,
    )
    from evals.filing.planner_eval import load_case, score
    from evals.filing.worktree import (
        apply_and_gate,
        apply_with_production_repair,
        effective_plan,
        initial_graph_manifest,
        prepared,
    )

from stigmergy.kernel.llm import (  # noqa: E402
    LIBRARIAN_MAX_TOKENS,
    LIBRARIAN_MODEL,
    LIBRARIAN_PROVIDER_ROUTING,
    LIBRARIAN_TEMPERATURE,
)
from stigmergy.knowledge.contract import (  # noqa: E402
    KnowledgeContractError,
    librarian_skill_provenance,
    validate_librarian_skill,
)
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
        "--brain-root",
        required=True,
        help="verified Git checkout whose librarian prompt executed the evaluation",
    )
    parser.add_argument(
        "--worktree",
        default=str(DEFAULT_WORKTREE),
        help="fixed versioned initial-graph template for the temporary evaluation worktree",
    )
    parser.add_argument("--timeout-s", type=int, default=300)
    parser.add_argument("--max-turns", type=int, default=PRODUCTION_MAX_TURNS)
    parser.add_argument(
        "--execution-mode",
        choices=(PRODUCTION_EQUIVALENT_MODE, PLANNER_ONLY_MODE),
        default=PRODUCTION_EQUIVALENT_MODE,
        help="production-equivalent exercises writer gates and bounded correction; planner-only is diagnostic",
    )
    parser.add_argument(
        "--include-payload",
        action="store_true",
        help="emit derived plan bodies for a local release artifact; stdout is otherwise safe telemetry",
    )
    args = parser.parse_args(argv)

    if not args.live:
        parser.error("--live is required: this sends source text to configured OpenRouter and can cost money")
    if args.execution_mode == PRODUCTION_EQUIVALENT_MODE and args.max_turns != PRODUCTION_MAX_TURNS:
        parser.error(
            f"{PRODUCTION_EQUIVALENT_MODE} requires --max-turns {PRODUCTION_MAX_TURNS}; "
            f"use {PLANNER_ONLY_MODE} for diagnostics"
        )
    try:
        worktree_manifest = initial_graph_manifest(args.worktree)
    except ValueError as error:
        parser.error(str(error))
    if args.execution_mode == PRODUCTION_EQUIVALENT_MODE and worktree_manifest != initial_graph_manifest(
        DEFAULT_WORKTREE
    ):
        parser.error("production-equivalent evidence requires the canonical evals/filing/repo template")

    source_path = Path(args.source)
    if not source_path.is_file():
        parser.error(f"source is not a file: {source_path}")
    case_path = Path(args.case)
    case = load_case(case_path)
    fixture = case_path.parent / str(case.get("fixture_path") or "")
    if not fixture.is_file():
        parser.error("case fixture_path must name a versioned readable fixture")
    source_bytes = source_path.read_bytes()
    fixture_bytes = fixture.read_bytes()
    if hashlib.sha256(source_bytes).digest() != hashlib.sha256(fixture_bytes).digest():
        parser.error("source bytes do not match the versioned case fixture")
    try:
        validate_librarian_skill(args.worktree)
        brain_prompt = librarian_skill_provenance(args.brain_root)
    except KnowledgeContractError as error:
        parser.error(str(error))
    if args.include_payload:
        print(
            "run-planner: --include-payload emits derived page bodies; keep this diagnostic output local",
            file=sys.stderr,
        )
    source_text = source_bytes.decode("utf-8")
    case_sha256 = hashlib.sha256(case_path.read_bytes()).hexdigest()
    fixture_sha256 = hashlib.sha256(fixture_bytes).hexdigest()
    reasoning_level = PRODUCTION_REASONING_LEVEL
    started_ns = time.monotonic_ns()
    with prepared(case, source_text, template=str(args.worktree)) as evaluation:
        settings = Settings(
            repo=evaluation.root,
            model=LIBRARIAN_MODEL,
            timeout_s=args.timeout_s,
            max_turns=args.max_turns,
        )
        planner = PydanticPlanner(settings)
        run = planner.plan(
            worktree=evaluation.root,
            envelope=evaluation.envelope,
            source_path=evaluation.source_path,
            source_text=evaluation.source_text,
            context=evaluation.context,
        )
        if args.execution_mode == PRODUCTION_EQUIVALENT_MODE:
            gates, plan_for_score = apply_with_production_repair(
                evaluation,
                run.plan,
                planner,
                planning_model_requests=run.model_requests,
                max_turns=args.max_turns,
                return_plan=True,
            )
        else:
            gates = apply_and_gate(evaluation, run.plan)
            plan_for_score = run.plan
            gates["semantic_revision_required"] = False
            gates["semantic_revision_attempted"] = False
            gates["semantic_revision_applied"] = False
            gates["semantic_revision_model_requests"] = 0
        scored_plan = effective_plan(evaluation, plan_for_score) if gates["passed"] else plan_for_score
        semantic = score(scored_plan, case, source_text=source_text)
        planning_model_requests = int(run.model_requests)
        semantic_revision_model_requests = int(gates["semantic_revision_model_requests"])
        model_requests = planning_model_requests + semantic_revision_model_requests
        schema_retry_count = int(run.schema_retry_count) + max(
            0, semantic_revision_model_requests - int(gates["semantic_revision_attempted"])
        )
        raw_gates = {gate: semantic[gate]["passed"] for gate in sorted(semantic) if gate != "passed"}
        raw_gates["writer"] = gates["passed"]
        correction_plan = plan_for_score.model_dump(mode="json") if gates["semantic_revision_applied"] else None
        writer_gates = {
            "passed": gates["passed"],
            "violations": gates["violations"],
            "changed_paths": gates["changed_paths"],
            "plan_rejection": gates["plan_rejection"],
        }
        output_payload = {
            "input": {
                "brain_prompt": brain_prompt,
                "case_sha256": case_sha256,
                "fixture_sha256": fixture_sha256,
                "source_sha256": fixture_sha256,
                "initial_worktree_manifest_sha256": evaluation.initial_worktree_manifest_sha256,
            },
            "plans": {
                "initial": run.plan.model_dump(mode="json"),
                "correction": correction_plan,
                "effective": scored_plan.model_dump(mode="json"),
            },
            "result": {
                "raw_gates": raw_gates,
                "score": semantic,
                "writer_gates": writer_gates,
            },
        }
        output_sha256 = hashlib.sha256(
            json.dumps(output_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        elapsed_ms = (time.monotonic_ns() - started_ns) // 1_000_000
        runtime = {
            "model": LIBRARIAN_MODEL.removeprefix("openrouter:"),
            "reasoning_level": reasoning_level,
            "provider": (
                LIBRARIAN_PROVIDER_ROUTING["only"][0]
                if LIBRARIAN_PROVIDER_ROUTING.get("only")
                else f"openrouter:{LIBRARIAN_PROVIDER_ROUTING.get('sort', 'default')}"
            ),
            "max_tokens": LIBRARIAN_MAX_TOKENS,
            "temperature": LIBRARIAN_TEMPERATURE,
        }
        case_result = {
            "case_id": Path(args.case).stem,
            "runtime": runtime,
            "execution": {
                "mode": args.execution_mode,
                "configured_max_turns": args.max_turns,
            },
            "input": output_payload["input"],
            "requests": {
                "initial": planning_model_requests,
                "correction": semantic_revision_model_requests,
                "total": model_requests,
                "schema_retries": schema_retry_count,
                "elapsed_ms": elapsed_ms,
                "usage": {"requests": model_requests},
            },
            "correction": {
                "required": gates["semantic_revision_required"],
                "attempted": gates["semantic_revision_attempted"],
                "applied": gates["semantic_revision_applied"],
                "plan": correction_plan,
            },
            "evidence": output_payload,
            "output": {
                "sha256": output_sha256,
                "artifact_ref": f"sha256:{output_sha256}",
            },
        }
        result = {
            **case_result,
            "case": case["name"],
            "passed": bool(semantic["passed"] and gates["passed"]),
        }
        if args.include_payload:
            case_result["evidence"] = output_payload
        else:
            case_result.pop("evidence")
            result.pop("evidence")
        result["case_result"] = case_result
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
