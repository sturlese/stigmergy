"""The capture queue: insert, claim, release, terminal transitions, and the read surfaces.

The seam between the capture surfaces and the librarian: this module fills the queue and hands
over the claim primitive, and the librarian drains it. Nothing here files a page, resolves a
wikilink, anchors an entity or touches git — those are the librarian's, and their absence from
this module is deliberate.

**The state machine.** `queued` on submit; `queued -> claimed` on claim; `claimed -> queued` when
a claim expires (a worker died mid-item); `claimed -> failed` when the attempts are exhausted.
`filed`, `rejected`, `needs_input` and `triage` are reachable through `finish()` but are never
produced by this module — they are the contract the librarian fills.

**The transitions OUT of a park** are the only ones no lease is involved in: a `needs_input` row
returns to `queued` when the submitter answers (`record_reply`), and either parked state moves to
`queued`, `resolved` or `rejected` when a steward disposes of it (`dispose`). Both
are guarded on the row's STATE rather than on the `attempts` fence, because the caller holds no
delivery to fence with — see the section comment above `_DISPOSE` for why that distinction is the
whole design rather than a convenience.

**Exactly-once claiming.** `FOR UPDATE SKIP LOCKED` inside the `UPDATE ... WHERE id = (SELECT
... LIMIT 1)` form: each claimer locks a different row and skips the ones its peers hold, so N
parallel claimers against M queued rows produce exactly M claims. This cannot be faked in a
double — the property lives in Postgres, not in this file.

**Leases are fenced by `attempts`.** Claiming exactly once is only half of it: the other half is
that a worker which STALLS cannot come back and finish an item that has since been given to
somebody else. `attempts` is monotonic per delivery, so the value handed out at claim time names
that delivery; `finish()` requires it and updates nothing without it. Without the fence, a
`status = 'claimed'` guard silently lets a stale worker overwrite the live one's row — improbable
with one worker, structural with N, and the librarian worker is built on this seam.

**`attempts` counts deliveries, not failures.** It is incremented when a row is CLAIMED, never
when it is released. A worker that dies before writing anything still burns an attempt, which is
what stops a poison item from being redelivered forever; a row released by the visibility timeout
keeps the attempts its deliveries earned. When they run out, the row goes `failed` and an
`ingest_errors` row records the stage and the count.

**Write order.** The evidence blob is written BEFORE the queue row. The failure modes are not
symmetric: an orphan blob is inert and content-addressed (the next identical submission reuses
it), while a row pointing at a blob that was never written is a submission whose material the
worker can never read. Validation runs before both, so a refusal writes neither.
"""
from datetime import datetime

from psycopg.types.json import Jsonb

from stigmergy.capture import ops, schema
from stigmergy.capture.errors import QueueStateError

# How long a claim is held before the queue assumes the worker died. Generous relative to a
# librarian run (~1 min), so a slow-but-alive worker is never robbed of its item; the CLI and the
# librarian worker both take it as a parameter rather than reading a global.
DEFAULT_VISIBILITY_TIMEOUT_S = 300
# Deliveries before an item is given up on. 3 is the usual "twice more, then stop" — enough to
# survive a deploy and a crash, few enough that a poison item surfaces the same day.
DEFAULT_MAX_ATTEMPTS = 3
# How many expired claims one sweep repairs. Bounded because the sweep runs on the claim hot path
# and holds a row lock per item: a backlog is drained over several claims rather than in one long
# transaction that blocks every other claimer.
RECLAIM_BATCH = 100

# The bound on one `brain_submissions`/`stigmergy-queue list` page, and on how much captured text
# is echoed per row. The excerpt is truncated IN POSTGRES (`left(...)`), so a 256 KB payload never
# crosses the wire to be thrown away in Python. Whether it is echoed AT ALL is a separate question,
# settled in the same statement — see `_MATERIAL_WITHHELD`.
MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 20
EXCERPT_CHARS = 500

# What a claim hands the worker. `asked_at` and `reply` are read by the librarian's ROUTING, not
# merely reported: `asked_at` is the one-ask budget (a capture that has
# already spent its question parks in `triage` instead of asking a second one), and `reply` is the
# submitter's answer, handed to the next agent pass as fenced data.
_ITEM_COLUMNS = ("id", "kind", "payload", "blob_refs", "submitted_by", "hints", "status",
                 "attempts", "created_at", "claimed_at", "finished_at", "result_ref", "error",
                 "asked_at", "reply", "outcome")

_INSERT = """
INSERT INTO capture_queue (kind, payload, blob_refs, submitted_by, hints, status)
VALUES (%s, %s, %s, %s, %s, %s)
RETURNING id, status, created_at
"""

