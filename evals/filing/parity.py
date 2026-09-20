#!/usr/bin/env python3
"""Fail closed on replayable, source-inclusive filing evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from planner_eval import load_case, score
    from worktree import (
        apply_with_production_repair,
        effective_plan,
        initial_graph_manifest,
        prepared,
    )
except ModuleNotFoundError:
    from evals.filing.planner_eval import load_case, score
    from evals.filing.worktree import (
        apply_with_production_repair,
        effective_plan,
        initial_graph_manifest,
        prepared,
    )

from stigmergy.kernel.llm import (
    LIBRARIAN_MAX_TOKENS,
    LIBRARIAN_REASONING_LEVEL,
    LIBRARIAN_TEMPERATURE,
)
from stigmergy.knowledge.contract import KnowledgeContractError, librarian_skill_provenance
from stigmergy.knowledge.plan import FilingPlan
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

ARTIFACT_SCHEMA_VERSION = 6
REQUIRED_IMPLEMENTATIONS = frozenset({"hippocampus", "stigmergy"})
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
        "entity_editorial_quality",
        "entity_wikilinks",
        "anti_fragmentation",
        "editorial_quality",
        "seeded_update",
        "writer",
    }
)
BLIND_REVIEW_DIMENSIONS = (
    "factual_support",
    "relevance",
    "knowledge_graph_utility",
)
STIGMERGY_RUNTIME = {
    "model": "deepseek/deepseek-v4.1-flash",
    "provider": "openrouter:throughput",
    "max_tokens": LIBRARIAN_MAX_TOKENS,
    "temperature": LIBRARIAN_TEMPERATURE,
}

ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "admission_status",
        "blind_review_packet",
        "blind_editorial_review",
        "release",
        "runs",
        "reasoning_matrix",
    }
)
RELEASE_FIELDS = frozenset(
    {
        "corpus_sha256",
        "initial_graph_ref",
        "initial_graph_manifest_sha256",
        "source_cases",
        "source_fixtures",
        "candidate",
    }
)
CANDIDATE_FIELDS = frozenset({"commit", "librarian_skill_sha256", "brain_prompt"})
RUN_FIELDS = frozenset({"implementation", "run_id", "runtime", "execution", "provenance", "case_results"})
EXECUTION_FIELDS = frozenset({"mode", "configured_max_turns"})
CASE_FIELDS = frozenset({"case_id", "runtime", "execution", "input", "requests", "correction", "evidence", "output"})
INPUT_FIELDS = frozenset(
    {
        "brain_prompt",
        "case_sha256",
        "fixture_sha256",
        "source_sha256",
        "initial_worktree_manifest_sha256",
    }
)
REQUEST_FIELDS = frozenset({"initial", "correction", "total", "schema_retries", "elapsed_ms", "usage"})
CORRECTION_FIELDS = frozenset({"required", "attempted", "applied", "plan"})
EVIDENCE_FIELDS = frozenset({"input", "plans", "result"})
PLAN_EVIDENCE_FIELDS = frozenset({"initial", "correction", "effective"})
RESULT_FIELDS = frozenset({"score", "writer_gates", "raw_gates"})
WRITER_GATE_FIELDS = frozenset({"passed", "violations", "changed_paths"})
OUTPUT_FIELDS = frozenset({"sha256", "artifact_ref"})
MATRIX_FIELDS = frozenset({"reasoning_level", "runtime", "run_ids", "passed"})
PACKET_STATE_FIELDS = frozenset({"status", "raw_sha256", "canonical_sha256"})
PUBLIC_REVIEW_FIELDS = frozenset({"verdict", "provenance"})
PUBLIC_REVIEW_PROVENANCE_FIELDS = frozenset(
    {
        "reviewer",
        "method",
        "artifact_ref",
        "corpus_sha256",
        "initial_graph_ref",
        "initial_graph_manifest_sha256",
        "source_cases",
        "source_fixtures",
        "runs",
    }
)
REVIEW_FIELDS = frozenset(
    {
        "schema_version",
        "verdict",
        "reviewer_response",
        "packet",
        "mapping",
        "unblinding",
    }
)
PACKET_FIELDS = frozenset({"schema_version", "comparisons"})
PACKET_COMPARISON_FIELDS = frozenset({"comparison_id", "case_id", "source", "candidates"})
PACKET_SOURCE_FIELDS = frozenset({"sha256", "text"})
PACKET_CANDIDATE_FIELDS = frozenset(
    {"label", "case_id", "source_sha256", "effective_payload", "effective_payload_sha256"}
)
MAPPING_FIELDS = frozenset({"schema_version", "packet_sha256", "mapping"})
MAPPING_COMPARISON_FIELDS = frozenset({"comparison_id", "case_id", "source_sha256", "labels"})
MAPPING_LABEL_FIELDS = frozenset(
    {
        "label",
        "implementation",
        "run_id",
        "case_id",
        "case_output_sha256",
        "effective_payload_sha256",
        "initial_plan_sha256",
        "correction_plan_sha256",
        "correction",
    }
)
RESPONSE_FIELDS = frozenset({"reviewer_identity", "method", "packet_sha256", "pairs"})
RESPONSE_PAIR_FIELDS = frozenset({"pair_id", "judgments", "overall", "material_regression"})
JUDGMENT_FIELDS = frozenset({"winner", "reason"})
MATERIAL_REGRESSION_FIELDS = frozenset({"side", "reason"})
UNBLINDING_FIELDS = frozenset({"material_regressions", "stigmergy_material_regressions"})

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_REF = re.compile(r"^[0-9a-f]{40}$")
_CONTENT_ADDRESS = re.compile(r"^sha256:([0-9a-f]{64})$")
ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ReleaseInputs:
    commit: str
    librarian_skill_sha256: str
    initial_graph_manifest_sha256: str
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
    root = repo_root.resolve()
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReleaseInputError("candidate commit could not be resolved") from error
    if not _GIT_REF.fullmatch(commit):
        raise ReleaseInputError("candidate commit is invalid")

    skill_path = root / "src" / "stigmergy" / "knowledge" / "librarian_skill.md"
    case_dir = root / "evals" / "filing" / "cases"
    template = root / "evals" / "filing" / "repo"
    if not skill_path.is_file() or not case_dir.is_dir() or not template.is_dir():
        raise ReleaseInputError("candidate parity inputs are missing")
    try:
        manifest = initial_graph_manifest(template)
    except ValueError as error:
        raise ReleaseInputError(str(error)) from error

    case_paths = {path.stem: path for path in sorted(case_dir.glob("*.json"))}
    cases = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in case_paths.items()}
    if not cases:
        raise ReleaseInputError("candidate source cases are missing")
    fixtures: dict[str, str] = {}
    fixture_paths: dict[str, Path] = {}
    for key, path in case_paths.items():
        try:
            fixture = path.parent / str(json.loads(path.read_text(encoding="utf-8"))["fixture_path"])
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ReleaseInputError(f"candidate case fixture is invalid: {key}") from error
        if not fixture.is_file():
            raise ReleaseInputError(f"candidate case fixture is missing: {key}")
        fixture_paths[key] = fixture
        fixtures[key] = hashlib.sha256(fixture.read_bytes()).hexdigest()

    prompt = None
    if brain_root is not None:
        try:
            prompt = librarian_skill_provenance(brain_root, expected_commit=brain_commit)
        except KnowledgeContractError as error:
            raise ReleaseInputError(str(error)) from error
    return ReleaseInputs(
        commit=commit,
        librarian_skill_sha256=hashlib.sha256(skill_path.read_bytes()).hexdigest(),
        initial_graph_manifest_sha256=manifest,
        source_cases=cases,
        source_fixtures=fixtures,
        case_paths=case_paths,
        fixture_paths=fixture_paths,
        repo_root=root,
        brain_prompt=prompt,
    )


def evaluate(
    artifact: dict[str, Any],
    *,
    expected: ReleaseInputs | None = None,
    review_root: Path | None = None,
) -> dict[str, Any]:
    expected = expected or current_release_inputs(ROOT)
    failures: list[dict[str, Any]] = []
    if not _exact_keys(artifact, ROOT_FIELDS, failures, "artifact", "artifact-shape"):
        artifact = {}
    if artifact.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        _failure(failures, "artifact", "schema-version")

    binding = _binding(artifact.get("release"), expected, failures)
    _admission(artifact, failures)
    replayed: dict[tuple[str, str, str], dict[str, Any]] = {}
    runs = _runs(artifact.get("runs"), binding, expected, failures, replayed)
    groups = {
        name: [run for run in runs.values() if run["implementation"] == name] for name in REQUIRED_IMPLEMENTATIONS
    }
    for name in sorted(REQUIRED_IMPLEMENTATIONS - {run["implementation"] for run in runs.values()}):
        _failure(failures, name, "missing-implementation")
    selected = _matrix(artifact.get("reasoning_matrix"), runs, groups["stigmergy"], failures)
    _blind(
        artifact.get("blind_editorial_review"),
        artifact.get("blind_review_packet"),
        binding,
        {"hippocampus": groups["hippocampus"], "stigmergy": selected},
        replayed,
        review_root,
        expected.repo_root,
        failures,
    )
    return {
        "passed": not failures,
        "missing_implementations": sorted(REQUIRED_IMPLEMENTATIONS - {run["implementation"] for run in runs.values()}),
        "failures": failures,
        "corpus_sha256": binding["corpus_sha256"],
        "initial_graph_ref": binding["initial_graph_ref"],
    }


def _failure(failures: list[dict[str, Any]], implementation: str, reason: str, **details: Any) -> None:
    failures.append({"implementation": implementation, "reason": reason, **details})


def _exact_keys(
    value: object,
    expected_keys: frozenset[str],
    failures: list[dict[str, Any]],
    implementation: str,
    reason: str,
    **details: Any,
) -> bool:
    if not isinstance(value, dict) or set(value) != expected_keys:
        _failure(failures, implementation, reason, **details)
        return False
    return True


def _binding(value: object, expected: ReleaseInputs, failures: list[dict[str, Any]]) -> dict[str, Any]:
    if not _exact_keys(value, RELEASE_FIELDS, failures, "artifact", "release-shape"):
        value = {}
    candidate = value.get("candidate") if isinstance(value, dict) else None
    if not _exact_keys(candidate, CANDIDATE_FIELDS, failures, "artifact", "candidate-shape"):
        candidate = {}
    corpus = _sha(value.get("corpus_sha256"), "corpus_sha256", failures)
    initial = _ref(value.get("initial_graph_ref"), "initial_graph_ref", failures)
    manifest = _sha(value.get("initial_graph_manifest_sha256"), "initial_graph_manifest_sha256", failures)
    cases = _cases(value.get("source_cases"), failures, "source-cases")
    fixtures = _cases(value.get("source_fixtures"), failures, "source-fixtures")
    commit = _ref(candidate.get("commit"), "candidate.commit", failures)
    skill = _sha(candidate.get("librarian_skill_sha256"), "candidate.librarian_skill_sha256", failures)
    prompt = _prompt(candidate.get("brain_prompt"), failures)
    if commit != expected.commit:
        _failure(failures, "artifact", "candidate-commit")
    if skill != expected.librarian_skill_sha256:
        _failure(failures, "artifact", "candidate-skill")
    if manifest != expected.initial_graph_manifest_sha256:
        _failure(failures, "artifact", "candidate-initial-graph-manifest")
    if cases != expected.source_cases:
        _failure(failures, "artifact", "candidate-cases")
    if fixtures != expected.source_fixtures:
        _failure(failures, "artifact", "candidate-fixtures")
    if expected.brain_prompt is None or prompt != expected.brain_prompt:
        _failure(failures, "artifact", "candidate-brain-prompt")
    return {
        "corpus_sha256": corpus,
        "initial_graph_ref": initial,
        "initial_graph_manifest_sha256": manifest,
        "source_cases": cases,
        "source_fixtures": fixtures,
        "candidate": {
            "commit": commit,
            "librarian_skill_sha256": skill,
            "brain_prompt": prompt,
        },
    }


def _sha(value: object, field: str, failures: list[dict[str, Any]]) -> str | None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value) or len(set(value)) == 1:
        _failure(failures, "artifact", field)
        return None
    return value


def _ref(value: object, field: str, failures: list[dict[str, Any]]) -> str | None:
    if not isinstance(value, str) or not _GIT_REF.fullmatch(value) or len(set(value)) == 1:
        _failure(failures, "artifact", field)
        return None
    return value


def _cases(value: object, failures: list[dict[str, Any]], field: str) -> dict[str, str]:
    if not isinstance(value, list) or not value:
        _failure(failures, "artifact", field)
        return {}
    result: dict[str, str] = {}
    for item in value:
        if not _exact_keys(item, frozenset({"id", "sha256"}), failures, "artifact", field):
            continue
        case_id = item["id"]
        digest = _sha(item["sha256"], "source-case-sha256", failures)
        if not _text(case_id):
            _failure(failures, "artifact", "source-case-id")
        elif case_id in result:
            _failure(failures, "artifact", "duplicate-source-case")
        elif digest is not None:
            result[case_id] = digest
    return result


def _prompt(value: object, failures: list[dict[str, Any]]) -> dict[str, str] | None:
    if not _exact_keys(value, frozenset({"commit", "sha256"}), failures, "artifact", "brain-prompt"):
        return None
    commit = _ref(value["commit"], "brain_prompt.commit", failures)
    digest = _sha(value["sha256"], "brain_prompt.sha256", failures)
    return {"commit": commit, "sha256": digest} if commit and digest else None


def _admission(artifact: dict[str, Any], failures: list[dict[str, Any]]) -> None:
    if artifact.get("admission_status") != "passed":
        _failure(failures, "artifact", "admission-status")
    if _pending(artifact):
        _failure(failures, "artifact", "pending-admission-field")
    packet = artifact.get("blind_review_packet")
    if not _exact_keys(packet, PACKET_STATE_FIELDS, failures, "review", "blind-review-packet"):
        return
    if packet["status"] != "completed":
        _failure(failures, "review", "blind-review-packet")


def _pending(value: object) -> bool:
    if isinstance(value, dict):
        return any("pending" in str(key).lower() or _pending(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_pending(item) for item in value)
    return False


def _runs(
    value: object,
    binding: dict[str, Any],
    expected: ReleaseInputs,
    failures: list[dict[str, Any]],
    replayed: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or not value:
        _failure(failures, "artifact", "runs")
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in value:
        if not _exact_keys(item, RUN_FIELDS, failures, "artifact", "run-shape"):
            continue
        implementation = item["implementation"]
        run_id = item["run_id"]
        if implementation not in REQUIRED_IMPLEMENTATIONS or not _text(run_id):
            _failure(failures, "artifact", "run-shape")
            continue
        if run_id in result:
            _failure(failures, implementation, "duplicate-run-id", run_id=run_id)
            continue
        result[run_id] = item
        _run(item, binding, expected, failures, replayed)
    return result


def _runtime_fields(implementation: str) -> frozenset[str]:
    if implementation == "stigmergy":
        return frozenset({"model", "reasoning_level", "provider", "max_tokens", "temperature"})
    return frozenset({"model", "reasoning_level", "provider"})


def _provenance_fields(implementation: str) -> frozenset[str]:
    fields = {
        "corpus_sha256",
        "initial_graph_ref",
        "initial_graph_manifest_sha256",
        "source_cases",
        "source_fixtures",
    }
    if implementation == "stigmergy":
        fields.add("candidate")
    return frozenset(fields)


def _run(
    run: dict[str, Any],
    binding: dict[str, Any],
    expected: ReleaseInputs,
    failures: list[dict[str, Any]],
    replayed: dict[tuple[str, str, str], dict[str, Any]],
) -> None:
    implementation = run["implementation"]
    runtime = run["runtime"]
    execution = run["execution"]
    if not _exact_keys(runtime, _runtime_fields(implementation), failures, implementation, "runtime-metadata"):
        return
    if not all(_text(runtime.get(key)) for key in ("model", "reasoning_level", "provider")):
        _failure(failures, implementation, "runtime-metadata")
        return
    if implementation == "stigmergy" and runtime != {
        **STIGMERGY_RUNTIME,
        "reasoning_level": runtime["reasoning_level"],
    }:
        _failure(failures, implementation, "runtime-route")
    if not _exact_keys(execution, EXECUTION_FIELDS, failures, implementation, "execution-metadata"):
        return
    if not _text(execution["mode"]) or not _nonnegative(execution["configured_max_turns"]):
        _failure(failures, implementation, "execution-metadata")
        return
    provenance = run["provenance"]
    if not _exact_keys(provenance, _provenance_fields(implementation), failures, implementation, "run-provenance"):
        return
    for key in (
        "corpus_sha256",
        "initial_graph_ref",
        "initial_graph_manifest_sha256",
        "source_cases",
        "source_fixtures",
    ):
        if provenance[key] != binding[key]:
            _failure(failures, implementation, "stale-or-mismatched-provenance")
            break
    if implementation == "stigmergy" and provenance["candidate"] != binding["candidate"]:
        _failure(failures, implementation, "stale-or-mismatched-candidate")

    cases = run["case_results"]
    if not isinstance(cases, list) or not cases:
        _failure(failures, implementation, "aggregate-only-evidence")
        return
    found: set[str] = set()
    for case in cases:
        if not _exact_keys(case, CASE_FIELDS, failures, implementation, "case-result-shape"):
            continue
        case_id = case["case_id"]
        if not _text(case_id) or case_id in found:
            _failure(failures, implementation, "case-result-shape")
            continue
        found.add(case_id)
        if case_id not in expected.source_cases:
            _failure(failures, implementation, "unknown-case-result", case_id=case_id)
            continue
        _case(case, implementation, runtime, execution, expected, run["run_id"], failures, replayed)
    for case_id in sorted(set(expected.source_cases) - found):
        _failure(failures, implementation, "missing-case-result", case_id=case_id)


def _case(
    case: dict[str, Any],
    implementation: str,
    runtime: dict[str, Any],
    execution: dict[str, Any],
    expected: ReleaseInputs,
    run_id: str,
    failures: list[dict[str, Any]],
    replayed: dict[tuple[str, str, str], dict[str, Any]],
) -> None:
    case_id = case["case_id"]
    input_data = case["input"]
    requests = case["requests"]
    correction = case["correction"]
    evidence = case["evidence"]
    output = case["output"]
    if case["runtime"] != runtime or case["execution"] != execution:
        _failure(failures, implementation, "case-runtime", case_id=case_id)
        return
    if not _exact_keys(input_data, INPUT_FIELDS, failures, implementation, "case-input", case_id=case_id):
        return
    if (
        input_data["case_sha256"] != expected.source_cases[case_id]
        or input_data["fixture_sha256"] != expected.source_fixtures[case_id]
        or input_data["source_sha256"] != expected.source_fixtures[case_id]
        or (implementation == "stigmergy" and input_data["brain_prompt"] != expected.brain_prompt)
        or (implementation == "hippocampus" and input_data["brain_prompt"] is not None)
    ):
        _failure(failures, implementation, "case-input", case_id=case_id)
        return
    if not _exact_keys(requests, REQUEST_FIELDS, failures, implementation, "case-request-telemetry", case_id=case_id):
        return
    if (
        any(not _nonnegative(requests[key]) for key in REQUEST_FIELDS - {"usage"})
        or not isinstance(requests["usage"], dict)
        or any(not _nonnegative(value) for value in requests["usage"].values())
        or requests["total"] != requests["initial"] + requests["correction"]
        or requests["total"] > execution["configured_max_turns"]
    ):
        _failure(failures, implementation, "case-request-telemetry", case_id=case_id)
        return
    if not _valid_correction(correction, requests):
        _failure(failures, implementation, "case-correction", case_id=case_id)
        return
    if not _exact_keys(output, OUTPUT_FIELDS, failures, implementation, "case-output", case_id=case_id):
        return
    if not _SHA256.fullmatch(output["sha256"]) or output["artifact_ref"] != f"sha256:{output['sha256']}":
        _failure(failures, implementation, "case-output", case_id=case_id)
        return
    if not _exact_keys(evidence, EVIDENCE_FIELDS, failures, implementation, "case-evidence", case_id=case_id):
        return
    if evidence["input"] != input_data:
        _failure(failures, implementation, "case-evidence", case_id=case_id)
        return
    plans = evidence["plans"]
    result = evidence["result"]
    if not _exact_keys(plans, PLAN_EVIDENCE_FIELDS, failures, implementation, "case-evidence", case_id=case_id):
        return
    if plans["correction"] != correction["plan"]:
        _failure(failures, implementation, "case-evidence", case_id=case_id)
        return
    if not _exact_keys(result, RESULT_FIELDS, failures, implementation, "case-evidence", case_id=case_id):
        return
    if not _exact_keys(
        result["writer_gates"], WRITER_GATE_FIELDS, failures, implementation, "case-evidence", case_id=case_id
    ):
        return
    if hashlib.sha256(_json(evidence)).hexdigest() != output["sha256"]:
        _failure(failures, implementation, "case-evidence", case_id=case_id)
        return
    try:
        initial = FilingPlan.model_validate(plans["initial"])
        correction_plan = FilingPlan.model_validate(correction["plan"]) if correction["plan"] else None
        effective = FilingPlan.model_validate(plans["effective"])
        source = expected.fixture_paths[case_id].read_text(encoding="utf-8")
        with prepared(
            load_case(expected.case_paths[case_id]),
            source,
            template=str(expected.repo_root / "evals" / "filing" / "repo"),
        ) as worktree:
            if input_data["initial_worktree_manifest_sha256"] != worktree.initial_worktree_manifest_sha256:
                _failure(failures, implementation, "initial-worktree-manifest", case_id=case_id)
                return
            writer, replayed_plan = apply_with_production_repair(
                worktree,
                initial,
                _RecordedCorrection(correction_plan, requests["correction"]),
                planning_model_requests=requests["initial"],
                max_turns=execution["configured_max_turns"],
                return_plan=True,
            )
            replayed_plan = effective_plan(worktree, replayed_plan) if writer["passed"] else replayed_plan
    except (OSError, TypeError, ValueError) as error:
        _failure(failures, implementation, "case-replay", case_id=case_id, error=error.__class__.__name__)
        return
    actual_correction = {
        "required": writer["semantic_revision_required"],
        "attempted": writer["semantic_revision_attempted"],
        "applied": writer["semantic_revision_applied"],
        "plan": correction_plan.model_dump(mode="json") if writer["semantic_revision_applied"] else None,
    }
    if correction != actual_correction:
        _failure(failures, implementation, "case-correction", case_id=case_id)
        return
    replayed_payload = replayed_plan.model_dump(mode="json")
    if _json(replayed_payload) != _json(effective.model_dump(mode="json")):
        _failure(failures, implementation, "replay-effective-plan-mismatch", case_id=case_id)
        return
    semantic = score(replayed_plan, load_case(expected.case_paths[case_id]), source_text=source)
    raw = {
        **{gate: semantic[gate]["passed"] for gate in sorted(semantic) if gate != "passed"},
        "writer": writer["passed"],
    }
    if _json(result["score"]) != _json(semantic):
        _failure(failures, implementation, "case-semantic-score", case_id=case_id)
    if result["writer_gates"] != _writer_gates(writer):
        _failure(failures, implementation, "case-writer-gate", case_id=case_id)
    if result["raw_gates"] != raw:
        _failure(failures, implementation, "case-raw-gates", case_id=case_id)
    replayed[(implementation, run_id, case_id)] = {
        "case_id": case_id,
        "source_sha256": input_data["source_sha256"],
        "source_text": source,
        "effective_payload": replayed_payload,
    }


def _valid_correction(correction: object, requests: dict[str, Any]) -> bool:
    if not isinstance(correction, dict) or set(correction) != CORRECTION_FIELDS:
        return False
    if any(not isinstance(correction[key], bool) for key in ("required", "attempted", "applied")):
        return False
    empty = {"required": False, "attempted": False, "applied": False, "plan": None}
    if not correction["required"]:
        return correction == empty
    if correction["attempted"] != (requests["correction"] >= 1):
        return False
    if correction["applied"]:
        return correction["attempted"] and isinstance(correction["plan"], dict)
    return correction["plan"] is None


class _RecordedCorrection:
    def __init__(self, plan: FilingPlan | None, requests: int):
        self.plan = plan
        self.model_requests = requests

    def revise(self, **_kwargs: Any) -> PlanRun:
        if self.plan is None:
            raise GateRefused("recorded correction is unavailable")
        return PlanRun(self.plan, model_requests=self.model_requests)


def _writer_gates(gates: dict[str, Any]) -> dict[str, Any]:
    return {key: gates.get(key) for key in WRITER_GATE_FIELDS}


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _passed(case: dict[str, Any]) -> bool:
    result = case.get("evidence", {}).get("result", {})
    raw = result.get("raw_gates")
    return (
        bool(result.get("score", {}).get("passed"))
        and bool(result.get("writer_gates", {}).get("passed"))
        and isinstance(raw, dict)
        and set(raw) == REQUIRED_SEMANTIC_GATES
        and all(value is True or value == "passed" for value in raw.values())
    )


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "<" not in value and ">" not in value


def _nonnegative(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _matrix(
    value: object,
    runs: dict[str, dict[str, Any]],
    stigmergy_runs: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        _failure(failures, "stigmergy", "reasoning-matrix")
        return []
    levels: dict[str, tuple[bool, list[dict[str, Any]]]] = {}
    run_ids: set[str] = set()
    for row in value:
        if not _exact_keys(row, MATRIX_FIELDS, failures, "stigmergy", "reasoning-matrix"):
            continue
        level = row["reasoning_level"]
        if level not in REASONING_LEVELS or level in levels:
            _failure(failures, "stigmergy", "reasoning-matrix")
            continue
        runtime = row["runtime"]
        ids = row["run_ids"]
        if (
            runtime != {**STIGMERGY_RUNTIME, "reasoning_level": level}
            or not isinstance(ids, list)
            or not ids
            or len(set(ids)) != len(ids)
            or not all(_text(run_id) for run_id in ids)
        ):
            _failure(failures, "stigmergy", "reasoning-matrix")
            continue
        selected: list[dict[str, Any]] = []
        for run_id in ids:
            run = runs.get(run_id)
            if run is None or run["implementation"] != "stigmergy" or run["runtime"] != runtime:
                _failure(failures, "stigmergy", "reasoning-matrix-run", run_id=run_id)
                if level == LIBRARIAN_REASONING_LEVEL:
                    _failure(failures, "stigmergy", "runtime-production-reasoning", run_id=run_id)
                continue
            if level == LIBRARIAN_REASONING_LEVEL and run["runtime"]["reasoning_level"] != LIBRARIAN_REASONING_LEVEL:
                _failure(failures, "stigmergy", "runtime-production-reasoning", run_id=run_id)
            if run["execution"] != {
                "mode": PRODUCTION_EQUIVALENT_MODE,
                "configured_max_turns": PRODUCTION_MAX_TURNS,
            }:
                _failure(failures, "stigmergy", "production-equivalence", run_id=run_id)
            selected.append(run)
            run_ids.add(run_id)
        observed = bool(selected) and all(_passed(case) for run in selected for case in run["case_results"])
        if row["passed"] != observed:
            _failure(failures, "stigmergy", "reasoning-matrix-result")
        levels[level] = (observed, selected)
    if run_ids != {run["run_id"] for run in stigmergy_runs}:
        _failure(failures, "stigmergy", "reasoning-matrix-coverage")
    selected = levels.get(LIBRARIAN_REASONING_LEVEL)
    if selected is None or not selected[0]:
        _failure(failures, "stigmergy", "selected-reasoning-not-passing")
        return []
    for level in REASONING_LEVELS[: REASONING_LEVELS.index(LIBRARIAN_REASONING_LEVEL)]:
        if level not in levels:
            _failure(failures, "stigmergy", "reasoning-matrix-incomplete")
        elif levels[level][0]:
            _failure(failures, "stigmergy", "reasoning-not-lowest-passing")
    if len(selected[1]) < SELECTED_LEVEL_MIN_REPEATS:
        _failure(failures, "stigmergy", "selected-reasoning-insufficient-repeats")
    return selected[1]


def _blind(
    value: object,
    packet_state: object,
    binding: dict[str, Any],
    runs: dict[str, list[dict[str, Any]]],
    replayed: dict[tuple[str, str, str], dict[str, Any]],
    review_root: Path | None,
    candidate_root: Path,
    failures: list[dict[str, Any]],
) -> None:
    if not _exact_keys(value, PUBLIC_REVIEW_FIELDS, failures, "review", "blind-editorial-review"):
        return
    if value["verdict"] != "no_material_stigmergy_regression":
        _failure(failures, "review", "blind-editorial-review")
        return
    provenance = value["provenance"]
    if not _exact_keys(
        provenance,
        PUBLIC_REVIEW_PROVENANCE_FIELDS,
        failures,
        "review",
        "stale-or-mismatched-review",
    ):
        return
    reference = _CONTENT_ADDRESS.fullmatch(provenance["artifact_ref"])
    expected_runs = {key: sorted(run["run_id"] for run in items) for key, items in runs.items()}
    normalized_runs = _normalized_runs(provenance["runs"])
    if (
        reference is None
        or not _text(provenance["reviewer"])
        or not _text(provenance["method"])
        or any(
            provenance[key] != binding[key]
            for key in (
                "corpus_sha256",
                "initial_graph_ref",
                "initial_graph_manifest_sha256",
                "source_cases",
                "source_fixtures",
            )
        )
        or normalized_runs != expected_runs
    ):
        _failure(failures, "review", "stale-or-mismatched-review")
        return
    if review_root is None or review_root.resolve().is_relative_to(candidate_root.resolve()):
        _failure(failures, "review", "review-evidence-root")
        return

    review = _load_content_addressed(
        review_root / f"blind-review-unblind-{reference.group(1)}.json",
        reference.group(1),
        failures,
    )
    if review is None or not _exact_keys(review, REVIEW_FIELDS, failures, "review", "blind-review-evidence"):
        return
    if review["schema_version"] != "blind-editorial-review-unblind-v2" or review["verdict"] != value["verdict"]:
        _failure(failures, "review", "blind-review-evidence")
        return
    response = _load_review_document(
        review_root,
        review["reviewer_response"],
        "blind-reviewer-response-",
        ".json",
        True,
        failures,
    )
    packet = _load_review_document(
        review_root,
        review["packet"],
        "blind-review-packet-",
        ".json",
        False,
        failures,
    )
    mapping = _load_review_document(
        review_root,
        review["mapping"],
        "blind-review-mapping-",
        ".secret.json",
        False,
        failures,
    )
    if response is None or packet is None or mapping is None:
        return
    response_raw, response_value = response
    packet_raw, packet_value = packet
    mapping_value = mapping[1]
    if not _packet_state_matches(packet_state, packet_raw, packet_value, failures):
        return
    if (
        not _exact_keys(packet_value, PACKET_FIELDS, failures, "review", "blind-review-evidence")
        or not _exact_keys(mapping_value, MAPPING_FIELDS, failures, "review", "blind-review-evidence")
        or not _exact_keys(response_value, RESPONSE_FIELDS, failures, "review", "blind-review-evidence")
        or packet_value["schema_version"] != 1
        or mapping_value["schema_version"] != 1
        or response_value["reviewer_identity"] != provenance["reviewer"]
        or response_value["method"] != provenance["method"]
        or response_value["packet_sha256"] != hashlib.sha256(packet_raw).hexdigest()
        or mapping_value["packet_sha256"] != hashlib.sha256(_json(packet_value)).hexdigest()
    ):
        _failure(failures, "review", "blind-review-evidence")
        return

    packet_pairs = _index(packet_value["comparisons"], "comparison_id")
    mapping_pairs = _index(mapping_value["mapping"], "comparison_id")
    response_pairs = _index(response_value["pairs"], "pair_id")
    if not packet_pairs or set(packet_pairs) != set(mapping_pairs) or set(packet_pairs) != set(response_pairs):
        _failure(failures, "review", "blind-review-evidence")
        return
    known = _blind_known(runs, replayed, failures)
    if known is None:
        return

    reviewed: set[tuple[str, str, str]] = set()
    regressions: list[dict[str, Any]] = []
    for comparison_id, packet_pair in packet_pairs.items():
        regression = _validate_blind_comparison(
            comparison_id,
            packet_pair,
            mapping_pairs[comparison_id],
            response_pairs[comparison_id],
            known,
            reviewed,
            failures,
        )
        if regression is False:
            return
        if regression is not None:
            regressions.append(regression)
    if reviewed != set(known):
        _failure(failures, "review", "blind-review-evidence")
        return
    stigmergy_regressions = [item for item in regressions if item["implementation"] == "stigmergy"]
    unblinding = review["unblinding"]
    if (
        not _exact_keys(unblinding, UNBLINDING_FIELDS, failures, "review", "blind-review-evidence")
        or unblinding["material_regressions"] != regressions
        or unblinding["stigmergy_material_regressions"] != stigmergy_regressions
        or stigmergy_regressions
    ):
        _failure(failures, "review", "blind-review-evidence")


def _normalized_runs(value: object) -> dict[str, list[str]] | None:
    if not isinstance(value, dict) or set(value) != REQUIRED_IMPLEMENTATIONS:
        return None
    if not all(isinstance(run_ids, list) and all(_text(run_id) for run_id in run_ids) for run_ids in value.values()):
        return None
    return {key: sorted(run_ids) for key, run_ids in value.items()}


def _packet_state_matches(
    value: object,
    raw: bytes,
    packet: dict[str, Any],
    failures: list[dict[str, Any]],
) -> bool:
    if not _exact_keys(value, PACKET_STATE_FIELDS, failures, "review", "blind-review-packet"):
        return False
    expected = {
        "status": "completed",
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "canonical_sha256": hashlib.sha256(_json(packet)).hexdigest(),
    }
    if value != expected:
        _failure(failures, "review", "blind-review-packet")
        return False
    return True


def _blind_known(
    runs: dict[str, list[dict[str, Any]]],
    replayed: dict[tuple[str, str, str], dict[str, Any]],
    failures: list[dict[str, Any]],
) -> dict[tuple[str, str, str], dict[str, Any]] | None:
    known: dict[tuple[str, str, str], dict[str, Any]] = {}
    for values in runs.values():
        for run in values:
            for case in run["case_results"]:
                binding = _blind_binding(
                    case,
                    replayed.get((run["implementation"], run["run_id"], case["case_id"])),
                )
                if binding is None:
                    _failure(failures, "review", "blind-review-evidence")
                    return None
                key = (run["implementation"], run["run_id"], case["case_id"])
                if key in known:
                    _failure(failures, "review", "blind-review-evidence")
                    return None
                binding["implementation"] = run["implementation"]
                binding["run_id"] = run["run_id"]
                known[key] = binding
    return known


def _validate_blind_comparison(
    comparison_id: str,
    packet: dict[str, Any],
    mapping: dict[str, Any],
    response: dict[str, Any],
    known: dict[tuple[str, str, str], dict[str, Any]],
    reviewed: set[tuple[str, str, str]],
    failures: list[dict[str, Any]],
) -> dict[str, Any] | None | bool:
    if (
        not _exact_keys(packet, PACKET_COMPARISON_FIELDS, failures, "review", "blind-review-evidence")
        or not _exact_keys(mapping, MAPPING_COMPARISON_FIELDS, failures, "review", "blind-review-evidence")
        or not _exact_keys(response, RESPONSE_PAIR_FIELDS, failures, "review", "blind-review-evidence")
        or packet["comparison_id"] != comparison_id
        or mapping["comparison_id"] != comparison_id
        or response["pair_id"] != comparison_id
        or not _text(packet["case_id"])
        or mapping["case_id"] != packet["case_id"]
    ):
        return False
    source = packet["source"]
    if (
        not _exact_keys(source, PACKET_SOURCE_FIELDS, failures, "review", "blind-review-evidence")
        or source["sha256"] != mapping["source_sha256"]
        or not _SHA256.fullmatch(source["sha256"])
        or not isinstance(source["text"], str)
        or hashlib.sha256(source["text"].encode("utf-8")).hexdigest() != source["sha256"]
    ):
        return False
    candidates = _index(packet["candidates"], "label")
    labels = _index(mapping["labels"], "label")
    if len(candidates) != 2 or set(candidates) != set(labels):
        _failure(failures, "review", "blind-review-evidence")
        return False
    for label, candidate in candidates.items():
        mapped = labels[label]
        if (
            not _exact_keys(candidate, PACKET_CANDIDATE_FIELDS, failures, "review", "blind-review-evidence")
            or not _exact_keys(mapped, MAPPING_LABEL_FIELDS, failures, "review", "blind-review-evidence")
            or candidate["label"] != label
            or mapped["label"] != label
            or candidate["case_id"] != packet["case_id"]
            or mapped["case_id"] != packet["case_id"]
            or candidate["source_sha256"] != source["sha256"]
            or candidate["effective_payload_sha256"]
            != hashlib.sha256(_json(candidate["effective_payload"])).hexdigest()
            or mapped["effective_payload_sha256"] != candidate["effective_payload_sha256"]
        ):
            return False
        key = (mapped["implementation"], mapped["run_id"], mapped["case_id"])
        expected = known.get(key)
        if expected is None or any(
            mapped[field] != expected[field]
            for field in (
                "case_output_sha256",
                "effective_payload_sha256",
                "initial_plan_sha256",
                "correction_plan_sha256",
                "correction",
            )
        ):
            _failure(failures, "review", "blind-review-evidence")
            return False
        if (
            expected["source_sha256"] != source["sha256"]
            or expected["source_text"] != source["text"]
            or _json(expected["effective_payload"]) != _json(candidate["effective_payload"])
        ):
            _failure(failures, "review", "blind-review-evidence")
            return False
        if expected["implementation"] == "stigmergy" and key in reviewed:
            _failure(failures, "review", "blind-review-evidence")
            return False
        reviewed.add(key)
    return _review_regression(response, labels, comparison_id, failures)


def _review_regression(
    response: dict[str, Any],
    labels: dict[str, dict[str, Any]],
    comparison_id: str,
    failures: list[dict[str, Any]],
) -> dict[str, Any] | None | bool:
    judgments = response["judgments"]
    overall = response["overall"]
    material = response["material_regression"]
    if (
        not isinstance(judgments, dict)
        or set(judgments) != set(BLIND_REVIEW_DIMENSIONS)
        or not _exact_keys(overall, JUDGMENT_FIELDS, failures, "review", "blind-review-evidence")
        or not _exact_keys(material, MATERIAL_REGRESSION_FIELDS, failures, "review", "blind-review-evidence")
    ):
        return False
    for dimension in BLIND_REVIEW_DIMENSIONS:
        if not _exact_keys(judgments[dimension], JUDGMENT_FIELDS, failures, "review", "blind-review-evidence"):
            return False
    decisions = [*judgments.values(), overall]
    if not all(decision["winner"] in set(labels) | {"tie"} and _text(decision["reason"]) for decision in decisions):
        _failure(failures, "review", "blind-review-evidence")
        return False
    side = material["side"]
    if side == "none":
        if not _text(material["reason"]):
            _failure(failures, "review", "blind-review-evidence")
            return False
        return None
    if side not in labels or not _text(material["reason"]) or overall["winner"] in {"tie", side}:
        _failure(failures, "review", "blind-review-evidence")
        return False
    selected = labels[side]
    return {
        "comparison_id": comparison_id,
        "candidate_label": selected["label"],
        "implementation": selected["implementation"],
        "run_id": selected["run_id"],
        "case_id": selected["case_id"],
        "source_sha256": selected["source_sha256"],
        "case_output_sha256": selected["case_output_sha256"],
        "effective_payload_sha256": selected["effective_payload_sha256"],
        "reason": material["reason"],
    }


def _load_content_addressed(
    path: Path,
    digest: str,
    failures: list[dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _failure(failures, "review", "blind-review-evidence")
        return None
    if not isinstance(value, dict) or hashlib.sha256(_json(value)).hexdigest() != digest:
        _failure(failures, "review", "blind-review-evidence")
        return None
    return value


def _load_review_document(
    root: Path,
    metadata: object,
    prefix: str,
    suffix: str,
    requires_reference: bool,
    failures: list[dict[str, Any]],
) -> tuple[bytes, dict[str, Any]] | None:
    metadata_fields = frozenset({"path", "raw_sha256", "canonical_sha256", "artifact_ref"})
    if not requires_reference:
        metadata_fields = frozenset({"path", "raw_sha256", "canonical_sha256"})
    if not _exact_keys(metadata, metadata_fields, failures, "review", "blind-review-evidence"):
        return None
    canonical = metadata["canonical_sha256"]
    raw_digest = metadata["raw_sha256"]
    reference = _CONTENT_ADDRESS.fullmatch(metadata.get("artifact_ref", ""))
    if (
        not isinstance(canonical, str)
        or not _SHA256.fullmatch(canonical)
        or not isinstance(raw_digest, str)
        or not _SHA256.fullmatch(raw_digest)
        or (requires_reference and (reference is None or reference.group(1) != canonical))
        or (not requires_reference and reference is not None)
        or metadata["path"] != f"{prefix}{canonical}{suffix}"
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
    if (
        not isinstance(value, dict)
        or hashlib.sha256(raw).hexdigest() != raw_digest
        or hashlib.sha256(_json(value)).hexdigest() != canonical
    ):
        _failure(failures, "review", "blind-review-evidence")
        return None
    return raw, value


def _index(value: object, key: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        return {}
    indexed = {item.get(key): item for item in value if isinstance(item, dict) and _text(item.get(key))}
    return indexed if len(indexed) == len(value) else {}


def _blind_binding(case: dict[str, Any], replayed: dict[str, Any] | None) -> dict[str, Any] | None:
    evidence = case.get("evidence")
    input_data = case.get("input")
    correction = case.get("correction")
    output = case.get("output")
    if (
        not isinstance(evidence, dict)
        or not isinstance(input_data, dict)
        or not isinstance(correction, dict)
        or not isinstance(output, dict)
        or not isinstance(replayed, dict)
    ):
        return None
    plans = evidence.get("plans")
    if not isinstance(plans, dict) or not isinstance(plans.get("initial"), dict):
        return None
    revised = plans.get("correction")
    if revised is not None and not isinstance(revised, dict):
        return None
    effective = replayed.get("effective_payload")
    source = replayed.get("source_text")
    source_sha = replayed.get("source_sha256")
    if not isinstance(effective, dict) or not isinstance(source, str) or not _SHA256.fullmatch(str(source_sha)):
        return None
    return {
        "case_output_sha256": output.get("sha256"),
        "case_id": case.get("case_id"),
        "source_sha256": source_sha,
        "source_text": source,
        "effective_payload": effective,
        "effective_payload_sha256": hashlib.sha256(_json(effective)).hexdigest(),
        "initial_plan_sha256": hashlib.sha256(_json(plans["initial"])).hexdigest(),
        "correction_plan_sha256": hashlib.sha256(_json(revised)).hexdigest() if revised is not None else None,
        "correction": correction,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--repo-root", default=str(ROOT))
    parser.add_argument("--brain-root", required=True)
    parser.add_argument("--brain-commit", required=True)
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
