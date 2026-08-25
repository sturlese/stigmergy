"""Capture reports describe accepted writer effects, never discarded plan prose."""
from stigmergy.capture import evidence, schema
from stigmergy.capture.schema import Actor
from stigmergy.capture.service import CaptureService
from stigmergy.knowledge.plan import FilingPlan, PageMutation
from stigmergy.knowledge.planner import ScriptedPlanner
from stigmergy.knowledge.writer import WriterDeps
from stigmergy.librarian import config, worker


def test_report_falls_back_when_every_claimed_wiki_change_is_skipped(clean_queue, target_repo):
    """A model summary cannot claim an update that the writer did not accept."""
    store = evidence.MemoryEvidenceStore()
    CaptureService(clean_queue, store).capture_text(
        actor=Actor(subject="marc", display_name="Marc"),
        audience=None,
        adapter="mcp",
        text="The supplied note names a page that does not exist.",
        idempotency_key="report-reconciles-skipped-plan",
    )
    plan = FilingPlan(
        summary="Updated the non-existent operational policy",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/notes/Missing operational policy.md",
                body="# Missing operational policy\n\nThe policy changed.",
                reason="The supplied note supposedly updates this policy",
            ),
        ),
    )

    item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(
            config.Settings(repo=str(target_repo), branch="main", backend="scripted"),
            store,
            ScriptedPlanner(plan),
            str(target_repo),
        ),
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["wiki_changes"] == 0
    assert item["report"]["plan_skipped"] == [
        "mutation[0] update: planned page does not exist",
    ]
    assert item["report"]["summary"] == "Archived source without wiki changes"


def test_report_reconciles_partial_plans_without_repeating_the_model_summary(
    clean_queue, target_repo
):
    """Accepted and skipped effects, not planner prose, are the operator-facing result."""
    store = evidence.MemoryEvidenceStore()
    CaptureService(clean_queue, store).capture_text(
        actor=Actor(subject="marc", display_name="Marc"),
        audience=None,
        adapter="mcp",
        text="The approved policy is now weekly.",
        idempotency_key="report-reconciles-partial-plan",
    )
    plan = FilingPlan(
        summary="Replaced every operational policy across the organization",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Weekly policy",
                body="# Weekly policy\n\nThe approved policy is now weekly.",
                reason="The supplied evidence records the approved weekly policy",
            ),
            PageMutation(
                action="update",
                path="wiki/notes/Missing operational policy.md",
                body="# Missing operational policy\n\nThe policy changed.",
                reason="The supplied note supposedly updates this policy",
            ),
        ),
    )

    item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(
            config.Settings(repo=str(target_repo), branch="main", backend="scripted"),
            store,
            ScriptedPlanner(plan),
            str(target_repo),
        ),
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["wiki_changes"] == 1
    assert item["report"]["plan_skipped"] == [
        "mutation[1] update: planned page does not exist",
    ]
    assert item["report"]["summary"] == "Applied 1 wiki change(s); skipped 1 plan operation(s)"


def test_report_reconciles_fully_accepted_plans_without_repeating_model_prose(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    CaptureService(clean_queue, store).capture_text(
        actor=Actor(subject="marc", display_name="Marc"),
        audience=None,
        adapter="mcp",
        text="The approved policy is now monthly.",
        idempotency_key="report-reconciles-full-plan",
    )
    plan = FilingPlan(
        summary="Completed unrelated worldwide organizational restructuring",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Monthly policy",
                body="# Monthly policy\n\nThe approved policy is now monthly.",
                reason="The supplied evidence records the approved monthly policy",
            ),
        ),
    )

    item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(
            config.Settings(repo=str(target_repo), branch="main", backend="scripted"),
            store,
            ScriptedPlanner(plan),
            str(target_repo),
        ),
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["wiki_changes"] == 1
    assert item["report"]["plan_skipped"] == []
    assert item["report"]["summary"] == "Applied 1 wiki change(s); skipped 0 plan operation(s)"