# The claim. The inner SELECT takes the oldest claimable row and locks it, skipping rows a
# concurrent claimer already holds; the outer UPDATE flips it in the same statement, so there is
# no window between "chosen" and "claimed" for a second claimer to see it free.
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

# THE definition of "this claim has outlived its lease", written once.
#
# Two callers with opposite jobs: `release_expired` ACTS on it (the row goes back to the queue with
# an attempt burned) and `query_in_flight` REPORTS it (`stigmergy-librarian status` telling an operator
# whether the worker holding an item looks alive). A second copy of this predicate would let the
# status command call a claim healthy in the same second the sweep took it away — the exact class of
# disagreement `RECLAIM_NOW` was extracted to prevent between two tools printing one recovery.
_LEASE_EXPIRED = "claimed_at < now() - make_interval(secs => %(visibility)s)"

_SELECT_EXPIRED = f"""
SELECT id, attempts FROM capture_queue
WHERE status = '{schema.CLAIMED}' AND {_LEASE_EXPIRED}
ORDER BY claimed_at
FOR UPDATE SKIP LOCKED
LIMIT %(batch)s
"""

# What is in flight RIGHT NOW, with the two numbers that say whether anybody is still working on it.
# The age is computed IN POSTGRES, deliberately: `claimed_at` was written by `now()` on the database
# server, and subtracting a Python `datetime.now()` from it would fold this machine's clock skew
# into the one number an operator uses to decide whether a worker is dead.
_SELECT_IN_FLIGHT = f"""
SELECT id, kind, submitted_by, attempts, claimed_at,
       extract(epoch from (now() - claimed_at)) * 1000 AS claimed_age_ms,
       ({_LEASE_EXPIRED}) AS lease_expired
FROM capture_queue
WHERE status = '{schema.CLAIMED}'
ORDER BY claimed_at, id
"""

# capture -> filed, from the trace COLUMNS ALONE: no instrumentation, no second clock, nothing the
# librarian has to remember to write. Mirrors `get_submission_trace`'s `total_latency_ms`
# (`_millis(created_at, finished_at)`) for one row, in aggregate — a declared duplication of one
# definition, so a change to what "capture -> filed" means must change both. Scoped to `filed`
# because that is the measure that matters; a `rejected` row's latency is the latency of a
# refusal, which is a different question.
_SELECT_FILED_LATENCIES = f"""
SELECT extract(epoch from (finished_at - created_at)) * 1000 AS latency_ms
FROM capture_queue
WHERE status = '{schema.FILED}' AND created_at IS NOT NULL AND finished_at IS NOT NULL
ORDER BY finished_at DESC, id DESC
LIMIT %(limit)s
"""

# How many recent filings a latency answer is computed over. Bounded because the table only grows,
# and recent because a percentile over the whole of history describes a system that no longer
# exists — the number an operator wants is "how is it behaving now".
LATENCY_WINDOW = 500

_REQUEUE = f"""
UPDATE capture_queue SET status = '{schema.QUEUED}', claimed_at = NULL
WHERE id = ANY(%s) AND status = '{schema.CLAIMED}'
RETURNING id
"""

# The terminal/parked transition, guarded IN THE WHERE CLAUSE by TWO conditions, not one.
#
# `status = 'claimed'` alone is not a lease check, it is a state check — and it is not enough
# once a row can be redelivered. The hole it leaves: worker A claims row 5 (attempts=1), stalls
# past the visibility timeout, the sweep requeues it, worker B claims it (attempts=2), and A then
# calls finish() — the row IS `claimed`, so A's stale write lands and silently steals B's item.
# `attempts` is therefore the FENCING TOKEN: it is monotonic per delivery (incremented only by
# `_CLAIM`), so the value a worker was handed at claim time identifies ITS delivery and no other.
# A stale finish matches no row and raises, which is what makes single-writer serialization a
# property of the queue rather than of there happening to be one worker (spec resolved question 7:
# "N>1 safe if it is ever needed").
# ── the row's human history, appended in ONE place ────────────────────────────────────────────
# Built in SQL rather than in Python so `at` is the DATABASE's clock, exactly like `created_at` and
# `claimed_at`: an event stamped by whichever machine happened to run the CLI would sort against
# those two through one operator's clock skew.
#
# Bounded in the same statement that appends, so the ceiling cannot be forgotten by a caller. The
# OLDEST event is dropped (`#- '{0}'`) when the list is full: a steward acting on a parked row reads
# the recent history, and silently refusing to record the newest event would make the trace lie
# about the very action being taken.
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

# Read back on the FAILURE path only, so the error can say what actually happened — lease lost to
# a redelivery, or the row was never in flight — instead of one message covering both. Also the
# read `holds_lease` uses to answer the same question BEFORE an irreversible step.
_FINISH_DIAGNOSE = "SELECT status, attempts FROM capture_queue WHERE id = %s"

