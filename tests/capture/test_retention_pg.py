"""`stigmergy.capture.retention` against real Postgres: `purge` nulls `payload`/`hints` on terminal
rows older than the retention window, while `id`, `submitted_by`, `status`, the three timestamps,
`attempts` and `result_ref` survive. Asserted by a test, never by inspection."""
from psycopg.types.json import Jsonb

from stigmergy.capture import queue, retention, schema
from stigmergy.capture.evidence import MemoryEvidenceStore
from tests.capture.conftest import unique_material

ALICE = "alice@example.com"


def _terminal_row(conn, *, status: str, finished_days_ago: int, result_ref: str = ""):
    """Submit, claim and finish one row, then backdate `finished_at` directly (SQL) — the ONLY
    way to get a row that is genuinely 'terminal for more than 30 days' without a real 30-day
    wait, exactly like backdating a fixture row in any retention/TTL test."""
    ack = queue.submit(conn, MemoryEvidenceStore(), kind="raw", material=unique_material(),
                       hints={"title": "t"}, submitted_by=ALICE)
    claimed = queue.claim_next(conn)
    queue.finish(conn, ack["id"], status=status, expected_attempts=claimed["attempts"],
                result_ref=result_ref)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE capture_queue SET finished_at = now() - make_interval(days => %s) WHERE id = %s",
            (finished_days_ago, ack["id"]))
    return ack["id"]


def _row(conn, submission_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload, hints, id, submitted_by, status, created_at, claimed_at, finished_at,"
            " result_ref FROM capture_queue WHERE id = %s", (submission_id,))
        cols = [c.name for c in cur.description]
        return dict(zip(cols, cur.fetchone(), strict=True))


def test_purge_nulls_payload_and_hints_of_an_old_terminal_row(clean_queue):
    old_id = _terminal_row(clean_queue, status=schema.FAILED, finished_days_ago=45)

    result = retention.purge(clean_queue, older_than_days=30)

    assert result["purged"] == 1
    assert old_id in result["ids"]
    row = _row(clean_queue, old_id)
    assert row["payload"] is None
    assert row["hints"] is None


def test_purge_survives_id_submitter_timestamps_status_and_result_ref(clean_queue):
    old_id = _terminal_row(clean_queue, status=schema.FILED, finished_days_ago=45,
                           result_ref="wiki/decision.md")
    retention.purge(clean_queue, older_than_days=30)

    row = _row(clean_queue, old_id)
    assert row["id"] == old_id
    assert row["submitted_by"] == ALICE
    assert row["status"] == schema.FILED
    assert row["created_at"] is not None
    assert row["claimed_at"] is not None
    assert row["finished_at"] is not None
    assert row["result_ref"] == "wiki/decision.md"


# ── `outcome` joins the purge as belt-and-braces, never as the primary defense ──────────────────
# `queue.finish`/`queue.dispose` already clear `outcome` unconditionally on every terminal
# transition (`tests/capture/test_queue_pg.py`, `test_dispositions_pg.py`), so an ordinary row
# reaching retention already has `outcome IS NULL` — the precondition below cannot be produced
# through the ordinary `finish()` path at all, which is the point. It stands in for what
# `retention.py`'s own comment names: a crash between the status write and the clear, or a row a
# worker from before that clear existed left behind. `outcome` holds the full drafted body of every
# page a distillation produced — exactly the accumulation retention exists to prevent.
def test_purge_also_clears_a_stray_outcome_an_older_worker_left_behind(clean_queue):
    old_id = _terminal_row(clean_queue, status=schema.FAILED, finished_days_ago=45)
    with clean_queue.cursor() as cur:
        cur.execute("UPDATE capture_queue SET outcome = %s WHERE id = %s",
                    (Jsonb({"version": 1, "raw": {"decisions": [{"title": "a stray distillation"}]}}),
                     old_id))

    retention.purge(clean_queue, older_than_days=30)

    with clean_queue.cursor() as cur:
        cur.execute("SELECT outcome FROM capture_queue WHERE id = %s", (old_id,))
        assert cur.fetchone()[0] is None


def test_purge_leaves_a_recent_rows_stray_outcome_alone_the_benign_twin(clean_queue):
    """The age guard applies to `outcome` exactly like it already does to `payload`/`hints`
    (`test_purge_leaves_a_recent_terminal_row_untouched`, below) — a row not yet eligible must not
    have anything cleared, this column included, or the retention window quietly narrows for one
    field and not the others."""
    recent_id = _terminal_row(clean_queue, status=schema.FAILED, finished_days_ago=5)
    with clean_queue.cursor() as cur:
        cur.execute("UPDATE capture_queue SET outcome = %s WHERE id = %s",
                    (Jsonb({"version": 1, "raw": {"decisions": [{"title": "too recent to purge"}]}}),
                     recent_id))

    result = retention.purge(clean_queue, older_than_days=30)

    assert recent_id not in result["ids"]
    with clean_queue.cursor() as cur:
        cur.execute("SELECT outcome FROM capture_queue WHERE id = %s", (recent_id,))
        assert cur.fetchone()[0] is not None


