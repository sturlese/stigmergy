"""`stigmergy.capture.queue` against a REAL Postgres (`docker compose up`) — the `stigmergy_test`
database, never the one a running brain serves (`tests/testdb.py`). Skips cleanly without a
database; fails loudly instead of skipping when `$STIGMERGY_TEST_DSN` is set (CI mode) — same
posture as `tests/index/test_pg_integration.py`.

Exactly-once claiming is the one property that CANNOT be proven any other way: `FOR UPDATE SKIP
LOCKED` is a real-Postgres guarantee, so the concurrency test here opens one connection PER
simulated claimer, exactly as N independent librarian workers would.
"""
import concurrent.futures

import pytest

from stigmergy.capture import ops, queue, schema
from stigmergy.capture.errors import QueueStateError
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.index import store
from tests.capture.conftest import unique_material

ALICE = "alice@example.com"
BOB = "bob@example.com"


def _submit(conn, *, material=None, hints=None, submitted_by=ALICE, kind="raw"):
    evidence = MemoryEvidenceStore()
    ack = queue.submit(conn, evidence, kind=kind, material=material or unique_material(),
                      hints=hints, submitted_by=submitted_by)
    return ack, evidence


def _row(conn, submission_id):
    with conn.cursor() as cur:
        cur.execute("SELECT id, kind, payload, blob_refs, submitted_by, hints, status, attempts,"
                    " created_at, claimed_at, finished_at, result_ref, error"
                    " FROM capture_queue WHERE id = %s", (submission_id,))
        cols = [c.name for c in cur.description]
        row = cur.fetchone()
    return dict(zip(cols, row, strict=True)) if row else None


# ── ensure_capture_schema: idempotent DDL ────────────────────────────────────────────────────────
def test_ensure_capture_schema_creates_its_own_three_tables_and_is_idempotent(conn):
    """`ensure_capture_schema` owns `capture_queue`, `job_runs` and `ingest_errors` — NOT
    `audit_log` (that table belongs to `stigmergy.server.audit.ensure_audit_table`; `schema.py`'s
    own docstring: "`audit_log` belongs to `stigmergy.server.audit`... named here because this is
    the one place the durable/disposable boundary is written down"). `DURABLE_TABLES` is the
    cross-cutting rebuild-survival contract (proven together with `ensure_audit_table` in
    `test_capture_queue_survives_stigmergy_index_rebuild` below), not a claim about what THIS
    function alone provisions."""
    schema.ensure_capture_schema(conn)   # a second call must not raise
    with conn.cursor() as cur:
        for table in ("capture_queue", "job_runs", "ingest_errors"):
            cur.execute("SELECT to_regclass(%s)", (table,))
            assert cur.fetchone()[0] == table, f"{table} missing after ensure_capture_schema"


# ── submit works end to end ─────────────────────────────────────────────────────────────────────
def test_submit_creates_a_queued_row_with_the_material_recorded(clean_queue):
    material = unique_material("submit")
    ack, evidence = _submit(clean_queue, material=material)

    assert ack["status"] == "queued"
    assert isinstance(ack["id"], int)
    row = _row(clean_queue, ack["id"])
    assert row["status"] == "queued"
    assert row["payload"]["text"] == material
    assert row["kind"] == "raw"
    assert row["attempts"] == 0
    assert row["claimed_at"] is None and row["finished_at"] is None


# ── submitted_by lands verbatim from the caller, at the primitive level ─────────────────────────
def test_submit_records_exactly_the_submitted_by_it_was_given(clean_queue):
    ack, _ = _submit(clean_queue, submitted_by="steward@example.com")
    assert ack["submitted_by"] == "steward@example.com"
    assert _row(clean_queue, ack["id"])["submitted_by"] == "steward@example.com"


# ── identical material -> two rows, one blob key ────────────────────────────────────────────────
def test_submit_of_identical_material_twice_yields_two_rows_and_one_blob_key(clean_queue):
    material = unique_material("dedup")
    evidence = MemoryEvidenceStore()
    ack1 = queue.submit(clean_queue, evidence, kind="raw", material=material, hints=None,
                       submitted_by=ALICE)
    ack2 = queue.submit(clean_queue, evidence, kind="raw", material=material, hints=None,
                       submitted_by=ALICE)
    assert ack1["id"] != ack2["id"]
    assert ack1["blob_refs"] == ack2["blob_refs"]
    assert len(evidence.objects) == 1


