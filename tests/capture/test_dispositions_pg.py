"""`capture.dispositions` against a real Postgres — the steward's drain: `requeue`/`resolve`/
`reject` move only from `triage`/`needs_input`, refuse a `claimed` or terminal row (fencing — a
disposition can never race a live worker claim), record actor + note on the row's own trace,
never touch `attempts`, and leave the row claimable.

One real-concurrency test opens TWO connections (one per simulated steward), exactly like
`test_queue_pg.py`'s exactly-once-claiming test opens one per simulated worker: the guarantee is a
real Postgres row-lock property, and a faked race would prove nothing about it.
"""
import concurrent.futures

import pytest

from stigmergy.capture import dispositions, queue, schema
from stigmergy.capture.errors import QueueStateError
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.index import store
from tests.capture.conftest import unique_material

ALICE = "alice@example.com"
STEWARD = "steward"


def _submit(conn, *, submitted_by=ALICE):
    return queue.submit(conn, MemoryEvidenceStore(), kind="raw", material=unique_material(),
                        hints=None, submitted_by=submitted_by)


def _parked(conn, *, status: str, submitted_by=ALICE) -> dict:
    """Submit, claim, and park at `status` (`triage` or `needs_input`) — the state every
    disposition acts on."""
    ack = _submit(conn, submitted_by=submitted_by)
    claimed = queue.claim_next(conn)
    queue.finish(conn, ack["id"], status=status, expected_attempts=claimed["attempts"],
                error="which entity?" if status == schema.TRIAGE else "")
    return ack


def _row(conn, submission_id) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT status, attempts, trace, claimed_at FROM capture_queue WHERE id = %s",
                    (submission_id,))
        status, attempts, trace, claimed_at = cur.fetchone()
    return {"status": status, "attempts": attempts, "trace": trace or [], "claimed_at": claimed_at}


# ── requeue: the only non-terminal disposition ───────────────────────────────────────────────────
@pytest.mark.parametrize("origin", [schema.TRIAGE, schema.NEEDS_INPUT])
def test_requeue_moves_either_parked_state_back_to_queued(clean_queue, origin):
    ack = _parked(clean_queue, status=origin)
    result = dispositions.requeue(clean_queue, ack["id"], actor=STEWARD, note="try again")
    assert result["status"] == schema.QUEUED
    row = _row(clean_queue, ack["id"])
    assert row["status"] == schema.QUEUED


def test_requeue_never_touches_attempts(clean_queue):
    ack = _parked(clean_queue, status=schema.TRIAGE)
    before = _row(clean_queue, ack["id"])["attempts"]
    dispositions.requeue(clean_queue, ack["id"], actor=STEWARD)
    after = _row(clean_queue, ack["id"])["attempts"]
    assert after == before


def test_requeue_leaves_the_row_claimable(clean_queue):
    ack = _parked(clean_queue, status=schema.TRIAGE)
    dispositions.requeue(clean_queue, ack["id"], actor=STEWARD)
    claimed = queue.claim_next(clean_queue)
    assert claimed is not None and claimed["id"] == ack["id"]


def test_requeue_records_the_actor_and_note_on_the_trace(clean_queue):
    ack = _parked(clean_queue, status=schema.TRIAGE)
    dispositions.requeue(clean_queue, ack["id"], actor=STEWARD, note="looked wrong, try again")
    events = _row(clean_queue, ack["id"])["trace"]
    assert len(events) == 1
    assert events[0]["event"] == schema.EVENT_REQUEUED
    assert events[0]["actor"] == STEWARD
    assert "looked wrong, try again" in events[0]["note"]


def test_requeue_clears_the_stale_question_so_it_does_not_render_on_a_queued_row(clean_queue):
    ack = _parked(clean_queue, status=schema.NEEDS_INPUT)
    dispositions.requeue(clean_queue, ack["id"], actor=STEWARD)
    with clean_queue.cursor() as cur:
        cur.execute("SELECT error FROM capture_queue WHERE id = %s", (ack["id"],))
        assert cur.fetchone()[0] == ""


