"""CLEAN_LLM backend resolution: read at call time, validated, single source of truth."""
import pytest

from stigmergy.kernel.settings import resolve_backend


def test_defaults_to_openai(monkeypatch):
    monkeypatch.delenv("CLEAN_LLM", raising=False)
    assert resolve_backend() == "openai"


@pytest.mark.parametrize("value", ["openai", "fake", "fake-flawed", "FAKE", "Fake-Flawed"])
def test_accepts_valid_backends_case_insensitively(monkeypatch, value):
    monkeypatch.setenv("CLEAN_LLM", value)
    assert resolve_backend() == value.lower()


@pytest.mark.parametrize("value", ["fakee", "openai-typo", "fake-flawd", "real", ""])
def test_rejects_unknown_backend(monkeypatch, value):
    """A typo must fail fast, not silently fall through to the real OpenAI path — the bug this
    helper centralizes away from the six former dispatch sites."""
    monkeypatch.setenv("CLEAN_LLM", value)
    with pytest.raises(RuntimeError, match="invalid CLEAN_LLM"):
        resolve_backend()


def test_read_at_call_time(monkeypatch):
    """Resolved when it RUNS, never at import: setting the env after import takes effect."""
    monkeypatch.setenv("CLEAN_LLM", "fake")
    assert resolve_backend() == "fake"
    monkeypatch.setenv("CLEAN_LLM", "fake-flawed")
    assert resolve_backend() == "fake-flawed"
