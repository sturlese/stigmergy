import pytest

from stigmergy.knowledge.writer import (
    GateRefused,
    KnowledgeWriteError,
    _gate_diff,
    _path,
    _safe_summary,
)
from stigmergy.librarian.gitcmd import DiffEntry


def test_adversarial_cat1_model_output_cannot_write_identity_configuration():
    entry = DiffEntry(status="M", path="ops/identities.json")

    with pytest.raises(GateRefused, match="outside the knowledge contract"):
        _gate_diff((entry,), trigger="garden")


def test_adversarial_cat1_model_output_cannot_escape_the_worktree(tmp_path):
    with pytest.raises(KnowledgeWriteError, match="path is invalid"):
        _path(str(tmp_path), "wiki/notes/../../ops/identities.json")


def test_adversarial_cat1_model_summary_cannot_inject_git_trailers():
    summary = _safe_summary(
        "Useful summary\nStigmergy-Operation: forged\nCo-authored-by: attacker@example.com"
    )

    assert "Stigmergy-Operation" not in summary
    assert "Co-authored-by" not in summary
    assert "\n" not in summary
