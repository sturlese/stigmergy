import json
import threading
import time

import httpx
import pytest
from pydantic_ai.models.openrouter import OpenRouterModel

from stigmergy.kernel import llm


def test_runtime_model_contract_contains_no_premium_recovery_model():
    assert llm.ANSWER_MODEL == "openrouter:deepseek/deepseek-v4.1-flash"
    assert llm.LIBRARIAN_MODEL == "openrouter:deepseek/deepseek-v4.1-flash"
    assert llm.OCR_MODEL == "openrouter:qwen/qwen3-vl-8b-instruct"
    assert llm.LAYER_MODEL == "openrouter:typesafe/jev-1.13"
    assert frozenset(
        {llm.ANSWER_MODEL, llm.LIBRARIAN_MODEL, llm.OCR_MODEL, llm.LAYER_MODEL}
    ) == llm.APPROVED_MODELS
    assert all("gpt-5.4" not in model for model in llm.APPROVED_MODELS)
    assert all("jev-router" not in model for model in llm.APPROVED_MODELS)


def test_every_approved_chat_model_has_the_mandatory_privacy_policy(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    for configured in llm.CHAT_MODELS:
        model, settings = llm.build_model(configured)
        assert isinstance(model, OpenRouterModel)
        assert settings is model.settings
        assert {
            "require_parameters": True,
            "data_collection": "deny",
            "zdr": True,
        }.items() <= settings["openrouter_provider"].items()


def test_the_writer_starts_on_the_cheapest_measured_private_provider_and_falls_back():
    assert llm.provider_policy(llm.LIBRARIAN_MODEL, "writer") == {
        "allow_fallbacks": True,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
        "order": ["decart/fp4", "deepinfra/fp8"],
        "ignore": ["baseten"],
    }
    assert llm.librarian_routing_label() == "openrouter:order:decart/fp4+deepinfra/fp8-ignore:baseten"


def test_a_retried_filing_starts_on_the_provider_whose_runaways_recover():
    assert llm.provider_policy(llm.LIBRARIAN_MODEL, "writer_retry") == {
        "allow_fallbacks": True,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
        "order": ["deepinfra/fp8", "decart/fp4"],
        "ignore": ["baseten"],
    }


def test_answers_start_on_the_faster_private_provider():
    assert llm.provider_policy(llm.ANSWER_MODEL, "answer") == {
        "allow_fallbacks": True,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
        "order": ["deepinfra/fp8"],
        "ignore": ["baseten"],
    }
    with pytest.raises(ValueError, match="unknown model purpose"):
        llm.provider_policy(llm.ANSWER_MODEL, "chat")


def test_ocr_keeps_same_model_provider_failover_without_librarian_settings(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    model, _ = llm.build_model(llm.OCR_MODEL)
    assert model.settings["openrouter_provider"] == llm.OPENROUTER_PROVIDER_POLICY
    assert "openrouter_reasoning" not in model.settings
    assert "max_tokens" not in model.settings


def test_ocr_reads_an_image_the_same_way_every_time(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    model, _ = llm.build_model(llm.OCR_MODEL)
    assert model.settings["temperature"] == 0


def test_answers_share_the_librarian_model_settings_but_not_its_routing(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    _answer_model, answer_settings = llm.build_model(llm.ANSWER_MODEL)
    _librarian_model, librarian_settings = llm.build_model(llm.LIBRARIAN_MODEL, purpose="writer")

    shared = {key for key in answer_settings if key != "openrouter_provider"}
    assert {key: answer_settings[key] for key in shared} == {key: librarian_settings[key] for key in shared}
    assert answer_settings["openrouter_provider"] == llm.provider_policy(llm.ANSWER_MODEL, "answer")
    assert librarian_settings["openrouter_provider"] == llm.provider_policy(llm.LIBRARIAN_MODEL, "writer")


def test_librarian_requests_medium_reasoning_and_native_deterministic_output(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    model, settings = llm.build_model(llm.LIBRARIAN_MODEL)

    assert settings is model.settings
    assert settings["max_tokens"] == llm.LIBRARIAN_MAX_TOKENS == 65536
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


def test_the_decisions_model_is_not_a_chat_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    with pytest.raises(RuntimeError, match="not approved"):
        llm.build_model(llm.LAYER_MODEL)


@pytest.fixture(autouse=True)
def fresh_decision_slots(monkeypatch):
    monkeypatch.setattr(llm, "_DECISION_SLOTS", threading.BoundedSemaphore(llm.DECISION_CONCURRENCY))


OPTIONS = {"deals": "Customer deals and pricing.", "people": "Hiring and team organization."}
STATE = {"title": "Pricing call", "text": "We agreed the renewal price."}


def _answer(probabilities, *, kind="choice"):
    return {"id": "gen-dec-1", "model": "typesafe/jev-1.13-20260917", "provider": "TypeSafe",
            "answers": {"choice": {"type": kind, "choice": "deals", "confidence": 0.9,
                                   "probabilities": probabilities}},
            "usage": {"input_tokens": 10, "output_tokens": 1, "cost": 0.0}}


def _choose(handler, **kwargs):
    return llm.choose(OPTIONS, STATE, instructions="Which layer?",
                      transport=httpx.MockTransport(handler), **kwargs)


def test_choose_sends_one_private_typed_request_and_returns_every_probability(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=_answer({"deals": 0.93, "people": 0.07}))

    assert _choose(handler) == {"deals": 0.93, "people": 0.07}
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == llm.DECISIONS_URL
    assert request.headers["authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert body == {
        "model": "typesafe/jev-1.13",
        "provider": {"allow_fallbacks": True, "require_parameters": True,
                     "data_collection": "deny", "zdr": True},
        "state": STATE,
        "questions": {"choice": {"type": "choice", "instructions": "Which layer?", "criteria": OPTIONS}},
    }


def test_choose_without_a_key_fails_before_any_request():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=_answer({"deals": 1.0, "people": 0.0}))

    with pytest.raises(llm.DecisionFailed) as failed:
        _choose(handler)
    assert failed.value.reason == "no_key"
    assert calls == []


@pytest.mark.parametrize("response, reason", [
    (httpx.Response(429, json={"error": {"code": 429, "message": "Rate limit exceeded"}}), "http_429"),
    (httpx.Response(200, text="not json"), "invalid_response"),
    (httpx.Response(200, json={"answers": {}}), "invalid_response"),
    (httpx.Response(200, json=_answer({"deals": 0.9})), "invalid_response"),
    (httpx.Response(200, json=_answer({"deals": 0.9, "people": 0.1, "public": 0.0})), "invalid_response"),
    (httpx.Response(200, json=_answer({"deals": 1.4, "people": 0.1})), "invalid_response"),
    (httpx.Response(200, json=_answer({"deals": "high", "people": 0.1})), "invalid_response"),
    (httpx.Response(200, json=_answer({"deals": 0.9, "people": 0.1}, kind="score")), "invalid_response"),
    (httpx.Response(200, json=_answer({"deals": 0.9, "people": 0.9})), "invalid_response"),
])
def test_choose_treats_anything_but_the_documented_answer_as_failure(monkeypatch, response, reason):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    with pytest.raises(llm.DecisionFailed) as failed:
        _choose(lambda request: response)
    assert failed.value.reason == reason


def test_choose_reports_a_network_failure_by_class(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(llm.DecisionFailed) as failed:
        _choose(handler)
    assert failed.value.reason == "network"


def test_choose_never_waits_beyond_its_timeout(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def slow(request):
        time.sleep(1.0)
        return httpx.Response(200, json=_answer({"deals": 1.0, "people": 0.0}))

    started = time.monotonic()
    with pytest.raises(llm.DecisionFailed) as failed:
        _choose(slow, timeout_s=0.2)
    assert failed.value.reason == "timeout"
    assert time.monotonic() - started < 0.8


def test_a_slow_provider_holds_only_its_slots_and_later_calls_fail_at_once(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    release, entered = threading.Event(), threading.Semaphore(0)

    def held(request):
        entered.release()
        release.wait(5)
        return httpx.Response(200, json=_answer({"deals": 1.0, "people": 0.0}))

    outcomes = []

    def call():
        try:
            outcomes.append(_choose(held, timeout_s=5))
        except llm.DecisionFailed as error:
            outcomes.append(error.reason)

    callers = [threading.Thread(target=call) for _ in range(llm.DECISION_CONCURRENCY)]
    for caller in callers:
        caller.start()
    try:
        for _ in callers:
            assert entered.acquire(timeout=5)
        started = time.monotonic()
        with pytest.raises(llm.DecisionFailed) as failed:
            _choose(held, timeout_s=5)
        assert failed.value.reason == "busy"
        assert time.monotonic() - started < 0.1
    finally:
        release.set()
        for caller in callers:
            caller.join(5)
    assert outcomes == [{"deals": 1.0, "people": 0.0}] * llm.DECISION_CONCURRENCY


def test_decision_override_replaces_the_api_for_tests(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def scripted(options, state):
        return {name: 0.5 for name in options}

    with llm.decision_override(scripted):
        assert llm.choose(OPTIONS, STATE, instructions="Which layer?") == {"deals": 0.5, "people": 0.5}