# THE definition of "this row's captured material may not be read back", written once and
# evaluated IN POSTGRES — so for a withheld row the value never crosses the wire at all, rather
# than being fetched and then dropped in Python one layer from the response.
#
# Two clauses, and the second is the one that covers the rows already in the table:
#
#  * `reason_code` (`schema.WITHHELD_REASONS`) is the structured half of a refusal. `secret` and
#    `pii` are the two whose whole point is that the value must not travel.
#  * A `rejected` row with NO `reason_code` was written before that field existed. Which refusal
#    put it there cannot be known without reading its prose, and a confidentiality property must
#    not rest on wording — so it is withheld. Fail-closed, deliberately asymmetric: over-withholding
#    costs a submitter an excerpt of material they wrote and still have, while under-withholding
#    hands a steward listing everybody's captures somebody else's live credential.
#
# The `hints` column loses both of its material-bearing halves and keeps the third: `client` is the
# submitter's own suggestions (`type`/`path`/`entity`/`title` — free text from the same submission,
# and scanned by nothing) and `declared_frontmatter` is the material's leading block verbatim.
# `flagged` stays: it is a list of FIELD NAMES and carries none of the material. Dropped in SQL
# rather than in `_shape_listed` — which emits neither today — so the rule stays "a withheld value
# does not leave Postgres" instead of "no current caller happens to read it".
#
# **`reply` is withheld too**, for the same reason and reached by a real path: a capture can be
# asked, answered, and only then refused for a secret the gates found in the drafted page. The
# answer is the submitter's own free text, scanned by nothing, so a row whose material may not be
# read back must not hand back the sentence they wrote about it either. The `trace` column stays:
# its notes are code-built or steward-authored, never captured material.
_WITHHELD_REASON_LITERALS = ", ".join(f"'{r}'" for r in sorted(schema.WITHHELD_REASONS))
_REASON_CODE_SQL = f"report ->> '{schema.REASON_CODE_KEY}'"
# The narrow reason: a secret/PII match, or a `rejected` row with no `reason_code` at all
# (fail-closed — see the comment block above). `schema._reason_flagged` is the Python mirror of
# exactly this expression, computed from the already-fetched `report` column rather than a second
# SQL round-trip, which is how `withheld_reason` picks a SENTENCE below.
#
# Wrapped in `COALESCE(..., false)` so this is a genuine two-valued boolean, matching
# `schema._reason_flagged`'s own definite `True`/`False` return exactly. Without it, a row whose
# `report` column is NULL entirely (a `queued`/`claimed` row nothing has ever finished) makes
# `report ->> '...' IN (...)` evaluate to SQL NULL rather than `false` — harmless everywhere this
# expression is actually CONSUMED today (`CASE WHEN`/`WHERE` treat NULL and `false` identically),
# but a real drift from the Python mirror under a literal equality check, and a landmine for any
# future consumer that negates this expression directly (`NOT NULL` is `NULL`, not `true`).
# `tests/capture/test_queue_withheld_reply_pg.py` walks the full status x reason_code matrix and
# is what catches that gap.
_REASON_FLAGGED_SQL = (
    f"COALESCE(({_REASON_CODE_SQL} IN ({_WITHHELD_REASON_LITERALS})"
    f" OR (status = '{schema.REJECTED}' AND {_REASON_CODE_SQL} IS NULL)), false)"
)

# WIDENED to also withhold while the gate has not run at all (`queued`/`claimed`) or ran too late
# to matter (`failed` — the accepted residual). This is the
# boolean that actually gates `excerpt`/`reply`/`hints` below — evaluated IN POSTGRES, same as
# before, so a withheld value never crosses the wire regardless of WHICH of the three reasons
# applies. `schema.withheld_reason(status, report)` is what picks the SENTENCE the widened boolean
# alone cannot distinguish (see its docstring for the three-way priority).
_GATE_NOT_YET_RUN_LITERALS = ", ".join(f"'{s}'" for s in sorted(schema.GATE_NOT_YET_RUN_STATUSES))
_MATERIAL_WITHHELD = (
    f"({_REASON_FLAGGED_SQL} OR status IN ({_GATE_NOT_YET_RUN_LITERALS}, '{schema.FAILED}'))"
)

