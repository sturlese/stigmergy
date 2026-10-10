import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from evals.filing import constants, parity, planner_eval, run_planner
from evals.filing import worktree as eval_worktree
from stigmergy.kernel.normalize import resolution_key
from stigmergy.knowledge.pages import read_page
from stigmergy.knowledge.plan import FilingPlan, PageMutation
from stigmergy.knowledge.planner import PlanRun

ROOT = Path(__file__).resolve().parents[2]
CASE = ROOT / "evals" / "filing" / "cases" / "harness_engineering.json"
SEEDED_CASE = ROOT / "evals" / "filing" / "cases" / "harness_engineering_seeded.json"
FIXTURE = ROOT / "evals" / "filing" / "fixtures" / "harness_engineering_synthetic.md"
SOURCE = "sources/2026/09/00000000-0000-4000-8000-000000000001.md"
MEETING_CASE = ROOT / "evals" / "filing" / "cases" / "meeting_entity_quality.json"
MEETING_FIXTURE = ROOT / "evals" / "filing" / "fixtures" / "meeting_entity_quality.md"
MEETING_SOURCE = "sources/2026/09/00000000-0000-4000-8000-000000000003.md"
TICK = chr(96)


def _citation(source: str = SOURCE) -> str:
    return f"(Source: {TICK}{source}{TICK})"


def _entity_page(
    title: str,
    entity_type: str,
    *,
    description: str,
    facts: tuple[str, ...],
    connections: tuple[tuple[str, str], ...],
    aliases: tuple[str, ...] = (),
    source: str = SOURCE,
) -> PageMutation:
    """An entity page shaped like the librarian skill's template: one paragraph, dated facts, links."""
    citation = _citation(source)
    body = f"# {title}\n\n## What / Who\n\n{description} {citation}"
    if facts:
        body += "\n\n## Facts\n\n" + "\n".join(f"- {fact} {citation}" for fact in facts)
    if connections:
        body += "\n\n## Connections\n\n" + "\n".join(f"- [[{target}]] - {why}" for target, why in connections)
    return PageMutation(
        action="create",
        role="entity",
        title=title,
        entity_type=entity_type,
        aliases=aliases,
        body=body,
        reason=f"The source names {title}.",
    )


def _harness_engineering_concept(extra: str = "") -> PageMutation:
    citation = _citation()
    body = (
        "# Harness Engineering\n\n"
        "## Definition\n\nA model alone is not an agentic system: the model reasons while the "
        f"[[Agent Harness]] turns choices into work. {citation}\n\n"
        "## How It Works\n\nTools expose actions; a loop repeats decide, act, observe; memory/state "
        "preserves work; context selection chooses what the model sees; an isolated working "
        "environment contains execution; and objective and verification provide acceptance criteria. "
        "Permissions and limits, observability, and evals supply trust. Skills, MCP, subagents, and "
        f"long-term memory extend the design. {citation}\n\n"
        "## Why It Matters\n\nHarness changes can improve outcomes without changing the model. "
        f"{citation}\n\n"
        "## Evidence and Examples\n\n[[Robin Hale]] (@robinhale_example) authored the explanation. [[OpenAI]] built "
        "one million lines and 1,500 pull requests; [[LangChain]] rose from rank 30 to the top 5; "
        f"[[Anthropic]] compared a polished but broken application with a working app. {citation}"
        f"{extra}\n\n"
        "## Connections\n\n- [[Agent Harness]] - the system this practice improves.\n"
        "- [[Robin Hale]] - the author of the explanation."
    )
    return PageMutation(
        action="create",
        role="concept",
        title="Harness Engineering",
        body=body,
        reason="The source explains a reusable practice",
    )


