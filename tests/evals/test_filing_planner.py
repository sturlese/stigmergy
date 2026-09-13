import json
from pathlib import Path

import pytest

from evals.filing import planner_eval, run_planner
from stigmergy.knowledge.plan import EntityProposal, FilingPlan, PageMutation
from stigmergy.knowledge.planner import PlanRun

ROOT = Path(__file__).resolve().parents[2]
CASE = ROOT / "evals" / "filing" / "cases" / "harness_engineering.json"
FIXTURE = ROOT / "evals" / "filing" / "fixtures" / "harness_engineering_synthetic.md"
DECISION_TRACE_CASE = ROOT / "evals" / "filing" / "cases" / "decision_trace_quality.json"
DECISION_TRACE_FIXTURE = ROOT / "evals" / "filing" / "fixtures" / "decision_trace_quality.md"


def _plan(*, entities=(), links=()):
    return FilingPlan(
        summary="Filed synthetic Harness Engineering knowledge",
        entities=tuple(EntityProposal(name=name, entity_type="organization") for name in entities),
        mutations=(
            PageMutation(
                action="create",
                role="concept",
                title="Harness Engineering",
                body="# Harness Engineering\n\nA durable explanation.",
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
                body="# Decision Trace Quality\n\nA durable explanation.",
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


def test_harness_score_requires_exactly_one_harness_engineering_concept():
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
            return PlanRun(
                _plan(
                    entities=("Santi", "OpenAI", "Anthropic", "LangChain"),
                    links=("Santi", "OpenAI", "Anthropic", "LangChain"),
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
    assert calls[0]["settings"].repo == str(worktree)
    assert calls[0]["settings"].max_turns == expected_max_turns
    assert calls[1]["source_text"] == source_text
    assert calls[1]["source_path"] == "sources/2026/09/00000000-0000-4000-8000-000000000001.md"
    assert calls[1]["context"] == run_planner.EMPTY_CONTEXT
    assert payload["case"] == "harness-engineering"
    assert payload["score"]["passed"] is True


def test_cli_requires_live_acknowledgement_before_it_can_invoke_a_planner(capsys):
    with pytest.raises(SystemExit) as error:
        run_planner.main(["--source", str(FIXTURE)])

    assert error.value.code == 2
    assert "sends source text to configured OpenRouter" in capsys.readouterr().err
