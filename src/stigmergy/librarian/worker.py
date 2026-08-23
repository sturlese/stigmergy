"""The worker loop: claim one, process it, finish it — and shut down without losing anything.

`claim_next` hands back an `attempts` value that IS the lease's fencing token and `finish`
requires it, so a stalled worker whose item was redelivered cannot overwrite the live worker's
row. Two preconditions for N>1 workers: the lease must outlive the worst-case item (see
`startup_checks`), and each worker needs its own `STIGMERGY_LIBRARIAN_WORKTREE_ROOT`, because
`gitcmd.reap` deletes leftover worktree directories under a root that defaults to the shared
system temp dir — getting that wrong loses items silently.

Shutdown is not a cooperative cancel: nothing can abort a `process_item` already running, so
`stopping`/`releasing` affect only whether the NEXT item is claimed and the item in flight runs
to completion (and may well be filed, with a real commit). Only a kill returns the row to the
queue, after the visibility timeout, with its worktree reaped at the next startup.

The periodic view sweep below is cooperative BETWEEN ENTITIES and uncancellable within one. That
line is where the two properties meet: one entity is one commit, so any prefix of a pass is a
valid repo state and stopping between them costs one interval, while inside an entity there is a
synthesis call and a push that tearing in half would gain nothing. So a signal is observed after
at most one entity instead of after a whole ceiling's worth of model calls — which matters because
the deployed kill window is finite and `ops.job_run` writes its row on the way OUT, so a pass that
gets to SIGKILL leaves no row at all.
"""
import asyncio
import datetime
import logging
import os
import signal
import time

from stigmergy.capture import ops, queue, retention, schema
from stigmergy.capture.errors import QueueStateError
from stigmergy.gardener import schema as gardener_schema
from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import base_inputs, config, gates, gitcmd, processing, schedule
from stigmergy.librarian.errors import GitError, LibrarianConfigError, StaleBaseError
from stigmergy.views import regenerate as views_regenerate

log = logging.getLogger(__name__)

JOB_NAME = "librarian"
# The gardener's own `job_runs` name, imported rather than repeated: `maybe_garden` asks when it
# last ran, and a second spelling of it would make the pass run every idle tick forever.
GARDEN_JOB_NAME = gardener_schema.JOB_NAME
# How much of a failed garden pass's reason reaches its `job_runs` row. Generous, because the one
# refusal an operator most needs to read whole is the model-key one: it names the variable, the
# strip that makes an export useless, and the way to turn the pass off.
GARDEN_ERROR_CHARS = 2000


