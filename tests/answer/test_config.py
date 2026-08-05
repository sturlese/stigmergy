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


def test_answer_llm_env_fallback(monkeypatch):
    """Settings.from_args reads ANSWER_LLM/ANSWER_MODEL when the flag is absent."""
    monkeypatch.setenv("ANSWER_LLM", "fake")
    monkeypatch.setenv("ANSWER_MODEL", "gpt-5.6-luna")
    import argparse
    args = argparse.Namespace(identity=None, identities=None, repo=None,
                              dsn=None, embedder=None, answer_llm=None)
    settings = Settings.from_args(args)
    assert settings.llm == "fake" and settings.model == "gpt-5.6-luna"