def test_purge_leaves_a_recent_terminal_row_untouched(clean_queue):
    recent_id = _terminal_row(clean_queue, status=schema.FAILED, finished_days_ago=5)
    result = retention.purge(clean_queue, older_than_days=30)

    assert recent_id not in result["ids"]
    row = _row(clean_queue, recent_id)
    assert row["payload"] is not None
    assert row["hints"] is not None


def _legacy_resolved(conn, submission_id: int, *, finished_days_ago: int) -> None:
    """A row a steward closed by hand back when captures could park — written the way such a row
    exists in a deployment today: directly, since nothing reaches `resolved` any more."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE capture_queue SET status = %s, result_ref = %s, finished_at = now() - "
            "make_interval(days => %s), claimed_at = NULL WHERE id = %s",
            (schema.RESOLVED, "wiki/entities/Jordan Reyes.md@abc123", finished_days_ago,
             submission_id))


def test_purge_treats_resolved_as_terminal_on_the_ordinary_window(clean_queue):
    """`resolved` is LEGACY and still belongs to `TERMINAL_STATUSES`: purged on the SAME window as
    `filed`/`rejected`/`failed`, no separate grace period for a row a steward once handled by
    hand — retention's `_ELIGIBLE` predicate reads the terminal set and nothing else."""
    ack = queue.submit(clean_queue, MemoryEvidenceStore(), kind="raw", material=unique_material(),
                       hints={"title": "row six"}, submitted_by=ALICE)
    queue.claim_next(clean_queue)
    _legacy_resolved(clean_queue, ack["id"], finished_days_ago=45)

    result = retention.purge(clean_queue, older_than_days=30)

    assert ack["id"] in result["ids"]
    row = _row(clean_queue, ack["id"])
    assert row["status"] == schema.RESOLVED
    assert row["payload"] is None
    assert row["hints"] is None
    # the pointer to where the material went survives retention exactly like `filed`'s does —
    # a purged `resolved` row must still answer "where did this end up"
    assert row["result_ref"] == "wiki/entities/Jordan Reyes.md@abc123"


def test_purge_leaves_a_recent_resolved_row_untouched(clean_queue):
    ack = queue.submit(clean_queue, MemoryEvidenceStore(), kind="raw", material=unique_material(),
                       hints=None, submitted_by=ALICE)
    queue.claim_next(clean_queue)
    _legacy_resolved(clean_queue, ack["id"], finished_days_ago=0)

    result = retention.purge(clean_queue, older_than_days=30)

    assert ack["id"] not in result["ids"]
    row = _row(clean_queue, ack["id"])
    assert row["payload"] is not None


def test_purge_is_idempotent_a_second_run_purges_nothing_more(clean_queue):
    old_id = _terminal_row(clean_queue, status=schema.FAILED, finished_days_ago=45)
    first = retention.purge(clean_queue, older_than_days=30)
    second = retention.purge(clean_queue, older_than_days=30)

    assert old_id in first["ids"]
    assert second["purged"] == 0   # already-purged rows have payload/hints NULL: not eligible again


def test_purge_dry_run_lists_without_changing_anything(clean_queue):
    old_id = _terminal_row(clean_queue, status=schema.FAILED, finished_days_ago=45)

    preview = retention.purge(clean_queue, older_than_days=30, dry_run=True)

    assert preview["dry_run"] is True
    assert old_id in preview["ids"]
    row = _row(clean_queue, old_id)
    assert row["payload"] is not None   # dry run touched nothing
    assert row["hints"] is not None


def test_purge_dry_run_and_the_real_run_agree_on_the_same_predicate(clean_queue):
    """The preview must never disagree with the action that follows it (module docstring: "the
    same predicate")."""
    id_a = _terminal_row(clean_queue, status=schema.FAILED, finished_days_ago=45)
    id_b = _terminal_row(clean_queue, status=schema.REJECTED, finished_days_ago=60)
    _terminal_row(clean_queue, status=schema.FILED, finished_days_ago=1)   # too recent, excluded

    preview = retention.purge(clean_queue, older_than_days=30, dry_run=True)
    real = retention.purge(clean_queue, older_than_days=30, dry_run=False)

    assert sorted(preview["ids"]) == sorted(real["ids"]) == sorted([id_a, id_b])


def test_purge_records_a_job_run(clean_queue):
    _terminal_row(clean_queue, status=schema.FAILED, finished_days_ago=45)
    retention.purge(clean_queue, older_than_days=30)

    with clean_queue.cursor() as cur:
        cur.execute("SELECT job, status, stats FROM job_runs WHERE job = 'capture-purge'"
                    " ORDER BY id DESC LIMIT 1")
        job, status, stats = cur.fetchone()
    assert job == "capture-purge"
    assert status == "ok"
    assert stats["purged"] == 1
    assert stats["older_than_days"] == 30


