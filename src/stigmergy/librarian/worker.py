from __future__ import annotations

import datetime as dt
import logging
import os
import signal
import time
from dataclasses import dataclass

import httpx
import psycopg
from pydantic_ai.exceptions import ModelHTTPError

from stigmergy.capture import ops, queue, schema, uploads
from stigmergy.capture.errors import QueueStateError, SubmissionRejected
from stigmergy.kernel import shutdown, tenancy
from stigmergy.kernel.deadline import hard_deadline
from stigmergy.knowledge.writer import (
    WRITER_LOCK_KEY,
    process,
)
from stigmergy.librarian import config, schedule, workspaces
from stigmergy.librarian.errors import LibrarianConfigError
from stigmergy.ops import erase_layer, erase_workspace, erasure
from stigmergy.server import platform, usage

log = logging.getLogger(__name__)
GARDEN_JOB = "garden"
REBUILD_JOB = "index-rebuild"
GARDEN_CHECK_INTERVAL_S = 60
REBUILD_RETRY = dt.timedelta(minutes=10)
# A failed provisioning waits 1, 2, 4... minutes, at most an hour, before its next attempt.
PROVISION_RETRY_S = 60.0
PROVISION_RETRY_MAX_S = 3600.0
UPLOAD_PURGE_INTERVAL_S = 300
LEASE_ABORT_MARGIN_S = 60
STATEMENT_TIMEOUT_MS = 10_000
# An erasure rewrites history and rebuilds the index; its statements may outlast an operation's.
ERASURE_STATEMENT_TIMEOUT_MS = 900_000


@dataclass(frozen=True)
class ProcessOutcome:
    status: str
    report: dict


class LeaseAbort(RuntimeError):
    pass


def configure_connection(conn) -> None:
    _statement_timeout(conn, STATEMENT_TIMEOUT_MS)


def _statement_timeout(conn, milliseconds: int) -> None:
    with conn.cursor() as cursor:
        cursor.execute("SELECT set_config('statement_timeout', %s, false)", (f"{milliseconds}ms",))


def check_settings(settings) -> None:
    settings.check_domains()
    if settings.backend == "pydantic" and not os.environ.get("OPENROUTER_API_KEY", "").strip():
        raise LibrarianConfigError("OPENROUTER_API_KEY is required by the writer")


_PENDING = (
    ("capture_queue", "SELECT 1 FROM capture_queue q JOIN tenants t ON t.id = q.tenant_id "
                      "AND t.status = 'active' WHERE q.status IN ('queued', 'processing')"),
    ("layer_erasures", "SELECT 1 FROM layer_erasures WHERE status IN ('queued', 'running')"),
    ("workspace_erasures", "SELECT 1 FROM workspace_erasures WHERE status IN ('queued', 'running')"),
    ("tenants", "SELECT 1 FROM tenants WHERE status = 'provisioning'"),
    ("index_health", "SELECT 1 FROM tenants t WHERE t.status = 'active' AND NOT EXISTS "
                     "(SELECT 1 FROM index_health h WHERE h.tenant_id = t.id "
                     "AND h.last_full_rebuild_at IS NOT NULL)"),
)


def _pending_work(conn) -> bool:
    """Whether any workspace has work the writer must finish or retry, due now or later."""
    with tenancy.platform_scope(conn), conn.cursor() as cursor:
        for table, sql in _PENDING:
            cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
            if cursor.fetchone()[0]:
                cursor.execute(f"SELECT EXISTS ({sql})")
                if cursor.fetchone()[0]:
                    return True
    return False


def process_next(conn, deps):
    """Claim the next job of the tenant served longest ago and process it as that tenant, in that
    tenant's checkout. `deps` is one checkout's writer dependencies or the per-tenant workspaces."""
    places = workspaces.resolve(deps)
    settings = places.settings
    with tenancy.platform_scope(conn):
        _release_expired_if_writer_idle(conn, settings.visibility_timeout_s)
        item = queue.claim_next(
            conn,
            operation_budget_s=config.operation_budget_s(timeout_s=settings.timeout_s),
        )
    if item is None:
        return None
    with tenancy.tenant_scope(conn, item["tenant_id"]):
        return _process_claimed(conn, item, places, settings)


