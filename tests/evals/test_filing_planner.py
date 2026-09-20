from pathlib import Path

from evals.filing import constants, planner_eval, run_planner
from evals.filing import worktree as eval_worktree
from stigmergy.knowledge.plan import EntityProposal, FilingPlan, PageMutation

ROOT = Path(__file__).resolve().parents[2]
CASE = ROOT / "evals" / "filing" / "cases" / "harness_engineering.json"
FIXTURE = ROOT / "evals" / "filing" / "fixtures" / "harness_engineering_synthetic.md"
SOURCE = "sources/2026/09/00000000-0000-4000-8000-000000000001.md"
MEETING_CASE = ROOT / "evals" / "filing" / "cases" / "meeting_entity_quality.json"
MEETING_FIXTURE = ROOT / "evals" / "filing" / "fixtures" / "meeting_entity_quality.md"
MEETING_SOURCE = "sources/2026/09/00000000-0000-4000-8000-000000000003.md"
TICK = chr(96)


def _citation():
    return f"(Source: {TICK}{SOURCE}{TICK})"


def _passing_plan():
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
        "## Evidence and Examples\n\nSanti (@santtiagom_) authored the explanation. OpenAI built one "
        "million lines and 1,500 pull requests; LangChain rose from rank 30 to the top 5; Anthropic "
        f"compared a polished but broken application with a working app. {citation}\n\n"
        f"## Connections\n\n[[Agent Harness]] is the system this practice improves. {citation}"
    )
    entities = ("Santi", "OpenAI", "LangChain", "Anthropic")
    return FilingPlan(
        summary="Filed Harness Engineering",
        entities=tuple(
            EntityProposal(
                name=name,
                entity_type="person" if name == "Santi" else "organization",
                aliases=("@santtiagom_",) if name == "Santi" else (),
            )
            for name in entities
        ),
        mutations=(
            PageMutation(
                action="create",
                role="concept",
                title="Harness Engineering",
                body=body,
                entities=entities,
                reason="The source explains a reusable practice",
            ),
            PageMutation(
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
                    "to complete work. Teams can improve reliability by changing the harness rather "
                    "than assuming every failure requires a different model. "
                    f"{citation}\n\n## Connections\n\n[[Harness Engineering]] is the practice that "
                    f"improves this system. {citation}"
                ),
                entities=(),
                reason="The source separately explains the operational system",
            ),
        ),
    )


def _meeting_mutation_plan(*mutations: PageMutation) -> FilingPlan:
    return FilingPlan(summary="Recorded a dated operating state.", mutations=mutations)


def _meeting_note_mutation(*, title: str, role: str = "note") -> PageMutation:
    return PageMutation(
        action="create",
        role=role,
        title=title,
        body=f"# {title}\n\nA dated operating state. {_citation()}",
        entities=(),
        reason="The source records a dated operating state.",
    )


def test_production_budget_is_one_filing_plus_one_bounded_correction():
    assert constants.PRODUCTION_MAX_TURNS == 3
    assert constants.PRODUCTION_REASONING_LEVEL == "minimal"


def test_quality_score_checks_topology_content_and_entities():
    case = planner_eval.load_case(CASE)
    result = planner_eval.score(
        _passing_plan(),
        case,
        source_text=FIXTURE.read_text(encoding="utf-8"),
    )

    assert result["mutations"]["passed"] is True
    assert result["bodies"]["passed"] is True
    assert result["entity_relationships"]["passed"] is True
    assert result["editorial_quality"]["passed"] is True