# ── write order (blob before row): a failing evidence store must leave no row ───────────────────
class _FailingEvidenceStore:
    def put(self, data: bytes) -> str:
        raise RuntimeError("evidence store down")


def test_a_failed_blob_write_creates_no_queue_row(clean_queue):
    with pytest.raises(RuntimeError, match="evidence store down"):
        queue.submit(clean_queue, _FailingEvidenceStore(), kind="raw", material=unique_material(),
                    hints=None, submitted_by=ALICE)
    with clean_queue.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 0


# ── exactly-once claiming under real concurrency ────────────────────────────────────────────────
def test_claim_next_is_exactly_once_under_n_parallel_claimers(clean_queue):
    queued = 12
    for _ in range(queued):
        _submit(clean_queue, submitted_by=ALICE)

    def claim_once(_i):
        with store.connect() as worker_conn:
            item = queue.claim_next(worker_conn)
            return item["id"] if item else None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(claim_once, range(queued + 5)))   # more claimers than rows
    claimed_ids = [c for c in claimed if c is not None]

    assert len(claimed_ids) == queued                  # exactly M claims for M queued rows
    assert len(set(claimed_ids)) == queued              # no row claimed twice


def test_claim_next_returns_none_when_the_queue_is_empty(clean_queue):
    assert queue.claim_next(clean_queue) is None


def test_claim_next_takes_the_oldest_row_first(clean_queue):
    ack1, _ = _submit(clean_queue)
    ack2, _ = _submit(clean_queue)
    first = queue.claim_next(clean_queue)
    assert first["id"] == ack1["id"]
    second = queue.claim_next(clean_queue)
    assert second["id"] == ack2["id"]


def test_claim_next_increments_attempts(clean_queue):
    ack, _ = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    assert claimed["id"] == ack["id"]
    assert claimed["attempts"] == 1
    assert claimed["status"] == "claimed"
    assert claimed["claimed_at"] is not None


# ── a dead worker loses nothing — expiry + attempts increment on redelivery ─────────────────────
def test_an_expired_claim_is_returned_to_the_queue_with_attempts_kept(clean_queue):
    ack, _ = _submit(clean_queue)
    first_claim = queue.claim_next(clean_queue, visibility_timeout_s=300)
    assert first_claim["attempts"] == 1

    result = queue.release_expired(clean_queue, visibility_timeout_s=0)   # everything is "expired"
    assert result == {"released": 1, "failed": 0}
    row = _row(clean_queue, ack["id"])
    assert row["status"] == "queued"
    assert row["claimed_at"] is None
    assert row["attempts"] == 1                      # attempts counts deliveries, kept on release


def test_reclaiming_an_expired_row_increments_attempts_again_on_redelivery(clean_queue):
    ack, _ = _submit(clean_queue)
    queue.claim_next(clean_queue, visibility_timeout_s=0)             # attempts -> 1
    second_claim = queue.claim_next(clean_queue, visibility_timeout_s=0)   # sweep first, then claim
    assert second_claim is not None
    assert second_claim["id"] == ack["id"]
    assert second_claim["attempts"] == 2


def test_release_expired_refuses_to_guess_a_horizon(clean_queue):
    """OLD BEHAVIOUR: `visibility_timeout_s` defaulted to 300s here, so a caller that could not
    know the worker's lease got a plausible-looking number instead of an error. The admin console
    took that default and requeued captures out from under running workers (their lease is 900s).
    `claim_next` keeps its default — a claimer states the lease it is TAKING and does know it."""
    with pytest.raises(TypeError):
        queue.release_expired(clean_queue)          # type: ignore[call-arg]


def test_release_expired_is_a_noop_when_nothing_is_stale(clean_queue):
    _submit(clean_queue)
    queue.claim_next(clean_queue, visibility_timeout_s=300)   # generous timeout: not expired
    result = queue.release_expired(clean_queue, visibility_timeout_s=300)
    assert result == {"released": 0, "failed": 0}
    with clean_queue.cursor() as cur:
        cur.execute("SELECT job FROM job_runs WHERE job = 'capture-reclaim'")
        assert cur.fetchall() == []   # a no-op sweep stays silent — it never drowns the table