# How long a human has been waited on, computed IN POSTGRES for the same reason `claimed_age_ms`
# is: `parked_at` was written by `now()` on the database server, and subtracting this machine's
# clock from it would fold an operator's skew into the one number a steward prioritizes on.
#
# `COALESCE(parked_at, created_at)` is the migration's honest half: rows parked before the column
# existed have no `parked_at`, and their submission time is the best true answer rather than a
# NULL that renders as "unknown". NULL on a row that is not parked at all, because "how long has
# this been parked" has no answer there.
_PARKED_AGE_MS = f"""
CASE WHEN status IN ({', '.join(f"'{s}'" for s in sorted(schema.PARKED_STATUSES))})
     THEN extract(epoch from (now() - COALESCE(parked_at, created_at))) * 1000 END
"""

_LIST_SELECT = f"""
SELECT id, kind, submitted_by, status, attempts, created_at, claimed_at, finished_at,
       result_ref, error, report, blob_refs, asked_at, trace,
       {_PARKED_AGE_MS} AS parked_age_ms,
       (payload IS NULL) AS payload_purged,
       {_MATERIAL_WITHHELD} AS material_withheld,
       CASE WHEN {_MATERIAL_WITHHELD} THEN NULL
            ELSE left(payload ->> 'text', %(excerpt)s) END AS excerpt,
       CASE WHEN {_MATERIAL_WITHHELD} THEN NULL ELSE reply END AS reply,
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
    """Archive the material and enqueue it. Returns the ack.

    `submitted_by` is the caller's RESOLVED identity, supplied by the service layer — this
    function has no way to learn an identity from the input, which is the point: there is no code
    path from client-controlled bytes to this column.

    `extra_blob_refs` (ADR 028 D3): evidence keys the CALLER already archived, appended
    AFTER the material's own blob — so `blob_refs[0]` stays the text material every reader has
    always assumed, and a drive row's original bytes ride at `blob_refs[1]`. Operator-CLI
    callers only; the MCP transport never passes it (nothing model-facing can name an evidence
    key it did not write, and a key that merely does not exist would dangle harmlessly —
    `evidence.get` refuses).

    Validation -> blob -> row, in that order (see the module docstring on write order).
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
    """Claim the oldest queued item, exactly once, or None when the queue is empty.

    Sweeps expired claims first, so a worker that died mid-item is recovered by the NEXT claimer
    with no separate cron — the recovery path is on the hot path precisely so it cannot rot
    (`release_expired` is still public: `stigmergy-queue reclaim` and the librarian's worker loop
    both call it directly, and it is what a test drives to prove redelivery works).
    """
    release_expired(conn, visibility_timeout_s=visibility_timeout_s, max_attempts=max_attempts)
    with conn.cursor() as cur:
        cur.execute(_CLAIM)
        row = cur.fetchone()
    return _item(row) if row else None


