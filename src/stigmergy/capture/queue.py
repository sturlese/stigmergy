"""Durable idempotent work queue with fenced processing leases."""

from __future__ import annotations

import datetime as dt
import uuid

from psycopg.types.json import Jsonb

from stigmergy.capture import schema
from stigmergy.capture.errors import QueueStateError, SubmissionRejected
from stigmergy.kernel import wake

DEFAULT_VISIBILITY_TIMEOUT_S = 900
DEFAULT_MAX_ATTEMPTS = 2
RECLAIM_BATCH = 100
MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 20
RETRY_DELAY_S = 10
MAX_SAFE_ERROR_CHARS = 1000

_ITEM_COLUMNS = (
    "id",
    "tenant_id",
    "operation",
    "idempotency_key",
    "submitted_by",
    "actor",
    "acl",
    "layer_decision",
    "layer_probabilities",
    "request",
    "status",
    "attempts",
    "next_attempt_at",
    "created_at",
    "processing_started_at",
    "budget_deadline_at",
    "lease_started_at",
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
    id, operation, idempotency_key, submitted_by, actor, acl, layer_decision,
    layer_probabilities, request, status
) VALUES (%(id)s, %(operation)s, %(idempotency_key)s, %(submitted_by)s,
          %(actor)s, %(acl)s, %(layer_decision)s, %(layer_probabilities)s, %(request)s,
          '{schema.QUEUED}')
ON CONFLICT (tenant_id, submitted_by, idempotency_key) DO NOTHING
RETURNING {_ITEM_SQL}
"""

_BY_IDEMPOTENCY = f"""
SELECT {_ITEM_SQL} FROM capture_queue
WHERE submitted_by = %s AND idempotency_key = %s
"""

_CLAIM = f"""
UPDATE capture_queue
SET status = '{schema.PROCESSING}',
    processing_started_at = COALESCE(processing_started_at, clock_timestamp()),
    budget_deadline_at = COALESCE(
        budget_deadline_at,
        clock_timestamp() + make_interval(secs => %(operation_budget_s)s)
    ),
    lease_started_at = clock_timestamp(),
    attempts = attempts + 1,
    error_category = '',
    error = ''
WHERE id = (SELECT id FROM picked)
RETURNING {_ITEM_SQL}
"""

# Tenants take turns: the claim serves the active tenant served longest ago, then that tenant's
# oldest due item, so one tenant's backlog never starves another's.
_PICK = f"""
WITH picked AS (
    SELECT queued.id, queued.tenant_id
    FROM capture_queue AS queued
    JOIN tenants AS tenant ON tenant.id = queued.tenant_id AND tenant.status = 'active'
    WHERE queued.status = '{schema.QUEUED}' AND queued.next_attempt_at <= now()
      AND NOT EXISTS (
          SELECT 1 FROM layer_erasures AS erasing
          WHERE erasing.tenant_id = queued.tenant_id
            AND erasing.layer = ANY(COALESCE(queued.acl, '{{}}'::text[]))
            AND erasing.status IN ('{schema.QUEUED}', '{schema.ERASURE_RUNNING}'))
    ORDER BY tenant.last_claimed_at NULLS FIRST, queued.created_at, queued.id
    FOR UPDATE OF queued SKIP LOCKED
    LIMIT 1
), served AS (
    UPDATE tenants SET last_claimed_at = clock_timestamp()
    WHERE id = (SELECT tenant_id FROM picked)
)
"""

_LEASE_EXPIRED = (
    "lease_started_at < now() - make_interval(secs => %(visibility_timeout_s)s)"
)

_EXPIRED = f"""
SELECT id, attempts
FROM capture_queue
WHERE status = '{schema.PROCESSING}' AND {_LEASE_EXPIRED}
ORDER BY processing_started_at, id
FOR UPDATE SKIP LOCKED
LIMIT %(batch)s
"""

_FAIL_EXPIRED = f"""
UPDATE capture_queue
SET status = '{schema.FAILED}',
    lease_started_at = NULL,
    finished_at = now(),
    error_category = 'lease_expired',
    error = 'processing lease expired'