def human_duration(seconds) -> str:
    """`900s (15 min)` — the configured value, plus the unit a person thinks in."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, remainder = divmod(total, 60)
    if minutes < 60:
        pretty = f"{minutes} min" if not remainder else f"{minutes} min {remainder}s"
    else:
        hours, minutes = divmod(minutes, 60)
        pretty = f"{hours}h" if not minutes else f"{hours}h {minutes} min"
    return f"{total}s ({pretty})"


def visibility_timeout_clause(visibility_timeout_s) -> str:
    """How a held claim comes back, with the duration in it. One fragment, two callers
    (`Worker`'s shutdown messages and `cli`'s `once` interrupt), so they cannot word it
    differently."""
    return (f"after the {human_duration(visibility_timeout_s)} visibility timeout with attempts "
            f"incremented")


def startup_checks(settings) -> dict:
    """Validate EVERYTHING the worker needs, once, before a single item is claimed.

    Every check here is fail-closed and loud: per-item validation would turn a malformed
    `ops/entity-registry.json` or a missing secret scanner into N identical `failed` rows with the real cause
    buried under attempts-exhausted noise.

    Returns the resolved objects the run reuses, all read **at `base.sha`** rather than off the
    working tree (`base_inputs`). The registry and the ACL config returned here are a PRE-FLIGHT
    only — `processing.process_item` re-reads both at each item's own `base`, so an entity born
    or a tightened ACL pushed between two polls takes effect without a restart.
    """
    repo = gitcmd.ensure_repo(settings.repo)
    agent_module.ensure_known_backend(settings.backend)
    if settings.backend == agent_module.PYDANTIC_BACKEND:
        _check_pydantic_backend(settings)
        # `max_turns` maps to `UsageLimits(request_limit=...)`, and a tool-using pass needs at
        # least two requests — one to call a tool, one to write its account — so a lower ceiling
        # fails every ordinary capture at full cost. Refused by name rather than clamped.
        if int(settings.max_turns) < 2:
            raise LibrarianConfigError(
                f"max_turns is {settings.max_turns}, but the ordinary filing run needs at least 2 "
                f"model requests per pass — one to call a tool, one to write its account — so a "
                f"ceiling below that fails every capture at full cost. Raise "
                f"$STIGMERGY_LIBRARIAN_MAX_TURNS to 2 or more (the default is "
                f"{config.DEFAULT_MAX_TURNS}).")

    # The lease must outlive the item, or the queue redelivers a row this worker is still working
    # on — and since the commit and the push happen before `finish`, both workers file it.
    # Refused rather than warned: the failure mode is a duplicate page, visible only under a race.
    required = config.minimum_visibility_timeout_s(timeout_s=settings.timeout_s)
    if int(settings.visibility_timeout_s) <= required:
        raise LibrarianConfigError(
            f"visibility_timeout_s is {settings.visibility_timeout_s}s, which is not longer than "
            f"one item's worst case ({config.MAX_AGENT_ATTEMPTS} agent attempts x "
            f"{settings.timeout_s}s + {config.GATE_BUDGET_S}s for the gates, the commit and the "
            f"push = "
            f"{required}s). A shorter lease lets the queue redeliver an item this worker is still "
            f"processing, and both workers would file it. Raise --visibility-timeout above "
            f"{required}, or lower $STIGMERGY_LIBRARIAN_TIMEOUT_S")

    gates.ensure_scanner(settings.gitleaks_bin)

    # The ref every worktree will branch from, resolved ONCE and reported. It must be resolved
    # before the checks below: they ask about `base`, not about the working tree.
    base = gitcmd.base_ref(repo, settings.branch)
    log.info("librarian will file into %s against %s", repo, base.describe())

    base_inputs.check_linter_at(repo, base)

    # The agent's operating procedure, checked at the ref the run will use — the same bytes the
    # backend reads out of the worktree, not the local checkout.
    if settings.backend in agent_module.SKILL_READING_BACKENDS:
        # The meeting-distiller brief is deliberately NOT checked here: every claimed item needs
        # the skill, while only `kind="meeting"` rows need the brief, and
        # `agent.read_meeting_brief` already fails closed at the point of need.
        _check_skill_at(repo, base)
    _check_push_identity(repo)

    # Read AT `base`, and re-read per item by `processing.process_item`.
    registry = base_inputs.load_registry(repo, base)
    reaped = gitcmd.reap(repo, settings.worktree_root)
    if reaped:
        log.warning("reaped %d worktree(s) left by a previous run", reaped)
    return {"repo": repo, "registry": registry, "reaped": reaped, "base": base}


def _check_skill_at(repo: str, base: gitcmd.BaseRef) -> str:
    """The skill must be present, non-empty and under the ceiling AT `base` — not merely on disk.

    Returns the validated text so a caller needing the bytes reads one validated copy rather than
    fetching the blob again; `startup_checks` calls it for the refusal alone."""
    relpath = agent_module.SKILL_RELPATH
    where = base_inputs.where(base, relpath)
    size = gitcmd.blob_size(repo, base.sha, relpath)
    if size < 0:
        raise LibrarianConfigError(
            f"the librarian skill is not in the commit the worktrees branch from ({where}) — it is "
            f"the agent's operating procedure and it will not file without it. "
            + base_inputs.push_or_commit_hint(base))
    agent_module.check_skill_size(size, where)
    try:
        text = gitcmd.show(repo, base.sha, relpath)
    except GitError as ex:
        raise LibrarianConfigError(
            f"the librarian skill at {where} could not be read ({ex})") from ex
    return agent_module.validate_skill(text, where=where)


def check_garden_model(settings, *, environ: dict | None = None) -> None:
    """The gardener's own model key, checked before the pass runs — because the night shift moved
    INTO this process and brought a second model with it.

    This is not the librarian's check repeated for tidiness. It closes a trap the move created:
    `gardener.settings.DEFAULT_GARDENER_MODEL` is a BARE model id, which `kernel.llm` resolves
    through the OpenAI Responses API — and `OPENAI_API_KEY` is exactly what
    `bootstrap.READ_PATH_ONLY_ENV` strips before exec'ing this worker. So the default that is
    correct on a laptop authenticates with nothing on a deployment.

    **Deliberately NOT a startup refusal**, though it was written as one first. A worker whose
    gardener model is unconfigured must still file captures: refusing to boot over a nightly
    maintenance pass would take the queue down for the one thing that never depends on it, which
    is this package's own rule everywhere else in the night shift. So it fails the PASS, and
    `maybe_garden` records the refusal as a `job_runs` error row — which is what makes it loud
    rather than a log line nobody reads, and what stops it re-firing on every idle tick.

    What that would look like WITHOUT this check is the reason it is a refusal at all: the
    deterministic checks need no model and would keep working, so every night would produce a
    `partial` run with real findings and a quiet `sweep_error` — a pass that looks alive, forever,
    while two of its three model passes have never once run. Fail closed and loud, at startup, by
    name, is this package's posture for every other secret; the night shift does not get an
    exemption for being maintenance.
    """
    from stigmergy.gardener.settings import MODEL_ENV, GardenerSettings
    from stigmergy.librarian import bootstrap, pydantic_backend

    if str(settings.garden_at or "").strip().lower() == config.DAILY_OFF:
        return
    model = GardenerSettings.from_args(None).model
    source = os.environ if environ is None else environ
    provider = pydantic_backend.provider_of(model)
    key_env = pydantic_backend.PROVIDER_KEY_ENV.get(provider) if provider else "OPENAI_API_KEY"
    if key_env is None or source.get(key_env):
        return
    stripped = key_env in bootstrap.READ_PATH_ONLY_ENV
    raise LibrarianConfigError(
        f"the nightly garden pass would run {model!r}, which authenticates with ${key_env}, and "
        f"${key_env} is not set in this worker's environment."
        + (f" **On the DEPLOYED worker that is a dead end rather than a missing export**: "
           f"`stigmergy-librarian-boot` strips {key_env} on purpose, because it belongs to the "
           f"READ path's embedder and Fly secrets are app-wide. Nothing you export in that "
           f"container survives the strip. Set ${MODEL_ENV} to a provider-prefixed model whose "
           f"key this worker holds — the filing model's own provider is the obvious one "
           f"({settings.model!r})."
           if stripped else
           f" Export {key_env}, or set ${MODEL_ENV} to a model whose provider key this worker "
           f"holds.")
        + f" Or set ${config.GARDEN_AT_ENV}={config.DAILY_OFF} to run no garden pass at all — a "
          f"deliberate 'not here' is fine; a pass that reports findings every night while its "
          f"model half has never run is not.")


def _check_pydantic_backend(settings, *, environ: dict | None = None) -> None:
    """Everything the `pydantic` backend needs — a provider-prefixed model id, a configured price
    and the provider's own key — refused before the first claim.

    `environ` is injectable so the key preflight stays a pure function of a mapping and a model
    string, assertable without mutating the process environment.
    """
    from stigmergy.librarian import pricing, pydantic_backend

    provider = pydantic_backend.provider_of(settings.model)
    if not provider:
        raise LibrarianConfigError(
            f"$STIGMERGY_LIBRARIAN_MODEL is {settings.model!r}, which names no provider, and the "
            f"{agent_module.PYDANTIC_BACKEND!r} backend resolves a model string through pydantic-ai "
            f"— where a BARE name means the OpenAI Responses API, so this run would file this "
            f"brain's captures through a provider nobody chose. Give it a provider prefix "
            f"(<provider>:<model>); priced today: {', '.join(pricing.priced_models())}. A bare id "
            f"was the spelling of the retired backend, so a deployment that has just changed "
            f"STIGMERGY_LIBRARIAN_BACKEND and nothing else lands exactly here: "
            f"'anthropic:{settings.model}' is the same model, spelled for this one")

    # An unpriced model would report $0.00 for work that costs money — refused here rather than
    # discovered in a column of zeros.
    pricing.require_priced(settings.model)

    key_env = pydantic_backend.PROVIDER_KEY_ENV.get(provider)
    if key_env is None:
        # Not a refusal: pydantic-ai supports providers this table has not heard of. WARNING and
        # not INFO because nothing here configures logging, so INFO is dropped on the floor.
        log.warning("no API-key preflight exists for the provider prefix %r — proceeding. If the "
                    "run then fails unauthenticated, this line is the reason, and the fix is "
                    "whatever key that provider reads", f"{provider}:")
        return
    if not (os.environ if environ is None else environ).get(key_env):
        # The DEPLOYED worker STRIPS some of these by design, so "export it" is the one fix that
        # cannot work there and the refusal has to say which case this is.
        from stigmergy.librarian import bootstrap

        # Filtered to models the deployed worker can actually authenticate as: a refusal whose own
        # example fails the refusal that produced it is worse than one with no example.
        deployable = [m for m in pricing.priced_models()
                      if pydantic_backend.PROVIDER_KEY_ENV.get(pydantic_backend.provider_of(m))
                      not in bootstrap.READ_PATH_ONLY_ENV]
        dead_end = (
            f" **On the DEPLOYED worker this is a dead end rather than a missing export**: "
            f"`stigmergy-librarian-boot` strips {key_env} from the worker's environment before "
            f"exec'ing the loop, on purpose — it belongs to the READ path's embedder and Fly "
            f"secrets are app-wide, so stripping it here is the only place the write path can be "
            f"kept independent of it. Nothing you export in that container survives that strip. "
            f"Pick a filing model whose provider key is not read-path-only — priced and deployable "
            f"today: {', '.join(deployable)}. This configuration works on a laptop and cannot work "
            f"there."
            if key_env in bootstrap.READ_PATH_ONLY_ENV else "")
        raise LibrarianConfigError(
            f"$STIGMERGY_LIBRARIAN_MODEL is {settings.model!r} and ${key_env} is not set — the "
            f"{agent_module.PYDANTIC_BACKEND!r} backend authenticates with the provider's own key "
            f"and has nothing else to try. Export {key_env}: it normally lives in the gitignored "
            f"root env file, which `make` includes and exports, and a directly-invoked script "
            f"inherits nothing from it." + dead_end)


def _check_push_identity(repo: str) -> None:
    """If this run would push to GitHub, the App has to be the one doing it.

    Conditional on the destination: an unconfigured App is legitimate against a bare local remote,
    but a push to `github.com` with none configured uses whatever disk credentials the process
    holds and makes `git blame` name a human for a page the librarian wrote. `configured()` itself
    raises on HALF a configuration, so that is caught for every destination.
    """
    from stigmergy.librarian import githubapp

    configured = githubapp.configured()
    url = gitcmd.origin_url(repo)
    if "github.com" not in url or configured:
        return
    raise LibrarianConfigError(
        f"{repo} pushes to github.com and the librarian GitHub App is not configured, so the "
        f"commit would be authored with this machine's own git credentials — `git blame` would "
        f"name a human for a page the librarian wrote. Set {githubapp.APP_ID_ENV}, "
        f"{githubapp.INSTALLATION_ID_ENV} and {githubapp.PRIVATE_KEY_ENV} (or "
        f"{githubapp.PRIVATE_KEY_FILE_ENV}) — the setup procedure is in "
        f"docs/reference/operator-runbook.md")


def sweep(conn, settings) -> dict:
    """Return timed-out claims to the queue before anything is claimed.

    `queue.claim_next` does this silently on its own hot path; calling it here by name, with its
    counts returned, is what makes the recovery observable to an operator.
    """
    return queue.release_expired(conn, visibility_timeout_s=settings.visibility_timeout_s,
                                 max_attempts=settings.max_attempts)


def swept_clause(result: dict, settings) -> str:
    """One line about a sweep, or `""` when it moved nothing. Shared by `run` and `once` so the two
    cannot describe the same recovery differently."""
    released, failed = result.get("released", 0), result.get("failed", 0)
    if not released and not failed:
        return ""
    return (f"swept {released} stranded claim(s) back to the queue and failed {failed} that had "
            f"burned every delivery (claims held longer than "
            f"{human_duration(settings.visibility_timeout_s)})")


# ── the periodic view sweep ───────────────────────────────────────────────────────────────────
# A view is DERIVED, so it can go stale whenever anything writes a page — an ordinary capture, a
# Slack drop, an applied repair, an identity born with a capture, a hand edit in the repo. The
# fix is deliberately NOT a call at each of those doors: two of them run inside the HTTP server
# process, and any new door would have to remember. It is a state-based convergence pass that asks
# the corpus what is divergent NOW, which covers every writer that will ever exist.
#
# It lives HERE, in the worker, rather than in a cron: this process already holds the GitHub App
# credential the commit needs, and a scheduled Actions run that pushed would put that credential in
# a runner's environment. The same argument moved the repair pass here, and then the rest of the
# night shift after it — there is no scheduled job outside this deployment any more.


# The two refusals below both fail CLOSED before a single view is touched, and both exist because
# this pass is the one writer in the system with no operator in front of it: every other caller of
# `views.regenerate` is either a human at a terminal or a capture somebody submitted, and can
# afford to learn about a bad input from an error message.
_ABSENT_REGISTRY_REFUSAL = (
    "refusing to converge views/: the entity registry is not readable at {where}. It resolves to "
    "an EMPTY registry, and an empty registry makes every existing view an orphan — this pass's "
    "answer to an orphan is to DELETE it, so a fetch that raced a force-push, a corrupt object or "
    "a missing file would remove every view in the repo, {ceiling} per pass, for as long as the "
    "worker runs. The ceiling bounds the pass, not the incident. A registry that is present and "
    "declares no entities is a different thing — a committed, reviewable statement — and is "
    "honoured.")
_STALE_BASE_REFUSAL = (
    "refusing to converge views/: the base resolved to the local {base} instead of "
    "origin/{branch}, so the fetch failed and this deployed worker's checkout is whatever it was "
    "cloned at. A pass off a stale base re-derives every view from an OLD member set and then "
    "`gitcmd.push` rebases that state onto the current tip — replaying an older, potentially "
    "WIDER acl over the current one. `processing._resolve_filing_base` refuses a filing for the "
    "same fault; this is the same rule, and it binds harder here, because a capture writes new "
    "content while this writes DERIVED state over content somebody else already fixed.")


def run_view_sweep(conn, deps: processing.Deps, *, should_stop=None) -> views_regenerate.RunResult:
    """One convergence pass over `views/`, on this worker's own fresh worktree.

    **The worktree is materialized here, and that is what keeps `guarded=False` honest.** The
    post-meeting hook BORROWS the capture's worktree, so it inherits the "always a fresh, detached
    checkout" justification `views.regenerate.regenerate_entity` states for skipping the
    dirty-tree/wrong-branch guards. An idle pass has no capture to borrow from, so it builds the
    same thing itself off a freshly-fetched `origin/<branch>` — the justification stays literally
    true for this caller instead of quietly becoming a claim nobody checks.

    The registry is read at THIS pass's base, not at startup: a de-registration pushed since the
    worker booted is exactly the input that turns an orphaned view into a removal, and a pre-flight
    copy would miss it until the next restart. That is also why the base and the registry are both
    CHECKED here rather than trusted — see the two refusals above, and note that each is returned
    as a `skip_reason` on a `RunResult` rather than raised: a raise reaches
    `Worker.maybe_sweep_views`, which logs and swallows, leaving the one unattended loop in the
    system to refuse silently every interval.

    `should_stop` is threaded down to `views.regenerate.run`, which consults it BETWEEN entities
    and stops on the reason it answers with.
    """
    settings = deps.settings
    base = gitcmd.base_ref(deps.repo, settings.branch)
    if settings.require_remote_base and not base.remote:
        return _refused_sweep(conn, _STALE_BASE_REFUSAL.format(base=base.describe(),
                                                               branch=settings.branch),
                              error=StaleBaseError)
    if not base_inputs.registry_present_at(deps.repo, base):
        return _refused_sweep(conn, _ABSENT_REGISTRY_REFUSAL.format(
            where=base_inputs.where(base, config.REGISTRY_RELPATH),
            ceiling=settings.view_sweep_ceiling), error=LibrarianConfigError)
    with gitcmd.ephemeral_worktree(deps.repo, base.sha, settings.worktree_root) as worktree:
        registry = base_inputs.load_registry(deps.repo, base)
        return asyncio.run(views_regenerate.sweep(
            worktree, conn, registry=registry, branch=settings.branch, guarded=False,
            max_changes=settings.view_sweep_ceiling, should_stop=should_stop))


def run_repairs(conn, deps: processing.Deps, *, should_stop=None):
    """One repair pass: the latest gardener findings, derived into repairs and applied.

    Returns `repair.run.RepairRunResult`, or `None` when the pass had nothing to run against —
    which is the ordinary state, not a fault: a deployment whose gardener has not completed a run
    since the last pass has no findings to answer, and saying so every idle tick would bury the
    passes that did something.

    IMPORTED HERE, not at module scope, and that is load-bearing: `repair.run` loads a model stack
    and pulls `pydantic_ai` in with it. The worker's filing path must not pay for that at import
    time, and `tests/test_architecture.py` pins the edge as a function-level exception.

    `should_stop` is threaded down and consulted BETWEEN repairs — never inside one, because a
    repair is a model call, the gates and a push, and abandoning it half-way is what leaves the
    corpus in a state nobody chose.
    """
    from stigmergy.gardener import store as gardener_store
    from stigmergy.repair import run as repair_run
    from stigmergy.repair.settings import RepairSettings

    latest = gardener_store.latest_completed_run(conn)
    if latest is None:
        return None
    # The watermark: this pass answers a gardener run, and a pass that ran since that run finished
    # has already answered it. Without this the loop would re-derive the same findings every
    # interval — cheap only because the ledger's memory catches each one afterwards, which is
    # paying for a model call to be told what a timestamp already knew.
    last = ops.latest_run(conn, repair_run.JOB_NAME)
    if last is not None and last.get("finished_at") and latest.get("finished_at") \
            and last["finished_at"] >= latest["finished_at"]:
        return None
    return asyncio.run(repair_run.run_repairs(
        conn, settings=RepairSettings.from_env(), repo=deps.repo,
        branch=deps.settings.branch, worktree_root=deps.settings.worktree_root,
        should_stop=should_stop))


def repair_clause(result) -> str:
    """One line about a repair pass, or `""` when it did nothing — `view_sweep_clause`'s shape, for
    the same reason: a maintenance pass that printed "nothing to do" every interval would bury the
    passes that changed the corpus."""
    if result is None:
        return ""
    stats = result.stats
    if not (stats["applied"] or stats["failed"] or stats["skipped_invalid"]):
        return ""
    line = (f"repairs: {stats['findings_seen']} finding(s) seen — {stats['applied']} applied, "
            f"{stats['failed']} failed, {stats['skipped_known']} already answered")
    if stats["failures"]:
        line += " — " + "; ".join(stats["failures"][:3])
    return line


def run_garden(conn, deps: processing.Deps) -> dict:
    """One gardener pass — the night shift's whole-corpus half.

    IMPORTED INSIDE THE FUNCTION, not at module scope: `gardener.run` builds a model stack at
    import time, and every librarian process would pay for it at startup, including the ones that
    never garden. `tests/test_architecture.py` pins that edge as a named exception with a fresh
    interpreter, exactly as the views reach in the other direction is pinned.

    **The index rebuild is deliberately not a second half of this pass, and cannot be**: the
    deployed worker's environment has no embedding key at all — `bootstrap.READ_PATH_ONLY_ENV`
    strips it before exec, so the write path cannot reach the read path's credential — so nothing
    in this process could repair a `pages_index` that has drifted from the corpus. Nor does this
    pass LINT the index: `index.check` reads `pages_index`, and a scheduled write-path read of the
    served index would widen an invariant for a log line, when the admin console's
    Index page already lints the live index, in-process and on demand, for the operator who is
    actually looking.

    Returns the pass's stats. The gardener writes its own `job_runs` row inside `run_gardener`, so
    this function adds none: two rows for one pass would make "when did the garden last run"
    ambiguous, which is the question `schedule.daily_due` asks.
    """
    from stigmergy.gardener.run import run_gardener
    from stigmergy.gardener.settings import GardenerSettings

    check_garden_model(deps.settings)
    result = asyncio.run(run_gardener(conn, repo=deps.repo, settings=GardenerSettings.from_args(None)))
    return {"findings": len(result.findings), "pages_checked": result.pages_checked,
            "entities_checked": result.entities_checked}


def garden_clause(stats: dict | None) -> str:
    """One line about a garden pass. Unlike the two convergence passes, this one ALWAYS prints when
    it runs: it is daily, so a line a day is not noise, and "the garden ran and found nothing" is
    the sentence an operator most wants to be able to see."""
    if not stats:
        return ""
    return (f"garden: {stats['pages_checked']} page(s) and {stats['entities_checked']} "
            f"entity(ies) checked — {stats['findings']} finding(s)")


def run_retention(conn, deps: processing.Deps) -> dict:
    """The retention purge: the payload and hints of terminal rows past their window, plus any
    secret/PII refusal whatever its age.

    It writes its own `job_runs` row (`capture.retention.purge`), so this adds none — and it is the
    one pass that touches no git at all, which is why it is safe to run last: nothing it does can
    leave a tree half-written.
    """
    from stigmergy.capture import retention

    return retention.purge(conn, older_than_days=deps.settings.retention_days)


def retention_clause(result: dict | None) -> str:
    """One line, and only when something was purged: a nightly "purged 0" would bury the nights
    that removed somebody's material."""
    if not result or not result.get("purged"):
        return ""
    return f"retention: purged the payload of {result['purged']} terminal capture(s)"


