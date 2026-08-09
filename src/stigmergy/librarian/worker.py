"""The worker loop: claim one, process it, finish it — and shut down without losing anything.

Built on the queue's own primitives rather than beside them. `claim_next` takes the item with
`FOR UPDATE SKIP LOCKED` and hands back an `attempts` value that IS the lease's fencing token;
`finish` requires that token and updates nothing without it. That fencing closes a real defect,
so it is used, never reimplemented: a stalled worker whose item was redelivered cannot overwrite
the live worker's row.

**One worker active by configuration, and N>1 needs TWO preconditions, not one.**

1. *The lease must outlive the worst-case item.* The claim is atomic and the finish is fenced by
   `attempts`, but the commit and the push happen before `finish` is attempted, so a worker whose
   lease expired mid-item would file a capture a second worker is also filing. `startup_checks`
   refuses to run unless `visibility_timeout_s > MAX_AGENT_ATTEMPTS * timeout_s + GATE_BUDGET_S`,
   and `_file` re-asserts the lease immediately before the push. That precondition is what makes
   the claim true; it is not true by construction, and this docstring used to say it was.
2. *Each worker needs its own `STIGMERGY_LIBRARIAN_WORKTREE_ROOT`* — and this half was missing, which
   made "N>1 safe if ever needed" false on one machine whatever the lease said. `startup_checks`
   calls `gitcmd.reap`, which deletes leftover worktree directories under the root; the default
   root is the SHARED system temp dir. The reap is now scoped by repo and by creating pid
   (`gitcmd.reapable`), which fixes the cross-repo case and the documented `once`-beside-`run`
   pairing on one repo. Two workers on the same repo AND the same root still cannot be told apart
   from a crash's leftovers in every case, so they get distinct roots.

The failure mode of getting (2) wrong is item LOSS with nothing logged on the victim's side — the
reap is `ignore_errors=True` / `check=False` on both halves — surfacing as a `GitError`. It is not
duplicate filing, which is what (1) guards.

**Shutdown: what actually happens, which is less than a cooperative cancel.**

Nothing here can abort a `process_item` that is already running — there is no cancellation point
inside an agent turn, a gitleaks run or a push. `stopping`/`releasing` affect only whether the
NEXT item is claimed. So the messages say that, and no more:

- **SIGINT** — a human at a keyboard. The item in flight runs to completion (and may well be
  filed, with a real commit); the loop then stops instead of claiming another.
- **SIGTERM** — `docker stop`, a supervisor. Same: the item in flight finishes, then the loop
  stops. It does NOT abandon the item, and it cannot promise nothing was committed. The compose
  stop-grace is set above the worst-case per-item budget so this drain can actually complete
  rather than earning a SIGKILL halfway through.

If the process IS killed before the item finishes, the row returns to the queue after the
visibility timeout with `attempts` incremented and the abandoned worktree is reaped at the next
startup — which is the crash guarantee, and is what the messages describe.

There is no cooperative cancellation, which is why no shutdown message may promise "nothing was
committed": the in-flight item finishes, and may well be filed with a real commit directly under
that message.
"""
import logging
import os
import signal
import time

from stigmergy.capture import ops, queue, retention, schema
from stigmergy.capture.errors import QueueStateError
from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import base_inputs, config, gates, gitcmd, processing
from stigmergy.librarian.errors import GitError, LibrarianConfigError, StaleBaseError

log = logging.getLogger(__name__)

JOB_NAME = "librarian"


def human_duration(seconds) -> str:
    """`900s (15 min)` — the machine value AND the unit a person thinks in.

    Both halves, deliberately. The number has to be the configured one so it matches
    `--visibility-timeout` and the queue column; the parenthetical is what makes it actionable
    without arithmetic, which is the whole reason the number was put in the message.
    """
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
    """How a held claim comes back — **with the number in it, and its human unit**.

    Renders `after the 900s (15 min) visibility timeout with attempts incremented`. One fragment,
    two callers (`Worker`'s shutdown messages and `cli`'s `once` interrupt), because the `once`
    message shipped this same sentence WITHOUT the value: "after the visibility timeout" names a
    duration an operator cannot act on — not knowing whether that is ten seconds or fifteen
    minutes, they cannot choose between waiting and reclaiming, so they resubmit instead. Naming
    `900s` alone left the same arithmetic to a person mid-incident, so the unit goes with it.
    """
    return (f"after the {human_duration(visibility_timeout_s)} visibility timeout with attempts "
            f"incremented")


