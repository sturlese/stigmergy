import subprocess

import pytest

from stigmergy.capture import evidence, schema
from stigmergy.capture.schema import Actor
from stigmergy.capture.service import CaptureService
from stigmergy.capture.source import source_path
from stigmergy.knowledge.plan import FilingPlan, PageMutation, RepairPlan
from stigmergy.knowledge.planner import PlanRun, ScriptedPlanner
from stigmergy.knowledge.writer import WriterDeps
from stigmergy.librarian import config, worker


class RevisionPlanner(ScriptedPlanner):
    def __init__(self, draft, revision, *, planning_requests=1, revision_requests=1):
        super().__init__(draft, revision_plan=revision)
        self.planning_requests = planning_requests
        self.revision_requests = revision_requests
        self.plan_context = ""
        self.revision_calls = []

    def plan(self, **kwargs):
        self.plan_context = kwargs["context"]
        return PlanRun(self.result, model_requests=self.planning_requests)

    def revise(self, **kwargs):
        self.revision_calls.append(kwargs)
        return PlanRun(self.revision_result, model_requests=self.revision_requests)


def _capture(conn, store, *, actor, audience, key, text):
    receipt = CaptureService(conn, store).capture_text(
        actor=actor,
        audience=audience,
        adapter="mcp",
        text=text,
        idempotency_key=key,
    )
    return receipt, source_path(schema.parse_capture(receipt["request"]))


def _process(conn, repo, store, planner):
    settings = config.Settings(repo=str(repo), branch="main", backend="scripted")
    return worker.process_next(conn, WriterDeps(settings, store, planner, str(repo)))


def _body(title, text, source):
    return f"# {title}\n\n{text} (Source: `{source}`)"


def _seed_page(conn, repo, store, *, actor, audience, key, title, text):
    _receipt, source = _capture(
        conn,
        store,
        actor=actor,
        audience=audience,
        key=key,
        text=text,
    )
    plan = FilingPlan(
        summary=f"Recorded {title}.",
        mutations=(
            PageMutation(
                action="create",
                role="concept",
                title=title,
                body=_body(title, text, source),
                reason="The capture establishes durable context.",
            ),
        ),
    )
    _item, outcome = _process(conn, repo, store, ScriptedPlanner(plan))
    assert outcome.status == schema.LANDED
    return source


def _changed_paths(repo, commit_sha):
    return subprocess.check_output(
        ["git", "show", "--format=", "--name-only", commit_sha], cwd=repo, text=True
    ).splitlines()


@pytest.mark.parametrize(("planning_requests", "correction_budget"), ((1, 2), (2, 1)))
def test_contract_rejected_update_gets_one_bounded_correction(
    clean_queue, target_repo, planning_requests, correction_budget
):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="morgan", display_name="Morgan")
    prior_source = _seed_page(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="semantic-revision-seed",
        title="Agent Harness",
        text="An agent harness is the lower-level operational system around a model.",
    )
    _receipt, source = _capture(
        clean_queue,
        store,
        actor=actor,
        audience=None,
        key="semantic-revision-update",
        text="Harness engineering is the broader discipline that designs an agent harness.",
    )
    draft = FilingPlan(
        summary="Collapsed the framework into the existing artifact.",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/concepts/Agent Harness.md",
                body=_body("Agent Harness", "The draft collapses the broader discipline.", source),
                reason="The draft only updates the existing artifact.",
            ),
        ),
    )
    revision = FilingPlan(
        summary="Preserved the existing evidence while adding the new source.",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/concepts/Agent Harness.md",
                body=(
                    _body(
                        "Agent Harness",
                        "Harness engineering designs the agent harness as its operational system.",
                        source,
                    )
                    + "\n\nAn agent harness is the lower-level operational system around a "
                    f"model. (Source: `{prior_source}`)"
                ),
                reason="The correction preserves prior evidence and adds the new claim.",
            ),
        ),
    )
    planner = RevisionPlanner(draft, revision, planning_requests=planning_requests)

    item, outcome = _process(clean_queue, target_repo, store, planner)

    assert outcome.status == schema.LANDED
    assert planner.revision_calls[0]["context"] == planner.plan_context
    assert planner.revision_calls[0]["draft"] == draft
    assert planner.revision_calls[0]["source_path"] == source
    # The correction keeps the schema retry the plan did not use, within three requests in all.
    assert planner.revision_calls[0]["max_requests"] == correction_budget
    assert planner.revision_calls[0]["retry"] is False
    assert item["report"]["model_requests"] == planning_requests + 1
    assert item["report"]["planning_model_requests"] == planning_requests
    assert item["report"]["semantic_revision_model_requests"] == 1
    assert item["report"]["semantic_revision_required"] is True
    assert item["report"]["semantic_revision_attempted"] is True
    assert item["report"]["semantic_revision_applied"] is True
    assert item["report"]["repair_model_requests"] == 0
    assert item["report"]["plan_rejection"] == "existing-source-block-not-preserved"
    page = subprocess.check_output(
        ["git", "show", "main:wiki/concepts/Agent Harness.md"], cwd=target_repo, text=True
    )
    assert "Harness engineering designs the agent harness" in page
    assert f"(Source: `{prior_source}`)" in page


