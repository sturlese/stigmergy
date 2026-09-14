import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.models.test import TestModel

from stigmergy.kernel import llm
from stigmergy.knowledge import planner
from stigmergy.knowledge.plan import FilingPlan


def test_runtime_model_contract_is_exact():
    assert llm.ANSWER_MODEL == "openrouter:z-ai/glm-5.2"
    assert llm.LIBRARIAN_MODEL == "openrouter:openai/gpt-oss-120b"
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
        assert model.settings["openrouter_provider"] == llm.provider_policy(configured)
        assert {
            "require_parameters": True,
            "data_collection": "deny",
            "zdr": True,
        }.items() <= model.settings["openrouter_provider"].items()


def test_non_librarian_models_enable_provider_failover_without_relaxing_privacy(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    for configured in (llm.ANSWER_MODEL, llm.OCR_MODEL):
        model, _ = llm.build_model(configured)
        assert model.settings["openrouter_provider"] == llm.OPENROUTER_PROVIDER_POLICY


def test_librarian_is_pinned_to_cerebras_for_native_structured_output():
    assert llm.provider_policy(llm.LIBRARIAN_MODEL) == {
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
        "only": ["cerebras"],
    }


def test_only_the_librarian_model_is_pinned_to_the_verified_host(monkeypatch):
    """Only the librarian needs a verified host for structured plans."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    librarian, _ = llm.build_model(llm.LIBRARIAN_MODEL)
    assert librarian.settings["openrouter_provider"]["only"] == ["cerebras"]
    assert librarian.settings["openrouter_provider"]["allow_fallbacks"] is False

    for configured in (llm.ANSWER_MODEL, llm.OCR_MODEL):
        model, _ = llm.build_model(configured)
        assert "only" not in model.settings["openrouter_provider"]
        assert model.settings["openrouter_provider"]["allow_fallbacks"] is True


def test_librarian_requests_high_reasoning_without_returning_reasoning(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    model, settings = llm.build_model(llm.LIBRARIAN_MODEL)

    assert settings is model.settings
    assert llm.LIBRARIAN_REASONING_LEVEL == "high"
    assert llm.LIBRARIAN_MAX_TOKENS == 32768
    assert model.settings["max_tokens"] == llm.LIBRARIAN_MAX_TOKENS
    assert model.settings["openrouter_reasoning"] == {
        "effort": llm.LIBRARIAN_REASONING_LEVEL,
        "exclude": True,
    }


def test_answer_and_ocr_do_not_inherit_librarian_reasoning(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    for configured in (llm.ANSWER_MODEL, llm.OCR_MODEL):
        model, _ = llm.build_model(configured)
        assert "max_tokens" not in model.settings
        assert "openrouter_reasoning" not in model.settings


def test_openrouter_provider_policy_survives_two_real_adapter_requests(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "test-completion",
            "object": "chat.completion",
            "created": 0,
            "model": "openai/gpt-oss-120b",
            "provider": "cerebras",
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

    expected = llm.provider_policy(llm.LIBRARIAN_MODEL)
    assert [payload["model"] for payload in payloads] == ["openai/gpt-oss-120b"] * 2
    assert [payload["provider"] for payload in payloads] == [expected, expected]
    assert [payload["max_tokens"] for payload in payloads] == [32768, 32768]
    assert all("max_completion_tokens" not in payload for payload in payloads)
    assert model.settings["openrouter_provider"] == expected


@pytest.mark.parametrize("configured", (llm.ANSWER_MODEL, llm.OCR_MODEL))
def test_non_librarian_requests_keep_the_default_output_parameter_mapping(monkeypatch, configured):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "test-completion",
            "object": "chat.completion",
            "created": 0,
            "model": configured.removeprefix("openrouter:"),
            "provider": "fixture",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    async def run():
        model, settings = llm.build_model(configured)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        model.provider._set_http_client(client)
        try:
            await model.request(
                [ModelRequest(parts=[UserPromptPart(content="hello")])],
                settings,
                ModelRequestParameters(),
            )
        finally:
            await client.aclose()

    asyncio.run(run())

    assert len(payloads) == 1
    assert "max_tokens" not in payloads[0]
    assert "max_completion_tokens" not in payloads[0]


def test_librarian_native_output_request_uses_strict_json_schema_and_pins_cerebras(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "test-completion",
            "object": "chat.completion",
            "created": 0,
            "model": "openai/gpt-oss-120b",
            "provider": "cerebras",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                "content": '{"summary":"Filed"}',
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    async def run():
        model, _ = llm.build_model(llm.LIBRARIAN_MODEL)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        model.provider._set_http_client(client)
        subject = planner.PydanticPlanner(
            SimpleNamespace(model=llm.LIBRARIAN_MODEL, max_turns=1),
            model_factory=lambda: model,
        )
        try:
            return await subject._run_structured(
                output_type=FilingPlan,
                instructions="File supported conclusions only.",
                prompt="A supported conclusion.",
            )
        finally:
            await client.aclose()

    result = asyncio.run(run())

    assert result.plan.summary == "Filed"
    assert len(payloads) == 1
    assert "tool_choice" not in payloads[0]
    assert "tools" not in payloads[0]
    assert payloads[0]["model"] == "openai/gpt-oss-120b"
    assert payloads[0]["provider"] == {
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
        "only": ["cerebras"],
    }
    assert payloads[0]["response_format"]["type"] == "json_schema"
    assert payloads[0]["response_format"]["json_schema"]["strict"] is True
    assert payloads[0]["response_format"]["json_schema"]["name"] == "FilingPlan"
    assert payloads[0]["reasoning"] == {
        "effort": "high",
        "exclude": True,
    }


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