def startup_checks(settings) -> dict:
    """Validate EVERYTHING the worker needs, once, before a single item is claimed.

    Every check here is fail-closed and loud. Per-item validation was the alternative and is
    strictly worse: a malformed `ops/acl.json` would produce N identical `failed` rows and bury
    the real cause under attempts-exhausted noise, and a missing secret scanner would silently
    pass every capture. One loud line at startup is the whole point.

    Returns the resolved objects the run reuses, so nothing is read twice per item. Every one of
    them is read **at `base.sha`** rather than off the working tree (`base_inputs`).

    **The registry and the ACL config read here are a PRE-FLIGHT, not the values the run files
    with** — and that is the correction the steward flow forced. They used to be both, which was
    correct only while nothing could change either mid-run. `stigmergy-entities approve` now pushes a
    new entity between two polls and requeues the capture that was waiting on it, so a registry
    resolved once at startup would judge that capture against a commit that predates its own
    approval: it would park a second time, and the full circle would be broken by a cache rather
    than by anything about the circle.

    **The ACL config kept that flaw one release longer, and the argument for it was wrong.** It read
    "nothing rewrites it mid-run", which is true of the platform and false of a steward pushing a
    tightened `ops/acl.json` to `main`. A worker holding the boot-time config then stamps pages with
    the audience labels of a commit the remote has moved past — under D16 a rule made NARROWER is
    silently ignored, which is the one direction that fails open. So `processing.process_item`
    re-reads BOTH at each item's own `base`, against seams that were already the right shape
    (`base_inputs.load_registry`/`load_acl` are pure functions of a commit). The linter was always
    per item; the three repo-sourced inputs are now symmetric.

    What these two calls still do, and why they stay: a malformed or unreadable registry or ACL
    config refuses the WORKER before a single item is claimed, rather than failing N items one at a
    time with the real cause buried under attempts-exhausted noise. Same fail-closed argument as
    every other check here.
    """
    repo = gitcmd.ensure_repo(settings.repo)
    if settings.backend not in agent_module.BACKENDS:
        raise LibrarianConfigError(
            f"invalid librarian backend {settings.backend!r} "
            f"(use {' or '.join(agent_module.BACKENDS)})")

    # The lease must outlive the item, or the queue redelivers a row this worker is still working
    # on — and since the commit and the push happen before `finish`, both workers file it. The
    # queue's own 300s default was chosen for a human-scale claim (`stigmergy-queue claim`) and was
    # inherited for an agent-scale one; inheriting the number rather than inventing a second one is
    # the right doctrine and the wrong value. Refuse rather than warn: the failure mode is a
    # duplicate page in the company's knowledge, and it only shows up under a race.
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

    # The ref every worktree will branch from, resolved ONCE and reported. Implicit before, and
    # implicit is how a service ends up filing against a commit nobody named.
    #
    # It is resolved BEFORE the linter check now, which used to sit above it. Not a reordering of
    # the checks themselves — the linter is still asked about between the scanner and the skill —
    # but of the fact they depend on: the inputs are read at `base`, so "is the linter there?" is
    # a question about `base`, not about the working tree, and `base` has to exist to ask it.
    # Every refusal below is
    # still a `LibrarianConfigError` and still exits 2.
    base = gitcmd.base_ref(repo, settings.branch)
    log.info("librarian will file into %s against %s", repo, base.describe())

    base_inputs.check_linter_at(repo, base)

    # The agent's operating procedure, checked AT THE REF THE RUN WILL USE. `sdk` only, and not for
    # symmetry's sake — the offline double never reads the skill, so requiring it of a `double` run
    # would be a check that can only ever fail on something nothing was going to use.
    #
    # This used to read `settings.repo` — the local checkout — while `_run` reads it out of the
    # worktree, which is built from `base`. A skill commit that existed locally and not on the
    # remote therefore PASSED the check and failed the run, after burning both agent attempts. A
    # check that can pass while the thing it checks is absent is worse than no check, so it now
    # reads the same bytes the agent will.
    if settings.backend == "sdk":
        # The credential BEFORE the skill: it is the cheapest of the two (no git read at all) and
        # by far the more frequent fault, so an operator with neither is told about that one
        # rather than about a skill they may well have pushed.
        _check_agent_credential()
        _check_skill_at(repo, base)
        # The meeting-distiller brief is deliberately NOT checked here, unlike the ordinary
        # librarian skill above. The skill is needed by every item this worker will ever claim
        # (100% of them); the meeting brief is needed only by `kind="meeting"` rows, which may be
        # zero for a deployment's whole lifetime. Blocking the WHOLE worker at startup over a
        # brief that flow may never need would refuse ordinary captures for an unrelated reason.
        # `agent.read_meeting_brief` (called from `SdkAgent._run_meeting`, on the FIRST meeting
        # item actually claimed) already fails closed with the same `LibrarianConfigError`, which
        # `worker.process_next` turns into a `failed` row naming the config stage — fail-closed at
        # the point of need, not globally. A deployment that wants a global meeting-brief
        # pre-flight can build one against `base_inputs.MEETING_BRIEF_RELPATH` the same way
        # `_check_skill_at` below reads the ordinary skill's.
    _check_push_identity(repo)

    # Both read AT `base`, never off the working tree (`base_inputs` carries the argument),
    # and both re-read per item by `processing.process_item` — these two are the loud early refusal,
    # not the values the run files with.
    acl_config = base_inputs.load_acl(repo, base)
    registry = base_inputs.load_registry(repo, base)
    reaped = gitcmd.reap(repo, settings.worktree_root)
    if reaped:
        log.warning("reaped %d worktree(s) left by a previous run", reaped)
    return {"repo": repo, "acl_config": acl_config, "registry": registry, "reaped": reaped,
            "base": base}


