"""`stigmergy.capture.ops` — the operational spine writers: `job_runs` and `ingest_errors`, plus the
`job_run` context manager both `retention.purge` and the librarian use. Real Postgres for the
happy paths; a genuinely CLOSED connection (a real failure mode, not a hand-rolled double of
internal logic) proves the swallow-and-log guarantee, mirroring
`tests/server/test_audit.py::test_write_failure_is_logged_loudly_and_swallowed_not_raised`."""
import logging

import pytest

from stigmergy.capture import ops


def test_record_job_run_inserts_a_queryable_row(clean_queue):
    job_id = ops.record_job_run(clean_queue, "capture-reclaim", stats={"released": 3})
    assert job_id is not None
    with clean_queue.cursor() as cur:
        cur.execute("SELECT job, status, stats, error FROM job_runs WHERE id = %s", (job_id,))
        job, status, stats, error = cur.fetchone()
    assert job == "capture-reclaim"
    assert status == "ok"
    assert stats == {"released": 3}
    assert error == ""


def test_record_job_run_can_record_an_explicit_error_status(clean_queue):
    job_id = ops.record_job_run(clean_queue, "capture-reclaim", status="error", stats={},
                                error="RuntimeError")
    with clean_queue.cursor() as cur:
        cur.execute("SELECT status, error FROM job_runs WHERE id = %s", (job_id,))
        status, error = cur.fetchone()
    assert status == "error"
    assert error == "RuntimeError"


def test_record_ingest_error_inserts_a_queryable_row(clean_queue):
    error_id = ops.record_ingest_error(clean_queue, source_doc_id="42", stage="claim",
                                       error="claim expired", attempts=3)
    assert error_id is not None
    with clean_queue.cursor() as cur:
        cur.execute("SELECT source, source_doc_id, stage, error, attempts, resolved"
                    " FROM ingest_errors WHERE id = %s", (error_id,))
        source, doc_id, stage, error, attempts, resolved = cur.fetchone()
    assert source == ops.SOURCE_CAPTURE_QUEUE
    assert doc_id == "42"
    assert stage == "claim"
    assert error == "claim expired"
    assert attempts == 3
    assert resolved is False


def test_record_ingest_error_accepts_a_custom_source(clean_queue):
    ops.record_ingest_error(clean_queue, source_doc_id="x", stage="s", error="e", attempts=1,
                            source="other-source")
    with clean_queue.cursor() as cur:
        cur.execute("SELECT source FROM ingest_errors WHERE source_doc_id = 'x'")
        assert cur.fetchone()[0] == "other-source"


# ── job_run(): the context manager both retention and the librarian use ────────────────────────
def test_job_run_context_manager_records_ok_with_the_yielded_stats(clean_queue):
    with ops.job_run(clean_queue, "capture-purge") as stats:
        stats["purged"] = 7

    with clean_queue.cursor() as cur:
        cur.execute("SELECT status, stats FROM job_runs WHERE job = 'capture-purge'"
                    " ORDER BY id DESC LIMIT 1")
        status, recorded = cur.fetchone()
    assert status == "ok"
    assert recorded == {"purged": 7}


def test_job_run_context_manager_records_error_and_reraises(clean_queue):
    with pytest.raises(RuntimeError, match="boom"), ops.job_run(clean_queue, "capture-purge") as stats:
        stats["partial"] = True
        raise RuntimeError("boom")

    with clean_queue.cursor() as cur:
        cur.execute("SELECT status, error, stats FROM job_runs WHERE job = 'capture-purge'"
                    " ORDER BY id DESC LIMIT 1")
        status, error, recorded = cur.fetchone()
    assert status == "error"
    assert error == "RuntimeError"          # the CLASS name, never str(ex) — never content
    assert recorded == {"partial": True}    # partial stats survive the exception


# ── the write-failure-must-never-fail-the-work guarantee: observability must not take down the
# thing it observes — the same posture `server.audit.AuditWriter` takes ─────────────────────────
class _ClosedConn:
    """A connection that is genuinely unusable — the real failure mode `record_job_run`/
    `record_ingest_error` are guarding against (a dead connection mid-run), not a hand-rolled
    mock of internal logic: `cursor()` on a closed psycopg connection raises for real."""

    def cursor(self):
        raise RuntimeError("connection already closed")


def test_record_job_run_swallows_a_write_failure_and_logs_it(caplog):
    with caplog.at_level(logging.ERROR):
        result = ops.record_job_run(_ClosedConn(), "capture-purge", stats={"purged": 1})
    assert result is None
    assert any("job_runs write failed" in r.message for r in caplog.records)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_record_ingest_error_swallows_a_write_failure_and_logs_it(caplog):
    with caplog.at_level(logging.ERROR):
        result = ops.record_ingest_error(_ClosedConn(), source_doc_id="1", stage="claim",
                                         error="e", attempts=1)
    assert result is None
    assert any("ingest_errors write failed" in r.message for r in caplog.records)


def test_job_run_bookkeeping_failure_never_masks_the_original_exception(caplog):
    """The stronger claim (module docstring): a failure to WRITE the bookkeeping must never fail
    the work it was recording — proven here by making BOTH the work AND the bookkeeping fail, and
    checking the ORIGINAL exception is what propagates, not a bookkeeping-write error."""
    with (caplog.at_level(logging.ERROR), pytest.raises(ValueError, match="the real failure"),
         ops.job_run(_ClosedConn(), "capture-reclaim") as stats):
        stats["x"] = 1
        raise ValueError("the real failure")
    assert any("job_runs write failed" in r.message for r in caplog.records)
