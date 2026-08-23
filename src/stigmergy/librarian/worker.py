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

The maintenance passes below all run on the IDLE branch and never while a capture is waiting
(`librarian.schedule` owns the due-ness arithmetic). None of them can be torn in half: a pass is
observed as a whole, and `ops.job_run` writes its row on the way OUT, so a pass that gets to
SIGKILL leaves no row at all.
"""
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


def run_garden(conn, deps: processing.Deps) -> dict:
    """One gardener pass — the night shift's whole-corpus half.

    IMPORTED INSIDE THE FUNCTION, not at module scope: the gardener is a peer this worker
    SCHEDULES, not a library the filing path is built on, and every librarian process — including
    `stigmergy-librarian once`, which never gardens — would otherwise carry its import weight and
    its whole transitive edge. `tests/test_architecture.py` pins that edge as a named exception.

    **The pass asks no model.** Every gardener check is deterministic, so this is the one night
    shift pass with no key to preflight and no model spend to bound — it needs a database and a
    readable checkout and nothing else.

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

    result = run_gardener(conn, repo=deps.repo, settings=GardenerSettings.from_args(None))
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

    # ONE pipe for material, ONE lane for removal — and that is the whole dispatch. `kind` chooses
    # the prose (what the brief asks for, the byte cap, the `sources/` folder) and never a code
    # path: a transcript and a note are the same journey, and a removal is not material at all.
    try:
        if item.get("kind") == schema.DELETE:
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
                 garden=run_garden, purge=run_retention, utcnow=None):
        self.conn = conn
        self.deps = deps
        self.on_output = on_output
        self.stopping = False       # do not claim another item after this one
        self.releasing = False      # do not claim another item, and do not sleep waiting either
        # The maintenance passes, injected: when one runs is a timing contract, and a test that
        # had to wait one out could only prove it by sleeping.
        self._garden = garden
        self._purge = purge
        # A WALL clock, deliberately: the daily passes are scheduled off a time somebody wrote as
        # "05:07", and a test that fed a monotonic float to `daily_due` would be testing nothing.
        self._utcnow = utcnow or (lambda: datetime.datetime.now(datetime.UTC))

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
            while not self.releasing and not self.stopping:
                outcome = (process_next(self.conn, self.deps)
                           if not (self.releasing or self.stopping) else None)
                if outcome is not None:
                    processed += 1
                    item, result = outcome
                    self.on_output(f"#{item['id']} -> {result.status}")
                    # After EVERY item, not once after the loop: `StaleBaseError` escapes this
                    # loop, and `ops.job_run` would then persist a `stats` that still said the run
                    # had done nothing — an audit trail lying by omission about pushed work.
                    stats["processed"] = processed
                if self.stopping:
                    break
                if outcome is None:
                    # The queue is empty — where maintenance belongs. Each pass decides for
                    # itself whether it is due: an empty queue polls every few seconds, and a
                    # pass that ran on every one of those ticks would not be maintenance.
                    self.maybe_garden()
                    self.maybe_purge()
                    self._sleep(self.deps.settings.poll_interval_s)
            stats["processed"] = processed
        return processed

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
        night.
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