def _check_skill_at(repo: str, base: gitcmd.BaseRef) -> None:
    """The skill must be present, non-empty and under the ceiling AT `base` — not merely on disk."""
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
    agent_module.validate_skill(text, where=where)


def _check_agent_credential(environ: dict | None = None) -> None:
    """The `sdk` backend needs something the Claude Code CLI can authenticate with, checked HERE.

    The credential lives in the gitignored root env file, `make` includes and exports it, and a
    directly-invoked `.venv/bin/stigmergy-librarian` inherits nothing.
    Without this check the run got as far as the agent, the CLI subprocess exited unauthenticated,
    and the item burned both attempts before landing `failed` with a stage name — a missing export
    wearing the costume of a product defect.

    Named as a MISSING CAPABILITY with the fix, and never as a suggestion to switch backends:
    `--backend double` files fixed fabricated pages, so offering it here as a workaround would
    invite an operator to commit the double's output to the company's knowledge repo.

    **Three outcomes, not two** (`agent.credential_status`, which carries the argument). The middle
    one is the correction: with no variable set but the CLI's own config directory present, this run
    is relying on an interactive login that no pre-flight can verify without spending a request, and
    the check that refused it would have blocked `make librarian-walk` on the very machine the walk
    was for — becoming the fifth detour of the four it was written to prevent.

    The advisory is a WARNING and not an INFO on purpose: nothing in this package configures
    logging, so `logging.lastResort` prints WARNING and above to stderr and drops INFO on the floor.
    An advisory the operator cannot see is not an advisory — the same reason the reaped-worktree line
    above is a warning.
    """
    status = agent_module.credential_status(environ)
    if status == agent_module.CREDENTIAL_IN_ENV:
        return
    if status == agent_module.CREDENTIAL_AMBIENT:
        log.warning(
            "no Claude credential in the environment — proceeding on the CLI's own stored login "
            "under %s (on macOS that is the login Keychain, so no credentials file there is "
            "normal). If the agent then fails unauthenticated, this line is the reason, and "
            "exporting one of %s is the fix",
            agent_module.agent_config_dir(environ), ", ".join(agent_module.CREDENTIAL_ENV))
        return
    config_dir = agent_module.agent_config_dir(environ)
    where = (f"{config_dir}, which is not there" if config_dir else
             "the CLI's own config directory — and this run would pass the agent no $HOME at all, "
             "so it could not reach one wherever it is")
    raise LibrarianConfigError(
        f"the sdk backend needs a Claude credential and neither of the TWO ways of having one is "
        f"reachable. (1) Export one of {', '.join(agent_module.CREDENTIAL_ENV)} — it normally lives "
        f"in the gitignored root env file, which `make librarian-walk` includes and exports, and a "
        f"directly-invoked .venv/bin/stigmergy-librarian inherits nothing from it. (2) Or log the CLI "
        f"in interactively — running `claude` once and authenticating counts, and is what most "
        f"machines already have; its stored login lives under {where}. Nothing else reaches the "
        f"agent: the librarian passes only its allow-list through to that subprocess (see "
        f"agent.AGENT_ENV_PASSTHROUGH)")


