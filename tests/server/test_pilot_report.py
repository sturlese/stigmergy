"""`stigmergy-pilot-report`: the pilot-readiness table, built from real `audit_log`/`capture_queue`
rows. Real Postgres — this instrument's whole point is that the numbers come from the database,
not from a mock of it.
"""
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from stigmergy.capture import ops, queue, schema
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.server import pilot_report
from stigmergy.server.audit import AuditWriter, ensure_audit_table
from stigmergy.server.webhook import JOB_NAME as WEBHOOK_JOB_NAME
from tests import testdb


def _connect_or_skip():
    conn = testdb.connect_or_skip("index")
    ensure_audit_table(conn)
    schema.ensure_capture_schema(conn)
    return conn


@pytest.fixture()
def clean_pilot_report_db():
    conn = _connect_or_skip()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM audit_log")
        cur.execute("DELETE FROM capture_queue")
        cur.execute("DELETE FROM job_runs")
    yield conn


def _write_ask_row(conn, *, identity: str, result: dict, outcome: str = "ok", ts=None) -> None:
    with conn.cursor() as cur:
        if ts is not None:
            cur.execute(
                "INSERT INTO audit_log (ts, identity, tool, args, duration_ms, outcome,"
                " error_class, result) VALUES (%s, %s, 'ask', %s, 1.0, %s, '', %s)",
                (ts, identity, Jsonb({"question": "x"}), outcome,
                 None if result is None else Jsonb(result)))
        else:
            AuditWriter(conn).write(identity=identity, tool="ask", args={"question": "x"},
                                    duration_ms=1.0, outcome=outcome, result=result)


# ── questions per identity per week ──────────────────────────────────────────────────────────────
def test_questions_per_identity_per_week_buckets_by_iso_week(clean_pilot_report_db):
    conn = clean_pilot_report_db
    week1 = datetime(2026, 1, 5, tzinfo=UTC)   # a Monday
    week2 = week1 + timedelta(days=8)
    _write_ask_row(conn, identity="ana@example.com", result={"refused": False}, ts=week1)
    _write_ask_row(conn, identity="ana@example.com", result={"refused": False}, ts=week1)
    _write_ask_row(conn, identity="ana@example.com", result={"refused": False}, ts=week2)
    _write_ask_row(conn, identity="steward@example.com", result={"refused": False}, ts=week1)

    counts = pilot_report.questions_per_identity_per_week(conn)
    assert sum(counts["ana@example.com"].values()) == 3
    assert len(counts["ana@example.com"]) == 2      # two distinct weeks
    assert sum(counts["steward@example.com"].values()) == 1


def test_questions_per_identity_per_week_respects_since(clean_pilot_report_db):
    conn = clean_pilot_report_db
    old = datetime(2025, 1, 1, tzinfo=UTC)
    recent = datetime(2026, 7, 1, tzinfo=UTC)
    _write_ask_row(conn, identity="ana@example.com", result={"refused": False}, ts=old)
    _write_ask_row(conn, identity="ana@example.com", result={"refused": False}, ts=recent)

    counts = pilot_report.questions_per_identity_per_week(
        conn, since=datetime(2026, 1, 1, tzinfo=UTC))
    assert sum(counts["ana@example.com"].values()) == 1


# ── answered-with-citation vs honest refusal ────────────────────────────────────────────────────
def test_answer_shape_counts_citation_refusal_and_no_citation_cases(clean_pilot_report_db):
    conn = clean_pilot_report_db
    _write_ask_row(conn, identity="a", result={"refused": False, "citations": ["p.md"]})
    _write_ask_row(conn, identity="a", result={"refused": False, "citations": ["p.md"]})
    _write_ask_row(conn, identity="a", result={"refused": True, "citations": []})
    _write_ask_row(conn, identity="a", result={"refused": False, "citations": []})   # no citation

    shape = pilot_report.answer_shape(conn)
    assert shape["total"] == 4
    assert shape["answered_with_citation"] == 2
    assert shape["refused"] == 1
    assert shape["answered_no_citation"] == 1
    assert shape["answered_with_citation_pct"] == 50.0
    assert shape["refused_pct"] == 25.0


def test_answer_shape_ignores_errored_calls_and_calls_with_no_result(clean_pilot_report_db):
    conn = clean_pilot_report_db
    _write_ask_row(conn, identity="a", result={"refused": False, "citations": ["p.md"]})
    _write_ask_row(conn, identity="a", result=None)                       # no result recorded
    _write_ask_row(conn, identity="a", result={"refused": True}, outcome="error")

    shape = pilot_report.answer_shape(conn)
    assert shape["total"] == 1


