"""The single CLEAN_LLM dispatch (llm.build_processor): fake selection, flawed flag, fail-fast."""
import pytest
from pydantic import BaseModel

from stigmergy.kernel.llm import build_processor


class _Output(BaseModel):
    """A stand-in for whatever a caller's own structured output happens to be — this module's
    subject is the fake/real DISPATCH, not any particular schema. (It used to be the ingest
    pipeline's `ProcessorOutput`, which went with the pipeline.)"""
    reason: str = ""


class _Fake:
    def __init__(self, flawed):
        self.flawed = flawed


def test_fake_backend_returns_the_callers_fake(monkeypatch):
    monkeypatch.setenv("CLEAN_LLM", "fake")
    p = build_processor(_Output, "sys", fake=lambda flawed: _Fake(flawed))
    assert isinstance(p, _Fake) and p.flawed is False


def test_fake_flawed_backend_sets_the_flag(monkeypatch):
    monkeypatch.setenv("CLEAN_LLM", "fake-flawed")
    p = build_processor(_Output, "sys", fake=lambda flawed: _Fake(flawed))
    assert isinstance(p, _Fake) and p.flawed is True


def test_unknown_backend_fails_fast_before_any_construction(monkeypatch):
    """A CLEAN_LLM typo must raise (settings.resolve_backend) — never fall through to the real
    OpenAI path, and never touch the caller's fake either."""
    monkeypatch.setenv("CLEAN_LLM", "fakee")
    with pytest.raises(RuntimeError, match="invalid CLEAN_LLM"):
        build_processor(_Output, "sys", fake=lambda flawed: _Fake(flawed))


def test_tools_hook_not_called_on_the_fake_path(monkeypatch):
    monkeypatch.setenv("CLEAN_LLM", "fake")
    calls = []
    build_processor(_Output, "sys", fake=lambda flawed: _Fake(flawed),
                    tools=lambda agent: calls.append(agent))
    assert calls == []


def test_openai_path_builds_a_real_agent_and_runs_tools_hook(monkeypatch):
    monkeypatch.setenv("CLEAN_LLM", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    seen = []
    agent = build_processor(_Output, "sys", fake=lambda flawed: _Fake(flawed),
                            tools=seen.append)
    assert seen == [agent]
