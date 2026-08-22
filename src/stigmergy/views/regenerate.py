"""views.regenerate — orchestration: staleness, force, removal, and the batch entry points both
the CLI and the librarian worker call.

One commit per entity: a batch is N independent commits, so a run that fails halfway leaves a
coherent repo and a statable Ctrl-C. Deliberately unlike the meeting flow's one-indivisible-
page-set rule — each entity's view is independent, so no shared invariant spans a batch.
"""
import os
from dataclasses import dataclass, field

from stigmergy.capture import ops
from stigmergy.index import corpus
from stigmergy.kernel.acl import view_acl
from stigmergy.kernel.fsutil import write_text_atomic
from stigmergy.kernel.registry import Registry
from stigmergy.views import render, skeleton, synthesis, writer
from stigmergy.views.staleness import (
    ENTITY_ID_RULE,
    current_signals,
    existing_member_hash,
    existing_signals,
    existing_view_ids,
    is_usable_view_id,
    list_all_anchored_entities,
    list_stale_entities,
    list_sweep_entities,
    view_is_current,
    view_path,
    view_relpath,
)

JOB_NAME = "views"
# The periodic convergence pass's own job name, so an operator reading `job_runs` can tell a
# maintenance pass from the post-meeting hook.
SWEEP_JOB_NAME = f"{JOB_NAME}-sweep"

# The convergence pass's mutual-exclusion key. Two sweepers are a supported SHAPE (two workers on
# separate checkouts, plus an operator's `--sweep` at any moment) and a broken RUN: they double the
# model spend on the same divergence, and each one's push rebases the other's worktree mid-batch.
# A DIFFERENT key from `slack.app._SINGLETON_LOCK_KEY` and `capture.schema._STARTUP_DDL_LOCK_KEY` —
# advisory locks on one database, and a shared key would make unrelated things exclude each other.
VIEW_SWEEP_LOCK_KEY = int.from_bytes(b"SYNVIEW", "big")

# The staleness reads live in `views.staleness` (the read-only half, importable by
# `gardener.checks` without this module's write path) and are re-exported here on purpose —
# `__all__` says so — so every call site reaches them as `regenerate.X`.
__all__ = ["view_relpath", "view_path", "existing_view_ids", "existing_member_hash",
          "existing_signals", "current_signals", "view_is_current", "is_usable_view_id",
          "list_all_anchored_entities", "list_stale_entities", "list_sweep_entities",
          "regenerate_entity", "run", "sweep", "RegenOutcome", "RunResult"]


@dataclass(frozen=True)
class RegenOutcome:
    entity_id: str
    entity_name: str
    # "written" | "removed" | "unchanged" | "refused-unknown-entity" | "refused-no-members" |
    # "refused-unusable-id"
    action: str
    message: str = ""
    member_count: int = 0
    synthesis_shipped: bool = True
    acl: list[str] | None = None
    commit: str | None = None
    path: str = ""
    timeline_total: int = 0
    timeline_shown: int = 0
    # Did this entity's push lose a race and rebase the worktree onto foreign commits? A batch
    # caller reads it to stop; a single-entity caller has nothing left to invalidate.
    rebased: bool = False


@dataclass(frozen=True)
class _RemovalCause:
    """Why a view was removed, in the two lengths its two readers need: `tail` closes the commit
    subject somebody skims in `git log`, `message` is the sentence an operator reads on the terminal
    and in `--json`. ONE object per cause, so the log and the report can never name different
    causes for the same write — which is what happened while the CLI had to guess: it knew a
    removal had occurred and not which road led there, so it stated one of the two as fact for
    both."""
    tail: str
    message: str


REMOVED_DEREGISTERED = _RemovalCause(
    tail="entity de-registered",
    message="entity de-registered — its pages are untouched, but nothing in the registry governs "
            "a member set for them any more")
REMOVED_NO_MEMBERS = _RemovalCause(
    tail="no anchored pages remain",
    message="no anchored pages remain — the last page anchored to it is gone (superseded or "
            "re-anchored elsewhere)")