def test_attempts_exhausted_moves_the_row_to_failed_and_records_an_ingest_error(clean_queue):
    ack, _ = _submit(clean_queue)
    for _ in range(queue.DEFAULT_MAX_ATTEMPTS):
        claimed = queue.claim_next(clean_queue, visibility_timeout_s=0, max_attempts=queue.DEFAULT_MAX_ATTEMPTS)
        assert claimed is not None
    result = queue.release_expired(clean_queue, visibility_timeout_s=0,
                                   max_attempts=queue.DEFAULT_MAX_ATTEMPTS)
    assert result["failed"] == 1

    row = _row(clean_queue, ack["id"])
    assert row["status"] == "failed"
    assert row["attempts"] == queue.DEFAULT_MAX_ATTEMPTS
    assert row["finished_at"] is not None

    with clean_queue.cursor() as cur:
        cur.execute("SELECT stage, attempts FROM ingest_errors WHERE source_doc_id = %s",
                    (str(ack["id"]),))
        stage, attempts = cur.fetchone()
    assert stage == "claim"
    assert attempts == queue.DEFAULT_MAX_ATTEMPTS


# ── finish(): the only transition helper, fenced by `expected_attempts` ─────────────────────────
def test_finish_into_a_terminal_status_stamps_finished_at_and_keeps_claimed_at(clean_queue):
    ack, _ = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    result = queue.finish(clean_queue, ack["id"], status=schema.FILED,
                         expected_attempts=claimed["attempts"], result_ref="wiki/x.md")
    assert result == {"id": ack["id"], "status": "filed", "result_ref": "wiki/x.md",
                      "attempts": claimed["attempts"]}
    row = _row(clean_queue, ack["id"])
    assert row["status"] == "filed"
    assert row["result_ref"] == "wiki/x.md"
    assert row["finished_at"] is not None
    assert row["claimed_at"] is not None    # terminal: claimed_at is KEPT, not nulled


def test_finish_into_a_parked_status_leaves_finished_at_null_and_drops_claimed_at(clean_queue):
    ack, _ = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.NEEDS_INPUT,
                expected_attempts=claimed["attempts"], error="which entity is this about?")
    row = _row(clean_queue, ack["id"])
    assert row["status"] == "needs_input"
    assert row["finished_at"] is None
    assert row["claimed_at"] is None
    assert row["error"] == "which entity is this about?"


# ── `outcome` — stored across a park, cleared unconditionally on every terminal status ──────────
def _outcome(conn, submission_id):
    with conn.cursor() as cur:
        cur.execute("SELECT outcome FROM capture_queue WHERE id = %s", (submission_id,))
        return cur.fetchone()[0]


def test_finish_into_a_parked_status_stores_the_outcome_it_is_given(clean_queue):
    """The write half — `tests/librarian/test_meeting_processing_pg.py` proves this through the
    full meeting pipeline (`test_a_park_keeps_the_distillation_it_produced`); this is the same
    property at the primitive `queue.finish` reaches, with no agent, no gates and no git in the
    way, matching this file's own posture (the DATABASE PRIMITIVE, not the flow around it)."""
    ack, _ = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    stored = {"version": 1, "raw": {"decisions": [{"title": "a decision worth keeping"}]}}
    queue.finish(clean_queue, ack["id"], status=schema.TRIAGE, expected_attempts=claimed["attempts"],
                error="which entity is this about?", outcome=stored)
    assert _outcome(clean_queue, ack["id"]) == stored


