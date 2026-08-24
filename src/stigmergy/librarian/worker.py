from __future__ import annotations

import datetime as dt
import logging
import os
import signal
import time
from dataclasses import dataclass

import psycopg
from pydantic_ai.exceptions import AgentRunError, ModelHTTPError

from stigmergy.capture import ops, queue, schema, uploads
from stigmergy.capture.errors import CaptureError, QueueStateError, SubmissionRejected
from stigmergy.kernel.deadline import hard_deadline
from stigmergy.knowledge.planner import PydanticPlanner, ScriptedPlanner
from stigmergy.knowledge.writer import (
    KnowledgeWriteError,
    WriterDeadline,
    WriterDeps,
    process,
)
from stigmergy.librarian import gitcmd, schedule
from stigmergy.librarian.errors import GitError, LibrarianConfigError

log = logging.getLogger(__name__)
GARDEN_JOB = "garden"
GARDEN_CHECK_INTERVAL_S = 60
UPLOAD_PURGE_INTERVAL_S = 300
LEASE_ABORT_MARGIN_S = 60
STATEMENT_TIMEOUT_MS = 10_000


@dataclass(frozen=True)
class ProcessOutcome:
    status: str
    report: dict


class LeaseAbort(RuntimeError):
    pass


def configure_connection(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('statement_timeout', %s, false)",
            (f"{STATEMENT_TIMEOUT_MS}ms",),
        )


def startup_checks(settings) -> dict:
    settings.check_domains()
    if settings.backend == "pydantic" and not os.environ.get("OPENROUTER_API_KEY", "").strip():
        raise LibrarianConfigError("OPENROUTER_API_KEY is required by the writer")
    repo = gitcmd.ensure_repo(settings.repo)
    if settings.backend == "pydantic":
        skill = os.path.join(repo, ".claude", "skills", "librarian", "SKILL.md")
        if not os.path.isfile(skill):
            raise LibrarianConfigError("the knowledge repository has no librarian skill")
    base = gitcmd.base_ref(repo, settings.branch)
    if settings.require_remote_base and not base.remote:
        raise LibrarianConfigError("the deployed writer could not resolve the remote branch")
    gitcmd.reap(repo, settings.worktree_root)
    return {"repo": repo, "base": base}


def build_deps(settings, resolved: dict, evidence) -> WriterDeps:
    planner = PydanticPlanner(settings) if settings.backend == "pydantic" else ScriptedPlanner()
    return WriterDeps(
        settings=settings,
        evidence=evidence,
        planner=planner,
        repo=resolved["repo"],
    )


def process_next(conn, deps: WriterDeps):
    settings = deps.settings
    item = queue.claim_next(
        conn,
        visibility_timeout_s=settings.visibility_timeout_s,
        max_attempts=settings.max_attempts,
    )
    if item is None:
        return None
    lease_budget_s = settings.visibility_timeout_s - LEASE_ABORT_MARGIN_S
    with hard_deadline(
        lease_budget_s,
        lambda: LeaseAbort("knowledge operation could not finalize before lease expiry"),
    ):
        ops.heartbeat(conn, "processing")
        try:
            result = process(conn, item, deps)
            row = queue.finish_landed(
                conn,
                item["id"],
                expected_attempts=item["attempts"],
                source_path=result.source_path,
                commit_sha=result.commit_sha,
                change_id=result.change_id,
                extraction=result.extraction,
                report=result.report,
            )
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
                category=getattr(error, "category", error.__class__.__name__),
                error=_safe_error(error),
                retryable=retryable,
                max_attempts=settings.max_attempts,
            )
            if retryable:
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
    if isinstance(error, KnowledgeWriteError):
        return bool(error.retryable)
    if isinstance(error, ModelHTTPError):
        return error.status_code in {408, 409, 425, 429} or error.status_code >= 500
    return isinstance(
        error,
        (
            AgentRunError,
            CaptureError,
            GitError,
            WriterDeadline,
            psycopg.OperationalError,
            TimeoutError,
            ConnectionError,
            OSError,
        ),
    )


def _safe_error(error: Exception) -> str:
    if isinstance(error, SubmissionRejected):
        return " ".join(str(error).split())[:1000]
    return f"knowledge processing failed ({error.__class__.__name__})"


class Worker:
    def __init__(
        self,
        conn,
        deps: WriterDeps,
        *,
        on_output=print,
        utcnow=None,
        monotonic=None,
    ):
        self.conn = conn
        self.deps = deps
        self.on_output = on_output
        self.utcnow = utcnow or (lambda: dt.datetime.now(dt.UTC))
        self.monotonic = monotonic or time.monotonic
        self.stopping = False
        self._last_garden_check = None
        self._last_upload_purge = None

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._stop)
        signal.signal(signal.SIGTERM, self._stop)

    def _stop(self, _signum, _frame) -> None:
        self.stopping = True

    def run(self) -> int:
        processed = 0
        ops.heartbeat(self.conn, "idle")
        queue.release_expired(
            self.conn,
            visibility_timeout_s=self.deps.settings.visibility_timeout_s,
            max_attempts=self.deps.settings.max_attempts,
        )
        while not self.stopping:
            self._maybe_garden()
            self._maybe_purge_uploads()
            outcome = process_next(self.conn, self.deps)
            if outcome is not None:
                processed += 1
                item, result = outcome
                self.on_output(f"#{item['id']} -> {result.status}")
                ops.heartbeat(self.conn, "idle")
                continue
            ops.heartbeat(self.conn, "idle")
            self._sleep(self.deps.settings.poll_interval_s)
        return processed

    def _maybe_garden(self) -> bool:
        monotonic = self.monotonic()
        if self._last_garden_check is not None and monotonic - self._last_garden_check < GARDEN_CHECK_INTERVAL_S:
            return False
        self._last_garden_check = monotonic
        if str(self.deps.settings.garden_at).strip().lower() == "off":
            return False
        at = schedule.parse_daily(
            self.deps.settings.garden_at,
            default=schedule.DEFAULT_GARDEN_AT,
        )
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
            return uploads.purge_expired(self.conn, self.deps.evidence)
        except Exception as error:
            log.error("expired upload cleanup failed (%s)", error.__class__.__name__)
            return 0

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < deadline:
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
