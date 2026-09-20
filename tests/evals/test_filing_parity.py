import json
from pathlib import Path

from evals.filing.parity import ARTIFACT_SCHEMA_VERSION, current_release_inputs, evaluate

ROOT = Path(__file__).resolve().parents[2]


def test_release_inputs_bind_commit_skill_cases_and_fixtures():
    inputs = current_release_inputs(ROOT)

    assert len(inputs.commit) == 40
    assert len(inputs.librarian_skill_sha256) == 64
    assert len(inputs.initial_graph_manifest_sha256) == 64
    assert inputs.source_cases
    assert inputs.source_cases.keys() == inputs.source_fixtures.keys()


def test_parity_fails_closed_on_missing_evidence():
    result = evaluate({"schema_version": ARTIFACT_SCHEMA_VERSION})

    assert result["passed"] is False
    assert result["failures"]


def test_v6_example_excludes_staged_planner_fields():
    artifact = json.loads((ROOT / "evals" / "filing" / "parity-artifact.example.json").read_text(encoding="utf-8"))
    forbidden = {
        "graph_shape",
        "graph_shape_draft",
        "graph_shape_review",
        "graph_shape_violations",
        "reviewed_plan",
        "repair_model_requests",
        "compilation_model_requests",
        "expected_graph_mutations",
        "topology",
        "coverage_audit",
        "evidence_limits",
    }

    def keys(value):
        if isinstance(value, dict):
            yield from value
            for child in value.values():
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    assert artifact["schema_version"] == ARTIFACT_SCHEMA_VERSION
    assert forbidden.isdisjoint(keys(artifact))