def _check_push_identity(repo: str) -> None:
    """If this run would push to GitHub, the App has to be the one doing it.

    `githubapp.configured()` returning False is a legitimate configuration — the suite and the
    docker e2e push to a bare local remote that wants no credential at all — so this cannot be an
    unconditional requirement. What it CAN be is conditional on the destination, which is the thing
    that actually distinguishes the two cases: a push to `github.com` with no App configured is a
    push made with whoever's disk credentials the process happens to hold, and that makes
    `git blame` lie about the one thing the git substrate exists to record. Refused rather than
    warned, because the damage is a commit that cannot be un-attributed.

    `configured()` itself raises on HALF a configuration, so a partially set App is caught here for
    every destination, including the bare remote.
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

    `queue.claim_next` already does this on its own hot path, which is the right place for the
    recovery to live — but it does it SILENTLY, and a walk drains with `stigmergy-librarian once`, so
    an operator staring at a row that has been `claimed` for fifty minutes has no way to see
    whether anything is repairing it. Calling it here, by name, with its counts returned, is what
    makes the recovery observable; the second sweep inside `claim_next` is a no-op by then.
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


def build_deps(settings, resolved: dict, evidence) -> processing.Deps:
    return processing.Deps(
        settings=settings, evidence=evidence, agent=agent_module.build_agent(settings),
        registry=resolved["registry"], acl_config=resolved["acl_config"], repo=resolved["repo"])


def process_next(conn, deps: processing.Deps):
    """Claim one item, process it, finish it. Returns `(item, result)` or `None` when the queue
    is empty.

    The `finish` is fenced by the `attempts` value this delivery was handed. A `QueueStateError`
    here means the lease was lost — the item was redelivered to somebody else while we worked —
    and the correct response is to DISCARD this run's work rather than retry: the other worker
    owns it now, and both writing would be the duplicate-filing failure the fence exists to
    prevent. Nothing was committed at that point unless `_file` already pushed, which is why the
    commit is the last thing that happens.
    """
    settings = deps.settings
    item = queue.claim_next(conn, visibility_timeout_s=settings.visibility_timeout_s,
                            max_attempts=settings.max_attempts)
    if item is None:
        return None

    # Dispatch by `kind`, through the SAME fenced claim (`queue.claim_next` above knows nothing
    # about `kind` at all — claiming stays kind-agnostic). Only the PROCESSING seam differs: a
    # `meeting` row files a page SET (`processing.process_meeting_item`) instead of the ordinary
    # one-page flow, and a `drive` row (ADR 028) converts its bytes first and then IS the ordinary
    # flow (`processing.process_drive_item` delegates to `process_item`). No extra process, no
    # second worker — one dispatch, in the one place every row already passes through.
    try:
        if item.get("kind") == schema.MEETING:
            result = processing.process_meeting_item(conn, item, deps)
        elif item.get("kind") == schema.DRIVE:
            result = processing.process_drive_item(conn, item, deps)
        else:
            result = processing.process_item(conn, item, deps)
    except StaleBaseError:
        # The ONE config fault that must not become a `failed` row. It says the deployed worker
        # could not reach the remote, so it applies identically to this item and to every item
        # behind it — finishing them one at a time would drain the queue into `failed` for as long
        # as a credential stays broken, which is the outcome `LibrarianConfigError` exists to
        # prevent. Left `claimed` instead: the lease expires, the next start's `sweep` returns it to
        # `queued`, and `stigmergy-librarian-boot` refuses at startup with the same reason. The
        # material is untouched and nobody is told their capture failed.
        raise
    except LibrarianConfigError as ex:
        # `errors.py` reserves this class for "the WORKER cannot run" and `startup_checks` raises
        # it before anything is claimed — but a config fault can still surface mid-run (the ACL
        # file or the linter changing on disk under a long-lived loop). Naming the stage `config`
        # rather than letting the generic handler call it `unexpected` is the difference between an
        # operator checking `ops/acl.json` and an operator hunting for a bug.
        #
        # **A FIXED sentence, never `str(ex)`.** These messages are written for a local CLI, where
        # naming the file is the whole point, and every one of them carries a filesystem path:
        # `githubapp._private_key` names the App PRIVATE KEY's location, `gates.gate_contract` the
        # linter's, `gitcmd.ensure_repo` the checkout's, `acl_rules.load` the ACL config's. This
        # branch is a WIRE path — `Result.error` and `Result.report` both reach MCP clients through
        # `capture_queue` — so R5 and the spec's "generic over HTTP, specific in the CLI" apply, and
        # the detail goes to the operator's log instead. The submitter learns the one thing that
        # concerns them: it was not their capture.
        log.error("item %s hit a configuration fault mid-run: %s", item["id"], ex)
        result = processing.failure_result(
            item, "config",
            "a librarian configuration fault (the operator's log names the file); nothing about "
            "your capture caused it",
            agent_attempts=_agent_attempts(ex))
    except processing.PROCESSING_ERRORS as ex:
        # **NO `exc_info`.** This is the branch for the KNOWN family — classes this code catches,
        # names a stage for, and reports honestly in the carefully-worded sentence composed directly
        # below it. Thirty lines of Python above that sentence make a handled validation read as a
        # crash and undercut the report entirely — a raw traceback where a person expected a
        # sentence, which is a defect this codebase has shipped more than once.
        # What an operator can act on is the class, the stage and the message, and all three
        # are here — `str(ex)` is safe in a log by construction (`GitError` truncates stderr and
        # never carries the push token; the wire-path rule is `except LibrarianConfigError`'s, above).
        # `exc_info` stays on the branch below, where the fault really is unexpected and the
        # traceback IS the diagnosis.
        log.error("item %s failed at %s: %s", item["id"], ex.__class__.__name__, ex)
        result = processing.failure_result(item, ex.__class__.__name__, str(ex),
                                           agent_attempts=_agent_attempts(ex))
    except Exception as ex:  # noqa: BLE001 — an unexpected fault is still this item's outcome
        log.error("item %s failed unexpectedly", item["id"], exc_info=True)
        result = processing.failure_result(item, "unexpected", ex.__class__.__name__,
                                           agent_attempts=_agent_attempts(ex))

    _finish(conn, item, result)
    return item, result


def _agent_attempts(ex: BaseException) -> int:
    """How many agent passes an exception was carrying, defaulting to none.

    `getattr` rather than an `isinstance` check because `PROCESSING_ERRORS` deliberately includes
    `CaptureError` (an unreadable evidence blob), which is not a `LibrarianError` and cannot carry
    the counter. Zero is the honest answer there: that fault happens before the agent runs, and
    `report.failed_system` omits the agent counter rather than guessing at one.
    """
    return int(getattr(ex, "agent_attempts", 0) or 0)


def _finish(conn, item: dict, result) -> None:
    try:
        queue.finish(conn, item["id"], status=result.status,
                     expected_attempts=item["attempts"], result_ref=result.result_ref,
                     error=result.error, report=result.report,
                     # Set only on a park, and only when the pass produced a
                     # distillation worth re-filing (`processing._with_park_outcome`). `None`
                     # everywhere else, which `finish` reads as "do not touch the column" — and
                     # `finish` clears it unconditionally on the terminal statuses regardless.
                     outcome=result.outcome)
    except QueueStateError as ex:
        # Do not retry: the lease is gone and another worker owns the row (see `process_next`).
        log.error("could not finish item %s: %s", item["id"], ex)
        return
    if result.status == schema.FAILED:
        ops.record_ingest_error(conn, source_doc_id=str(item["id"]), stage="librarian",
                                error=result.report.get("summary", "")[:500],
                                attempts=item["attempts"])
    # A rejection whose REASON is a secret or PII match purges its
    # payload/hints IMMEDIATELY rather than waiting for the ordinary 30-day retention window — the
    # one case where that window is the wrong clock entirely. Checked here, once, right after the
    # row lands `rejected`: both gates that can produce this reason (the pre-agent material scan
    # and `_refuse`'s post-agent veto over the drafted page) route through the same `Result` shape,
    # so this is the one place that has to know about it.
    if result.status == schema.REJECTED:
        reason_code = result.report.get(schema.REASON_CODE_KEY)
        if reason_code in schema.WITHHELD_REASONS:
            retention.purge_secret_capture_immediately(conn, item["id"], reason_code=reason_code)


STDOUT_FD = 1


def emit_from_signal_handler(message: str) -> None:
    """Write an operator notice from inside a signal handler, WITHOUT touching Python's buffered
    stdout — `os.write` to the descriptor, not `print`.

    This exists because `print` from a handler is a latent crash, not a style preference. CPython
    runs a handler on the main thread at a check point it reaches while `BufferedWriter` still
    holds its lock across the raw write; a handler that prints then re-enters that locked writer
    and raises `RuntimeError: reentrant call inside <_io.BufferedWriter>` — raised at the point
    the signal interrupted, which is inside `process_item`. The item in flight FAILS instead of
    draining, which is the precise opposite of what these messages promise. It is narrow (only
    while the main thread is mid-write) and it bit three CI runs on `main` in five days before
    anyone could see the mechanism.

    Every caller's ordinary output goes through `Worker.on_output` and is flushed per line, so
    the buffer is empty between lines and this does not interleave in practice. Errors are
    swallowed on purpose: a closed or redirected descriptor must not turn an orderly shutdown
    into a crash — losing the notice is survivable, failing to stop is not.
    """
    try:
        os.write(STDOUT_FD, (message + "\n").encode(errors="replace"))
    except OSError:
        pass


class Worker:
    """The long-running loop. `once` uses `process_next` directly and never constructs this."""

    def __init__(self, conn, deps: processing.Deps, *, on_output=print):
        self.conn = conn
        self.deps = deps
        self.on_output = on_output
        self.stopping = False       # do not claim another item after this one
        self.releasing = False      # do not claim another item, and do not sleep waiting either

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._on_sigint)
        signal.signal(signal.SIGTERM, self._on_sigterm)

    # Both messages describe what the code DOES, which is less than a cooperative cancel: neither
    # flag can abort a `process_item` already running, so the item in flight always runs to
    # completion and may well be filed with a real commit. The previous wording promised the item
    # "returns to the queue" and that "nothing was committed" — and in a real run the in-flight
    # item finished and was filed, with a commit, printed directly under that promise. What IS
    # true is stated instead, including the crash case, which is the only path where the row does
    # come back.
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
            # `stopping` guards the CLAIM, not just the exit. The loop used to test only
            # `releasing`, so after a first Ctrl-C — which sets `stopping` alone — `_sleep`
            # returned immediately, control went back to the top, and `process_next` claimed,
            # filed, committed and PUSHED one more item before the `break` below was ever
            # reached. The handler prints "finishing the item in flight, then stopping — no
            # further items will be claimed" one instruction earlier, and
            # `docs/reference/librarian.md` states the same contract: the flags affect only
            # whether the NEXT item is claimed. The break after the claim cannot deliver that on
            # its own; only the guard before it can.
            while not self.releasing and not self.stopping:
                outcome = (process_next(self.conn, self.deps)
                           if not (self.releasing or self.stopping) else None)
                if outcome is not None:
                    processed += 1
                    item, result = outcome
                    self.on_output(f"#{item['id']} -> {result.status}")
                    # Written after EVERY item, not once after the loop. `process_next`
                    # deliberately re-raises `StaleBaseError` rather than turning it into a
                    # `failed` row, so it escapes this loop — and `ops.job_run`'s own
                    # `except Exception` then persists whatever `stats` holds, which was `{}`.
                    # A worker that filed, committed and PUSHED five captures before its
                    # installation token expired recorded a run that looked like it had done
                    # nothing. `views/regenerate` states the rule this now follows: updating only
                    # at the end writes "a `job_runs` audit trail lying by omission about real,
                    # already-pushed work".
                    stats["processed"] = processed
                if self.stopping:
                    break
                if outcome is None:
                    self._sleep(self.deps.settings.poll_interval_s)
            stats["processed"] = processed
        return processed

    def _sleep(self, seconds: float) -> None:
        """Poll in slices so a signal is observed promptly rather than after a whole interval."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.stopping:
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
