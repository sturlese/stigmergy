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
    from worktree import apply_with_production_repair, effective_plan, prepared
except ModuleNotFoundError:
    from evals.filing.planner_eval import load_case, score
    from evals.filing.worktree import apply_with_production_repair, effective_plan, prepared

from stigmergy.kernel.llm import (
    LIBRARIAN_MAX_TOKENS,
    LIBRARIAN_REASONING_LEVEL,
    LIBRARIAN_TEMPERATURE,
)
from stigmergy.knowledge.contract import KnowledgeContractError, librarian_skill_provenance
from stigmergy.knowledge.plan import FilingPlan, GraphShape, GraphTopology, RepairPlan
from stigmergy.knowledge.planner import PlanRun
from stigmergy.knowledge.writer import GateRefused

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
        "editorial_quality",
        "seeded_update",
        "writer",
    }
)
STIGMERGY_RUNTIME = {
    "model": "deepseek/deepseek-v4.1-flash",
    "provider": "openrouter:throughput",
    "max_tokens": LIBRARIAN_MAX_TOKENS,
    "temperature": LIBRARIAN_TEMPERATURE,
}
ARTIFACT_SCHEMA_VERSION = 5
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_REF = re.compile(r"^[0-9a-f]{40}$")
_CONTENT_ADDRESS = re.compile(r"^sha256:([0-9a-f]{64})$")
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