def _refused_sweep(conn, reason: str, *, error: type[BaseException]) -> views_regenerate.RunResult:
    """A pass that did not run, in the shape a pass that did returns — so `view_sweep_clause`
    prints it and nothing downstream needs a second kind of answer.

    It also writes its own `job_runs` error row, named for the exception the FILING path raises for
    the same fault, because a maintenance pass nobody is watching that refuses only into a log has
    no operator-facing surface at all: `job_runs` is where the pass is read from.
    """
    log.error("%s", reason)
    result = views_regenerate.RunResult(skip_reasons=[reason])
    ops.record_job_run(conn, views_regenerate.SWEEP_JOB_NAME, status="error",
                       stats=result.stats, error=error.__name__)
    return result


def view_sweep_clause(result: views_regenerate.RunResult) -> str:
    """One line about a view sweep, or `""` when it converged with nothing to do — `swept_clause`'s
    shape, for the same reason: a maintenance pass that printed "nothing changed" every interval
    would bury the passes that did change something."""
    stats = result.stats
    if not (stats["written"] or stats["removed"] or stats["refused"] or result.skip_reasons):
        return ""
    line = (f"view sweep: {stats['checked']} of {stats['population']} entity(ies) checked — "
            f"{stats['written']} regenerated, {stats['removed']} removed, "
            f"{stats['unchanged']} already current")
    if stats["refused"]:
        line += f", {stats['refused']} refused"
    if result.skip_reasons:
        line += " — " + "; ".join(result.skip_reasons)
    return line


