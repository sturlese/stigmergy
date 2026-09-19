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
from stigmergy.knowledge.plan import (
    EntityProposal,
    ExistingPageRelation,
    FilingPlan,
    GraphEntity,
    GraphShape,
    GraphSubject,
    GraphTopology,
    PageMutation,
    RepairMutation,
    RepairPlan,
    expected_graph_mutations,
)


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


def _settings(*, max_turns=6):
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


def _filing_model(summary: str) -> FunctionModel:
    responses = iter(
        (
            {
                "summary": "No durable graph subject was supported.",
                "subjects": [],
                "existing_relations": [],
            },
            {
                "summary": "No durable graph subject was supported after review.",
                "subjects": [],
                "existing_relations": [],
            },
            {
                "summary": "No durable graph subject required enrichment.",
                "subjects": [],
                "existing_relations": [],
            },
            {
                "summary": "No durable graph subject remained after inventory review.",
                "subjects": [],
                "existing_relations": [],
            },
            {"summary": summary},
        )
    )
    return FunctionModel(
        lambda _messages, _info: ModelResponse(
            parts=[TextPart(json.dumps(next(responses)))]
        )
    )


def test_pydantic_planner_returns_a_typed_filing_plan_without_a_network_call(tmp_path):
    model = _filing_model("Filed the supported decision")
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
    assert result.model_requests == 5
    assert result.graph_shape is not None
    assert result.graph_shape.subjects == ()
    assert result.graph_shape_model_requests == 1
    assert result.graph_shape_review_model_requests == 1
    assert result.graph_shape_enrichment_model_requests == 2
    assert result.compilation_model_requests == 1
    assert result.semantic_reviewed is True


def _graph_shape() -> GraphShape:
    return GraphShape(
        summary="Preserve a practice separately from the system it improves.",
        subjects=(
            GraphSubject(
                title="Harness Engineering",
                title_evidence="Harness Engineering",
                name_variants=("Harness Engineering",),
                role="concept",
                abstraction="practice",
                abstraction_evidence="a practice for designing and iteratively improving",
                significance="The reusable practice for improving an agent harness.",
                required_terms=("six capabilities", "extensions"),
                entities=(
                    GraphEntity(
                        name="Santi",
                        entity_type="person",
                        aliases=("@santi",),
                        relationship_kind="authored",
                        relationship="Authored the source explanation.",
                        evidence_terms=(),
                    ),
                ),
            ),
            GraphSubject(
                title="Agent Harness",
                title_evidence="Agent Harness",
                name_variants=("Agent Harness",),
                role="concept",
                abstraction="system",
                abstraction_evidence="the operational layer around the model",
                significance="The runtime system improved by the practice.",
                required_terms=("runtime",),
                entities=(),
            ),
        ),
        existing_relations=(
            ExistingPageRelation(
                source_subject="Harness Engineering",
                path="wiki/concepts/Agent Harness.md",
                relation="distinct_related",
                reason="The practice improves the system but is not the system.",
            ),
        ),
    )


def test_graph_shape_derives_an_exact_title_update_without_collapsing_the_related_subject():
    assert expected_graph_mutations(_graph_shape()) == (
        {
            "subject": "Harness Engineering",
            "action": "create",
            "role": "concept",
            "path": None,
            "title": "Harness Engineering",
        },
        {
            "subject": "Agent Harness",
            "action": "update",
            "role": None,
            "path": "wiki/concepts/Agent Harness.md",
            "title": None,
        },
    )


def test_graph_shape_does_not_rewrite_a_context_page_only_to_make_a_link_reciprocal():
    shape = _graph_shape().model_copy(update={"subjects": (_graph_shape().subjects[0],)})

    assert expected_graph_mutations(shape) == (
        {
            "subject": "Harness Engineering",
            "action": "create",
            "role": "concept",
            "path": None,
            "title": "Harness Engineering",
        },
    )


def test_graph_shape_rejects_same_subject_without_exact_title_identity():
    with pytest.raises(ValueError, match="exact normalized title match"):
        GraphShape(
            summary="Unsafe identity collapse.",
            subjects=(_graph_shape().subjects[0],),
            existing_relations=(
                ExistingPageRelation(
                    source_subject="Harness Engineering",
                    path="wiki/concepts/Agent Harness.md",
                    relation="same_subject",
                    reason="They discuss a related topic.",
                ),
            ),
        )


