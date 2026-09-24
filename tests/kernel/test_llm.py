import pytest
from pydantic_ai.models.openrouter import OpenRouterModel

from stigmergy.kernel import llm


def test_runtime_model_contract_contains_no_premium_recovery_model():
    assert llm.ANSWER_MODEL == "openrouter:deepseek/deepseek-v4.1-flash"
    assert llm.LIBRARIAN_MODEL == "openrouter:deepseek/deepseek-v4.1-flash"
    assert llm.OCR_MODEL == "openrouter:qwen/qwen3-vl-8b-instruct"
    assert frozenset(
        {llm.ANSWER_MODEL, llm.LIBRARIAN_MODEL, llm.OCR_MODEL}
    ) == llm.APPROVED_MODELS
    assert all("gpt-5.4" not in model for model in llm.APPROVED_MODELS)


def test_every_approved_model_has_the_mandatory_privacy_policy(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    for configured in llm.APPROVED_MODELS:
        model, settings = llm.build_model(configured)
        assert isinstance(model, OpenRouterModel)
        assert settings is model.settings
        assert {
            "require_parameters": True,
            "data_collection": "deny",
            "zdr": True,
        }.items() <= settings["openrouter_provider"].items()


def test_librarian_prefers_the_fastest_compatible_private_provider():
    assert llm.provider_policy(llm.LIBRARIAN_MODEL) == {
        "allow_fallbacks": True,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
        "sort": "throughput",
    }


def test_ocr_keeps_same_model_provider_failover_without_librarian_settings(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    model, _ = llm.build_model(llm.OCR_MODEL)
    assert model.settings["openrouter_provider"] == llm.OPENROUTER_PROVIDER_POLICY
    assert "openrouter_reasoning" not in model.settings
    assert "max_tokens" not in model.settings


def test_answers_share_the_librarian_model_settings(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    _answer_model, answer_settings = llm.build_model(llm.ANSWER_MODEL)
    _librarian_model, librarian_settings = llm.build_model(llm.LIBRARIAN_MODEL)

    assert answer_settings == librarian_settings
    assert answer_settings["openrouter_provider"] == llm.provider_policy(llm.LIBRARIAN_MODEL)


def test_librarian_requests_medium_reasoning_and_native_deterministic_output(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    model, settings = llm.build_model(llm.LIBRARIAN_MODEL)

    assert settings is model.settings
    assert settings["max_tokens"] == llm.LIBRARIAN_MAX_TOKENS
    assert settings["temperature"] == 0
    assert settings["openrouter_reasoning"] == {
        "effort": "medium",
        "exclude": True,
    }
    assert model.profile["supports_json_schema_output"] is True
    assert model.profile["supports_json_object_output"] is True


def test_build_model_requires_openrouter_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        llm.build_model(llm.LIBRARIAN_MODEL)


def test_unapproved_model_is_rejected_before_credentials(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    with pytest.raises(RuntimeError, match="not approved"):
        llm.build_model("openrouter:openai/gpt-5.4")
