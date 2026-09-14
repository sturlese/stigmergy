import json
import subprocess

from stigmergy.capture import evidence, queue, schema
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
                entities=(),
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


def test_visible_update_uses_full_semantic_revision_before_application(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="marc", display_name="Marc")
    _seed_page(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="semantic-revision-seed",
        title="Agent Harness",
        text="An agent harness is the lower-level operational system around a model.",
    )
    receipt, source = _capture(
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
        summary="Preserved both reusable conceptual levels.",
        mutations=(
            PageMutation(
                action="create",
                role="concept",
                title="Harness Engineering",
                body=_body(
                    "Harness Engineering",
                    "Harness Engineering designs the [[Agent Harness]], the operational system.",
                    source,
                ),
                entities=(),
                reason="The source defines a broader reusable discipline.",
            ),
            PageMutation(
                action="update",
                path="wiki/concepts/Agent Harness.md",
                body=_body(
                    "Agent Harness",
                    "The [[Agent Harness]] implements the broader [[Harness Engineering]] discipline.",
                    source,
                ),
                reason="The lower-level system remains independently reusable.",
            ),
        ),
    )
    planner = RevisionPlanner(draft, revision)

    item, outcome = _process(clean_queue, target_repo, store, planner)

    assert outcome.status == schema.LANDED
    assert planner.revision_calls[0]["context"] == planner.plan_context
    assert planner.revision_calls[0]["draft"] == draft
    assert planner.revision_calls[0]["source_path"] == source
    assert planner.revision_calls[0]["max_requests"] == 1
    assert item["report"]["model_requests"] == 2
    assert item["report"]["planning_model_requests"] == 1
    assert item["report"]["semantic_revision_model_requests"] == 1
    assert item["report"]["semantic_revision_required"] is True
    assert item["report"]["semantic_revision_attempted"] is True
    assert item["report"]["semantic_revision_applied"] is True
    assert item["report"]["repair_model_requests"] == 0
    assert "Harness Engineering" in subprocess.check_output(
        ["git", "show", "main:wiki/concepts/Harness Engineering.md"], cwd=target_repo, text=True
    )


def test_pure_create_does_not_invoke_semantic_revision(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="marc", display_name="Marc")
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
                entities=(),
                reason="The concept has independent future reuse.",
            ),
        ),
    )

    class NoRevisionPlanner(RevisionPlanner):
        def revise(self, **_kwargs):
            raise AssertionError("pure creates must not request semantic revision")

    planner = NoRevisionPlanner(plan, plan)
    item, outcome = _process(clean_queue, target_repo, store, planner)

    assert outcome.status == schema.LANDED
    assert planner.revision_calls == []
    assert item["report"]["model_requests"] == 1
    assert item["report"]["semantic_revision_required"] is False
    assert item["report"]["semantic_revision_attempted"] is False


def test_failed_semantic_revision_never_falls_back_to_the_draft(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="marc", display_name="Marc")
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
    assert item["report"]["plan_rejection"] == "semantic revision failed"
    assert _changed_paths(target_repo, item["commit_sha"]) == [source]
    existing = subprocess.check_output(
        ["git", "show", "main:wiki/concepts/Existing Concept.md"], cwd=target_repo, text=True
    )
    assert "Draft-only content" not in existing


def test_visible_update_fails_closed_when_the_draft_consumes_the_two_request_budget(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="marc", display_name="Marc")
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
            raise AssertionError("no third model request is permitted")

    planner = ExhaustedPlanner(draft, draft, planning_requests=2)
    item, outcome = _process(clean_queue, target_repo, store, planner)

    assert outcome.status == schema.LANDED
    assert planner.revision_calls == []
    assert item["report"]["model_requests"] == 2
    assert item["report"]["plan_rejection"] == "semantic revision budget exhausted"
    assert _changed_paths(target_repo, item["commit_sha"]) == [source]