def release_expired(conn, *, visibility_timeout_s: int,
                    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                    batch: int = RECLAIM_BATCH) -> dict:
    """Return timed-out claims to the queue; give up on the ones that have burned their attempts.

    `visibility_timeout_s` is REQUIRED, unlike `claim_next`'s. The difference is not style: a
    claimer states the lease it is taking and therefore knows the number, while a sweeper states
    how dead a worker must be before its work is taken away, which is a fact about a worker this
    module cannot see. A default here is a guess wearing a policy's clothes — and it was one: the
    admin console reclaimed against 300s while the librarian's lease was 900s, requeueing captures
    out from under running workers. Every caller names its own horizon now.

    Returns `{"released": n, "failed": m}`. The queue transitions happen in ONE transaction (a
    sweep either moves a row or does not); the `ingest_errors`/`job_runs` bookkeeping is written
    after it, deliberately outside, so a bookkeeping failure can never abort the recovery it was
    recording. A `job_runs` row is written only when the sweep actually moved something: every
    real recovery is recorded, while the no-op sweep that runs before every claim stays silent
    instead of drowning the table.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_SELECT_EXPIRED,
                    {"visibility": visibility_timeout_s, "batch": max(1, int(batch))})
        expired = cur.fetchall()
        exhausted = [(sid, attempts) for sid, attempts in expired if attempts >= max_attempts]
        recoverable = [sid for sid, attempts in expired if attempts < max_attempts]

        for submission_id, attempts in exhausted:
            # The fence always matches here: these rows were selected FOR UPDATE inside this same
            # transaction, so nothing can have redelivered them between the read and this write.
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
    """Move a row THIS caller is holding out of flight. The only transition helper — the librarian
    files through this one.

    `expected_attempts` is the **fencing token**: pass the `attempts` value `claim_next` returned
    for this delivery. It is required, not optional, because an optional fence is a fence nobody
    passes — and the failure it prevents (a stalled worker finishing an item that has since been
    redelivered to somebody else) is silent, duplicated work rather than a loud error. The row is
    updated only if it is still `claimed` AND still on that same delivery.

    `status` must be one of `schema.FINISHED_STATUSES`. The terminal three (`filed`, `rejected`,
    `failed`) stamp `finished_at`, which is what retention counts 30 days from and what the
    submit->terminal latency is measured against; the parked pair (`needs_input`, `triage`) leave
    it NULL and drop `claimed_at`, because a row waiting for a human is not in flight and is not
    done — it must not be purged out from under the question it is waiting on.

    `report` is the librarian's structured account of the item (page, commit, anchoring, links,
    overlaps, pages edited, findings — `schema.base_report` defines the shape). It is ADDITIVE and
    optional: `None` leaves whatever the column held (`COALESCE`), so this module's own caller —
    `release_expired`, which fails a row whose worker died — keeps working unchanged and never
    blanks a report a previous delivery wrote. `error` keeps its meaning either way: the one human
    sentence saying why the row is where it is.

    **Three side effects, all of them on the parked branch and all of them here rather than in the
    caller**, because this is the single transition every producer of a parked row goes through:

    - `parked_at` is stamped on every park, so `list`/`show` can say how long a human has been
      waited on without inferring it from `created_at` (which is when the material arrived);
    - `asked_at` is stamped on the FIRST transition into `needs_input` and never again
      (`COALESCE`), which is the durable half of the one-ask budget — a requeue does not clear it
      and a lease redelivery does not reset it, so the budget survives both;
    - the question is recorded as an `asked` trace event, so the row's own history still answers
      "what was this person asked" after the next pass has overwritten `report`.

    `outcome` is the agent's structured account of this pass, kept across a park so a re-file can
    reuse it instead of re-reading the material — see
    `capture.schema._CAPTURE_QUEUE_OUTCOME_COLUMN` for why it exists. It follows
    `report`'s `COALESCE` convention on a PARK (`None` leaves whatever a previous delivery stored)
    and is **cleared on every terminal status**, which is the half that matters: once a row is
    `filed`, `rejected` or `failed`, a stored distillation can never be reused by anything, and
    keeping the full drafted text of every page beside a closed row is exactly the accumulation
    retention exists to prevent.

    **"The column holds a distillation only while a human is being waited on" reads as though that
    were a bounded SPAN. It is not.** A parked row
    (`needs_input`/`triage`) has no scheduled purge of any kind (`capture.retention.purge`'s own
    eligibility only ever matches a TERMINAL status), so this column's window is unbounded by
    policy: it stays exactly as it is for as long as the row stays parked, which can be an hour or
    a year, and ends only when a HUMAN act — a reply, or a steward's disposition — moves the row
    to `queued` (cleared implicitly, since the next claim starts a fresh pass) or to a terminal
    state (cleared explicitly, right here). There is no clock running against it.

    Raises `QueueStateError` when the write did not apply, naming which of the two reasons it was:
    the lease was lost to a redelivery, or the row was not in flight at all.
    """
    if status not in schema.FINISHED_STATUSES:
        raise QueueStateError(
            f"cannot finish into {status!r} (allowed: {', '.join(sorted(schema.FINISHED_STATUSES))})")
    terminal = status in schema.TERMINAL_STATUSES
    asking = status == schema.NEEDS_INPUT
    with conn.cursor() as cur:
        # `report is None`, not `if report`: an EMPTY dict is a report, and `COALESCE` would
        # otherwise leave a previous delivery's report in place — so a row could carry a report
        # describing an earlier attempt while its status described this one. Only `None` means
        # "this caller has no report and must not blank the column" (`release_expired`).
        cur.execute(_FINISH, {
            "status": status, "result_ref": result_ref, "error": error,
            "report": None if report is None else Jsonb(report),
            # Same `is None` distinction `report` needs, for the same reason — plus the terminal
            # clear, which is unconditional and takes precedence over any value passed in.
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

    The same question `finish` asks with its fence, asked EARLY. `finish` refusing after the fact
    is the right guarantee for a queue row and no guarantee at all for something already done
    outside the database: a worker whose lease expired mid-item still went on to commit and push,
    and only then discovered the row belonged to somebody else. The result was one capture filed
    twice, with the second page referenced by no queue row.

    A caller uses this immediately before an irreversible step, not instead of the fence — the
    fence stays, because between this read and that step there is still a window. Narrowing it
    from "a whole agent run plus the gates" to "one push" is the point.
    """
    with conn.cursor() as cur:
        cur.execute(_FINISH_DIAGNOSE, (submission_id,))
        row = cur.fetchone()
    if row is None:
        return False
    status, attempts = row
    return status == schema.CLAIMED and int(attempts) == int(expected_attempts)