def _agent_harness_concept(*, links: tuple[str, ...] = ("Anthropic",)) -> PageMutation:
    citation = _citation()
    example = " ".join(f"[[{name}]]" for name in links) if links else "The source"
    return PageMutation(
        action="create",
        role="concept",
        title="Agent Harness",
        body=(
            "# Agent Harness\n\n## Definition\n\nAn agent harness is the operational "
            "system around a model. It turns text decisions into observable actions while "
            f"retaining state outside the stateless model. {citation}\n\n## How It Works\n\n"
            "It supplies named tools, repeats a decide-act-observe loop, preserves memory, "
            "selects context, and contains execution. Permissions, verification, telemetry, "
            f"and evals make that loop controllable and measurable. {citation}\n\n"
            "## Why It Matters\n\nIt separates model capability from the environment needed "
            f"to complete work. {example} showed the same model producing a polished but broken "
            "application or a working app depending on the harness, so teams can improve reliability "
            "by changing the harness rather than assuming every failure requires a different model. "
            f"{citation}\n\n## Connections\n\n- [[Harness Engineering]] - the practice that "
            "improves this system."
        ),
        reason="The source separately explains the operational system",
    )


def _harness_entity_pages() -> tuple[PageMutation, ...]:
    return (
        _entity_page(
            "Robin Hale",
            "person",
            aliases=("@robinhale_example",),
            description="Robin Hale, publishing as @robinhale_example, wrote the harness explainer this wiki files.",
            facts=("Authored the explanation that separates the model from its [[Agent Harness]].",),
            connections=(("Harness Engineering", "the practice Robin Hale explains."),),
        ),
        _entity_page(
            "OpenAI",
            "organization",
            description="OpenAI is an AI lab the source cites for a large-scale harness result.",
            facts=("Built one million lines of code and 1,500 pull requests with an agent harness.",),
            connections=(("Harness Engineering", "the practice behind the cited result."),),
        ),
        _entity_page(
            "LangChain",
            "organization",
            description="LangChain is an agent framework company the source cites for a benchmark climb.",
            facts=("Rose from rank 30 to the top 5 on Terminal Bench by changing the harness, not the model.",),
            connections=(("Harness Engineering", "the practice behind the cited climb."),),
        ),
        _entity_page(
            "Anthropic",
            "organization",
            description="Anthropic is an AI lab the source cites for a same-model harness comparison.",
            facts=("Showed a polished but broken application and a working app from the same model.",),
            connections=(("Agent Harness", "the system whose configuration the comparison varied."),),
        ),
    )


def _passing_plan() -> FilingPlan:
    return FilingPlan(
        summary="Filed Harness Engineering",
        mutations=(_harness_engineering_concept(), _agent_harness_concept(), *_harness_entity_pages()),
    )


def _meeting_entities() -> dict[str, dict]:
    return {
        "Helio Stack": {
            "entity_type": "organization",
            "description": (
                "Helio Stack is an early-stage company building a tool that helps teams compare "
                "candidate product directions using recorded customer evidence."
            ),
            "facts": (
                "On 2026-09-20 its founders reviewed the product's evaluation workflow with the "
                "engineering and customer research leads.",
            ),
        },
        "Maya Ortiz": {
            "entity_type": "person",
            "description": "Maya Ortiz is a founder of [[Helio Stack]] who takes part in its product reviews.",
            "facts": (
                "At the 2026-09-20 review, Maya Ortiz was assigned to decide with [[Leon Park]] whether "
                "the customer pilot is ready.",
            ),
        },
        "Leon Park": {
            "entity_type": "person",
            "description": "Leon Park is a founder of [[Helio Stack]] who takes part in its product reviews.",
            "facts": (
                "At the 2026-09-20 review, Leon Park was assigned to decide with [[Maya Ortiz]] whether "
                "the customer pilot is ready.",
            ),
        },
        "Noor Balan": {
            "entity_type": "person",
            "description": (
                "Noor Balan is the engineering lead working with [[Helio Stack]] on its evaluation workflow."
            ),
            "facts": (
                "At the 2026-09-20 review, Noor Balan was assigned to run the next evaluation cycle by 2026-10-01.",
            ),
        },
        "Priya Sen": {
            "entity_type": "person",
            "description": "Priya Sen leads customer research for [[Helio Stack]] and supplies interview evidence.",
            "facts": ("At the 2026-09-20 review, Priya Sen was assigned to provide five interview summaries.",),
        },
    }


