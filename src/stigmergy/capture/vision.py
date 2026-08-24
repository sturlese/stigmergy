"""Faithful image transcription through the approved Qwen vision model."""

from __future__ import annotations

from collections.abc import Callable

from stigmergy.capture.errors import ExtractionError
from stigmergy.kernel.llm import OCR_MODEL

OCR_PROMPT = (
    "Transcribe this image faithfully and completely to plain text or Markdown. Preserve visible "
    "headings, reading order, lists, and tables. Mark illegible passages as [illegible]. Do not "
    "summarize, interpret, add, or correct content. Return only the transcription."
)


def transcribe_image(
    data: bytes,
    media_type: str,
    *,
    model_name: str = OCR_MODEL,
    agent_builder: Callable | None = None,
) -> str:
    from pydantic_ai import Agent, BinaryContent
    from pydantic_ai.usage import UsageLimits

    from stigmergy.kernel.usage_repair import ensure_usage_extraction_repaired

    ensure_usage_extraction_repaired()
    if media_type not in {"image/png", "image/jpeg"}:
        raise ExtractionError("vision OCR requires a PNG or JPEG image")
    if model_name != OCR_MODEL:
        raise ExtractionError(f"vision OCR model must be {OCR_MODEL}")
    if agent_builder:
        agent = agent_builder(model_name)
    else:
        from stigmergy.kernel.llm import build_model
        model, model_settings = build_model(model_name)
        agent = Agent(model, model_settings=model_settings, retries=2)
    result = agent.run_sync(
        [OCR_PROMPT, BinaryContent(data=data, media_type=media_type)],
        usage_limits=UsageLimits(request_limit=3),
    )
    text = str(result.output or "").strip()
    if not text:
        raise ExtractionError("vision OCR found no readable text")
    return text
