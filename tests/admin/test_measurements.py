"""`admin.measurements`: the console's measurement reads, built from real `audit_log`/
`capture_queue` rows. Real Postgres — this instrument's whole point is that the numbers come from
the database, not from a mock of it.

On the admin suite's own `conn`/`clean_tables` fixtures rather than a private connection of its
own: these functions are read by the Activity page and the dashboard's `ask` chart, so the tables
they are exercised over are the tables the rest of this suite provisions and truncates.
"""
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from stigmergy.admin import measurements
from stigmergy.capture import ops, queue, schema
from stigmergy.server.audit import AuditWriter
from stigmergy.server.webhook import JOB_NAME as WEBHOOK_JOB_NAME
from tests.admin.conftest import submit_one


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
def test_questions_per_identity_per_week_buckets_by_iso_week(conn):
    week1 = datetime(2026, 1, 5, tzinfo=UTC)   # a Monday
    week2 = week1 + timedelta(days=8)
    _write_ask_row(conn, identity="ana@example.com", result={"refused": False}, ts=week1)
    _write_ask_row(conn, identity="ana@example.com", result={"refused": False}, ts=week1)
    _write_ask_row(conn, identity="ana@example.com", result={"refused": False}, ts=week2)
    _write_ask_row(conn, identity="steward@example.com", result={"refused": False}, ts=week1)

    counts = measurements.questions_per_identity_per_week(conn)
    assert sum(counts["ana@example.com"].values()) == 3
    assert len(counts["ana@example.com"]) == 2      # two distinct weeks
    assert sum(counts["steward@example.com"].values()) == 1


def test_questions_per_identity_per_week_respects_since(conn):
    old = datetime(2025, 1, 1, tzinfo=UTC)
    recent = datetime(2026, 7, 1, tzinfo=UTC)
    _write_ask_row(conn, identity="ana@example.com", result={"refused": False}, ts=old)
    _write_ask_row(conn, identity="ana@example.com", result={"refused": False}, ts=recent)

    counts = measurements.questions_per_identity_per_week(
        conn, since=datetime(2026, 1, 1, tzinfo=UTC))
    assert sum(counts["ana@example.com"].values()) == 1


# ── answered-with-citation vs honest refusal ────────────────────────────────────────────────────
def test_answer_shape_counts_citation_refusal_and_no_citation_cases(conn):
    _write_ask_row(conn, identity="a", result={"refused": False, "citations": ["p.md"]})
    _write_ask_row(conn, identity="a", result={"refused": False, "citations": ["p.md"]})
    _write_ask_row(conn, identity="a", result={"refused": True, "citations": []})
    _write_ask_row(conn, identity="a", result={"refused": False, "citations": []})   # no citation

    shape = measurements.answer_shape(conn)
    assert shape["total"] == 4
    assert shape["answered_with_citation"] == 2
    assert shape["refused"] == 1
    assert shape["answered_no_citation"] == 1
    assert shape["answered_with_citation_pct"] == 50.0
    assert shape["refused_pct"] == 25.0


def test_answer_shape_ignores_errored_calls_and_calls_with_no_result(conn):
    _write_ask_row(conn, identity="a", result={"refused": False, "citations": ["p.md"]})
    _write_ask_row(conn, identity="a", result=None)                       # no result recorded
    _write_ask_row(conn, identity="a", result={"refused": True}, outcome="error")

    shape = measurements.answer_shape(conn)
    assert shape["total"] == 1


def test_answer_shape_with_no_calls_at_all_reports_zero_not_a_crash(conn):
    shape = measurements.answer_shape(conn)
    assert shape["total"] == 0
    assert shape["refused_pct"] is None
    assert shape["answered_with_citation_pct"] is None


