"""`describe_entity` as `ask`'s third tool.

Three layers, mirroring how the other two tools are pinned:

  * `AnswerBrain.entity_text` — the renderer, over a duck-typed service double (the
    `test_synthesize_entity_topology.py` `_FakeService` pattern): layout, absence shape, and
    the `SynthesisContext` bookkeeping (`searched` + surfaced pages) the refusal composer and
    the verifier read.
  * the agent wiring — a real `Agent.run()` driven by `FunctionModel` through the PUBLIC path
    (the discipline recorded in `test_synthesize_entity_topology.py`'s docstring: never a
    private pydantic_ai attribute).
  * the live seam — `entity_text` over a REAL `BrainService` + Postgres (the conftest fixture
    corpus), where "globex" resolves by scoped-id membership with no registry file at all.
"""
import asyncio

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from stigmergy.answer import brain as brain_mod
from stigmergy.answer.brain import AnswerBrain
from stigmergy.answer.synthesize import ANSWER_SYS, SynthesisContext, build_synthesizer
from stigmergy.server.settings import Settings
from tests.answer.conftest import brain_service


@pytest.fixture(autouse=True)
def _dummy_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy-not-real")


# ── the renderer over a service double ─────────────────────────────────────────────────────────

_DESCRIBED = {
    "entity": {"id": "vantage", "name": "Vantage", "type": "organization",
               "aliases": ["vantage.com"],
               "page": {"path": "wiki/entities/Vantage.md", "title": "Vantage"}},
    "timeline": [
        {"path": "wiki/notes/Vantage June 2026 Investor Update.md",
         "title": "Vantage June 2026 Investor Update", "type": "note",
         "status": "developing", "as_of": "2026-06-30"},
        {"path": "wiki/notes/Vantage Hiring.md", "title": "Vantage Hiring", "type": "note",
         "status": "developing", "as_of": ""},
    ],
    "timeline_note": "2 page(s) anchored to this entity — showing all 2.",
}


