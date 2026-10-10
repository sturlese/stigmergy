"""Approved OpenRouter model construction for every model-backed runtime path."""

from __future__ import annotations

import concurrent.futures
import contextlib
import math
import os
import threading

import httpx
from pydantic_ai.models.openrouter import OpenRouterModel, OpenRouterModelSettings

LIBRARIAN_MODEL = "openrouter:deepseek/deepseek-v4.1-flash"
# Answers share the librarian model, and so its reasoning and token ceiling; not its routing.
ANSWER_MODEL = LIBRARIAN_MODEL
LIBRARIAN_REASONING_LEVEL = "medium"
LIBRARIAN_MAX_TOKENS = 65536
LIBRARIAN_TEMPERATURE = 0
OCR_MODEL = "openrouter:qwen/qwen3-vl-8b-instruct"
OCR_TEMPERATURE = 0
# A typed-decision model: it answers `choose` with one probability per option and never writes text.
LAYER_MODEL = "openrouter:typesafe/jev-1.13"
CHAT_MODELS = frozenset({ANSWER_MODEL, LIBRARIAN_MODEL, OCR_MODEL})
APPROVED_MODELS = CHAT_MODELS | {LAYER_MODEL}
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
DECISION_TIMEOUT_S = 10.0
# Callers wait in the shared blocking pool, so a slow provider may hold only this many of its slots.
DECISION_CONCURRENCY = 2
_DECISION_SLOTS = threading.BoundedSemaphore(DECISION_CONCURRENCY)

OPENROUTER_PROVIDER_POLICY = {
    "allow_fallbacks": True,
    "require_parameters": True,
    "data_collection": "deny",
    "zdr": True,
}

# Writing runs in the background, so it starts on the cheapest measured privacy-compatible host; the
# hedge and the fallback cover its slower tail. Answers keep a person waiting, so they start on the
# faster of the two.
# Native structured output is required below, so OpenRouter excludes providers that do not declare
# the librarian's schema; a provider that declares it but returns invalid JSON is ignored by name.
LIBRARIAN_PROVIDER_ROUTING = {
    "order": ["decart/fp4", "deepinfra/fp8"],
    "ignore": ["baseten"],
}
# A capture's second filing attempt starts where a runaway ends in a truncated plan that the schema
# retry can correct, not in an empty response (spec §4.1).
LIBRARIAN_RETRY_PROVIDER_ROUTING = {
    "order": ["deepinfra/fp8", "decart/fp4"],
    "ignore": ["baseten"],
}
ANSWER_PROVIDER_ROUTING = {
    "order": ["deepinfra/fp8"],
    "ignore": ["baseten"],
}
PURPOSES = ("answer", "writer", "writer_retry")


def librarian_routing_label() -> str:
    """The librarian's provider routing as release evidence records it."""
    routing = LIBRARIAN_PROVIDER_ROUTING
    if routing.get("only"):
        return routing["only"][0]
    if routing.get("order"):
        label = "openrouter:order:" + "+".join(routing["order"])
    else:
        label = f"openrouter:{routing.get('sort', 'default')}"
    return label + "".join(f"-ignore:{provider}" for provider in routing.get("ignore", ()))


def provider_policy(model_name: str, purpose: str = "writer") -> dict:
    """The OpenRouter provider policy one approved model is requested with for one purpose."""
    if purpose not in PURPOSES:
        raise ValueError(f"unknown model purpose: {purpose!r}")
    policy = dict(OPENROUTER_PROVIDER_POLICY)
    if model_name == LIBRARIAN_MODEL:
        policy.update(
            LIBRARIAN_PROVIDER_ROUTING if purpose == "writer"
            else LIBRARIAN_RETRY_PROVIDER_ROUTING if purpose == "writer_retry"
            else ANSWER_PROVIDER_ROUTING
        )
    return policy

_MODEL_OVERRIDE = None


@contextlib.contextmanager
def model_override(model):
    """Temporarily inject a pydantic-ai model object for controlled tests."""
    global _MODEL_OVERRIDE
    previous = _MODEL_OVERRIDE
    _MODEL_OVERRIDE = model
    try:
        yield
    finally:
        _MODEL_OVERRIDE = previous


