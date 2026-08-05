"""The fencing regression. The lease fencing in `capture.queue.finish` fixed a real defect and
must not be reimplemented around: `finish` is fenced by `expected_attempts`, and the `report`
column must be fenced by the exact same token, or a stale worker could overwrite a legitimately
finished row's report even though its `status`/`result_ref` write is correctly refused.

Pure `capture.queue` + Postgres, no git and no gitleaks: the property under test is the database
primitive the worker is built on, not the filing path around it.
"""
import threading
import time

from stigmergy.capture import queue, schema
from stigmergy.capture.errors import QueueStateError
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.index import store as index_store


def _submit_and_claim(conn):
    evidence = MemoryEvidenceStore()
    ack = queue.submit(conn, evidence, kind="raw", material="fencing test material",
                       hints=None, submitted_by="fencing@stigmergy.test")
    item = queue.claim_next(conn)
    assert item["id"] == ack["id"]
    return item


def test_a_stale_expected_attempts_raises_and_writes_nothing(clean_queue):
    item = _submit_and_claim(clean_queue)          # attempts=1, the live delivery's own token

    # the item is redelivered — e.g. the sweep decided the first worker's claim had expired —
    # and a SECOND worker legitimately claims and finishes it (attempts=2).
    queue.release_expired(clean_queue, visibility_timeout_s=0)
    redelivered = queue.claim_next(clean_queue)
    assert redelivered["id"] == item["id"]
    assert redelivered["attempts"] == 2

    real_report = {"status": schema.FILED, "summary": f"{schema.FILED} — the legitimate worker"}
    queue.finish(clean_queue, item["id"], status=schema.FILED, expected_attempts=2,
                result_ref="wiki/notes/Real.md@deadbeef", report=real_report)

    # the FIRST worker, unaware it was ever redelivered, now tries to finish with its STALE
    # token (attempts=1) — this must raise, not silently lose the race.
    stale_report = {"status": schema.REJECTED, "summary": "a stale worker's report — must not win"}
    try:
        queue.finish(clean_queue, item["id"], status=schema.REJECTED, expected_attempts=1,
                    result_ref="", error="stale rejection", report=stale_report)
        raised = False
    except QueueStateError:
        raised = True
    assert raised, "a stale expected_attempts finish() must raise QueueStateError, not succeed"

    # the row still carries exactly what the LEGITIMATE (attempts=2) worker wrote — status,
    # result_ref AND report, all three, none of them quietly overwritten by the stale write.
    row = queue.get_submission_trace(clean_queue, item["id"])
    assert row["status"] == schema.FILED
    assert row["result_ref"] == "wiki/notes/Real.md@deadbeef"
    assert row["report"] == real_report
    assert row["report"] != stale_report


def test_finish_with_no_report_argument_never_blanks_a_report_already_written(clean_queue):
    """`report=None` is COALESCEd to the existing column value (schema.py: "`None` leaves
    whatever the column held ... and never blanks a report a previous delivery wrote"). This is
    exactly the shape `release_expired`'s own exhausted-attempts call uses (`finish(..., error=
    ...)`, no `report=` at all) — but that call only ever fires on a row whose report is already
    NULL (nothing can have finished it before, or it would no longer be `claimed`), so the
    COALESCE branch has no naturally-occurring production path where it protects a REAL report
    today. That makes it exactly the kind of "a future call site, or a refactor, could silently
    invert this" guard worth pinning directly at the SQL level: a raw UPDATE stands in for
    whatever future caller re-opens a row that already carries a report, so the COALESCE formula
    itself — not a currently-reachable call sequence — is what is under test."""
    item = _submit_and_claim(clean_queue)
    written = {"status": schema.FILED, "summary": "a report a previous delivery already wrote"}
    queue.finish(clean_queue, item["id"], status=schema.FILED, expected_attempts=1,
                result_ref="wiki/notes/Once.md@cafefeed", report=written)

    # Re-open the row exactly like a fresh delivery would (status='claimed', attempts bumped),
    # WITHOUT touching `report` — standing in for a future caller reaching `finish()` again on a
    # row that already carries one, the precondition the COALESCE branch exists to protect.
    with clean_queue.cursor() as cur:
        cur.execute("UPDATE capture_queue SET status = 'claimed', attempts = attempts + 1,"
                    " finished_at = NULL WHERE id = %s", (item["id"],))

    queue.finish(clean_queue, item["id"], status=schema.FAILED, expected_attempts=2,
                error="a later delivery finishing with no report argument at all")

    row = queue.get_submission_trace(clean_queue, item["id"])
    assert row["status"] == schema.FAILED               # the new write DID apply ...
    assert row["report"] == written                      # ... but the report was never touched