def test_answer_shape_with_no_calls_at_all_reports_zero_not_a_crash(clean_pilot_report_db):
    shape = pilot_report.answer_shape(clean_pilot_report_db)
    assert shape["total"] == 0
    assert shape["refused_pct"] is None
    assert shape["answered_with_citation_pct"] is None


# ── the whole report + render ────────────────────────────────────────────────────────────────────
def _file_capture(conn, *, sha: str = "", searchable: bool = False) -> None:
    evidence = MemoryEvidenceStore()
    ack = queue.submit(conn, evidence, kind="raw", material="pilot report material",
                      hints=None, submitted_by="tester@example.com")
    claimed = queue.claim_next(conn)
    result_ref = f"wiki/x.md@{sha}" if sha else ""
    queue.finish(conn, ack["id"], status=schema.FILED, expected_attempts=claimed["attempts"],
                result_ref=result_ref)
    if searchable:
        ops.record_job_run(conn, WEBHOOK_JOB_NAME, status="ok", stats={"sha": sha})


def test_build_report_has_every_expected_section(clean_pilot_report_db):
    conn = clean_pilot_report_db
    _write_ask_row(conn, identity="ana@example.com", result={"refused": False, "citations": ["p.md"]})
    _file_capture(conn, sha="abc123", searchable=True)

    report = pilot_report.build_report(conn)
    assert set(report) == {"questions_per_identity_per_week", "answer_shape",
                           "capture_to_filed_latency", "capture_to_searchable_latency"}
    assert report["capture_to_filed_latency"]["samples"] == 1
    assert report["capture_to_searchable_latency"]["samples"] == 1


def test_render_produces_a_readable_table_with_no_data(clean_pilot_report_db):
    text = pilot_report.render(pilot_report.build_report(clean_pilot_report_db))
    assert "stigmergy-pilot-report" in text
    assert "no `ask` calls recorded" in text
    assert "not enough data yet" in text


def test_render_produces_a_readable_table_with_data(clean_pilot_report_db):
    conn = clean_pilot_report_db
    _write_ask_row(conn, identity="ana@example.com", result={"refused": False, "citations": ["p.md"]})
    _write_ask_row(conn, identity="ana@example.com", result={"refused": True, "citations": []})

    text = pilot_report.render(pilot_report.build_report(conn))
    assert "ana@example.com" in text
    assert "answered with citation: 1 (50.0%)" in text
    assert "honest refusal: 1 (50.0%)" in text


def test_main_writes_nothing_and_prints_json(clean_pilot_report_db, capsys, monkeypatch):
    """spec: "give it no flags that write" — asserted here by checking the row counts are
    unchanged after a `--json` run."""
    import json

    from stigmergy.index import store as index_store
    conn = clean_pilot_report_db
    _write_ask_row(conn, identity="ana@example.com", result={"refused": False, "citations": []})

    def fake_connect(dsn=None):
        return conn
    monkeypatch.setattr(index_store, "connect", fake_connect)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_log")
        before = cur.fetchone()[0]

    monkeypatch.setattr(conn, "close", lambda: None)   # main() closes conn; keep it open for reuse
    rc = pilot_report.main(["--json"])
    assert rc == 0

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "answer_shape" in payload

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_log")
        after = cur.fetchone()[0]
    assert after == before   # nothing was written


# ── it reads; it writes nothing — and that rules out DDL too, which would otherwise make a
# READ-ONLY database role impossible ───────────────────────────────────────────────────────────
def test_main_module_imports_no_ddl_helper():
    """`main` used to call `ensure_audit_table`/`ensure_capture_schema` (`CREATE TABLE IF NOT
    EXISTS`/`ALTER TABLE ADD COLUMN IF NOT EXISTS`) — DDL a read-only database role cannot execute
    even when it is a no-op against an already-existing table. Checked structurally: this module
    must not even IMPORT either DDL helper, so there is no name left for a future edit to
    "helpfully" call again without re-litigating why it was removed."""
    assert not hasattr(pilot_report, "ensure_audit_table")
    assert not hasattr(pilot_report, "ensure_capture_schema")


def test_main_succeeds_with_no_ddl_when_the_schema_already_exists(clean_pilot_report_db, monkeypatch):
    """The behavioral half: `main` still runs end to end (the tables already exist, via this
    module's own fixture — a real deployment's tables are provisioned by the server/worker
    startup paths, never by this reporting command)."""
    from stigmergy.index import store as index_store
    conn = clean_pilot_report_db

    def fake_connect(dsn=None):
        return conn
    monkeypatch.setattr(index_store, "connect", fake_connect)
    monkeypatch.setattr(conn, "close", lambda: None)

    rc = pilot_report.main(["--json"])
    assert rc == 0