def test_finish_with_no_outcome_argument_never_blanks_one_already_stored(clean_queue):
    """`outcome=None` follows `report`'s own COALESCE convention (`finish`'s docstring): a caller
    with nothing new to say about the outcome must not blank what a previous delivery stored — the
    same shape `test_finish_fencing_pg.py` pins directly at the SQL level for `report`, applied
    here to the sibling column that joined the same COALESCE formula."""
    ack, _ = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    stored = {"version": 1, "raw": {"decisions": [{"title": "kept across a second delivery"}]}}
    queue.finish(clean_queue, ack["id"], status=schema.TRIAGE, expected_attempts=claimed["attempts"],
                error="which entity?", outcome=stored)

    # a second delivery re-parks the SAME row with no outcome argument at all — standing in for a
    # pass that re-hits the ordinary `needs_input` ask path, which never touches this column.
    with clean_queue.cursor() as cur:
        cur.execute("UPDATE capture_queue SET status = 'claimed', attempts = attempts + 1"
                    " WHERE id = %s", (ack["id"],))
    queue.finish(clean_queue, ack["id"], status=schema.NEEDS_INPUT,
                expected_attempts=claimed["attempts"] + 1, error="a different, later question")
    assert _outcome(clean_queue, ack["id"]) == stored


@pytest.mark.parametrize("terminal_status", [schema.FILED, schema.REJECTED, schema.FAILED])
def test_finish_into_a_terminal_status_clears_outcome_even_when_one_is_passed(clean_queue,
                                                                              terminal_status):
    """The half that actually matters (`finish`'s own docstring: "cleared on every terminal
    status... which is the half that matters"): once a row is `filed`, `rejected` or `failed`, a
    stored distillation must never survive it — the retention property that same docstring is
    built on ("keeping the full drafted text of every page beside a closed row is exactly the
    accumulation retention exists to prevent").

    A non-`None` `outcome` is passed ALONGSIDE the terminal status here on purpose: `finish`'s own
    docstring says the clear "takes precedence over any value passed in", and the weaker version of
    this test (finishing terminal with `outcome=None`, which any caller finishing FILED/REJECTED/
    FAILED does today since nothing ever passes an outcome on those paths) would pass even if that
    CASE WHEN precedence were reversed — it would still read as NULL via plain COALESCE-of-None.
    Passing a real value is what actually exercises the unconditional clear."""
    ack, _ = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    stored = {"version": 1, "raw": {"decisions": [{"title": "must not survive a terminal finish"}]}}
    queue.finish(clean_queue, ack["id"], status=schema.TRIAGE, expected_attempts=claimed["attempts"],
                error="which entity?", outcome=stored)
    assert _outcome(clean_queue, ack["id"]) == stored   # sanity: it really was stored first

    with clean_queue.cursor() as cur:
        cur.execute("UPDATE capture_queue SET status = 'claimed', attempts = attempts + 1"
                    " WHERE id = %s", (ack["id"],))
    queue.finish(clean_queue, ack["id"], status=terminal_status,
                expected_attempts=claimed["attempts"] + 1,
                outcome={"version": 1, "raw": {"decisions": [{"title": "a value passed anyway"}]}})
    assert _outcome(clean_queue, ack["id"]) is None, (
        f"a stored outcome survived a finish() into {terminal_status!r} — the terminal clear must "
        f"win over any outcome value the caller passes, not just over a COALESCE(None, ...)")


def test_finish_rejects_a_status_outside_finished_statuses(clean_queue):
    ack, _ = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    with pytest.raises(QueueStateError, match="cannot finish into"):
        queue.finish(clean_queue, ack["id"], status=schema.QUEUED,
                    expected_attempts=claimed["attempts"])


def test_finish_raises_when_the_row_was_never_claimed(clean_queue):
    ack, _ = _submit(clean_queue)
    with pytest.raises(QueueStateError, match="not claimed"):
        queue.finish(clean_queue, ack["id"], status=schema.FILED, expected_attempts=0)


def test_finish_raises_when_the_claim_already_expired_out_from_under_the_caller(clean_queue):
    """The lost-race case (module docstring), asserted precisely: a claim requeued by the
    visibility timeout while this worker still believed it held it must fail LOUDLY, never
    silently update nothing.

    After `release_expired` the row is back to `queued` (nobody has re-claimed it YET) — so this
    takes the generic "not claimed by this worker" branch, not the "redelivered to worker B"
    branch (that one needs a SECOND claim to have actually happened; see
    `test_a_finish_against_a_redelivered_row_is_refused_and_the_new_owners_finish_still_applies`
    below for the fence's own load-bearing case, where a second claim DOES happen)."""
    ack, _ = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue, visibility_timeout_s=0)
    queue.release_expired(clean_queue, visibility_timeout_s=0)   # requeues it out from under us
    with pytest.raises(QueueStateError, match=r"is 'queued', not claimed by this worker"):
        queue.finish(clean_queue, ack["id"], status=schema.FILED,
                    expected_attempts=claimed["attempts"])