def test_duplicate_enrichment_subjects_are_corrected_against_unique_topology():
    subject = _graph_shape().subjects[0]
    with pytest.raises(ValueError, match="unique titles"):
        GraphTopology(
            summary="Duplicate topology is invalid.",
            subjects=(subject, subject),
            existing_relations=(),
        )

    topology = GraphTopology(
        summary="One reviewed subject.",
        subjects=(subject,),
        existing_relations=(),
    )
    enriched = GraphShape(
        summary=topology.summary,
        subjects=(subject, subject),
        existing_relations=(),
    )

    assert planner.graph_topology_violations(topology, enriched) == (
        "graph-enrichment changed the reviewed subject set",
    )


@pytest.mark.parametrize("abstraction", ("person", "organization"))
def test_graph_topology_routes_identities_to_entity_enrichment(abstraction):
    with pytest.raises(ValueError, match="entity enrichment"):
        GraphSubject(
            title="Northstar Signal Lab" if abstraction == "organization" else "Mira Chen",
            title_evidence=(
                "Northstar Signal Lab" if abstraction == "organization" else "Mira Chen"
            ),
            name_variants=(
                "Northstar Signal Lab" if abstraction == "organization" else "Mira Chen",
            ),
            role="note",
            abstraction=abstraction,
            abstraction_evidence="researcher at Northstar Signal Lab",
            significance="Credited with source evidence.",
            required_terms=("source evidence",),
            entities=(),
        )


def test_graph_shape_gate_enforces_inventory_entities_and_reciprocal_links():
    plan = FilingPlan(
        summary="Created the practice and enriched its related system.",
        entities=(
            EntityProposal(
                name="Santi",
                entity_type="person",
                aliases=("@santi",),
            ),
        ),
        mutations=(
            PageMutation(
                action="create",
                role="concept",
                title="Harness Engineering",
                body=(
                        "# Harness Engineering\n\nThe six capabilities and extensions improve "
                        "the [[Agent Harness]]. [[Santi]] (@santi) authored the explanation."
                ),
                entities=("Santi",),
                reason="Created the reusable practice.",
            ),
            PageMutation(
                action="update",
                path="wiki/concepts/Agent Harness.md",
                body="# Agent Harness\n\nThe runtime is improved through [[Harness Engineering]].",
                entities=(),
                reason="Added the reciprocal relationship.",
            ),
        ),
    )

    assert planner.graph_shape_violations(_graph_shape(), plan) == ()
    incomplete = plan.model_copy(
        update={
            "mutations": (
                plan.mutations[0].model_copy(
                    update={"body": plan.mutations[0].body.replace(" and extensions", "")}
                ),
                plan.mutations[1],
            )
        }
    )
    assert any(
        violation.startswith("required-terms:Harness Engineering")
        for violation in planner.graph_shape_violations(_graph_shape(), incomplete)
    )


def test_graph_shape_gate_rejects_term_dumps_and_duplicate_page_bodies():
    harness, agent_harness = _graph_shape().subjects
    shape = GraphShape(
        summary="Keep the practice and system distinct.",
        subjects=(
            harness.model_copy(
                update={
                    "required_terms": (
                        "six capabilities",
                        "extensions",
                        "operating model",
                        "quality gates",
                        "feedback loops",
                    )
                }
            ),
            agent_harness,
        ),
        existing_relations=(),
    )
    dumped = (
        "six capabilities, extensions, operating model, quality gates, feedback loops.\n\n"
        "Santi (@santi) authored the explanation."
    )
    plan = FilingPlan(
        summary="Duplicated a lexical inventory.",
        entities=(
            EntityProposal(name="Santi", entity_type="person", aliases=("@santi",)),
        ),
        mutations=(
            PageMutation(
                action="create",
                role="concept",
                title="Harness Engineering",
                body=f"# Harness Engineering\n\n{dumped}",
                entities=("Santi",),
                reason="Created the practice.",
            ),
            PageMutation(
                action="create",
                role="concept",
                title="Agent Harness",
                body=f"# Agent Harness\n\n{dumped}",
                entities=(),
                reason="Created the system.",
            ),
        ),
    )

    violations = planner.graph_shape_violations(shape, plan)

    assert "required-term-inventory:Harness Engineering" in violations
    assert "duplicate-page-body:Harness Engineering:Agent Harness" in violations


