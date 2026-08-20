"""The capture queue: insert, claim, release, terminal transitions, and the read surfaces.

Claims are exactly-once (`FOR UPDATE SKIP LOCKED` inside one UPDATE-of-SELECT); `attempts` —
incremented on claim only, so it counts deliveries, never failures — is the fencing token, and
`finish()` updates nothing unless the row is still `claimed` on that same delivery, so a stalled
worker cannot overwrite a redelivered item. The transitions out of a park (`record_reply`,
`dispose`) are guarded on the row's STATE instead: the caller holds no delivery to fence with.

Write order: validate → evidence blob → queue row, so a refusal writes neither and a row never
points at material the worker cannot read.
"""
from datetime import datetime

from psycopg.types.json import Jsonb

from stigmergy.capture import ops, schema
from stigmergy.capture.errors import QueueStateError

# How long a claim is held before the queue assumes the worker died — generous relative to a
# librarian run, so a slow-but-alive worker is never robbed of its item.
DEFAULT_VISIBILITY_TIMEOUT_S = 300
# Deliveries before an item is given up on.
DEFAULT_MAX_ATTEMPTS = 3
# Expired claims one sweep repairs. Bounded: the sweep runs on the claim hot path holding a row
# lock per item, so a backlog drains over several claims, not one blocking transaction.
RECLAIM_BATCH = 100

# Bounds on one listing page and on the echoed excerpt, truncated IN POSTGRES so a 256 KB payload
# never crosses the wire; whether it is echoed at all is `_MATERIAL_WITHHELD`.
MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 20
EXCERPT_CHARS = 500

# What a claim hands the worker; `asked_at` and `reply` feed the librarian's routing.
_ITEM_COLUMNS = ("id", "kind", "payload", "blob_refs", "submitted_by", "hints", "status",
                 "attempts", "created_at", "claimed_at", "finished_at", "result_ref", "error",
                 "asked_at", "reply", "outcome")

_INSERT = """
INSERT INTO capture_queue (kind, payload, blob_refs, submitted_by, hints, status)
VALUES (%s, %s, %s, %s, %s, %s)
RETURNING id, status, created_at
"""

# The inner SELECT locks the oldest claimable row, skipping ones a peer holds; the UPDATE flips it
# in the same statement, so there is no window for a second claimer.
_CLAIM = f"""
UPDATE capture_queue SET status = '{schema.CLAIMED}', claimed_at = now(), attempts = attempts + 1
WHERE id = (
    SELECT id FROM capture_queue
    WHERE status = '{schema.QUEUED}'
    ORDER BY created_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING {', '.join(_ITEM_COLUMNS)}
"""

# THE definition of "this claim has outlived its lease": `release_expired` acts on it and
# `query_in_flight` reports it, so the two can never disagree.
_LEASE_EXPIRED = "claimed_at < now() - make_interval(secs => %(visibility)s)"

_SELECT_EXPIRED = f"""
SELECT id, attempts FROM capture_queue
WHERE status = '{schema.CLAIMED}' AND {_LEASE_EXPIRED}
ORDER BY claimed_at
FOR UPDATE SKIP LOCKED
LIMIT %(batch)s
"""

# The age is computed IN POSTGRES: `claimed_at` came from the database's `now()`, and subtracting a
# local clock would fold skew into the number an operator uses to decide a worker is dead.
_SELECT_IN_FLIGHT = f"""
SELECT id, kind, submitted_by, attempts, claimed_at,
       extract(epoch from (now() - claimed_at)) * 1000 AS claimed_age_ms,
       ({_LEASE_EXPIRED}) AS lease_expired
FROM capture_queue
WHERE status = '{schema.CLAIMED}'
ORDER BY claimed_at, id
"""

# capture -> filed, from the trace columns alone. A declared duplication of
# `get_submission_trace`'s `total_latency_ms`: a change to what "capture -> filed" means changes
# both. Scoped to `filed`; a `rejected` row's latency measures a refusal.
_SELECT_FILED_LATENCIES = f"""
SELECT extract(epoch from (finished_at - created_at)) * 1000 AS latency_ms
FROM capture_queue
WHERE status = '{schema.FILED}' AND created_at IS NOT NULL AND finished_at IS NOT NULL
ORDER BY finished_at DESC, id DESC
LIMIT %(limit)s
"""

# How many recent filings a latency answer covers: a percentile over all history describes a
# system that no longer exists.
LATENCY_WINDOW = 500