def test_meeting_case_requires_every_named_participant_as_a_cited_entity_anchor():
    case = planner_eval.load_case(MEETING_CASE)
    citation = f"(Source: {TICK}{MEETING_SOURCE}{TICK})"
    people = ("Maya Ortiz", "Leon Park", "Noor Balan", "Priya Sen")
    plan = FilingPlan(
        summary="Recorded the Helio Stack review and its dated next evaluation cycle.",
        entities=(
            EntityProposal(
                name="Maya Ortiz",
                entity_type="person",
                description="Founder of Helio Stack.",
                facts=(
                    "At the 2026-09-20 review, Maya Ortiz was assigned to decide whether the customer pilot is ready.",
                ),
            ),
            EntityProposal(
                name="Leon Park",
                entity_type="person",
                description="Founder of Helio Stack.",
                facts=(
                    "At the 2026-09-20 review, Leon Park was assigned to decide whether the customer pilot is ready.",
                ),
            ),
            EntityProposal(
                name="Noor Balan",
                entity_type="person",
                description="Engineering lead at Helio Stack.",
                facts=(
                    "At the 2026-09-20 review, Noor Balan was assigned to run the next evaluation cycle.",
                ),
            ),
            EntityProposal(
                name="Priya Sen",
                entity_type="person",
                description="Customer research lead at Helio Stack.",
                facts=(
                    "At the 2026-09-20 review, Priya Sen was assigned to provide interview summaries.",
                ),
            ),
            EntityProposal(
                name="Helio Stack",
                entity_type="organization",
                description="Organization developing a tool for early-stage teams.",
                facts=(
                    "Its evaluation workflow was reviewed at the 2026-09-20 product review.",
                ),
            ),
        ),
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Helio Stack product review",
                body=(
                    "# Helio Stack product review\n\n"
                    "Maya Ortiz and Leon Park are Helio Stack founders. Noor Balan leads engineering "
                    "and Priya Sen leads customer research for the product review. "
                    f"{citation}\n\n"
                    "As of the 2026-09-20 meeting, Noor Balan will run the next evaluation cycle by "
                    "2026-10-01, while Priya Sen will provide five interview summaries before that "
                    f"review. {citation}"
                ),
                entities=(*people, "Helio Stack"),
                reason="The meeting records durable participant responsibilities and a dated operating state.",
            ),
        ),
    )

    result = planner_eval.score(plan, case, source_text=MEETING_FIXTURE.read_text(encoding="utf-8"))

    assert result["passed"] is True
    assert result["identity_proposals"]["found"] == [
        "helio stack",
        "leon park",
        "maya ortiz",
        "noor balan",
        "priya sen",
    ]
    assert result["entity_relationships"]["passed"] is True
    assert result["entity_editorial_quality"]["passed"] is True


def test_meeting_case_accepts_a_semantically_arbitrary_note_title():
    case = planner_eval.load_case(MEETING_CASE)
    plan = _meeting_mutation_plan(_meeting_note_mutation(title="Current operating state"))

    result = planner_eval.score(plan, case, source_text=MEETING_FIXTURE.read_text(encoding="utf-8"))

    assert result["mutations"]["passed"] is True


def test_meeting_case_rejects_a_concept_where_a_note_is_required():
    case = planner_eval.load_case(MEETING_CASE)
    plan = _meeting_mutation_plan(
        _meeting_note_mutation(title="Current operating state", role="concept")
    )

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


def test_meeting_case_rejects_empty_entity_proposals():
    case = planner_eval.load_case(MEETING_CASE)
    citation = f"(Source: {TICK}{MEETING_SOURCE}{TICK})"
    plan = FilingPlan(
        summary="Recorded the Helio Stack review and its dated next evaluation cycle.",
        entities=(
            *(EntityProposal(name=name, entity_type="person") for name in ("Maya Ortiz", "Leon Park", "Noor Balan", "Priya Sen")),
            EntityProposal(name="Helio Stack", entity_type="organization"),
        ),
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Helio Stack product review",
                body=(
                    "# Helio Stack product review\n\n"
                    "Maya Ortiz and Leon Park are Helio Stack founders. Noor Balan leads engineering "
                    "and Priya Sen leads customer research for the product review. "
                    f"{citation}\n\n"
                    "As of the 2026-09-20 meeting, Noor Balan will run the next evaluation cycle by "
                    "2026-10-01, while Priya Sen will provide five interview summaries before that "
                    f"review. {citation}"
                ),
                entities=("Maya Ortiz", "Leon Park", "Noor Balan", "Priya Sen", "Helio Stack"),
                reason="The meeting records durable participant responsibilities and a dated operating state.",
            ),
        ),
    )

    result = planner_eval.score(plan, case, source_text=MEETING_FIXTURE.read_text(encoding="utf-8"))

    assert result["entity_editorial_quality"]["passed"] is False