def _remove_view(repo: str, entity_id: str, *, entity_name: str, branch: str, guarded: bool,
                 cause: _RemovalCause) -> RegenOutcome:
    """Delete a view that has no member set left, and commit the deletion. Both roads here — a
    de-registered entity, and an entity whose last anchored page went away — are the same write;
    only the CAUSE differs, because the two reasons are not one reason, and it travels on the
    outcome as well as into the commit: a caller reading `action == "removed"` cannot re-derive
    which road was taken."""
    if guarded:
        writer.ensure_on_branch(repo, branch)
        writer.ensure_clean(repo)
    os.remove(view_path(repo, entity_id))
    landed = writer.commit_and_push(
        repo, branch=branch, message=f"chore(views): remove {entity_id} — {cause.tail}\n")
    return RegenOutcome(entity_id=entity_id, entity_name=entity_name, action="removed",
                        message=cause.message, commit=landed.sha, rebased=landed.rebased,
                        path=view_relpath(entity_id))


async def regenerate_entity(repo: str, entity_id: str, *, registry: Registry, branch: str = "main",
                            force: bool = False, guarded: bool = True,
                            rows=None) -> RegenOutcome:
    """One entity, one call, one commit (or none, on a no-op/refusal). `guarded=True` (the CLI's
    operator-clone path) runs the dirty-tree/wrong-branch checks before writing anything;
    `guarded=False` (the librarian worker, whose ephemeral worktree is always a fresh checkout)
    skips them, since neither condition is reachable there.

    `rows` is ONE `corpus.load_pages` parse a batch caller already holds, threaded down to
    `skeleton.members_of` and to the backlink half of the staleness signal. **What it shares is
    the repeated READ AND PARSE of the corpus, not the per-entity work**: `skeleton._member_rows`
    and `skeleton.backlinks_of` each still filter the whole parsed row set for every entity, and
    `existing_signals` still opens the view file once per entity (once, for both signals), so a
    converged pass stays O(population x corpus) in CPU with one open each — it is the
    O(population) re-reads and re-parses of every page on disk that go away, which is the term
    that actually dominates.

    It is safe across a batch under TWO conditions, and only the first is this module's to keep:

    1. Nothing this loop commits can change a member set — `skeleton.MEMBER_ZONES` is
       `("wiki", "sources")` and `views/` is deliberately not in it. If `views/` ever became a
       member zone the parse would go stale mid-batch and every caller would have to stop passing
       it, which is the same change that would break the staleness hash's convergence: the two
       constraints are one. The BACKLINK signal is the one thing here the loop's own commits do
       move (`backlinks_of` scans `views/` too), and it is knowingly one pass late rather than
       wrong: the next pass sees the view this one wrote. What gets WRITTEN never uses the
       snapshot — see the `backlinks_of` call below.
    2. No FOREIGN commit enters the tree mid-batch. `gitcmd.push` answers a lost race by rebasing
       this worktree onto `FETCH_HEAD`, checking somebody else's pages into the tree the batch is
       still reading — after which `members`/`member_hash`/`view_audience` come from the pre-rebase
       parse while the synthesis reads post-rebase bytes off disk, which can publish a rollup of
       NEW content under an OLD, wider `acl`. This function reports the rebase on
       `RegenOutcome.rebased`; `run` below is what acts on it.

    `None` parses the repo per entity, as every single-entity caller wants.
    """
    # BEFORE any read that would build a path from it. The population's frontmatter half is
    # `entity:` off a page — hand-editable, agent-proposable, repair-appliable — so an id no view
    # file can be named from is ordinary input here, not an attack: `Acme Corp` is what a human
    # writes where an id belongs. `view_relpath` answers it with a raise (correctly: it is the
    # choke point nothing may escape `views/` through), and a raise inside a batch loop that
    # catches only `KeyboardInterrupt` costs the WHOLE pass, every interval, forever. Refused by
    # name instead — never filtered out of the population, which would converge the pass and leave
    # the person who typed the id waiting for a rollup nobody is building.
    if not is_usable_view_id(entity_id):
        return RegenOutcome(
            entity_id=entity_id, entity_name=entity_id, action="refused-unusable-id",
            message=f'"{entity_id}" cannot name a view file — {ENTITY_ID_RULE}. Some page '
                    f'declares it in its `entity:` list; fix that anchor (or mint the entity '
                    f'under a well-formed id) and the next pass will build the view')
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
            cause=REMOVED_DEREGISTERED)
    entity_name = entity["name"]
    members = skeleton.members_of(repo, entity_id, rows=rows)

    if not members:
        if existing_member_hash(repo, entity_id) is None:
            return RegenOutcome(
                entity_id=entity_id, entity_name=entity_name, action="refused-no-members",
                message=f'no page anywhere in the repo declares entity: ["{entity_id}"] yet')
        return _remove_view(
            repo, entity_id, entity_name=entity_name, branch=branch, guarded=guarded,
            cause=REMOVED_NO_MEMBERS)

    # BOTH staleness signals, compared as a pair (`staleness.view_is_current`): the member set
    # AND the backlinks the page would cite now. The backlink half is computed off the shared
    # parse here and off a FRESH one at the write below — two moments, two needs, stated in
    # `skeleton.backlinks_of`.
    current = current_signals(repo, entity_id, members, rows=rows)
    existing = existing_signals(repo, entity_id)
    if not force and view_is_current(existing=existing, current=current):
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
    # **`rows` is deliberately NOT threaded here.** `backlinks_of` scans every indexed zone,
    # `views/` INCLUDED — another entity's view may legitimately link to this entity's page — so
    # unlike the member set it is not immune to this batch's own commits: a view written earlier in
    # the same sweep is a real backlink source, and a shared parse would silently drop it. The
    # member-set parse is shared because `MEMBER_ZONES` excludes `views/`; that argument stops
    # exactly at this line, and this parse pays for itself only on entities being rewritten anyway.
    # The staleness signal above CAN use the snapshot, because being one pass late about a
    # backlink is not the same fault as publishing a page that never had it.
    backlink_rows = skeleton.backlinks_of(repo, entity_page, view_acl=view_audience,
                                          exclude_path=view_relpath(entity_id))
    backlinks_md = skeleton.render_backlinks(backlink_rows, entity_title=title)

    agent = synthesis.build_view_agent()
    result = await synthesis.write_synthesis(agent, entity_id, repo, members)

    # The persisted `backlink_hash` is the hash of the rows this page just RENDERED — the fresh
    # parse's, never the snapshot's, so what the page claims about itself is what it shows.
    page = render.render(entity_id, title, members, member_hash=current.member_hash,
                         backlink_hash=skeleton.backlink_hash(backlink_rows),
                         timeline_md=timeline_md, backlinks_md=backlinks_md,
                         synthesis_body=result.body_markdown, shipped=result.shipped)

    # Decided from the signals read BEFORE anything was written — a fresh read here would see
    # this call's own write and report "regenerate" for a first write.
    verb = "regenerate" if existing.member_hash is not None else "write"
    # Deliberately NOT named `view_path`: shadowing the imported function would turn every
    # earlier call in this function into an UnboundLocalError on the next reorder.
    view_file_path = view_path(repo, entity_id)
    write_text_atomic(view_file_path, page)  # atomic replace, never a truncating open()
    landed = writer.commit_and_push(
        repo, branch=branch,
        message=(f"chore(views): {verb} {entity_id} — {len(members)} page(s)"
                 f"{'' if result.shipped else ', synthesis withheld (budget)'}\n"))
    return RegenOutcome(
        entity_id=entity_id, entity_name=entity_name, action="written", commit=landed.sha,
        rebased=landed.rebased,
        path=view_relpath(entity_id), member_count=len(members),
        synthesis_shipped=result.shipped, acl=view_audience,
        timeline_total=len(timeline_ordered),
        timeline_shown=skeleton.timeline_shown(len(timeline_ordered)))