def evaluate(
    artifact: dict,
    *,
    expected: ReleaseInputs | None = None,
    review_root: Path | None = None,
) -> dict:
    """Validate per-case provenance, observability, and reasoning-level selection."""
    expected = expected or current_release_inputs(ROOT)
    failures: list[dict] = []
    if artifact.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        _failure(
            failures,
            "artifact",
            (
                "schema-v4-obsolete"
                if artifact.get("schema_version") == 4
                else "schema-v3-obsolete"
                if artifact.get("schema_version") == 3
                else "schema-version"
            ),
        )
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
    _admission_state(artifact, failures)

    replayed_effective: dict[tuple[str, str, str], dict] = {}
    runs = _implementation_runs(
        artifact.get("runs"),
        corpus,
        initial,
        expected,
        failures,
        replayed_effective=replayed_effective,
    )
    missing = sorted(REQUIRED_IMPLEMENTATIONS - set(runs))
    for implementation in missing:
        _failure(failures, implementation, "missing-implementation")
    selected_runs = runs.get("stigmergy", ())
    selected_level = _selected_level(selected_runs, failures)
    _blind_review(
        artifact.get("blind_editorial_review"),
        corpus,
        initial,
        case_hashes,
        runs,
        review_root=review_root,
        candidate_root=expected.repo_root,
        packet_state=artifact.get("blind_review_packet"),
        replayed_effective=replayed_effective,
        failures=failures,
    )
    _reasoning_matrix(
        artifact.get("reasoning_matrix"),
        selected_level,
        selected_runs,
        corpus,
        initial,
        expected,
        failures,
        replayed_effective=replayed_effective,
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


def _admission_state(artifact: dict, failures: list[dict]) -> None:
    """Reject incomplete release packets before examining their self-reported result."""
    if artifact.get("admission_status") != "passed":
        _failure(failures, "artifact", "admission-status")
    if _has_pending_field(artifact):
        _failure(failures, "artifact", "pending-admission-field")
    packet = artifact.get("blind_review_packet")
    if not isinstance(packet, dict) or packet.get("status") != "completed":
        _failure(failures, "review", "blind-review-packet")


def _has_pending_field(value) -> bool:
    if isinstance(value, dict):
        return any(
            "pending" in str(key).lower() or _has_pending_field(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_pending_field(item) for item in value)
    return False


def _implementation_runs(
    value,
    corpus,
    initial,
    expected,
    failures: list[dict],
    *,
    replayed_effective: dict[tuple[str, str, str], dict],
) -> dict[str, list[dict]]:
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
            require_runtime_reasoning=implementation == "stigmergy",
            failures=failures,
            replayed_effective=replayed_effective,
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
    require_runtime_reasoning: bool,
    failures: list[dict],
    replayed_effective: dict[tuple[str, str, str], dict],
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
    elif implementation == "stigmergy":
        if (
            runtime.get("model") != STIGMERGY_RUNTIME["model"]
            or runtime.get("provider") != STIGMERGY_RUNTIME["provider"]
            or runtime.get("temperature") != LIBRARIAN_TEMPERATURE
            or runtime.get("reasoning_level") not in REASONING_LEVELS
        ):
            _failure(failures, implementation, "runtime-route")
        if runtime.get("max_tokens") != LIBRARIAN_MAX_TOKENS:
            _failure(failures, implementation, "runtime-output-ceiling")
        if require_runtime_reasoning and runtime.get("reasoning_level") != LIBRARIAN_REASONING_LEVEL:
            _failure(failures, implementation, "runtime-production-reasoning")
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
        run_id=run_id,
        replayed_effective=replayed_effective,
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
    run_id,
    replayed_effective,
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
        _case_observability(
            item,
            implementation,
            case_id,
            runtime,
            execution,
            expected,
            require_passing,
            failures,
            run_id=run_id,
            replayed_effective=replayed_effective,
        )
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
    require_passing: bool,
    failures: list[dict],
    run_id: str | None,
    replayed_effective: dict[tuple[str, str, str], dict],
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
        "semantic_revision_model_requests",
        "repair_model_requests",
        "graph_shape_model_requests",
        "graph_shape_review_model_requests",
        "graph_shape_enrichment_model_requests",
        "compilation_model_requests",
        "graph_semantic_review_model_requests",
        "schema_retry_count",
        "semantic_repair_count",
        "elapsed_ms",
    ):
        if not _nonnegative_int(item.get(field)):
            _failure(failures, implementation, "case-observability", case_id=case_id, field=field)
    for field in (
        "graph_semantic_reviewed",
        "semantic_revision_required",
        "semantic_revision_attempted",
        "semantic_revision_applied",
    ):
        if not isinstance(item.get(field), bool):
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
            and _nonnegative_int(item.get("semantic_revision_model_requests"))
            and _nonnegative_int(item.get("repair_model_requests"))
            and (
                item["planning_model_requests"]
                + item["semantic_revision_model_requests"]
                + item["repair_model_requests"]
                != model_requests
            )
        ):
            _failure(failures, implementation, "case-observability", case_id=case_id, field="model_requests")
        if implementation == "stigmergy":
            phase_fields = (
                "graph_shape_model_requests",
                "graph_shape_review_model_requests",
                "graph_shape_enrichment_model_requests",
                "compilation_model_requests",
                "graph_semantic_review_model_requests",
            )
            if any(item.get(field, 0) != 0 for field in phase_fields):
                _failure(
                    failures,
                    implementation,
                    "case-observability",
                    case_id=case_id,
                    field="graph_phase_requests",
                )
        if (
            _nonnegative_int((execution or {}).get("configured_max_turns"))
            and model_requests > execution["configured_max_turns"]
        ):
            _failure(failures, implementation, "case-observability", case_id=case_id, field="max_turns")
        if _nonnegative_int(item.get("schema_retry_count")) and item["schema_retry_count"] > model_requests:
            _failure(failures, implementation, "case-observability", case_id=case_id, field="schema_retry_count")
        if (
            _nonnegative_int(item.get("semantic_repair_count"))
            and item["semantic_repair_count"] > item.get("repair_model_requests", 0)
        ):
            _failure(failures, implementation, "case-observability", case_id=case_id, field="semantic_repair_count")
        if item.get("semantic_revision_applied") and not item.get("semantic_revision_required"):
            _failure(failures, implementation, "case-observability", case_id=case_id, field="semantic_revision_applied")
        if item.get("semantic_revision_applied") and item.get("semantic_revision_model_requests", 0) < 1:
            _failure(
                failures,
                implementation,
                "case-observability",
                case_id=case_id,
                field="semantic_revision_model_requests",
            )
        if item.get("semantic_revision_model_requests", 0) and not item.get("semantic_revision_attempted"):
            _failure(
                failures,
                implementation,
                "case-observability",
                case_id=case_id,
                field="semantic_revision_model_requests",
            )
    if item.get("execution_mode") == "planner-only" and item.get("semantic_repair_count") != 0:
        _failure(failures, implementation, "case-observability", case_id=case_id, field="semantic_repair_count")
    _verify_case_payload(
        item,
        implementation,
        case_id,
        expected,
        execution=execution,
        require_passing=require_passing,
        failures=failures,
        run_id=run_id,
        replayed_effective=replayed_effective,
    )


