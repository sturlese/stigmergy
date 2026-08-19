"""Model policy: default gpt-5.6-terra; ANSWER_LLM ∈ {openai, fake}; an invalid
value fails fast; the fake path is keyless; a missing key with openai gives a clean error.

Pure and keyless — build_synthesizer is exercised directly (no service, no Postgres).
"""
import pytest

from stigmergy.answer.synthesize import FakeSynthesizer, build_synthesizer
from stigmergy.server.settings import Settings


def test_default_model_is_terra():
    assert Settings().model == "gpt-5.6-terra"
    assert Settings().llm == "openai"


def test_fake_backend_is_keyless(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert isinstance(build_synthesizer(Settings(llm="fake")), FakeSynthesizer)


def test_invalid_answer_llm_fails_fast():
    """A typo must raise — never silently pick the fake nor fall through to the real path."""
    with pytest.raises(RuntimeError, match="invalid ANSWER_LLM"):
        build_synthesizer(Settings(llm="fakee"))


def test_openai_without_key_is_a_clean_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is required"):
        build_synthesizer(Settings(llm="openai", model="gpt-5.6-terra"))


def test_a_provider_prefixed_model_builds_without_the_openai_key(monkeypatch):
    """The two-form convention CLEAN_MODEL and the librarian's model already follow, applied to
    ANSWER_MODEL: a provider-prefixed id is resolved by pydantic-ai, whose provider reads its OWN
    env key — OPENAI_API_KEY stays the bare-name Responses path's credential and nothing else's.
    OLD BEHAVIOUR: every ANSWER_LLM=openai model was built as an OpenAI Responses model, so `ask`
    could not run on any provider the Responses API cannot name."""
    from pydantic_ai import Agent

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-fake")

    agent = build_synthesizer(Settings(llm="openai", model="openrouter:z-ai/glm-5.2"))

    assert isinstance(agent, Agent)
    assert agent.model.model_name == "z-ai/glm-5.2"


def test_a_prefixed_model_missing_its_own_key_is_refused_naming_that_key(monkeypatch):
    """The benign twin of the keyless build above, from the other side: the refusal a
    misconfigured deployment gets names the PROVIDER'S variable, never OPENAI_API_KEY — an
    operator sent to export the wrong key would export it and still be broken."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")   # present, and must not be the ask
    with pytest.raises(Exception, match="OPENROUTER_API_KEY"):
        build_synthesizer(Settings(llm="openai", model="openrouter:z-ai/glm-5.2"))


def test_answer_llm_env_fallback(monkeypatch):
    """Settings.from_args reads ANSWER_LLM/ANSWER_MODEL when the flag is absent."""
    monkeypatch.setenv("ANSWER_LLM", "fake")
    monkeypatch.setenv("ANSWER_MODEL", "gpt-5.6-luna")
    import argparse
    args = argparse.Namespace(identity=None, identities=None, repo=None,
                              dsn=None, embedder=None, answer_llm=None)
    settings = Settings.from_args(args)
    assert settings.llm == "fake" and settings.model == "gpt-5.6-luna"
