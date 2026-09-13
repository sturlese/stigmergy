#!/usr/bin/env python3
"""Fail closed on the recorded real-model Hippocampus/Stigmergy parity gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

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
STIGMERGY_RUNTIME = {"model": "openai/gpt-oss-120b", "provider": "cerebras"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_REF = re.compile(r"^[0-9a-f]{40}$")
_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")
ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ReleaseInputs:
    """Candidate-owned inputs that an artifact must not be able to self-attest."""

    commit: str
    librarian_skill_sha256: str
    source_cases: dict[str, str]


class ReleaseInputError(ValueError):
    pass


def current_release_inputs(repo_root: Path) -> ReleaseInputs:
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
    cases = {
        path.stem: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(case_dir.glob("*.json"))
    }
    if not cases:
        raise ReleaseInputError("candidate source cases are missing")
    return ReleaseInputs(
        commit=commit,
        librarian_skill_sha256=hashlib.sha256(skill_path.read_bytes()).hexdigest(),
        source_cases=cases,
    )


def evaluate(artifact: dict, *, expected: ReleaseInputs | None = None) -> dict:
    """Validate provenance, raw results, and the lowest-passing reasoning decision."""
    expected = expected or current_release_inputs(ROOT)
    failures = []
    corpus = _sha(artifact.get("corpus_sha256"), "corpus_sha256", failures)
    initial = _ref(artifact.get("initial_graph_ref"), "initial_graph_ref", failures)
    commit = _ref(artifact.get("stigmergy_commit"), "stigmergy_commit", failures)
    skill = _sha(artifact.get("librarian_skill_sha256"), "librarian_skill_sha256", failures)
    case_hashes = _cases(artifact.get("source_cases"), failures)
    if commit != expected.commit:
        failures.append({"implementation": "artifact", "reason": "candidate-commit"})
    if skill != expected.librarian_skill_sha256:
        failures.append({"implementation": "artifact", "reason": "candidate-skill"})
    if case_hashes != expected.source_cases:
        failures.append({"implementation": "artifact", "reason": "candidate-cases"})
    found = _runs(artifact.get("runs"), failures)
    missing = sorted(REQUIRED_IMPLEMENTATIONS - set(found))
    for implementation, item in found.items():
        runtime = item.get("runtime")
        if not isinstance(runtime, dict) or not set(runtime) >= REQUIRED_RUNTIME_FIELDS or not all(
            _recorded_text(runtime.get(field)) for field in REQUIRED_RUNTIME_FIELDS
        ):
            failures.append({"implementation": implementation, "reason": "runtime-metadata"})
            continue
        if implementation == "stigmergy" and any(
            runtime.get(field) != value for field, value in STIGMERGY_RUNTIME.items()
        ):
            failures.append({"implementation": implementation, "reason": "runtime-route"})
        if not _passing_raw_gates(item.get("raw_gates")):
            failures.append({"implementation": implementation, "reason": "raw-gates"})
        if not isinstance(item.get("score"), dict) or "passed" not in item["score"]:
            failures.append({"implementation": implementation, "reason": "semantic-score"})
        if not bool((item.get("score") or {}).get("passed")):
            failures.append({"implementation": implementation, "reason": "semantic-score"})
        if not bool((item.get("gates") or {}).get("passed")):
            failures.append({"implementation": implementation, "reason": "writer-gates"})
        _run_provenance(
            implementation,
            item,
            corpus,
            initial,
            expected.commit,
            expected.librarian_skill_sha256,
            expected.source_cases,
            failures,
        )
    _blind_review(
        artifact.get("blind_editorial_review"), corpus, initial, case_hashes, found, failures
    )
    _reasoning_matrix(
        artifact.get("reasoning_matrix"),
        found.get("stigmergy"),
        corpus,
        initial,
        expected,
        failures,
    )
    return {
        "passed": not missing and not failures,
        "missing_implementations": missing,
        "failures": failures,
        "corpus_sha256": corpus,
        "initial_graph_ref": initial,
    }


def _sha(value, field: str, failures: list[dict]) -> str | None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value) or len(set(value)) == 1:
        failures.append({"implementation": "artifact", "reason": field})
        return None
    return value


def _ref(value, field: str, failures: list[dict]) -> str | None:
    if not isinstance(value, str) or not _GIT_REF.fullmatch(value) or len(set(value)) == 1:
        failures.append({"implementation": "artifact", "reason": field})
        return None
    return value


def _cases(value, failures: list[dict]) -> dict[str, str]:
    if not isinstance(value, list) or not value:
        failures.append({"implementation": "artifact", "reason": "source-cases"})
        return {}
    result = {}
    for item in value:
        if not isinstance(item, dict) or not _recorded_text(item.get("id")):
            failures.append({"implementation": "artifact", "reason": "source-case-id"})
            continue
        digest = _sha(item.get("sha256"), "source-case-sha256", failures)
        if item["id"] in result:
            failures.append({"implementation": "artifact", "reason": "duplicate-source-case"})
        elif digest:
            result[item["id"]] = digest
    return result


def _runs(value, failures: list[dict]) -> dict[str, dict]:
    if not isinstance(value, list):
        failures.append({"implementation": "artifact", "reason": "runs"})
        return {}
    found = {}
    run_ids = set()
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("implementation"), str):
            failures.append({"implementation": "artifact", "reason": "run-shape"})
            continue
        implementation = item["implementation"]
        run_id = item.get("run_id")
        if not _recorded_text(run_id):
            failures.append({"implementation": implementation, "reason": "run-id"})
        elif run_id in run_ids:
            failures.append({"implementation": implementation, "reason": "duplicate-run-id"})
        else:
            run_ids.add(run_id)
        if implementation in found:
            failures.append({"implementation": implementation, "reason": "duplicate-run"})
        else:
            found[implementation] = item
    return found


def _recorded_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "<" not in value and ">" not in value


def _passing_raw_gates(value) -> bool:
    return _complete_raw_gates(value) and all(_gate_passed(result) for result in value.values())


def _complete_raw_gates(value) -> bool:
    return isinstance(value, dict) and set(value) == REQUIRED_SEMANTIC_GATES


def _gate_passed(value) -> bool:
    return value is True or value == "passed"


def _run_provenance(implementation, item, corpus, initial, commit, skill, case_hashes, failures) -> None:
    provenance = item.get("provenance")
    if not isinstance(provenance, dict):
        failures.append({"implementation": implementation, "reason": "run-provenance"})
        return
    if provenance.get("corpus_sha256") != corpus or provenance.get("initial_graph_ref") != initial:
        failures.append({"implementation": implementation, "reason": "stale-or-mismatched-corpus"})
    if provenance.get("source_cases") != case_hashes:
        failures.append({"implementation": implementation, "reason": "stale-or-mismatched-cases"})
    if implementation == "stigmergy":
        if provenance.get("commit") != commit:
            failures.append({"implementation": implementation, "reason": "stale-or-mismatched-code"})
        if provenance.get("librarian_skill_sha256") != skill:
            failures.append({"implementation": implementation, "reason": "stale-or-mismatched-skill"})


def _blind_review(value, corpus, initial, case_hashes, runs, failures: list[dict]) -> None:
    if not isinstance(value, dict) or value.get("verdict") != "no_material_stigmergy_regression":
        failures.append({"implementation": "review", "reason": "blind-editorial-review"})
        return
    provenance = value.get("provenance")
    if not isinstance(provenance, dict) or not all(
        _recorded_text(provenance.get(field))
        for field in ("reviewer", "method", "artifact_ref")
    ):
        failures.append({"implementation": "review", "reason": "blind-review-provenance"})
        return
    expected_run_ids = {
        implementation: item.get("run_id") for implementation, item in runs.items()
    }
    if (
        provenance.get("corpus_sha256") != corpus
        or provenance.get("initial_graph_ref") != initial
        or provenance.get("source_cases") != case_hashes
        or provenance.get("runs") != expected_run_ids
    ):
        failures.append({"implementation": "review", "reason": "stale-or-mismatched-review"})


def _reasoning_matrix(
    matrix,
    run,
    corpus: str | None,
    initial: str | None,
    expected: ReleaseInputs,
    failures: list[dict],
) -> None:
    if not isinstance(run, dict) or not isinstance(matrix, list) or not matrix:
        failures.append({"implementation": "stigmergy", "reason": "reasoning-matrix"})
        return
    selected = (run.get("runtime") or {}).get("reasoning_level")
    values = {}
    for item in matrix:
        if not isinstance(item, dict) or item.get("reasoning_level") not in _LEVELS:
            failures.append({"implementation": "stigmergy", "reason": "reasoning-matrix"})
            return
        level = item["reasoning_level"]
        if level in values:
            failures.append({"implementation": "stigmergy", "reason": "duplicate-reasoning-level"})
            return
        runtime = item.get("runtime")
        if not isinstance(runtime, dict) or runtime != {**STIGMERGY_RUNTIME, "reasoning_level": level}:
            failures.append({"implementation": "stigmergy", "reason": "reasoning-matrix-runtime"})
            return
        if not _complete_raw_gates(item.get("raw_gates")):
            failures.append({"implementation": "stigmergy", "reason": "reasoning-matrix-gates"})
            return
        provenance = item.get("provenance")
        if not isinstance(provenance, dict) or (
            provenance.get("corpus_sha256") != corpus
            or provenance.get("initial_graph_ref") != initial
            or provenance.get("source_cases") != expected.source_cases
            or provenance.get("commit") != expected.commit
            or provenance.get("librarian_skill_sha256") != expected.librarian_skill_sha256
        ):
            failures.append({"implementation": "stigmergy", "reason": "reasoning-matrix-provenance"})
            return
        score_passed = bool((item.get("score") or {}).get("passed"))
        writer_passed = bool((item.get("gates") or {}).get("passed"))
        observed = score_passed and writer_passed and all(
            _gate_passed(result) for result in item["raw_gates"].values()
        )
        if not isinstance(item.get("passed"), bool) or item["passed"] != observed:
            failures.append({"implementation": "stigmergy", "reason": "reasoning-matrix-result"})
            return
        values[level] = observed
    if selected not in values or not values[selected]:
        failures.append({"implementation": "stigmergy", "reason": "selected-reasoning-not-passing"})
        return
    selected_index = _LEVELS.index(selected)
    if any(values.get(level) for level in _LEVELS[:selected_index]):
        failures.append({"implementation": "stigmergy", "reason": "reasoning-not-lowest-passing"})
    if any(level not in values for level in _LEVELS[:selected_index]):
        failures.append({"implementation": "stigmergy", "reason": "reasoning-matrix-incomplete"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, help="JSON artifact produced by the real-model parity run")
    parser.add_argument(
        "--repo-root",
        default=str(ROOT),
        help="candidate repository root whose commit, skill, and cases must match the artifact",
    )
    args = parser.parse_args(argv)
    try:
        artifact = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
        result = evaluate(artifact, expected=current_release_inputs(Path(args.repo_root)))
    except (OSError, ReleaseInputError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
