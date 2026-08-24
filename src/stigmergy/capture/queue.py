"""Durable idempotent work queue with fenced processing leases."""

from __future__ import annotations

import datetime as dt
import uuid

from psycopg.types.json import Jsonb

from stigmergy.capture import schema
from stigmergy.capture.errors import QueueStateError, SubmissionRejected

DEFAULT_VISIBILITY_TIMEOUT_S = 900
DEFAULT_MAX_ATTEMPTS = 3
RECLAIM_BATCH = 100
MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 20
RETRY_BASE_S = 30
MAX_SAFE_ERROR_CHARS = 1000

_ITEM_COLUMNS = (
    "id",
    "operation",
    "idempotency_key",
    "submitted_by",
    "actor",
    "acl",
    "request",
    "status",
    "attempts",
    "next_attempt_at",
    "created_at",
    "processing_started_at",
    "finished_at",
    "source_path",
    "commit_sha",
    "change_id",
    "extraction",
    "report",
    "error_category",
    "error",
)
_ITEM_SQL = ", ".join(_ITEM_COLUMNS)

_INSERT = f"""
INSERT INTO capture_queue (
    id, operation, idempotency_key, submitted_by, actor, acl, request, status
) VALUES (%(id)s, %(operation)s, %(idempotency_key)s, %(submitted_by)s,
          %(actor)s, %(acl)s, %(request)s, '{schema.QUEUED}')
ON CONFLICT (submitted_by, idempotency_key) DO NOTHING
RETURNING {_ITEM_SQL}
"""

_BY_IDEMPOTENCY = f"""
SELECT {_ITEM_SQL} FROM capture_queue
WHERE submitted_by = %s AND idempotency_key = %s
"""

_CLAIM = f"""
UPDATE capture_queue
SET status = '{schema.PROCESSING}',
    processing_started_at = now(),
    attempts = attempts + 1,
    error_category = '',
    error = ''
WHERE id = (
    SELECT id
    FROM capture_queue
    WHERE status = '{schema.QUEUED}' AND next_attempt_at <= now()
    ORDER BY created_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING {_ITEM_SQL}
"""

_LEASE_EXPIRED = (
    "processing_started_at < now() - make_interval(secs => %(visibility_timeout_s)s)"
)

_EXPIRED = f"""
SELECT id, attempts
FROM capture_queue
WHERE status = '{schema.PROCESSING}' AND {_LEASE_EXPIRED}
ORDER BY processing_started_at, id
FOR UPDATE SKIP LOCKED
LIMIT %(batch)s
"""

_REQUEUE_EXPIRED = f"""
UPDATE capture_queue
SET status = '{schema.QUEUED}',
    processing_started_at = NULL,
    next_attempt_at = now(),
    error_category = 'lease_expired',
    error = 'processing lease expired'
WHERE id = ANY(%s) AND status = '{schema.PROCESSING}'
"""

_FAIL_EXPIRED = f"""
UPDATE capture_queue
SET status = '{schema.FAILED}',
    finished_at = now(),
    error_category = 'attempts_exhausted',
    error = 'processing lease expired after all attempts'
WHERE id = ANY(%s) AND status = '{schema.PROCESSING}'
"""


def _iso(value) -> str | None:
    return value.isoformat() if isinstance(value, (dt.datetime, dt.date)) else None


def _shape(row) -> dict:
    item = dict(zip(_ITEM_COLUMNS, row, strict=True))
    item["id"] = str(item["id"])
    item["actor"] = item["actor"] or {}
    item["acl"] = None if item["acl"] is None else list(item["acl"])
    item["request"] = item["request"] or {}
    item["extraction"] = item["extraction"] or {}
    item["report"] = item["report"] or {}
    item["change_id"] = str(item["change_id"]) if item["change_id"] else None
    for key in (
        "next_attempt_at",
        "created_at",
        "processing_started_at",
        "finished_at",
    ):
        item[key] = _iso(item[key])
    return item