# ── the whole report ─────────────────────────────────────────────────────────────────────────────
def _file_capture(conn, *, sha: str = "", searchable: bool = False) -> None:
    ack = submit_one(conn, material="measurement material")
    claimed = queue.claim_next(conn)
    result_ref = f"wiki/x.md@{sha}" if sha else ""
    queue.finish(conn, ack["id"], status=schema.FILED, expected_attempts=claimed["attempts"],
                 result_ref=result_ref)
    if searchable:
        ops.record_job_run(conn, WEBHOOK_JOB_NAME, status="ok", stats={"sha": sha})


def test_build_report_has_every_expected_section(conn):
    _write_ask_row(conn, identity="ana@example.com", result={"refused": False, "citations": ["p.md"]})
    _file_capture(conn, sha="abc123", searchable=True)

    report = measurements.build_report(conn)
    assert set(report) == {"questions_per_identity_per_week", "answer_shape",
                           "capture_to_filed_latency", "capture_to_searchable_latency"}
    assert report["capture_to_filed_latency"]["samples"] == 1
    assert report["capture_to_searchable_latency"]["samples"] == 1


# ── it reads; it writes nothing — and that rules out DDL too ─────────────────────────────────────
def test_the_module_imports_no_ddl_helper():
    """`ensure_audit_table`/`ensure_capture_schema` (`CREATE TABLE IF NOT EXISTS`/`ALTER TABLE ADD
    COLUMN IF NOT EXISTS`) must not be reachable from here. A reporting read that provisions its
    own tables hides whichever boot path was supposed to have provisioned them — the failure looks
    like an empty report instead of a missing table. Checked structurally, so there is no name left
    for a future edit to "helpfully" call again without re-litigating why it was removed."""
    assert not hasattr(measurements, "ensure_audit_table")
    assert not hasattr(measurements, "ensure_capture_schema")


# ── answer_shape_by_day: the report's classifier with a time axis, grouped in SQL ───────────────
def test_shape_of_is_the_one_precedence_refused_then_cited_then_uncited():
    assert measurements.shape_of({"refused": True, "citations": 3}) == measurements.SHAPE_REFUSED
    assert measurements.shape_of({"refused": False, "citations": 2}) == measurements.SHAPE_CITED
    assert measurements.shape_of({"refused": False, "citations": 0}) == measurements.SHAPE_UNCITED
    assert measurements.shape_of({}) == measurements.SHAPE_UNCITED
    assert measurements.shape_of("not a dict") == measurements.SHAPE_UNCITED


def test_answer_shape_by_day_agrees_with_answer_shape_on_the_same_rows(conn):
    """The SQL mirror of `shape_of` is pinned against the Python original on one set of rows:
    summing the per-day buckets must reproduce `answer_shape`'s totals exactly. Errored calls and
    successful ones with no recorded result are counted APART — `answer_shape` never sees them,
    and the per-day read must not fold them into an answer shape either."""
    now = datetime.now(UTC)
    _write_ask_row(conn, identity="a", result={"refused": True, "citations": 0})
    _write_ask_row(conn, identity="a", result={"refused": False, "citations": 2})
    _write_ask_row(conn, identity="b", result={"refused": False, "citations": 0})
    _write_ask_row(conn, identity="b", result=None)                     # ok, no shape recorded
    _write_ask_row(conn, identity="b", result=None, outcome="error")    # errored
    _write_ask_row(conn, identity="a", result={"refused": True}, ts=now - timedelta(days=3))
    _write_ask_row(conn, identity="a", result={"refused": False, "citations": 1},
                   ts=now - timedelta(days=40))                         # outside a 30-day window

    by_day = measurements.answer_shape_by_day(conn, days=30)
    assert len(by_day) == 2 and by_day[0]["day"] < by_day[1]["day"], "ascending by UTC day"
    today = by_day[1]
    assert today[measurements.SHAPE_REFUSED] == 1
    assert today[measurements.SHAPE_CITED] == 1
    assert today[measurements.SHAPE_UNCITED] == 1
    assert today["unrecorded"] == 1 and today["errors"] == 1
    assert by_day[0][measurements.SHAPE_REFUSED] == 1

    whole = measurements.answer_shape(conn, since=now - timedelta(days=30))
    summed = {shape: sum(bucket[shape] for bucket in by_day)
              for shape in (measurements.SHAPE_REFUSED, measurements.SHAPE_CITED,
                            measurements.SHAPE_UNCITED)}
    assert summed == {measurements.SHAPE_REFUSED: whole["refused"],
                      measurements.SHAPE_CITED: whole["answered_with_citation"],
                      measurements.SHAPE_UNCITED: whole["answered_no_citation"]}
    assert whole["total"] == sum(summed.values()), "neither read counts the unrecorded or errored"


