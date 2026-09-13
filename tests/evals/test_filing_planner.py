import hashlib
import json
from pathlib import Path

import pytest

from evals.filing import planner_eval, run_planner
from evals.filing import worktree as eval_worktree
from stigmergy.knowledge.plan import (
    EntityProposal,
    FilingPlan,
    PageMutation,
    RepairMutation,
    RepairPlan,
)
from stigmergy.knowledge.planner import PlanRun
from stigmergy.knowledge.relationships import has_entity_relationship_evidence

ROOT = Path(__file__).resolve().parents[2]
CASE = ROOT / "evals" / "filing" / "cases" / "harness_engineering.json"
SEEDED_CASE = ROOT / "evals" / "filing" / "cases" / "harness_engineering_seeded.json"
FIXTURE = ROOT / "evals" / "filing" / "fixtures" / "harness_engineering_synthetic.md"
DECISION_TRACE_CASE = ROOT / "evals" / "filing" / "cases" / "decision_trace_quality.json"
DECISION_TRACE_FIXTURE = ROOT / "evals" / "filing" / "fixtures" / "decision_trace_quality.md"
SOURCE = "sources/2026/09/00000000-0000-4000-8000-000000000001.md"
HARNESS_BODY = (
    "# Harness Engineering\n\n"
    "A model alone is not an agentic system: the model supplies reasoning while the "
    "[[Agent Harness]] turns its text into work. The six capabilities are tools let the model "
    "request actions; a loop repeats decide, act, observe; memory/state preserves work; context "
    "selection chooses what the model sees; a working environment provides an isolated workspace; "
    "and a clear objective and verification establish external acceptance criteria.\n\n"
    "The three trust capabilities are permissions and limits, observability through traces of "
    "context and outcomes, and evals using stable evaluation tasks. Skills, MCP, subagents, and "
    "long-term memory extend the same design. Santi (@santtiagom_) authored this explanation. "
    "OpenAI built one million lines and 1,500 pull requests; LangChain rose from rank 30 to the "
    "top 5 on Terminal Bench; Anthropic showed a polished but broken application versus a working "
    f"app under different harness configurations. (Source: `{SOURCE}`)"
)


def _plan(*, entities=(), links=()):
    return FilingPlan(
        summary="Filed synthetic Harness Engineering knowledge",
        entities=tuple(EntityProposal(name=name, entity_type="organization") for name in entities),
        mutations=(
            PageMutation(
                action="create",
                role="concept",
                title="Harness Engineering",
                body=(
                    f"{HARNESS_BODY}\n\nClaude Code and Codex are coding-agent environments named "
                    f"by the source. (Source: `{SOURCE}`)"
                ),
                entities=tuple(links),
                reason="The source explains the concept",
            ),
        ),
    )


def _decision_trace_plan(*, entities=(), links=()):
    types = {"Mira Chen": "person", "Northstar Signal Lab": "organization"}
    return FilingPlan(
        summary="Filed synthetic Decision Trace Quality knowledge",
        entities=tuple(
            EntityProposal(name=name, entity_type=types.get(name, "organization"))
            for name in entities
        ),
        mutations=(
            PageMutation(
                action="create",
                role="concept",
                title="Decision Trace Quality",
                body=(
                    "# Decision Trace Quality\n\nDecision trace quality records the evidence behind "
                    "a decision so later readers can inspect the result. Mira Chen authored the method "
                    "and Northstar Signal Lab measured its use. (Source: "
                    "`sources/2026/09/00000000-0000-4000-8000-000000000002.md`)"
                ),
                entities=tuple(links),
                reason="The source explains the concept.",
            ),
        ),
    )


def test_decision_trace_quality_requires_the_author_and_attributed_organization():
    case = planner_eval.load_case(DECISION_TRACE_CASE)
    plan = _decision_trace_plan(
        entities=("Mira Chen", "Northstar Signal Lab"),
        links=("Mira Chen", "Northstar Signal Lab"),
    )

    result = planner_eval.score(
        plan,
        case,
        source_text=DECISION_TRACE_FIXTURE.read_text(encoding="utf-8"),
    )

    assert result["mutations"]["passed"] is True
    assert result["identity_proposals"]["found"] == ["mira chen", "northstar signal lab"]
    assert result["entity_links"]["found"] == ["mira chen", "northstar signal lab"]
    assert result["external_ids"]["found"] == []
    assert result["link_coverage"]["unlinked_proposals"] == []
    assert result["passed"] is True, result


