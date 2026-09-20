from pathlib import Path

from evals.filing.parity import ARTIFACT_SCHEMA_VERSION, current_release_inputs, evaluate

ROOT = Path(__file__).resolve().parents[2]


def test_release_inputs_bind_commit_skill_cases_and_fixtures():
    inputs = current_release_inputs(ROOT)

    assert len(inputs.commit) == 40
    assert len(inputs.librarian_skill_sha256) == 64
    assert inputs.source_cases
    assert inputs.source_cases.keys() == inputs.source_fixtures.keys()


def test_parity_fails_closed_on_missing_evidence():
    result = evaluate({"schema_version": ARTIFACT_SCHEMA_VERSION})

    assert result["passed"] is False
    assert result["failures"]