# The one wording for "this run stopped at its ceiling", deliberately the SAME shape as
# `repair.proposer.RUN_CEILING_REASON` — same `run-ceiling-reached(N)` prefix, same "the next run
# will see them" tail — so an operator reading `job_runs.stats` does not learn two spellings of one
# fact. Deferred entities are not lost: the population is recomputed from state on every pass, so
# whatever this run did not reach is still divergent when the next one starts.
RUN_CEILING_REASON = (
    "run-ceiling-reached({ceiling}): this run stopped at its regeneration ceiling — {deferred} "
    "further entity(ies) in the population were not checked; the next run will see them")

# The other two ways a run stops early, in the SAME voice — an operator reading `job_runs.stats`
# learns one grammar for "this run stopped, and here is what it owes", never three.
#
# A rebase is not a fault and does not need a retry: the branch moved, so the shared parse now
# describes a tree that no longer exists, and the population is recomputed from state every pass.
BRANCH_MOVED_REASON = (
    "branch-moved({entity_id}): a commit from outside this run entered the worktree while it was "
    "pushing this entity, so the corpus parse the rest of the batch would have used describes a "
    "tree that is gone — {deferred} further entity(ies) were not checked; the next run re-derives "
    "them from the new tip")
STOPPED_EARLY_REASON = (
    "stopped-early({why}): this run stopped between entities — {deferred} further entity(ies) in "
    "the population were not checked; the next run will see them")