@pytest.mark.parametrize(
    "technical_term",
    ("OrbitBench", "Recall at Five", "Paired Holdout Review", "VectorCache"),
)
def test_decision_trace_quality_rejects_technical_terms_as_identities_and_links(technical_term):
    case = planner_eval.load_case(DECISION_TRACE_CASE)
    plan = _decision_trace_plan(
        entities=("Mira Chen", "Northstar Signal Lab", technical_term),
        links=("Mira Chen", "Northstar Signal Lab", technical_term),
    )

    result = planner_eval.score(
        plan,
        case,
        source_text=DECISION_TRACE_FIXTURE.read_text(encoding="utf-8"),
    )

    expected = technical_term.casefold()
    assert result["identity_proposals"]["forbidden_present"] == [expected]
    assert result["entity_links"]["forbidden_present"] == [expected]
    assert result["passed"] is False


def test_harness_score_separates_identity_proposals_from_deliberate_links():
    case = planner_eval.load_case(CASE)
    plan = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain"),
    )
    unlinked = plan.mutations[0].model_copy(update={"entities": ("Santi",)})
    plan = plan.model_copy(update={"mutations": (unlinked,)})

    result = planner_eval.score(plan, case)

    assert result["mutations"]["passed"] is True
    assert result["identity_proposals"]["passed"] is True
    assert result["entity_links"]["missing"] == ["anthropic", "langchain", "openai"]
    assert result["passed"] is False


def test_harness_score_rejects_duplicate_pages_without_imposing_a_one_page_quota():
    case = planner_eval.load_case(CASE)
    plan = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain"),
    )
    duplicate = plan.model_copy(update={"mutations": plan.mutations * 2})

    result = planner_eval.score(duplicate, case)

    assert result["mutations"]["actual_count"] == 2
    assert result["mutations"]["passed"] is False
    assert result["passed"] is False


def test_empty_graph_case_rejects_a_redundant_split_without_using_page_count_as_a_proxy():
    case = planner_eval.load_case(CASE)
    case["anti_fragmentation"] = {
        "redundant_title_groups": [["Harness Engineering", "Harness Engineering Explained"]]
    }
    plan = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain"),
    )
    redundant = PageMutation(
        action="create",
        role="concept",
        title="Harness Engineering Explained",
        body=(
            "# Harness Engineering Explained\n\nThis restates harness engineering rather than "
            "adding independently reusable knowledge. (Source: "
            "`sources/2026/09/00000000-0000-4000-8000-000000000001.md`)"
        ),
        entities=(),
        reason="Redundant split fixture.",
    )

    result = planner_eval.score(plan.model_copy(update={"mutations": (*plan.mutations, redundant)}), case)

    assert result["anti_fragmentation"]["passed"] is False
    assert result["anti_fragmentation"]["fragmented_groups"] == [
        ["harness engineering", "harness engineering explained"]
    ]
    assert result["passed"] is False


def test_seeded_harness_case_requires_two_reciprocally_connected_reusable_pages():
    case = planner_eval.load_case(SEEDED_CASE)
    source = "sources/2026/09/00000000-0000-4000-8000-000000000001.md"
    entities = tuple(
        EntityProposal(name=name, entity_type="organization")
        for name in ("Santi", "OpenAI", "Anthropic", "LangChain")
    )
    plan = FilingPlan(
        summary="Enriched the existing harness graph with a new reusable concept.",
        entities=entities,
        mutations=(
            PageMutation(
                action="create", role="concept", title="Harness Engineering",
                body=HARNESS_BODY,
                entities=("Santi", "OpenAI", "Anthropic", "LangChain"), reason="New reusable concept.",
            ),
            PageMutation(
                action="update", path="wiki/concepts/Agent Harness.md", title="Ignored model label",
                body=("# Agent Harness\n\nAn agent harness is the operating layer enriched by "
                      "[[Harness Engineering]]. Santi, OpenAI, Anthropic, and LangChain illustrate its "
                      f"leverage. (Source: `{source}`)"),
                entities=("Santi", "OpenAI", "Anthropic", "LangChain"), reason="Existing concept gains evidence.",
            ),
        ),
    )

    result = planner_eval.score(plan, case)

    assert result["connections"]["passed"] is True
    assert result["bodies"]["passed"] is True
    assert result["entity_relationships"]["passed"] is True
    assert result["passed"] is True