class _FakeDescribeService:
    """Stands in for the `BrainService` at the seam `entity_text` actually calls
    (`self.service.describe_entity`) — the `_FakeService` pattern from
    `test_synthesize_entity_topology.py`, one seam down."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def describe_entity(self, entity):
        self.calls.append(entity)
        return self.result


def test_entity_text_lays_out_identity_page_and_dated_timeline():
    text = AnswerBrain(_FakeDescribeService(_DESCRIBED)).entity_text("Vantage")

    assert "entity: vantage" in text
    assert "name: Vantage" in text
    assert "aliases: vantage.com" in text
    assert "page: wiki/entities/Vantage.md — Vantage" in text
    assert "timeline: 2 page(s) anchored to this entity — showing all 2." in text
    assert "2026-06-30 · wiki/notes/Vantage June 2026 Investor Update.md" in text
    assert "(undated) · wiki/notes/Vantage Hiring.md" in text


def test_entity_text_records_the_lookup_and_every_shown_page_as_surfaced():
    """The bookkeeping the rest of the loop depends on: the verifier accepts citations only to
    surfaced pages (`read_paths`), and the refusal composer names what was searched and what
    came back — both must see what this tool showed the agent."""
    ctx = SynthesisContext(service=None)
    AnswerBrain(_FakeDescribeService(_DESCRIBED)).entity_text("Vantage", ctx)

    assert ctx.searched == ["Vantage"]
    assert ctx.read_paths == {"wiki/entities/Vantage.md",
                              "wiki/notes/Vantage June 2026 Investor Update.md",
                              "wiki/notes/Vantage Hiring.md"}
    assert ctx.read_paths_order[0] == "wiki/entities/Vantage.md"


def test_entity_text_absence_is_one_line_and_surfaces_nothing():
    """Unknown and out-of-scope arrive as the service's byte-identical absence shape — the
    renderer keeps them one repairable line and records no page."""
    ctx = SynthesisContext(service=None)
    brain = AnswerBrain(_FakeDescribeService({"error": "unknown entity: ghost-corp"}))
    text = brain.entity_text("ghost-corp", ctx)

    assert text == brain_mod.UNKNOWN_ENTITY
    assert ctx.read_paths == set()
    assert ctx.searched == ["ghost-corp"]     # the lookup itself is still a recorded fact


def test_entity_text_renders_registry_gaps_honestly():
    bare = {"entity": {"id": "acme", "name": "", "type": "", "aliases": [], "page": None},
            "timeline": [], "timeline_note": "No anchored pages."}
    text = AnswerBrain(_FakeDescribeService(bare)).entity_text("acme")

    assert "name: (unregistered)" in text
    assert "page: (none)" in text
    assert "timeline: No anchored pages." in text


# ── the agent wiring, through the public path ──────────────────────────────────────────────────


class _FakeAnswerBrain:
    def __init__(self):
        self.calls = []

    def entity_text(self, entity, ctx=None):
        self.calls.append(entity)
        return f"entity territory for {entity}"


def _describe_then_refuse_model() -> FunctionModel:
    def step(messages: list, agent_info: AgentInfo) -> ModelResponse:
        already_called = any(
            isinstance(part, ToolReturnPart) and part.tool_name == "describe_entity"
            for message in messages for part in message.parts)
        if not already_called:
            return ModelResponse(parts=[
                ToolCallPart(tool_name="describe_entity", args={"entity": "Vantage"})])
        output_tool = agent_info.output_tools[0].name
        return ModelResponse(parts=[ToolCallPart(
            tool_name=output_tool,
            args={"answer_markdown": "", "citations": [], "confidence": "low", "refused": True})])
    return FunctionModel(step)


def test_the_real_agent_carries_a_describe_entity_tool_wired_to_entity_text():
    agent = build_synthesizer(Settings(llm="openai", model="gpt-5.6-terra"))
    service = _FakeAnswerBrain()
    deps = SynthesisContext(service=service)
    result = asyncio.run(agent.run("What do we know about Vantage?", deps=deps,
                                   model=_describe_then_refuse_model()))

    tool_return = next(
        part.content for message in result.all_messages() for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == "describe_entity")
    assert tool_return == "entity territory for Vantage"
    assert service.calls == ["Vantage"]
    assert deps.evidence == ["entity territory for Vantage"]   # recorded — the verifier's corpus


def test_answer_sys_names_the_third_tool():
    assert "describe_entity(<name or id>)" in ANSWER_SYS
    assert "maps the territory in one call" in ANSWER_SYS


# ── the live seam: a real BrainService over the fixture corpus (no registry file) ──────────────


def test_entity_text_over_the_real_service_resolves_a_scoped_id(answer_indexed):
    """"globex" is anchored in the fixture corpus but registered nowhere (the fixture repo has
    no ops/entity-registry.json) — exactly the anchored-but-unregistered case `describe_entity`
    resolves by scoped-id membership. The timeline must show the globex pages and
    the hostile title must arrive neutralized by the service, never re-fenced here."""
    conn, fx = answer_indexed
    brain = AnswerBrain(brain_service(conn, fx, "steward"))
    ctx = SynthesisContext(service=brain)

    text = brain.entity_text("globex", ctx)

    assert "entity: globex" in text
    assert "name: (unregistered)" in text
    assert fx.GLOBEX_FINAL in text
    assert fx.GLOBEX_FINAL in ctx.read_paths
    assert "UNTRUSTED-DATA;end" not in text          # the hostile title arrived neutralized


def test_entity_text_over_the_real_service_absence_for_the_unknown(answer_indexed):
    conn, fx = answer_indexed
    brain = AnswerBrain(brain_service(conn, fx, "steward"))

    assert brain.entity_text("ghost-corp") == brain_mod.UNKNOWN_ENTITY
