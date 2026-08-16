"""The console's use-cases — every route handler is a thin skin over one method here.

Everything operational lands on a seam another package owns; the ONLY SQL this module owns is
read-side plumbing no library exposes: `job_runs`/`ingest_errors` reads, `audit_log` aggregates,
the `pages_index` zone AGGREGATE (a declared reader exception — counts only, never content), and
`admin_actions` via `admin.schema`.

Untrusted text passes `text.sanitize` on the way out — control characters die at the server,
HTML-escaping is the client's non-negotiable half. Errors: `AdminBadRequest` / `AdminNotFound` /
`AdminRefused` cross with the library's own sentence; anything else becomes the routes' 500,
class name only.
"""
import logging
import os

from stigmergy import text as textutil
from stigmergy.admin import schema as admin_schema
from stigmergy.admin.github import ActionsError
from stigmergy.admin.settings import AdminSettings
from stigmergy.capture import dispositions, latency, queue, retention
from stigmergy.capture import schema as capture_schema
from stigmergy.capture.errors import CaptureError
from stigmergy.digest import run as digest_run
from stigmergy.digest.settings import DIGEST_CHANNEL_ID_ENV, SLACK_BOT_TOKEN_ENV, DigestSettings
from stigmergy.entities import situations
from stigmergy.entities.errors import EntityError
from stigmergy.entities.generator import ENTITY_TYPES, canonical_id_for
from stigmergy.gardener import store as gardener_store
from stigmergy.gardener.schema import JOB_NAME as GARDENER_JOB
from stigmergy.index import check as index_check
from stigmergy.index import store as index_store
from stigmergy.index.errors import StigmergyIndexError
from stigmergy.librarian import config as librarian_config
from stigmergy.review_kinds import KIND_ENTITY_PROPOSAL
from stigmergy.server import pilot_report
from stigmergy.server import review as server_review
from stigmergy.server.errors import StartupError
from stigmergy.server.webhook import JOB_NAME as WEBHOOK_JOB

log = logging.getLogger(__name__)

PURGE_JOB, PURGE_DRY_RUN_JOB = "capture-purge", "capture-purge-dry-run"
DIGEST_JOB, DIGEST_DRY_RUN_JOB = "digest", "digest-dry-run"

# The crons tab's table: file, title, schedule, and WHERE the database truth for "did it run"
# lives — `job_runs` for the two that write one, `index_meta.built_at` for the rebuild, which
# writes none. `schedule_utc` is pinned against the parsed workflow YAML by test.
CRON_WORKFLOWS = (
    {"file": "index-rebuild.yml", "title": "Index rebuild", "schedule_utc": "17 4 * * *",
     "truth": "index_meta.built_at", "dispatch_inputs": ()},
    {"file": "retention-purge.yml", "title": "Retention purge", "schedule_utc": "42 4 * * *",
     "truth": f"job_runs:{PURGE_JOB}", "dispatch_inputs": ("dry_run",)},
    {"file": "gardener.yml", "title": "Gardener", "schedule_utc": "7 5 * * *",
     "truth": f"job_runs:{GARDENER_JOB}", "dispatch_inputs": ()},
)
DISPATCHABLE = tuple(w["file"] for w in CRON_WORKFLOWS)

# The worker's OWN attempts budget, never the queue CLI's shorter flagless default — comparing a
# long agent item against the CLI's lease calls it dead while its worker is still on it. The one
# declared reach into `stigmergy.librarian` is the config module alone.
WORKER_MAX_ATTEMPTS = queue.DEFAULT_MAX_ATTEMPTS