def _lost_lease_reason(submission_id: int, status: str, expected_attempts: int, current) -> str:
    """Why a `finish` did not apply. Names the id, the state and the attempt counts and nothing
    else — enough for a worker log to be actionable, with no material and no identity in it."""
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
# `finish` moves a row a WORKER holds. These two move a row NOBODY holds — a parked row waiting on
# a person — and that difference is why they are separate statements rather than a flag on
# `finish`: the fence there is `attempts`, which names a delivery, and a steward or a submitter has
# no delivery. Passing one would be inventing a lease to satisfy a guard.
#
# So the guard is the STATE instead, and it is in the WHERE clause for the same reason `finish`'s
# is: a disposition that raced a live worker's claim must fail loudly rather than silently update
# nothing. `status = ANY(PARKED_STATUSES)` refuses a `claimed` row (a worker is mid-item), a
# `queued` row (nothing is parked), and every terminal row (it is already done) — three refusals
# from one predicate, each of which the caller then names specifically from a diagnostic read.
#
# **`attempts` appears in neither statement.** Not as a guard and not as a value: the lease fence is
# monotonic per delivery, and a disposition that bumped or reset it would either burn a delivery a
# worker never got or hand a stale worker back a fence it should have lost. A requeued or replied
# row is claimable again with exactly the deliveries it had earned.
_PARKED_LITERALS = ", ".join(f"'{s}'" for s in sorted(schema.PARKED_STATUSES))

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
    """THE state-guarded transition out of a park — the one base every disposition rides.

    Callers do not build this call themselves: they go through `capture.dispositions`, which names
    the three business intents (`requeue`, `resolve`, `reject`) and owns the wording each one puts
    in front of the submitter. This function owns exactly one thing — that the move is legal — and
    it decides it in SQL, so a caller cannot decide it differently.

    Returns `{"id", "status", "attempts"}`; raises `QueueStateError` naming which refusal it was.
    `attempts` comes back UNCHANGED and is returned so a caller can say so.
    """
    if status not in (schema.QUEUED, schema.RESOLVED, schema.REJECTED):
        raise QueueStateError(
            f"cannot dispose into {status!r} (a disposition returns a parked row to "
            f"{schema.QUEUED!r} or closes it as {schema.RESOLVED!r} or {schema.REJECTED!r})")
    if not actor:
        # Attribution is the whole point of a disposition: attribution, not authorization. An
        # unattributed one records that a row moved and not who moved it.
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

    State-guarded on `needs_input` alone: a row that has already been answered, filed or drained by
    a steward has nothing to answer, and the guard is what makes that true under a race rather than
    only in the caller's reading of the row a moment earlier.

    `actor` is the RESOLVED identity that replied — the submitter, or a steward replying on their
    behalf — and it is recorded on the trace event, which is the attribution. `note` distinguishes
    the two without a second column.

    Does not touch `attempts` (see the section comment above) and leaves the row claimable, which
    is what makes the next pass an ordinary delivery rather than a special case.
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
    """This row's status, or None when there is no such row. The read a caller needs to turn a
    refused transition into a sentence naming the actual state — and the one `server.service`'s
    reply path uses to tell an AUTHORIZED caller why their reply bounced."""
    with conn.cursor() as cur:
        cur.execute(_DISPOSE_DIAGNOSE, (submission_id,))
        row = cur.fetchone()
    return row[0] if row else None


def _not_parked_reason(submission_id: int, action: str, current) -> str:
    """Why a disposition or a reply did not apply. Names the id and the state and nothing else —
    no material, no identity, no note.

    Three cases, deliberately distinguished: a row that does not exist, a row a worker is holding
    (the fencing case — a disposition must never race a live claim), and a row that is simply not
    parked. They have three different next actions for the operator, and collapsing them into one
    message is how "it is currently being worked on" reads as "it is gone".
    """
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

    `submitter=None` means "every identity" — a MANAGEMENT scope that only the steward entry
    point may ask for. Callers do not build this call themselves: they go through
    `list_own_submissions` (a submitter's own) or `list_all_submissions` (the steward's), so the
    scoping decision is made once, by name, instead of being re-remembered at each surface.

    A purged row (retention nulled its payload/hints) is still returned, with
    `payload_purged: true` and no excerpt: history stays readable — id, submitter, timestamps,
    status and `result_ref` are exactly what survives on purpose.

    A row refused for a SECRET or PII match is returned the same way, with `withheld_reason` set
    and no excerpt and no client hints (`_MATERIAL_WITHHELD`). The two cases are deliberately the
    same shape: history stays readable, the material does not. The refusal sentence in that row's
    `error`/`report` promises the matched value is not handed back, and this query is what makes
    that promise true — for the submitter reading their own row, and for a steward listing
    everybody's.
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
    """A submitter's own submissions — the scoped (operational) entry point. `submitter` is not
    optional here: an empty or None identity would silently widen the query to everybody's rows,
    so this is where that mistake is made impossible."""
    if not submitter:
        raise ValueError("submitter is required to list a caller's own submissions")
    return query_submissions(conn, submitter=submitter, **kwargs)