def _process_claimed(conn, item: dict, places, settings):
    lease_budget_s = settings.visibility_timeout_s - LEASE_ABORT_MARGIN_S
    with hard_deadline(
        lease_budget_s,
        lambda: LeaseAbort("knowledge operation could not finalize before lease expiry"),
    ):
        ops.heartbeat(conn, "processing")
        try:
            result = process(conn, item, places.deps_for(item["tenant_id"]))
            usage.record_filing(conn, result.report, settings.model)
            row = queue.finish_landed(
                conn,
                item["id"],
                expected_attempts=item["attempts"],
                expected_lease_started_at=item["lease_started_at"],
                source_path=result.source_path,
                commit_sha=result.commit_sha,
                change_id=result.change_id,
                extraction=result.extraction,
                report=result.report,
            )
        except shutdown.WriterShutdown:
            queue.release_claim(conn, item["id"], expected_lease_started_at=item["lease_started_at"])
            log.warning("knowledge operation %s returned to the queue: the writer is stopping", item["id"])
            raise
        except LeaseAbort:
            raise
        except QueueStateError:
            raise
        except Exception as error:
            retryable = _retryable(error)
            row = queue.fail_or_retry(
                conn,
                item["id"],
                expected_attempts=item["attempts"],
                expected_lease_started_at=item["lease_started_at"],
                category=getattr(error, "category", error.__class__.__name__),
                error=_safe_error(error),
                retryable=retryable,
                fresh_budget=retryable and bool(getattr(error, "fresh_budget", False)),
            )
            if row["status"] == schema.QUEUED:
                log.error(
                    "knowledge operation %s will retry (%s)",
                    item["id"],
                    error.__class__.__name__,
                )
            else:
                log.warning(
                    "knowledge operation %s failed: %s",
                    item["id"],
                    error.__class__.__name__,
                )
    return row, ProcessOutcome(status=row["status"], report=row.get("report") or {})


def _retryable(error: Exception) -> bool:
    if isinstance(error, SubmissionRejected):
        return False
    if hasattr(error, "retryable"):
        return bool(error.retryable)
    if isinstance(error, ModelHTTPError):
        return error.status_code in {408, 409, 425, 429} or error.status_code >= 500
    return isinstance(
        error,
        (httpx.TimeoutException, httpx.NetworkError, psycopg.OperationalError, ConnectionError),
    )


def _release_expired_if_writer_idle(conn, visibility_timeout_s: int) -> dict[str, int]:
    """Reclaim leases only when no writer can be inside its publish transaction."""
    with ops.try_advisory_lock(conn, WRITER_LOCK_KEY) as acquired:
        if not acquired:
            return {"released": 0, "failed": 0}
        return queue.release_expired(
            conn,
            visibility_timeout_s=visibility_timeout_s,
        )


def _safe_error(error: Exception) -> str:
    if isinstance(error, SubmissionRejected):
        return " ".join(str(error).split())[:1000]
    return f"knowledge processing failed ({error.__class__.__name__})"