def build_model(model_name: str = ANSWER_MODEL, *, purpose: str = "answer"):
    """Construct an approved model with the mandatory OpenRouter privacy policy and the provider
    routing of its purpose: `writer` for the librarian's plans, `writer_retry` for a capture's
    second filing attempt, `answer` for cited answers."""
    if _MODEL_OVERRIDE is not None:
        return _MODEL_OVERRIDE, None
    if model_name not in CHAT_MODELS:
        raise RuntimeError(f"model is not approved for Stigmergy: {model_name!r}")
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is required")

    from pydantic_ai.providers.openrouter import OpenRouterProvider

    model_settings = OpenRouterModelSettings(
        openrouter_provider=provider_policy(model_name, purpose)
    )
    if model_name == LIBRARIAN_MODEL:
        model_settings["max_tokens"] = LIBRARIAN_MAX_TOKENS
        model_settings["openrouter_reasoning"] = {
            "effort": LIBRARIAN_REASONING_LEVEL,
            "exclude": True,
        }
    if model_name == LIBRARIAN_MODEL:
        model_settings["temperature"] = LIBRARIAN_TEMPERATURE
    if model_name == OCR_MODEL:
        model_settings["temperature"] = OCR_TEMPERATURE
    model_kwargs = {}
    if model_name == LIBRARIAN_MODEL:
        # OpenRouter exposes native JSON Schema for DeepSeek V4.1 Flash. The installed
        # pydantic-ai catalogue predates the model and otherwise falls back to tool output.
        model_kwargs["profile"] = {
            "supports_json_schema_output": True,
            "supports_json_object_output": True,
        }
    model = OpenRouterModel(
        model_name.removeprefix("openrouter:"),
        provider=OpenRouterProvider(api_key=key),
        settings=model_settings,
        **model_kwargs,
    )
    return model, model_settings


def routed_host(message) -> str:
    """The host OpenRouter routed a model response to."""
    return str((getattr(message, "provider_details", None) or {}).get("downstream_provider") or "")


class DecisionFailed(RuntimeError):
    """A typed decision with no usable answer; `reason` names the class of failure and nothing else."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_DECISION_OVERRIDE = None


@contextlib.contextmanager
def decision_override(decider):
    """Temporarily replace `choose` with a scripted decider for controlled tests."""
    global _DECISION_OVERRIDE
    previous = _DECISION_OVERRIDE
    _DECISION_OVERRIDE = decider
    try:
        yield
    finally:
        _DECISION_OVERRIDE = previous


def choose(
    options: dict[str, str],
    state: dict,
    *,
    instructions: str,
    timeout_s: float = DECISION_TIMEOUT_S,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, float]:
    """One probability per option from `LAYER_MODEL`, or `DecisionFailed`.

    One request, no retry, and never longer than `timeout_s` in total: httpx bounds each network
    phase, not the whole exchange, so the wait is bounded here. A request abandoned at the timeout
    keeps its slot until it ends, and no slot free is a failure. The decisions API is alpha; only
    its documented `choice` answer is read and anything else is a failure.
    """
    if _DECISION_OVERRIDE is not None:
        return _DECISION_OVERRIDE(options, state)
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise DecisionFailed("no_key")
    body = {
        "model": LAYER_MODEL.removeprefix("openrouter:"),
        "provider": provider_policy(LAYER_MODEL),
        "state": state,
        "questions": {"choice": {"type": "choice", "instructions": instructions, "criteria": options}},
    }

    slots = _DECISION_SLOTS
    if not slots.acquire(blocking=False):
        raise DecisionFailed("busy")

    def post() -> httpx.Response:
        try:
            timeout = httpx.Timeout(timeout_s, connect=min(3.0, timeout_s))
            with httpx.Client(transport=transport, timeout=timeout) as client:
                return client.post(DECISIONS_URL, headers={"Authorization": f"Bearer {key}"}, json=body)
        finally:
            slots.release()

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(post)
    except BaseException:
        slots.release()
        executor.shutdown(wait=False)
        raise
    try:
        response = future.result(timeout=timeout_s)
    except (concurrent.futures.TimeoutError, httpx.TimeoutException) as error:
        raise DecisionFailed("timeout") from error
    except httpx.HTTPError as error:
        raise DecisionFailed("network") from error
    finally:
        executor.shutdown(wait=False)
    if response.status_code != 200:
        raise DecisionFailed(f"http_{response.status_code}")
    try:
        answer = response.json()["answers"]["choice"]
        probabilities = answer["probabilities"]
        if answer["type"] != "choice" or set(probabilities) != set(options):
            raise ValueError
        result = {option: float(probabilities[option]) for option in options}
    except (ValueError, KeyError, TypeError) as error:
        raise DecisionFailed("invalid_response") from error
    if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in result.values()):
        raise DecisionFailed("invalid_response")
    if abs(sum(result.values()) - 1.0) > 0.05:
        raise DecisionFailed("invalid_response")
    return result
