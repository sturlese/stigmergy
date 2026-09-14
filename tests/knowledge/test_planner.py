import asyncio
import datetime as dt
import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from stigmergy.capture import schema
from stigmergy.knowledge import planner
from stigmergy.knowledge.plan import FilingPlan, RepairMutation, RepairPlan


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


def _settings(*, max_turns=2):
    return SimpleNamespace(
        model="openrouter:openai/gpt-oss-120b",
        timeout_s=5,
        max_turns=max_turns,
    )


def _native_model(summary: str) -> FunctionModel:
    return FunctionModel(
        lambda _messages, _info: ModelResponse(
            parts=[TextPart(json.dumps({"summary": summary}))]
        )
    )


def test_pydantic_planner_returns_a_typed_filing_plan_without_a_network_call(tmp_path):
    model = _native_model("Filed the supported decision")
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


def test_pydantic_planner_runs_one_native_output_path(tmp_path, monkeypatch):
    subject = planner.PydanticPlanner(
        _settings(),
        model_factory=lambda: pytest.fail("the mode helper must own model execution"),
    )
    calls = []

    async def run_filing(*, source_text, **_kwargs):
        calls.append(source_text)
        assert "PO-EL27-1847" in source_text
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

    assert calls == ["## Page 1\n\nPURCHASE ORDER PO-EL27-1847"]
    assert isinstance(result.plan, FilingPlan)
    assert result.plan.summary == "Filed the scanned purchase order"


def test_native_output_allows_one_schema_repair_within_the_total_request_budget():
    calls = []

    def respond(_messages, agent_info):
        calls.append(agent_info.output_tools)
        text = (
            "not structured output"
            if len(calls) == 1
            else '{"summary":"Filed after the schema repair"}'
        )
        return ModelResponse(parts=[TextPart(text)])

    subject = planner.PydanticPlanner(
        _settings(),
        model_factory=lambda: FunctionModel(respond),
    )

    result = asyncio.run(subject._run_structured(
        output_type=FilingPlan,
        instructions="File supported conclusions only.",
        prompt="A supported conclusion.",
    ))

    assert calls == [[], []]
    assert result.plan.summary == "Filed after the schema repair"
    assert result.model_requests == 2


def test_native_output_never_exceeds_the_two_request_budget_after_schema_repairs():
    calls = []

    def respond(_messages, agent_info):
        calls.append(agent_info.output_tools)
        return ModelResponse(parts=[TextPart("not structured output")])

    subject = planner.PydanticPlanner(
        _settings(max_turns=2),
        model_factory=lambda: FunctionModel(respond),
    )

    with pytest.raises(UnexpectedModelBehavior, match="maximum output retries"):
        asyncio.run(subject._run_structured(
            output_type=FilingPlan,
            instructions="File supported conclusions only.",
            prompt="A supported conclusion.",
        ))

    assert calls == [[], []]


def test_repair_context_uses_only_the_explicitly_authorized_files(tmp_path):
    worktree = _worktree(tmp_path)
    violations = (
        SimpleNamespace(path="wiki/notes/Terms.md", code="frontmatter", message="repair it"),
        SimpleNamespace(path="wiki/concepts/Missing.md", code="missing", message="gone"),
        SimpleNamespace(path="wiki/concepts/Oversized.md", code="large", message="too large"),
        SimpleNamespace(path="sources/2026/08/source.md", code="source", message="immutable"),
    )
    model = _native_model("No bounded repair was needed")
    subject = planner.PydanticPlanner(_settings(), model_factory=lambda: model)

    result = subject.repair(
        worktree=worktree,
        violations=violations,
        files={"wiki/notes/Terms.md": "# Terms\n\nCurrent text.\n"},
        source_path="sources/2026/08/capture.md",
        source_text="Decision: renew for one year.",
        context='{"candidates": []}',
        max_requests=1,
    )

    assert result.plan.summary == "No bounded repair was needed"
    assert result.plan.mutations == ()
    assert result.model_requests == 1


def test_repair_prompt_requests_only_markdown_bodies(tmp_path, monkeypatch):
    subject = planner.PydanticPlanner(_settings())
    captured = {}

    async def run_structured(**kwargs):
        captured.update(kwargs)
        return planner.PlanRun(RepairPlan(summary="No bounded repair was needed"))

    monkeypatch.setattr(subject, "_run_structured", run_structured)

    result = subject.repair(
        worktree=_worktree(tmp_path),
        violations=(),
        files={"wiki/notes/Terms.md": "# Terms\n\nCurrent text."},
        source_path="sources/2026/08/capture.md",
        source_text="Decision: renew for one year.",
        context='{"candidates": []}',
        max_requests=1,
    )

    assert result.plan.summary == "No bounded repair was needed"
    assert "only the replacement Markdown body" in captured["prompt"]
    assert "do not include YAML front matter" in captured["prompt"]


def test_repair_mutation_accepts_only_body():
    mutation = RepairMutation(
        path="wiki/notes/Terms.md",
        body="# Terms\n\nCurrent text.",
        reason="Restored the required source citation.",
    )

    assert mutation.body == "# Terms\n\nCurrent text."
    with pytest.raises(ValueError, match="text"):
        RepairMutation(
            path="wiki/notes/Terms.md",
            text="# Terms\n\nCurrent text.",
            reason="Retired whole-page field.",
        )


def test_revision_reuses_the_exact_safe_context_and_returns_a_filing_plan(tmp_path, monkeypatch):
    subject = planner.PydanticPlanner(_settings())
    captured = {}

    async def run_structured(**kwargs):
        captured.update(kwargs)
        return planner.PlanRun(FilingPlan(summary="Revised the supported decision."), model_requests=1)

    monkeypatch.setattr(subject, "_run_structured", run_structured)
    context = '{"candidates":[{"path":"wiki/notes/Visible.md"}],"entities":[]}'
    draft = FilingPlan(summary="Draft decision")

    result = subject.revise(
        worktree=_worktree(tmp_path),
        envelope=_envelope(),
        source_path="sources/2026/08/capture.md",
        source_text="Decision: renew for one year.",
        context=context,
        draft=draft,
        max_requests=1,
    )

    assert isinstance(result.plan, FilingPlan)
    assert captured["output_type"] is FilingPlan
    assert captured["max_requests"] == 1
    assert context in captured["prompt"]
    assert "DRAFT FILING PLAN" in captured["prompt"]
    assert json.dumps(draft.model_dump(mode="json"), sort_keys=True) in captured["prompt"]
    assert "collapsed abstraction levels" in captured["prompt"]


def test_planner_rejects_a_prompt_over_its_byte_budget(monkeypatch):
    monkeypatch.setattr(planner, "MAX_PLANNER_PROMPT_BYTES", 10)

    with pytest.raises(ValueError, match="byte limit"):
        planner._prompt(
            envelope=_envelope(),
            source_path="sources/2026/08/capture.md",
            source_text="long source text",
            context="",
        )