def _enqueue(
    conn,
    *,
    operation: str,
    item_id: uuid.UUID,
    idempotency_key: str,
    actor: schema.Actor,
    acl: tuple[str, ...] | None,
    request: dict,
) -> dict:
    params = {
        "id": item_id,
        "operation": operation,
        "idempotency_key": idempotency_key,
        "submitted_by": actor.subject,
        "actor": Jsonb(actor.model_dump(mode="json")),
        "acl": None if acl is None else list(acl),
        "request": Jsonb(request),
    }
    with conn.cursor() as cursor:
        cursor.execute(_INSERT, params)
        row = cursor.fetchone()
        created = row is not None
    if row is None:
        return _find_idempotent(
            conn,
            operation=operation,
            actor=actor,
            idempotency_key=idempotency_key,
            request=request,
            required=True,
        )
    shaped = _shape(row)
    shaped["created"] = created
    return shaped


def _find_idempotent(
    conn,
    *,
    operation: str,
    actor: schema.Actor,
    idempotency_key: str,
    request: dict,
    required: bool = False,
) -> dict | None:
    with conn.cursor() as cursor:
        cursor.execute(_BY_IDEMPOTENCY, (actor.subject, idempotency_key))
        row = cursor.fetchone()
    if row is None:
        if required:
            raise QueueStateError("idempotent queue lookup failed")
        return None
    shaped = _shape(row)
    if shaped["operation"] != operation or not _equivalent_request(
        shaped["request"], request
    ):
        raise SubmissionRejected("idempotency key was already used for a different request")
    shaped["created"] = False
    return shaped


def _equivalent_request(left: dict, right: dict) -> bool:
    def semantic(value: dict) -> dict:
        normalized = dict(value)
        normalized.pop("capture_id", None)
        normalized.pop("operation_id", None)
        normalized.pop("idempotency_key", None)
        origin = dict(normalized.get("origin") or {})
        origin.pop("captured_at", None)
        if origin:
            normalized["origin"] = origin
        return normalized

    return semantic(left) == semantic(right)


def enqueue_capture(conn, envelope: schema.CaptureEnvelope) -> dict:
    return _enqueue(
        conn,
        operation=schema.CAPTURE,
        item_id=envelope.capture_id,
        idempotency_key=envelope.idempotency_key,
        actor=envelope.actor,
        acl=envelope.audience,
        request=envelope.as_json(),
    )


def find_capture(conn, envelope: schema.CaptureEnvelope) -> dict | None:
    return _find_idempotent(
        conn,
        operation=schema.CAPTURE,
        actor=envelope.actor,
        idempotency_key=envelope.idempotency_key,
        request=envelope.as_json(),
    )


def enqueue_delete(conn, request: schema.DeleteRequest) -> dict:
    return _enqueue(
        conn,
        operation=schema.DELETE,
        item_id=request.operation_id,
        idempotency_key=request.idempotency_key,
        actor=request.actor,
        acl=None,
        request=request.as_json(),
    )


def enqueue_entity_operation(conn, request: schema.EntityOperationRequest) -> dict:
    return _enqueue(
        conn,
        operation=schema.ENTITY,
        item_id=request.operation_id,
        idempotency_key=request.idempotency_key,
        actor=request.actor,
        acl=None,
        request=request.as_json(),
    )


def enqueue_garden(conn, request: schema.GardenRequest) -> dict:
    return _enqueue(
        conn,
        operation=schema.GARDEN,
        item_id=request.operation_id,
        idempotency_key=request.idempotency_key,
        actor=request.actor,
        acl=None,
        request=request.as_json(),
    )


