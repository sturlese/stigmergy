"""views.regenerate — orchestration: staleness, force, removal, and the batch entry points both
the CLI and the librarian worker call.

One commit per entity: a batch is N independent commits, so a run that fails halfway leaves a
coherent repo and a statable Ctrl-C. Deliberately unlike the meeting flow's one-indivisible-
page-set rule — each entity's view is independent, so no shared invariant spans a batch.
"""
import os
from dataclasses import dataclass, field

from stigmergy.capture import ops
from stigmergy.kernel.acl import view_acl
from stigmergy.kernel.fsutil import write_text_atomic
from stigmergy.kernel.registry import Registry
from stigmergy.views import render, skeleton, synthesis, writer
from stigmergy.views.staleness import (
    existing_member_hash,
    existing_view_ids,
    list_all_anchored_entities,
    list_stale_entities,
    view_path,
    view_relpath,
)

JOB_NAME = "views"

# The staleness reads live in `views.staleness` (the read-only half, importable by
# `gardener.checks` without this module's write path) and are re-exported here on purpose —
# `__all__` says so — so every call site reaches them as `regenerate.X`.
__all__ = ["view_relpath", "view_path", "existing_view_ids", "existing_member_hash",
          "list_all_anchored_entities", "list_stale_entities", "regenerate_entity", "run",
          "RegenOutcome", "RunResult"]


@dataclass(frozen=True)
class RegenOutcome:
    entity_id: str
    entity_name: str
    # "written" | "removed" | "unchanged" | "refused-unknown-entity" | "refused-no-members"
    action: str
    message: str = ""
    member_count: int = 0
    synthesis_shipped: bool = True
    acl: list[str] | None = None
    commit: str | None = None
    path: str = ""
    timeline_total: int = 0
    timeline_shown: int = 0


def _remove_view(repo: str, entity_id: str, *, entity_name: str, branch: str, guarded: bool,
                 commit_message: str) -> RegenOutcome:
    """Delete a view that has no member set left, and commit the deletion. Both roads here — a
    de-registered entity, and an entity whose last anchored page went away — are the same write;
    only the commit message differs, because the two reasons are not one reason."""
    if guarded:
        writer.ensure_on_branch(repo, branch)
        writer.ensure_clean(repo)
    os.remove(view_path(repo, entity_id))
    sha = writer.commit_and_push(repo, branch=branch, message=commit_message)
    return RegenOutcome(entity_id=entity_id, entity_name=entity_name, action="removed",
                        commit=sha, path=view_relpath(entity_id))