# ── resolve: terminal, honest, echoes what the steward actually gave it ─────────────────────────
def test_resolve_closes_as_resolved_and_stamps_finished_at(clean_queue):
    ack = _parked(clean_queue, status=schema.TRIAGE)
    result = dispositions.resolve(clean_queue, ack["id"], actor=STEWARD, note="folded by hand",
                                  page="wiki/entities/Jordan Reyes.md", commit="abc123")
    assert result["status"] == schema.RESOLVED
    with clean_queue.cursor() as cur:
        cur.execute("SELECT status, finished_at, result_ref, report FROM capture_queue"
                    " WHERE id = %s", (ack["id"],))
        status, finished_at, result_ref, report = cur.fetchone()
    assert status == schema.RESOLVED
    assert finished_at is not None
    assert result_ref == "wiki/entities/Jordan Reyes.md@abc123"
    assert report["status"] == schema.RESOLVED
    assert "wiki/entities/Jordan Reyes.md@abc123" in report["summary"]


def test_resolve_with_only_a_commit_echoes_that_and_nothing_else(clean_queue):
    ack = _parked(clean_queue, status=schema.TRIAGE)
    dispositions.resolve(clean_queue, ack["id"], actor=STEWARD, note="an edit, no new page",
                         commit="deadbeef")
    with clean_queue.cursor() as cur:
        cur.execute("SELECT result_ref FROM capture_queue WHERE id = %s", (ack["id"],))
        assert cur.fetchone()[0] == "deadbeef"


def test_resolve_never_touches_attempts(clean_queue):
    ack = _parked(clean_queue, status=schema.TRIAGE)
    before = _row(clean_queue, ack["id"])["attempts"]
    dispositions.resolve(clean_queue, ack["id"], actor=STEWARD, note="handled")
    assert _row(clean_queue, ack["id"])["attempts"] == before


def test_resolve_records_actor_and_note_on_the_trace(clean_queue):
    ack = _parked(clean_queue, status=schema.NEEDS_INPUT)
    dispositions.resolve(clean_queue, ack["id"], actor=STEWARD, note="folded it in by hand")
    events = _row(clean_queue, ack["id"])["trace"]
    assert events[-1]["event"] == schema.EVENT_RESOLVED
    assert events[-1]["actor"] == STEWARD


# ── reject: attribution, not authorization — the queue's rule, applied to a human's call ────────
def test_reject_closes_as_rejected_with_the_reason_recorded(clean_queue):
    ack = _parked(clean_queue, status=schema.TRIAGE)
    result = dispositions.reject(clean_queue, ack["id"], actor=STEWARD, reason="wrong venue")
    assert result["status"] == schema.REJECTED
    with clean_queue.cursor() as cur:
        cur.execute("SELECT status, report FROM capture_queue WHERE id = %s", (ack["id"],))
        status, report = cur.fetchone()
    assert status == schema.REJECTED
    assert "wrong venue" in report["summary"]
    assert report[schema.REASON_CODE_KEY] == schema.REASON_STEWARD


def test_reject_never_touches_attempts(clean_queue):
    ack = _parked(clean_queue, status=schema.TRIAGE)
    before = _row(clean_queue, ack["id"])["attempts"]
    dispositions.reject(clean_queue, ack["id"], actor=STEWARD, reason="no")
    assert _row(clean_queue, ack["id"])["attempts"] == before


# ── `resolve`/`reject` are terminal dispositions — `outcome` must not survive either ────────────
# `queue.dispose` (the one guarded transition BOTH of these ride) clears `outcome` unconditionally
# whenever the target status is terminal, matching `queue.finish`'s own rule and for the identical
# reason: a stored distillation carries the full drafted text of every page, and a row a human just
# closed by hand can never be re-filed by anything, so keeping it beside a closed row is retained
# with no consumer. `test_queue_pg.py` proves the SAME property for `finish`; this is the OTHER
# family of transitions that closes a row — a steward's `resolve`/`reject` never goes through
# `finish` at all, so a defect isolated to `dispose`'s own CASE WHEN would have no test over there
# that could catch it. `requeue` (not terminal) is deliberately absent from this pair: it must
# LEAVE `outcome` alone (that is the whole re-file-reuse mechanism), and that half is already
# proven live, end to end, by `test_the_re_file_reuses_the_parked_distillation_and_spends_no_agent_
# pass` — a requeue that silently cleared it would make that test's agent call count go non-zero.
def _parked_with_outcome(conn, *, status: str) -> dict:
    """`_parked`'s sibling: the same shape, plus a stored `outcome` — the precondition every test
    in this section needs (a value that COULD leak past the disposition if the clear were wrong)."""
    ack = _submit(conn)
    claimed = queue.claim_next(conn)
    stored = {"version": 1, "raw": {"decisions": [{"title": "must not survive a disposition"}]}}
    queue.finish(conn, ack["id"], status=status, expected_attempts=claimed["attempts"],
                error="which entity?" if status == schema.TRIAGE else "", outcome=stored)
    return ack