def test_meeting_case_rejects_missing_expected_entity_fact_coverage():
    case = planner_eval.load_case(MEETING_CASE)
    citation = f"(Source: {TICK}{MEETING_SOURCE}{TICK})"
    people = ("Maya Ortiz", "Leon Park", "Noor Balan", "Priya Sen")
    plan = FilingPlan(
        summary="Recorded the Helio Stack review and its dated next evaluation cycle.",
        entities=(
            EntityProposal(
                name="Maya Ortiz",
                entity_type="person",
                description="Founder of Helio Stack.",
                facts=(
                    "At the 2026-09-20 review, Maya Ortiz was assigned to decide whether the customer pilot is ready.",
                ),
            ),
            EntityProposal(
                name="Leon Park",
                entity_type="person",
                description="Founder of Helio Stack.",
                facts=(
                    "At the 2026-09-20 review, Leon Park was assigned to decide whether the customer pilot is ready.",
                ),
            ),
            EntityProposal(
                name="Noor Balan",
                entity_type="person",
                description="Engineering lead at Helio Stack.",
                facts=("At the 2026-09-20 review, Noor Balan participated.",),
            ),
            EntityProposal(
                name="Priya Sen",
                entity_type="person",
                description="Customer research lead at Helio Stack.",
                facts=(
                    "At the 2026-09-20 review, Priya Sen was assigned to provide interview summaries.",
                ),
            ),
            EntityProposal(
                name="Helio Stack",
                entity_type="organization",
                description="Organization developing a tool for early-stage teams.",
                facts=(
                    "Its evaluation workflow was reviewed at the 2026-09-20 product review.",
                ),
            ),
        ),
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Helio Stack product review",
                body=(
                    "# Helio Stack product review\n\n"
                    "Maya Ortiz and Leon Park are Helio Stack founders. Noor Balan leads engineering "
                    "and Priya Sen leads customer research for the product review. "
                    f"{citation}\n\n"
                    "As of the 2026-09-20 meeting, Noor Balan will run the next evaluation cycle by "
                    "2026-10-01, while Priya Sen will provide five interview summaries before that "
                    f"review. {citation}"
                ),
                entities=(*people, "Helio Stack"),
                reason="The meeting records durable participant responsibilities and a dated operating state.",
            ),
        ),
    )

    result = planner_eval.score(plan, case, source_text=MEETING_FIXTURE.read_text(encoding="utf-8"))

    assert result["entity_editorial_quality"]["passed"] is False


def test_meeting_case_rejects_description_fact_duplication():
    case = planner_eval.load_case(MEETING_CASE)
    citation = f"(Source: {TICK}{MEETING_SOURCE}{TICK})"
    repeated = "Founder of Helio Stack who participated in the 2026-09-20 review and will decide on the pilot."
    people = ("Maya Ortiz", "Leon Park", "Noor Balan", "Priya Sen")
    plan = FilingPlan(
        summary="Recorded the Helio Stack review and its dated next evaluation cycle.",
        entities=(
            EntityProposal(
                name="Maya Ortiz",
                entity_type="person",
                description=repeated,
                facts=(repeated,),
            ),
            EntityProposal(
                name="Leon Park",
                entity_type="person",
                description="Founder of Helio Stack.",
                facts=(
                    "At the 2026-09-20 review, Leon Park was assigned to decide whether the customer pilot is ready.",
                ),
            ),
            EntityProposal(
                name="Noor Balan",
                entity_type="person",
                description="Engineering lead at Helio Stack.",
                facts=(
                    "At the 2026-09-20 review, Noor Balan was assigned to run the next evaluation cycle.",
                ),
            ),
            EntityProposal(
                name="Priya Sen",
                entity_type="person",
                description="Customer research lead at Helio Stack.",
                facts=(
                    "At the 2026-09-20 review, Priya Sen was assigned to provide interview summaries.",
                ),
            ),
            EntityProposal(
                name="Helio Stack",
                entity_type="organization",
                description="Organization developing a tool for early-stage teams.",
                facts=(
                    "Its evaluation workflow was reviewed at the 2026-09-20 product review.",
                ),
            ),
        ),
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Helio Stack product review",
                body=(
                    "# Helio Stack product review\n\n"
                    "Maya Ortiz and Leon Park are Helio Stack founders. Noor Balan leads engineering "
                    "and Priya Sen leads customer research for the product review. "
                    f"{citation}\n\n"
                    "As of the 2026-09-20 meeting, Noor Balan will run the next evaluation cycle by "
                    "2026-10-01, while Priya Sen will provide five interview summaries before that "
                    f"review. {citation}"
                ),
                entities=(*people, "Helio Stack"),
                reason="The meeting records durable participant responsibilities and a dated operating state.",
            ),
        ),
    )

    result = planner_eval.score(plan, case, source_text=MEETING_FIXTURE.read_text(encoding="utf-8"))

    assert result["entity_editorial_quality"]["passed"] is False


def test_real_writer_gate_accepts_a_grounded_plan():
    case = planner_eval.load_case(CASE)
    source_text = FIXTURE.read_text(encoding="utf-8")
    with eval_worktree.prepared(
        case,
        source_text,
        template=str(run_planner.DEFAULT_WORKTREE),
    ) as worktree:
        result = eval_worktree.apply_and_gate(worktree, _passing_plan())

    assert result["passed"] is True
    assert "wiki/concepts/Harness Engineering.md" in result["changed_paths"]
