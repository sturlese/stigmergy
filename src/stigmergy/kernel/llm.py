"""Approved OpenRouter model construction for every model-backed runtime path."""

from __future__ import annotations

import contextlib
import os

ANSWER_MODEL = "openrouter:z-ai/glm-5.2"
LIBRARIAN_MODEL = "openrouter:deepseek/deepseek-v4-flash"
OCR_MODEL = "openrouter:qwen/qwen3-vl-8b-instruct"
APPROVED_MODELS = frozenset(
    {ANSWER_MODEL, LIBRARIAN_MODEL, OCR_MODEL}
)

OPENROUTER_PROVIDER_POLICY = {
    "allow_fallbacks": False,
    "require_parameters": True,
    "data_collection": "deny",
    "zdr": True,
}

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


def build_model(model_name: str = ANSWER_MODEL):
    """Construct an approved model with the mandatory OpenRouter privacy policy."""
    if _MODEL_OVERRIDE is not None:
        return _MODEL_OVERRIDE, None
    if model_name not in APPROVED_MODELS:
        raise RuntimeError(f"model is not approved for Stigmergy: {model_name!r}")
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is required")

    from pydantic_ai.models.openrouter import OpenRouterModel, OpenRouterModelSettings
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    from stigmergy.kernel.usage_repair import ensure_usage_extraction_repaired

    ensure_usage_extraction_repaired()
    settings = OpenRouterModelSettings(
        openrouter_provider=dict(OPENROUTER_PROVIDER_POLICY)
    )
    model = OpenRouterModel(
        model_name.removeprefix("openrouter:"),
        provider=OpenRouterProvider(api_key=key),
        settings=settings,
    )
    return model, None
