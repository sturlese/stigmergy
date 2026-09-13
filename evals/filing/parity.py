#!/usr/bin/env python3
"""Fail closed on per-case, repeat-aware real-model parity evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from planner_eval import load_case, score
    from worktree import apply_and_gate, prepared
except ModuleNotFoundError:
    from evals.filing.planner_eval import load_case, score
    from evals.filing.worktree import apply_and_gate, prepared

from stigmergy.knowledge.contract import KnowledgeContractError, librarian_skill_provenance
from stigmergy.knowledge.plan import FilingPlan

try:
    from constants import (
        PRODUCTION_EQUIVALENT_MODE,
        PRODUCTION_MAX_TURNS,
        REASONING_LEVELS,
        SELECTED_LEVEL_MIN_REPEATS,
    )
except ModuleNotFoundError:
    from evals.filing.constants import (
        PRODUCTION_EQUIVALENT_MODE,
        PRODUCTION_MAX_TURNS,
        REASONING_LEVELS,
        SELECTED_LEVEL_MIN_REPEATS,
    )

REQUIRED_IMPLEMENTATIONS = frozenset({"hippocampus", "stigmergy"})
REQUIRED_RUNTIME_FIELDS = frozenset({"model", "reasoning_level", "provider"})
REQUIRED_SEMANTIC_GATES = frozenset(
    {
        "mutations",
        "identity_proposals",
        "external_ids",
        "entity_links",
        "link_coverage",
        "reference_resolution",
        "alias_evidence",
        "bodies",
        "connections",
        "entity_relationships",
        "entity_wikilinks",
        "anti_fragmentation",
        "writer",
    }
)
STIGMERGY_RUNTIME = {"model": "openai/gpt-5.4", "provider": "azure"}
ARTIFACT_SCHEMA_VERSION = 3
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_REF = re.compile(r"^[0-9a-f]{40}$")
ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ReleaseInputs:
    """Candidate-owned inputs that an artifact must not be able to self-attest."""

    commit: str
    librarian_skill_sha256: str
    source_cases: dict[str, str]
    source_fixtures: dict[str, str]
    case_paths: dict[str, Path]
    fixture_paths: dict[str, Path]
    repo_root: Path
    brain_prompt: dict[str, str] | None = None


class ReleaseInputError(ValueError):
    pass


def current_release_inputs(
    repo_root: Path,
    *,
    brain_root: Path | None = None,
    brain_commit: str | None = None,
) -> ReleaseInputs:
    """Hash the exact candidate checked out for image publication."""
    root = repo_root.resolve()
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReleaseInputError("candidate commit could not be resolved") from error
    if not _GIT_REF.fullmatch(commit):
        raise ReleaseInputError("candidate commit is invalid")
    skill_path = root / "src" / "stigmergy" / "knowledge" / "librarian_skill.md"
    case_dir = root / "evals" / "filing" / "cases"
    if not skill_path.is_file() or not case_dir.is_dir():
        raise ReleaseInputError("candidate parity inputs are missing")
    case_paths = {path.stem: path for path in sorted(case_dir.glob("*.json"))}
    cases = {case_id: hashlib.sha256(path.read_bytes()).hexdigest() for case_id, path in case_paths.items()}
    if not cases:
        raise ReleaseInputError("candidate source cases are missing")
    fixture_paths = {}
    fixtures = {}
    for case_id, path in case_paths.items():
        try:
            fixture_path = path.parent / str(json.loads(path.read_text(encoding="utf-8"))["fixture_path"])
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ReleaseInputError(f"candidate case fixture is invalid: {case_id}") from error
        if not fixture_path.is_file():
            raise ReleaseInputError(f"candidate case fixture is missing: {case_id}")
        fixture_paths[case_id] = fixture_path
        fixtures[case_id] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    brain_prompt = None
    if brain_root is not None:
        try:
            brain_prompt = librarian_skill_provenance(brain_root, expected_commit=brain_commit)
        except KnowledgeContractError as error:
            raise ReleaseInputError(str(error)) from error
    return ReleaseInputs(
        commit=commit,
        librarian_skill_sha256=hashlib.sha256(skill_path.read_bytes()).hexdigest(),
        source_cases=cases,
        source_fixtures=fixtures,
        case_paths=case_paths,
        fixture_paths=fixture_paths,
        repo_root=root,
        brain_prompt=brain_prompt,
    )


def evaluate(artifact: dict, *, expected: ReleaseInputs | None = None) -> dict:
    """Validate per-case provenance, observability, and reasoning-level selection."""
    expected = expected or current_release_inputs(ROOT)
    failures: list[dict] = []
    if artifact.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        _failure(failures, "artifact", "schema-version")
    corpus = _sha(artifact.get("corpus_sha256"), "corpus_sha256", failures)
    initial = _ref(artifact.get("initial_graph_ref"), "initial_graph_ref", failures)
    commit = _ref(artifact.get("stigmergy_commit"), "stigmergy_commit", failures)
    skill = _sha(artifact.get("librarian_skill_sha256"), "librarian_skill_sha256", failures)
    case_hashes = _cases(artifact.get("source_cases"), failures)
    fixture_hashes = _cases(artifact.get("source_fixtures"), failures, field="source-fixtures")
    brain_prompt = _brain_prompt(artifact.get("brain_prompt"), failures)
    if commit != expected.commit:
        _failure(failures, "artifact", "candidate-commit")
    if skill != expected.librarian_skill_sha256:
        _failure(failures, "artifact", "candidate-skill")
    if case_hashes != expected.source_cases:
        _failure(failures, "artifact", "candidate-cases")
    if fixture_hashes != expected.source_fixtures:
        _failure(failures, "artifact", "candidate-fixtures")
    if expected.brain_prompt is None or brain_prompt != expected.brain_prompt:
        _failure(failures, "artifact", "candidate-brain-prompt")

    runs = _implementation_runs(artifact.get("runs"), corpus, initial, expected, failures)
    missing = sorted(REQUIRED_IMPLEMENTATIONS - set(runs))
    for implementation in missing:
        _failure(failures, implementation, "missing-implementation")
    selected_runs = runs.get("stigmergy", ())
    selected_level = _selected_level(selected_runs, failures)
    _blind_review(
        artifact.get("blind_editorial_review"), corpus, initial, case_hashes, runs, failures
    )
    _reasoning_matrix(
        artifact.get("reasoning_matrix"),
        selected_level,
        selected_runs,
        corpus,
        initial,
        expected,
        failures,
    )
    return {
        "passed": not failures,
        "missing_implementations": missing,
        "failures": failures,
        "corpus_sha256": corpus,
        "initial_graph_ref": initial,
    }


def _failure(failures: list[dict], implementation: str, reason: str, **details) -> None:
    failures.append({"implementation": implementation, "reason": reason, **details})


def _sha(value, field: str, failures: list[dict]) -> str | None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value) or len(set(value)) == 1:
        _failure(failures, "artifact", field)
        return None
    return value


def _ref(value, field: str, failures: list[dict]) -> str | None:
    if not isinstance(value, str) or not _GIT_REF.fullmatch(value) or len(set(value)) == 1:
        _failure(failures, "artifact", field)
        return None
    return value


def _cases(value, failures: list[dict], *, field: str = "source-cases") -> dict[str, str]:
    if not isinstance(value, list) or not value:
        _failure(failures, "artifact", field)
        return {}
    result = {}
    for item in value:
        if not isinstance(item, dict) or not _recorded_text(item.get("id")):
            _failure(failures, "artifact", "source-case-id")
            continue
        digest = _sha(item.get("sha256"), "source-case-sha256", failures)
        if item["id"] in result:
            _failure(failures, "artifact", "duplicate-source-case")
        elif digest:
            result[item["id"]] = digest
    return result


def _brain_prompt(value, failures: list[dict]) -> dict[str, str] | None:
    if not isinstance(value, dict):
        _failure(failures, "artifact", "brain-prompt")
        return None
    commit = _ref(value.get("commit"), "brain_prompt.commit", failures)
    digest = _sha(value.get("sha256"), "brain_prompt.sha256", failures)
    if commit is None or digest is None:
        return None
    return {"commit": commit, "sha256": digest}


def _implementation_runs(value, corpus, initial, expected, failures: list[dict]) -> dict[str, list[dict]]:
    if not isinstance(value, list) or not value:
        _failure(failures, "artifact", "runs")
        return {}
    result: dict[str, list[dict]] = {}
    seen_run_ids = set()
    for item in value:
        if not isinstance(item, dict) or item.get("implementation") not in REQUIRED_IMPLEMENTATIONS:
            _failure(failures, "artifact", "run-shape")
            continue
        implementation = item["implementation"]
        run_id = _validate_run(
            item,
            implementation=implementation,
            corpus=corpus,
            initial=initial,
            expected=expected,
            # Hippocampus is comparative evidence: preserve complete replay validation without
            # applying Stigmergy's admission threshold to a different filing workflow.
            require_passing=implementation == "stigmergy",
            require_production_equivalent=implementation == "stigmergy",
            failures=failures,
        )
        if run_id:
            if run_id in seen_run_ids:
                _failure(failures, implementation, "duplicate-run-id", run_id=run_id)
            seen_run_ids.add(run_id)
        result.setdefault(implementation, []).append(item)
    return result


def _validate_run(
    item: dict,
    *,
    implementation: str,
    corpus: str | None,
    initial: str | None,
    expected: ReleaseInputs,
    require_passing: bool,
    require_production_equivalent: bool,
    failures: list[dict],
) -> str | None:
    if any(field in item for field in ("score", "gates", "raw_gates")):
        _failure(failures, implementation, "aggregate-only-evidence")
    run_id = item.get("run_id")
    if not _recorded_text(run_id):
        _failure(failures, implementation, "run-id")
        run_id = None
    runtime = item.get("runtime")
    if not isinstance(runtime, dict) or not set(runtime) >= REQUIRED_RUNTIME_FIELDS or not all(
        _recorded_text(runtime.get(field)) for field in REQUIRED_RUNTIME_FIELDS
    ):
        _failure(failures, implementation, "runtime-metadata")
    elif implementation == "stigmergy" and (
        runtime.get("model") != STIGMERGY_RUNTIME["model"]
        or runtime.get("provider") != STIGMERGY_RUNTIME["provider"]
        or runtime.get("reasoning_level") not in REASONING_LEVELS
    ):
        _failure(failures, implementation, "runtime-route")
    execution = item.get("execution")
    if not isinstance(execution, dict) or not _recorded_text(execution.get("mode")) or not _nonnegative_int(
        execution.get("configured_max_turns")
    ):
        _failure(failures, implementation, "execution-metadata")
    elif require_production_equivalent and (
        execution["mode"] != PRODUCTION_EQUIVALENT_MODE
        or execution["configured_max_turns"] != PRODUCTION_MAX_TURNS
    ):
        _failure(failures, implementation, "production-equivalence")
    _run_provenance(implementation, item, corpus, initial, expected, failures)
    _case_results(
        item.get("case_results"),
        implementation=implementation,
        expected=expected,
        runtime=runtime,
        execution=execution,
        require_passing=require_passing,
        failures=failures,
    )
    return run_id


def _run_provenance(implementation, item, corpus, initial, expected, failures) -> None:
    provenance = item.get("provenance")
    if not isinstance(provenance, dict):
        _failure(failures, implementation, "run-provenance")
        return
    if provenance.get("corpus_sha256") != corpus or provenance.get("initial_graph_ref") != initial:
        _failure(failures, implementation, "stale-or-mismatched-corpus")
    if provenance.get("source_cases") != expected.source_cases:
        _failure(failures, implementation, "stale-or-mismatched-cases")
    if provenance.get("source_fixtures") != expected.source_fixtures:
        _failure(failures, implementation, "stale-or-mismatched-fixtures")
    if implementation == "stigmergy":
        if provenance.get("commit") != expected.commit:
            _failure(failures, implementation, "stale-or-mismatched-code")
        if provenance.get("librarian_skill_sha256") != expected.librarian_skill_sha256:
            _failure(failures, implementation, "stale-or-mismatched-skill")
        if provenance.get("brain_prompt") != expected.brain_prompt:
            _failure(failures, implementation, "stale-or-mismatched-brain-prompt")


def _case_results(
    value,
    *,
    implementation,
    expected: ReleaseInputs,
    runtime,
    execution,
    require_passing,
    failures,
) -> dict[str, dict]:
    if not isinstance(value, list) or not value:
        _failure(failures, implementation, "aggregate-only-evidence")
        return {}
    found = {}
    for item in value:
        if not isinstance(item, dict) or not _recorded_text(item.get("case_id")):
            _failure(failures, implementation, "case-result-shape")
            continue
        case_id = item["case_id"]
        if case_id in found:
            _failure(failures, implementation, "duplicate-case-result", case_id=case_id)
            continue
        found[case_id] = item
        if case_id not in expected.source_cases:
            _failure(failures, implementation, "unknown-case-result", case_id=case_id)
        _case_observability(item, implementation, case_id, runtime, execution, expected, failures)
        if require_passing and not _case_passed(item):
            _failure(failures, implementation, "case-failed", case_id=case_id)
    for case_id in sorted(set(expected.source_cases) - set(found)):
        _failure(failures, implementation, "missing-case-result", case_id=case_id)
    return found


def _case_observability(
    item: dict,
    implementation: str,
    case_id: str,
    runtime: dict | None,
    execution: dict | None,
    expected: ReleaseInputs,
    failures: list[dict],
) -> None:
    if item.get("runtime") != runtime:
        _failure(failures, implementation, "case-runtime", case_id=case_id)
    if (
        item.get("execution_mode") != (execution or {}).get("mode")
        or item.get("configured_max_turns") != (execution or {}).get("configured_max_turns")
    ):
        _failure(failures, implementation, "case-execution", case_id=case_id)
    if item.get("case_sha256") != expected.source_cases.get(case_id):
        _failure(failures, implementation, "case-input", case_id=case_id, field="case_sha256")
    if item.get("fixture_sha256") != expected.source_fixtures.get(case_id):
        _failure(failures, implementation, "case-input", case_id=case_id, field="fixture_sha256")
    if implementation == "stigmergy" and item.get("brain_prompt") != expected.brain_prompt:
        _failure(failures, implementation, "case-brain-prompt", case_id=case_id)
    for field in (
        "model_requests",
        "planning_model_requests",
        "repair_model_requests",
        "schema_retry_count",
        "semantic_repair_count",
        "elapsed_ms",
    ):
        if not _nonnegative_int(item.get(field)):
            _failure(failures, implementation, "case-observability", case_id=case_id, field=field)
    usage = item.get("usage")
    if usage is not None and (
        not isinstance(usage, dict) or any(not _nonnegative_int(value) for value in usage.values())
    ):
        _failure(failures, implementation, "case-usage", case_id=case_id)
    output = item.get("output")
    digest = output.get("sha256") if isinstance(output, dict) else None
    if (
        not isinstance(output, dict)
        or not _SHA256.fullmatch(digest or "")
        or output.get("artifact_ref") != f"sha256:{digest}"
    ):
        _failure(failures, implementation, "case-output", case_id=case_id)
    model_requests = item.get("model_requests")
    if _nonnegative_int(model_requests):
        if (
            _nonnegative_int(item.get("planning_model_requests"))
            and _nonnegative_int(item.get("repair_model_requests"))
            and item["planning_model_requests"] + item["repair_model_requests"] != model_requests
        ):
            _failure(failures, implementation, "case-observability", case_id=case_id, field="model_requests")
        if _nonnegative_int(item.get("schema_retry_count")) and item["schema_retry_count"] > model_requests:
            _failure(failures, implementation, "case-observability", case_id=case_id, field="schema_retry_count")
        if (
            _nonnegative_int(item.get("semantic_repair_count"))
            and item["semantic_repair_count"] > item.get("repair_model_requests", 0)
        ):
            _failure(failures, implementation, "case-observability", case_id=case_id, field="semantic_repair_count")
    if item.get("execution_mode") == "planner-only" and item.get("semantic_repair_count") != 0:
        _failure(failures, implementation, "case-observability", case_id=case_id, field="semantic_repair_count")
    _verify_case_payload(item, implementation, case_id, expected, failures)


def _verify_case_payload(
    item: dict,
    implementation: str,
    case_id: str,
    expected: ReleaseInputs,
    failures: list[dict],
) -> None:
    """Recompute all source-free evidence that a case result claims to have observed."""
    if case_id not in expected.case_paths:
        return
    payload = item.get("payload")
    expected_payload = {
        "brain_prompt": item.get("brain_prompt"),
        "case_sha256": item.get("case_sha256"),
        "fixture_sha256": item.get("fixture_sha256"),
        "plan": None if not isinstance(payload, dict) else payload.get("plan"),
        "effective_plan": None if not isinstance(payload, dict) else payload.get("effective_plan"),
        "score": item.get("score"),
        "gates": item.get("gates"),
        "raw_gates": item.get("raw_gates"),
    }
    if payload != expected_payload:
        _failure(failures, implementation, "case-payload", case_id=case_id)
        return
    output = item.get("output") or {}
    digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
    if output.get("sha256") != digest:
        _failure(failures, implementation, "case-output-hash", case_id=case_id)
        return
    try:
        FilingPlan.model_validate(payload["plan"])
        effective = FilingPlan.model_validate(payload["effective_plan"])
        case = load_case(expected.case_paths[case_id])
        source_text = expected.fixture_paths[case_id].read_text(encoding="utf-8")
        semantic = score(effective, case, source_text=source_text)
        with prepared(
            case,
            source_text,
            template=str(expected.repo_root / "evals" / "filing" / "repo"),
        ) as worktree:
            writer = apply_and_gate(worktree, effective)
    except (OSError, ValueError, TypeError) as error:
        _failure(failures, implementation, "case-replay", case_id=case_id, error=error.__class__.__name__)
        return
    expected_raw_gates = {
        **{gate: semantic[gate]["passed"] for gate in sorted(semantic) if gate != "passed"},
        "writer": writer["passed"],
    }
    if item.get("score") != semantic:
        _failure(failures, implementation, "case-semantic-score", case_id=case_id)
    if (item.get("gates") or {}).get("passed") != writer["passed"]:
        _failure(failures, implementation, "case-writer-gate", case_id=case_id)
    if item.get("raw_gates") != expected_raw_gates:
        _failure(failures, implementation, "case-raw-gates", case_id=case_id)


def _canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _case_passed(item: dict) -> bool:
    return (
        bool((item.get("score") or {}).get("passed"))
        and bool((item.get("gates") or {}).get("passed"))
        and _passing_raw_gates(item.get("raw_gates"))
    )


def _selected_level(runs: list[dict], failures: list[dict]) -> str | None:
    if not runs:
        return None
    levels = {(item.get("runtime") or {}).get("reasoning_level") for item in runs}
    if len(levels) != 1 or not levels <= set(REASONING_LEVELS):
        _failure(failures, "stigmergy", "selected-reasoning-inconsistent")
        return None
    return levels.pop()


def _blind_review(value, corpus, initial, case_hashes, runs, failures: list[dict]) -> None:
    if not isinstance(value, dict) or value.get("verdict") != "no_material_stigmergy_regression":
        _failure(failures, "review", "blind-editorial-review")
        return
    provenance = value.get("provenance")
    if not isinstance(provenance, dict) or not all(
        _recorded_text(provenance.get(field)) for field in ("reviewer", "method", "artifact_ref")
    ):
        _failure(failures, "review", "blind-review-provenance")
        return
    expected_run_ids = {
        implementation: sorted(item.get("run_id") for item in values)
        for implementation, values in runs.items()
    }
    review_runs = provenance.get("runs")
    normalized_review_runs = (
        {key: sorted(value) for key, value in review_runs.items()}
        if isinstance(review_runs, dict) and all(isinstance(value, list) for value in review_runs.values())
        else None
    )
    if (
        provenance.get("corpus_sha256") != corpus
        or provenance.get("initial_graph_ref") != initial
        or provenance.get("source_cases") != case_hashes
        or normalized_review_runs != expected_run_ids
    ):
        _failure(failures, "review", "stale-or-mismatched-review")


def _reasoning_matrix(
    matrix,
    selected_level: str | None,
    selected_runs: list[dict],
    corpus: str | None,
    initial: str | None,
    expected: ReleaseInputs,
    failures: list[dict],
) -> None:
    if not isinstance(matrix, list) or not matrix:
        _failure(failures, "stigmergy", "reasoning-matrix")
        return
    values: dict[str, tuple[bool, list[dict]]] = {}
    matrix_run_ids = set()
    for item in matrix:
        if not isinstance(item, dict) or item.get("reasoning_level") not in REASONING_LEVELS:
            _failure(failures, "stigmergy", "reasoning-matrix")
            continue
        if any(field in item for field in ("score", "gates", "raw_gates")):
            _failure(failures, "stigmergy", "aggregate-only-evidence")
        level = item["reasoning_level"]
        if level in values:
            _failure(failures, "stigmergy", "duplicate-reasoning-level")
            continue
        runtime = item.get("runtime")
        if runtime != {**STIGMERGY_RUNTIME, "reasoning_level": level}:
            _failure(failures, "stigmergy", "reasoning-matrix-runtime")
        candidate_runs = item.get("runs")
        if not isinstance(candidate_runs, list) or not candidate_runs:
            _failure(failures, "stigmergy", "aggregate-only-evidence")
            candidate_runs = []
        normalized_runs = []
        for candidate in candidate_runs:
            if not isinstance(candidate, dict):
                _failure(failures, "stigmergy", "reasoning-matrix-run")
                continue
            normalized = {**candidate, "runtime": runtime}
            normalized_runs.append(normalized)
            run_id = _validate_run(
                normalized,
                implementation="stigmergy",
                corpus=corpus,
                initial=initial,
                expected=expected,
                require_passing=False,
                require_production_equivalent=True,
                failures=failures,
            )
            if run_id:
                if run_id in matrix_run_ids:
                    _failure(failures, "stigmergy", "duplicate-matrix-run-id", run_id=run_id)
                matrix_run_ids.add(run_id)
        observed = bool(normalized_runs) and all(_run_passed(candidate) for candidate in normalized_runs)
        if not isinstance(item.get("passed"), bool) or item["passed"] != observed:
            _failure(failures, "stigmergy", "reasoning-matrix-result")
        values[level] = (observed, normalized_runs)
    if selected_level not in values or not values[selected_level][0]:
        _failure(failures, "stigmergy", "selected-reasoning-not-passing")
        return
    selected_index = REASONING_LEVELS.index(selected_level)
    for level in REASONING_LEVELS[:selected_index]:
        if level not in values:
            _failure(failures, "stigmergy", "reasoning-matrix-incomplete")
        elif values[level][0]:
            _failure(failures, "stigmergy", "reasoning-not-lowest-passing")
    selected_matrix_runs = values[selected_level][1]
    if len(selected_matrix_runs) < SELECTED_LEVEL_MIN_REPEATS:
        _failure(failures, "stigmergy", "selected-reasoning-insufficient-repeats")
    selected_ids = {item.get("run_id") for item in selected_runs}
    matrix_ids = {item.get("run_id") for item in selected_matrix_runs}
    if selected_ids != matrix_ids or len(selected_ids) < SELECTED_LEVEL_MIN_REPEATS:
        _failure(failures, "stigmergy", "selected-run-mismatch")
        return
    by_id = {item.get("run_id"): item for item in selected_matrix_runs}
    if any(by_id.get(item.get("run_id")) != item for item in selected_runs):
        _failure(failures, "stigmergy", "selected-run-mismatch")


def _run_passed(item: dict) -> bool:
    return bool(item.get("case_results")) and all(
        _case_passed(case) for case in item["case_results"] if isinstance(case, dict)
    )


def _passing_raw_gates(value) -> bool:
    return _complete_raw_gates(value) and all(_gate_passed(result) for result in value.values())


def _complete_raw_gates(value) -> bool:
    return isinstance(value, dict) and set(value) == REQUIRED_SEMANTIC_GATES


def _gate_passed(value) -> bool:
    return value is True or value == "passed"


def _recorded_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "<" not in value and ">" not in value


def _nonnegative_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, help="JSON artifact produced by the real-model parity run")
    parser.add_argument(
        "--repo-root",
        default=str(ROOT),
        help="candidate repository root whose commit, skill, and cases must match the artifact",
    )
    parser.add_argument(
        "--brain-root",
        required=True,
        help="verified knowledge-repository checkout whose librarian prompt executed the evaluation",
    )
    parser.add_argument(
        "--brain-commit",
        required=True,
        help="verified HEAD commit for --brain-root",
    )
    args = parser.parse_args(argv)
    try:
        artifact = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
        result = evaluate(
            artifact,
            expected=current_release_inputs(
                Path(args.repo_root),
                brain_root=Path(args.brain_root),
                brain_commit=args.brain_commit,
            ),
        )
    except (OSError, ReleaseInputError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
