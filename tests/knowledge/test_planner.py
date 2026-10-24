import asyncio
import datetime as dt
import hashlib
import json
import os
import time
from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior
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


def test_a_correction_repeats_its_filing_prompt_up_to_the_task_so_a_provider_reuses_it():
    shared = dict(
        envelope=_envelope(),
        source_path="sources/2026/08/example.md",
        source_text="Primary evidence",
        context='{"catalog": ["Visible"]}',
    )
    filing = planner._filing_prompt(**shared)
    correction = planner._correction_prompt(
        **shared, draft=FilingPlan(summary="Draft"), violations=({"code": "heading-title"},)
    )

    prefix = os.path.commonprefix([filing, correction])
    assert prefix.startswith("SAFE EXISTING CONTEXT")
    assert '"catalog": ["Visible"]' in prefix
    assert "sources/2026/08/example.md" in prefix
    assert "Primary evidence" in prefix
    assert filing.index("PROVENANCE") < filing.index("READABLE SOURCE") < filing.index("Return one complete")


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


def test_a_retried_filing_requests_its_plan_and_correction_with_the_retry_routing(tmp_path, monkeypatch):
    from stigmergy.kernel import llm

    purposes = []

    def build_model(_name, *, purpose):
        purposes.append(purpose)
        return _model({"summary": "Filed on the retry"}), {}

    monkeypatch.setattr(llm, "build_model", build_model)
    librarian = planner.PydanticPlanner(_settings(max_turns=3))
    request = {"worktree": _worktree(tmp_path), "envelope": _envelope(),
               "source_path": "sources/2026/08/example.md", "source_text": "A durable decision.",
               "context": "{}"}

    librarian.plan(**request)
    librarian.plan(**request, retry=True)
    librarian.revise(**request, draft=FilingPlan(summary="Draft"), max_requests=1, retry=True)

    assert purposes == ["writer", "writer_retry", "writer_retry"]


def _plan_with(respond, tmp_path):
    librarian = planner.PydanticPlanner(_settings(), model_factory=lambda: FunctionModel(respond))
    started = time.monotonic()
    run = librarian.plan(
        worktree=_worktree(tmp_path),
        envelope=_envelope(),
        source_path="sources/2026/08/example.md",
        source_text="A durable decision.",
        context="{}",
    )
    return run, time.monotonic() - started


def test_a_slow_planning_request_is_raced_by_one_identical_request(tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "HEDGE_AFTER_S", 0.05)
    calls = []

    async def respond(_messages, _info):
        calls.append(len(calls))
        if len(calls) == 1:
            await asyncio.sleep(3)
            return ModelResponse(parts=[TextPart(json.dumps({"summary": "slow first plan"}))])
        return ModelResponse(parts=[TextPart(json.dumps({"summary": "hedged plan"}))])

    run, elapsed = _plan_with(respond, tmp_path)

    assert run.plan.summary == "hedged plan"
    assert elapsed < 2
    assert len(calls) == 2
    assert run.model_requests == 1
    assert run.telemetry["hedged"] is True


def test_a_prompt_planning_request_is_never_duplicated(tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "HEDGE_AFTER_S", 1)
    calls = []

    async def respond(_messages, _info):
        calls.append(1)
        return ModelResponse(parts=[TextPart(json.dumps({"summary": "prompt plan"}))])

    run, _elapsed = _plan_with(respond, tmp_path)

    assert run.plan.summary == "prompt plan"
    assert len(calls) == 1
    assert run.telemetry["hedged"] is False


def test_a_failed_first_request_yields_to_the_racing_request(tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "HEDGE_AFTER_S", 0.05)
    calls = []

    async def respond(_messages, _info):
        calls.append(1)
        if len(calls) == 1:
            await asyncio.sleep(0.3)
            raise RuntimeError("provider stalled and failed")
        await asyncio.sleep(0.6)
        return ModelResponse(parts=[TextPart(json.dumps({"summary": "hedged plan"}))])

    run, _elapsed = _plan_with(respond, tmp_path)

    assert run.plan.summary == "hedged plan"
    assert run.telemetry["hedged"] is True


def _truncated_then_valid():
    calls = []

    def respond(_messages, _info):
        calls.append(len(calls))
        if len(calls) == 1:
            return ModelResponse(parts=[TextPart('{"summary": "Cut off at the output ce')], finish_reason="length")
        return ModelResponse(parts=[TextPart(json.dumps({"summary": "Corrected plan"}))])

    return respond, calls


def _correct(tmp_path, respond, max_requests):
    librarian = planner.PydanticPlanner(_settings(max_turns=3), model_factory=lambda: FunctionModel(respond))
    return librarian.revise(
        worktree=_worktree(tmp_path),
        envelope=_envelope(),
        source_path="sources/2026/08/example.md",
        source_text="Primary evidence",
        context="{}",
        draft=FilingPlan(summary="Draft"),
        max_requests=max_requests,
    )


def test_a_correction_cut_off_at_the_output_ceiling_fails_without_a_request_left(tmp_path):
    respond, calls = _truncated_then_valid()
    with pytest.raises(planner.OutputExhausted):
        _correct(tmp_path, respond, max_requests=1)
    assert calls == [0]