def build_deps(settings, resolved: dict, evidence) -> processing.Deps:
    return processing.Deps(
        settings=settings, evidence=evidence, agent=agent_module.build_agent(settings),
        registry=resolved["registry"], repo=resolved["repo"])


def process_next(conn, deps: processing.Deps):
    """Claim one item, process it, finish it. Returns `(item, result)` or `None` when the queue
    is empty.

    The `finish` is fenced by the `attempts` value this delivery was handed. A `QueueStateError`
    means the lease was lost to a redelivery, and this run's work is DISCARDED rather than
    retried — both workers writing is the duplicate filing the fence exists to prevent.
    """
    settings = deps.settings
    item = queue.claim_next(conn, visibility_timeout_s=settings.visibility_timeout_s,
                            max_attempts=settings.max_attempts)
    if item is None:
        return None

    # Dispatch by `kind`, through the SAME fenced claim — claiming stays kind-agnostic and only
    # the processing seam differs.
    try:
        if item.get("kind") == schema.MEETING:
            result = processing.process_meeting_item(conn, item, deps)
        elif item.get("kind") == schema.DELETE:
            result = processing.process_delete_item(conn, item, deps)
        else:
            result = processing.process_item(conn, item, deps)
    except StaleBaseError:
        # The ONE config fault that must not become a `failed` row: the deployed worker could not
        # reach the remote, which applies to every item behind this one too, so finishing them
        # would drain the whole queue into `failed`. Left `claimed` for the next start's `sweep`.
        raise
    except LibrarianConfigError as ex:
        # A config fault can still surface mid-run (the ACL file or the linter changing under a
        # long-lived loop); naming the stage `config` points the operator at a file.
        #
        # **A FIXED sentence, never `str(ex)`.** These messages each name a filesystem path and
        # this branch is a WIRE path — `Result.error`/`Result.report` reach MCP clients — so the
        # detail goes to the operator's log and the submitter only learns it was not their capture.
        log.error("item %s hit a configuration fault mid-run: %s", item["id"], ex)
        result = processing.failure_result(
            item, "config",
            "a librarian configuration fault (the operator's log names the file); nothing about "
            "your capture caused it",
            agent_attempts=_agent_attempts(ex), cost_usd=_agent_cost_usd(ex))
    except processing.PROCESSING_ERRORS as ex:
        # **NO `exc_info`.** This is the KNOWN family: a traceback above the reported sentence
        # makes a handled validation read as a crash. `str(ex)` is safe in a log by construction
        # (`GitError` truncates stderr and never carries the push token). The branch below, where
        # the fault really is unexpected, keeps `exc_info` because the traceback IS the diagnosis.
        log.error("item %s failed at %s: %s", item["id"], ex.__class__.__name__, ex)
        result = processing.failure_result(item, ex.__class__.__name__, str(ex),
                                           agent_attempts=_agent_attempts(ex),
                                           cost_usd=_agent_cost_usd(ex))
    except Exception as ex:  # noqa: BLE001 — an unexpected fault is still this item's outcome
        log.error("item %s failed unexpectedly", item["id"], exc_info=True)
        result = processing.failure_result(item, "unexpected", ex.__class__.__name__,
                                           agent_attempts=_agent_attempts(ex),
                                           cost_usd=_agent_cost_usd(ex))

    _finish(conn, item, result)
    return item, result