def claim_next(
    conn,
    *,
    visibility_timeout_s: int = DEFAULT_VISIBILITY_TIMEOUT_S,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict | None:
    release_expired(
        conn,
        visibility_timeout_s=visibility_timeout_s,
        max_attempts=max_attempts,
    )
    with conn.cursor() as cursor:
        cursor.execute(_CLAIM)
        row = cursor.fetchone()
    return _shape(row) if row else None


def release_expired(
    conn,
    *,
    visibility_timeout_s: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    batch: int = RECLAIM_BATCH,
) -> dict[str, int]:
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute(
            _EXPIRED,
            {
                "visibility_timeout_s": max(0, int(visibility_timeout_s)),
                "batch": max(1, int(batch)),
            },
        )
        rows = cursor.fetchall()
        failed = [item_id for item_id, attempts in rows if attempts >= max_attempts]
        retry = [item_id for item_id, attempts in rows if attempts < max_attempts]
        if retry:
            cursor.execute(_REQUEUE_EXPIRED, (retry,))
        if failed:
            cursor.execute(_FAIL_EXPIRED, (failed,))
    return {"released": len(retry), "failed": len(failed)}


def holds_lease(conn, item_id: str | uuid.UUID, *, expected_attempts: int) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT status, attempts FROM capture_queue WHERE id = %s",
            (item_id,),
        )
        row = cursor.fetchone()
    return bool(
        row
        and row[0] == schema.PROCESSING
        and int(row[1]) == int(expected_attempts)
    )


def finish_landed(
    conn,
    item_id: str | uuid.UUID,
    *,
    expected_attempts: int,
    source_path: str = "",
    commit_sha: str,
    change_id: str | uuid.UUID | None,
    extraction: dict | None = None,
    report: dict | None = None,
) -> dict:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE capture_queue
            SET status = '{schema.LANDED}',
                finished_at = now(),
                source_path = %s,
                commit_sha = %s,
                change_id = %s,
                extraction = %s,
                report = %s,
                error_category = '',
                error = ''
            WHERE id = %s
              AND status = '{schema.PROCESSING}'
              AND attempts = %s
            RETURNING {_ITEM_SQL}
            """,
            (
                source_path,
                commit_sha,
                change_id,
                Jsonb(extraction or {}),
                Jsonb(report or {}),
                item_id,
                int(expected_attempts),
            ),
        )
        row = cursor.fetchone()
    if row is None:
        _raise_lost_lease(conn, item_id, expected_attempts)
    return _shape(row)


def fail_or_retry(
    conn,
    item_id: str | uuid.UUID,
    *,
    expected_attempts: int,
    category: str,
    error: str,
    retryable: bool,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict:
    safe_category = _safe(category, 100) or "processing_failed"
    safe_error = _safe(error, MAX_SAFE_ERROR_CHARS) or "processing failed"
    retry = retryable and expected_attempts < max_attempts
    status = schema.QUEUED if retry else schema.FAILED
    delay = min(RETRY_BASE_S * (2 ** max(0, expected_attempts - 1)), 900)
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE capture_queue
            SET status = %s,
                processing_started_at = CASE WHEN %s THEN NULL ELSE processing_started_at END,
                next_attempt_at = CASE
                    WHEN %s THEN now() + make_interval(secs => %s)
                    ELSE next_attempt_at
                END,
                finished_at = CASE WHEN %s THEN NULL ELSE now() END,
                error_category = %s,
                error = %s
            WHERE id = %s
              AND status = '{schema.PROCESSING}'
              AND attempts = %s
            RETURNING {_ITEM_SQL}
            """,
            (
                status,
                retry,
                retry,
                delay,
                retry,
                safe_category,
                safe_error,
                item_id,
                int(expected_attempts),
            ),
        )
        row = cursor.fetchone()
    if row is None:
        _raise_lost_lease(conn, item_id, expected_attempts)
    return _shape(row)


