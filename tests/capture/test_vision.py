from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from stigmergy.capture.errors import ExtractionError
from stigmergy.capture.vision import OCR_PROMPT, transcribe_image
from stigmergy.kernel import llm
from stigmergy.kernel.llm import ANSWER_MODEL, OCR_MODEL


class RecordingAgent:
    def __init__(self, output="faithful transcription"):
        self.output = output
        self.parts = None
        self.limits = None

    def run_sync(self, parts, *, usage_limits):
        self.parts = parts
        self.limits = usage_limits
        response = SimpleNamespace(provider_details={"downstream_provider": "Parasail"})
        return SimpleNamespace(output=self.output, usage=SimpleNamespace(input_tokens=782, output_tokens=257),
                               all_messages=lambda: [response])


def test_qwen_vision_request_is_bounded_and_receives_exact_image_bytes():
    agent = RecordingAgent()
    seen_models = []

    reading = transcribe_image(
        b"png-bytes",
        "image/png",
        agent_builder=lambda model: seen_models.append(model) or agent,
    )

    assert reading.text == "faithful transcription"
    assert (reading.model, reading.upstream) == (OCR_MODEL, "Parasail")
    assert (reading.input_tokens, reading.output_tokens) == (782, 257)
    assert seen_models == [OCR_MODEL]
    assert agent.parts[0] == OCR_PROMPT
    assert agent.parts[1].data == b"png-bytes"
    assert agent.parts[1].media_type == "image/png"
    assert agent.limits.request_limit == 3


def test_vision_rejects_non_image_media_before_the_model_runs():
    with pytest.raises(ExtractionError, match="PNG or JPEG"):
        transcribe_image(
            b"pdf",
            "application/pdf",
            agent_builder=lambda _model: RecordingAgent(),
        )


def test_vision_rejects_another_approved_model_for_ocr():
    with pytest.raises(ExtractionError, match="vision OCR model"):
        transcribe_image(
            b"image",
            "image/png",
            model_name=ANSWER_MODEL,
            agent_builder=lambda _model: RecordingAgent(),
        )


def test_vision_rejects_an_empty_transcription():
    with pytest.raises(ExtractionError, match="no readable text"):
        transcribe_image(
            b"image",
            "image/jpeg",
            agent_builder=lambda _model: RecordingAgent(""),
        )


def test_ocr_runs_the_approved_model_through_its_own_agent(monkeypatch):
    purposes = []

    def build_model(name):
        purposes.append(name)
        return FunctionModel(lambda _messages, _info: ModelResponse(parts=[TextPart("visible [illegible]")])), {}

    monkeypatch.setattr(llm, "build_model", build_model)

    reading = transcribe_image(b"jpeg-bytes", "image/jpeg")

    assert reading.text == "visible [illegible]"
    assert purposes == [OCR_MODEL]