def test_answer_shape_by_day_reads_a_legacy_citation_list_the_way_shape_of_does(conn):
    """THE REGRESSION. OLD BEHAVIOUR: the SQL mirror cast `result ->> 'citations'` to `int`, so a
    row whose `citations` is the pre-`audit_summary` LIST (`["wiki/…md", …]`, which is what most
    rows on a deployment that has been running a while carry) raised
    `InvalidTextRepresentation: invalid input syntax for type integer` — and because the console's
    `metrics` call is fetched by most of its pages, one legacy row turned almost the whole
    console into "the operation failed (InvalidTextRepresentation)".

    `shape_of` never had the problem: it asks Python truthiness, for which a non-empty list, a
    non-zero count and a non-empty string are all "there is a citation". The mirror must answer
    the same question of the same JSON, whatever a past writer put in the column — so it compares
    the JSONB value against the falsy set instead of casting it to a type it may not be."""
    _write_ask_row(conn, identity="a", result={"refused": False, "citations": ["wiki/a.md", "wiki/b.md"]})
    _write_ask_row(conn, identity="a", result={"refused": False, "citations": []})
    _write_ask_row(conn, identity="a", result={"refused": True, "citations": ["wiki/c.md"]})
    _write_ask_row(conn, identity="a", result={"refused": False, "citations": 3})

    by_day = measurements.answer_shape_by_day(conn, days=30)

    assert len(by_day) == 1
    today = by_day[0]
    assert today[measurements.SHAPE_CITED] == 2, "a non-empty list counts, and so does a count"
    assert today[measurements.SHAPE_UNCITED] == 1, "an EMPTY list is no citation, like a zero"
    assert today[measurements.SHAPE_REFUSED] == 1, "refused wins over citations, in both shapes"

    whole = measurements.answer_shape(conn)
    assert (today[measurements.SHAPE_CITED], today[measurements.SHAPE_UNCITED],
            today[measurements.SHAPE_REFUSED]) == (
        whole["answered_with_citation"], whole["answered_no_citation"], whole["refused"])


def test_shape_of_and_its_sql_mirror_agree_on_every_json_shape_a_writer_could_leave(conn):
    """The mirror's contract, asked of the shapes a JSONB column can actually hold — no cast can
    survive all of these, which is why neither side casts. Each row is written, read back through
    the grouped query, and compared with `shape_of`'s own answer for the same value."""
    cases = [
        {"refused": False, "citations": ["wiki/a.md"]},      # legacy list
        {"refused": False, "citations": []},                  # legacy empty list
        {"refused": False, "citations": 2},                   # today's count
        {"refused": False, "citations": 0},
        {"refused": False},                                   # no citations key at all
        {"refused": True},
        {"refused": False, "citations": None},
        {"refused": None, "citations": 1},                    # a null where a bool was expected
        {"refused": False, "citations": "wiki/a.md"},         # a bare string
        {"refused": False, "citations": ""},
    ]
    expected = {measurements.SHAPE_CITED: 0, measurements.SHAPE_UNCITED: 0,
                measurements.SHAPE_REFUSED: 0}
    for result in cases:
        _write_ask_row(conn, identity="a", result=result)
        expected[measurements.shape_of(result)] += 1

    [today] = measurements.answer_shape_by_day(conn, days=30)

    assert {shape: today[shape] for shape in expected} == expected
