"""The reader-scoped entity projection exposed to the answer agent."""
import asyncio

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from stigmergy.answer import brain as brain_mod
from stigmergy.answer.brain import AnswerBrain
from stigmergy.answer.synthesize import ANSWER_SYS, SynthesisContext, build_synthesizer
from stigmergy.server.settings import Settings
from tests.answer.conftest import GLOBEX_ID, brain_service


@pytest.fixture(autouse=True)
def _dummy_openrouter_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


# ── the renderer over a service double ─────────────────────────────────────────────────────────

_DESCRIBED = {
    "found": True,
    "entity": {"id": "ent_40000000-0000-4000-8000-000000000001", "name": "Vantage",
               "type": "organization", "aliases": ["vantage.com"], "claims": []},
    "knowledge": [
        {"path": "wiki/notes/Vantage June 2026 Investor Update.md",
         "title": "Vantage June 2026 Investor Update", "type": "note",
         "status": "developing", "updated": "2026-06-30"},
        {"path": "wiki/notes/Vantage Hiring.md", "title": "Vantage Hiring", "type": "note",
         "status": "developing", "updated": ""},
    ],
    "knowledge_note": "2 page(s) anchored to this entity — showing all 2.",
    "sources": [{"path": "sources/2026/08/vantage.md", "title": "Vantage source"}],
}


class _FakeDescribeService:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def describe_entity(self, entity):
        self.calls.append(entity)
        return self.result


def test_entity_text_lays_out_identity_knowledge_and_sources():
    text = AnswerBrain(_FakeDescribeService(_DESCRIBED)).entity_text("Vantage")

    assert "entity: ent_40000000-0000-4000-8000-000000000001" in text
    assert "name: Vantage" in text
    assert "aliases: vantage.com" in text
    assert "knowledge: 2 page(s) anchored to this entity — showing all 2." in text
    assert "2026-06-30 · wiki/notes/Vantage June 2026 Investor Update.md" in text
    assert "(undated) · wiki/notes/Vantage Hiring.md" in text
    assert "sources/2026/08/vantage.md — Vantage source" in text


def test_entity_text_records_the_lookup_and_every_shown_page_as_surfaced():
    ctx = SynthesisContext(service=None)
    AnswerBrain(_FakeDescribeService(_DESCRIBED)).entity_text("Vantage", ctx)

    assert ctx.searched == ["Vantage"]
    assert ctx.read_paths == {"wiki/notes/Vantage June 2026 Investor Update.md",
                              "wiki/notes/Vantage Hiring.md",
                              "sources/2026/08/vantage.md"}
    assert ctx.read_paths_order[0] == "wiki/notes/Vantage June 2026 Investor Update.md"


def test_entity_text_absence_is_one_line_and_surfaces_nothing():
    ctx = SynthesisContext(service=None)
    brain = AnswerBrain(_FakeDescribeService({
        "found": False,
        "entity": None,
        "knowledge": [],
        "knowledge_note": "No visible entity was found.",
        "sources": [],
    }))
    text = brain.entity_text("ghost-corp", ctx)

    assert text == brain_mod.UNKNOWN_ENTITY
    assert ctx.read_paths == set()
    assert ctx.searched == ["ghost-corp"]


def test_entity_text_renders_empty_knowledge_and_sources_honestly():
    bare = {"found": True,
            "entity": {"id": "ent_40000000-0000-4000-8000-000000000002",
                       "name": "Acme", "type": "organization", "aliases": [], "claims": []},
            "knowledge": [], "knowledge_note": "No anchored pages.", "sources": []}
    text = AnswerBrain(_FakeDescribeService(bare)).entity_text("Acme")

    assert "name: Acme" in text
    assert "knowledge: No anchored pages." in text
    assert "sources:\n  (none)" in text


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
    agent = build_synthesizer(Settings())
    service = _FakeAnswerBrain()
    deps = SynthesisContext(service=service)
    result = asyncio.run(agent.run("What do we know about Vantage?", deps=deps,
                                   model=_describe_then_refuse_model()))

    tool_return = next(
        part.content for message in result.all_messages() for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == "describe_entity")
    assert tool_return == "entity territory for Vantage"
    assert service.calls == ["Vantage"]
    assert deps.evidence == ["entity territory for Vantage"]


def test_answer_sys_names_the_third_tool():
    assert "describe_entity(<name or id>)" in ANSWER_SYS
    assert "maps the reader-visible territory in one call" in ANSWER_SYS


def test_answer_sys_requires_false_premises_to_be_corrected_explicitly():
    assert "explicitly correct it" in ANSWER_SYS
    assert "false premise" in ANSWER_SYS


def test_entity_text_over_the_real_service_resolves_a_scoped_name(answer_indexed):
    conn, fx = answer_indexed
    brain = AnswerBrain(brain_service(conn, fx, "steward"))
    ctx = SynthesisContext(service=brain)

    text = brain.entity_text("globex", ctx)

    assert f"entity: {GLOBEX_ID}" in text
    assert "name: Globex" in text
    assert fx.GLOBEX_FINAL in text
    assert fx.GLOBEX_FINAL in ctx.read_paths
    assert "UNTRUSTED-DATA;end" not in text


def test_entity_text_over_the_real_service_absence_for_the_unknown(answer_indexed):
    conn, fx = answer_indexed
    brain = AnswerBrain(brain_service(conn, fx, "steward"))

    assert brain.entity_text("ghost-corp") == brain_mod.UNKNOWN_ENTITY