_REQUEUE = f"""
UPDATE capture_queue SET status = '{schema.QUEUED}', claimed_at = NULL
WHERE id = ANY(%s) AND status = '{schema.CLAIMED}'
RETURNING id
"""

# ── the row's human history, appended in ONE place ────────────────────────────────────────────
# Built in SQL so `at` is the DATABASE's clock, sortable against `created_at`/`claimed_at`, and
# bounded in the same statement that appends. The OLDEST event is dropped when full: refusing the
# newest would make the trace lie about the very action being taken.
_TRACE_EVENT = """
jsonb_build_array(jsonb_build_object(
    'at', to_jsonb(now()), 'event', %(event)s::text,
    'actor', %(actor)s::text, 'note', %(note)s::text))
"""
_TRACE_APPEND = f"""
    (CASE WHEN jsonb_array_length(COALESCE(trace, '[]'::jsonb)) >= {schema.MAX_TRACE_EVENTS}
          THEN COALESCE(trace, '[]'::jsonb) #- '{{0}}'
          ELSE COALESCE(trace, '[]'::jsonb) END) || {_TRACE_EVENT}
"""

# The terminal/parked transition, fenced in the WHERE clause: still `claimed`, still this delivery.
_FINISH = f"""
UPDATE capture_queue
SET status = %(status)s,
    result_ref = %(result_ref)s,
    error = %(error)s,
    report = COALESCE(%(report)s::jsonb, report),
    outcome = CASE WHEN %(clear_outcome)s THEN NULL
                   ELSE COALESCE(%(outcome)s::jsonb, outcome) END,
    finished_at = CASE WHEN %(terminal)s THEN now() ELSE NULL END,
    claimed_at = CASE WHEN %(terminal)s THEN claimed_at ELSE NULL END,
    parked_at = CASE WHEN %(parking)s THEN now() ELSE parked_at END,
    asked_at = CASE WHEN %(asking)s THEN COALESCE(asked_at, now()) ELSE asked_at END,
    trace = CASE WHEN %(asking)s THEN {_TRACE_APPEND} ELSE trace END
WHERE id = %(id)s AND status = '{schema.CLAIMED}' AND attempts = %(attempts)s
RETURNING id
"""

# Read back on the FAILURE path only, so the error can name which refusal; also `holds_lease`'s.
_FINISH_DIAGNOSE = "SELECT status, attempts FROM capture_queue WHERE id = %s"

# THE definition of "this row's material may not be read back", evaluated IN POSTGRES so a withheld
# value never crosses the wire. Two clauses: a secret/PII `reason_code`, and a `rejected` row with
# NO code at all — fail-closed, because under-withholding hands a steward somebody else's live
# credential. `hints` loses `client`/`declared_frontmatter` and keeps `flagged` (field names only);
# `reply` is withheld too (a capture can be asked, answered, and only then refused); `trace` stays,
# its notes being code-built or steward-authored.
_WITHHELD_REASON_LITERALS = schema.sql_literals(schema.WITHHELD_REASONS)
_REASON_CODE_SQL = f"report ->> '{schema.REASON_CODE_KEY}'"
# `schema._reason_flagged` is the Python mirror of exactly this expression — change both.
# `COALESCE(..., false)` keeps it two-valued: a NULL `report` would otherwise yield SQL NULL, a
# landmine for any future consumer that negates the expression.
_REASON_FLAGGED_SQL = (
    f"COALESCE(({_REASON_CODE_SQL} IN ({_WITHHELD_REASON_LITERALS})"
    f" OR (status = '{schema.REJECTED}' AND {_REASON_CODE_SQL} IS NULL)), false)"
)

# Widened to also withhold while the gate has not run (`queued`/`claimed`) or never will
# (`failed`); `schema.withheld_reason` picks the sentence this boolean cannot distinguish.
_GATE_NOT_YET_RUN_LITERALS = schema.sql_literals(schema.GATE_NOT_YET_RUN_STATUSES)
_MATERIAL_WITHHELD = (
    f"({_REASON_FLAGGED_SQL} OR status IN ({_GATE_NOT_YET_RUN_LITERALS}, '{schema.FAILED}'))"
)

# The parked pair as SQL literals, used by the age expression below and by the disposition guard
# further down: "is this row parked" must be one set, or a disposition could act on a state the
# age column does not consider parked.
_PARKED_LITERALS = schema.sql_literals(schema.PARKED_STATUSES)