def _outcome(conn, submission_id):
    with conn.cursor() as cur:
        cur.execute("SELECT outcome FROM capture_queue WHERE id = %s", (submission_id,))
        return cur.fetchone()[0]


def test_resolve_clears_a_stored_outcome(clean_queue):
    ack = _parked_with_outcome(clean_queue, status=schema.TRIAGE)
    assert _outcome(clean_queue, ack["id"]) is not None    # sanity: it really was stored first
    dispositions.resolve(clean_queue, ack["id"], actor=STEWARD, note="handled by hand")
    assert _outcome(clean_queue, ack["id"]) is None, (
        "a steward's resolve left a stored distillation on a row nothing can ever re-file again")


def test_reject_clears_a_stored_outcome(clean_queue):
    ack = _parked_with_outcome(clean_queue, status=schema.TRIAGE)
    assert _outcome(clean_queue, ack["id"]) is not None
    dispositions.reject(clean_queue, ack["id"], actor=STEWARD, reason="wrong venue")
    assert _outcome(clean_queue, ack["id"]) is None, (
        "a steward's reject left a stored distillation on a row nothing can ever re-file again")


def test_requeue_the_benign_twin_leaves_a_stored_outcome_alone(clean_queue):
    """Requeue is the ONE disposition that must NOT clear `outcome` — it is the non-terminal half
    of the exact mechanism the column exists for (steward mints the entity, requeues, the next pass
    re-files the SAME stored distillation). Placed beside the two clearing tests above so a reader
    sees the full three-way contrast in one place rather than inferring the third from silence."""
    ack = _parked_with_outcome(clean_queue, status=schema.TRIAGE)
    stored = _outcome(clean_queue, ack["id"])
    dispositions.requeue(clean_queue, ack["id"], actor=STEWARD, note="minted, try again")
    assert _outcome(clean_queue, ack["id"]) == stored


# ── the fencing test: a disposition can NEVER race a live worker claim ──────────────────────────
@pytest.mark.parametrize("intent,call", [
    ("requeue", lambda conn, sid: dispositions.requeue(conn, sid, actor=STEWARD)),
    ("resolve", lambda conn, sid: dispositions.resolve(conn, sid, actor=STEWARD, note="x")),
    ("reject", lambda conn, sid: dispositions.reject(conn, sid, actor=STEWARD, reason="x")),
])
def test_a_disposition_refuses_a_row_a_worker_currently_holds(clean_queue, intent, call):
    """A row moves `triage`/`needs_input` -> `queued` (a requeue, or a reply) and is then CLAIMED —
    a worker is now mid-item. Every disposition must refuse it exactly the same way: naming that a
    worker may be mid-item, never silently overwriting a live claim."""
    ack = _parked(clean_queue, status=schema.TRIAGE)
    dispositions.requeue(clean_queue, ack["id"], actor=STEWARD)
    claimed = queue.claim_next(clean_queue)
    assert claimed["id"] == ack["id"]
    attempts_before = _row(clean_queue, ack["id"])["attempts"]

    with pytest.raises(QueueStateError, match="currently claimed"):
        call(clean_queue, ack["id"])
    # refused cleanly: the row is exactly as the worker left it
    row = _row(clean_queue, ack["id"])
    assert row["status"] == schema.CLAIMED
    assert row["attempts"] == attempts_before


@pytest.mark.parametrize("terminal_status", [schema.FILED, schema.REJECTED, schema.RESOLVED,
                                             schema.FAILED])
@pytest.mark.parametrize("intent,call", [
    ("requeue", lambda conn, sid: dispositions.requeue(conn, sid, actor=STEWARD)),
    ("resolve", lambda conn, sid: dispositions.resolve(conn, sid, actor=STEWARD, note="x")),
    ("reject", lambda conn, sid: dispositions.reject(conn, sid, actor=STEWARD, reason="x")),
])
def test_a_disposition_refuses_an_already_terminal_row(clean_queue, intent, call, terminal_status):
    ack = _parked(clean_queue, status=schema.TRIAGE)
    if terminal_status == schema.RESOLVED:
        dispositions.resolve(clean_queue, ack["id"], actor=STEWARD, note="already handled")
    else:
        dispositions.requeue(clean_queue, ack["id"], actor=STEWARD)
        claimed = queue.claim_next(clean_queue)
        queue.finish(clean_queue, ack["id"], status=terminal_status,
                    expected_attempts=claimed["attempts"])

    with pytest.raises(QueueStateError, match=f"is '{terminal_status}'"):
        call(clean_queue, ack["id"])