def _verify_case_payload(
    item: dict,
    implementation: str,
    case_id: str,
    expected: ReleaseInputs,
    *,
    execution: dict | None,
    require_passing: bool,
    failures: list[dict],
    run_id: str | None,
    replayed_effective: dict[tuple[str, str, str], dict],
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
        "graph_shape": None if not isinstance(payload, dict) else payload.get("graph_shape"),
        "graph_shape_draft": (
            None if not isinstance(payload, dict) else payload.get("graph_shape_draft")
        ),
        "graph_shape_review": (
            None if not isinstance(payload, dict) else payload.get("graph_shape_review")
        ),
        "graph_shape_violations": (
            None if not isinstance(payload, dict) else payload.get("graph_shape_violations")
        ),
        "reviewed_plan": None if not isinstance(payload, dict) else payload.get("reviewed_plan"),
        "semantic_revision": _semantic_revision_telemetry(item),
        "repair_plan": None if not isinstance(payload, dict) else payload.get("repair_plan"),
        "repair_plan_sha256": item.get("repair_plan_sha256"),
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
        draft = FilingPlan.model_validate(payload["plan"])
        reviewed = (
            FilingPlan.model_validate(payload["reviewed_plan"])
            if payload["reviewed_plan"] is not None
            else None
        )
        recorded_repair = (
            RepairPlan.model_validate(payload["repair_plan"])
            if payload["repair_plan"] is not None
            else None
        )
        recorded_effective = FilingPlan.model_validate(payload["effective_plan"])
        case = load_case(expected.case_paths[case_id])
        recorded_shape = (
            GraphShape.model_validate(payload["graph_shape"])
            if payload["graph_shape"] is not None
            else None
        )
        recorded_shape_draft = (
            GraphTopology.model_validate(payload["graph_shape_draft"])
            if payload["graph_shape_draft"] is not None
            else None
        )
        recorded_shape_review = (
            GraphTopology.model_validate(payload["graph_shape_review"])
            if payload["graph_shape_review"] is not None
            else None
        )
        recorded_shape_violations = payload["graph_shape_violations"]
        if not isinstance(recorded_shape_violations, list) or any(
            not isinstance(value, str) for value in recorded_shape_violations
        ):
            raise ValueError("graph shape violations must be a string list")
        if implementation == "stigmergy":
            recorded_graph_parts = (
                recorded_shape,
                recorded_shape_draft,
                recorded_shape_review,
            )
            if any(item is not None for item in recorded_graph_parts):
                _failure(failures, implementation, "case-obsolete-graph-pipeline", case_id=case_id)
            if recorded_shape_violations:
                _failure(failures, implementation, "case-obsolete-graph-pipeline", case_id=case_id)
        source_text = expected.fixture_paths[case_id].read_text(encoding="utf-8")
        with prepared(
            case,
            source_text,
            template=str(expected.repo_root / "evals" / "filing" / "repo"),
        ) as worktree:
            telemetry = _semantic_revision_telemetry(item)
            if not telemetry["required"] and telemetry != _empty_revision_telemetry():
                _failure(failures, implementation, "case-semantic-revision-telemetry", case_id=case_id)
            passing_production_case = (
                require_passing
                and (execution or {}).get("mode") == PRODUCTION_EQUIVALENT_MODE
                and _case_passed(item)
            )
            if telemetry["required"] and passing_production_case and not (
                telemetry["attempted"]
                and telemetry["applied"]
                and telemetry["model_requests"] >= 1
                and reviewed is not None
            ):
                _failure(failures, implementation, "case-semantic-revision-telemetry", case_id=case_id)
            if telemetry["applied"] and reviewed is None:
                _failure(failures, implementation, "case-semantic-revision-telemetry", case_id=case_id)
            if not telemetry["applied"] and reviewed is not None:
                _failure(failures, implementation, "case-semantic-revision-telemetry", case_id=case_id)
            replay_planner = _RecordedRevisionPlanner(
                reviewed,
                recorded_repair,
                model_requests=telemetry["model_requests"],
                repair_model_requests=item["repair_model_requests"],
            )
            writer, replayed_plan, replayed_repair = apply_with_production_repair(
                worktree,
                draft,
                replay_planner,
                planning_model_requests=item["planning_model_requests"],
                max_turns=item["configured_max_turns"],
                graph_shape=recorded_shape,
                semantic_reviewed=item["graph_semantic_reviewed"],
                return_plan=True,
                return_repair_plan=True,
            )
            replayed_effective_plan = (
                effective_plan(worktree, replayed_plan) if writer["passed"] else replayed_plan
            )
    except (OSError, ValueError, TypeError) as error:
        _failure(failures, implementation, "case-replay", case_id=case_id, error=error.__class__.__name__)
        return
    if _semantic_revision_telemetry(writer) != telemetry:
        _failure(failures, implementation, "case-semantic-revision-telemetry", case_id=case_id)
    if writer.get("repair_model_requests") != item.get("repair_model_requests") or writer.get(
        "semantic_repair_count"
    ) != item.get("semantic_repair_count"):
        _failure(failures, implementation, "case-repair-telemetry", case_id=case_id)
    _verify_repair_evidence(
        item,
        payload,
        recorded_repair,
        replayed_repair,
        telemetry=telemetry,
        implementation=implementation,
        case_id=case_id,
        failures=failures,
    )
    replayed_effective_payload = replayed_effective_plan.model_dump(mode="json")
    if _canonical_json(replayed_effective_payload) != _canonical_json(recorded_effective.model_dump(mode="json")):
        _failure(failures, implementation, "replay-effective-plan-mismatch", case_id=case_id)
        return
    if run_id is not None:
        replayed_effective[(implementation, run_id, case_id)] = replayed_effective_payload
    semantic = score(replayed_effective_plan, case, source_text=source_text)
    expected_raw_gates = {
        **{gate: semantic[gate]["passed"] for gate in sorted(semantic) if gate != "passed"},
        "writer": writer["passed"],
    }
    if _canonical_json(item.get("score")) != _canonical_json(semantic):
        _failure(failures, implementation, "case-semantic-score", case_id=case_id)
    if item.get("gates") not in (_recorded_writer_gates(writer), writer):
        _failure(failures, implementation, "case-writer-gate", case_id=case_id)
    if item.get("raw_gates") != expected_raw_gates:
        _failure(failures, implementation, "case-raw-gates", case_id=case_id)


class _RecordedRevisionPlanner:
    """Replay only the recorded complete replacement plan; never call a model."""

    def __init__(
        self,
        plan: FilingPlan | None,
        repair_plan: RepairPlan | None,
        *,
        model_requests: int,
        repair_model_requests: int,
    ):
        self.plan = plan
        self.repair_plan = repair_plan
        self.model_requests = model_requests
        self.repair_model_requests = repair_model_requests

    def revise(self, **_kwargs) -> PlanRun:
        if self.plan is None:
            raise GateRefused("recorded semantic revision is unavailable")
        return PlanRun(self.plan, model_requests=self.model_requests)

    def repair(self, **_kwargs) -> PlanRun:
        if self.repair_plan is None:
            raise GateRefused("recorded structural repair is unavailable")
        return PlanRun(self.repair_plan, model_requests=self.repair_model_requests)


def _semantic_revision_telemetry(item: dict) -> dict:
    return {
        "required": item.get("semantic_revision_required"),
        "attempted": item.get("semantic_revision_attempted"),
        "applied": item.get("semantic_revision_applied"),
        "model_requests": item.get("semantic_revision_model_requests"),
    }


def _recorded_writer_gates(gates: dict) -> dict:
    """Keep the public writer-gate record separate from internal replay telemetry."""
    return {
        "passed": gates.get("passed"),
        "violations": gates.get("violations"),
        "changed_paths": gates.get("changed_paths"),
    }


def _verify_repair_evidence(
    item: dict,
    payload: dict,
    recorded_repair: RepairPlan | None,
    replayed_repair: RepairPlan | None,
    *,
    telemetry: dict,
    implementation: str,
    case_id: str,
    failures: list[dict],
) -> None:
    """Require the one bounded structural repair to be canonical and replayable."""
    repair_requests = item.get("repair_model_requests")
    repair_count = item.get("semantic_repair_count")
    recorded_payload = payload.get("repair_plan")
    recorded_hash = payload.get("repair_plan_sha256")
    evidence_valid = "repair_plan_sha256" in item and item.get("repair_plan_sha256") == recorded_hash
    if recorded_repair is None:
        evidence_valid = (
            evidence_valid
            and recorded_payload is None
            and recorded_hash is None
            and repair_requests == 0
            and repair_count == 0
            and replayed_repair is None
        )
    else:
        canonical_repair = recorded_repair.model_dump(mode="json")
        expected_hash = hashlib.sha256(_canonical_json(canonical_repair)).hexdigest()
        evidence_valid = (
            evidence_valid
            and _canonical_json(recorded_payload) == _canonical_json(canonical_repair)
            and recorded_hash == expected_hash
            and repair_requests > 0
            and repair_count == 1
            and not telemetry["required"]
            and not telemetry["applied"]
            and replayed_repair is not None
            and _canonical_json(replayed_repair.model_dump(mode="json")) == _canonical_json(canonical_repair)
        )
    if not evidence_valid:
        _failure(failures, implementation, "case-repair-evidence", case_id=case_id)


def _empty_revision_telemetry() -> dict:
    return {"required": False, "attempted": False, "applied": False, "model_requests": 0}


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
    level = levels.pop()
    if level != LIBRARIAN_REASONING_LEVEL:
        _failure(failures, "stigmergy", "selected-production-reasoning")
    return level


def _blind_review(
    value,
    corpus,
    initial,
    case_hashes,
    runs,
    *,
    review_root: Path | None,
    candidate_root: Path,
    packet_state,
    replayed_effective: dict[tuple[str, str, str], dict],
    failures: list[dict],
) -> None:
    if not isinstance(value, dict) or value.get("verdict") != "no_material_stigmergy_regression":
        _failure(failures, "review", "blind-editorial-review")
        return
    provenance = value.get("provenance")
    reference = _content_address(provenance.get("artifact_ref") if isinstance(provenance, dict) else None)
    if not isinstance(provenance, dict) or not all(
        _recorded_text(provenance.get(field)) for field in ("reviewer", "method")
    ) or reference is None:
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
        return
    root = _external_review_root(review_root, candidate_root, failures)
    if root is None:
        return
    review = _load_content_addressed_json(
        root, prefix="blind-review-unblind-", digest=reference, suffix=".json", failures=failures
    )
    if review is None:
        return
    if review.get("verdict") != value["verdict"]:
        _failure(failures, "review", "blind-review-evidence")
        return
    response = _load_review_document(
        root,
        review.get("reviewer_response"),
        prefix="blind-reviewer-response-",
        suffix=".json",
        require_reference=True,
        failures=failures,
    )
    packet = _load_review_document(
        root,
        review.get("packet"),
        prefix="blind-review-packet-",
        suffix=".json",
        require_reference=False,
        failures=failures,
    )
    mapping = _load_review_document(
        root,
        review.get("mapping"),
        prefix="blind-review-mapping-",
        suffix=".secret.json",
        require_reference=False,
        failures=failures,
    )
    if response is None or packet is None or mapping is None:
        return
    response_raw, response_value = response
    packet_raw, packet_value = packet
    _mapping = mapping[1]
    if not _packet_state_matches(packet_state, packet_raw, packet_value):
        _failure(failures, "review", "blind-review-packet")
        return
    if (
        response_value.get("packet_sha256") != hashlib.sha256(packet_raw).hexdigest()
        or _mapping.get("packet_sha256") != hashlib.sha256(_canonical_json(packet_value)).hexdigest()
    ):
        _failure(failures, "review", "blind-review-evidence")
        return
    _verify_unblind_review(
        review,
        response_value,
        packet_value,
        _mapping,
        runs,
        replayed_effective,
        failures,
    )


def _content_address(value) -> str | None:
    match = _CONTENT_ADDRESS.fullmatch(value) if isinstance(value, str) else None
    return match.group(1) if match else None


def _external_review_root(
    review_root: Path | None, candidate_root: Path, failures: list[dict]
) -> Path | None:
    if review_root is None:
        _failure(failures, "review", "blind-review-evidence")
        return None
    root = review_root.resolve()
    if root.is_relative_to(candidate_root.resolve()):
        _failure(failures, "review", "review-evidence-root")
        return None
    return root


def _load_content_addressed_json(
    root: Path,
    *,
    prefix: str,
    digest: str,
    suffix: str,
    failures: list[dict],
) -> dict | None:
    path = root / f"{prefix}{digest}{suffix}"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _failure(failures, "review", "blind-review-evidence")
        return None
    if hashlib.sha256(_canonical_json(value)).hexdigest() != digest:
        _failure(failures, "review", "blind-review-evidence")
        return None
    return value


def _load_review_document(
    root: Path,
    metadata,
    *,
    prefix: str,
    suffix: str,
    require_reference: bool,
    failures: list[dict],
) -> tuple[bytes, dict] | None:
    if not isinstance(metadata, dict):
        _failure(failures, "review", "blind-review-evidence")
        return None
    canonical = metadata.get("canonical_sha256")
    raw_digest = metadata.get("raw_sha256")
    reference = _content_address(metadata.get("artifact_ref"))
    if (
        not isinstance(canonical, str)
        or not _SHA256.fullmatch(canonical)
        or not isinstance(raw_digest, str)
        or not _SHA256.fullmatch(raw_digest)
        or (require_reference and reference != canonical)
        or (not require_reference and reference is not None and reference != canonical)
        or metadata.get("path") != f"{prefix}{canonical}{suffix}"
    ):
        _failure(failures, "review", "blind-review-evidence")
        return None
    path = root / metadata["path"]
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        _failure(failures, "review", "blind-review-evidence")
        return None
    if hashlib.sha256(raw).hexdigest() != raw_digest or hashlib.sha256(_canonical_json(value)).hexdigest() != canonical:
        _failure(failures, "review", "blind-review-evidence")
        return None
    return raw, value


def _packet_state_matches(packet_state, raw: bytes, packet: dict) -> bool:
    return isinstance(packet_state, dict) and packet_state == {
        "status": "completed",
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "canonical_sha256": hashlib.sha256(_canonical_json(packet)).hexdigest(),
    }


def _verify_unblind_review(
    review,
    response,
    packet,
    mapping,
    runs,
    replayed_effective: dict[tuple[str, str, str], dict],
    failures: list[dict],
) -> None:
    packet_pairs = _indexed_pairs(packet.get("comparisons"), "comparison_id")
    mapping_pairs = _indexed_pairs(mapping.get("mapping"), "comparison_id")
    response_pairs = _indexed_pairs(response.get("pairs"), "pair_id")
    if not packet_pairs or set(packet_pairs) != set(mapping_pairs) or set(packet_pairs) != set(response_pairs):
        _failure(failures, "review", "blind-review-evidence")
        return
    known_outputs = {
        (item.get("implementation"), item.get("run_id"), case.get("output", {}).get("sha256")):
        _blind_case_binding(
            case,
            replayed_effective_plan=replayed_effective.get(
                (item.get("implementation"), item.get("run_id"), case.get("case_id"))
            ),
        )
        for values in runs.values()
        for item in values
        for case in item.get("case_results", [])
        if isinstance(case, dict)
    }
    expected_regressions = []
    reviewed_outputs = set()
    for comparison_id, packet_pair in packet_pairs.items():
        mapping_pair = mapping_pairs[comparison_id]
        response_pair = response_pairs[comparison_id]
        candidates = _indexed_pairs(packet_pair.get("candidates"), "label")
        labels = _indexed_pairs(mapping_pair.get("labels"), "label")
        if len(candidates) != 2 or set(candidates) != set(labels):
            _failure(failures, "review", "blind-review-evidence")
            return
        for label, mapped in labels.items():
            candidate = candidates[label]
            if (
                candidate.get("case_output_sha256") != mapped.get("case_output_sha256")
                or candidate.get("effective_pages_sha256") != mapped.get("effective_pages_sha256")
                or candidate.get("draft_plan_sha256") != mapped.get("draft_plan_sha256")
                or candidate.get("reviewed_plan_sha256") != mapped.get("reviewed_plan_sha256")
                or candidate.get("repair_plan_sha256") != mapped.get("repair_plan_sha256")
                or candidate.get("semantic_revision") != mapped.get("semantic_revision")
                or not _SHA256.fullmatch(mapped.get("case_output_sha256", ""))
                or not _SHA256.fullmatch(mapped.get("effective_pages_sha256", ""))
                or not _SHA256.fullmatch(mapped.get("draft_plan_sha256", ""))
                or not _nullable_sha256(mapped.get("repair_plan_sha256"))
            ):
                _failure(failures, "review", "blind-review-evidence")
                return
            output = (mapped.get("implementation"), mapped.get("run_id"), mapped.get("case_output_sha256"))
            binding = known_outputs.get(output)
            if binding is None or any(mapped.get(key) != value for key, value in binding.items()):
                _failure(failures, "review", "blind-review-evidence")
                return
            reviewed_outputs.add(output)
        if not _reviewer_labels_match(response_pair, set(labels)):
            _failure(failures, "review", "blind-review-evidence")
            return
        regression = response_pair.get("material_regression")
        if not isinstance(regression, dict):
            _failure(failures, "review", "blind-review-evidence")
            return
        if regression.get("side") == "none":
            if not _recorded_text(regression.get("reason")):
                _failure(failures, "review", "blind-review-evidence")
                return
            continue
        if regression.get("side") not in labels:
            _failure(failures, "review", "blind-review-evidence")
            return
        selected = labels[regression["side"]]
        expected_regressions.append(
            {
                "comparison_id": comparison_id,
                "candidate_label": selected["label"],
                "implementation": selected["implementation"],
                "run_id": selected["run_id"],
                "case_output_sha256": selected["case_output_sha256"],
                "effective_pages_sha256": selected["effective_pages_sha256"],
                "reason": regression.get("reason"),
            }
        )
    if reviewed_outputs != set(known_outputs):
        _failure(failures, "review", "blind-review-evidence")
        return
    unblinding = review.get("unblinding")
    stigmergy_regressions = [
        item for item in expected_regressions if item["implementation"] == "stigmergy"
    ]
    if (
        not isinstance(unblinding, dict)
        or unblinding.get("material_regressions") != expected_regressions
        or unblinding.get("stigmergy_material_regressions") != stigmergy_regressions
        or (review.get("verdict") == "no_material_stigmergy_regression" and stigmergy_regressions)
    ):
        _failure(failures, "review", "blind-review-evidence")


def _blind_case_binding(
    case: dict,
    *,
    replayed_effective_plan: dict | None,
) -> dict | None:
    payload = case.get("payload")
    if not isinstance(payload, dict):
        return None
    plan = payload.get("plan")
    effective = replayed_effective_plan
    revision = payload.get("semantic_revision")
    reviewed = payload.get("reviewed_plan")
    if not isinstance(plan, dict) or not isinstance(effective, dict) or not isinstance(revision, dict):
        return None
    if reviewed is not None and not isinstance(reviewed, dict):
        return None
    return {
        "case_output_sha256": (case.get("output") or {}).get("sha256"),
        "effective_pages_sha256": hashlib.sha256(_canonical_json(effective)).hexdigest(),
        "draft_plan_sha256": hashlib.sha256(_canonical_json(plan)).hexdigest(),
        "reviewed_plan_sha256": (
            hashlib.sha256(_canonical_json(reviewed)).hexdigest() if reviewed is not None else None
        ),
        "repair_plan_sha256": payload.get("repair_plan_sha256"),
        "semantic_revision": revision,
    }


def _indexed_pairs(value, key: str) -> dict[str, dict]:
    if not isinstance(value, list):
        return {}
    indexed = {
        item.get(key): item
        for item in value
        if isinstance(item, dict) and _recorded_text(item.get(key))
    }
    return indexed if len(indexed) == len(value) else {}


def _nullable_sha256(value) -> bool:
    return value is None or (isinstance(value, str) and _SHA256.fullmatch(value) is not None)


def _reviewer_labels_match(pair: dict, labels: set[str]) -> bool:
    if not isinstance(pair, dict):
        return False
    winners = []
    judgments = pair.get("judgments")
    if isinstance(judgments, dict):
        winners.extend(
            judgment.get("winner") for judgment in judgments.values() if isinstance(judgment, dict)
        )
    overall = pair.get("overall")
    if isinstance(overall, dict):
        winners.append(overall.get("winner"))
    return all(winner in labels | {"tie"} for winner in winners)


def _reasoning_matrix(
    matrix,
    selected_level: str | None,
    selected_runs: list[dict],
    corpus: str | None,
    initial: str | None,
    expected: ReleaseInputs,
    failures: list[dict],
    *,
    replayed_effective: dict[tuple[str, str, str], dict],
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
                require_runtime_reasoning=level == LIBRARIAN_REASONING_LEVEL,
                failures=failures,
                replayed_effective=replayed_effective,
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
            review_root=Path(args.artifact).resolve().parent,
        )
    except (OSError, ReleaseInputError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