def list_all_submissions(conn, **kwargs) -> list[dict]:
    """Every identity's submissions — the steward (management) entry point. The caller is
    responsible for having established that the identity is unrestricted; the server does that in
    exactly one place (`BrainService.submissions`, from the resolved audience scope), never from
    a client argument."""
    return query_submissions(conn, submitter=None, **kwargs)


def get_submission_trace(conn, submission_id: int, *, submitter: str | None = None) -> dict | None:
    """The per-submission trace: `created_at -> claimed_at -> finished_at`, `attempts`, and the
    latencies computable from them alone.

    `submitter`, when given, scopes the lookup — a row belonging to somebody else returns None,
    the same shape a nonexistent id returns, so an id lookup can never confirm that another
    identity's submission exists (the same no-existence-leak posture `read_page` takes).

    **`reply` obeys `_MATERIAL_WITHHELD` here too, and that is a correction.** The rule was written
    into `_LIST_SELECT` and not into this query, so the same row handed back its answer through
    `stigmergy-queue show` and `stigmergy-entities show` while the list suppressed it — a confidentiality
    asymmetry rather than a live leak (no wire caller reads the trace's `reply` today), which is one
    refactor away from being a leak and had a comment in `server.service` asserting the opposite.
    The rule itself is one expression, `_MATERIAL_WITHHELD`, used by both paths, so they cannot drift
    again. `withheld_reason` is shaped exactly as `_shape_listed` shapes it: a SENTENCE, so every
    surface says why there is nothing to show in the same words.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, kind, submitted_by, status, attempts, created_at, claimed_at,"
            " finished_at, result_ref, error, report, blob_refs, asked_at, trace,"
            f" CASE WHEN {_MATERIAL_WITHHELD} THEN NULL ELSE reply END AS reply,"
            f" {_MATERIAL_WITHHELD} AS material_withheld,"
            f" {_PARKED_AGE_MS} AS parked_age_ms,"
            " (payload IS NULL) AS payload_purged"
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
    # The listed row's shaping, applied to the same fact: `schema.withheld_reason` is the one
    # function both this path and `_shape_listed` call, so `stigmergy-queue show` and
    # `stigmergy-entities show` explain an empty `reply` in exactly the words `stigmergy-queue list`
    # uses — including the pending/failed cases, which this path gets for free rather than needing
    # its own copy of the three-way branch.
    trace.pop("material_withheld")   # superseded by schema.withheld_reason below (same underlying fact)
    trace["withheld_reason"] = schema.withheld_reason(trace["status"], trace["report"])
    trace["events"] = list(trace.pop("trace") or [])
    trace["parked_age_ms"] = _float_or_none(trace["parked_age_ms"])
    trace["waiting_on"] = waiting_on(trace["status"], trace["submitted_by"])
    return trace


# Who a parked row is waiting on — the ONE piece of information a steward triaging a list
# prioritizes on, and the reason `needs_input` and `triage` must not render alike. Written once and
# shared by every surface (`list`, `show`, `brain_submissions`) because two states parked on two
# different people is exactly the distinction a second implementation would blur.
WAITING_ON_STEWARD = "a steward"


def waiting_on(status: str, submitted_by: str) -> str:
    """`needs_input` waits on the SUBMITTER (they have a question to answer); `triage` waits on a
    steward (they have a placement to decide). Everything else waits on nobody, and says so with an
    empty string rather than a phrase a caller has to parse."""
    if status == schema.NEEDS_INPUT:
        return submitted_by or WAITING_ON_STEWARD
    return WAITING_ON_STEWARD if status == schema.TRIAGE else ""


def _float_or_none(value):
    return None if value is None else float(value)


def query_in_flight(conn, *, visibility_timeout_s: int = DEFAULT_VISIBILITY_TIMEOUT_S) -> list[dict]:
    """Every `claimed` row, oldest first, with how long it has been held and whether that lease has
    already expired against `visibility_timeout_s`.

    The read behind `stigmergy-librarian status`'s in-flight line. It carries no payload and no
    excerpt on purpose: the question is "is a worker alive and working on this", and captured
    material has no part in the answer (nor in the terminal it is printed to).

    `lease_expired` is `_LEASE_EXPIRED`, the same predicate the sweep acts on — so "looks stale" and
    "will be returned to the queue by the next sweep" are one fact rather than two estimates.
    Passing the timeout is required in spirit even though it defaults: a status command that
    compared against the QUEUE's 300s default while the worker was configured for 900s would call
    every long-running agent item dead.
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
    """capture -> filed durations in milliseconds for the most recent filings, newest first.

    The raw material for the p50/p95, read from the trace and nothing else. Returning
    the samples rather than the percentiles is deliberate: the percentile arithmetic is a pure
    function with an interesting edge (too few samples to answer at all) and belongs somewhere a
    test can drive it without a database — `stigmergy.capture.latency`.
    """
    with conn.cursor() as cur:
        cur.execute(_SELECT_FILED_LATENCIES, {"limit": max(1, min(int(limit), 10_000))})
        return [float(row[0]) for row in cur.fetchall() if row[0] is not None]