# ── the fence itself: a stale delivery must never finish somebody else's claim ──────────────────
def test_a_finish_against_a_redelivered_row_is_refused_and_the_new_owners_finish_still_applies(
        clean_queue):
    """Worker A claims (attempts=1); A's lease expires; the sweep requeues; worker B claims
    (attempts=2) — the exact sequence the module docstring's `_FINISH` comment describes as the
    hole `status = 'claimed'` alone left open. A's stale `finish()` must be refused (it is no
    longer A's delivery), and it must NOT silently steal B's item; B's own `finish()`, using ITS
    delivery's `attempts`, must still succeed."""
    ack, _ = _submit(clean_queue)
    worker_a = queue.claim_next(clean_queue, visibility_timeout_s=0)
    assert worker_a["attempts"] == 1

    queue.release_expired(clean_queue, visibility_timeout_s=0)   # A's lease expires, unbeknownst to A
    worker_b = queue.claim_next(clean_queue, visibility_timeout_s=300)
    assert worker_b["id"] == ack["id"]
    assert worker_b["attempts"] == 2                             # a second, distinct delivery

    # A, still believing it owns the item, tries to finish with ITS (stale) delivery number
    with pytest.raises(QueueStateError, match="redelivered"):
        queue.finish(clean_queue, ack["id"], status=schema.FILED,
                    expected_attempts=worker_a["attempts"], result_ref="a-stole-this.md")

    # the row is untouched by A's refused write — still claimed, on B's delivery
    row = _row(clean_queue, ack["id"])
    assert row["status"] == "claimed" and row["attempts"] == 2
    assert row["result_ref"] == ""                                # A's result_ref never landed

    # B's own finish, with the CORRECT (current) delivery number, applies cleanly
    result = queue.finish(clean_queue, ack["id"], status=schema.FILED,
                         expected_attempts=worker_b["attempts"], result_ref="b-filed-this.md")
    assert result == {"id": ack["id"], "status": "filed", "result_ref": "b-filed-this.md",
                      "attempts": 2}
    assert _row(clean_queue, ack["id"])["result_ref"] == "b-filed-this.md"


# ── the listing query and its two entry points, at the primitive level ──────────────────────────
def test_list_own_submissions_is_scoped_to_the_submitter(clean_queue):
    a1, _ = _submit(clean_queue, submitted_by=ALICE)
    a2, _ = _submit(clean_queue, submitted_by=ALICE)
    _submit(clean_queue, submitted_by=BOB)

    own = queue.list_own_submissions(clean_queue, ALICE)
    assert {row["id"] for row in own} == {a1["id"], a2["id"]}
    assert all(row["submitted_by"] == ALICE for row in own)


def test_list_own_submissions_requires_a_non_empty_submitter(clean_queue):
    with pytest.raises(ValueError, match="submitter is required"):
        queue.list_own_submissions(clean_queue, "")
    with pytest.raises(ValueError, match="submitter is required"):
        queue.list_own_submissions(clean_queue, None)


def test_list_all_submissions_sees_every_identity(clean_queue):
    _submit(clean_queue, submitted_by=ALICE)
    _submit(clean_queue, submitted_by=BOB)
    all_rows = queue.list_all_submissions(clean_queue)
    assert {row["submitted_by"] for row in all_rows} == {ALICE, BOB}


def test_query_submissions_filters_by_status(clean_queue):
    a1, _ = _submit(clean_queue, submitted_by=ALICE)
    _submit(clean_queue, submitted_by=ALICE)
    queue.claim_next(clean_queue)   # claims the oldest (a1)

    queued_only = queue.query_submissions(clean_queue, submitter=ALICE, statuses=["queued"])
    claimed_only = queue.query_submissions(clean_queue, submitter=ALICE, statuses=["claimed"])
    assert all(r["status"] == "queued" for r in queued_only)
    assert [r["id"] for r in claimed_only] == [a1["id"]]


def test_query_submissions_rejects_an_unknown_status(clean_queue):
    with pytest.raises(ValueError, match="unknown status"):
        queue.query_submissions(clean_queue, statuses=["bogus"])