# How long a human has been waited on, computed IN POSTGRES (same clock-skew reason as
# `claimed_age_ms`); NULL on a row that is not parked at all.
_PARKED_AGE_MS = f"""
CASE WHEN status IN ({_PARKED_LITERALS})
     THEN extract(epoch from (now() - COALESCE(parked_at, created_at))) * 1000 END
"""

# ── what the listing and the single-row trace both select ─────────────────────────────────────
# The two read paths (`_LIST_SELECT` and `get_submission_trace`) answer the same questions about a
# row, and a column or an expression added to one and not the other is how `stigmergy-queue list`
# and `show` start describing the same submission differently. Written once here and interpolated
# into both; the column list carries the listing's own wrap, which the trace query flattens.
_SHARED_COLUMNS = """id, kind, submitted_by, status, attempts, created_at, claimed_at, finished_at,
       result_ref, error, report, blob_refs, asked_at, trace"""
_SHARED_COLUMNS_ONE_LINE = " ".join(_SHARED_COLUMNS.split())

# `reply` obeys `_MATERIAL_WITHHELD` on BOTH paths: a withheld reply that leaked through whichever
# surface forgot the CASE would be the whole point of the rule, missed.
_WITHHELD_REPLY = f"CASE WHEN {_MATERIAL_WITHHELD} THEN NULL ELSE reply END AS reply"
_PARKED_AGE = f"{_PARKED_AGE_MS} AS parked_age_ms"
_PAYLOAD_PURGED = "(payload IS NULL) AS payload_purged"

_LIST_SELECT = f"""
SELECT {_SHARED_COLUMNS},
       {_PARKED_AGE},
       {_PAYLOAD_PURGED},
       {_MATERIAL_WITHHELD} AS material_withheld,
       CASE WHEN {_MATERIAL_WITHHELD} THEN NULL
            ELSE left(payload ->> 'text', %(excerpt)s) END AS excerpt,
       {_WITHHELD_REPLY},
       payload ->> 'sha256' AS content_sha256,
       (payload ->> 'bytes')::bigint AS bytes,
       CASE WHEN {_MATERIAL_WITHHELD} THEN hints - ARRAY['client', 'declared_frontmatter']
            ELSE hints END AS hints
FROM capture_queue
WHERE (%(submitter)s::text IS NULL OR submitted_by = %(submitter)s)
  AND (%(statuses)s::text[] IS NULL OR status = ANY(%(statuses)s))
ORDER BY created_at DESC, id DESC
LIMIT %(limit)s OFFSET %(offset)s
"""


def _iso(value) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _item(row) -> dict:
    item = dict(zip(_ITEM_COLUMNS, row, strict=True))
    for field in ("created_at", "claimed_at", "finished_at", "asked_at"):
        item[field] = _iso(item[field])
    item["blob_refs"] = list(item["blob_refs"] or [])
    item["hints"] = item["hints"] or {}
    item["reply"] = item["reply"] or ""
    return item


# ── write path ────────────────────────────────────────────────────────────────────────────────
def submit(conn, evidence, *, kind: str, material: str, hints: dict | None,
           submitted_by: str, extra_blob_refs: tuple = ()) -> dict:
    """Archive the material and enqueue it, in that order — validate → blob → row.

    `submitted_by` is the caller's RESOLVED identity, supplied by the service layer; this function
    has no way to learn an identity from the input. `extra_blob_refs` (operator CLIs only) append
    AFTER the material's own blob, so `blob_refs[0]` stays the text every reader assumes.
    """
    submission = schema.prepare_submission(kind, material, hints)
    key = evidence.put(submission.material_bytes)
    blob_refs = [key, *extra_blob_refs]
    with conn.cursor() as cur:
        cur.execute(_INSERT, (submission.kind, Jsonb(submission.payload), blob_refs, submitted_by,
                              Jsonb(submission.hints), schema.QUEUED))
        submission_id, status, created_at = cur.fetchone()
    return {
        "id": submission_id,
        "status": status,
        "submitted_by": submitted_by,
        "created_at": _iso(created_at),
        "blob_refs": blob_refs,
        "content_sha256": submission.digest,
        "bytes": submission.size,
        "flagged_hints": submission.hints["flagged"],
    }