def test_contract_correction_preserves_an_existing_paragraph_with_its_distinct_source(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="morgan", display_name="Morgan")
    original_text = "The existing harness keeps a durable feedback loop for diagnosing failures."
    original_source = _seed_page(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="semantic-revision-preserve-prior-source",
        title="Agent Harness",
        text=original_text,
    )
    _receipt, current_source = _capture(
        clean_queue,
        store,
        actor=actor,
        audience=None,
        key="semantic-revision-preserve-current-source",
        text="Harness engineering improves the agent harness through source-backed evaluation.",
    )
    draft = FilingPlan(
        summary="Replaced an existing page with the current capture only.",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/concepts/Agent Harness.md",
                body=_body("Agent Harness", "The current capture adds source-backed evaluation.", current_source),
                reason="The current capture changes the existing concept.",
            ),
        ),
    )
    revision = FilingPlan(
        summary="Retained the update while dropping independently supported history.",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/concepts/Agent Harness.md",
                body=_body(
                    "Agent Harness",
                    "The current capture adds source-backed evaluation to the system.",
                    current_source,
                ),
                reason="The current capture changes the existing concept.",
            ),
        ),
    )

    _item, outcome = _process(clean_queue, target_repo, store, RevisionPlanner(draft, revision))

    assert outcome.status == schema.LANDED
    page = subprocess.check_output(
        ["git", "show", "main:wiki/concepts/Agent Harness.md"], cwd=target_repo, text=True
    )
    assert original_text in page
    assert f"(Source: `{original_source}`)" in page


def test_rejected_correction_that_drops_existing_source_backed_prose_rolls_back_all_derived_changes(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="morgan", display_name="Morgan")
    original_text = "The existing harness keeps a durable feedback loop for diagnosing failures."
    original_source = _seed_page(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="semantic-revision-reject-preservation-seed",
        title="Agent Harness",
        text=original_text,
    )
    _receipt, source = _capture(
        clean_queue,
        store,
        actor=actor,
        audience=None,
        key="semantic-revision-reject-preservation-update",
        text="Harness engineering improves the agent harness through source-backed evaluation.",
    )
    draft = FilingPlan(
        summary="Replaced independently supported history.",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/concepts/Agent Harness.md",
                body=_body("Agent Harness", "The current capture adds source-backed evaluation.", source),
                reason="The current capture changes the existing concept.",
            ),
        ),
    )
    rejected_correction = FilingPlan(
        summary="Still replaced independently supported history.",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/concepts/Agent Harness.md",
                body=_body("Agent Harness", "The correction still drops prior evidence.", source),
                reason="The current capture changes the existing concept.",
            ),
        ),
    )

    item, outcome = _process(
        clean_queue,
        target_repo,
        store,
        RevisionPlanner(draft, rejected_correction),
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["plan_rejected"] is True
    assert item["report"]["plan_rejection"] == "existing-source-block-not-preserved"
    assert _changed_paths(target_repo, item["commit_sha"]) == [source]
    page = subprocess.check_output(
        ["git", "show", "main:wiki/concepts/Agent Harness.md"], cwd=target_repo, text=True
    )
    assert original_text in page
    assert f"(Source: `{original_source}`)" in page


def test_valid_create_does_not_invoke_contract_correction(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="morgan", display_name="Morgan")
    _receipt, source = _capture(
        clean_queue,
        store,
        actor=actor,
        audience=None,
        key="semantic-revision-pure-create",
        text="A source establishes a new standalone concept.",
    )
    plan = FilingPlan(
        summary="Created a standalone concept.",
        mutations=(
            PageMutation(
                action="create",
                role="concept",
                title="Standalone Concept",
                body=_body("Standalone Concept", "The source establishes a reusable concept.", source),
                reason="The concept has independent future reuse.",
            ),
        ),
    )

    class NoRevisionPlanner(RevisionPlanner):
        def revise(self, **_kwargs):
            raise AssertionError("a valid plan must not request contract correction")

    planner = NoRevisionPlanner(plan, plan)
    item, outcome = _process(clean_queue, target_repo, store, planner)

    assert outcome.status == schema.LANDED
    assert planner.revision_calls == []
    assert item["report"]["model_requests"] == 1
    assert item["report"]["semantic_revision_required"] is False
    assert item["report"]["semantic_revision_attempted"] is False


def test_failed_contract_correction_never_falls_back_to_the_draft(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="morgan", display_name="Morgan")
    _seed_page(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="semantic-revision-failure-seed",
        title="Existing Concept",
        text="The existing concept remains durable.",
    )
    _receipt, source = _capture(
        clean_queue,
        store,
        actor=actor,
        audience=None,
        key="semantic-revision-failure",
        text="The source would otherwise rewrite the existing concept.",
    )
    draft = FilingPlan(
        summary="Draft rewrite.",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/concepts/Existing Concept.md",
                body=_body("Existing Concept", "Draft-only content must not land.", source),
                reason="The draft changes visible knowledge.",
            ),
        ),
    )
    planner = RevisionPlanner(draft, RepairPlan(summary="Not a FilingPlan."))

    item, outcome = _process(clean_queue, target_repo, store, planner)

    assert outcome.status == schema.LANDED
    assert item["report"]["plan_rejected"] is True
    assert item["report"]["plan_rejection"] == "filing correction failed"
    assert _changed_paths(target_repo, item["commit_sha"]) == [source]
    existing = subprocess.check_output(
        ["git", "show", "main:wiki/concepts/Existing Concept.md"], cwd=target_repo, text=True
    )
    assert "Draft-only content" not in existing


