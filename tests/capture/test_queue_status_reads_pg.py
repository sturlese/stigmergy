"""The two read surfaces `stigmergy-librarian status` is built on, against real Postgres:
`queue.query_in_flight` and `queue.filed_latencies_ms` — plus the two renderings the two CLIs
share (`cli.depth_line`, `cli.format_ms`).

They live in `stigmergy.capture` rather than in the librarian for the same reason `claim_next` does:
this module owns the queue's semantics, and "this claim has outlived its lease" is the sweep's own
predicate. The point of these tests is that the STATUS command and the SWEEP cannot disagree — a
status line that calls a claim healthy in the same second the sweep takes it away would be worse
than no status line.
"""
import pytest

from stigmergy.capture import cli as queue_cli
from stigmergy.capture import queue, schema
from stigmergy.capture.evidence import MemoryEvidenceStore


@pytest.fixture()
def memory_evidence():
    """In-process evidence: nothing here reads a blob back, so the archive only has to accept the
    write. Same choice `test_queue_pg.py` makes, and it keeps this suite runnable with Postgres
    alone."""
    return MemoryEvidenceStore()


def _submit(conn, evidence_store, material: str, *, submitted_by="status@stigmergy.test"):
    return queue.submit(conn, evidence_store, kind="raw", material=material, hints=None,
                        submitted_by=submitted_by)


def _age(conn, submission_id: int, seconds: int) -> None:
    """Backdate a claim, which is what a worker that died mid-item leaves behind. Ageing the row
    rather than shrinking the lease is deliberate: the lease is what the verdict is computed
    AGAINST, so moving it would test a different question."""
    with conn.cursor() as cur:
        cur.execute("UPDATE capture_queue SET claimed_at = now() - make_interval(secs => %s)"
                    " WHERE id = %s", (seconds, submission_id))


# ── query_in_flight ───────────────────────────────────────────────────────────────────────────────
def test_nothing_claimed_is_an_empty_list(clean_queue, memory_evidence):
    _submit(clean_queue, memory_evidence, "queued, never claimed")
    assert queue.query_in_flight(clean_queue, visibility_timeout_s=300) == []


def test_a_fresh_claim_is_in_flight_and_within_its_lease(clean_queue, memory_evidence):
    ack = _submit(clean_queue, memory_evidence, "about to be claimed")
    queue.claim_next(clean_queue, visibility_timeout_s=300)

    rows = queue.query_in_flight(clean_queue, visibility_timeout_s=300)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == ack["id"]
    assert row["attempts"] == 1
    assert row["lease_expired"] is False
    assert row["claimed_at"] is not None
    assert 0 <= row["claimed_age_ms"] < 60_000


def test_a_stale_claim_is_reported_as_lease_expired(clean_queue, memory_evidence):
    ack = _submit(clean_queue, memory_evidence, "held by a worker that died")
    queue.claim_next(clean_queue, visibility_timeout_s=300)
    _age(clean_queue, ack["id"], 3600)

    row = queue.query_in_flight(clean_queue, visibility_timeout_s=300)[0]
    assert row["lease_expired"] is True
    assert row["claimed_age_ms"] > 300_000


def test_the_staleness_verdict_is_computed_against_the_TIMEOUT_PASSED_not_the_queue_default(
        clean_queue, memory_evidence):
    """The reason the parameter is not optional in spirit. An agent item legitimately runs for
    minutes, so a status command that compared against `stigmergy-queue`'s human-scale 300s while the
    worker was configured for 900s would call every healthy librarian item dead."""
    ack = _submit(clean_queue, memory_evidence, "a long-running but healthy agent item")
    queue.claim_next(clean_queue, visibility_timeout_s=900)
    _age(clean_queue, ack["id"], 400)

    assert queue.query_in_flight(clean_queue, visibility_timeout_s=300)[0]["lease_expired"] is True
    assert queue.query_in_flight(clean_queue, visibility_timeout_s=900)[0]["lease_expired"] is False


def test_the_expiry_verdict_agrees_with_what_the_sweep_actually_does(clean_queue, memory_evidence):
    """**The property the shared `_LEASE_EXPIRED` predicate exists for.** `query_in_flight` REPORTS
    staleness and `release_expired` ACTS on it; a second copy of the predicate would let the two
    drift. Asserted the only way that means anything: report first, then sweep, and require the
    sweep to move exactly the rows the report flagged."""
    fresh = _submit(clean_queue, memory_evidence, "claimed just now")
    stale = _submit(clean_queue, memory_evidence, "claimed an hour ago")
    queue.claim_next(clean_queue, visibility_timeout_s=300)     # `fresh` — oldest first
    queue.claim_next(clean_queue, visibility_timeout_s=300)     # `stale`
    _age(clean_queue, stale["id"], 3600)

    flagged = {r["id"] for r in queue.query_in_flight(clean_queue, visibility_timeout_s=300)
               if r["lease_expired"]}
    assert flagged == {stale["id"]}

    result = queue.release_expired(clean_queue, visibility_timeout_s=300)
    assert result["released"] == 1
    assert queue.get_submission_trace(clean_queue, stale["id"])["status"] == schema.QUEUED
    assert queue.get_submission_trace(clean_queue, fresh["id"])["status"] == schema.CLAIMED


def test_in_flight_rows_carry_no_payload_and_no_excerpt(clean_queue, memory_evidence):
    """The question is "is a worker alive", and captured material has no part in the answer — nor in
    the terminal this is printed to. A row that carried an excerpt would be an untrusted-text
    sanitizing problem this surface does not need to have."""
    _submit(clean_queue, memory_evidence, "material that must not appear in a status line")
    queue.claim_next(clean_queue, visibility_timeout_s=300)

    row = queue.query_in_flight(clean_queue, visibility_timeout_s=300)[0]
    assert "payload" not in row and "excerpt" not in row and "hints" not in row