def test_query_submissions_excerpt_is_truncated_in_postgres(clean_queue):
    material = "x" * 1000
    ack, _ = _submit(clean_queue, material=material, submitted_by=ALICE)
    # a `queued` row's excerpt is withheld — the gate has not looked at it
    # yet. Move it past the gate (`filed`) before asserting the excerpt is visible at all, so this
    # stays a test of TRUNCATION and not an accidental re-test of the withholding rule above it.
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.FILED,
                expected_attempts=claimed["attempts"], result_ref="wiki/x.md")
    rows = queue.query_submissions(clean_queue, submitter=ALICE, excerpt_chars=10)
    assert rows[0]["excerpt"] == "x" * 10


def test_query_submissions_a_purged_row_reports_payload_purged_true_with_no_excerpt(clean_queue):
    ack, _ = _submit(clean_queue, submitted_by=ALICE)
    with clean_queue.cursor() as cur:
        cur.execute("UPDATE capture_queue SET payload = NULL, hints = NULL WHERE id = %s",
                    (ack["id"],))
    rows = queue.query_submissions(clean_queue, submitter=ALICE)
    row = next(r for r in rows if r["id"] == ack["id"])
    assert row["payload_purged"] is True
    assert row["excerpt"] == ""


def test_query_submissions_limit_is_clamped_to_the_max():
    assert queue.MAX_LIST_LIMIT == 200   # the documented ceiling; a regression here is silent otherwise


# ── the per-submission trace ────────────────────────────────────────────────────────────────────
def test_get_submission_trace_computes_queue_wait_and_total_latency(clean_queue):
    ack, _ = _submit(clean_queue, submitted_by=ALICE)
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.FILED,
                expected_attempts=claimed["attempts"], result_ref="x.md")

    trace = queue.get_submission_trace(clean_queue, ack["id"])
    assert trace["id"] == ack["id"]
    assert trace["queue_wait_ms"] is not None and trace["queue_wait_ms"] >= 0
    assert trace["total_latency_ms"] is not None and trace["total_latency_ms"] >= 0
    assert trace["attempts"] == 1


def test_get_submission_trace_scoped_to_another_submitter_returns_none_no_existence_leak(clean_queue):
    ack, _ = _submit(clean_queue, submitted_by=ALICE)
    assert queue.get_submission_trace(clean_queue, ack["id"], submitter=BOB) is None
    # the SAME shape as a genuinely nonexistent id — no way to distinguish "not yours" from
    # "doesn't exist" (mirrors read_page's no-existence-leak posture)
    assert queue.get_submission_trace(clean_queue, 99999999, submitter=BOB) is None


def test_get_submission_trace_unscoped_finds_any_row(clean_queue):
    ack, _ = _submit(clean_queue, submitted_by=ALICE)
    trace = queue.get_submission_trace(clean_queue, ack["id"])
    assert trace is not None and trace["submitted_by"] == ALICE


# ── counts_by_status: every status present, zero included ──────────────────────────────────────
def test_counts_by_status_includes_every_status_with_zero_default(clean_queue):
    counts = queue.counts_by_status(clean_queue)
    assert set(counts) == set(schema.STATUSES)
    assert all(v == 0 for v in counts.values())

    _submit(clean_queue)
    _submit(clean_queue)
    queue.claim_next(clean_queue)
    counts = queue.counts_by_status(clean_queue)
    assert counts["queued"] == 1
    assert counts["claimed"] == 1
    assert counts["filed"] == 0