def test_seeded_evaluation_worktree_exposes_the_real_candidate_and_runs_writer_gates():
    case = planner_eval.load_case(SEEDED_CASE)
    source_text = FIXTURE.read_text(encoding="utf-8")
    plan = FilingPlan(
        summary="Updated the seeded concept with current source evidence.",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/concepts/Agent Harness.md",
                body=(
                    "# Agent Harness\n\nAn agent harness supplies the task loop, tools, feedback, "
                    "and memory around a model; this source retains that durable definition. "
                    "(Source: `sources/2026/09/00000000-0000-4000-8000-000000000001.md`)"
                ),
                reason="The current source supports the existing reusable concept.",
            ),
        ),
    )

    with eval_worktree.prepared(case, source_text, template=str(run_planner.DEFAULT_WORKTREE)) as root:
        gates = eval_worktree.apply_and_gate(root, plan)

        assert "Agent Harness" in root.context
        assert gates["passed"] is True


def test_harness_score_rejects_heading_only_body_even_with_expected_entities():
    case = planner_eval.load_case(CASE)
    plan = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain"),
    )
    empty = plan.model_copy(update={"mutations": (plan.mutations[0].model_copy(
        update={"body": "# Harness Engineering"}
    ),)})

    result = planner_eval.score(empty, case)

    assert result["bodies"]["placeholders"] == ["Harness Engineering"]
    assert result["passed"] is False


def test_harness_score_requires_the_complete_framework_not_a_generic_summary():
    case = planner_eval.load_case(CASE)
    plan = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain"),
    )
    incomplete = plan.model_copy(update={"mutations": (plan.mutations[0].model_copy(
        update={"body": "# Harness Engineering\n\nA model alone is not an agentic system. "
               f"(Source: `{SOURCE}`)"}
    ),)})

    result = planner_eval.score(incomplete, case)

    assert "capability: tools" in result["bodies"]["missing_required_any"]
    assert "trust: evals" in result["bodies"]["missing_required_any"]
    assert "author attribution" in result["bodies"]["missing_required_any"]
    assert result["passed"] is False


def test_harness_score_accepts_unicode_dash_variants_in_semantic_requirements():
    case = planner_eval.load_case(CASE)
    plan = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain"),
    )
    nonbreaking_hyphen = plan.model_copy(update={"mutations": (plan.mutations[0].model_copy(
        update={"body": plan.mutations[0].body.replace("long-term memory", "long‑term memory")}
    ),)})

    result = planner_eval.score(nonbreaking_hyphen, case)

    assert result["bodies"]["missing_required_any"] == []
    assert result["passed"] is True


def test_harness_score_rejects_entity_name_wikilinks_without_normal_pages():
    case = planner_eval.load_case(CASE)
    plan = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain"),
    )
    linked_entity = plan.model_copy(update={"mutations": (plan.mutations[0].model_copy(
        update={"body": plan.mutations[0].body.replace("OpenAI built", "[[OpenAI]] built")}
    ),)})

    result = planner_eval.score(linked_entity, case)

    assert result["entity_wikilinks"]["violations"] == [
        {"mutation": "Harness Engineering", "target": "OpenAI"}
    ]
    assert result["passed"] is False


def test_harness_score_rejects_a_post_identifier_as_a_page():
    case = planner_eval.load_case(CASE)
    plan = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain"),
    )
    resource_note = PageMutation(
        action="create",
        role="note",
        title="2098782814837543075",
        body=f"# 2098782814837543075\n\nA source identifier. (Source: `{SOURCE}`)",
        entities=(),
        reason="Incorrectly treated a post identifier as knowledge.",
    )

    result = planner_eval.score(plan.model_copy(update={"mutations": (*plan.mutations, resource_note)}), case)

    assert result["mutations"]["forbidden_present"] == [
        {"action": "create", "role": "note", "title": "2098782814837543075"}
    ]
    assert result["passed"] is False


def test_harness_score_rejects_framework_categories_as_synthetic_child_concepts():
    case = planner_eval.load_case(CASE)
    plan = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain"),
    )
    category_page = PageMutation(
        action="create",
        role="concept",
        title="Harness Engineering Capability Framework",
        body=("# Harness Engineering Capability Framework\n\nA category from the parent framework. "
              f"(Source: `{SOURCE}`)"),
        entities=(),
        reason="Incorrectly split a parent-framework category into a child concept.",
    )

    result = planner_eval.score(plan.model_copy(update={"mutations": (*plan.mutations, category_page)}), case)

    assert any(
        item["title"] == "harness engineering capability framework"
        for item in result["mutations"]["forbidden_present"]
    )
    assert result["passed"] is False


def test_harness_score_requires_each_page_anchor_to_have_its_own_cited_relationship():
    case = planner_eval.load_case(CASE)
    identities = ("Santi", "OpenAI", "Anthropic", "LangChain")
    plan = _plan(entities=identities, links=identities)
    copied_anchors = PageMutation(
        action="update",
        path="wiki/concepts/Agent Harness.md",
        body=f"# Agent Harness\n\nA related normal page. (Source: `{SOURCE}`)",
        entities=identities,
        reason="Incorrectly copied anchors without page-specific evidence.",
    )

    result = planner_eval.score(plan.model_copy(update={"mutations": (*plan.mutations, copied_anchors)}), case)

    assert result["entity_relationships"]["missing"]
    assert result["passed"] is False


def test_entity_relationship_accepts_an_immediately_following_collective_attribution():
    body = (
        "## Examples\n\n"
        "- **OpenAI** built a large internal software product through an agent harness.\n\n"
        f"These examples support the conclusion. (Source: `{SOURCE}`)"
    )

    assert has_entity_relationship_evidence(body, "OpenAI", (SOURCE,)) is True
    assert has_entity_relationship_evidence(
        body.replace("\n\nThese examples", "\n\nUnrelated analysis.\n\nThese examples"),
        "OpenAI",
        (SOURCE,),
    ) is False
    assert has_entity_relationship_evidence(
        f"**Claude\u202fCode** is a coding-agent environment. (Source: `{SOURCE}`)",
        "Claude Code",
        (SOURCE,),
    ) is True


def test_seeded_harness_score_uses_the_update_path_not_model_create_fields():
    case = planner_eval.load_case(SEEDED_CASE)
    identities = tuple(
        EntityProposal(name=name, entity_type="organization")
        for name in ("Santi", "OpenAI", "Anthropic", "LangChain")
    )
    plan = FilingPlan(
        summary="Created the central discipline and connected its adjacent artifact.",
        entities=identities,
        mutations=(
            PageMutation(
                action="create",
                role="concept",
                title="Harness Engineering",
                body=HARNESS_BODY,
                entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
                reason="Created the source-named central discipline.",
            ),
            PageMutation(
                action="update",
                path="wiki/concepts/Agent Harness.md",
                role="note",
                title="Incorrect model title",
                body=(
                    "# Agent Harness\n\nAn agent harness is the artifact designed and improved by "
                    "[[Harness Engineering]]. Santi, OpenAI, Anthropic, and LangChain provide the "
                    f"source evidence for that relationship. (Source: `{SOURCE}`)"
                ),
                entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
                reason="Added the reciprocal relationship to the central discipline.",
            ),
        ),
    )

    result = planner_eval.score(plan, case)

    assert result["mutations"]["passed"] is True
    assert result["bodies"]["heading_mismatch"] == []
    assert result["passed"] is True


def test_harness_score_allows_optional_coding_environments_and_rejects_react():
    case = planner_eval.load_case(CASE)
    passed = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain", "Claude Code", "Codex"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain", "Claude Code", "Codex"),
    )
    rejected = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain", "ReAct"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain", "ReAct"),
    )

    passed_result = planner_eval.score(passed, case)
    rejected_result = planner_eval.score(rejected, case)

    assert passed_result["passed"] is True
    assert passed_result["identity_proposals"]["allowed_present"] == ["claude code", "codex"]
    assert rejected_result["identity_proposals"]["forbidden_present"] == ["react"]
    assert rejected_result["entity_links"]["forbidden_present"] == ["react"]
    assert rejected_result["passed"] is False


def test_harness_score_rejects_unexpected_zoom_identity_and_link():
    case = planner_eval.load_case(CASE)
    plan = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain", "Zoom"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain", "Zoom"),
    )

    result = planner_eval.score(plan, case)

    assert result["identity_proposals"]["unexpected"] == ["zoom"]
    assert result["entity_links"]["unexpected"] == ["zoom"]
    assert result["passed"] is False


def test_harness_score_rejects_an_allowed_optional_identity_without_a_link():
    case = planner_eval.load_case(CASE)
    plan = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain", "Codex"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain", "Codex"),
    )
    unlinked = plan.mutations[0].model_copy(
        update={"entities": ("Santi", "OpenAI", "Anthropic", "LangChain")}
    )
    plan = plan.model_copy(update={"mutations": (unlinked,)})

    result = planner_eval.score(plan, case)

    assert result["identity_proposals"]["allowed_present"] == ["codex"]
    assert result["link_coverage"]["unlinked_proposals"] == ["codex"]
    assert result["passed"] is False


def test_harness_score_rejects_an_unrelated_non_delete_mutation():
    case = planner_eval.load_case(CASE)
    plan = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain"),
    )
    unrelated = PageMutation(
        action="create",
        role="note",
        title="Unrelated Zoom decision",
        body="# Unrelated Zoom decision\n\nUnrelated knowledge.",
        entities=(),
        reason="The source contains a different page.",
    )
    plan = plan.model_copy(update={"mutations": (*plan.mutations, unrelated)})

    result = planner_eval.score(plan, case)

    assert result["mutations"]["actual_count"] == 2
    assert result["mutations"]["unexpected"] == [
        {"action": "create", "role": "note", "title": "unrelated zoom decision"}
    ]
    assert result["passed"] is False


def test_harness_score_rejects_an_unrelated_delete_mutation():
    case = planner_eval.load_case(CASE)
    plan = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain"),
    )
    unrelated = PageMutation(
        action="delete",
        path="wiki/notes/Unrelated.md",
        reason="The source requests an unrelated deletion.",
    )
    plan = plan.model_copy(update={"mutations": (*plan.mutations, unrelated)})

    result = planner_eval.score(plan, case)

    assert result["mutations"]["actual_count"] == 2
    assert result["mutations"]["unexpected"] == [
        {"action": "delete", "role": None, "title": "wiki notes unrelated md"}
    ]
    assert result["passed"] is False


def test_harness_score_reports_a_shared_selected_alias_as_ambiguous():
    case = planner_eval.load_case(CASE)
    plan = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain", "VSW"),
    ).model_copy(
        update={
            "entities": (
                EntityProposal(name="Santi", entity_type="person"),
                EntityProposal(name="OpenAI", entity_type="organization"),
                EntityProposal(name="Anthropic", entity_type="organization"),
                EntityProposal(name="LangChain", entity_type="organization"),
                EntityProposal(
                    name="Velorum Signal Works",
                    entity_type="organization",
                    aliases=("VSW",),
                ),
                EntityProposal(
                    name="Valence Switching Works",
                    entity_type="organization",
                    aliases=("VSW",),
                ),
            )
        }
    )

    result = planner_eval.score(plan, case, source_text="VSW is named in this synthetic source.")

    assert result["reference_resolution"]["ambiguous"] == [
        {
            "reference": "vsw",
            "candidates": ["velorum signal works", "valence switching works"],
        }
    ]
    assert result["reference_resolution"]["passed"] is False


def test_harness_score_reports_a_selected_reference_with_no_candidate():
    case = planner_eval.load_case(CASE)
    plan = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain", "Unknown Vendor"),
    )

    result = planner_eval.score(plan, case)

    assert result["reference_resolution"]["unresolved"] == ["unknown vendor"]
    assert result["reference_resolution"]["passed"] is False
    assert result["passed"] is False


def test_harness_score_rejects_an_alias_absent_from_the_source():
    case = planner_eval.load_case(CASE)
    plan = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain"),
    ).model_copy(
        update={
            "entities": (
                EntityProposal(name="Santi", entity_type="person", aliases=("Santti",)),
                EntityProposal(name="OpenAI", entity_type="organization"),
                EntityProposal(name="Anthropic", entity_type="organization"),
                EntityProposal(name="LangChain", entity_type="organization"),
            )
        }
    )

    result = planner_eval.score(plan, case, source_text=FIXTURE.read_text(encoding="utf-8"))

    assert result["alias_evidence"] == {
        "missing": [{"proposal": "Santi", "alias": "Santti"}],
        "passed": False,
    }
    assert result["passed"] is False


def test_harness_score_rejects_an_external_id_inferred_from_the_post_identifier():
    case = planner_eval.load_case(CASE)
    plan = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain"),
    ).model_copy(
        update={
            "entities": (
                EntityProposal(
                    name="Santi",
                    entity_type="person",
                    external_namespace="x",
                    external_id="2098782814837543075",
                ),
                EntityProposal(name="OpenAI", entity_type="organization"),
                EntityProposal(name="Anthropic", entity_type="organization"),
                EntityProposal(name="LangChain", entity_type="organization"),
            )
        }
    )

    result = planner_eval.score(plan, case, source_text=FIXTURE.read_text(encoding="utf-8"))

    assert result["external_ids"]["unexpected"] == ["x:2098782814837543075"]
    assert result["passed"] is False


def test_harness_score_requires_santi_as_the_canonical_name_not_only_an_alias():
    case = planner_eval.load_case(CASE)
    plan = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain"),
    ).model_copy(
        update={
            "entities": (
                EntityProposal(name="Santiago", entity_type="person", aliases=("Santi",)),
                EntityProposal(name="OpenAI", entity_type="organization"),
                EntityProposal(name="Anthropic", entity_type="organization"),
                EntityProposal(name="LangChain", entity_type="organization"),
            )
        }
    )

    result = planner_eval.score(plan, case, source_text=FIXTURE.read_text(encoding="utf-8"))

    assert result["identity_proposals"]["missing"] == ["santi"]
    assert result["identity_proposals"]["unexpected"] == ["santiago"]
    assert result["passed"] is False