async def regenerate_entity(repo: str, entity_id: str, *, registry: Registry, branch: str = "main",
                            force: bool = False, guarded: bool = True) -> RegenOutcome:
    """One entity, one call, one commit (or none, on a no-op/refusal). `guarded=True` (the CLI's
    steward-clone path) runs the dirty-tree/wrong-branch checks before writing anything;
    `guarded=False` (the librarian worker, whose ephemeral worktree is always a fresh checkout)
    skips them, since neither condition is reachable there."""
    entity = registry.entities.get(entity_id)
    if entity is None:
        existing_hash = existing_member_hash(repo, entity_id)
        if existing_hash is None:
            return RegenOutcome(entity_id=entity_id, entity_name=entity_id,
                                action="refused-unknown-entity",
                                message=f'"{entity_id}" is not a registered entity')
        # A view orphaned by de-registration: without this branch `--entity` would refuse it and
        # `--stale` would list it forever, with no way to act. No governed member set remains, so
        # this is unconditionally a removal.
        return _remove_view(
            repo, entity_id, entity_name=entity_id, branch=branch, guarded=guarded,
            commit_message=f"chore(views): remove {entity_id} — entity de-registered\n")
    entity_name = entity["name"]
    members = skeleton.members_of(repo, entity_id)

    if not members:
        if existing_member_hash(repo, entity_id) is None:
            return RegenOutcome(
                entity_id=entity_id, entity_name=entity_name, action="refused-no-members",
                message=f'no page anywhere in the repo declares entity: ["{entity_id}"] yet')
        return _remove_view(
            repo, entity_id, entity_name=entity_name, branch=branch, guarded=guarded,
            commit_message=f"chore(views): remove {entity_id} — no anchored pages remain\n")

    member_hash = skeleton.member_hash(members)
    existing_hash = existing_member_hash(repo, entity_id)
    if not force and existing_hash == member_hash:
        return RegenOutcome(entity_id=entity_id, entity_name=entity_name, action="unchanged",
                            member_count=len(members), path=view_relpath(entity_id))

    # The guards run BEFORE any per-write work — refusing after the synthesis would cost a full
    # agent run to reach a refusal already knowable from a dirty clone or a detached HEAD.
    if guarded:
        writer.ensure_on_branch(repo, branch)
        writer.ensure_clean(repo)

    entity_page = skeleton.entity_own_page(members)
    title = entity_page.title if entity_page else entity_name
    timeline_ordered = skeleton.timeline_order(members)
    timeline_md = skeleton.render_timeline(members)
    # The view's audience, computed once from MEMBERS only and threaded into every governed but
    # non-member feed; `render.render` recomputes the same pure value for the frontmatter.
    view_audience = view_acl([m.acl for m in members])
    backlink_rows = skeleton.backlinks_of(repo, entity_page, view_acl=view_audience,
                                          exclude_path=view_relpath(entity_id))
    backlinks_md = skeleton.render_backlinks(backlink_rows, entity_title=title)

    agent = synthesis.build_view_agent()
    result = await synthesis.write_synthesis(agent, entity_id, repo, members)

    page = render.render(entity_id, title, members, member_hash=member_hash,
                         timeline_md=timeline_md, backlinks_md=backlinks_md,
                         synthesis_body=result.body_markdown, shipped=result.shipped)

    # Decided from `existing_hash`, read BEFORE anything was written — a fresh call here would
    # read back this call's own write and report "regenerate" for a first write.
    verb = "regenerate" if existing_hash is not None else "write"
    # Deliberately NOT named `view_path`: shadowing the imported function would turn every
    # earlier call in this function into an UnboundLocalError on the next reorder.
    view_file_path = view_path(repo, entity_id)
    write_text_atomic(view_file_path, page)  # atomic replace, never a truncating open()
    sha = writer.commit_and_push(
        repo, branch=branch,
        message=(f"chore(views): {verb} {entity_id} — {len(members)} page(s)"
                 f"{'' if result.shipped else ', synthesis withheld (budget)'}\n"))
    return RegenOutcome(
        entity_id=entity_id, entity_name=entity_name, action="written", commit=sha,
        path=view_relpath(entity_id), member_count=len(members),
        synthesis_shipped=result.shipped, acl=view_audience,
        timeline_total=len(timeline_ordered),
        timeline_shown=skeleton.timeline_shown(len(timeline_ordered)))


@dataclass
class RunResult:
    checked: int = 0
    outcomes: list[RegenOutcome] = field(default_factory=list)

    @property
    def stats(self) -> dict:
        counts = {"checked": self.checked, "written": 0, "withheld": 0, "removed": 0,
                 "unchanged": 0, "refused": 0}
        for o in self.outcomes:
            if o.action == "written":
                counts["written"] += 1
                if not o.synthesis_shipped:
                    counts["withheld"] += 1
            elif o.action == "removed":
                counts["removed"] += 1
            elif o.action == "unchanged":
                counts["unchanged"] += 1
            elif o.action.startswith("refused"):
                counts["refused"] += 1
        return counts


async def run(repo: str, conn, entity_ids: list[str], *, registry: Registry, branch: str = "main",
              force: bool = False, guarded: bool = True,
              job: str = JOB_NAME) -> RunResult:
    """The shared base every caller funnels through: one `job_runs` row for the WHOLE batch, one
    commit per entity inside it.

    Stats are updated incrementally, after every entity — updating only at the end would write an
    empty stats row for a fault at entity k of n, lying by omission about k-1 pushed commits. A
    `KeyboardInterrupt` gets its own explicit error row below, because `job_run`'s
    `except Exception` cannot see one."""
    result = RunResult()
    try:
        with ops.job_run(conn, job) as stats:
            for entity_id in entity_ids:
                outcome = await regenerate_entity(repo, entity_id, registry=registry, branch=branch,
                                                  force=force, guarded=guarded)
                result.outcomes.append(outcome)
                result.checked = len(result.outcomes)
                stats.update(result.stats)
    except KeyboardInterrupt:
        ops.record_job_run(conn, job, status="error", stats=result.stats, error="KeyboardInterrupt")
        raise
    return result