WHERE id = ANY(%s) AND status = '{schema.PROCESSING}'
"""

_REQUEUE_EXPIRED = f"""
UPDATE capture_queue
SET status = '{schema.QUEUED}',
    lease_started_at = NULL,
    next_attempt_at = now(),
    error_category = 'lease_expired',
    error = 'processing lease expired; queued for commit reconciliation'
WHERE id = ANY(%s) AND status = '{schema.PROCESSING}'
"""


def _iso(value) -> str | None:
    return value.isoformat() if isinstance(value, (dt.datetime, dt.date)) else None


def _shape(row) -> dict:
    item = dict(zip(_ITEM_COLUMNS, row, strict=True))
    item["id"] = str(item["id"])
    item["tenant_id"] = str(item["tenant_id"])
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
        "budget_deadline_at",
        "lease_started_at",
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
    layer_decision: schema.LayerDecision | None = None,
) -> dict:
    params = {
        "id": item_id,
        "operation": operation,
        "idempotency_key": idempotency_key,
        "submitted_by": actor.subject,
        "actor": Jsonb(actor.model_dump(mode="json")),
        "acl": None if acl is None else list(acl),
        "layer_decision": None if layer_decision is None else layer_decision.decision,
        "layer_probabilities": (
            None if layer_decision is None or layer_decision.probabilities is None
            else Jsonb(layer_decision.probabilities)
        ),
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
    wake.writer()
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


def enqueue_capture(
    conn,
    envelope: schema.CaptureEnvelope,
    layer_decision: schema.LayerDecision | None = None,
) -> dict:
    return _enqueue(
        conn,
        operation=schema.CAPTURE,
        item_id=envelope.capture_id,
        idempotency_key=envelope.idempotency_key,
        actor=envelope.actor,
        acl=envelope.audience,
        request=envelope.as_json(),
        layer_decision=layer_decision,
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


def layer_closed(conn, layer: str) -> bool:
    """Whether an erasure of `layer` is open; captures to it are refused until it ends."""
    with conn.cursor() as cursor:
        cursor.execute("SELECT 1 FROM layer_erasures WHERE layer = %s AND status = ANY(%s) LIMIT 1",
                       (layer, list(schema.ERASURE_OPEN)))
        return cursor.fetchone() is not None


def record_extraction(conn, item_id: str | uuid.UUID, extraction: dict) -> None:
    """Keep what extraction stored before filing, so the references of a capture that never lands
    are still known."""
    with conn.cursor() as cursor:
        cursor.execute("UPDATE capture_queue SET extraction = %s WHERE id = %s",
                       (Jsonb(extraction), item_id))


def claim_next(
    conn,
    *,
    operation_budget_s: int,
) -> dict | None:
    with conn.cursor() as cursor:
        cursor.execute(_PICK + _CLAIM, {"operation_budget_s": max(1, int(operation_budget_s))})
        row = cursor.fetchone()
    return _shape(row) if row else None


def release_expired(
    conn,
    *,
    visibility_timeout_s: int,
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
        released = [item_id for item_id, attempts in rows if attempts <= DEFAULT_MAX_ATTEMPTS]
        failed = [item_id for item_id, attempts in rows if attempts > DEFAULT_MAX_ATTEMPTS]
        if released:
            cursor.execute(_REQUEUE_EXPIRED, (released,))
        if failed:
            cursor.execute(_FAIL_EXPIRED, (failed,))
    return {"released": len(released), "failed": len(failed)}


def holds_lease(
    conn,
    item_id: str | uuid.UUID,
    *,
    expected_attempts: int,
    expected_lease_started_at: str,
) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT EXISTS(
                SELECT 1 FROM capture_queue
                WHERE id = %s
                  AND status = '{schema.PROCESSING}'
                  AND attempts = %s
                  AND lease_started_at = %s::timestamptz
            )
            """,
            (item_id, int(expected_attempts), expected_lease_started_at),
        )
        row = cursor.fetchone()
    return bool(row and row[0])


def finish_landed(
    conn,
    item_id: str | uuid.UUID,
    *,
    expected_attempts: int,
    expected_lease_started_at: str,
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
                lease_started_at = NULL,
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
              AND lease_started_at = %s::timestamptz
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
                expected_lease_started_at,
            ),
        )
        row = cursor.fetchone()
    if row is None:
        _raise_lost_lease(conn, item_id)
    return _shape(row)


