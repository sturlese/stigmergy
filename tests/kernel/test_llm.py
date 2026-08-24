import asyncio
import json

import httpx
import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.models.test import TestModel

from stigmergy.kernel import llm


def test_runtime_model_contract_is_exact():
    assert llm.ANSWER_MODEL == "openrouter:z-ai/glm-5.2"
    assert llm.LIBRARIAN_MODEL == "openrouter:deepseek/deepseek-v4-flash"
    assert llm.OCR_MODEL == "openrouter:qwen/qwen3-vl-8b-instruct"
    assert {
        llm.ANSWER_MODEL,
        llm.LIBRARIAN_MODEL,
        llm.OCR_MODEL,
    } == llm.APPROVED_MODELS


def test_every_approved_model_has_the_mandatory_provider_policy(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    for configured in llm.APPROVED_MODELS:
        model, settings = llm.build_model(configured)
        assert isinstance(model, OpenRouterModel)
        assert model.model_name == configured.removeprefix("openrouter:")
        assert settings is model.settings
        assert model.settings["openrouter_provider"] == llm.OPENROUTER_PROVIDER_POLICY


def test_approved_models_enable_provider_failover_without_relaxing_privacy(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    model, _ = llm.build_model(llm.LIBRARIAN_MODEL)

    assert model.model_name == "deepseek/deepseek-v4-flash"
    assert model.settings["openrouter_provider"] == {
        "allow_fallbacks": True,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
    }


def test_openrouter_provider_policy_survives_two_real_adapter_requests(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "test-completion",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek/deepseek-v4-flash",
            "provider": "deepseek",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    async def run_twice():
        model, model_settings = llm.build_model(llm.LIBRARIAN_MODEL)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        model.provider._set_http_client(client)
        request = ModelRequest(parts=[UserPromptPart(content="hello")])
        try:
            await model.request([request], model_settings, ModelRequestParameters())
            await model.request([request], model_settings, ModelRequestParameters())
        finally:
            await client.aclose()
        return model

    model = asyncio.run(run_twice())

    assert [payload["provider"] for payload in payloads] == [
        llm.OPENROUTER_PROVIDER_POLICY,
        llm.OPENROUTER_PROVIDER_POLICY,
    ]
    assert model.settings["openrouter_provider"] == llm.OPENROUTER_PROVIDER_POLICY


@pytest.mark.parametrize(
    "model",
    [
        "anthropic:claude-sonnet-5",
        "openrouter:anthropic/claude-sonnet-5",
        "gpt-5.6-terra",
        "openai:gpt-5.6-terra",
        "google-gla:gemini-3-flash",
    ],
)
def test_unapproved_providers_and_models_are_rejected(monkeypatch, model):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    with pytest.raises(RuntimeError, match="not approved"):
        llm.build_model(model)


def test_approved_models_require_only_the_openrouter_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-used")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-be-used")

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        llm.build_model(llm.ANSWER_MODEL)


def test_model_override_is_scoped_and_keyless(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    test_model = TestModel()

    with llm.model_override(test_model):
        assert llm.build_model(llm.ANSWER_MODEL) == (test_model, None)

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        llm.build_model(llm.ANSWER_MODEL)