def test_visible_update_fails_closed_when_the_draft_consumes_the_request_budget(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="morgan", display_name="Morgan")
    _seed_page(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="semantic-revision-budget-seed",
        title="Budgeted Concept",
        text="The seeded concept is visible.",
    )
    _receipt, source = _capture(
        clean_queue,
        store,
        actor=actor,
        audience=None,
        key="semantic-revision-budget",
        text="The source asks for a visible update.",
    )
    draft = FilingPlan(
        summary="Budget-exhausting draft.",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/concepts/Budgeted Concept.md",
                body=_body("Budgeted Concept", "The draft must be reviewed.", source),
                reason="The visible page changes.",
            ),
        ),
    )

    class ExhaustedPlanner(RevisionPlanner):
        def revise(self, **_kwargs):
            raise AssertionError("no additional model request is permitted")

    planner = ExhaustedPlanner(draft, draft, planning_requests=3)
    item, outcome = _process(clean_queue, target_repo, store, planner)

    assert outcome.status == schema.LANDED
    assert planner.revision_calls == []
    assert item["report"]["model_requests"] == 3
    assert item["report"]["plan_rejection"] == "filing correction budget exhausted"
    assert _changed_paths(target_repo, item["commit_sha"]) == [source]


def test_contract_correction_receives_only_the_same_acl_filtered_context(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    _seed_page(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="bob", display_name="Bob"),
        audience=("finance",),
        key="semantic-revision-private-seed",
        title="Private Finance Concept",
        text="Private finance context must remain hidden.",
    )
    actor = Actor(subject="alice", display_name="Alice")
    _seed_page(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=("engineering",),
        key="semantic-revision-visible-seed",
        title="Engineering Concept",
        text="Engineering context remains visible to Alice.",
    )
    _receipt, source = _capture(
        clean_queue,
        store,
        actor=actor,
        audience=("engineering",),
        key="semantic-revision-acl",
        text="Engineering Concept gains a source-backed update.",
    )
    draft = FilingPlan(
        summary="Updated engineering context.",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/concepts/Engineering Concept.md",
                body=_body("Engineering Concept", "The engineering concept remains current.", source),
                reason="The source updates visible engineering knowledge.",
            ),
        ),
    )
    planner = RevisionPlanner(draft, draft)

    _item, outcome = _process(clean_queue, target_repo, store, planner)

    assert outcome.status == schema.LANDED
    assert planner.revision_calls[0]["context"] == planner.plan_context
    assert "Private Finance Concept" not in planner.plan_context
    assert "Private finance context" not in planner.plan_context