def test_a_plan_spent_on_reasoning_up_to_the_output_limit_gets_a_fresh_attempt(tmp_path):
    def respond(_messages, _info):
        return ModelResponse(parts=[], finish_reason="length")

    with pytest.raises(planner.OutputExhausted) as raised:
        _plan_with(respond, tmp_path)

    assert isinstance(raised.value, UnexpectedModelBehavior)
    assert raised.value.retryable is True and raised.value.fresh_budget is True
    assert raised.value.category == "UnexpectedModelBehavior"


def test_an_invalid_plan_that_was_not_cut_off_stays_terminal(tmp_path):
    def respond(_messages, _info):
        return ModelResponse(parts=[TextPart(json.dumps(
            {"summary": "Updated a page", "mutations": [{"action": "update", "reason": "No path"}]}))])

    with pytest.raises(UnexpectedModelBehavior) as raised:
        _plan_with(respond, tmp_path)

    assert not isinstance(raised.value, planner.OutputExhausted)


def test_a_planning_request_past_its_timeout_gets_a_fresh_attempt(tmp_path):
    async def respond(_messages, _info):
        await asyncio.sleep(3)
        return ModelResponse(parts=[TextPart(json.dumps({"summary": "too late"}))])

    librarian = planner.PydanticPlanner(SimpleNamespace(**{**vars(_settings()), "timeout_s": 0.2}),
                                        model_factory=lambda: FunctionModel(respond))
    with pytest.raises(planner.PlanningTimeout) as raised:
        librarian.plan(worktree=_worktree(tmp_path), envelope=_envelope(),
                       source_path="sources/2026/08/example.md", source_text="A durable decision.", context="{}")

    assert isinstance(raised.value, TimeoutError)
    assert raised.value.retryable is True and raised.value.fresh_budget is True
    assert raised.value.category == "TimeoutError"


def test_a_correction_cut_off_at_the_output_ceiling_retries_within_its_budget(tmp_path):
    respond, calls = _truncated_then_valid()

    run = _correct(tmp_path, respond, max_requests=2)

    assert run.plan.summary == "Corrected plan"
    assert run.model_requests == 2 and calls == [0, 1]


def test_a_failed_model_run_logs_how_it_ended_but_never_model_text(tmp_path, caplog):
    respond, _calls = _truncated_then_valid()
    with caplog.at_level("WARNING", logger="stigmergy.knowledge.planner"), pytest.raises(UnexpectedModelBehavior):
        _correct(tmp_path, respond, max_requests=1)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "phase=semantic_revision" in logged and "UnexpectedModelBehavior" in logged
    assert '"finish_reason": "length"' in logged
    assert "Cut off" not in logged


def test_an_invalid_plan_logs_which_rule_failed_but_never_the_offending_text(tmp_path, caplog):
    def respond(_messages, _info):
        return ModelResponse(parts=[TextPart(json.dumps(
            {"summary": "Updated a page", "mutations": [{"action": "update", "reason": "secret words"}]}))])

    with caplog.at_level("WARNING", logger="stigmergy.knowledge.planner"), pytest.raises(UnexpectedModelBehavior):
        _correct(tmp_path, respond, max_requests=1)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert '"loc": "mutations.0", "type": "value_error"' in logged
    assert "secret words" not in logged


def test_a_plan_that_needed_a_schema_retry_records_why(tmp_path):
    calls = []

    def respond(_messages, _info):
        calls.append(len(calls))
        payload = ({"summary": "Updated a page", "mutations": [{"action": "update", "reason": "No path"}]}
                   if len(calls) == 1 else {"summary": "Filed one coherent plan"})
        return ModelResponse(parts=[TextPart(json.dumps(payload))])

    librarian = planner.PydanticPlanner(_settings(max_turns=3), model_factory=lambda: FunctionModel(respond))
    run = librarian.plan(worktree=_worktree(tmp_path), envelope=_envelope(),
                         source_path="sources/2026/08/example.md", source_text="A durable decision.", context="{}")

    assert run.model_requests == 2
    assert run.telemetry["schema_retries"] == [{"loc": "mutations.0", "type": "value_error"}]


def test_telemetry_names_the_provider_that_served_each_response(tmp_path, caplog):
    calls = []

    def respond(_messages, _info):
        calls.append(len(calls))
        payload = ({"summary": "Updated a page", "mutations": [{"action": "update", "reason": "No path"}]}
                   if len(calls) == 1 else {"summary": "Filed one coherent plan"})
        return ModelResponse(parts=[TextPart(json.dumps(payload))],
                             provider_details={"downstream_provider": "Together" if len(calls) > 1 else "Other"})

    librarian = planner.PydanticPlanner(_settings(max_turns=3), model_factory=lambda: FunctionModel(respond))
    run = librarian.plan(worktree=_worktree(tmp_path), envelope=_envelope(),
                         source_path="sources/2026/08/example.md", source_text="A durable decision.", context="{}")

    assert [response["upstream"] for response in run.telemetry["responses"]] == ["Other", "Together"]
