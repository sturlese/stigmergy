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


def test_model_override_reaches_the_real_agent_with_no_key_and_no_private_reaching(monkeypatch):
    """The public seam issue #81 asked for: any package proves a tool-loop property against the
    REAL Agent — pydantic-ai's own enforcement — by installing an explicit model object, with no
    API key in the environment and no reaching into this module's private functions. Before this,
    a suite needed `monkeypatch.setattr(kernel_llm, "build_model", ...)`, a cross-package reach
    into a private-by-convention seam."""
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from stigmergy.kernel import llm

    monkeypatch.setenv("CLEAN_LLM", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with llm.model_override(TestModel(custom_output_args={"reason": "seam"})):
        agent = build_processor(_Output, "instructions", fake=_Fake)
        assert isinstance(agent, Agent)
        assert agent.run_sync("hi").output.reason == "seam"

    # The override dies with the block: outside it, the openai path wants its key again.
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        build_processor(_Output, "instructions", fake=_Fake)


def test_model_override_is_inert_on_the_fake_path(monkeypatch):
    """The benign twin: `CLEAN_LLM=fake` still returns the caller's fake — the override replaces
    the PROVIDER, never the dispatch, so an offline suite that happens to run inside the block is
    unchanged."""
    from pydantic_ai.models.test import TestModel

    from stigmergy.kernel import llm

    monkeypatch.setenv("CLEAN_LLM", "fake")
    with llm.model_override(TestModel()):
        assert isinstance(build_processor(_Output, "instructions", fake=_Fake), _Fake)