def test_an_update_that_inserts_a_dated_line_into_a_cited_timeline_keeps_the_whole_plan(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="morgan", display_name="Morgan")
    _receipt, prior = _capture(clean_queue, store, actor=actor, audience=None, key="timeline-seed",
                               text="Ana met Acme in April and joined its board in July.")
    timeline = (f"- 2026-04-20: Ana met [[Acme]]. (Source: `{prior}`)\n"
                f"- 2026-07-16: Ana joined the board of [[Acme]]. (Source: `{prior}`)")
    seed = FilingPlan(summary="Recorded Ana.", mutations=(
        PageMutation(action="create", role="entity", entity_type="person", title="Ana",
                     body=f"# Ana\n\n{timeline}", reason="The capture names a person."),
        PageMutation(action="create", role="entity", entity_type="organization", title="Acme",
                     body=f"# Acme\n\nAcme is a company. (Source: `{prior}`)", reason="Named."),
    ))
    _item, outcome = _process(clean_queue, target_repo, store, ScriptedPlanner(seed))
    assert outcome.status == schema.LANDED

    _receipt, source = _capture(clean_queue, store, actor=actor, audience=None, key="timeline-update",
                                text="In May Ana pitched Acme's bridge round to two investors.")
    inserted = f"- 2026-05-15: Ana pitched the [[Acme]] bridge round. (Source: `{source}`)"
    lines = timeline.splitlines()
    plan = FilingPlan(summary="Added the pitch to Ana's timeline.", mutations=(
        PageMutation(action="update", path="wiki/entities/Ana.md",
                     body=f"# Ana\n\n{lines[0]}\n{inserted}\n{lines[1]}",
                     reason="The source adds a dated event."),
        PageMutation(action="update", path="wiki/entities/Acme.md",
                     body=f"# Acme\n\nAcme is a company. (Source: `{prior}`)\n\n"
                          f"Acme raised a bridge round in May 2026. (Source: `{source}`)",
                     reason="The source adds the round."),
    ))
    planner = RevisionPlanner(plan, plan)

    item, outcome = _process(clean_queue, target_repo, store, planner)

    assert outcome.status == schema.LANDED
    assert planner.revision_calls == []
    assert item["report"]["plan_rejected"] is False and item["report"]["wiki_changes"] == 2
    page = subprocess.check_output(["git", "show", "main:wiki/entities/Ana.md"], cwd=target_repo, text=True)
    assert inserted in page and lines[0] in page and lines[1] in page


def test_an_update_that_rewords_a_cited_line_is_still_refused(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="morgan", display_name="Morgan")
    _receipt, prior = _capture(clean_queue, store, actor=actor, audience=None, key="reword-seed",
                               text="The Acme agreement is a draft with blank signature blocks.")
    seed = FilingPlan(summary="Recorded Acme.", mutations=(
        PageMutation(action="create", role="entity", entity_type="organization", title="Acme",
                     body=f"# Acme\n\n- 2026-08-01: The Acme agreement is an unsigned draft. (Source: `{prior}`)\n"
                          f"- 2026-08-02: Acme proposed a board seat. (Source: `{prior}`)",
                     reason="Named."),
    ))
    _item, outcome = _process(clean_queue, target_repo, store, ScriptedPlanner(seed))
    assert outcome.status == schema.LANDED

    _receipt, source = _capture(clean_queue, store, actor=actor, audience=None, key="reword-update",
                                text="Acme asked about the agreement again.")
    plan = FilingPlan(summary="Rewrote the agreement line.", mutations=(
        PageMutation(action="update", path="wiki/entities/Acme.md",
                     body=f"# Acme\n\n- 2026-08-01: The Acme agreement was signed. (Source: `{prior}`)\n"
                          f"- 2026-08-02: Acme proposed a board seat. (Source: `{prior}`)\n"
                          f"- 2026-09-01: Acme asked about the agreement. (Source: `{source}`)",
                     reason="The source mentions the agreement."),
    ))

    item, outcome = _process(clean_queue, target_repo, store, RevisionPlanner(plan, plan))

    assert item["report"]["plan_rejected"] is True
    assert item["report"]["plan_rejection"] == "existing-source-block-not-preserved"