def claim_next(conn, *, visibility_timeout_s: int = DEFAULT_VISIBILITY_TIMEOUT_S,
               max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> dict | None:
    """Claim the oldest queued item, exactly once, or None when the queue is empty. Sweeps expired
    claims first, so a dead worker's item is recovered by the NEXT claimer with no separate cron
    — the recovery path lives on the hot path precisely so it cannot rot."""
    release_expired(conn, visibility_timeout_s=visibility_timeout_s, max_attempts=max_attempts)
    with conn.cursor() as cur:
        cur.execute(_CLAIM)
        row = cur.fetchone()
    return _item(row) if row else None


def release_expired(conn, *, visibility_timeout_s: int,
                    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                    batch: int = RECLAIM_BATCH) -> dict:
    """Return timed-out claims to the queue; give up on the ones that have burned their attempts.

    `visibility_timeout_s` is REQUIRED, unlike `claim_next`'s: a sweeper states how dead somebody
    else's worker must be, and a default here once requeued captures out from under live workers.
    The queue transitions happen in ONE transaction; the bookkeeping is written outside it, so its
    failure can never abort the recovery it records.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_SELECT_EXPIRED,
                    {"visibility": visibility_timeout_s, "batch": max(1, int(batch))})
        expired = cur.fetchall()
        exhausted = [(sid, attempts) for sid, attempts in expired if attempts >= max_attempts]
        recoverable = [sid for sid, attempts in expired if attempts < max_attempts]

        for submission_id, attempts in exhausted:
            # The fence always matches: these rows are locked FOR UPDATE in this transaction.
            finish(conn, submission_id, status=schema.FAILED, expected_attempts=attempts,
                   error=f"claim expired after {attempts} attempt(s); the worker never finished")

        released = []
        if recoverable:
            cur.execute(_REQUEUE, (recoverable,))
            released = [row[0] for row in cur.fetchall()]

    for submission_id, attempts in exhausted:
        ops.record_ingest_error(conn, source_doc_id=str(submission_id), stage="claim",
                                error="claim expired (visibility timeout)", attempts=attempts)
    result = {"released": len(released), "failed": len(exhausted)}
    if released or exhausted:
        ops.record_job_run(conn, "capture-reclaim", stats={
            **result, "visibility_timeout_s": visibility_timeout_s,
            "released_ids": released, "failed_ids": [sid for sid, _ in exhausted]})
    return result


def finish(conn, submission_id: int, *, status: str, expected_attempts: int,
           result_ref: str = "", error: str = "", report: dict | None = None,
           outcome: dict | None = None) -> dict:
    """Move a row THIS caller is holding out of flight — the only transition helper.

    `expected_attempts` is the `attempts` value `claim_next` returned for this delivery, and is
    REQUIRED: an optional fence is a fence nobody passes. The write applies only while the row is
    still `claimed` on that same value; otherwise `QueueStateError` names which reason.

    The terminal three stamp `finished_at` (what retention counts from); the parked pair leave it
    NULL, so a row waiting for a human is not purged out from under its question. `report` and
    `outcome` are additive on a park (`COALESCE`), so `release_expired` never blanks what a
    previous delivery wrote; `outcome` is CLEARED on every terminal status. `asked_at` is stamped
    on the FIRST `needs_input` and never again, which is the one-ask budget.
    """
    if status not in schema.FINISHED_STATUSES:
        raise QueueStateError(
            f"cannot finish into {status!r} (allowed: {', '.join(sorted(schema.FINISHED_STATUSES))})")
    terminal = status in schema.TERMINAL_STATUSES
    asking = status == schema.NEEDS_INPUT
    with conn.cursor() as cur:
        # `is None`, not falsy: an EMPTY dict is a report; only `None` means "do not blank it".
        cur.execute(_FINISH, {
            "status": status, "result_ref": result_ref, "error": error,
            "report": None if report is None else Jsonb(report),
            "outcome": None if outcome is None else Jsonb(outcome),
            "clear_outcome": terminal,
            "terminal": terminal, "parking": status in schema.PARKED_STATUSES, "asking": asking,
            "id": submission_id, "attempts": int(expected_attempts),
            "event": schema.EVENT_ASKED, "actor": schema.ACTOR_LIBRARIAN,
            "note": (error or "")[:schema.MAX_TRACE_NOTE_CHARS] if asking else "",
        })
        if cur.fetchone() is not None:
            return {"id": submission_id, "status": status, "result_ref": result_ref,
                    "attempts": int(expected_attempts)}
        cur.execute(_FINISH_DIAGNOSE, (submission_id,))
        current = cur.fetchone()
    raise QueueStateError(_lost_lease_reason(submission_id, status, expected_attempts, current))


def holds_lease(conn, submission_id: int, *, expected_attempts: int) -> bool:
    """Is this delivery still the live one — the row `claimed`, on this same `attempts` value?

    The fence's question asked EARLY, immediately before an irreversible step outside the database
    (a commit and push), so an expired lease does not file a capture twice. Not a replacement for
    the fence: it only narrows the window from a whole agent run to one push.
    """
    with conn.cursor() as cur:
        cur.execute(_FINISH_DIAGNOSE, (submission_id,))
        row = cur.fetchone()
    if row is None:
        return False
    status, attempts = row
    return status == schema.CLAIMED and int(attempts) == int(expected_attempts)


def _lost_lease_reason(submission_id: int, status: str, expected_attempts: int, current) -> str:
    """Why a `finish` did not apply — id, state and attempt counts only; no material, no
    identity."""
    if current is None:
        return f"submission {submission_id} does not exist — it cannot be moved to {status!r}"
    actual_status, actual_attempts = current
    if actual_status == schema.CLAIMED and actual_attempts != expected_attempts:
        return (f"submission {submission_id} was redelivered (this worker held delivery "
                f"{expected_attempts}, it is now on {actual_attempts}) — its lease is gone and "
                f"another worker owns it; do not retry, discard this run's work")
    return (f"submission {submission_id} is {actual_status!r}, not claimed by this worker — "
            f"it cannot be moved to {status!r}")


# ── the human loop's two transitions: neither is a lease, and neither fakes one ───────────────
# These move a row NOBODY holds, so the guard is the STATE in the WHERE clause and a disposition
# racing a live claim fails loudly. `attempts` appears in neither statement: bumping it would burn
# a delivery no worker got, resetting it would hand a stale worker back a fence it had lost.
_DISPOSE = f"""
UPDATE capture_queue
SET status = %(status)s,
    result_ref = CASE WHEN %(result_ref)s::text = '' THEN result_ref ELSE %(result_ref)s END,
    error = %(error)s,
    report = COALESCE(%(report)s::jsonb, report),
    claimed_at = NULL,
    parked_at = CASE WHEN %(terminal)s THEN parked_at ELSE NULL END,
    finished_at = CASE WHEN %(terminal)s THEN now() ELSE NULL END,
    -- A REQUEUE deliberately leaves `outcome` alone — that is the whole mechanism (the steward
    -- minted the entity, the distillation is still good, and the next pass re-files it).
    -- A terminal disposition (`resolved`/`rejected`) clears it, matching `finish`: once the row is
    -- closed nothing can ever reuse it, and the full drafted text of every page is not something
    -- to keep beside a closed row.
    outcome = CASE WHEN %(terminal)s THEN NULL ELSE outcome END,
    trace = {_TRACE_APPEND}
WHERE id = %(id)s AND status IN ({_PARKED_LITERALS})
RETURNING id, status, attempts
"""

_RECORD_REPLY = f"""
UPDATE capture_queue
SET status = '{schema.QUEUED}',
    reply = %(reply)s,
    error = '',
    claimed_at = NULL,
    parked_at = NULL,
    trace = {_TRACE_APPEND}
WHERE id = %(id)s AND status = '{schema.NEEDS_INPUT}'
RETURNING id, attempts
"""

_DISPOSE_DIAGNOSE = "SELECT status FROM capture_queue WHERE id = %s"


def dispose(conn, submission_id: int, *, status: str, actor: str, event: str, action: str = "",
            note: str = "", error: str = "", result_ref: str = "",
            report: dict | None = None) -> dict:
    """THE state-guarded transition out of a park — the one base every disposition rides. Callers
    go through `capture.dispositions`, which owns the wording; this function owns only that the
    move is legal, decided in SQL so a caller cannot decide it differently."""
    if status not in (schema.QUEUED, schema.RESOLVED, schema.REJECTED):
        raise QueueStateError(
            f"cannot dispose into {status!r} (a disposition returns a parked row to "
            f"{schema.QUEUED!r} or closes it as {schema.RESOLVED!r} or {schema.REJECTED!r})")
    if not actor:
        # Attribution, not authorization: an unattributed disposition records no actor.
        raise QueueStateError("a disposition needs an actor — `--by <who>` is who is answering for it")
    terminal = status in schema.TERMINAL_STATUSES
    with conn.cursor() as cur:
        cur.execute(_DISPOSE, {
            "status": status, "error": error, "result_ref": result_ref,
            "report": None if report is None else Jsonb(report), "terminal": terminal,
            "id": submission_id, "event": event, "actor": actor,
            "note": (note or "")[:schema.MAX_TRACE_NOTE_CHARS],
        })
        row = cur.fetchone()
        if row is not None:
            return {"id": row[0], "status": row[1], "attempts": row[2]}
        cur.execute(_DISPOSE_DIAGNOSE, (submission_id,))
        current = cur.fetchone()
    raise QueueStateError(_not_parked_reason(submission_id, action or event, current))


def record_reply(conn, submission_id: int, *, answer: str, actor: str, note: str = "") -> dict:
    """Record a submitter's answer to this row's one question and return it to the queue.

    State-guarded on `needs_input` alone, so it is true under a race and not merely in the caller's
    earlier read. `actor` is the RESOLVED identity that replied. Does not touch `attempts` and
    leaves the row claimable, so the next pass is an ordinary delivery.
    """
    with conn.cursor() as cur:
        cur.execute(_RECORD_REPLY, {
            "id": submission_id, "reply": answer, "event": schema.EVENT_REPLIED,
            "actor": actor, "note": (note or "")[:schema.MAX_TRACE_NOTE_CHARS],
        })
        row = cur.fetchone()
        if row is not None:
            return {"id": row[0], "status": schema.QUEUED, "attempts": row[1]}
        cur.execute(_DISPOSE_DIAGNOSE, (submission_id,))
        current = cur.fetchone()
    raise QueueStateError(_not_parked_reason(submission_id, schema.REPLY_TOOL, current))


def current_status(conn, submission_id: int) -> str | None:
    """This row's status, or None — how the reply path tells an AUTHORIZED caller why it
    bounced."""
    with conn.cursor() as cur:
        cur.execute(_DISPOSE_DIAGNOSE, (submission_id,))
        row = cur.fetchone()
    return row[0] if row else None


def _not_parked_reason(submission_id: int, action: str, current) -> str:
    """Why a disposition or a reply did not apply — id and state only. The three cases stay
    distinct: they have three different operator next actions."""
    parked = " or ".join(repr(s) for s in sorted(schema.PARKED_STATUSES))
    if current is None:
        return f"submission {submission_id} does not exist — there is nothing for `{action}` to act on"
    status = current[0]
    if status == schema.CLAIMED:
        return (f"submission {submission_id} is currently claimed — a worker may be mid-item, and "
                f"`{action}` must never race a live claim. Wait for it to finish, or check "
                f"`stigmergy-queue show {submission_id}` for its state")
    return (f"submission {submission_id} is {status!r} — `{action}` acts only on a PARKED row "
            f"({parked}), never on one a worker holds or a terminal state has already closed")


# ── read path: one shared base, two semantic entry points ─────────────────────────────────────
def query_submissions(conn, *, submitter: str | None = None, statuses: list[str] | None = None,
                      limit: int = DEFAULT_LIST_LIMIT, offset: int = 0,
                      excerpt_chars: int = EXCERPT_CHARS) -> list[dict]:
    """The ONE query behind every submissions listing: scope, status filter, ordering, paging.

    `submitter=None` is the MANAGEMENT scope; callers go through `list_own_submissions` or
    `list_all_submissions`, so the scoping decision is made once, by name. A purged row and a
    withheld row return the same shape — history stays readable, the material does not.
    """
    for status in statuses or ():
        if status not in schema.STATUSES:
            raise ValueError(f"unknown status {status!r} (allowed: {', '.join(schema.STATUSES)})")
    params = {
        "submitter": submitter,
        "statuses": list(statuses) if statuses else None,
        "limit": max(1, min(int(limit), MAX_LIST_LIMIT)),
        "offset": max(0, int(offset)),
        "excerpt": max(0, int(excerpt_chars)),
    }
    with conn.cursor() as cur:
        cur.execute(_LIST_SELECT, params)
        columns = [c.name for c in cur.description]
        rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
    return [_shape_listed(row) for row in rows]


def list_own_submissions(conn, submitter: str, **kwargs) -> list[dict]:
    """`submitter` is not optional: an empty identity would widen this to everybody's rows."""
    if not submitter:
        raise ValueError("submitter is required to list a caller's own submissions")
    return query_submissions(conn, submitter=submitter, **kwargs)


def list_all_submissions(conn, **kwargs) -> list[dict]:
    """Every identity's submissions — the caller establishes that the identity is unrestricted."""
    return query_submissions(conn, submitter=None, **kwargs)


def get_submission_trace(conn, submission_id: int, *, submitter: str | None = None) -> dict | None:
    """The per-submission trace and the latencies computable from its columns alone. `submitter`,
    when given, scopes the lookup: somebody else's row returns None, the same shape as a
    nonexistent id. `reply` obeys the same `_MATERIAL_WITHHELD` expression `_LIST_SELECT` uses."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_SHARED_COLUMNS_ONE_LINE},"
            f" {_WITHHELD_REPLY},"
            f" {_MATERIAL_WITHHELD} AS material_withheld,"
            f" {_PARKED_AGE},"
            f" {_PAYLOAD_PURGED}"
            " FROM capture_queue"
            " WHERE id = %s AND (%s::text IS NULL OR submitted_by = %s)",
            (submission_id, submitter, submitter))
        row = cur.fetchone()
        columns = [c.name for c in cur.description]
    if row is None:
        return None
    trace = dict(zip(columns, row, strict=True))
    created, claimed, finished = trace["created_at"], trace["claimed_at"], trace["finished_at"]
    trace["queue_wait_ms"] = _millis(created, claimed)
    trace["total_latency_ms"] = _millis(created, finished)
    trace["created_at"], trace["claimed_at"] = _iso(created), _iso(claimed)
    trace["finished_at"], trace["asked_at"] = _iso(finished), _iso(trace["asked_at"])
    trace["blob_refs"] = list(trace["blob_refs"] or [])
    trace["report"] = trace["report"] or {}     # same "nothing yet" shape as _shape_listed
    trace["reply"] = trace["reply"] or ""
    trace.pop("material_withheld")   # superseded by schema.withheld_reason below (same underlying fact)
    trace["withheld_reason"] = schema.withheld_reason(trace["status"], trace["report"])
    trace["events"] = list(trace.pop("trace") or [])
    trace["parked_age_ms"] = _float_or_none(trace["parked_age_ms"])
    trace["waiting_on"] = waiting_on(trace["status"], trace["submitted_by"])
    return trace


# Who a parked row is waiting on — written once, because two states parked on two different people
# is exactly the distinction a second implementation would blur.
WAITING_ON_STEWARD = "a steward"


def waiting_on(status: str, submitted_by: str) -> str:
    """`needs_input` waits on the SUBMITTER; `triage` waits on a steward; everything else waits
    on nobody and says so with an empty string."""
    if status == schema.NEEDS_INPUT:
        return submitted_by or WAITING_ON_STEWARD
    return WAITING_ON_STEWARD if status == schema.TRIAGE else ""


def _float_or_none(value):
    return None if value is None else float(value)


def query_in_flight(conn, *, visibility_timeout_s: int = DEFAULT_VISIBILITY_TIMEOUT_S) -> list[dict]:
    """Every `claimed` row, oldest first, with how long it has been held and whether the lease has
    expired. No payload and no excerpt: captured material has no part in "is a worker alive".
    `lease_expired` is `_LEASE_EXPIRED`, so "looks stale" and "will be requeued" are one fact.
    Pass the WORKER's own timeout — against the queue's default a longer-leased worker reads dead.
    """
    with conn.cursor() as cur:
        cur.execute(_SELECT_IN_FLIGHT, {"visibility": visibility_timeout_s})
        columns = [c.name for c in cur.description]
        rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
    for row in rows:
        row["claimed_at"] = _iso(row["claimed_at"])
        row["claimed_age_ms"] = _float_or_none(row["claimed_age_ms"])
        row["lease_expired"] = bool(row["lease_expired"])
    return rows


def filed_latencies_ms(conn, *, limit: int = LATENCY_WINDOW) -> list[float]:
    """capture -> filed durations in ms, newest first. Samples, not percentiles: the arithmetic
    lives in `capture.latency`, where a test drives it without a database."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_FILED_LATENCIES, {"limit": max(1, min(int(limit), 10_000))})
        return [float(row[0]) for row in cur.fetchall() if row[0] is not None]


# capture -> SEARCHABLE. `job_name` is a parameter because `capture` may not import
# `stigmergy.server`, which owns the webhook's job-name constant. `DISTINCT ON (cq.id)` is
# load-bearing: a redelivered webhook writes a SECOND `ok` row for the identical sha, which would
# fan the join out and double-count a capture.
_SELECT_SEARCHABLE_LATENCIES = f"""
SELECT latency_ms FROM (
    SELECT DISTINCT ON (cq.id)
           cq.id AS cq_id, jr.finished_at AS jr_finished_at,
           extract(epoch from (jr.finished_at - cq.created_at)) * 1000 AS latency_ms
    FROM capture_queue cq
    JOIN job_runs jr
      ON jr.job = %(job_name)s
     AND jr.status = 'ok'
     AND jr.stats ->> 'sha' = split_part(cq.result_ref, '@', 2)
    WHERE cq.status = '{schema.FILED}' AND cq.result_ref <> '' AND cq.created_at IS NOT NULL
    ORDER BY cq.id, jr.finished_at ASC
) one_sample_per_capture
ORDER BY jr_finished_at DESC, cq_id DESC
LIMIT %(limit)s
"""


def searchable_latencies_ms(conn, *, job_name: str, limit: int = LATENCY_WINDOW) -> list[float]:
    """capture -> SEARCHABLE durations in ms — a `filed` row's `result_ref` (`path@sha`) joined
    against the webhook's `job_runs` row for that sha. A `filed` row with no matching webhook row
    contributes NO sample: absent, not zero."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_SEARCHABLE_LATENCIES,
                   {"job_name": job_name, "limit": max(1, min(int(limit), 10_000))})
        return [float(row[0]) for row in cur.fetchall() if row[0] is not None]


def work_waiting(conn) -> bool:
    """Is anything QUEUED right now? An EXISTS, not a count — the caller is the worker's view
    sweep asking between entities whether to yield the loop back to the queue (issue #102), and
    the answer it needs is one bit."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT EXISTS (SELECT 1 FROM capture_queue WHERE status = '{schema.QUEUED}')")
        return bool(cur.fetchone()[0])


def counts_by_status(conn) -> dict[str, int]:
    """Queue depth per status; every declared status is present, zero included."""
    with conn.cursor() as cur:
        cur.execute("SELECT status, count(*) FROM capture_queue GROUP BY status")
        counted = dict(cur.fetchall())
    return {status: counted.get(status, 0) for status in schema.STATUSES}


def outcomes_by_day(conn, *, days: int) -> list[dict]:
    """`counts_by_status` over time: captures that ARRIVED in the last `days` days, bucketed by
    their UTC arrival day and their CURRENT status — `[{"day": "YYYY-MM-DD", "status", "count"}]`,
    ascending, with no row for an empty day. Beside `counts_by_status` because it is the same fact
    with a time axis, and a surface drawing "what happened to what arrived" must not carry its own
    query over this table."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (created_at AT TIME ZONE 'UTC')::date AS day, status, count(*)"
            " FROM capture_queue WHERE created_at >= now() - make_interval(days => %s)"
            " GROUP BY 1, 2 ORDER BY 1, 2", (max(1, int(days)),))
        rows = cur.fetchall()
    return [{"day": day.isoformat(), "status": status, "count": int(count)}
            for day, status, count in rows]


def _millis(start, end) -> float | None:
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    return (end - start).total_seconds() * 1000


def _shape_listed(row: dict) -> dict:
    """One listed row. Which FIELDS are suppressed is decided in SQL (`_MATERIAL_WITHHELD`), so
    `excerpt` and the client hints are already gone here; `schema.withheld_reason` supplies the
    sentence, so surfaces cannot invent their own wording."""
    hints = row.get("hints") or {}
    return {
        "id": row["id"],
        "kind": row["kind"],
        "submitted_by": row["submitted_by"],
        "status": row["status"],
        "attempts": row["attempts"],
        "created_at": _iso(row["created_at"]),
        "claimed_at": _iso(row["claimed_at"]),
        "finished_at": _iso(row["finished_at"]),
        "result_ref": row["result_ref"],
        "error": row["error"],
        # `{}` rather than None for an unprocessed row; the COLUMN itself stays NULL.
        "report": row["report"] or {},
        "blob_refs": list(row["blob_refs"] or []),
        "payload_purged": bool(row["payload_purged"]),
        "withheld_reason": schema.withheld_reason(row["status"], row["report"]),
        # a `triage` row carrying `asked_at` was asked and answered, unlike one never asked
        "asked_at": _iso(row["asked_at"]),
        "reply": row["reply"] or "",
        "events": list(row["trace"] or []),
        "parked_age_ms": _float_or_none(row["parked_age_ms"]),
        "waiting_on": waiting_on(row["status"], row["submitted_by"]),
        "excerpt": row["excerpt"] or "",
        "content_sha256": row["content_sha256"] or "",
        "bytes": row["bytes"],
        "hints": hints.get("client", {}),
        "flagged_hints": hints.get("flagged", []),
    }