def _agent_attempts(ex: BaseException) -> int:
    """How many agent passes an exception was carrying, defaulting to none.

    `getattr` and not `isinstance`: `PROCESSING_ERRORS` includes `CaptureError`, which is no
    `LibrarianError` and cannot carry the counter — and zero is honest, that fault pre-dates the
    agent.
    """
    return int(getattr(ex, "agent_attempts", 0) or 0)


def _agent_cost_usd(ex: BaseException) -> float:
    """`_agent_attempts`' twin for the spend the passes had banked when the fault was raised —
    same `getattr` posture, for the same reason."""
    return float(getattr(ex, "agent_cost_usd", 0.0) or 0.0)


def _finish(conn, item: dict, result) -> None:
    try:
        queue.finish(conn, item["id"], status=result.status,
                     expected_attempts=item["attempts"], result_ref=result.result_ref,
                     error=result.error, report=result.report)
    except QueueStateError as ex:
        # Do not retry: the lease is gone and another worker owns the row (see `process_next`).
        log.error("could not finish item %s: %s", item["id"], ex)
        return
    if result.status == schema.FAILED:
        ops.record_ingest_error(conn, source_doc_id=str(item["id"]), stage="librarian",
                                error=result.report.get("summary", "")[:500],
                                attempts=item["attempts"])
    # A rejection whose reason is a secret or PII match purges its payload/hints IMMEDIATELY
    # rather than waiting for the 30-day retention window. Here because both gates that can
    # produce the reason route through this one `Result` shape.
    if result.status == schema.REJECTED:
        reason_code = result.report.get(schema.REASON_CODE_KEY)
        if reason_code in schema.WITHHELD_REASONS:
            retention.purge_secret_capture_immediately(conn, item["id"], reason_code=reason_code)