def test_graph_enrichment_allows_shared_evidence_across_distinct_abstraction_levels():
    harness, agent_harness = _graph_shape().subjects
    shared_terms = (
        "tools",
        "feedback",
        "memory",
        "verification",
        "observability",
        "evaluation",
    )
    shape = GraphShape(
        summary="Keep practice evidence separate from system evidence.",
        subjects=(
            harness.model_copy(update={"required_terms": shared_terms + ("improvement",)}),
            agent_harness.model_copy(
                update={"required_terms": shared_terms + ("runtime", "interfaces")}
            ),
        ),
        existing_relations=(),
    )
    topology = GraphTopology.model_validate(
        {
            "summary": shape.summary,
            "subjects": [
                subject.model_dump(exclude={"required_terms", "entities"})
                for subject in shape.subjects
            ],
            "existing_relations": [],
        }
    )

    violations = planner.graph_topology_violations(topology, shape)

    assert violations == ()


def test_graph_enrichment_corrects_entity_evidence_missing_from_page_inventory():
    harness, agent_harness = _graph_shape().subjects
    shape = GraphShape(
        summary="Keep attributed evidence in the page inventory.",
        subjects=(
            harness,
            agent_harness.model_copy(
                update={
                    "entities": (
                        GraphEntity(
                            name="LangChain",
                            entity_type="organization",
                            aliases=(),
                            relationship_kind="produced_evidence",
                            relationship="Reported a benchmark rise from rank 30 to top 5.",
                            evidence_terms=("rank 30", "top 5"),
                        ),
                    )
                }
            ),
        ),
        existing_relations=(),
    )
    topology = GraphTopology.model_validate(
        {
            "summary": shape.summary,
            "subjects": [
                subject.model_dump(exclude={"required_terms", "entities"})
                for subject in shape.subjects
            ],
            "existing_relations": [],
        }
    )

    violations = planner.graph_topology_violations(topology, shape)

    assert violations == (
        "graph-enrichment entity evidence is not required page evidence: "
        "Agent Harness <> LangChain; missing=['rank 30', 'top 5']",
    )


def test_graph_enrichment_rejects_terms_not_copied_contiguously_from_source():
    shape = _graph_shape()
    topology = GraphTopology.model_validate(
        {
            "summary": shape.summary,
            "subjects": [
                subject.model_dump(exclude={"required_terms", "entities"})
                for subject in shape.subjects
            ],
            "existing_relations": [
                relation.model_dump(mode="json") for relation in shape.existing_relations
            ],
        }
    )

    violations = planner.graph_topology_violations(
        topology,
        shape,
        source_text="The source names six capabilities and the runtime system.",
    )

    assert violations == (
        "graph-enrichment required terms are not contiguous source spans: "
        "Harness Engineering; missing=['extensions']",
    )


def test_graph_enrichment_projection_restores_topology_and_preserves_valid_evidence():
    shape = _graph_shape()
    topology = GraphTopology.model_validate(
        {
            "summary": shape.summary,
            "subjects": [
                subject.model_dump(exclude={"required_terms", "entities"})
                for subject in shape.subjects
            ],
            "existing_relations": [
                relation.model_dump(mode="json") for relation in shape.existing_relations
            ],
        }
    )
    harness, agent_harness = shape.subjects
    evidence = GraphEntity(
        name="LangChain",
        entity_type="organization",
        aliases=(),
        relationship_kind="produced_evidence",
        relationship="Reported a benchmark rise from rank 30.",
        evidence_terms=("rank 30",),
    )
    invalid = GraphShape(
        summary="Fallible enrichment.",
        subjects=(
            harness.model_copy(
                update={
                    "significance": "Changed immutable field.",
                    "required_terms": ("six capabilities", "invented span"),
                }
            ),
            agent_harness,
            agent_harness.model_copy(
                update={"required_terms": ("not copied",), "entities": (evidence,)}
            ),
        ),
        existing_relations=(),
    )
    source_text = (
        "Harness Engineering defines six capabilities. The Agent Harness runtime improved from "
        "rank 30."
    )

    projected = planner._project_graph_enrichment(
        topology,
        invalid,
        source_text=source_text,
    )

    assert [subject.title for subject in projected.subjects] == [
        "Harness Engineering",
        "Agent Harness",
    ]
    assert projected.subjects[0].significance == topology.subjects[0].significance
    assert projected.subjects[0].required_terms == ("six capabilities",)
    assert projected.subjects[1].required_terms == ("runtime", "rank 30")
    assert [entity.name for entity in projected.subjects[1].entities] == ["LangChain"]
    assert projected.existing_relations == topology.existing_relations
    assert planner.graph_topology_violations(
        topology,
        projected,
        source_text=source_text,
    ) == ()