def test_a_disposition_refuses_a_row_that_was_never_parked_at_all(clean_queue):
    """`queued` is not one of the two parked states — a disposition acts only on `triage`/
    `needs_input`, never on an ordinary row waiting its turn."""
    ack = _submit(clean_queue)
    with pytest.raises(QueueStateError, match="acts only on a PARKED row"):
        dispositions.requeue(clean_queue, ack["id"], actor=STEWARD)


def test_a_disposition_on_a_nonexistent_id_names_it_and_nothing_else(clean_queue):
    with pytest.raises(QueueStateError, match="does not exist"):
        dispositions.resolve(clean_queue, 999_999_999, actor=STEWARD, note="x")


def test_requeue_requires_an_actor(clean_queue):
    """Attribution IS the whole point of a disposition: an unattributed one records that a row
    moved and not who moved it."""
    ack = _parked(clean_queue, status=schema.TRIAGE)
    with pytest.raises(QueueStateError, match="needs an actor"):
        dispositions.requeue(clean_queue, ack["id"], actor="")


# ── trace bounding: the oldest events are dropped, never the newest (schema.MAX_TRACE_EVENTS) ───
def test_trace_is_bounded_at_twenty_events_oldest_dropped_first(clean_queue):
    """25 requeue/re-park cycles append 25 trace events; only the newest 20 survive."""
    ack = _parked(clean_queue, status=schema.TRIAGE)
    for i in range(25):
        dispositions.requeue(clean_queue, ack["id"], actor=STEWARD, note=f"attempt {i}")
        with clean_queue.cursor() as cur:
            cur.execute("UPDATE capture_queue SET status = %s WHERE id = %s",
                       (schema.TRIAGE, ack["id"]))

    events = _row(clean_queue, ack["id"])["trace"]
    assert len(events) == schema.MAX_TRACE_EVENTS
    notes = [e["note"] for e in events]
    for dropped in range(5):
        assert f"attempt {dropped}" not in notes
    for kept in range(5, 25):
        assert f"attempt {kept}" in notes


# ── the genuine race: two stewards disposing of ONE row at the same instant ─────────────────────
def test_two_concurrent_dispositions_on_one_row_exactly_one_wins(clean_queue):
    """A real Postgres row-lock race, never a simulated one: two independent
    connections both attempt a disposition on the SAME parked row at the same instant. Exactly one
    UPDATE matches the row (Postgres serializes them); the loser's guard then reads a row that is
    no longer parked and refuses it — the row is never left ambiguous, and it is never disposed of
    twice."""
    ack = _parked(clean_queue, status=schema.TRIAGE)

    import threading
    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def _resolve():
        with store.connect() as conn:
            barrier.wait(timeout=5)
            try:
                outcomes["resolve"] = dispositions.resolve(
                    conn, ack["id"], actor="steward-a", note="resolved by A")
            except QueueStateError as ex:
                outcomes["resolve"] = ex

    def _reject():
        with store.connect() as conn:
            barrier.wait(timeout=5)
            try:
                outcomes["reject"] = dispositions.reject(
                    conn, ack["id"], actor="steward-b", reason="rejected by B")
            except QueueStateError as ex:
                outcomes["reject"] = ex

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_resolve), pool.submit(_reject)]
        for f in futures:
            f.result(timeout=10)

    results = [outcomes["resolve"], outcomes["reject"]]
    successes = [r for r in results if isinstance(r, dict)]
    failures = [r for r in results if isinstance(r, QueueStateError)]
    assert len(successes) == 1, "exactly one disposition must win the race"
    assert len(failures) == 1, "the loser must be refused, never silently ignored or duplicated"

    final_status = _row(clean_queue, ack["id"])["status"]
    assert final_status in (schema.RESOLVED, schema.REJECTED)
    assert successes[0]["status"] == final_status
    # the LOSER's refusal names the state the WINNER actually left it in — never a stale guess
    assert f"is '{final_status}'" in str(failures[0])