def fail_or_retry(
    conn,
    item_id: str | uuid.UUID,
    *,
    expected_attempts: int,
    expected_lease_started_at: str,
    category: str,
    error: str,
    retryable: bool,
    fresh_budget: bool = False,
) -> dict:
    safe_category = _safe(category, 100) or "processing_failed"
    safe_error = _safe(error, MAX_SAFE_ERROR_CHARS) or "processing failed"
    retry = retryable and expected_attempts < DEFAULT_MAX_ATTEMPTS
    status = schema.QUEUED if retry else schema.FAILED
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE capture_queue
            SET status = %s,
                lease_started_at = NULL,
                next_attempt_at = CASE
                    WHEN %s THEN now() + make_interval(secs => %s)
                    ELSE next_attempt_at
                END,
                finished_at = CASE WHEN %s THEN NULL ELSE now() END,
                budget_deadline_at = CASE WHEN %s THEN NULL ELSE budget_deadline_at END,
                error_category = %s,
                error = %s
            WHERE id = %s
              AND status = '{schema.PROCESSING}'
              AND attempts = %s
              AND lease_started_at = %s::timestamptz
            RETURNING {_ITEM_SQL}
            """,
            (
                status,
                retry,
                RETRY_DELAY_S,
                retry,
                retry and fresh_budget,
                safe_category,
                safe_error,
                item_id,
                int(expected_attempts),
                expected_lease_started_at,
            ),
        )
        row = cursor.fetchone()
    if row is None:
        _raise_lost_lease(conn, item_id)
    return _shape(row)


def release_claim(conn, item_id: str | uuid.UUID, *, expected_lease_started_at) -> bool:
    """Hand a claim the writer stopped during back to the queue, with its attempt and budget
    restored. The next claim recovers a commit that already landed before filing anew."""
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE capture_queue
            SET status = '{schema.QUEUED}',
                attempts = GREATEST(attempts - 1, 0),
                next_attempt_at = now(),
                processing_started_at = NULL,
                budget_deadline_at = NULL,
                lease_started_at = NULL
            WHERE id = %s AND status = '{schema.PROCESSING}' AND lease_started_at = %s
            """,
            (item_id, expected_lease_started_at),
        )
        return cursor.rowcount == 1


def retry_failed(conn, item_id: str | uuid.UUID) -> dict:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE capture_queue
            SET status = '{schema.QUEUED}',
                attempts = 0,
                next_attempt_at = now(),
                processing_started_at = NULL,
                budget_deadline_at = NULL,
                lease_started_at = NULL,
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
    wake.writer()
    return _shape(row)


def _raise_lost_lease(conn, item_id) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT status, attempts FROM capture_queue WHERE id = %s",
            (item_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise QueueStateError(f"queue item {item_id} does not exist")
    status, _attempts = row
    if status == schema.PROCESSING:
        raise QueueStateError("processing lease was redelivered")
    raise QueueStateError(f"queue item is {status!r}, not owned by this processor")


def _safe(value: str, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def query_submissions(
    conn,
    *,
    submitter: str | None = None,
    statuses: list[str] | None = None,
    layer_decisions: list[str] | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> list[dict]:
    unknown = sorted(set(statuses or ()) - set(schema.STATUSES))
    if unknown:
        raise ValueError(f"unknown status: {', '.join(unknown)}")
    unknown = sorted(set(layer_decisions or ()) - set(schema.LAYER_DECISIONS))
    if unknown:
        raise ValueError(f"unknown layer decision: {', '.join(unknown)}")
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT {_ITEM_SQL}
            FROM capture_queue
            WHERE (%s::text IS NULL OR submitted_by = %s)
              AND (%s::text[] IS NULL OR status = ANY(%s))
              AND (%s::text[] IS NULL OR layer_decision = ANY(%s))
            ORDER BY created_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            (
                submitter,
                submitter,
                statuses,
                statuses,
                layer_decisions,
                layer_decisions,
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