class Worker:
    def __init__(
        self,
        conn,
        deps,
        *,
        on_output=print,
        utcnow=None,
        monotonic=None,
        embedder_factory=None,
        provisioner=None,
        repo_remover=None,
    ):
        self.conn = conn
        self.deps = deps
        self.places = workspaces.resolve(deps)
        self.settings = self.places.settings
        self.evidence = getattr(self.places, "evidence", None)
        self.provisioner = provisioner
        self.repo_remover = repo_remover
        self.embedder_factory = embedder_factory or self._index_embedder
        self.on_output = on_output
        self.utcnow = utcnow or (lambda: dt.datetime.now(dt.UTC))
        self.monotonic = monotonic or time.monotonic
        self.stopping = False
        shutdown.clear()
        self._last_garden_check = None
        self._last_rebuild_check = None
        self._provision_retry: dict[str, tuple[float, float]] = {}
        self._last_upload_purge = None

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._stop)
        signal.signal(signal.SIGTERM, self._stop)

    def _stop(self, _signum, _frame) -> None:
        self.stopping = True
        shutdown.request()

    def run(self) -> int:
        processed = 0
        ops.heartbeat(self.conn, "idle")
        last_work = self.monotonic()
        while not self.stopping:
            if self._maybe_erase_workspace() or self._maybe_provision():
                last_work = self.monotonic()
                continue
            self._maybe_garden()
            self._maybe_purge_uploads()
            if self._maybe_erase() or self._maybe_rebuild():
                last_work = self.monotonic()
                continue
            try:
                outcome = process_next(self.conn, self.places)
            except shutdown.WriterShutdown:
                return processed
            if outcome is not None:
                processed += 1
                item, result = outcome
                self.on_output(f"#{item['id']} -> {result.status}")
                ops.heartbeat(self.conn, "idle")
                last_work = self.monotonic()
                continue
            idle_exit = getattr(self.settings, "idle_exit_s", 0)
            if idle_exit and self.monotonic() - last_work >= idle_exit:
                if self._may_stop():
                    self.on_output("writer idle; its machine stops until work is queued")
                    return processed
                last_work = self.monotonic()
            ops.heartbeat(self.conn, "idle")
            self._sleep(self.settings.poll_interval_s)
        return processed

    def _may_stop(self) -> bool:
        """Announce the stop, then look once more: anything queued, retrying, open, provisioning,
        or an index never built keeps the writer running, since nothing would wake it for those.
        Work queued after this look wakes a new writer (`kernel.wake`)."""
        ops.heartbeat(self.conn, "sleeping")
        if _pending_work(self.conn):
            ops.heartbeat(self.conn, "idle")
            return False
        return True

    def _active_tenants(self) -> list[platform.Tenant]:
        return platform.list_tenants(self.conn, statuses=("active",))

    def _maybe_erase(self) -> bool:
        """Run an open layer erasure before the next operation, under the writer lock, as the
        tenant that asked for it and in that tenant's checkout."""
        with tenancy.platform_scope(self.conn):
            if not erasure.pending(self.conn):
                return False
        with ops.try_advisory_lock(self.conn, WRITER_LOCK_KEY) as acquired:
            if not acquired:
                return False
            with tenancy.platform_scope(self.conn):
                item = erasure.claim(self.conn)
            if item is None:
                return False
            ops.heartbeat(self.conn, "erasing")
            _statement_timeout(self.conn, ERASURE_STATEMENT_TIMEOUT_MS)
            try:
                with tenancy.tenant_scope(self.conn, item["tenant_id"]):
                    erase_layer.run_claimed(
                        self.conn,
                        item,
                        repo=self.places.refresh(item["tenant_id"]),
                        branch=self.settings.branch,
                        evidence=self.evidence,
                        embedder_factory=self.embedder_factory,
                        scratch_root=self.settings.worktree_root,
                        on_output=self.on_output,
                    )
            finally:
                _statement_timeout(self.conn, STATEMENT_TIMEOUT_MS)
                ops.heartbeat(self.conn, "idle")
        return True

    def _maybe_erase_workspace(self) -> bool:
        """Carry one due workspace deletion through its remaining steps, under the writer lock."""
        if not erase_workspace.pending(self.conn):
            return False
        with ops.try_advisory_lock(self.conn, WRITER_LOCK_KEY) as acquired:
            if not acquired:
                return False
            item = erase_workspace.claim(self.conn)
            if item is None:
                return False
            ops.heartbeat(self.conn, "erasing")
            _statement_timeout(self.conn, ERASURE_STATEMENT_TIMEOUT_MS)
            try:
                receipt = erase_workspace.run(self.conn, item, places=self.places,
                                              evidence=self.evidence, remove_repo=self.repo_remover)
            except Exception as error:  # noqa: BLE001 — recorded on the request, retried with backoff
                status = erase_workspace.fail(self.conn, item, error)
                log.error("deleting workspace %s stopped (%s); %s", item["slug"],
                          error.__class__.__name__, status)
                platform.record_action(self.conn, actor="system:worker", action="tenant.erase",
                                       tenant_slug=item["slug"], outcome="failed",
                                       args={"error": error.__class__.__name__, "status": status})
                return False
            finally:
                _statement_timeout(self.conn, STATEMENT_TIMEOUT_MS)
                ops.heartbeat(self.conn, "idle")
            erase_workspace.finish(self.conn, item, receipt)
            platform.record_action(self.conn, actor="system:worker", action="tenant.erased",
                                   tenant_slug=item["slug"], outcome="ok", args=receipt)
            self.on_output(f"workspace {item['slug']} -> deleted")
        return True

    def _maybe_provision(self) -> bool:
        """Create the repository of a tenant still provisioning before anything else it needs."""
        if self.provisioner is None:
            return False
        now = self.monotonic()
        waiting = [tenant for tenant in platform.list_tenants(self.conn, statuses=("provisioning",))
                   if self._provision_retry.get(tenant.id, (0.0, 0.0))[0] <= now]
        if not waiting:
            return False
        tenant = waiting[0]
        with ops.try_advisory_lock(self.conn, WRITER_LOCK_KEY) as acquired:
            if not acquired:
                return False
            ops.heartbeat(self.conn, "provisioning")
            try:
                self.provisioner(tenant, self.places.path(tenant.id))
            except Exception as error:  # noqa: BLE001 — retried after a growing pause
                delay = min(PROVISION_RETRY_MAX_S,
                            2 * self._provision_retry.get(tenant.id, (0.0, PROVISION_RETRY_S / 2))[1])
                self._provision_retry[tenant.id] = (now + delay, delay)
                log.error("provisioning %s failed (%s); retrying in %ds", tenant.slug,
                          error.__class__.__name__, delay)
                platform.record_action(self.conn, actor="system:worker", action="tenant.provision",
                                       tenant_slug=tenant.slug, outcome="failed",
                                       args={"error": error.__class__.__name__})
                return False
            finally:
                ops.heartbeat(self.conn, "idle")
            self._provision_retry.pop(tenant.id, None)
            platform.set_tenant_status(self.conn, tenant.id, "active")
            platform.record_action(self.conn, actor="system:worker", action="tenant.provisioned",
                                   tenant_slug=tenant.slug, outcome="ok")
            self.on_output(f"tenant {tenant.slug} -> active")
        return True

    def _maybe_rebuild(self) -> bool:
        """The daily full index rebuild of each tenant whose rebuild is due, one per pass."""
        monotonic = self.monotonic()
        if (self._last_rebuild_check is not None
                and monotonic - self._last_rebuild_check < GARDEN_CHECK_INTERVAL_S):
            return False
        self._last_rebuild_check = monotonic
        if str(self.settings.rebuild_at).strip().lower() == "off":
            return False
        at = schedule.parse_daily(self.settings.rebuild_at, default=schedule.DEFAULT_REBUILD_AT)
        for tenant in self._active_tenants():
            with tenancy.tenant_scope(self.conn, tenant.id):
                if not self._rebuild_due(at):
                    continue
                return self._rebuild(tenant)
        return False

    def _rebuild_due(self, at) -> bool:
        """Daily, or at once for an index never built (a new tenant, a migrated one, or one the
        console asked to rebuild), at most every ten minutes while that keeps failing."""
        from stigmergy.index import health

        now = self.utcnow()
        last = schedule.last_run_at(self.conn, REBUILD_JOB)
        if health.read(self.conn)["last_full_rebuild_at"] is None:
            return last is None or now - last > REBUILD_RETRY
        return schedule.daily_due(now, last, at)

    def _rebuild(self, tenant: platform.Tenant) -> bool:
        from stigmergy.index import build

        with ops.try_advisory_lock(self.conn, WRITER_LOCK_KEY) as acquired:
            if not acquired:
                return False
            ops.heartbeat(self.conn, "rebuilding")
            _statement_timeout(self.conn, ERASURE_STATEMENT_TIMEOUT_MS)
            try:
                with ops.job_run(self.conn, REBUILD_JOB) as run:
                    repo = self.places.refresh(tenant.id)
                    stats = build.rebuild(self.conn, repo, self.embedder_factory(),
                                          require_repository_head=True)
                    run.stats.update({key: stats[key] for key in ("pages", "fragments", "embedded",
                                                                  "cached")})
                    run.head_commit_sha = str(stats.get("commit_sha") or "")
                self.on_output(f"rebuild {tenant.slug} -> {stats['pages']} pages")
            except Exception as error:  # noqa: BLE001 — recorded on the run; retried tomorrow
                log.error("index rebuild of %s failed (%s)", tenant.slug, error.__class__.__name__)
            finally:
                _statement_timeout(self.conn, STATEMENT_TIMEOUT_MS)
                ops.heartbeat(self.conn, "idle")
        return True

    def _index_embedder(self):
        from stigmergy.index import store
        from stigmergy.index.backends.embedder import build_embedder, embedder_for_model

        model = (store.read_meta(self.conn) or {}).get("model") or ""
        return embedder_for_model(model) if model else build_embedder("openrouter")

    def _maybe_garden(self) -> bool:
        monotonic = self.monotonic()
        if self._last_garden_check is not None and monotonic - self._last_garden_check < GARDEN_CHECK_INTERVAL_S:
            return False
        self._last_garden_check = monotonic
        if str(self.settings.garden_at).strip().lower() == "off":
            return False
        at = schedule.parse_daily(
            self.settings.garden_at,
            default=schedule.DEFAULT_GARDEN_AT,
        )
        created = False
        for tenant in self._active_tenants():
            with tenancy.tenant_scope(self.conn, tenant.id):
                created = self._garden_tenant(at) or created
        return created

    def _garden_tenant(self, at) -> bool:
        last = schedule.last_run_at(self.conn, GARDEN_JOB)
        if not schedule.daily_due(self.utcnow(), last, at):
            return False
        today = self.utcnow().astimezone(dt.UTC).date().isoformat()
        request = schema.GardenRequest(
            idempotency_key=f"garden:scheduled:{today}",
            actor=schema.Actor(subject="system:garden", display_name="Stigmergy Gardener"),
            rationale="Scheduled corpus health run",
        )
        queued = queue.enqueue_garden(self.conn, request)
        if queued["created"]:
            self.on_output(f"garden #{queued['id']} -> queued")
        return queued["created"]

    def _maybe_purge_uploads(self) -> int:
        now = self.monotonic()
        if self._last_upload_purge is not None and now - self._last_upload_purge < UPLOAD_PURGE_INTERVAL_S:
            return 0
        self._last_upload_purge = now
        try:
            platform.purge_access_requests(self.conn)
        except Exception as error:  # noqa: BLE001 — retried on the next interval
            log.error("access request expiry failed (%s)", error.__class__.__name__)
        try:
            return uploads.purge_expired(self.conn, self.evidence)
        except Exception as error:
            log.error("expired upload cleanup failed (%s)", error.__class__.__name__)
            return 0

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < deadline:
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