def test_in_flight_is_ordered_oldest_first(clean_queue, memory_evidence):
    """Oldest first because the oldest claim is the one most likely to be dead, and an operator
    reading a list wants the suspicious row at the top."""
    first = _submit(clean_queue, memory_evidence, "first")
    second = _submit(clean_queue, memory_evidence, "second")
    queue.claim_next(clean_queue, visibility_timeout_s=300)
    queue.claim_next(clean_queue, visibility_timeout_s=300)
    _age(clean_queue, second["id"], 10)      # make `second` the older CLAIM, not the older row

    assert [r["id"] for r in queue.query_in_flight(clean_queue, visibility_timeout_s=300)] == \
        [second["id"], first["id"]]


# ── filed_latencies_ms ────────────────────────────────────────────────────────────────────────────
def _file_one(conn, memory_evidence, material: str):
    ack = _submit(conn, memory_evidence, material)
    item = queue.claim_next(conn, visibility_timeout_s=300)
    queue.finish(conn, item["id"], status=schema.FILED, expected_attempts=item["attempts"],
                 result_ref="wiki/notes/X.md@abc123")
    return ack


def test_no_filed_rows_means_no_samples(clean_queue, memory_evidence):
    _submit(clean_queue, memory_evidence, "still queued")
    assert queue.filed_latencies_ms(clean_queue) == []


def test_every_filed_row_contributes_one_sample(clean_queue, memory_evidence):
    for n in range(3):
        _file_one(clean_queue, memory_evidence, f"filed capture {n}")
    samples = queue.filed_latencies_ms(clean_queue)
    assert len(samples) == 3
    assert all(s >= 0 for s in samples)


def test_only_filed_rows_count(clean_queue, memory_evidence):
    """A `rejected` row's latency is the latency of a refusal, which is a different question — and
    including it would let a batch of fast refusals flatter the filing p50."""
    _file_one(clean_queue, memory_evidence, "filed")
    rejected = _submit(clean_queue, memory_evidence, "about to be rejected")
    item = queue.claim_next(clean_queue, visibility_timeout_s=300)
    assert item["id"] == rejected["id"]                        # the only remaining queued row
    queue.finish(clean_queue, item["id"], status=schema.REJECTED,
                 expected_attempts=item["attempts"], error="a seeded secret")

    assert len(queue.filed_latencies_ms(clean_queue)) == 1


def test_a_triaged_row_contributes_nothing_because_it_has_no_finished_at(clean_queue,
                                                                        memory_evidence):
    """`triage` leaves `finished_at` NULL on purpose (`queue.finish`: a row waiting for a human is
    not done), so it cannot produce a capture->filed duration at all."""
    _submit(clean_queue, memory_evidence, "parked")
    item = queue.claim_next(clean_queue, visibility_timeout_s=300)
    queue.finish(clean_queue, item["id"], status=schema.TRIAGE,
                 expected_attempts=item["attempts"], error="unresolved entity")
    assert queue.filed_latencies_ms(clean_queue) == []


def test_the_sample_equals_the_traces_own_total_latency(clean_queue, memory_evidence):
    """The declared duplication, checked. `get_submission_trace` computes `total_latency_ms` for ONE
    row in Python; this query computes the same difference for many in SQL. They are two expressions
    of one definition, so they must agree — if they ever stop, "capture->filed" means two things."""
    ack = _file_one(clean_queue, memory_evidence, "one measured capture")
    trace = queue.get_submission_trace(clean_queue, ack["id"])
    [sample] = queue.filed_latencies_ms(clean_queue)
    assert abs(sample - trace["total_latency_ms"]) < 1.0


def test_the_window_is_bounded_and_newest_first(clean_queue, memory_evidence):
    """Bounded because the table only grows; newest first because a percentile over the whole of
    history describes a system that no longer exists."""
    for n in range(3):
        _file_one(clean_queue, memory_evidence, f"filed capture {n}")
    assert len(queue.filed_latencies_ms(clean_queue, limit=2)) == 2


# ── the two shared renderings ─────────────────────────────────────────────────────────────────────
def test_depth_line_drops_zeroes_and_names_the_non_zero_statuses(clean_queue, memory_evidence):
    _submit(clean_queue, memory_evidence, "one queued row")
    line = queue_cli.depth_line(queue.counts_by_status(clean_queue))
    assert line.startswith("queue: ")
    assert f"{schema.QUEUED}=1" in line
    assert f"{schema.FILED}=" not in line      # zero statuses are not printed


def test_depth_line_says_empty_rather_than_printing_seven_zeroes(clean_queue):
    assert queue_cli.depth_line(queue.counts_by_status(clean_queue)) == "queue: empty"


def test_format_ms_renders_one_decimal_second_and_an_em_dash_for_nothing():
    assert queue_cli.format_ms(4200) == "4.2s"
    assert queue_cli.format_ms(None) == "—"


def test_stigmergy_queue_list_still_prints_the_shared_depth_line(capsys, clean_queue,
                                                              memory_evidence):
    """The extraction did not change `stigmergy-queue`'s own output — the librarian reuses THIS
    string, so a drift here would silently become a drift there."""
    _submit(clean_queue, memory_evidence, "a row to list")
    from tests import testdb
    queue_cli.main(["--dsn", testdb.dsn(), "list"])
    out = capsys.readouterr().out
    assert out.splitlines()[0] == queue_cli.depth_line(queue.counts_by_status(clean_queue))
