import asyncio
import datetime as dt
import hashlib
from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from stigmergy.capture import schema
from stigmergy.knowledge import planner
from stigmergy.knowledge.plan import FilingPlan


def _envelope() -> schema.CaptureEnvelope:
    data = b"Quarterly decision"
    digest = hashlib.sha256(data).hexdigest()
    return schema.CaptureEnvelope(
        idempotency_key="planner-test",
        actor=schema.Actor(subject="ana@example.com", display_name="Ana"),
        audience=("finance",),
        origin=schema.Origin(
            adapter="mcp",
            captured_at=dt.datetime(2026, 8, 24, 12, tzinfo=dt.UTC),
            title="Quarterly decision",
        ),
        artifacts=(
            schema.ArtifactRef(
                blob_ref=schema.content_ref(digest),
                sha256=digest,
                bytes=len(data),
                media_type=schema.MEDIA_TEXT,
            ),
        ),
    )


def _worktree(tmp_path):
    skill = tmp_path / ".claude" / "skills" / "librarian" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("File supported conclusions only.\n", encoding="utf-8")
    return str(tmp_path)


def _settings(*, max_turns=1):
    return SimpleNamespace(
        model="openrouter:deepseek/deepseek-v4-flash",
        timeout_s=5,
        max_turns=max_turns,
    )


def test_pydantic_planner_returns_a_typed_filing_plan_without_a_network_call(tmp_path):
    model = TestModel(custom_output_args={"summary": "Filed the supported decision"})
    subject = planner.PydanticPlanner(_settings(), model_factory=lambda: model)

    result = subject.plan(
        worktree=_worktree(tmp_path),
        envelope=_envelope(),
        source_path="sources/2026/08/capture.md",
        source_text="Decision: renew for one year.",
        context="# Existing terms\n\nThe old term was monthly.",
    )

    assert result.plan.summary == "Filed the supported decision"
    assert result.plan.mutations == ()
    assert result.model_requests == 1


def test_pydantic_planner_retries_once_with_prompted_json_after_tool_output_exhaustion(
        tmp_path, monkeypatch):
    subject = planner.PydanticPlanner(
        _settings(),
        model_factory=lambda: pytest.fail("the mode helper must own model execution"),
    )
    calls = []

    async def run_filing(*, mode, source_text, **_kwargs):
        calls.append(mode)
        assert "PO-EL27-1847" in source_text
        if mode == "tool":
            raise UnexpectedModelBehavior("output retries exhausted")
        assert mode == "json"
        return planner.PlanRun(
            plan=FilingPlan(summary="Filed the scanned purchase order"),
            model_requests=1,
        )

    monkeypatch.setattr(subject, "_run_filing", run_filing, raising=False)

    result = subject.plan(
        worktree=_worktree(tmp_path),
        envelope=_envelope(),
        source_path="sources/2026/08/qa-scan.md",
        source_text="## Page 1\n\nPURCHASE ORDER PO-EL27-1847",
        context="",
    )

    assert calls == ["tool", "json"]
    assert isinstance(result.plan, FilingPlan)
    assert result.plan.summary == "Filed the scanned purchase order"


def test_prompted_json_mode_parses_a_real_text_completion():
    subject = planner.PydanticPlanner(
        _settings(),
        model_factory=lambda: FunctionModel(
            lambda _messages, _info: ModelResponse(
                parts=[TextPart('{"summary":"Filed the scanned purchase order"}')]
            )
        ),
    )

    result = asyncio.run(subject._run_structured(
        mode="json",
        output_type=FilingPlan,
        instructions="File supported conclusions only.",
        prompt="PURCHASE ORDER PO-EL27-1847",
    ))

    assert result.plan.summary == "Filed the scanned purchase order"
    assert result.model_requests == 1


def test_output_mode_fallback_shares_one_request_budget_and_reports_total_usage():
    calls = []
    usage = RunUsage()

    def respond(_messages, agent_info):
        calls.append(len(agent_info.output_tools))
        text = (
            "not structured output"
            if agent_info.output_tools
            else '{"summary":"Filed after the output-mode fallback"}'
        )
        return ModelResponse(parts=[TextPart(text)])

    subject = planner.PydanticPlanner(
        _settings(max_turns=4),
        model_factory=lambda: FunctionModel(respond),
    )

    result = asyncio.run(subject._run_output_modes(
        lambda mode: subject._run_structured(
            mode=mode,
            output_type=FilingPlan,
            instructions="File supported conclusions only.",
            prompt="A supported conclusion.",
            usage=usage,
        )
    ))

    assert calls == [1, 1, 1, 0]
    assert result.plan.summary == "Filed after the output-mode fallback"
    assert result.model_requests == 4


def test_output_mode_fallback_cannot_exceed_the_shared_request_budget():
    calls = []

    def respond(_messages, agent_info):
        calls.append(len(agent_info.output_tools))
        text = (
            "not structured output"
            if agent_info.output_tools
            else '{"summary":"This request must never run"}'
        )
        return ModelResponse(parts=[TextPart(text)])

    subject = planner.PydanticPlanner(
        _settings(max_turns=3),
        model_factory=lambda: FunctionModel(respond),
    )
    usage = RunUsage()

    with pytest.raises(UsageLimitExceeded, match="request_limit"):
        asyncio.run(subject._run_output_modes(
            lambda mode: subject._run_structured(
                mode=mode,
                output_type=FilingPlan,
                instructions="File supported conclusions only.",
                prompt="A supported conclusion.",
                usage=usage,
            )
        ))

    assert calls == [1, 1, 1]
    assert usage.requests == 3


def test_repair_context_includes_only_bounded_note_and_concept_files(tmp_path):
    worktree = _worktree(tmp_path)
    note = tmp_path / "wiki" / "notes" / "Terms.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Terms\n\nCurrent text.\n", encoding="utf-8")
    large = tmp_path / "wiki" / "concepts" / "Oversized.md"
    large.parent.mkdir(parents=True)
    large.write_text("x" * 100_001, encoding="utf-8")
    violations = (
        SimpleNamespace(path="wiki/notes/Terms.md", code="frontmatter", message="repair it"),
        SimpleNamespace(path="wiki/concepts/Missing.md", code="missing", message="gone"),
        SimpleNamespace(path="wiki/concepts/Oversized.md", code="large", message="too large"),
        SimpleNamespace(path="sources/2026/08/source.md", code="source", message="immutable"),
    )
    model = TestModel(custom_output_args={"summary": "No bounded repair was needed"})
    subject = planner.PydanticPlanner(_settings(), model_factory=lambda: model)

    result = subject.repair(worktree=worktree, violations=violations)

    assert result.plan.summary == "No bounded repair was needed"
    assert result.plan.mutations == ()
    assert result.model_requests == 1


def test_planner_rejects_a_prompt_over_its_byte_budget(monkeypatch):
    monkeypatch.setattr(planner, "MAX_PLANNER_PROMPT_BYTES", 10)

    with pytest.raises(ValueError, match="byte limit"):
        planner._prompt(
            envelope=_envelope(),
            source_path="sources/2026/08/capture.md",
            source_text="long source text",
            context="",
        )