def test_graph_shape_gate_requires_local_citations_for_each_factual_block():
    source_path = "sources/2026/08/capture.md"
    body = (
        "# Harness Engineering\n\n"
        "## Definition\n"
        "An uncited source-grounded definition.\n\n"
        "## Evidence\n"
        f"A cited result. (Source: `{source_path}`)\n\n"
        "## Connections\n"
        "- [[Agent Harness]] is the system this practice improves.\n"
    )

    assert planner._uncited_factual_blocks(body, source_path) == (
        "An uncited source-grounded definition.",
    )


def test_graph_shape_gate_rejects_a_duplicated_entity_name_prefix():
    plan = FilingPlan(
        summary="Filed the practice and its related system.",
        entities=(EntityProposal(name="Santi", entity_type="person", aliases=("@santi",)),),
        mutations=(
            PageMutation(
                action="create",
                role="concept",
                title="Harness Engineering",
                body=("# Harness Engineering\n\nThe six capabilities and extensions improve "
                      "the [[Agent Harness]]. Santi (@santi) Santi authored the explanation."),
                entities=("Santi",),
                reason="Created the reusable practice.",
            ),
            PageMutation(
                action="update",
                path="wiki/concepts/Agent Harness.md",
                body="# Agent Harness\n\nThe runtime is improved through [[Harness Engineering]].",
                reason="Added the reciprocal relationship.",
            ),
        ),
    )

    violations = planner.graph_shape_violations(_graph_shape(), plan)

    assert "duplicate-entity-name:Harness Engineering:Santi" in violations


def test_graph_compilation_prompt_allows_concise_shared_context_for_cold_readers():
    prompt = planner._prompt(
        envelope=_envelope(),
        source_path="sources/2026/08/capture.md",
        source_text="Harness Engineering improves an Agent Harness.",
        context='{"candidates": []}',
        graph_shape=_graph_shape(),
    )

    assert "may paraphrase concise source-supported context needed for a cold reader" in prompt
    assert "detailed mechanisms, inventories, entities, and examples belong" in prompt


def test_update_graph_gate_accepts_a_preserved_distinct_local_source_citation():
    prior_source = "sources/2026/07/prior.md"

    assert planner._uncited_factual_blocks(
        f"Prior supported context. (Source: `{prior_source}`)",
        "sources/2026/08/current.md",
        allow_any_source=True,
    ) == ()


def test_cited_list_supports_its_lead_in_but_not_an_uncited_sibling_item():
    source_path = "sources/2026/08/capture.md"
    cited = f"(Source: `{source_path}`)"
    body = (
        "# Agent Harness\n\n"
        "## How It Works\n"
        "The harness has two capabilities:\n\n"
        f"- Tools request actions. {cited}\n"
        f"- Memory preserves work. {cited}\n\n"
        "A partially cited list follows:\n\n"
        f"- Observability preserves traces. {cited}\n"
        "- Evals catch regressions.\n"
    )

    assert planner._uncited_factual_blocks(body, source_path) == (
        "A partially cited list follows:",
        "- Evals catch regressions.",
    )


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
    assert captured["prompt"].rfind("FINAL REVIEW CHECKLIST") > captured["prompt"].find(
        "DRAFT FILING PLAN"
    )
    assert "every durable primary subject" in captured["prompt"]
    assert "practice remains distinct from the system or artifact it designs" in captured["prompt"]
    assert "framework members" in captured["prompt"]
    assert "stable name" in captured["prompt"]
    assert "every entity listed on a mutation" in captured["prompt"]
    assert "supported extensions" in captured["prompt"]
    assert "material quantitative outcomes" in captured["prompt"]
    assert "source-supplied handle" in captured["prompt"]
    assert "make links reciprocal" in captured["prompt"]
    assert "exact action schema" in captured["prompt"]


def test_planner_rejects_a_prompt_over_its_byte_budget(monkeypatch):
    monkeypatch.setattr(planner, "MAX_PLANNER_PROMPT_BYTES", 10)

    with pytest.raises(ValueError, match="byte limit"):
        planner._prompt(
            envelope=_envelope(),
            source_path="sources/2026/08/capture.md",
            source_text="long source text",
            context="",
        )
def test_graph_phase_instruction_does_not_conflict_with_requested_schema():
    assert "requested structured graph" in planner._GRAPH_SHAPE_INSTRUCTIONS
    assert "Return only the requested GraphShape" not in planner._GRAPH_SHAPE_INSTRUCTIONS
