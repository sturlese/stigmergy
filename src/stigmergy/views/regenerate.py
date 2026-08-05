"""views.regenerate — orchestration: staleness, force, removal, and the batch entry points both
the CLI and the librarian worker call.

**One commit per entity.** `regenerate_entity` performs its own commit-and-push per call, so a
batch (`--stale`/`--all`, or the worker's touched-entity set) is N independent commits, not one
commit for the whole run. A run that fails halfway leaves a coherent repo — the entities already
regenerated are genuinely done — and Ctrl-C is statable ("N entities regenerated, the rest
untouched"). This differs deliberately from the meeting flow's atomicity rule (ADR 020 D4: one
meeting capture is one indivisible page set); here each entity's view is independent of every
other entity's, so there is no shared invariant a partial batch could violate.
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

# `view_relpath`/`view_path`/`existing_member_hash`/`existing_view_ids`/
# `list_stale_entities`/`list_all_anchored_entities` live in `views.staleness` — the READ-ONLY
# half of view regeneration, kept separate so `gardener.checks` can reuse the staleness reads
# without loading THIS module's write path (`writer.commit_and_push` — see `staleness.py`'s own
# docstring for the full reasoning). They are imported here so every call site in this package
# (`views/cli.py`, `regenerate_entity` below) reaches them as `regenerate.X`: this file
# orchestrates the write, `staleness.py` only reads.
# `existing_view_ids`/`list_stale_entities`/`list_all_anchored_entities` are not called from THIS
# module's own code below (only `view_relpath`/`view_path`/`existing_member_hash` are) —
# `__all__` states plainly that they are re-exported on purpose, the same convention
# `digest.settings` uses for its own re-export of `gardener.settings`'s two env names.
__all__ = ["view_relpath", "existing_view_ids", "list_all_anchored_entities",
          "list_stale_entities", "regenerate_entity", "run", "RegenOutcome", "RunResult"]


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
        # A view ORPHANED BY DE-REGISTRATION: it once had a registry entry — that is the only
        # way it could have been written at all — and now has none. Without this branch it would
        # be stuck against the refusal above forever: `--entity` would refuse it every time and
        # `--stale` would list it stale every run (its member set cannot even be checked without
        # a live registry entry) with no way to ever act on it. A de-registered entity has, by
        # definition, no governed member set left to evaluate, so this is unconditionally a
        # removal — the same outcome the "last member vanishes" case below reaches, by a
        # different route.
        if guarded:
            writer.ensure_on_branch(repo, branch)
            writer.ensure_clean(repo)
        os.remove(view_path(repo, entity_id))
        sha = writer.commit_and_push(
            repo, branch=branch,
            message=f"chore(views): remove {entity_id} — entity de-registered\n")
        return RegenOutcome(entity_id=entity_id, entity_name=entity_id, action="removed",
                            commit=sha, path=view_relpath(entity_id))
    entity_name = entity["name"]
    members = skeleton.members_of(repo, entity_id)

    if not members:
        if existing_member_hash(repo, entity_id) is None:
            return RegenOutcome(
                entity_id=entity_id, entity_name=entity_name, action="refused-no-members",
                message=f'no page anywhere in the repo declares entity: ["{entity_id}"] yet')
        if guarded:
            writer.ensure_on_branch(repo, branch)
            writer.ensure_clean(repo)
        os.remove(view_path(repo, entity_id))
        sha = writer.commit_and_push(
            repo, branch=branch,
            message=f"chore(views): remove {entity_id} — no anchored pages remain\n")
        return RegenOutcome(entity_id=entity_id, entity_name=entity_name, action="removed",
                            commit=sha, path=view_relpath(entity_id))

    member_hash = skeleton.member_hash(members)
    existing_hash = existing_member_hash(repo, entity_id)
    if not force and existing_hash == member_hash:
        return RegenOutcome(entity_id=entity_id, entity_name=entity_name, action="unchanged",
                            member_count=len(members), path=view_relpath(entity_id))

    # The dirty-tree/wrong-branch guards run BEFORE any per-write work: refusing after the
    # synthesis below would cost a full agent run to reach a refusal that was already knowable
    # from a dirty clone or a detached HEAD.
    if guarded:
        writer.ensure_on_branch(repo, branch)
        writer.ensure_clean(repo)

    entity_page = skeleton.entity_own_page(members)
    title = entity_page.title if entity_page else entity_name
    timeline_ordered = skeleton.timeline_order(members)
    timeline_md = skeleton.render_timeline(members)
    # The view's own audience, computed ONCE from members only (never widened by what follows)
    # and threaded into every feed drawing on GOVERNED BUT NON-MEMBER sources — `render.render`
    # recomputes the same, pure value again below for the frontmatter; duplicating a cheap,
    # deterministic computation costs nothing and keeps `render`'s own tested contract (it
    # computes `acl` internally) intact.
    view_audience = view_acl([m.acl for m in members])
    backlink_rows = skeleton.backlinks_of(repo, entity_page, view_acl=view_audience)
    backlinks_md = skeleton.render_backlinks(backlink_rows, entity_title=title)

    agent = synthesis.build_view_agent()
    result = await synthesis.write_synthesis(agent, entity_id, repo, members)

    page = render.render(entity_id, title, members, member_hash=member_hash,
                         timeline_md=timeline_md, backlinks_md=backlinks_md,
                         synthesis_body=result.body_markdown, shipped=result.shipped)

    # Decided from `existing_hash`, read above BEFORE anything was written — never from a fresh
    # `existing_member_hash` call, which after the write below would read back the content this
    # very call just produced and report "regenerate" for what is actually a first write.
    verb = "regenerate" if existing_hash is not None else "write"
    # Local name deliberately NOT `view_path` — that name is the imported `staleness.
    # view_path` FUNCTION this whole file calls above; shadowing it with a local variable would
    # make every earlier call in this function resolve to the (not-yet-assigned) local instead,
    # an UnboundLocalError waiting to happen the next time this function is reordered.
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
        timeline_shown=min(len(timeline_ordered), skeleton.TIMELINE_CAP))


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
    """The shared base every caller (the CLI's three flags, the worker's touched-entity trigger)
    funnels through: one `job_runs` row for the WHOLE batch, one commit per entity inside it.

    **Stats are updated INCREMENTALLY, after every entity** — not once after the whole loop.
    `ops.job_run`'s own `except Exception` records whatever `stats` holds at the moment of a
    fault, so updating it only at the end would write an EMPTY stats row for an exception at
    entity k of n even though k-1 commits had already landed: a `job_runs` audit trail lying by
    omission about real, already-pushed work.

    **A `KeyboardInterrupt` gets its own explicit record.** `job_run`'s `except Exception` cannot
    see a `KeyboardInterrupt` — deliberately, so an operator's Ctrl-C is never mistaken for an
    ordinary job fault by anything that catches broadly — so it propagates straight past that
    bookkeeping without a row at all. The `except KeyboardInterrupt` below writes the same
    error-shaped row `job_run` would have written for an ordinary exception, using whatever
    `result.stats` already holds, before re-raising."""
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