# ── the SAME property, under REAL concurrency ──────────────────────────────────────────────────
# The two tests above simulate the interleaving by manually sequencing `release_expired`/
# `claim_next`/`finish` calls in a chosen order — and a retry path needs a test that actually
# loses the race, because the lease defect was found precisely where a simulated test would have
# passed. This test drives the identical scenario with two REAL threads, two REAL
# connections and real wall-clock timing instead: a worker that claims an item, is genuinely slow,
# has its lease genuinely expire while "processing", and only THEN tries to finish with its now
# stale `attempts` token — racing a second worker that reclaims and legitimately finishes the same
# row in the meantime. Nothing here is orchestrated in a fixed order; a `threading.Barrier`
# synchronizes only the START of the race, and everything after it is real concurrent execution.
def test_two_workers_racing_a_stale_lease_never_double_finishes_or_loses_the_write(clean_queue):
    """Two workers claiming and finishing CONCURRENTLY, for real — no lease stolen (the stale
    worker's finish is fenced, not silently allowed to win) and no item processed twice (the
    row's final state is the LEGITIMATE worker's write, intact)."""
    evidence = MemoryEvidenceStore()
    ack = queue.submit(clean_queue, evidence, kind="raw", material="two-worker race material",
                      hints=None, submitted_by="racer@stigmergy.test")

    visibility_s = 0.5   # short enough to race within a test's patience, long enough not to flake
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
            start_barrier.wait()                    # both threads now genuinely running together
            time.sleep(visibility_s * 4)             # "processing" long enough for the lease to expire
            try:
                queue.finish(conn, item["id"], status=schema.REJECTED,
                            expected_attempts=item["attempts"], error="stale worker, must not win")
                results["slow_finish_raised"] = False
            except QueueStateError:
                results["slow_finish_raised"] = True

    def fast_worker():
        start_barrier.wait()
        time.sleep(visibility_s * 2)                 # let the slow worker's lease actually expire
        with index_store.connect() as conn:
            # claim_next sweeps expired claims FIRST — this is the real redelivery, not a manual
            # call to release_expired standing in for one.
            item = queue.claim_next(conn, visibility_timeout_s=visibility_s)
            results["fast_claimed"] = item is not None
            if item is None:
                return
            results["fast_attempts"] = item["attempts"]
            queue.finish(conn, item["id"], status=schema.FILED,
                        expected_attempts=item["attempts"], result_ref="wiki/x.md@deadbeef")

    t1 = threading.Thread(target=slow_worker)
    t2 = threading.Thread(target=fast_worker)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    assert not t1.is_alive() and not t2.is_alive(), "a worker thread never finished — real hang"

    assert results.get("slow_claimed") is True, "the slow worker never even claimed the item"
    assert results.get("fast_claimed") is True, "the fast worker never reclaimed the redelivered item"
    assert results["fast_attempts"] > results["slow_attempts"], (
        "no genuine redelivery happened — the fast worker must have claimed a LATER delivery")
    assert results["slow_finish_raised"] is True, (
        "the stale worker's finish() was not fenced — a lease was stolen")

    row = queue.get_submission_trace(clean_queue, ack["id"])
    assert row["status"] == schema.FILED                              # the legitimate write survived
    assert row["result_ref"] == "wiki/x.md@deadbeef"
    assert row["attempts"] == results["fast_attempts"]                 # not double-processed