def worker_visibility_timeout_s() -> int:
    """The lease the deployed worker actually holds, resolved fresh PER CALL — it derives from
    `$STIGMERGY_LIBRARIAN_TIMEOUT_S`, and `fly.toml`'s `[env]` is app-wide, so this process's env
    is the worker's. The meter, the verdicts and Reclaim's default horizon all read THIS, so the
    number the operator sees and the number the button acts on are one number.

    A refused env value falls back to the class default rather than failing the request: `meta()`
    is the console's boot call, and raising would answer a config typo with a login screen. The
    fallback is honest because the same refusal stops the WORKER from booting — there is no live
    lease to misreport."""
    try:
        return librarian_config.resolved_visibility_timeout_s()
    except librarian_config.LibrarianConfigError as ex:   # attribute, never a second import edge
        log.error("admin: the worker lease could not be resolved (%s) — the console falls back to "
                  "the class default %ss; a worker in this environment refuses to boot",
                  ex, librarian_config.DEFAULT_VISIBILITY_TIMEOUT_S)
        return librarian_config.DEFAULT_VISIBILITY_TIMEOUT_S


class AdminBadRequest(Exception):
    """The caller's input is wrong (unknown status, unlisted workflow, malformed body)."""


class AdminNotFound(Exception):
    """The named row does not exist."""


class AdminRefused(Exception):
    """A domain seam refused — the message is the library's own operator-facing sentence."""


def _clean(value) -> str:
    """Control characters stripped, newlines KEPT — the web renders multi-line questions whole;
    HTML escaping is the client's job, not flattening."""
    return textutil.sanitize(str(value or ""))


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


