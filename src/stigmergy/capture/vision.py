"""Faithful image transcription through the approved Qwen vision model."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from stigmergy.capture.errors import ExtractionError
from stigmergy.kernel.llm import OCR_MODEL, routed_host

OCR_PROMPT = (
    "Transcribe this image faithfully and completely to plain text or Markdown. Preserve visible "
    "headings, reading order, lists, and tables. Mark illegible passages as [illegible]. Do not "
    "summarize, interpret, add, or correct content. Return only the transcription."
)


@dataclass(frozen=True)
class Transcription:
    text: str
    model: str
    upstream: str = ""
    ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def transcribe_image(
    data: bytes,
    media_type: str,
    *,
    model_name: str = OCR_MODEL,
    agent_builder: Callable | None = None,
) -> Transcription:
    from pydantic_ai import Agent, BinaryContent
    from pydantic_ai.usage import UsageLimits
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
    started = time.perf_counter()
    result = agent.run_sync(
        [OCR_PROMPT, BinaryContent(data=data, media_type=media_type)],
        usage_limits=UsageLimits(request_limit=3),
    )
    text = str(result.output or "").strip()
    if not text:
        raise ExtractionError("vision OCR found no readable text")
    usage = result.usage
    return Transcription(
        text=text,
        model=model_name,
        upstream=routed_host(result.all_messages()[-1]),
        ms=round((time.perf_counter() - started) * 1000),
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )
