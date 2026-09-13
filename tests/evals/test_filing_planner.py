import json
from pathlib import Path

import pytest

from evals.filing import planner_eval, run_planner
from evals.filing import worktree as eval_worktree
from stigmergy.knowledge.plan import EntityProposal, FilingPlan, PageMutation
from stigmergy.knowledge.planner import PlanRun

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
    assert result["passed"] is True


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
        links=("Santi",),
    )

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
                action="update", path="wiki/concepts/Agent Harness.md", title="Agent Harness",
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
        title="Agent Harness",
        body=f"# Agent Harness\n\nA related normal page. (Source: `{SOURCE}`)",
        entities=identities,
        reason="Incorrectly copied anchors without page-specific evidence.",
    )

    result = planner_eval.score(plan.model_copy(update={"mutations": (*plan.mutations, copied_anchors)}), case)

    assert result["entity_relationships"]["missing"]
    assert result["passed"] is False


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
        links=("Santi", "OpenAI", "Anthropic", "LangChain"),
    )

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
    ("turn_arguments", "expected_max_turns"),
    (((), 1), (("--max-turns", "3"), 3)),
)
def test_cli_uses_one_request_by_default_and_preserves_an_explicit_turn_override(
    tmp_path, monkeypatch, capsys, turn_arguments, expected_max_turns
):
    source_text = FIXTURE.read_text(encoding="utf-8")
    worktree = tmp_path / "worktree"
    skill = worktree / ".claude" / "skills" / "librarian" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Synthetic librarian skill.\n", encoding="utf-8")
    calls = []

    class RecordingPlanner:
        def __init__(self, settings):
            calls.append({"settings": settings})

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
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert calls[0]["settings"].repo != str(worktree)
    assert calls[0]["settings"].max_turns == expected_max_turns
    assert source_text in calls[1]["source_text"]
    assert calls[1]["source_path"] == "sources/2026/09/00000000-0000-4000-8000-000000000001.md"
    assert '"candidates": []' in calls[1]["context"]
    assert payload["case"] == "harness-engineering"
    assert payload["score"]["passed"] is True
    assert payload["gates"]["passed"] is True


def test_cli_requires_live_acknowledgement_before_it_can_invoke_a_planner(capsys):
    with pytest.raises(SystemExit) as error:
        run_planner.main(["--source", str(FIXTURE)])

    assert error.value.code == 2
    assert "sends source text to configured OpenRouter" in capsys.readouterr().err
