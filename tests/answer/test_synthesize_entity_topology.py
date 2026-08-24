"""`synthesize.py`'s agent wiring: the search tool takes `filters` (an unknown filter column
returns an error STRING, never a crash), the budgets are 6/8, and `ANSWER_SYS` carries the
topology paragraph.

Pure and keyless: `build_synthesizer(Settings())` only checks that
`OPENROUTER_API_KEY` is present and never calls the API unless the agent is
actually RUN.

**The real `search` tool is driven through the PUBLIC path, never a private pydantic_ai
attribute.** Reaching `Agent._function_toolset.tools["search"].function` and calling it directly
against a hand-rolled `RunContext` double bypasses pydantic_ai's own tool dispatch, argument
validation/coercion and `RunContext` construction entirely — so a real wiring break in any of
those (a bad type annotation, a renamed tool, a schema pydantic_ai itself would reject) passes
such a suite silently. Instead, REAL tool wiring is exercised offline: a real `Agent.run()`
driven by `pydantic_ai.models.function.FunctionModel` — a model implemented as a plain Python
function that emits exactly the tool calls a test wants, with NO network reached (`model=`
overrides the agent's real backend for one call). `FunctionModel` rather than bare `TestModel`
because these tests need to control the exact `filters` argument each call passes — `TestModel`'s
own auto-generated arguments cannot express that.
"""
import asyncio

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from stigmergy.answer.synthesize import (
    ANSWER_REQUEST_LIMIT,
    ANSWER_SYS,
    ANSWER_TOOL_CALLS_LIMIT,
    EVIDENCE_COMPLETION_REQUEST_LIMIT,
    MAX_ANSWER_REQUESTS,
    SynthesisContext,
    answer_limits,
    build_synthesizer,
)
from stigmergy.server.settings import Settings


@pytest.fixture(autouse=True)
def _dummy_openrouter_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


class _FakeService:
    """Stands in for `AnswerBrain` at the seam the real `search` tool closure actually calls
    (`rc.deps.service.search_text`) — records what it was called with, and raises exactly the
    ValueError `search.py::_filter_clause` raises for an unknown filter column."""

    def __init__(self):
        self.calls = []

    def search_text(self, query, ctx, filters=None):
        self.calls.append({"query": query, "filters": filters})
        if filters and "bogus" in filters:
            raise ValueError("unknown filter column(s): ['bogus'] (allowed: ('entity', 'zone'))")
        return f"listing for {query}"


def _two_turn_search_model(filters: dict | None) -> FunctionModel:
    """A `FunctionModel` driving the real `Agent` through exactly two turns: call the real
    `search` tool once, with `filters`, then emit a schema-valid refusal as the final structured
    output. `agent_info.output_tools[0].name` reads the output tool's name dynamically — never
    hardcoded — so this stays correct regardless of what pydantic_ai internally calls it."""
    def step(messages: list, agent_info: AgentInfo) -> ModelResponse:
        already_called = any(
            isinstance(part, ToolReturnPart) and part.tool_name == "search"
            for message in messages for part in message.parts)
        if not already_called:
            return ModelResponse(parts=[
                ToolCallPart(tool_name="search",
                            args={"query": "quarterly revenue", "filters": filters})])
        output_tool = agent_info.output_tools[0].name
        return ModelResponse(parts=[ToolCallPart(
            tool_name=output_tool,
            args={"answer_markdown": "", "citations": [], "confidence": "low", "refused": True})])
    return FunctionModel(step)


def _run_search_tool(filters: dict | None) -> tuple[str, _FakeService, SynthesisContext]:
    """Runs the REAL agent end to end over the REAL `search` tool closure; returns the literal
    string the tool returned to the model (read back off the `ToolReturnPart` the framework itself
    recorded — not a value this test invented), the fake service double, and the real
    `SynthesisContext` deps object."""
    agent = build_synthesizer(Settings())
    service = _FakeService()
    deps = SynthesisContext(service=service)
    result = asyncio.run(agent.run("what was quarterly revenue?", deps=deps,
                                   model=_two_turn_search_model(filters)))
    search_return = next(
        part.content for message in result.all_messages() for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == "search")
    return search_return, service, deps


# ── filters passthrough, unknown filter -> error string, never a crash ─────────────────────────
def test_search_tool_accepts_and_forwards_filters():
    search_return, service, _ = _run_search_tool({"entity": "acme"})

    assert search_return == "listing for quarterly revenue"
    assert service.calls[-1] == {"query": "quarterly revenue", "filters": {"entity": "acme"}}


def test_search_tool_omitted_filters_defaults_to_none():
    _, service, _ = _run_search_tool(None)

    assert service.calls[-1]["filters"] is None


def test_search_tool_an_unknown_filter_column_returns_an_error_string_never_a_crash():
    """The repair brief the agent can read — not an exception the pydantic_ai run
    loop would have to handle, and not a crash of the answering loop (proven here by the run
    itself completing at all: a real, unhandled exception inside the tool would have propagated
    out of `agent.run()` and failed this test with an error, not an assertion failure)."""
    search_return, _, _ = _run_search_tool({"bogus": "x"})

    assert isinstance(search_return, str)
    assert search_return.startswith("error:")
    assert "unknown filter column" in search_return


def test_search_tool_error_string_is_not_recorded_as_evidence():
    """Only successful listings become evidence text (`ctx.record`) — a procedural repair brief
    about the tool call itself is not document-derived content the verifier should trace figures
    against."""
    _, _, deps = _run_search_tool({"bogus": "x"})

    assert deps.evidence == []


# ── primary budget: 6 requests/8 tools; evidence completion: 1 request/no tools ───────────────
def test_budgets_are_six_requests_eight_tool_calls():
    assert ANSWER_REQUEST_LIMIT == 6
    assert ANSWER_TOOL_CALLS_LIMIT == 8
    assert EVIDENCE_COMPLETION_REQUEST_LIMIT == 1
    assert MAX_ANSWER_REQUESTS == 7


def test_answer_limits_builds_a_usage_limits_object_with_the_pinned_budgets():
    limits = answer_limits()
    assert limits.request_limit == 6
    assert limits.tool_calls_limit == 8


# ── ANSWER_SYS carries the topology paragraph, phrase-pinned ───────────────────────────────────
def test_answer_sys_carries_the_topology_paragraph():
    assert "resolves known entity names automatically" in ANSWER_SYS
    assert 'filters={"entity": <id>}' in ANSWER_SYS
    assert "reader-visible territory" in ANSWER_SYS
    assert "knowledge and source pages" in ANSWER_SYS
    assert "type: entity" not in ANSWER_SYS


def test_the_entity_filter_is_taught_as_enumeration_with_its_cost_named():
    assert "ENUMERATES" in ANSWER_SYS, "the filter is a tool for one job, not the default"
    assert "entity: []" in ANSWER_SYS, "the cost of the filter must be named where it is offered"
    assert "Prefer search(filters=" not in ANSWER_SYS