class AdminService:
    """One instance per process, sharing the process-wide autocommit connection. Concurrency
    invariant: no cursor is ever held across an `await` — the two async digest methods await
    inside `digest.run`, between statements, never mid-cursor."""

    def __init__(self, conn, *, server_settings, admin_settings: AdminSettings, gateway=None):
        self._conn = conn
        self._server = server_settings
        self._admin = admin_settings
        self._gateway = gateway

    # ── meta / overview ───────────────────────────────────────────────────────────────────────
    def meta(self) -> dict:
        return {
            "actor_default": self._admin.actor,
            "github": {"configured": self._gateway is not None, "repo": self._admin.github_repo},
            "digest": self._digest_pieces(),
            "workflows": [dict(w) for w in CRON_WORKFLOWS],
            "worker": {"visibility_timeout_s": worker_visibility_timeout_s(),
                       "max_attempts": WORKER_MAX_ATTEMPTS},
            # Shipped from here so the frontend never hardcodes a second copy of
            # `generator.ENTITY_TYPES` that could drift.
            "entity_types": list(ENTITY_TYPES),
        }

    def overview(self) -> dict:
        counts = queue.counts_by_status(self._conn)
        latest = {w["file"]: self._latest_job_run(self._truth_jobs(w)) for w in CRON_WORKFLOWS
                  if w["truth"].startswith("job_runs:")}
        gardener = gardener_store.latest_completed_run(self._conn)
        severity_counts: dict[str, int] = {}
        if gardener is not None:
            for f in gardener_store.findings_for_run(self._conn, gardener["id"]):
                severity_counts[f["severity"]] = severity_counts.get(f["severity"], 0) + 1
        return {
            "queue": {"counts": counts,
                      "parked": counts.get(capture_schema.NEEDS_INPUT, 0)
                      + counts.get(capture_schema.TRIAGE, 0)},
            "in_flight": self._in_flight(),
            "ingest_errors": self._ingest_errors(limit=5),
            "crons": {"latest_runs": latest, "index_built_at": self._built_at()},
            "gardener": {"run": self._run_row(gardener), "severity_counts": severity_counts},
            "digest": {"last_window_until": self._digest_watermark()},
            "admin_actions": admin_schema.recent_actions(self._conn, limit=5),
        }

    # ── queue: the steward drain ──────────────────────────────────────────────────────────────
    def queue_list(self, *, statuses: list[str] | None = None, submitter: str | None = None,
                   limit: int = queue.DEFAULT_LIST_LIMIT) -> dict:
        try:
            rows = queue.list_all_submissions(
                self._conn, statuses=statuses or None, submitter=submitter, limit=limit) \
                if submitter else queue.query_submissions(
                    self._conn, statuses=statuses or None, limit=limit)
        except ValueError as ex:   # unknown status — the library's own sentence names the vocabulary
            raise AdminBadRequest(str(ex)) from ex
        return {"counts": queue.counts_by_status(self._conn),
                "submissions": [self._listed(row) for row in rows]}

    def queue_show(self, submission_id: int) -> dict:
        trace = queue.get_submission_trace(self._conn, submission_id)
        if trace is None:
            raise AdminNotFound(f"no submission {submission_id}")
        return self._traced(trace)

    def queue_requeue(self, submission_id: int, *, actor: str, note: str = "") -> dict:
        return self._mutate("queue.requeue", actor, {"id": submission_id},
                            lambda by: dispositions.requeue(self._conn, submission_id,
                                                            actor=by, note=note))

    def queue_resolve(self, submission_id: int, *, actor: str, note: str, page: str = "",
                      commit: str = "") -> dict:
        result = self._mutate("queue.resolve", actor,
                              {"id": submission_id, "page": page, "commit": commit},
                              lambda by: dispositions.resolve(self._conn, submission_id, actor=by,
                                                              note=note, page=page, commit=commit))
        # `resolve` means the material WAS used — a resolve with no pointer leaves the
        # submitter's report permanently silent about where it went.
        warning = ("" if (page or commit) else
                   f"resolved #{submission_id} with no page and no commit — the submitter's report "
                   f"will say only what your note said, with no pointer to where the material went")
        return {**result, "warning": warning}

    def queue_reject(self, submission_id: int, *, actor: str, reason: str) -> dict:
        # A rejected entity situation is a GOVERNANCE decision, so it also writes
        # `review_decisions` — the same ledger MCP and Slack write — or "who decided this
        # identity" would answer from different tables per verdict. The Entities tab routes its
        # Reject here rather than growing a second button.
        situation = situations.get_situation(self._conn, submission_id)
        is_entity_proposal = bool(situation and situations.classify(situation))
        result = self._mutate("queue.reject", actor, {"id": submission_id},
                              lambda by: dispositions.reject(self._conn, submission_id,
                                                             actor=by, reason=reason))
        if is_entity_proposal:
            server_review.record_decision(
                self._conn, item_kind=KIND_ENTITY_PROPOSAL, item_id=str(submission_id),
                verdict="reject", actor=actor or self._admin.actor, notes=reason)
        return result

    def queue_reclaim(self, *, actor: str, visibility_timeout_s: int | None = None,
                      max_attempts: int = WORKER_MAX_ATTEMPTS) -> dict:
        # The WORKER's lease, never the queue CLI's shorter one: an unqualified Reclaim means
        # "recover whatever the worker abandoned", and the meter beside this button renders the
        # same number. The clamp wraps BOTH branches — the default carries operator-controlled
        # env data, and a negative horizon inverts `release_expired`'s predicate, reading EVERY
        # claimed row as expired.
        timeout = max(0, int(worker_visibility_timeout_s() if visibility_timeout_s is None
                             else visibility_timeout_s))
        return self._mutate("queue.reclaim", actor, {"visibility_timeout_s": timeout},
                            lambda by: queue.release_expired(
                                self._conn, visibility_timeout_s=timeout,
                                max_attempts=max_attempts))

    def queue_purge(self, *, actor: str, older_than_days: int = retention.DEFAULT_RETENTION_DAYS,
                    dry_run: bool = False) -> dict:
        days = max(0, int(older_than_days))
        if dry_run:   # a preview changes nothing and is not a mutation — no admin_actions row
            return retention.purge(self._conn, older_than_days=days, dry_run=True)
        return self._mutate("queue.purge", actor, {"older_than_days": days},
                            lambda by: retention.purge(self._conn, older_than_days=days,
                                                       dry_run=False))

    # ── gardener ──────────────────────────────────────────────────────────────────────────────
    def gardener_state(self) -> dict:
        run = gardener_store.latest_completed_run(self._conn)
        findings = ([self._finding(f) for f in
                     gardener_store.findings_for_run(self._conn, run["id"])]
                    if run is not None else [])
        return {"run": self._run_row(run), "findings": findings,
                "history": self._job_runs((GARDENER_JOB,), limit=10)}

    # ── digest ────────────────────────────────────────────────────────────────────────────────
    def digest_state(self) -> dict:
        return {"pieces": self._digest_pieces(),
                "last_window_until": self._digest_watermark(),
                "history": self._job_runs((DIGEST_JOB, DIGEST_DRY_RUN_JOB), limit=10)}

    async def digest_preview(self) -> dict:
        result = await digest_run.run_digest(
            self._conn, settings=self._digest_settings(), channels_path=self._admin.channels_path,
            gateway=None, dry_run=True, since_override=None)
        return {"body": result.body, "since": _iso(result.since), "until": _iso(result.until)}

    async def digest_post(self, *, actor: str) -> dict:
        pieces = self._digest_pieces()
        for key, sentence in (
                ("digest_channel_id", f"${DIGEST_CHANNEL_ID_ENV} is not set — the digest has no "
                                      f"channel to post to"),
                ("bot_token", f"${SLACK_BOT_TOKEN_ENV} is not set — the digest cannot post "
                              f"without the bot token")):
            if not pieces[key]:
                raise AdminRefused(sentence)
        # The Slack SDK is loaded only at the moment a real post is about to happen.
        from stigmergy.slack.bolt_gateway import build_gateway

        gateway = build_gateway(os.environ[SLACK_BOT_TOKEN_ENV])
        settings = self._digest_settings()

        async def _post(_by: str):
            return await digest_run.run_digest(
                self._conn, settings=settings, channels_path=self._admin.channels_path,
                gateway=gateway, dry_run=False, since_override=None)

        result = await self._mutate_async("digest.post", actor, {}, _post)
        response = {"posted": result.posted, "run_id": result.run_id,
                    "since": _iso(result.since), "until": _iso(result.until)}
        if result.posted and result.run_id is None:
            # The digest CLI's own warning, the console edition: the post landed but the watermark
            # write failed, so the next run may re-post this window as a duplicate.
            response["warning"] = ("posted, but the watermark could not be recorded — the next "
                                   "run may re-post this window as a duplicate; check job_runs "
                                   "for a 'digest' row before running again")
        return response

    # ── index ─────────────────────────────────────────────────────────────────────────────────
    def index_state(self) -> dict:
        return {"meta": index_store.read_meta(self._conn), "zones": self._zone_counts(),
                "webhook": self._job_runs((WEBHOOK_JOB,), limit=10)}

    def index_substrate_check(self) -> dict:
        registry = self._server.entity_registry_path or None
        try:
            findings = index_check.run_checks(self._conn, registry_path=registry)
        except StigmergyIndexError as ex:
            raise AdminRefused(str(ex)) from ex
        return {"findings": findings,
                "errors": sum(1 for f in findings if f["severity"] == "error"),
                "warnings": sum(1 for f in findings if f["severity"] == "warn")}

    # ── entity situations (read, and a real Approve — ADR 030) ────────────────────────────────
    def entities_list(self, *, limit: int = situations.DEFAULT_LIST_LIMIT) -> list[dict]:
        return [self._situation(row) for row in
                situations.list_pending_situations(self._conn, limit=limit)]

    def entities_show(self, submission_id: int) -> dict:
        row = situations.get_situation(self._conn, submission_id)
        if row is None:
            raise AdminNotFound(f"submission {submission_id} does not exist")
        return self._situation(row)

    def entity_approve(self, situation_id: int, *, actor: str, name: str, entity_type: str,
                       entity_id: str = "", aliases: str = "", role: str = "",
                       requeue: bool = True) -> dict:
        """Mint the entity this situation names, through the same server-driven door the review
        lane's `review_decide` walks — literally the same function, `server.review.
        mint_and_record_approval` (mint -> `review_decisions` row -> requeue strictly after the
        push), which reaches `entities.remote.mint_via_clone` -> `entities.mint.mint`
        (ADR 030 D3/D4). The CLI reaches `entities.mint.mint` too, but from the steward's OWN
        clone — `remote.py` is the throwaway-clone half only, and the shared seam is `mint.mint`,
        one function below both. The shared sequence is entered directly rather than through
        `review_decide` itself: the console mints under the admin token with `actor` as
        ATTRIBUTION, the same trust model as every other console mutation and as the CLI it
        replaces (D2). `review_decide`'s steward check and self-approval refusal are for a
        RESOLVED identity (a bearer token, a Slack profile) and are deliberately not reached from
        here — enforcing them against a free-text `actor` field would fake a second-human rule
        this one shared credential cannot actually back. The asymmetry is stated, not hidden: this
        is the same lane the console's Queue tab already writes through (`_mutate`), not a weaker
        copy of MCP/Slack's.

        Records TWO ledgers, like the other two doors: `admin_actions` (via `_mutate`, this
        package's own bookkeeping, actor-attributed) and `review_decisions` (the append-only
        governance record, written inside the shared sequence rather than re-implemented, so the
        ledger never drifts between hand-written INSERTs on three different doors). This door
        passes no `notes` — the form has no note field — so the row's note is `''`.

        `name`/`entity_type` are validated — and the situation itself confirmed still pending —
        before anything is attempted. `entity_id` defaults to `name`'s slug
        (`generator.canonical_id_for`, the same default `review_decide` and the CLI's own `--id`
        reuse) and is deliberately not its own form field: "one less field to mistype" (ADR 030
        D5), the same call `slack.render`'s entity-mint modal already makes.
        """
        clean_name = " ".join(str(name or "").split())
        clean_type = str(entity_type or "").strip().lower()
        missing = [field for field, value in (("name", clean_name), ("entity_type", clean_type))
                  if not value]
        if missing:
            raise AdminBadRequest(
                f"approving an entity proposal mints it (ADR 030) — missing "
                f"{' and '.join(missing)}: pass name (the entity's page title) and entity_type "
                f"(one of {', '.join(ENTITY_TYPES)})")
        if clean_type not in ENTITY_TYPES:
            raise AdminBadRequest(
                f"entity_type {clean_type!r} is not one of {', '.join(ENTITY_TYPES)}")
        resolved_id = str(entity_id or "").strip() or canonical_id_for(clean_name)
        alias_list = [a.strip() for a in str(aliases or "").split(",") if a.strip()]
        args = {"id": situation_id, "entity_id": resolved_id, "name": clean_name,
               "entity_type": clean_type, "requeue": bool(requeue)}

        def _do(by: str) -> dict:
            # The guard stays HERE rather than inside the shared sequence: this door runs it after
            # its own name/type validation and the review lane runs it before, so one shared
            # answer would change what a caller wrong in both ways at once is told.
            situations.require_situation(self._conn, situation_id, action="approve")
            # Nothing is caught around this call: the library's own exception class must reach
            # `_mutate`, which records it in `admin_actions` before the `except` below renames it
            # for the caller.
            return server_review.mint_and_record_approval(
                self._conn, repo_url=self._server.librarian_repo_url,
                submission_id=situation_id, entity_id=resolved_id, name=clean_name,
                entity_type=clean_type, aliases=alias_list, role=role or "", actor=by,
                requeue=requeue)

        try:
            return self._mutate("entities.approve", actor, args, _do)
        except EntityError as ex:
            # `entities.errors.EntityError` (a collision, drift, a missing template, a secret in
            # the role/aliases text, a lost push race, no App credential/repo URL configured, or
            # `require_situation`'s own three refusals) — this package's own domain refusal, the
            # library's sentence carried verbatim, same posture as every other `AdminRefused`.
            # Caught HERE, outside `_mutate`, rather than inside `_do`: `_mutate` already recorded
            # `admin_actions` with the ORIGINAL exception's class name (its own `except Exception`
            # branch) before re-raising, so converting a second time would only rename what the
            # bookkeeping row already captured precisely.
            raise AdminRefused(str(ex)) from ex

    # ── activity ──────────────────────────────────────────────────────────────────────────────
    def activity(self) -> dict:
        return {
            "report": pilot_report.build_report(self._conn),
            "by_identity_tool": self._audit_aggregate(),
            "ask_questions": self._ask_questions(),
            "rate_limited": self._rate_limited(),
            "admin_actions": admin_schema.recent_actions(self._conn, limit=50),
        }

    # ── worker ────────────────────────────────────────────────────────────────────────────────
    def worker_status(self) -> dict:
        measured = latency.summarize(queue.filed_latencies_ms(self._conn))
        return {"counts": queue.counts_by_status(self._conn), "in_flight": self._in_flight(),
                "latency": measured.as_json(),
                "visibility_timeout_s": worker_visibility_timeout_s(),
                "max_attempts": WORKER_MAX_ATTEMPTS}

    # ── crons ─────────────────────────────────────────────────────────────────────────────────
    def crons_state(self) -> dict:
        rows = []
        for w in CRON_WORKFLOWS:
            rows.append({**w,
                         "latest_run": (self._latest_job_run(self._truth_jobs(w))
                                        if w["truth"].startswith("job_runs:") else None),
                         "index_built_at": (self._built_at()
                                            if w["truth"] == "index_meta.built_at" else None)})
        state = {"configured": self._gateway is not None, "workflows": rows}
        if self._gateway is None:
            return state
        try:
            by_path = {w["path"].rsplit("/", 1)[-1]: w for w in self._gateway.workflows()}
            for row in rows:
                remote = by_path.get(row["file"])
                row["state"] = remote["state"] if remote else "unknown"
                row["runs"] = self._gateway.runs(row["file"], limit=5)
        except ActionsError as ex:
            state["github_error"] = str(ex)
        return state

    def cron_dispatch(self, workflow_file: str, *, actor: str, inputs: dict | None = None) -> dict:
        self._require_workflow(workflow_file)
        gateway = self._require_gateway()
        declared = next(w["dispatch_inputs"] for w in CRON_WORKFLOWS
                        if w["file"] == workflow_file)
        cleaned = {}
        for key, value in (inputs or {}).items():
            if key not in declared:
                raise AdminBadRequest(
                    f"workflow {workflow_file} declares no {key!r} input"
                    + (f" (accepted: {', '.join(declared)})" if declared else ""))
            cleaned[key] = "true" if value in (True, "true") else "false"
        self._mutate(f"cron.dispatch:{workflow_file}", actor, {"inputs": cleaned},
                     lambda by: gateway.dispatch(workflow_file, inputs=cleaned or None))
        return {"dispatched": workflow_file, "inputs": cleaned}

    def cron_set_enabled(self, workflow_file: str, *, actor: str, enabled: bool) -> dict:
        self._require_workflow(workflow_file)
        gateway = self._require_gateway()
        verb = "enable" if enabled else "disable"
        self._mutate(f"cron.{verb}:{workflow_file}", actor, {},
                     lambda by: gateway.set_enabled(workflow_file, enabled=enabled))
        return {"workflow": workflow_file, "enabled": enabled}

    # ── internals ─────────────────────────────────────────────────────────────────────────────
    def _mutate(self, action: str, actor: str, args: dict, fn):
        """Run one mutation with `admin_actions` bookkeeping around it. The actor falls back to
        the configured default — attribution, not authorization, exactly `--by`'s contract. Any
        failure (a domain refusal included) records `outcome='error'` with the class and
        re-raises; the bookkeeping write itself may fail without failing the work."""
        by = (actor or "").strip() or self._admin.actor
        try:
            result = fn(by)
        except CaptureError as ex:
            admin_schema.record_action(self._conn, actor=by, action=action, args=args,
                                       outcome="error", error_class=ex.__class__.__name__)
            raise AdminRefused(str(ex)) from ex
        except Exception as ex:
            admin_schema.record_action(self._conn, actor=by, action=action, args=args,
                                       outcome="error", error_class=ex.__class__.__name__)
            raise
        admin_schema.record_action(self._conn, actor=by, action=action, args=args, outcome="ok")
        return result

    async def _mutate_async(self, action: str, actor: str, args: dict, fn):
        by = (actor or "").strip() or self._admin.actor
        try:
            result = await fn(by)
        except Exception as ex:
            admin_schema.record_action(self._conn, actor=by, action=action, args=args,
                                       outcome="error", error_class=ex.__class__.__name__)
            raise
        admin_schema.record_action(self._conn, actor=by, action=action, args=args, outcome="ok")
        return result

    def _require_workflow(self, workflow_file: str) -> None:
        if workflow_file not in DISPATCHABLE:
            raise AdminBadRequest(
                f"{workflow_file!r} is not a console-drivable workflow "
                f"(allowed: {', '.join(DISPATCHABLE)})")

    def _require_gateway(self):
        if self._gateway is None:
            raise AdminRefused(
                "GitHub is not configured — set the admin GitHub token to drive workflows from "
                "here; the database truth on this page stays readable without it")
        return self._gateway

    def _digest_settings(self) -> DigestSettings:
        try:
            return DigestSettings.from_args(None)
        except StartupError as ex:
            raise AdminRefused(str(ex)) from ex

    def _digest_pieces(self) -> dict:
        path = self._admin.channels_path
        return {"digest_channel_id": bool(os.environ.get(DIGEST_CHANNEL_ID_ENV)),
                "bot_token": bool(os.environ.get(SLACK_BOT_TOKEN_ENV)),
                "channels_path": bool(path), "channels_file_exists": bool(path) and
                os.path.exists(path)}

    def _digest_watermark(self) -> str | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT stats ->> 'until' FROM job_runs WHERE job = %s AND status = 'ok' "
                        "ORDER BY started_at DESC LIMIT 1", (DIGEST_JOB,))
            row = cur.fetchone()
        return row[0] if row else None

    def _truth_jobs(self, workflow: dict) -> tuple[str, ...]:
        job = workflow["truth"].split(":", 1)[1]
        return (job, f"{job}-dry-run") if job == PURGE_JOB else (job,)

    def _latest_job_run(self, jobs: tuple[str, ...]) -> dict | None:
        rows = self._job_runs(jobs, limit=1)
        return rows[0] if rows else None

    def _job_runs(self, jobs: tuple[str, ...], *, limit: int) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, job, status, started_at, finished_at, stats, error FROM job_runs"
                " WHERE job = ANY(%s) ORDER BY started_at DESC LIMIT %s",
                (list(jobs), max(1, min(int(limit), 100))))
            rows = cur.fetchall()
        return [{"id": r[0], "job": r[1], "status": r[2], "started_at": _iso(r[3]),
                 "finished_at": _iso(r[4]), "stats": r[5] or {}, "error": _clean(r[6])}
                for r in rows]

    def _ingest_errors(self, *, limit: int) -> dict:
        with self._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ingest_errors WHERE NOT resolved")
            total = cur.fetchone()[0]
            cur.execute(
                "SELECT id, source, source_doc_id, stage, error, attempts, last_at"
                " FROM ingest_errors WHERE NOT resolved ORDER BY last_at DESC LIMIT %s",
                (max(1, min(int(limit), 100)),))
            rows = cur.fetchall()
        return {"unresolved": total,
                "rows": [{"id": r[0], "source": r[1], "source_doc_id": r[2], "stage": r[3],
                          "error": _clean(r[4]), "attempts": r[5], "last_at": _iso(r[6])}
                         for r in rows]}

    def _built_at(self) -> str | None:
        meta = index_store.read_meta(self._conn)
        return meta.get("built_at") if meta else None

    def _zone_counts(self) -> dict[str, int]:
        """The one `pages_index` read this package makes — an AGGREGATE, no content columns, and
        a declared entry on the architecture test's reader-exception list."""
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT zone, count(*) FROM pages_index GROUP BY zone ORDER BY zone")
                return dict(cur.fetchall())
        except Exception:  # noqa: BLE001 — no index yet is a state, not an error
            return {}

    def _in_flight(self) -> list[dict]:
        rows = queue.query_in_flight(self._conn,
                                     visibility_timeout_s=worker_visibility_timeout_s())
        for row in rows:
            exhausted = int(row["attempts"]) >= WORKER_MAX_ATTEMPTS
            # `stigmergy-librarian status`'s three verdicts, same order, same facts beside them.
            if not row["lease_expired"]:
                row["verdict"] = "within its lease — a worker is presumably on it"
            elif exhausted:
                row["verdict"] = (f"lease expired and every delivery is burned "
                                  f"({row['attempts']}/{WORKER_MAX_ATTEMPTS}) — the next sweep "
                                  f"fails this row and records an ingest error")
            else:
                row["verdict"] = ("lease expired — a live worker would have finished or renewed "
                                  "it by now; the next sweep returns it to the queue with an "
                                  "attempt burned")
        return rows

    def _run_row(self, run: dict | None) -> dict | None:
        if run is None:
            return None
        return {"id": run["id"], "started_at": _iso(run.get("started_at")),
                "finished_at": _iso(run.get("finished_at")), "stats": run.get("stats") or {}}

    def _finding(self, finding: dict) -> dict:
        return {**finding, "subject": _clean(finding["subject"]),
                "detail": _clean(finding["detail"]),
                "suggested_action": _clean(finding["suggested_action"]),
                "created_at": _iso(finding.get("created_at"))}

    def _listed(self, row: dict) -> dict:
        shaped = self._traced_fields(row)
        if row["status"] == capture_schema.NEEDS_INPUT:
            shaped["reply_invocation"] = capture_schema.reply_invocation(row["id"])
        return shaped

    def _traced(self, trace: dict) -> dict:
        shaped = self._traced_fields(trace)
        if shaped.get("status") == capture_schema.NEEDS_INPUT:
            shaped["reply_invocation"] = capture_schema.reply_invocation(shaped["id"])
        return shaped

    def _traced_fields(self, row: dict) -> dict:
        """Sanitize every untrusted string a queue row carries, structure untouched."""
        out = dict(row)
        for key in ("excerpt", "error", "reply"):
            if key in out:
                out[key] = _clean(out[key])
        if out.get("events"):
            out["events"] = [{**e, "actor": _clean(e.get("actor")), "note": _clean(e.get("note")),
                              "event": _clean(e.get("event")), "at": _clean(e.get("at"))}
                             for e in out["events"]]
        if out.get("report"):
            out["report"] = {k: (_clean(v) if isinstance(v, str) else v)
                             for k, v in out["report"].items()}
        if out.get("hints") and isinstance(out["hints"], dict):
            out["hints"] = {k: (_clean(v) if isinstance(v, str) else v)
                            for k, v in out["hints"].items()}
        return out

    def _situation(self, row: dict) -> dict:
        """Sanitize a row `entities.situations` already shaped. Every derived field arrives
        decided — `subject`, `subjects` and `mint_name_prefill` alike — and is only cleaned here,
        so no caller can hand this a differently-preprocessed row and get a different default
        name than the other route. `mint_name_prefill == ""` is the instruction to leave the
        Approve form's field empty and list `subjects`, not an absent key.
        """
        out = self._traced_fields(row)
        out["subject"] = _clean(out.get("subject"))
        out["subjects"] = [_clean(s) for s in out.get("subjects") or []]
        out["mint_name_prefill"] = _clean(out.get("mint_name_prefill"))
        return out

    def _audit_aggregate(self) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT identity, tool, count(*), avg(duration_ms), max(ts) FROM audit_log"
                " GROUP BY identity, tool ORDER BY identity, tool")
            rows = cur.fetchall()
        return [{"identity": r[0], "tool": r[1], "calls": r[2],
                 "avg_duration_ms": float(r[3]) if r[3] is not None else None,
                 "last_at": _iso(r[4])} for r in rows]

    def _ask_questions(self, *, limit: int = 200) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT args ->> 'question' FROM audit_log"
                " WHERE tool = 'ask' AND outcome = 'ok' AND args ->> 'question' IS NOT NULL"
                " ORDER BY 1 LIMIT %s", (max(1, min(int(limit), 1000)),))
            return [_clean(row[0]) for row in cur.fetchall()]

    def _rate_limited(self, *, limit: int = 50) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT ts, identity, tool FROM audit_log WHERE error_class = 'RateLimitError'"
                " ORDER BY ts DESC LIMIT %s", (max(1, min(int(limit), 500)),))
            rows = cur.fetchall()
        return [{"ts": _iso(r[0]), "identity": r[1], "tool": r[2]} for r in rows]
