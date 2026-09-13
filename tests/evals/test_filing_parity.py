from pathlib import Path

from evals.filing.parity import (
    REQUIRED_SEMANTIC_GATES,
    current_release_inputs,
    evaluate,
)

EXPECTED = current_release_inputs(Path(__file__).resolve().parents[2])


def _raw_gates(*, passed=True):
    return {gate: "passed" if passed else "failed" for gate in REQUIRED_SEMANTIC_GATES}


def _matrix_item(level, *, passed):
    raw = _raw_gates(passed=passed)
    if not passed:
        raw["bodies"] = "failed"
    return {
        "reasoning_level": level,
        "runtime": {
            "model": "openai/gpt-oss-120b",
            "reasoning_level": level,
            "provider": "cerebras",
        },
        "score": {"passed": passed},
        "gates": {"passed": passed},
        "raw_gates": raw,
        "provenance": {
            "corpus_sha256": "0123456789abcdef" * 4,
            "initial_graph_ref": "0123456789abcdef0123456789abcdef01234567",
            "source_cases": EXPECTED.source_cases,
            "commit": EXPECTED.commit,
            "librarian_skill_sha256": EXPECTED.librarian_skill_sha256,
        },
        "passed": passed,
    }


def _artifact():
    corpus = "0123456789abcdef" * 4
    initial = "0123456789abcdef0123456789abcdef01234567"
    commit = EXPECTED.commit
    skill = EXPECTED.librarian_skill_sha256
    cases = EXPECTED.source_cases
    return {
        "corpus_sha256": corpus,
        "initial_graph_ref": initial,
        "stigmergy_commit": commit,
        "librarian_skill_sha256": skill,
        "source_cases": [{"id": key, "sha256": value} for key, value in cases.items()],
        "runs": [
            {
                "implementation": "hippocampus",
                "run_id": "hippocampus-fixture-run",
                "runtime": {"model": "fixture", "reasoning_level": "high", "provider": "fixture"},
                "score": {"passed": True},
                "gates": {"passed": True},
                "raw_gates": _raw_gates(),
                "provenance": {
                    "corpus_sha256": corpus, "initial_graph_ref": initial, "source_cases": cases,
                },
            },
            {
                "implementation": "stigmergy",
                "run_id": "stigmergy-fixture-run",
                "runtime": {"model": "openai/gpt-oss-120b", "reasoning_level": "high", "provider": "cerebras"},
                "score": {"passed": True},
                "gates": {"passed": True},
                "raw_gates": _raw_gates(),
                "provenance": {
                    "corpus_sha256": corpus, "initial_graph_ref": initial, "source_cases": cases,
                    "commit": commit, "librarian_skill_sha256": skill,
                },
            },
        ],
        "reasoning_matrix": [
            _matrix_item("minimal", passed=False),
            _matrix_item("low", passed=False),
            _matrix_item("medium", passed=False),
            _matrix_item("high", passed=True),
        ],
        "blind_editorial_review": {
            "verdict": "no_material_stigmergy_regression",
            "provenance": {
                "reviewer": "fixture",
                "method": "blind-pairwise",
                "artifact_ref": "fixture",
                "corpus_sha256": corpus,
                "initial_graph_ref": initial,
                "source_cases": cases,
                "runs": {
                    "hippocampus": "hippocampus-fixture-run",
                    "stigmergy": "stigmergy-fixture-run",
                },
            },
        },
    }


def test_parity_gate_requires_two_real_runs_runtime_metadata_and_blind_review():
    assert evaluate(_artifact(), expected=EXPECTED)["passed"] is True

    incomplete = _artifact()
    incomplete["runs"][1]["runtime"].pop("reasoning_level")
    incomplete["blind_editorial_review"] = {"verdict": "pending"}

    result = evaluate(incomplete, expected=EXPECTED)

    assert result["passed"] is False
    assert {item["reason"] for item in result["failures"]} == {
        "runtime-metadata",
        "blind-editorial-review",
        "selected-reasoning-not-passing",
    }


def test_parity_gate_rejects_stale_provenance_duplicate_runs_and_a_nonminimal_reasoning_choice():
    artifact = _artifact()
    artifact["runs"].append(dict(artifact["runs"][0]))
    artifact["runs"][1]["provenance"]["librarian_skill_sha256"] = "0" * 64
    artifact["reasoning_matrix"][1] = _matrix_item("low", passed=True)

    result = evaluate(artifact, expected=EXPECTED)

    assert result["passed"] is False
    assert {item["reason"] for item in result["failures"]} >= {
        "duplicate-run", "stale-or-mismatched-skill", "reasoning-not-lowest-passing",
    }


def test_parity_gate_rejects_unrecorded_run_identity_and_nonpassing_raw_gates():
    artifact = _artifact()
    artifact["runs"][1]["run_id"] = "<replace-me>"
    artifact["runs"][1]["raw_gates"] = {"writer": "failed"}

    result = evaluate(artifact, expected=EXPECTED)

    assert result["passed"] is False
    assert {item["reason"] for item in result["failures"]} >= {"run-id", "raw-gates"}


def test_parity_gate_rejects_unknown_or_missing_semantic_gates_and_wrong_runtime_route():
    artifact = _artifact()
    artifact["runs"][1]["raw_gates"].pop("writer")
    artifact["runs"][1]["raw_gates"]["invented"] = "passed"
    artifact["runs"][1]["runtime"]["provider"] = "other"

    result = evaluate(artifact, expected=EXPECTED)

    assert result["passed"] is False
    assert {item["reason"] for item in result["failures"]} >= {"raw-gates", "runtime-route"}


def test_parity_gate_rejects_artifact_hashes_that_do_not_match_current_candidate_inputs():
    artifact = _artifact()
    artifact["stigmergy_commit"] = "f" * 40
    artifact["librarian_skill_sha256"] = "e" * 64
    artifact["source_cases"][0]["sha256"] = "d" * 64

    result = evaluate(artifact, expected=EXPECTED)

    assert result["passed"] is False
    assert {item["reason"] for item in result["failures"]} >= {
        "candidate-commit", "candidate-skill", "candidate-cases",
    }


def test_reasoning_matrix_requires_complete_gates_and_candidate_bound_provenance():
    artifact = _artifact()
    artifact["reasoning_matrix"][0]["raw_gates"].pop("writer")

    result = evaluate(artifact, expected=EXPECTED)

    assert result["passed"] is False
    assert {item["reason"] for item in result["failures"]} >= {"reasoning-matrix-gates"}

    artifact = _artifact()
    artifact["reasoning_matrix"][1]["provenance"]["commit"] = "a" * 40
    result = evaluate(artifact, expected=EXPECTED)

    assert result["passed"] is False
    assert {item["reason"] for item in result["failures"]} >= {"reasoning-matrix-provenance"}