def retry_failed(conn, item_id: str | uuid.UUID) -> dict:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE capture_queue
            SET status = '{schema.QUEUED}',
                attempts = 0,
                next_attempt_at = now(),
                processing_started_at = NULL,
                finished_at = NULL,
                error_category = '',
                error = ''
            WHERE id = %s AND status = '{schema.FAILED}'
            RETURNING {_ITEM_SQL}
            """,
            (item_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise QueueStateError("only a failed item can be retried")
    return _shape(row)


def _raise_lost_lease(conn, item_id, expected_attempts: int) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT status, attempts FROM capture_queue WHERE id = %s",
            (item_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise QueueStateError(f"queue item {item_id} does not exist")
    status, attempts = row
    if status == schema.PROCESSING and int(attempts) != int(expected_attempts):
        raise QueueStateError("processing lease was redelivered")
    raise QueueStateError(f"queue item is {status!r}, not owned by this processor")


def _safe(value: str, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def query_submissions(
    conn,
    *,
    submitter: str | None = None,
    statuses: list[str] | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> list[dict]:
    unknown = sorted(set(statuses or ()) - set(schema.STATUSES))
    if unknown:
        raise ValueError(f"unknown status: {', '.join(unknown)}")
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT {_ITEM_SQL}
            FROM capture_queue
            WHERE (%s::text IS NULL OR submitted_by = %s)
              AND (%s::text[] IS NULL OR status = ANY(%s))
            ORDER BY created_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            (
                submitter,
                submitter,
                statuses,
                statuses,
                max(1, min(int(limit), MAX_LIST_LIMIT)),
                max(0, int(offset)),
            ),
        )
        return [_shape(row) for row in cursor.fetchall()]


def list_own_submissions(conn, submitter: str, **kwargs) -> list[dict]:
    if not submitter:
        raise ValueError("submitter is required")
    return query_submissions(conn, submitter=submitter, **kwargs)


def list_all_submissions(conn, **kwargs) -> list[dict]:
    return query_submissions(conn, **kwargs)


def get_submission_trace(
    conn,
    item_id: str | uuid.UUID,
    *,
    submitter: str | None = None,
) -> dict | None:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT {_ITEM_SQL}
            FROM capture_queue
            WHERE id = %s AND (%s::text IS NULL OR submitted_by = %s)
            """,
            (item_id, submitter, submitter),
        )
        row = cursor.fetchone()
    return _shape(row) if row else None


def query_in_flight(
    conn, *, visibility_timeout_s: int = DEFAULT_VISIBILITY_TIMEOUT_S
) -> list[dict]:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT {_ITEM_SQL},
                   extract(epoch from (now() - processing_started_at)) * 1000,
                   ({_LEASE_EXPIRED})
            FROM capture_queue
            WHERE status = '{schema.PROCESSING}'
            ORDER BY processing_started_at, id
            """,
            {"visibility_timeout_s": max(0, int(visibility_timeout_s))},
        )
        rows = cursor.fetchall()
    result = []
    for row in rows:
        item = _shape(row[: len(_ITEM_COLUMNS)])
        item["processing_age_ms"] = float(row[-2])
        item["lease_expired"] = bool(row[-1])
        result.append(item)
    return result


def counts_by_status(conn) -> dict[str, int]:
    with conn.cursor() as cursor:
        cursor.execute("SELECT status, count(*) FROM capture_queue GROUP BY status")
        found = dict(cursor.fetchall())
    return {status: int(found.get(status, 0)) for status in schema.STATUSES}


def current_status(conn, item_id: str | uuid.UUID) -> str | None:
    with conn.cursor() as cursor:
        cursor.execute("SELECT status FROM capture_queue WHERE id = %s", (item_id,))
        row = cursor.fetchone()
    return row[0] if row else None


def work_waiting(conn) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT EXISTS (SELECT 1 FROM capture_queue "
            f"WHERE status = '{schema.QUEUED}' AND next_attempt_at <= now())"
        )
        return bool(cursor.fetchone()[0])


def landed_latencies_ms(conn, *, limit: int = 500) -> list[float]:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT extract(epoch from (finished_at - created_at)) * 1000
            FROM capture_queue
            WHERE status = '{schema.LANDED}' AND finished_at IS NOT NULL
            ORDER BY finished_at DESC
            LIMIT %s
            """,
            (max(1, min(int(limit), 10_000)),),
        )
        return [float(row[0]) for row in cursor.fetchall()]


def outcomes_by_day(conn, *, days: int) -> list[dict]:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT (created_at AT TIME ZONE 'UTC')::date, status, count(*) "
            "FROM capture_queue "
            "WHERE created_at >= now() - make_interval(days => %s) "
            "GROUP BY 1, 2 ORDER BY 1, 2",
            (max(1, int(days)),),
        )
        rows = cursor.fetchall()
    return [
        {"day": day.isoformat(), "status": status, "count": int(count)}
        for day, status, count in rows
    ]