STDOUT_FD = 1


def emit_from_signal_handler(message: str) -> None:
    """Write an operator notice from inside a signal handler, WITHOUT touching Python's buffered
    stdout — `os.write` to the descriptor, never `print`.

    A handler that prints can re-enter a `BufferedWriter` still holding its lock and raise
    `RuntimeError: reentrant call` at the interrupted point inside `process_item`, failing the
    item in flight instead of draining it. Errors are swallowed on purpose: losing the notice is
    survivable, turning an orderly shutdown into a crash is not.
    """
    try:
        os.write(STDOUT_FD, (message + "\n").encode(errors="replace"))
    except OSError:
        pass


class Worker:
    """The long-running loop. `once` uses `process_next` directly and never constructs this."""

    def __init__(self, conn, deps: processing.Deps, *, on_output=print,
                 view_sweep=run_view_sweep, repair_pass=run_repairs, now=time.monotonic,
                 garden=run_garden, purge=run_retention, utcnow=None):
        self.conn = conn
        self.deps = deps
        self.on_output = on_output
        self.stopping = False       # do not claim another item after this one
        self.releasing = False      # do not claim another item, and do not sleep waiting either
        # The maintenance pass and the clock that schedules it, both injected: the interval is a
        # timing contract, and a test that had to wait one out could only prove it by sleeping.
        self._view_sweep = view_sweep
        self._repair_pass = repair_pass
        self._garden = garden
        self._purge = purge
        self._now = now
        # A SECOND clock, and deliberately not `now`: the two convergence passes are scheduled off
        # a monotonic counter (an interval must not move when the host's wall clock is corrected),
        # while the daily passes are scheduled off a WALL time somebody wrote as "05:07". One
        # injected clock could not serve both, and a test that fed a monotonic float to
        # `daily_due` would be testing nothing.
        self._utcnow = utcnow or (lambda: datetime.datetime.now(datetime.UTC))
        # `None` means "due at the first idle tick" — a worker that has just started converges
        # `views/` before it waits an interval, the same posture `sweep()` above already takes.
        self._view_sweep_due_at: float | None = None
        self._repair_due_at: float | None = None
        # "Something this worker did changed the corpus since the last view sweep." Set by the
        # loop after a filing and by the repair pass after an applied repair; read and CLEARED by
        # the sweep it causes, so one piece of work makes the sweep due once rather than forever.
        self._corpus_changed = False

    def _sweep_pause_reason(self) -> str:
        """Why the view sweep should yield between entities, or `""` — read at the moment it is
        asked, never captured. Two causes, each in its own words because the recorded deferral
        repeats them: a shutdown signal landed, or a capture is WAITING in the queue — a pass that
        held the worker's only loop through a whole ceiling of syntheses would put up to ten model
        calls and pushes between a capture and its filing, the exact latency the queue exists to
        avoid (issue #102). Stopping costs one interval and loses nothing: the population is
        recomputed from state on the next idle tick."""
        if self.stopping or self.releasing:
            return "the process is shutting down"
        if queue.work_waiting(self.conn):
            return "a capture is waiting in the queue"
        return ""

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._on_sigint)
        signal.signal(signal.SIGTERM, self._on_sigterm)

    # Neither flag can abort a `process_item` already running, so no shutdown message may promise
    # that the item returns to the queue or that nothing was committed: only a kill does that.
    def _requeue_clause(self) -> str:
        return ("if this process is killed before it finishes, that item returns to the queue "
                + visibility_timeout_clause(self.deps.settings.visibility_timeout_s))

    def _on_sigint(self, signum, frame) -> None:
        if self.stopping:
            self.releasing = True
            emit_from_signal_handler(
                f"\nstopping as soon as the item in flight finishes — it cannot be "
                f"cancelled mid-item, so it will reach a terminal state (possibly "
                f"filed, with a commit). {self._requeue_clause()}.")
            return
        self.stopping = True
        emit_from_signal_handler(
            "\nfinishing the item in flight, then stopping — no further items will be "
            "claimed. Press Ctrl-C again for the same thing without waiting to poll.")

    def _on_sigterm(self, signum, frame) -> None:
        # No "press again" hint: nobody is there to press it.
        self.stopping = True
        self.releasing = True
        emit_from_signal_handler(
            f"received SIGTERM — finishing the item in flight, then stopping. It "
            f"cannot be cancelled mid-item, so it will reach a terminal state "
            f"(possibly filed, with a commit); {self._requeue_clause()}.")

    def run(self) -> int:
        """Drain the queue until a signal says stop. Returns how many items were processed."""
        processed = 0
        swept = swept_clause(sweep(self.conn, self.deps.settings), self.deps.settings)
        if swept:
            self.on_output(swept)
        with ops.job_run(self.conn, JOB_NAME) as stats:
            # `stopping` guards the CLAIM, not just the exit: the contract is that the flags stop
            # the NEXT claim, and a break placed after the claim cannot deliver that — the loop
            # would file, commit and push one more item first.
            worked = False
            while not self.releasing and not self.stopping:
                outcome = (process_next(self.conn, self.deps)
                           if not (self.releasing or self.stopping) else None)
                if outcome is not None:
                    processed += 1
                    worked = True
                    item, result = outcome
                    self.on_output(f"#{item['id']} -> {result.status}")
                    # After EVERY item, not once after the loop: `StaleBaseError` escapes this
                    # loop, and `ops.job_run` would then persist a `stats` that still said the run
                    # had done nothing — an audit trail lying by omission about pushed work.
                    stats["processed"] = processed
                if self.stopping:
                    break
                if outcome is None:
                    # The queue is empty — where maintenance belongs. On its OWN interval, not
                    # every idle tick: an empty queue polls every few seconds and a corpus parse
                    # per tick is not free.
                    #
                    # EXCEPT on the tick that follows work. A capture, a repair or a removal has
                    # just changed the corpus, and `views/` is derived from it — waiting out a
                    # whole interval would leave a view describing a page that is no longer there.
                    # "Something just changed the corpus" is the moment the rollups
                    # are most likely to be wrong and the cheapest time to fix them.
                    #
                    # The repair pass runs FIRST for exactly that reason: it is one of the things
                    # that changes the corpus, and a sweep asked before it would converge the tree
                    # as it was a moment ago. `_corpus_changed` is what carries the fact across —
                    # set by a filing here and by an applied repair inside the pass, read and
                    # cleared by the sweep.
                    self._corpus_changed = self._corpus_changed or worked
                    worked = False
                    self.maybe_run_repairs()
                    self.maybe_sweep_views(due_now=self._corpus_changed)
                    # The daily passes go LAST, and the garden last but one on purpose: it writes
                    # findings the repair pass answers, and putting it after that pass gives them
                    # a whole repair interval to be seen rather than a whole day.
                    self.maybe_garden()
                    self.maybe_purge()
                    self._sleep(self.deps.settings.poll_interval_s)
            stats["processed"] = processed
        return processed

    def maybe_sweep_views(self, *, due_now: bool = False) -> bool:
        """Run the convergence pass if its interval has elapsed — or if `due_now`, which is the
        first idle tick after this worker actually did something. Returns whether it ran.

        SKIPPED, never blocked: the idle branch returns immediately when the pass is not due, so
        `_sleep` keeps polling in slices and a signal is still observed promptly.

        A fault is recorded and swallowed — `views.regenerate.run` has already written its own
        `job_runs` error row by the time one reaches here — because filing must never depend on a
        rollup. That is the post-meeting hook's posture, for the same reason.

        The pass is handed `self._sweep_pause_reason` rather than a copy of any flag: it is
        consulted between entities, and by then a signal may have landed or a capture may have
        arrived. Not starting one is still the first guard — a shutdown must not pick up a fresh
        multi-entity pass on its way out.
        """
        interval = float(self.deps.settings.view_sweep_interval_s)
        if interval <= config.VIEW_SWEEP_OFF or self.stopping or self.releasing:
            return False
        now = self._now()
        if not due_now and self._view_sweep_due_at is not None and now < self._view_sweep_due_at:
            return False
        self._corpus_changed = False
        # Scheduled BEFORE the pass runs, off the moment it STARTED: a fault would otherwise
        # re-attempt on every idle tick, and a pass slower than its own interval would owe another
        # the instant it finished.
        self._view_sweep_due_at = now + interval
        try:
            result = self._view_sweep(self.conn, self.deps, should_stop=self._sweep_pause_reason)
        except Exception:  # noqa: BLE001 — best-effort maintenance; see the docstring
            log.error("the periodic view sweep failed; the queue keeps draining", exc_info=True)
            return True
        clause = view_sweep_clause(result)
        if clause:
            self.on_output(clause)
        return True

    def maybe_run_repairs(self) -> bool:
        """Run the repair pass if its interval has elapsed. Returns whether it ran.

        `maybe_sweep_views`' shape, and every sentence of that docstring applies here: skipped
        rather than blocked, due-time scheduled BEFORE the pass so a fault cannot re-attempt every
        tick, and a fault logged and swallowed because filing must never depend on maintenance.

        What differs is what a fault costs. A view sweep that dies has regenerated nothing; a
        repair pass that dies may have PUSHED — each repair is committed and pushed on its own —
        so the ledger, not this method's return value, is where an operator reads what happened.
        That is why every repair records itself before the next one is derived.
        """
        interval = float(self.deps.settings.repair_interval_s)
        if interval <= config.REPAIR_PASS_OFF or self.stopping or self.releasing:
            return False
        now = self._now()
        if self._repair_due_at is not None and now < self._repair_due_at:
            return False
        self._repair_due_at = now + interval
        try:
            result = self._repair_pass(self.conn, self.deps,
                                       should_stop=self._sweep_pause_reason)
        except Exception:  # noqa: BLE001 — best-effort maintenance; see the docstring
            # A pass that died may still have PUSHED before it died — each repair commits on its
            # own — so the corpus is marked changed either way. A sweep that had nothing to do
            # costs a corpus parse; a view left describing a page a repair removed costs a wrong
            # answer.
            self._corpus_changed = True
            log.error("the periodic repair pass failed; the queue keeps draining", exc_info=True)
            return True
        if result is not None and result.stats.get("applied"):
            self._corpus_changed = True
        clause = repair_clause(result)
        if clause:
            self.on_output(clause)
        return True

    def maybe_garden(self) -> bool:
        """Run the daily gardener pass if today's time has come and it has not run today.

        The two convergence passes above ask "has the interval elapsed"; this one asks
        `schedule.daily_due`, which answers from the last `job_runs` row rather than from an
        in-process timer. That difference is the whole point: a worker restarts (a deploy, a crash,
        a scale event) far more often than once a day, and an in-process timer would garden again
        every time one did. Reading the ledger makes the pass idempotent across restarts, which is
        the one property a cron had for free and this had to be given.

        A fault is logged and swallowed, as with every maintenance pass — and the `job_runs` row
        the gardener wrote before failing is what stops the next idle tick from retrying it all
        night. `_corpus_changed` is NOT set: the gardener writes findings, never pages.
        """
        at = self._daily_at(self.deps.settings.garden_at, schedule.DEFAULT_GARDEN_AT)
        if at is None or not self._daily_due(GARDEN_JOB_NAME, at):
            return False
        try:
            stats = self._garden(self.conn, self.deps)
        except Exception as ex:  # noqa: BLE001 — best-effort maintenance; see the docstring
            # A row, not just a log line, and for two reasons: `_daily_due` reads the ledger, so
            # without one this retries on every idle tick until morning; and the console's Jobs
            # page reads the same rows, so this is what turns "the garden is quietly not running"
            # into something an operator can see.
            ops.record_job_run(self.conn, GARDEN_JOB_NAME, status="error",
                               error=str(ex)[:GARDEN_ERROR_CHARS])
            log.error("the nightly garden pass failed; the queue keeps draining", exc_info=True)
            return True
        clause = garden_clause(stats)
        if clause:
            self.on_output(clause)
        return True

    def maybe_purge(self) -> bool:
        """Run the daily retention purge if today's time has come and it has not run today.

        `maybe_garden`'s shape. What differs is that this pass is the one piece of the night shift
        with a PROMISE behind it — payload is kept for a window and then removed — so a deployment
        that never runs it is not merely un-converged, it is not keeping its word. That is why the
        pass is on by default and why its failure is logged at error level even though nothing
        downstream depends on it.
        """
        at = self._daily_at(self.deps.settings.retention_at, schedule.DEFAULT_RETENTION_AT)
        if at is None or not self._daily_due(retention.JOB_NAME, at):
            return False
        try:
            result = self._purge(self.conn, self.deps)
        except Exception:  # noqa: BLE001 — best-effort maintenance; see the docstring
            log.error("the nightly retention purge failed; the queue keeps draining", exc_info=True)
            return True
        clause = retention_clause(result)
        if clause:
            self.on_output(clause)
        return True

    def _daily_at(self, value: str, default: str) -> tuple[int, int] | None:
        """The configured time for a daily pass, or None when it is switched off or the worker is
        on its way out. One method for both passes, so "off" cannot come to mean two things."""
        if self.stopping or self.releasing:
            return None
        if str(value or "").strip().lower() == config.DAILY_OFF:
            return None
        return schedule.parse_daily(value, default=default)

    def _daily_due(self, job: str, at: tuple[int, int]) -> bool:
        """Whether `job` is due now — the ledger read, isolated here so a database that is briefly
        unreadable postpones a maintenance pass instead of taking the worker down with it."""
        try:
            return schedule.daily_due(self._utcnow(), schedule.last_run_at(self.conn, job), at)
        except Exception:  # noqa: BLE001 — see the docstring
            log.warning("could not read the last run of %s; postponing it", job, exc_info=True)
            return False

    def _sleep(self, seconds: float) -> None:
        """Poll in slices so a signal is observed promptly rather than after a whole interval."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.stopping:
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