# ── the console posture: a clean line and an exit code, never a traceback ──────────────────────
def test_a_bad_since_is_a_clean_refusal_not_a_traceback(capsys):
    """OLD BEHAVIOUR: a bare `ValueError` out of `strptime` reached the operator as a Python
    traceback. `server/errors.py` states the rule for every console entry point in this package:
    "the console entry point maps them to a clean stderr line and a non-zero exit code — no
    traceback ever reaches an operator's terminal". Every sibling CLI honours it; this one did
    not, and it refuses before any I/O so there is nothing to clean up either."""
    assert pilot_report.main(["--since", "not-a-date", "--dsn", "postgresql://unused"]) == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "YYYY-MM-DD" in err


def test_a_valid_since_is_accepted(capsys):
    """The benign twin: the guard must not reject a real date. Stops at the connection, which is
    the next step and not this test's subject."""
    code = pilot_report.main(["--since", "2026-01-01", "--dsn",
                              "postgresql://nobody@127.0.0.1:1/nothing"])
    assert code == 1                                   # a connection refusal, not an arg refusal
    assert "YYYY-MM-DD" not in capsys.readouterr().err


# ── answer_shape_by_day: the report's classifier with a time axis, grouped in SQL ───────────────
def test_shape_of_is_the_one_precedence_refused_then_cited_then_uncited():
    assert pilot_report.shape_of({"refused": True, "citations": 3}) == pilot_report.SHAPE_REFUSED
    assert pilot_report.shape_of({"refused": False, "citations": 2}) == pilot_report.SHAPE_CITED
    assert pilot_report.shape_of({"refused": False, "citations": 0}) == pilot_report.SHAPE_UNCITED
    assert pilot_report.shape_of({}) == pilot_report.SHAPE_UNCITED
    assert pilot_report.shape_of("not a dict") == pilot_report.SHAPE_UNCITED


def test_answer_shape_by_day_agrees_with_answer_shape_on_the_same_rows(clean_pilot_report_db):
    """The SQL mirror of `shape_of` is pinned against the Python original on one set of rows:
    summing the per-day buckets must reproduce `answer_shape`'s totals exactly. Errored calls and
    successful ones with no recorded result are counted APART — `answer_shape` never sees them,
    and the per-day read must not fold them into an answer shape either."""
    conn = clean_pilot_report_db
    now = datetime.now(UTC)
    _write_ask_row(conn, identity="a", result={"refused": True, "citations": 0})
    _write_ask_row(conn, identity="a", result={"refused": False, "citations": 2})
    _write_ask_row(conn, identity="b", result={"refused": False, "citations": 0})
    _write_ask_row(conn, identity="b", result=None)                     # ok, no shape recorded
    _write_ask_row(conn, identity="b", result=None, outcome="error")    # errored
    _write_ask_row(conn, identity="a", result={"refused": True}, ts=now - timedelta(days=3))
    _write_ask_row(conn, identity="a", result={"refused": False, "citations": 1},
                   ts=now - timedelta(days=40))                         # outside a 30-day window

    by_day = pilot_report.answer_shape_by_day(conn, days=30)
    assert len(by_day) == 2 and by_day[0]["day"] < by_day[1]["day"], "ascending by UTC day"
    today = by_day[1]
    assert today[pilot_report.SHAPE_REFUSED] == 1
    assert today[pilot_report.SHAPE_CITED] == 1
    assert today[pilot_report.SHAPE_UNCITED] == 1
    assert today["unrecorded"] == 1 and today["errors"] == 1
    assert by_day[0][pilot_report.SHAPE_REFUSED] == 1

    whole = pilot_report.answer_shape(conn, since=now - timedelta(days=30))
    summed = {shape: sum(bucket[shape] for bucket in by_day)
              for shape in (pilot_report.SHAPE_REFUSED, pilot_report.SHAPE_CITED,
                            pilot_report.SHAPE_UNCITED)}
    assert summed == {pilot_report.SHAPE_REFUSED: whole["refused"],
                      pilot_report.SHAPE_CITED: whole["answered_with_citation"],
                      pilot_report.SHAPE_UNCITED: whole["answered_no_citation"]}
    assert whole["total"] == sum(summed.values()), "neither read counts the unrecorded or errored"