# One id per line rather than a count: a count says a corpus has typos in it, a name says WHICH
# page to fix, and `job_runs.stats` is the only surface an unattended pass has.
UNUSABLE_ID_REASON = (
    "unusable-entity-id({entity_id}): a page anchors this id and no view can ever be built for "
    "it — {rule}. Nothing else in the run was affected")
# Not a fault and not a deferral either: the other sweeper is converging the same state, so there
# is nothing owed and nothing to retry. Said out loud all the same — an operator who typed
# `--sweep` and got a silent no-op would run it again.
SWEEP_IN_FLIGHT_REASON = (
    "sweep-already-in-flight: another convergence pass holds the sweep lock on this database and "
    "is converging the same state, so this one did nothing. Nothing is owed — read that pass's "
    "`views-sweep` row in `job_runs` for what it did")


@dataclass
class RunResult:
    checked: int = 0
    outcomes: list[RegenOutcome] = field(default_factory=list)
    # How many entities the run was ASKED about. Differs from `checked` only when the per-run
    # ceiling stopped it early, which is exactly when a report that said "checked N" without it
    # would read as "the corpus is converged".
    population: int = 0
    skip_reasons: list[str] = field(default_factory=list)

    @property
    def deferred(self) -> int:
        return max(0, self.population - self.checked)

    @property
    def stats(self) -> dict:
        counts = {"checked": self.checked, "population": self.population,
                 "deferred": self.deferred, "written": 0, "withheld": 0, "removed": 0,
                 "unchanged": 0, "refused": 0, "skip_reasons": self.skip_reasons}
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


# What one changed entity costs: a synthesis call. An `unchanged` entity costs a hash and no model
# call at all, so the ceiling counts WORK DONE, never entities examined — bounding the population
# instead would defer a corpus's worth of free no-ops and leave the tail of the alphabet
# permanently unconverged.
_BILLED_ACTIONS = ("written", "removed")


