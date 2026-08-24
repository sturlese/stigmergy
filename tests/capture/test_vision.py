from types import SimpleNamespace

import pytest

from stigmergy.capture.errors import ExtractionError
from stigmergy.capture.vision import OCR_PROMPT, transcribe_image
from stigmergy.kernel.llm import ANSWER_MODEL, OCR_MODEL


class RecordingAgent:
    def __init__(self, output="faithful transcription"):
        self.output = output
        self.parts = None
        self.limits = None

    def run_sync(self, parts, *, usage_limits):
        self.parts = parts
        self.limits = usage_limits
        return SimpleNamespace(output=self.output)


def test_qwen_vision_request_is_bounded_and_receives_exact_image_bytes():
    agent = RecordingAgent()
    seen_models = []

    text = transcribe_image(
        b"png-bytes",
        "image/png",
        agent_builder=lambda model: seen_models.append(model) or agent,
    )

    assert text == "faithful transcription"
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
