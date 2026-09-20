import datetime as dt
import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from stigmergy.capture import schema
from stigmergy.knowledge import planner
from stigmergy.knowledge.plan import FilingPlan, RepairPlan


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
    skill.write_text("Make one coherent filing decision.\n", encoding="utf-8")
    return str(tmp_path)


def _settings(max_turns=2):
    return SimpleNamespace(
        model="openrouter:deepseek/deepseek-v4.1-flash",
        timeout_s=5,
        max_turns=max_turns,
    )


def _model(payload):
    return FunctionModel(
        lambda _messages, _info: ModelResponse(parts=[TextPart(json.dumps(payload))])
    )


def test_one_model_result_is_the_complete_filing_plan(tmp_path):
    librarian = planner.PydanticPlanner(
        _settings(),
        model_factory=lambda: _model({"summary": "Filed one coherent plan"}),
    )

    run = librarian.plan(
        worktree=_worktree(tmp_path),
        envelope=_envelope(),
        source_path="sources/2026/08/example.md",
        source_text="A durable decision.",
        context="{}",
    )

    assert isinstance(run.plan, FilingPlan)
    assert run.plan.summary == "Filed one coherent plan"
    assert run.model_requests == 1
    assert run.schema_retry_count == 0


def test_filing_prompt_keeps_source_provenance_and_safe_context_together():
    prompt = planner._filing_prompt(
        envelope=_envelope(),
        source_path="sources/2026/08/example.md",
        source_text="Primary evidence",
        context='{"pages": ["Visible"]}',
    )

    assert "PROVENANCE" in prompt
    assert "READABLE SOURCE" in prompt
    assert "SAFE EXISTING CONTEXT" in prompt
    assert "sources/2026/08/example.md" in prompt
    assert "Primary evidence" in prompt
    assert '"pages": ["Visible"]' in prompt


def test_correction_is_one_complete_replacement_with_explicit_failures():
    prompt = planner._correction_prompt(
        envelope=_envelope(),
        source_path="sources/2026/08/example.md",
        source_text="Primary evidence",
        context="{}",
        draft=FilingPlan(summary="Draft"),
        violations=({"path": "wiki/concepts/Decision.md", "code": "heading-title"},),
    )

    assert "complete corrected FilingPlan" in prompt
    assert "CONTRACT FAILURES" in prompt
    assert "heading-title" in prompt
    assert '"summary": "Draft"' in prompt


def test_repair_with_no_remaining_request_returns_no_mutations(tmp_path):
    librarian = planner.PydanticPlanner(_settings())
    run = librarian.repair(
        worktree=_worktree(tmp_path),
        violations=(),
        files={},
        source_path="",
        source_text="",
        context="{}",
        max_requests=0,
    )

    assert isinstance(run.plan, RepairPlan)
    assert run.plan.mutations == ()
    assert run.model_requests == 0


def test_revision_requires_a_remaining_request(tmp_path):
    librarian = planner.PydanticPlanner(_settings())
    with pytest.raises(ValueError, match="one remaining request"):
        librarian.revise(
            worktree=_worktree(tmp_path),
            envelope=_envelope(),
            source_path="sources/2026/08/example.md",
            source_text="Primary evidence",
            context="{}",
            draft=FilingPlan(summary="Draft"),
            max_requests=0,
        )


def test_prompt_guard_rejects_oversized_inputs(monkeypatch):
    monkeypatch.setattr(planner, "MAX_PLANNER_PROMPT_BYTES", 10)
    with pytest.raises(ValueError, match="byte limit"):
        planner._filing_prompt(
            envelope=_envelope(),
            source_path="sources/2026/08/example.md",
            source_text="Primary evidence",
            context="{}",
        )