def _meeting_note() -> PageMutation:
    citation = _citation(MEETING_SOURCE)
    return PageMutation(
        action="create",
        role="note",
        title="Helio Stack product review",
        body=(
            "# Helio Stack product review\n\n"
            "[[Maya Ortiz]] and [[Leon Park]] are [[Helio Stack]] founders. [[Noor Balan]] leads engineering "
            f"and [[Priya Sen]] leads customer research for the product review. {citation}\n\n"
            "As of the 2026-09-20 meeting, Noor Balan will run the next evaluation cycle by "
            "2026-10-01, while Priya Sen will provide five interview summaries before that "
            f"review. The discussion connected the work to [[AI-assisted Founder Evaluation]]. {citation}"
        ),
        reason="The meeting records durable participant responsibilities and a dated operating state.",
    )


def _meeting_plan(entities: dict[str, dict] | None = None, *, bare_entities: bool = False) -> FilingPlan:
    entities = _meeting_entities() if entities is None else entities
    pages = []
    for title, item in entities.items():
        if bare_entities:
            pages.append(
                PageMutation(
                    action="create",
                    role="entity",
                    title=title,
                    entity_type=item["entity_type"],
                    body=f"# {title}\n\nA participant in the Helio Stack product review. {_citation(MEETING_SOURCE)}",
                    reason=f"The source names {title}.",
                )
            )
            continue
        pages.append(
            _entity_page(
                title,
                item["entity_type"],
                description=item["description"],
                facts=item["facts"],
                connections=(("Helio Stack product review", "the dated review this entity took part in."),),
                source=MEETING_SOURCE,
            )
        )
    return FilingPlan(
        summary="Recorded the Helio Stack review and its dated next evaluation cycle.",
        mutations=(_meeting_note(), *pages),
    )


def _meeting_mutation_plan(*mutations: PageMutation) -> FilingPlan:
    return FilingPlan(summary="Recorded a dated operating state.", mutations=mutations)


def _meeting_note_mutation(*, title: str, role: str = "note") -> PageMutation:
    return PageMutation(
        action="create",
        role=role,
        title=title,
        body=f"# {title}\n\nA dated operating state. {_citation(MEETING_SOURCE)}",
        reason="The source records a dated operating state.",
    )


def _score(plan: FilingPlan, case_path: Path, fixture: Path) -> dict:
    return planner_eval.score(plan, planner_eval.load_case(case_path), source_text=fixture.read_text(encoding="utf-8"))


def test_production_budget_is_one_filing_plus_one_bounded_correction():
    assert constants.PRODUCTION_MAX_TURNS == 3
    assert constants.PRODUCTION_REASONING_LEVEL == "medium"