# ── the queue survives an index rebuild ─────────────────────────────────────────────────────────
def test_capture_queue_survives_stigmergy_index_rebuild(clean_queue):
    import pathlib

    from stigmergy.index import build
    from stigmergy.index.backends.embedder import build_embedder
    from stigmergy.server.audit import ensure_audit_table

    ensure_audit_table(clean_queue)   # audit_log is a DURABLE_TABLES member too; ensure it exists
    _submit(clean_queue, submitted_by=ALICE)
    _submit(clean_queue, submitted_by=BOB)
    ops.record_job_run(clean_queue, "capture-purge", stats={"purged": 0})
    ops.record_ingest_error(clean_queue, source_doc_id="1", stage="claim", error="x", attempts=1)
    rows_before = queue.counts_by_status(clean_queue)

    fixture_repo = str(pathlib.Path(__file__).resolve().parents[1] / "index" / "fixtures" / "repo")
    build.rebuild(clean_queue, fixture_repo, build_embedder("fake"))   # drops+recreates pages_index

    assert queue.counts_by_status(clean_queue) == rows_before
    with clean_queue.cursor() as cur:
        for table in schema.DURABLE_TABLES:
            cur.execute("SELECT to_regclass(%s)", (table,))
            assert cur.fetchone()[0] == table, f"{table} did not survive the rebuild"
        cur.execute("SELECT count(*) FROM job_runs")
        assert cur.fetchone()[0] >= 1
        cur.execute("SELECT count(*) FROM ingest_errors")
        assert cur.fetchone()[0] >= 1


# ── capture -> searchable latency, joined against the webhook's job_runs ────────────────────────
def test_searchable_latencies_ms_joins_a_filed_rows_result_ref_sha_to_its_webhook_job_run(
        clean_queue):
    ack, _ = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.FILED, expected_attempts=claimed["attempts"],
                result_ref="wiki/x.md@deadbeef")
    ops.record_job_run(clean_queue, "webhook-index-upsert", status="ok",
                       stats={"sha": "deadbeef", "upserted": 1})

    samples = queue.searchable_latencies_ms(clean_queue, job_name="webhook-index-upsert")
    assert len(samples) == 1
    assert samples[0] >= 0


# ── a REDELIVERED webhook must not double-count one capture ─────────────────────────────────────
def test_searchable_latencies_ms_does_not_double_count_a_redelivered_webhook(clean_queue):
    """`_SELECT_SEARCHABLE_LATENCIES` had no `DISTINCT ON (cq.id)` — two `job_runs` rows for the
    SAME sha (GitHub redelivering the identical push) joined against the SAME `filed` row twice,
    inflating the sample count (and therefore p95) for one real capture. Exactly one sample must
    land per `filed` row, however many times its webhook run was recorded."""
    ack, _ = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.FILED, expected_attempts=claimed["attempts"],
                result_ref="wiki/x.md@redelivered1")
    # the SAME push delivered twice: two ok job_runs rows for the identical sha
    ops.record_job_run(clean_queue, "webhook-index-upsert", status="ok",
                       stats={"sha": "redelivered1", "upserted": 1})
    ops.record_job_run(clean_queue, "webhook-index-upsert", status="ok",
                       stats={"sha": "redelivered1", "upserted": 0})

    samples = queue.searchable_latencies_ms(clean_queue, job_name="webhook-index-upsert")
    assert len(samples) == 1   # one capture, one sample — never one per job_runs row


def test_searchable_latencies_ms_ignores_a_filed_row_with_no_matching_webhook_run(clean_queue):
    """A row filed via the nightly rebuild reconciliation path, or from before the webhook path
    existed, has no webhook `job_runs` row for its sha — it contributes NO sample, silently,
    rather than a guessed one."""
    ack, _ = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.FILED, expected_attempts=claimed["attempts"],
                result_ref="wiki/x.md@nomatchingsha")

    assert queue.searchable_latencies_ms(clean_queue, job_name="webhook-index-upsert") == []


def test_searchable_latencies_ms_ignores_a_different_jobs_run_with_the_same_sha_by_coincidence(
        clean_queue):
    ack, _ = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.FILED, expected_attempts=claimed["attempts"],
                result_ref="wiki/x.md@cafefeed")
    ops.record_job_run(clean_queue, "capture-purge", status="ok", stats={"sha": "cafefeed"})

    assert queue.searchable_latencies_ms(clean_queue, job_name="webhook-index-upsert") == []


def test_searchable_latencies_ms_ignores_a_failed_webhook_run(clean_queue):
    ack, _ = _submit(clean_queue)
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.FILED, expected_attempts=claimed["attempts"],
                result_ref="wiki/x.md@aaaa1111")
    ops.record_job_run(clean_queue, "webhook-index-upsert", status="error",
                       stats={"sha": "aaaa1111"}, error="RuntimeError")

    assert queue.searchable_latencies_ms(clean_queue, job_name="webhook-index-upsert") == []
