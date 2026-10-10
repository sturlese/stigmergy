"""The reader-scoped entity projection exposed to the answer agent."""
import asyncio

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from stigmergy.answer import brain as brain_mod
from stigmergy.answer.brain import AnswerBrain
from stigmergy.answer.synthesize import ANSWER_SYS, SynthesisContext, build_synthesizer
from stigmergy.server.settings import Settings
from tests.answer.conftest import GLOBEX_ENTITY, brain_service


@pytest.fixture(autouse=True)
def _dummy_openrouter_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


# ── the renderer over a service double ─────────────────────────────────────────────────────────

_DESCRIBED = {
    "found": True,
    "entity": {"path": "wiki/entities/Brightmoor.md", "title": "Brightmoor",
               "entity_type": "organization", "aliases": ["brightmoor.com"],
               "status": "developing", "updated": "2026-06-30",
               "excerpt": "Brightmoor is a portfolio company.", "excerpt_truncated": False},
    "knowledge": [
        {"path": "wiki/notes/Brightmoor June 2026 Investor Update.md",
         "title": "Brightmoor June 2026 Investor Update", "type": "note",
         "status": "developing", "updated": "2026-06-30",
         "excerpt": "Brightmoor reported a durable investor update.",
         "excerpt_truncated": False,
         "sources": [{"path": "sources/2026/08/brightmoor.md", "title": "Brightmoor source"}],
         "relationships": [{"path": "wiki/concepts/Capital Allocation.md",
                              "title": "Capital Allocation",
                              "statement": "The update explains Brightmoor's capital allocation.",
                              "statement_truncated": False}]},
        {"path": "wiki/notes/Brightmoor Hiring.md", "title": "Brightmoor Hiring", "type": "note",
         "status": "developing", "updated": "", "excerpt": "Brightmoor has an open hiring plan.",
         "excerpt_truncated": False, "sources": [], "relationships": []},
    ],
    "knowledge_note": "2 page(s) link this entity — showing all 2.",
    "knowledge_state": "available",
    "knowledge_truncated": False,
    "knowledge_returned": 2,
    "knowledge_cap": 20,
    "sources": [{"path": "sources/2026/08/brightmoor.md", "title": "Brightmoor source"}],
    "sources_truncated": False,
    "sources_returned": 1,
    "sources_cap": 40,
}


class _FakeDescribeService:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def describe_entity(self, entity):
        self.calls.append(entity)
        return self.result


def test_entity_text_lays_out_identity_knowledge_and_sources():
    text = AnswerBrain(_FakeDescribeService(_DESCRIBED)).entity_text("Brightmoor")

    assert "entity: wiki/entities/Brightmoor.md" in text
    assert "name: Brightmoor" in text
    assert "type: organization" in text
    assert "aliases: brightmoor.com" in text
    assert "page: Brightmoor is a portfolio company." in text
    assert "knowledge: 2 page(s) link this entity — showing all 2." in text
    assert "knowledge state: available" in text
    assert "2026-06-30 · wiki/notes/Brightmoor June 2026 Investor Update.md" in text
    assert "evidence: Brightmoor reported a durable investor update." in text
    assert "connection: The update explains Brightmoor's capital allocation." in text
    assert "(undated) · wiki/notes/Brightmoor Hiring.md" in text
    assert "sources/2026/08/brightmoor.md — Brightmoor source" in text


def test_entity_text_records_the_lookup_and_every_shown_page_as_surfaced():
    ctx = SynthesisContext(service=None)
    AnswerBrain(_FakeDescribeService(_DESCRIBED)).entity_text("Brightmoor", ctx)

    assert ctx.searched == ["Brightmoor"]
    assert ctx.read_paths == {"wiki/entities/Brightmoor.md",
                              "wiki/notes/Brightmoor June 2026 Investor Update.md",
                              "wiki/notes/Brightmoor Hiring.md",
                              "sources/2026/08/brightmoor.md"}
    # the entity page is a page the model may quote, so it is surfaced first, then its linkers
    assert ctx.read_paths_order[:2] == ["wiki/entities/Brightmoor.md",
                                        "wiki/notes/Brightmoor June 2026 Investor Update.md"]


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
            "entity": {"path": "wiki/entities/Acme.md", "title": "Acme",
                       "entity_type": "organization", "aliases": [], "status": "seed",
                       "updated": "", "excerpt": "Acme is a customer.", "excerpt_truncated": False},
            "knowledge": [], "knowledge_note": "No pages link this entity.", "sources": []}
    text = AnswerBrain(_FakeDescribeService(bare)).entity_text("Acme")

    assert "name: Acme" in text
    assert "aliases: (none)" in text
    assert "knowledge: No pages link this entity." in text
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
                ToolCallPart(tool_name="describe_entity", args={"entity": "Brightmoor"})])
        output_tool = agent_info.output_tools[0].name
        return ModelResponse(parts=[ToolCallPart(
            tool_name=output_tool,
            args={"answer_markdown": "", "citations": [], "confidence": "low", "refused": True})])
    return FunctionModel(step)


def test_the_real_agent_carries_a_describe_entity_tool_wired_to_entity_text():
    agent = build_synthesizer(Settings())
    service = _FakeAnswerBrain()
    deps = SynthesisContext(service=service)
    result = asyncio.run(agent.run("What do we know about Brightmoor?", deps=deps,
                                   model=_describe_then_refuse_model()))

    tool_return = next(
        part.content for message in result.all_messages() for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == "describe_entity")
    assert tool_return == "entity territory for Brightmoor"
    assert service.calls == ["Brightmoor"]
    assert deps.evidence == ["entity territory for Brightmoor"]


def test_answer_sys_names_the_third_tool():
    flat = " ".join(ANSWER_SYS.split())
    assert "describe_entity(<name, alias, or wiki/entities path>)" in flat
    assert "maps the reader-visible territory in one call" in flat


def test_answer_sys_requires_false_premises_to_be_corrected_explicitly():
    assert "explicitly correct it" in ANSWER_SYS
    assert "false premise" in ANSWER_SYS


def test_entity_text_over_the_real_service_resolves_a_scoped_name(answer_indexed):
    conn, fx = answer_indexed
    brain = AnswerBrain(brain_service(conn, fx, "steward"))
    ctx = SynthesisContext(service=brain)

    text = brain.entity_text("globex", ctx)

    assert f"entity: {GLOBEX_ENTITY}" in text
    assert "name: Globex" in text
    assert "type: organization" in text
    assert "aliases: Globex Corporation" in text
    assert fx.GLOBEX_FINAL in text
    assert fx.GLOBEX_FINAL in ctx.read_paths
    assert GLOBEX_ENTITY in ctx.read_paths
    assert "UNTRUSTED-DATA;end" not in text     # the hostile-titled linker is neutralized


def test_entity_text_over_the_real_service_accepts_an_alias_or_the_page_path(answer_indexed):
    conn, fx = answer_indexed
    brain = AnswerBrain(brain_service(conn, fx, "steward"))

    by_alias = brain.entity_text("globex corporation")
    by_path = brain.entity_text(GLOBEX_ENTITY)

    assert by_alias == by_path
    assert f"entity: {GLOBEX_ENTITY}" in by_alias


def test_entity_text_over_the_real_service_absence_for_the_unknown(answer_indexed):
    conn, fx = answer_indexed
    brain = AnswerBrain(brain_service(conn, fx, "steward"))

    assert brain.entity_text("ghost-corp") == brain_mod.UNKNOWN_ENTITY