@pytest.mark.parametrize("requires_correction", [False, True])
def test_runner_record_replays_through_v8_case_validation(monkeypatch, capsys, tmp_path, requires_correction):
    initial = (
        FilingPlan(
            summary="Deliberately uncited first draft.",
            mutations=(
                PageMutation(
                    action="create",
                    role="concept",
                    title="Harness Engineering",
                    body="# Harness Engineering\n\nThis draft has no source citation.",
                    reason="Deliberately invalid fixture draft.",
                ),
            ),
        )
        if requires_correction
        else _passing_plan()
    )

    class RecordedPlanner:
        def __init__(self, *_args, **_kwargs):
            pass

        def plan(self, **_kwargs):
            return PlanRun(initial, model_requests=1)

        def revise(self, **_kwargs):
            return PlanRun(_passing_plan(), model_requests=1)

    monkeypatch.setattr(run_planner, "PydanticPlanner", RecordedPlanner)

    assert (
        run_planner.main(
            [
                "--live",
                "--source",
                str(FIXTURE),
                "--include-payload",
            ]
        )
        == 0
    )
    record = json.loads(capsys.readouterr().out)["case_result"]
    inputs = parity.current_release_inputs(ROOT)
    expected = replace(
        inputs,
        source_cases={"harness_engineering": inputs.source_cases["harness_engineering"]},
        source_fixtures={"harness_engineering": inputs.source_fixtures["harness_engineering"]},
        case_paths={"harness_engineering": inputs.case_paths["harness_engineering"]},
        fixture_paths={"harness_engineering": inputs.fixture_paths["harness_engineering"]},
    )
    failures, replayed = [], {}

    parity._case(
        record,
        "stigmergy",
        record["runtime"],
        record["execution"],
        expected,
        "recorded-run",
        failures,
        replayed,
    )

    assert not failures
    assert record["correction"]["applied"] is requires_correction
    assert set(record["evidence"]["result"]["raw_gates"]) == parity.REQUIRED_SEMANTIC_GATES
    assert replayed[("stigmergy", "recorded-run", "harness_engineering")]

    malformed = json.loads(json.dumps(record))
    malformed["correction"].pop("plan")
    failures, replayed = [], {}
    parity._case(
        malformed,
        "stigmergy",
        malformed["runtime"],
        malformed["execution"],
        expected,
        "recorded-run",
        failures,
        replayed,
    )

    assert {failure["reason"] for failure in failures} == {"case-correction"}
    assert not replayed

    mismatched_manifest = json.loads(json.dumps(record))
    mismatched_manifest["input"]["initial_worktree_manifest_sha256"] = "a" * 64
    mismatched_manifest["evidence"]["input"] = mismatched_manifest["input"]
    digest = hashlib.sha256(
        json.dumps(
            mismatched_manifest["evidence"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    mismatched_manifest["output"] = {"sha256": digest, "artifact_ref": f"sha256:{digest}"}
    failures, replayed = [], {}
    parity._case(
        mismatched_manifest,
        "stigmergy",
        mismatched_manifest["runtime"],
        mismatched_manifest["execution"],
        expected,
        "recorded-run",
        failures,
        replayed,
    )

    assert {failure["reason"] for failure in failures} == {"initial-worktree-manifest"}
    assert not replayed


def test_production_equivalent_runner_rejects_a_noncanonical_worktree(tmp_path):
    alternate = tmp_path / "alternate-worktree"
    shutil.copytree(run_planner.DEFAULT_WORKTREE, alternate)
    (alternate / "unbound.txt").write_text("not the release template\n", encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        run_planner.main(
            [
                "--live",
                "--source",
                str(FIXTURE),
                "--worktree",
                str(alternate),
            ]
        )

    assert error.value.code == 2


def test_runner_records_typed_source_block_rejection_without_derived_paths(monkeypatch, capsys, tmp_path):
    draft = FilingPlan(
        summary="Replaced the seeded source-backed manuscript.",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/concepts/Agent Harness.md",
                body=f"# Agent Harness\n\nNew material only. {_citation()}",
                reason="The fixture deliberately drops preserved prose.",
            ),
        ),
    )
    rejected_correction = draft.model_copy(
        update={
            "summary": "The correction still drops the seeded source-backed manuscript.",
            "mutations": (
                draft.mutations[0].model_copy(
                    update={
                        "body": f"# Agent Harness\n\nStill new material only. {_citation()}",
                    }
                ),
            ),
        }
    )

    class RecordedPlanner:
        def __init__(self, *_args, **_kwargs):
            pass

        def plan(self, **_kwargs):
            return PlanRun(draft, model_requests=1)

        def revise(self, **_kwargs):
            return PlanRun(rejected_correction, model_requests=1)

    monkeypatch.setattr(run_planner, "PydanticPlanner", RecordedPlanner)

    assert (
        run_planner.main(
            [
                "--live",
                "--source",
                str(FIXTURE),
                "--case",
                str(SEEDED_CASE),
                "--include-payload",
            ]
        )
        == 1
    )
    record = json.loads(capsys.readouterr().out)["case_result"]

    assert record["correction"] == {"required": True, "attempted": True, "applied": False, "plan": None}
    assert record["evidence"]["result"]["writer_gates"] == {
        "passed": False,
        "violations": [],
        "changed_paths": [],
        "plan_rejection": "existing-source-block-not-preserved",
    }


def test_initial_source_block_rejection_is_typed_through_runner_and_parity_replay(
    monkeypatch, capsys, tmp_path
):
    seeded = planner_eval.load_case(SEEDED_CASE)["seed_pages"][0]["body"].replace(
        "{source_path}", eval_worktree.SEED_SOURCE_PATH
    )
    draft = FilingPlan(
        summary="The first draft drops the existing source-backed manuscript.",
        mutations=(
            _harness_engineering_concept(),
            *_harness_entity_pages(),
            PageMutation(
                action="update",
                path="wiki/concepts/Agent Harness.md",
                body=f"# Agent Harness\n\nNew material only. {_citation()}",
                reason="The fixture deliberately drops preserved prose.",
            ),
        ),
    )
    correction = draft.model_copy(
        update={
            "summary": "The bounded correction preserves the base manuscript.",
            "mutations": (
                *draft.mutations[:-1],
                PageMutation(
                    action="update",
                    path="wiki/concepts/Agent Harness.md",
                    body=(
                        f"{seeded}\n\n## Connections\n\n[[Harness Engineering]] is the practice that "
                        f"improves this system. {_citation()}"
                    ),
                    reason="The correction retains the existing evidence and adds the connection.",
                ),
            ),
        }
    )

    class RecordedPlanner:
        def __init__(self, *_args, **_kwargs):
            pass

        def plan(self, **_kwargs):
            return PlanRun(draft, model_requests=1)

        def revise(self, **_kwargs):
            return PlanRun(correction, model_requests=1)

    monkeypatch.setattr(run_planner, "PydanticPlanner", RecordedPlanner)

    assert (
        run_planner.main(
            [
                "--live",
                "--source",
                str(FIXTURE),
                "--case",
                str(SEEDED_CASE),
                "--include-payload",
            ]
        )
        == 0
    )
    record = json.loads(capsys.readouterr().out)["case_result"]
    case_id = record["case_id"]
    inputs = parity.current_release_inputs(ROOT)
    expected = replace(
        inputs,
        source_cases={case_id: hashlib.sha256(SEEDED_CASE.read_bytes()).hexdigest()},
        source_fixtures={case_id: hashlib.sha256(FIXTURE.read_bytes()).hexdigest()},
        case_paths={case_id: SEEDED_CASE},
        fixture_paths={case_id: FIXTURE},
    )
    failures, replayed = [], {}

    parity._case(
        record,
        "stigmergy",
        record["runtime"],
        record["execution"],
        expected,
        "recorded-run",
        failures,
        replayed,
    )

    assert record["evidence"]["result"]["writer_gates"]["plan_rejection"] == (
        "existing-source-block-not-preserved"
    )
    assert not failures
    assert replayed[("stigmergy", "recorded-run", case_id)]


def test_quality_score_checks_topology_links_bodies_and_entity_pages():
    result = _score(_passing_plan(), CASE, FIXTURE)

    assert set(result) - {"passed"} == parity.REQUIRED_SEMANTIC_GATES - {"writer"}
    assert result["passed"] is True
    assert result["mutations"]["passed"] is True
    assert result["entity_pages"]["found"] == ["anthropic", "langchain", "openai", "robin hale"]
    assert result["links"]["passed"] is True
    assert result["link_coverage"]["passed"] is True
    assert result["link_density"]["passed"] is True
    assert result["bodies"]["passed"] is True
    assert result["editorial_quality"]["passed"] is True
    assert result["entity_editorial_quality"]["passed"] is True


def test_link_coverage_fails_for_an_entity_page_nothing_links():
    orphan = _entity_page(
        "Codex",
        "product",
        description="Codex is a coding-agent environment the source names in passing.",
        facts=("Named by the source as one coding-agent environment among others.",),
        connections=(),
    )
    plan = _passing_plan().model_copy(update={"mutations": (*_passing_plan().mutations, orphan)})

    result = _score(plan, CASE, FIXTURE)

    assert result["entity_pages"]["passed"] is True
    assert result["link_coverage"]["passed"] is False
    assert result["link_coverage"]["unlinked_entity_pages"] == [resolution_key("Codex")]
    assert result["passed"] is False


def test_link_density_fails_for_a_sparsely_linked_concept():
    plan = FilingPlan(
        summary="Filed Harness Engineering with a sparse companion concept.",
        mutations=(_harness_engineering_concept(), _agent_harness_concept(links=()), *_harness_entity_pages()),
    )

    result = _score(plan, CASE, FIXTURE)

    assert result["link_density"]["minimum"] == 2
    assert result["link_density"]["links_per_page"]["Agent Harness"] == 1
    assert result["link_density"]["sparse_pages"] == ["Agent Harness"]
    assert result["link_density"]["passed"] is False
    assert result["passed"] is False


def test_meeting_case_requires_every_named_participant_as_a_linked_entity_page():
    result = _score(_meeting_plan(), MEETING_CASE, MEETING_FIXTURE)

    assert result["passed"] is True
    assert result["entity_pages"]["found"] == [
        "helio stack",
        "leon park",
        "maya ortiz",
        "noor balan",
        "priya sen",
    ]
    assert result["links"]["missing"] == []
    assert result["link_coverage"]["unlinked_entity_pages"] == []
    assert result["entity_editorial_quality"]["passed"] is True


def test_meeting_case_accepts_a_semantically_arbitrary_note_title():
    case = planner_eval.load_case(MEETING_CASE)
    plan = _meeting_mutation_plan(_meeting_note_mutation(title="Current operating state"))

    result = planner_eval.score(plan, case, source_text=MEETING_FIXTURE.read_text(encoding="utf-8"))

    assert result["mutations"]["passed"] is True


def test_meeting_case_rejects_a_concept_where_a_note_is_required():
    case = planner_eval.load_case(MEETING_CASE)
    plan = _meeting_mutation_plan(_meeting_note_mutation(title="Current operating state", role="concept"))

    result = planner_eval.score(plan, case, source_text=MEETING_FIXTURE.read_text(encoding="utf-8"))

    assert result["mutations"]["passed"] is False


def test_meeting_case_rejects_an_extra_unexpected_mutation():
    case = planner_eval.load_case(MEETING_CASE)
    plan = _meeting_mutation_plan(
        _meeting_note_mutation(title="Current operating state"),
        _meeting_note_mutation(title="Another dated operating state"),
    )

    result = planner_eval.score(plan, case, source_text=MEETING_FIXTURE.read_text(encoding="utf-8"))

    assert result["mutations"]["passed"] is False


def test_meeting_case_rejects_entity_pages_without_a_description_or_facts():
    result = _score(_meeting_plan(bare_entities=True), MEETING_CASE, MEETING_FIXTURE)

    assert result["entity_pages"]["passed"] is True
    assert result["entity_editorial_quality"]["passed"] is False
    assert set(result["entity_editorial_quality"]["missing_descriptions"]) == set(_meeting_entities())
    assert set(result["entity_editorial_quality"]["missing_fact_coverage"]) == set(_meeting_entities())


def test_meeting_case_rejects_undated_entity_fact_coverage():
    entities = _meeting_entities()
    entities["Noor Balan"]["facts"] = ("Noor Balan participated in the product review.",)

    result = _score(_meeting_plan(entities), MEETING_CASE, MEETING_FIXTURE)

    assert result["entity_editorial_quality"]["passed"] is False
    assert result["entity_editorial_quality"]["missing_temporal_fact_anchors"] == ["Noor Balan"]


def test_meeting_case_rejects_description_fact_duplication():
    repeated = "Founder of Helio Stack who participated in the 2026-09-20 review and will decide on the pilot."
    entities = _meeting_entities()
    entities["Maya Ortiz"]["description"] = repeated
    entities["Maya Ortiz"]["facts"] = (repeated,)

    result = _score(_meeting_plan(entities), MEETING_CASE, MEETING_FIXTURE)

    assert result["entity_editorial_quality"]["passed"] is False
    assert result["entity_editorial_quality"]["description_fact_duplicates"] == [
        {"entity": "Maya Ortiz", "fact": repeated}
    ]


def test_real_writer_gate_accepts_a_grounded_plan():
    case = planner_eval.load_case(CASE)
    source_text = FIXTURE.read_text(encoding="utf-8")
    with eval_worktree.prepared(
        case,
        source_text,
        template=str(run_planner.DEFAULT_WORKTREE),
    ) as worktree:
        result = eval_worktree.apply_and_gate(worktree, _passing_plan())
        author = read_page(worktree.root, "wiki/entities/Robin Hale.md")

    assert result["passed"] is True
    assert "wiki/concepts/Harness Engineering.md" in result["changed_paths"]
    assert "wiki/entities/Robin Hale.md" in result["changed_paths"]
    assert author.entity_type == "person"
    assert author.aliases == ("@robinhale_example",)
    assert author.sources == (SOURCE,)


def test_real_writer_gate_degrades_an_unresolved_link_to_its_text():
    case = planner_eval.load_case(CASE)
    plan = FilingPlan(
        summary="Filed Harness Engineering with one link nothing resolves.",
        mutations=(
            _harness_engineering_concept(extra=" See also [[Unfiled Topic|the unfiled topic]]."),
            _agent_harness_concept(),
            *_harness_entity_pages(),
        ),
    )
    with eval_worktree.prepared(
        case,
        FIXTURE.read_text(encoding="utf-8"),
        template=str(run_planner.DEFAULT_WORKTREE),
    ) as worktree:
        result = eval_worktree.apply_and_gate(worktree, plan)
        effective = eval_worktree.effective_plan(worktree, plan)

    assert result["passed"] is True
    landed = effective.mutations[0].body
    assert "[[Unfiled Topic" not in landed
    assert "See also the unfiled topic." in landed
    assert "[[Robin Hale]]" in landed


def test_real_writer_gate_skips_an_entity_page_whose_name_is_already_claimed():
    case = planner_eval.load_case(CASE)
    duplicate = _entity_page(
        "Robert Hale",
        "person",
        aliases=("Robin Hale",),
        description="Robert Hale is the same author under a name an earlier page already claims.",
        facts=("Named once more by the source under a claimed alias.",),
        connections=(),
    )
    plan = _passing_plan().model_copy(update={"mutations": (*_passing_plan().mutations, duplicate)})
    with eval_worktree.prepared(
        case,
        FIXTURE.read_text(encoding="utf-8"),
        template=str(run_planner.DEFAULT_WORKTREE),
    ) as worktree:
        result = eval_worktree.apply_and_gate(worktree, plan)
        created = sorted(path.name for path in Path(worktree.root, "wiki", "entities").glob("*.md"))

    assert result["passed"] is True
    assert "wiki/entities/Robin Hale.md" in result["changed_paths"]
    assert "wiki/entities/Robert Hale.md" not in result["changed_paths"]
    assert created == ["Anthropic.md", "LangChain.md", "OpenAI.md", "Robin Hale.md"]


def test_the_harness_gives_an_empty_plan_the_writers_correction():
    from stigmergy.knowledge.planner import PlanRun

    class _Corrects:
        def __init__(self):
            self.violations = None

        def revise(self, **kwargs):
            self.violations = kwargs["violations"]
            return PlanRun(_passing_plan(), model_requests=1)

    case = planner_eval.load_case(CASE)
    planner = _Corrects()
    with eval_worktree.prepared(case, FIXTURE.read_text(encoding="utf-8"),
                                template=str(run_planner.DEFAULT_WORKTREE)) as worktree:
        result = eval_worktree.apply_with_production_repair(
            worktree, FilingPlan(summary="Filed it.", mutations=()), planner,
            planning_model_requests=1, max_turns=3)

    assert [failure["code"] for failure in planner.violations] == ["empty-plan"]
    assert result["semantic_revision_applied"] is True and result["passed"] is True


def test_dated_current_state_lines_count_as_an_entitys_dated_facts():
    body = ("# Helio Stack\n\n## What / Who\nHelio Stack is a product company. (Source: `s.md`)\n\n"
            "## Current state\n- 2026-09-20: The evaluation workflow is under review. (Source: `s.md`)\n")
    description, facts = planner_eval._entity_sections(body)
    assert description.startswith("Helio Stack is a product company")
    assert facts == ("2026-09-20: The evaluation workflow is under review.",)