# capture -> SEARCHABLE, a different number from capture -> filed now that the incremental webhook
# makes it one (`stigmergy.server.pilot_report` is the one caller). `job_name` is a parameter, never
# a hardcoded string: `capture` sits below `stigmergy.server` (which owns `webhook.py` and its
# `JOB_NAME` constant) and may not import it, so the CALLER (`stigmergy.server.pilot_report`)
# supplies the exact job name it wrote.
#
# `DISTINCT ON (cq.id)` — a REDELIVERED webhook writes a SECOND `ok` job_runs row for the
# identical sha (GitHub's own redelivery, or an operator's manual re-trigger), and without it the
# join fans one `filed` row out to one sample PER job_runs row,
# double-counting a single capture and inflating p95. The inner query picks the EARLIEST `ok` run
# per capture (`ORDER BY cq.id, jr.finished_at ASC` — required by Postgres to match `DISTINCT ON`,
# and the honest choice besides: the first time this push made the page searchable, not the last
# redelivery's bookkeeping); the outer query keeps the original "most recent captures first" order
# and LIMIT unchanged.
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
    """capture -> SEARCHABLE durations in milliseconds — a `filed` row's `result_ref` (`path@sha`)
    joined against the incremental webhook's own `job_runs` row for that exact sha. One commit and
    one push per filed capture (`_file`'s own per-item design), so the join is 1:1, not an
    aggregate over a batched push.

    A `filed` row with no matching webhook `job_runs` row (the webhook was never configured, or
    the push exceeded the file cap and deferred to the nightly rebuild)
    contributes NO sample here — it is silently absent, not zero and not estimated, because
    "searchable" for that row is only known to within "sometime before the next nightly rebuild",
    which is not a latency this function can honestly report a millisecond figure for.
    """
    with conn.cursor() as cur:
        cur.execute(_SELECT_SEARCHABLE_LATENCIES,
                   {"job_name": job_name, "limit": max(1, min(int(limit), 10_000))})
        return [float(row[0]) for row in cur.fetchall() if row[0] is not None]


def counts_by_status(conn) -> dict[str, int]:
    """Queue depth per status — the steward's one-line answer to "what is waiting, what is
    stuck". Every declared status is present, zero included, so a caller never has to guess
    whether a missing key means zero or means the vocabulary changed."""
    with conn.cursor() as cur:
        cur.execute("SELECT status, count(*) FROM capture_queue GROUP BY status")
        counted = dict(cur.fetchall())
    return {status: counted.get(status, 0) for status in schema.STATUSES}


def _millis(start, end) -> float | None:
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    return (end - start).total_seconds() * 1000


def _shape_listed(row: dict) -> dict:
    """One listed row, with the withheld case already settled by the query.

    `withheld_reason` is `""` on an ordinary row and one of three shared sentences
    (`schema.withheld_reason`) on a row whose material may not be read back — pending
    (`queued`/`claimed`, the gate has not run), failed (the accepted residual), or a genuine
    secret/PII match. A SENTENCE rather than a bare boolean because every surface that renders
    this row has to say why there is nothing to show, and a boolean would make each of them invent
    its own wording — the drift `librarian.report` exists to prevent one layer up.

    Which FIELDS are suppressed is decided in SQL (`_MATERIAL_WITHHELD`), which is why `excerpt` is
    already NULL and the client hints are already gone by the time this function sees the row;
    `schema.withheld_reason` only picks which of the three sentences explains it.
    """
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
        # `{}` rather than None for an unprocessed row: a caller shaping this for the wire asks
        # "what did the librarian report", and an empty mapping answers "nothing yet" without
        # every consumer needing a None guard. The COLUMN stays NULL — see schema.py.
        "report": row["report"] or {},
        "blob_refs": list(row["blob_refs"] or []),
        "payload_purged": bool(row["payload_purged"]),
        "withheld_reason": schema.withheld_reason(row["status"], row["report"]),
        # The human loop's four facts. `asked_at` is the one-ask budget's visible half — a
        # `triage` row carrying one was ASKED and answered, which is a different story from one that
        # never got a question, and a steward reading the list should not have to open the row to
        # tell them apart.
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