async def run(repo: str, conn, entity_ids: list[str], *, registry: Registry, branch: str = "main",
              force: bool = False, guarded: bool = True, rows=None,
              max_changes: int | None = None, job: str = JOB_NAME,
              should_stop=None) -> RunResult:
    """The shared base every caller funnels through: one `job_runs` row for the WHOLE batch, one
    commit per entity inside it.

    Stats are updated incrementally, after every entity — updating only at the end would write an
    empty stats row for a fault at entity k of n, lying by omission about k-1 pushed commits — and
    ONCE BEFORE the loop, or a population of zero persists a literally empty `{}`: on the pass an
    operator most needs to read (the first one on a new deployment, or a converged corpus, neither
    of which prints anything) that row is the only way to tell "it ran and found nothing" from "it
    never ran". A `KeyboardInterrupt` gets its own explicit error row below, because `job_run`'s
    `except Exception` cannot see one.

    `max_changes` is the per-run ceiling: N changed entities are N model calls, and an unattended
    caller needs a bound. It counts `_BILLED_ACTIONS`, not entities, and `None` (every operator
    call) is unbounded — a human who typed the command is the bound.

    `should_stop` is the COOPERATIVE pause check, consulted between entities and nowhere else:
    one entity is one commit, so any prefix of this loop is a valid repo state, while inside an
    entity there is a synthesis call and a push that must not be torn in half. It answers with the
    REASON to stop ("" / falsy = keep going) — the worker's own callable says whether a signal
    landed or a capture is waiting, and the recorded deferral repeats its words, so an operator
    reads WHY the pass yielded rather than one sentence that covers two causes. It is what keeps
    a signal (or an arriving capture, issue #102) from waiting out a whole ceiling's worth of
    model calls. `None` (every operator call) never stops — a human who typed the command has
    Ctrl-C.

    `rows` is the single corpus parse, threaded down; see `regenerate_entity` for the two
    conditions it survives this loop under. The second one — no foreign commit mid-batch — is
    enforced HERE, because only a batch has a remainder to protect.
    """
    result = RunResult(population=len(entity_ids))
    try:
        with ops.job_run(conn, job) as stats:
            stats.update(result.stats)
            changed = 0
            for index, entity_id in enumerate(entity_ids):
                remaining = len(entity_ids) - index
                if max_changes is not None and changed >= max_changes:
                    # STOP, and say so — the same posture the repair proposer takes at its own
                    # ceiling. Recorded before the break so the row states what was deferred even
                    # if nothing else is ever read.
                    result.skip_reasons.append(RUN_CEILING_REASON.format(
                        ceiling=max_changes, deferred=remaining))
                    stats.update(result.stats)
                    break
                if should_stop is not None and (why := should_stop()):
                    result.skip_reasons.append(STOPPED_EARLY_REASON.format(why=why,
                                                                           deferred=remaining))
                    stats.update(result.stats)
                    break
                outcome = await regenerate_entity(repo, entity_id, registry=registry, branch=branch,
                                                  force=force, guarded=guarded, rows=rows)
                result.outcomes.append(outcome)
                result.checked = len(result.outcomes)
                if outcome.action in _BILLED_ACTIONS:
                    changed += 1
                if outcome.action == "refused-unusable-id":
                    result.skip_reasons.append(UNUSABLE_ID_REASON.format(
                        entity_id=outcome.entity_id, rule=ENTITY_ID_RULE))
                stats.update(result.stats)
                if outcome.rebased and rows is not None:
                    # A foreign commit is in the tree now, so `rows` describes a repo that no
                    # longer exists and every remaining entity would be summarized off post-rebase
                    # bytes under a pre-rebase audience. Stopping costs one interval and loses
                    # nothing: the population is recomputed from state on the next pass. Scoped to
                    # a SHARED parse, which is the whole hazard — a `rows=None` batch re-parses per
                    # entity, so the entity after the rebase already reads the new tree.
                    result.skip_reasons.append(BRANCH_MOVED_REASON.format(
                        entity_id=outcome.entity_id, deferred=remaining - 1))
                    stats.update(result.stats)
                    break
    except KeyboardInterrupt:
        ops.record_job_run(conn, job, status="error", stats=result.stats, error="KeyboardInterrupt")
        raise
    return result


async def sweep(repo: str, conn, *, registry: Registry, branch: str = "main", force: bool = False,
                guarded: bool = True, max_changes: int | None = None,
                job: str = SWEEP_JOB_NAME, should_stop=None) -> RunResult:
    """The CONVERGENCE pass: `run` over the union population, off one corpus parse, ONE AT A TIME
    across the whole deployment.

    The ONE population that converges `views/`, answered here and never at a call site — the
    worker's idle pass is its only caller now, and a second one would have to come through this
    function rather than build a population of its own. Everything else (the commits, the `job_runs`
    row, the ceiling) is `run`'s, unchanged.

    It is state-based rather than triggered: it asks the corpus what is divergent NOW, so it covers
    every door that ever wrote a page — an ordinary capture, a Slack drop, an applied
    repair, an entity mint, a hand edit — without any of them having to remember to call it.

    **The advisory lock is here rather than at the call sites** because two sweepers is a supported
    shape (N workers, each on its own checkout, plus an operator's `--sweep` at any moment) and a
    broken run: both derive the same divergence, both pay for it, and each one's push rebases the
    other's worktree out from under the batch it is reading. Losing the race is a SKIP with no
    `job_runs` row and no error — the pass that holds the lock is converging exactly the same state
    — reported as a `skip_reason` so neither an operator's terminal nor a worker's log calls it
    silence. Operator runs that are NOT the union population (`--entity`/`--stale`/`--all`) are
    deliberately outside it: they are a human's deliberate act on a named population, and blocking
    one on a maintenance pass would be a worse surprise than a rebase.
    """
    with ops.try_advisory_lock(conn, VIEW_SWEEP_LOCK_KEY) as acquired:
        if not acquired:
            return RunResult(skip_reasons=[SWEEP_IN_FLIGHT_REASON])
        parsed = corpus.load_pages(repo)
        entity_ids = list_sweep_entities(repo, rows=parsed)
        return await run(repo, conn, entity_ids, registry=registry, branch=branch, force=force,
                         guarded=guarded, rows=parsed, max_changes=max_changes, job=job,
                         should_stop=should_stop)