def test_purge_dry_run_records_its_own_distinct_job_name(clean_queue):
    _terminal_row(clean_queue, status=schema.FAILED, finished_days_ago=45)
    retention.purge(clean_queue, older_than_days=30, dry_run=True)

    with clean_queue.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_runs WHERE job = 'capture-purge-dry-run'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM job_runs WHERE job = 'capture-purge'")
        assert cur.fetchone()[0] == 0


# ── the scheduled purge self-heals a WITHHELD_REASONS row whose immediate purge
# (`retention.purge_secret_capture_immediately`, called from `librarian.worker._finish` in a
# SEPARATE, non-atomic statement — `store.connect` is autocommit) never ran, e.g. because the
# process crashed between the rejection write and the immediate purge ───────────────────────────
def test_purge_reconciles_a_withheld_reason_row_regardless_of_age(clean_queue):
    """A row rejected for a secret/PII match must not depend on `purge_secret_capture_immediately`
    having actually run — the ORDINARY scheduled purge also catches any `rejected` row whose
    `reason_code` is in `WITHHELD_REASONS` and still carries payload/hints, however recently it
    finished (the ordinary 30-day age check does not apply to this reconciliation clause)."""
    row_id = _terminal_row(clean_queue, status=schema.REJECTED, finished_days_ago=0)
    with clean_queue.cursor() as cur:
        cur.execute("UPDATE capture_queue SET report = %s WHERE id = %s",
                    (Jsonb({schema.REASON_CODE_KEY: schema.REASON_SECRET}), row_id))

    result = retention.purge(clean_queue, older_than_days=30)

    assert row_id in result["ids"]
    row = _row(clean_queue, row_id)
    assert row["payload"] is None
    assert row["hints"] is None


def test_purge_does_not_early_reconcile_a_recent_rejected_row_for_an_ordinary_reason(clean_queue):
    """The widened eligibility clause is scoped to `WITHHELD_REASONS` specifically — an ordinary
    `rejected` row (a duplicate, say) for a reason that is NOT secret/PII stays on the normal
    30-day window like any other terminal row."""
    row_id = _terminal_row(clean_queue, status=schema.REJECTED, finished_days_ago=0)
    with clean_queue.cursor() as cur:
        cur.execute("UPDATE capture_queue SET report = %s WHERE id = %s",
                    (Jsonb({schema.REASON_CODE_KEY: schema.REASON_DUPLICATE}), row_id))

    result = retention.purge(clean_queue, older_than_days=30)

    assert row_id not in result["ids"]
    row = _row(clean_queue, row_id)
    assert row["payload"] is not None


def test_purge_never_touches_the_evidence_blob():
    """The evidence blob has its own lifecycle and is not touched by this — proven at the
    `BrainService`/`MemoryEvidenceStore` level in `tests/server/test_service_capture.py`, since
    `retention.purge` itself never even receives an evidence store handle: there is no argument
    it COULD use to reach the bucket. Documented here as the structural half of the guarantee."""
    import inspect
    assert "evidence" not in inspect.signature(retention.purge).parameters


# ── a flagged risk: "physically" is honest, not absolute (module/ADR 014 §7
# caveat) — the UPDATE nulls the LIVE row immediately; the previous row version survives as a dead
# tuple until autovacuum, like any Postgres UPDATE. Asserted mechanically via `ctid` (Postgres's
# built-in physical-location system column — no extension needed): an UPDATE always assigns the
# row a NEW ctid, which is the direct, falsifiable signature of "a new tuple version was written;
# the old physical tuple was not touched/erased in place" — exactly what makes the previous
# version a recoverable dead tuple rather than a shredded one. ─────────────────────────────────────
def test_purge_physically_means_a_new_row_version_not_erasure_of_the_old_one(clean_queue):
    old_id = _terminal_row(clean_queue, status=schema.FAILED, finished_days_ago=45)
    with clean_queue.cursor() as cur:
        cur.execute("SELECT ctid FROM capture_queue WHERE id = %s", (old_id,))
        ctid_before = cur.fetchone()[0]

    retention.purge(clean_queue, older_than_days=30)

    with clean_queue.cursor() as cur:
        cur.execute("SELECT ctid, payload FROM capture_queue WHERE id = %s", (old_id,))
        ctid_after, payload_after = cur.fetchone()
    assert payload_after is None                 # the LIVE row now reads NULL (nulled "in place")
    assert ctid_after != ctid_before              # ...by writing a NEW tuple, not editing the old
    # the row at `ctid_before` is not shredded — it is a normal Postgres dead tuple, reclaimed only
    # by autovacuum (or an explicit VACUUM). A plain SELECT never sees it again (MVCC visibility),
    # which is exactly why "physically" needs the honest caveat this test makes falsifiable rather
    # than asserted in prose alone.
