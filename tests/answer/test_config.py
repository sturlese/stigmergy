import argparse

import pytest
from pydantic_ai import Agent

from stigmergy.answer.synthesize import FakeSynthesizer, build_synthesizer
from stigmergy.kernel.llm import ANSWER_MODEL, OPENROUTER_PROVIDER_POLICY
from stigmergy.server.settings import Settings


def test_default_answer_model_is_glm_5_2():
    assert Settings().model == ANSWER_MODEL
    assert Settings().llm == "openrouter"


def test_fake_backend_is_keyless(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert isinstance(build_synthesizer(Settings(llm="fake")), FakeSynthesizer)


def test_invalid_answer_llm_fails_fast():
    with pytest.raises(RuntimeError, match="invalid ANSWER_LLM"):
        build_synthesizer(Settings(llm="anthropic"))


def test_openrouter_without_key_is_a_clean_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        build_synthesizer(Settings())


def test_answer_agent_uses_glm_with_the_mandatory_provider_policy(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    agent = build_synthesizer(Settings())

    assert isinstance(agent, Agent)
    assert agent.model.model_name == "z-ai/glm-5.2"
    assert agent.model.settings["openrouter_provider"] == OPENROUTER_PROVIDER_POLICY


def test_unapproved_answer_model_is_rejected(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    with pytest.raises(RuntimeError, match="answer model"):
        build_synthesizer(Settings(model="openrouter:anthropic/claude-sonnet-5"))


def test_other_approved_model_is_rejected_for_answers(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    with pytest.raises(RuntimeError, match="answer model"):
        build_synthesizer(Settings(model="openrouter:deepseek/deepseek-v4-flash"))


def test_answer_llm_env_fallback(monkeypatch):
    monkeypatch.setenv("ANSWER_LLM", "fake")
    monkeypatch.setenv("ANSWER_MODEL", ANSWER_MODEL)
    args = argparse.Namespace(
        identity=None,
        identities=None,
        repo=None,
        entity_registry=None,
        dsn=None,
        embedder=None,
        answer_llm=None,
    )

    settings = Settings.from_args(args)

    assert settings.llm == "fake"
    assert settings.model == ANSWER_MODEL