def test_the_correction_names_the_page_and_the_cited_lines_the_draft_lost(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="morgan", display_name="Morgan")
    _receipt, prior = _capture(clean_queue, store, actor=actor, audience=None, key="named-seed",
                               text="Acme hired Ana in March.")
    kept = f"- 2026-03-01: Acme hired Ana. (Source: `{prior}`)"
    seed = FilingPlan(summary="Recorded Acme.", mutations=(
        PageMutation(action="create", role="entity", entity_type="organization", title="Acme",
                     body=f"# Acme\n\n{kept}", reason="Named."),
    ))
    _item, outcome = _process(clean_queue, target_repo, store, ScriptedPlanner(seed))
    assert outcome.status == schema.LANDED

    _receipt, source = _capture(clean_queue, store, actor=actor, audience=None, key="named-update",
                                text="Acme opened an office in Lisbon in May.")
    added = f"- 2026-05-01: Acme opened a Lisbon office. (Source: `{source}`)"
    draft = FilingPlan(summary="Replaced the timeline.", mutations=(
        PageMutation(action="update", path="wiki/entities/Acme.md", body=f"# Acme\n\n{added}",
                     reason="The source adds an office."),
    ))
    revision = FilingPlan(summary="Kept the hire and added the office.", mutations=(
        PageMutation(action="update", path="wiki/entities/Acme.md", body=f"# Acme\n\n{kept}\n{added}",
                     reason="The source adds an office."),
    ))
    planner = RevisionPlanner(draft, revision)

    item, outcome = _process(clean_queue, target_repo, store, planner)

    assert outcome.status == schema.LANDED and item["report"]["semantic_revision_applied"] is True
    (failure,) = [violation for violation in planner.revision_calls[0]["violations"]
                  if violation["code"] == "existing-source-block-not-preserved"]
    assert failure["path"] == "wiki/entities/Acme.md"
    assert kept in failure["message"]


def test_a_correction_that_still_drops_cited_lines_files_the_rest_of_the_plan(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="morgan", display_name="Morgan")
    _receipt, prior = _capture(clean_queue, store, actor=actor, audience=None, key="salvage-seed",
                               text="Acme hired Ana in March.")
    kept = f"- 2026-03-01: Acme hired Ana. (Source: `{prior}`)"
    seed = FilingPlan(summary="Recorded Acme.", mutations=(
        PageMutation(action="create", role="entity", entity_type="organization", title="Acme",
                     body=f"# Acme\n\n{kept}", reason="Named."),
    ))
    _item, outcome = _process(clean_queue, target_repo, store, ScriptedPlanner(seed))
    assert outcome.status == schema.LANDED

    _receipt, source = _capture(clean_queue, store, actor=actor, audience=None, key="salvage-update",
                                text="Acme's board met in May and approved a Lisbon office.")
    lossy = FilingPlan(summary="Recorded the board meeting.", mutations=(
        PageMutation(action="create", role="note", title="Acme board meeting 2026-05",
                     body=f"# Acme board meeting 2026-05\n\n[[Acme]]'s board approved a Lisbon office. "
                          f"(Source: `{source}`)", reason="A dated meeting."),
        PageMutation(action="update", path="wiki/entities/Acme.md",
                     body=f"# Acme\n\n- 2026-05-01: Acme approved a Lisbon office. (Source: `{source}`)",
                     reason="The source adds an office."),
    ))

    item, outcome = _process(clean_queue, target_repo, store, RevisionPlanner(lossy, lossy))

    assert outcome.status == schema.LANDED and item["report"]["plan_rejected"] is False
    assert item["report"]["plan_skipped"] == [
        "update of wiki/entities/Acme.md was not applied: it would drop cited lines"]
    note = subprocess.check_output(["git", "show", "main:wiki/notes/Acme board meeting 2026-05.md"],
                                   cwd=target_repo, text=True)
    assert "approved a Lisbon office" in note
    page = subprocess.check_output(["git", "show", "main:wiki/entities/Acme.md"], cwd=target_repo, text=True)
    assert kept in page and "Lisbon" not in page


@pytest.mark.parametrize("revised_to_pages", [True, False])
def test_an_empty_plan_gets_one_correction_before_the_source_is_archived(clean_queue, target_repo, revised_to_pages):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="morgan", display_name="Morgan")
    _receipt, source = _capture(clean_queue, store, actor=actor, audience=None, key=f"empty-{revised_to_pages}",
                                text="Acme's board approved a Lisbon office in May 2026.")
    empty = FilingPlan(summary="Created a note for the board meeting.", mutations=())
    note = FilingPlan(summary="Recorded the board meeting.", mutations=(
        PageMutation(action="create", role="note", title=f"Acme board meeting {revised_to_pages}",
                     body=f"# Acme board meeting {revised_to_pages}\n\nAcme's board approved a Lisbon office. "
                          f"(Source: `{source}`)", reason="A dated meeting."),
    ))
    planner = RevisionPlanner(empty, note if revised_to_pages else empty)

    item, outcome = _process(clean_queue, target_repo, store, planner)

    assert outcome.status == schema.LANDED
    (failure,) = planner.revision_calls[0]["violations"]
    assert failure["code"] == "empty-plan"
    assert item["report"]["plan_rejected"] is False
    assert item["report"]["wiki_changes"] == (1 if revised_to_pages else 0)