@pytest.mark.parametrize(
    (
        "turn_arguments",
        "expected_max_turns",
        "reasoning_arguments",
        "expected_reasoning",
        "expected_mode",
        "include_payload",
    ),
    (
        ((), 2, (), "high", "production-equivalent", False),
        (("--max-turns", "3", "--execution-mode", "planner-only"), 3, (), "high", "planner-only", True),
        ((), 2, ("--reasoning-level", "low"), "low", "production-equivalent", False),
    ),
)
def test_cli_uses_the_production_request_budget_and_preserves_an_explicit_turn_override(
    tmp_path,
    monkeypatch,
    capsys,
    turn_arguments,
    expected_max_turns,
    reasoning_arguments,
    expected_reasoning,
    expected_mode,
    include_payload,
):
    source_text = FIXTURE.read_text(encoding="utf-8")
    worktree = tmp_path / "worktree"
    skill = worktree / ".claude" / "skills" / "librarian" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Synthetic librarian skill.\n", encoding="utf-8")
    calls = []

    class RecordingPlanner:
        def __init__(self, settings, *, model_factory=None):
            calls.append({"settings": settings, "model_factory": model_factory})

        def plan(self, **kwargs):
            calls.append(kwargs)
            base = _plan(
                entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
                links=("Santi", "OpenAI", "Anthropic", "LangChain"),
            )
            return PlanRun(
                base.model_copy(
                    update={
                        "mutations": (
                            base.mutations[0].model_copy(
                                update={
                                    "body": base.mutations[0].body.replace(
                                        "[[Agent Harness]]", "the agent harness"
                                    )
                                }
                            ),
                        )
                    }
                ),
                model_requests=1,
            )

    monkeypatch.setattr(run_planner, "PydanticPlanner", RecordingPlanner)

    class Model:
        def __init__(self, settings):
            self.settings = settings

    def build_librarian_model(model_name):
        assert model_name == "openrouter:openai/gpt-5.4"
        settings = {
            "openrouter_provider": {"only": ["azure"], "allow_fallbacks": False},
            "openrouter_reasoning": {"effort": "high", "exclude": True},
        }
        return Model(settings), settings

    monkeypatch.setattr(run_planner, "build_model", build_librarian_model)
    monkeypatch.setattr(
        run_planner,
        "librarian_skill_provenance",
        lambda _worktree: {
            "commit": "0123456789abcdef0123456789abcdef01234567",
            "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        },
    )

    exit_code = run_planner.main(
        [
            "--live",
            "--source",
            str(FIXTURE),
            "--case",
            str(CASE),
            "--worktree",
            str(worktree),
            *turn_arguments,
            *reasoning_arguments,
            *(("--include-payload",) if include_payload else ()),
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert calls[0]["settings"].repo != str(worktree)
    assert calls[0]["settings"].max_turns == expected_max_turns
    assert calls[0]["settings"].model == "openrouter:openai/gpt-5.4"
    assert source_text in calls[1]["source_text"]
    assert calls[1]["source_path"] == "sources/2026/09/00000000-0000-4000-8000-000000000001.md"
    assert '"candidates": []' in calls[1]["context"]
    assert payload["case"] == "harness-engineering"
    assert payload["case_id"] == "harness_engineering"
    assert payload["execution_mode"] == expected_mode
    assert payload["configured_max_turns"] == expected_max_turns
    assert payload["model_requests"] == 1
    assert payload["planning_model_requests"] == 1
    assert payload["repair_model_requests"] == 0
    assert payload["schema_retry_count"] == 0
    assert payload["semantic_repair_count"] == 0
    assert payload["usage"] == {"requests": 1}
    assert payload["runtime"] == {
        "model": "openai/gpt-5.4",
        "reasoning_level": expected_reasoning,
        "provider": "azure",
    }
    assert payload["score"]["passed"] is True
    assert payload["gates"]["passed"] is True
    assert set(payload["raw_gates"]) == (set(payload["score"]) - {"passed"}) | {"writer"}
    assert "plan" not in payload
    assert "effective_plan" not in payload
    if include_payload:
        assert "payload" in payload["case_result"]
        assert "--include-payload emits derived page bodies" in captured.err
        assert payload["output"]["sha256"] == hashlib.sha256(
            json.dumps(
                payload["case_result"]["payload"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    else:
        assert "payload" not in payload["case_result"]
        assert not captured.err
    assert payload["output"]["artifact_ref"] == f"sha256:{payload['output']['sha256']}"
    expected_case_result = {
        key: payload[key]
        for key in (
                "brain_prompt", "case_id", "case_sha256", "fixture_sha256", "runtime",
                "execution_mode", "configured_max_turns", "model_requests",
            "planning_model_requests", "repair_model_requests", "schema_retry_count",
            "semantic_repair_count", "elapsed_ms", "usage", "score",
            "gates", "raw_gates", "output",
        )
    }
    if include_payload:
        expected_case_result["payload"] = payload["case_result"]["payload"]
    assert payload["case_result"] == expected_case_result
    factory = calls[0]["model_factory"]
    if reasoning_arguments:
        assert factory is not None
        assert factory().settings["openrouter_reasoning"] == {
            "effort": expected_reasoning,
            "exclude": True,
        }
    else:
        assert factory is None


def test_production_equivalent_worktree_records_a_bounded_semantic_repair_attempt():
    case = planner_eval.load_case(CASE)
    source_text = FIXTURE.read_text(encoding="utf-8")
    missing_local_citation = FilingPlan(
        summary="Exercise the bounded repair path.",
        mutations=(
            PageMutation(
                action="create",
                role="concept",
                title="Harness Engineering",
                body="# Harness Engineering\n\nA source-grounded concept without the required local citation.",
                entities=(),
                reason="A deliberately invalid evaluation fixture.",
            ),
        ),
    )

    class RepairingPlanner:
        def repair(self, **kwargs):
            assert kwargs["max_requests"] == 1
            return PlanRun(RepairPlan(summary="No repair supplied."), model_requests=1)

    with eval_worktree.prepared(case, source_text, template=str(run_planner.DEFAULT_WORKTREE)) as worktree:
        result = eval_worktree.apply_with_production_repair(
            worktree,
            missing_local_citation,
            RepairingPlanner(),
            planning_model_requests=1,
            max_turns=2,
        )

    assert result["passed"] is False
    assert result["semantic_repair_count"] == 1
    assert result["repair_model_requests"] == 1


def test_production_equivalent_worktree_exposes_safe_repair_rejection_shape():
    case = planner_eval.load_case(CASE)
    source_text = FIXTURE.read_text(encoding="utf-8")
    missing_local_citation = FilingPlan(
        summary="Exercise the bounded repair path.",
        mutations=(
            PageMutation(
                action="create",
                role="concept",
                title="Harness Engineering",
                body="# Harness Engineering\n\nA source-grounded concept without the required local citation.",
                entities=(),
                reason="A deliberately invalid evaluation fixture.",
            ),
        ),
    )

    class MisroutingPlanner:
        def repair(self, **_kwargs):
            return PlanRun(
                RepairPlan(
                    summary="Invalid repair target.",
                    mutations=(
                        RepairMutation(
                            path="wiki/entities/ent_11111111-1111-4111-8111-111111111111.md",
                            body="not emitted",
                            reason="invalid target fixture",
                        ),
                    ),
                ),
                model_requests=1,
            )

    with eval_worktree.prepared(case, source_text, template=str(run_planner.DEFAULT_WORKTREE)) as worktree:
        result = eval_worktree.apply_with_production_repair(
            worktree,
            missing_local_citation,
            MisroutingPlanner(),
            planning_model_requests=1,
            max_turns=2,
        )

    assert result["repair_rejection"] == "model repair targeted a path outside its violations"
    assert result["repair_mutation_shape"] == [
        {"target_kind": "entity", "body_bytes": 11, "reason_bytes": 22}
    ]


def test_production_equivalent_worktree_scores_the_final_repaired_body():
    case = planner_eval.load_case(CASE)
    source_text = FIXTURE.read_text(encoding="utf-8")
    base = _plan(
        entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
        links=("Santi", "OpenAI", "Anthropic", "LangChain"),
    )
    valid_body = base.mutations[0].body.replace("[[Agent Harness]]", "the agent harness")
    broken = base.model_copy(
        update={
            "mutations": (
                base.mutations[0].model_copy(
                    update={"body": valid_body.replace(f"(Source: `{SOURCE}`)", "")}
                ),
            )
        }
    )

    class CorrectingPlanner:
        def repair(self, **kwargs):
            path, body = next(iter(kwargs["files"].items()))
            repaired = body.replace(
                "app under different harness configurations. ",
                f"app under different harness configurations. (Source: `{SOURCE}`) ",
            )
            return PlanRun(
                RepairPlan(
                    summary="Restored local provenance.",
                    mutations=(
                        RepairMutation(
                            path=path,
                            body=repaired,
                            reason="Restored a local source reference.",
                        ),
                    ),
                ),
                model_requests=1,
            )

    with eval_worktree.prepared(case, source_text, template=str(run_planner.DEFAULT_WORKTREE)) as worktree:
        result = eval_worktree.apply_with_production_repair(
            worktree,
            broken,
            CorrectingPlanner(),
            planning_model_requests=1,
            max_turns=2,
        )
        final = eval_worktree.effective_plan(worktree, broken)

    assert result["passed"] is True, result
    assert planner_eval.score(final, case, source_text=source_text)["passed"] is True


def test_cli_requires_live_acknowledgement_before_it_can_invoke_a_planner(capsys):
    with pytest.raises(SystemExit) as error:
        run_planner.main(["--source", str(FIXTURE)])

    assert error.value.code == 2
    assert "sends source text to configured OpenRouter" in capsys.readouterr().err


def test_cli_rejects_an_unknown_reasoning_level(capsys):
    with pytest.raises(SystemExit) as error:
        run_planner.main(
            [
                "--live",
                "--source",
                str(FIXTURE),
                "--reasoning-level",
                "maximum",
            ]
        )

    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cli_rejects_a_nonproduction_budget_labeled_production_equivalent(capsys):
    with pytest.raises(SystemExit) as error:
        run_planner.main(
            [
                "--live",
                "--source",
                str(FIXTURE),
                "--max-turns",
                "3",
            ]
        )

    assert error.value.code == 2
    assert "production-equivalent requires --max-turns 2" in capsys.readouterr().err
