"""The worker claims a `kind='meeting'` row through the same fenced claiming as every capture:
the lease/redelivery/fencing properties, re-run over the meeting kind.

The gap this closes: `queue.claim_next`/`release_expired`/`finish` were only ever exercised with
`kind="raw"`, so all that was confirmed is that the suite still passes with `kind="raw"`
everywhere. Reading `queue.py` shows claiming genuinely never branches on
`kind` (`worker.process_next`'s own comment: "`queue.claim_next` above knows nothing about `kind` at
all — claiming stays kind-agnostic"), but a property proven by code reading is not a property proven
by a test: a `CHECK` constraint, a partial index, or a future per-kind branch could all silently
scope one of these guarantees to `kind='raw'` without any existing test noticing.

This file does not re-run every lease test in `test_queue_pg.py`/`test_finish_fencing_pg.py` against
`kind="meeting"` (that would duplicate ~30 tests for one boolean this system does not vary on) — it
re-runs the THREE load-bearing ones: exactly-once claiming under real concurrency (`FOR UPDATE SKIP
LOCKED`, provable only against real Postgres), the stale-`expected_attempts`-raises fencing property,
and the two-real-thread race that exercises both together. If any of the three silently regressed
for `kind="meeting"` specifically, this is where it would show.
"""
import concurrent.futures
import threading
import time

from stigmergy.capture import queue, schema
from stigmergy.capture.errors import QueueStateError
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.index import store as index_store

SUBMITTER = "meeting-fencing@stigmergy.test"


def _submit_meeting(conn, *, material: str = "a fencing-test transcript"):
    evidence = MemoryEvidenceStore()
    hints = {"title": "fencing test", "meeting_date": "2026-07-29",
            "source_label": "granola-manual"}
    return queue.submit(conn, evidence, kind=schema.MEETING, material=material, hints=hints,
                        submitted_by=SUBMITTER)


# ── part 1: exactly-once claiming, kind="meeting", real Postgres concurrency ───────────────────
def test_a_meeting_row_is_claimed_exactly_once_under_n_parallel_claimers(clean_queue):
    queued = 12
    for _ in range(queued):
        _submit_meeting(clean_queue)

    def claim_once(_i):
        with index_store.connect() as worker_conn:
            item = queue.claim_next(worker_conn)
            return item["id"] if item else None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(claim_once, range(queued + 5)))   # more claimers than rows
    claimed_ids = [c for c in claimed if c is not None]

    assert len(claimed_ids) == queued                   # exactly M claims for M queued meeting rows
    assert len(set(claimed_ids)) == queued               # no row claimed twice


# ── part 2: a redelivered meeting row increments attempts, and a stale finish() is
# fenced and writes nothing — `kind="meeting"`'s own copy of `test_finish_fencing_pg.py`'s property
def test_a_stale_meeting_finish_raises_and_writes_nothing(clean_queue):
    ack = _submit_meeting(clean_queue)
    item = queue.claim_next(clean_queue)
    assert item["id"] == ack["id"]
    assert item["kind"] == schema.MEETING

    # redelivered — e.g. the sweep decided the first worker's claim had expired — and a SECOND
    # worker legitimately claims and finishes it (attempts=2).
    queue.release_expired(clean_queue, visibility_timeout_s=0)
    redelivered = queue.claim_next(clean_queue)
    assert redelivered["id"] == item["id"]
    assert redelivered["attempts"] == 2

    real_report = {"status": schema.FILED, "summary": f"{schema.FILED} — the legitimate worker"}
    queue.finish(clean_queue, item["id"], status=schema.FILED, expected_attempts=2,
                result_ref="wiki/meetings/real.md@deadbeef", report=real_report)

    # the FIRST worker, unaware it was ever redelivered, tries to finish with its STALE token.
    stale_report = {"status": schema.REJECTED, "summary": "a stale worker's report — must not win"}
    raised = False
    try:
        queue.finish(clean_queue, item["id"], status=schema.REJECTED, expected_attempts=1,
                    result_ref="", error="stale rejection", report=stale_report)
    except QueueStateError:
        raised = True
    assert raised, "a stale expected_attempts finish() on a meeting row must raise, not succeed"

    row = queue.get_submission_trace(clean_queue, item["id"])
    assert row["status"] == schema.FILED
    assert row["result_ref"] == "wiki/meetings/real.md@deadbeef"
    assert row["report"] == real_report


# ── part 3: the real two-thread race, kind="meeting" — the property that can ONLY be
# shown under genuine concurrent execution (mirrors test_finish_fencing_pg.py's own real-thread test)
def test_two_workers_racing_a_stale_lease_on_a_meeting_row_never_double_finishes(clean_queue):
    ack = _submit_meeting(clean_queue, material="a two-worker meeting race transcript")

    visibility_s = 0.5
    results: dict = {}
    start_barrier = threading.Barrier(2)

    def slow_worker():
        with index_store.connect() as conn:
            item = queue.claim_next(conn, visibility_timeout_s=visibility_s)
            results["slow_claimed"] = item is not None
            if item is None:
                start_barrier.wait()
                return
            results["slow_attempts"] = item["attempts"]
            start_barrier.wait()
            time.sleep(visibility_s * 4)   # long enough the lease genuinely expires mid-"processing"
            try:
                queue.finish(conn, item["id"], status=schema.REJECTED,
                            expected_attempts=item["attempts"], error="stale worker, must not win")
                results["slow_finish_raised"] = False
            except QueueStateError:
                results["slow_finish_raised"] = True

    def fast_worker():
        start_barrier.wait()
        time.sleep(visibility_s * 2)
        with index_store.connect() as conn:
            item = queue.claim_next(conn, visibility_timeout_s=visibility_s)
            results["fast_claimed"] = item is not None
            if item is None:
                return
            results["fast_attempts"] = item["attempts"]
            queue.finish(conn, item["id"], status=schema.FILED,
                        expected_attempts=item["attempts"],
                        result_ref="wiki/meetings/x.md@deadbeef")

    t1 = threading.Thread(target=slow_worker)
    t2 = threading.Thread(target=fast_worker)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    assert not t1.is_alive() and not t2.is_alive(), "a worker thread never finished — real hang"

    assert results.get("slow_claimed") is True
    assert results.get("fast_claimed") is True
    assert results["fast_attempts"] > results["slow_attempts"], (
        "no genuine redelivery happened for the meeting row")
    assert results["slow_finish_raised"] is True, (
        "the stale worker's finish() on a meeting row was not fenced — a lease was stolen")

    row = queue.get_submission_trace(clean_queue, ack["id"])
    assert row["status"] == schema.FILED
    assert row["result_ref"] == "wiki/meetings/x.md@deadbeef"
    assert row["attempts"] == results["fast_attempts"]