def test_semantic_revision_receives_only_the_same_acl_filtered_context(clean_queue, target_repo):
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


def test_authorized_page_outside_bounded_context_still_requires_semantic_revision(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="marc", display_name="Marc")
    for index in range(13):
        _seed_page(
            clean_queue,
            target_repo,
            store,
            actor=actor,
            audience=None,
            key=f"semantic-revision-candidate-{index}",
            title=f"Candidate {index:02}",
            text="Common signal keeps records current.",
        )
    _seed_page(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="semantic-revision-authorized-target",
        title="Zebra Target",
        text="Common signal keeps records current.",
    )
    _receipt, source = _capture(
        clean_queue,
        store,
        actor=actor,
        audience=None,
        key="semantic-revision-authorized-update",
        text="Common signal keeps records current.",
    )
    draft = FilingPlan(
        summary="Updated an authorized page outside the bounded context.",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/concepts/Zebra Target.md",
                body=_body("Zebra Target", "The durable target remains current.", source),
                reason="The source updates an existing durable page.",
            ),
        ),
    )
    planner = RevisionPlanner(draft, draft)

    item, outcome = _process(clean_queue, target_repo, store, planner)

    context = json.loads(planner.plan_context)
    assert outcome.status == schema.LANDED
    assert len(context["candidates"]) == 12
    assert "wiki/concepts/Zebra Target.md" not in {
        candidate["path"] for candidate in context["candidates"]
    }
    assert len(planner.revision_calls) == 1
    assert item["report"]["semantic_revision_required"] is True


def test_inaccessible_page_update_never_becomes_semantic_revision_eligible(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    _seed_page(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="bob", display_name="Bob"),
        audience=("finance",),
        key="semantic-revision-inaccessible-seed",
        title="Private Finance Target",
        text="Private finance evidence remains restricted.",
    )
    actor = Actor(subject="alice", display_name="Alice")
    _receipt, source = _capture(
        clean_queue,
        store,
        actor=actor,
        audience=("engineering",),
        key="semantic-revision-inaccessible-update",
        text="Engineering evidence must not reveal finance context.",
    )
    draft = FilingPlan(
        summary="Attempted hidden update.",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/concepts/Private Finance Target.md",
                body=_body("Private Finance Target", "Hidden content must not land.", source),
                reason="The hidden target is not available to this capture.",
            ),
        ),
    )
    planner = RevisionPlanner(draft, draft)

    item, outcome = _process(clean_queue, target_repo, store, planner)

    assert outcome.status == schema.LANDED
    assert planner.revision_calls == []
    assert "Private Finance Target" not in planner.plan_context
    assert item["report"]["semantic_revision_required"] is False


def test_recompile_revises_a_create_only_draft_when_prior_pages_exist(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="marc", display_name="Marc")
    source = _seed_page(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="semantic-revision-recompile-seed",
        title="Recompile Target",
        text="The source defines a recompiled durable concept.",
    )
    draft = FilingPlan(
        summary="Recreated the source-backed concept.",
        mutations=(
            PageMutation(
                action="create",
                role="concept",
                title="Recompile Target",
                body=_body("Recompile Target", "The durable concept is recreated from its source.", source),
                entities=(),
                reason="The current compiler recreates the source-backed page.",
            ),
        ),
    )
    planner = RevisionPlanner(draft, draft)
    queue.enqueue_garden(
        clean_queue,
        schema.GardenRequest(
            idempotency_key="semantic-revision-recompile",
            actor=actor,
            rationale="Recompile the current source-backed graph.",
            mode="recompile",
        ),
    )

    item, outcome = _process(clean_queue, target_repo, store, planner)

    assert outcome.status == schema.LANDED
    assert len(planner.revision_calls) == 1
    assert planner.revision_calls[0]["draft"].mutations[0].action == "create"
    assert planner.revision_calls[0]["max_requests"] == 1
    assert item["report"]["semantic_revision_count"] == 1
