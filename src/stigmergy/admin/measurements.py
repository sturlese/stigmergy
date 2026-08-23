"""The console's measurement reads: answer shape (answered-with-citation vs honest refusal),
questions per identity per week (a per-scope signal, never an adoption metric), and capture->filed
/ capture->searchable latency percentiles.

Every number comes from a column something else already wrote (`audit_log.result`,
`capture_queue`, `job_runs`) — no measurement channel of its own. It READS, and it provisions
nothing: a reporting function that quietly runs `ensure_*_schema` hides whichever boot path was
supposed to have provisioned the table, so the DDL stays where the writers do
(`tests/admin/test_measurements.py` pins the absence structurally).

Lives in `stigmergy.admin` rather than under `stigmergy.server` because the console is the only
surface that reads it: the Activity page renders `build_report` whole, and the dashboard's `ask`
chart is `answer_shape_by_day`. Nothing in the serving path calls any of it.
"""
from collections import defaultdict
from datetime import datetime

from stigmergy.capture import latency as latency_mod
from stigmergy.capture import queue as capture_queue
from stigmergy.server.webhook import JOB_NAME as WEBHOOK_JOB_NAME


def _week_bucket(ts: datetime) -> str:
    """ISO year-week (`2026-W30`) — stable and sortable."""
    iso = ts.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def questions_per_identity_per_week(conn, *, since: datetime | None = None) -> dict[str, dict[str, int]]:
    """`identity -> {week -> count}`, from every `ask` call `audit_log` recorded (regardless of
    outcome — a rate-limited or errored `ask` is still a question somebody asked)."""
    params = (since,) if since is not None else ()
    query = ("SELECT identity, ts FROM audit_log WHERE tool = 'ask'"
             + (" AND ts >= %s" if params else ""))
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for identity, ts in rows:
        counts[identity or "(unknown)"][_week_bucket(ts)] += 1
    return {identity: dict(weeks) for identity, weeks in counts.items()}


SHAPE_REFUSED = "refused"
SHAPE_CITED = "answered_with_citation"
SHAPE_UNCITED = "answered_no_citation"


def shape_of(result) -> str:
    """The ONE reading of an `ask` result summary: a refusal, an answer with a citation, or an
    answer without one — in that precedence. Every surface that classifies an answer calls this
    (the Activity table, the console's per-day chart), so two charts cannot disagree about what an
    answer was. `result` is whatever `audit_log.result` holds for a successful call."""
    if not isinstance(result, dict):
        return SHAPE_UNCITED
    if result.get("refused"):
        return SHAPE_REFUSED
    return SHAPE_CITED if result.get("citations") else SHAPE_UNCITED


# Python truthiness, as a JSONB comparison: a value is falsy iff it is absent, JSON null, false,
# zero, an empty string, an empty array or an empty object — exactly what `if value:` answers for
# a decoded JSON value, and the only reading that CANNOT raise.
#
# It replaced a cast (`(result ->> 'citations')::int`), and the difference is not style: this
# column is JSONB with no CHECK under it, and what a past writer left there is permanent. Most
# `ask` rows on a deployment that has been running a while carry `citations` as the pre-
# `audit_summary` LIST of page paths, so the cast raised `InvalidTextRepresentation` on real data
# while every test that fed it today's integer stayed green.
def _truthy_sql(expression: str) -> str:
    return (f"({expression} IS NOT NULL AND {expression} NOT IN "
            f"('null'::jsonb, 'false'::jsonb, '0'::jsonb, '\"\"'::jsonb, '[]'::jsonb, '{{}}'::jsonb))")


# `shape_of` as SQL, for the grouped per-day read: the same precedence (refused first), the same
# truthiness. `tests/admin/test_measurements.py` pins the two against each other over every JSON
# shape the column can hold, so the SQL cannot drift from the function it mirrors.
_SHAPE_SQL = f"""
CASE WHEN {_truthy_sql("result -> 'refused'")} THEN '{SHAPE_REFUSED}'
     WHEN {_truthy_sql("result -> 'citations'")} THEN '{SHAPE_CITED}'
     ELSE '{SHAPE_UNCITED}' END
"""


def answer_shape_by_day(conn, *, days: int) -> list[dict]:
    """`answer_shape` with a time axis, grouped in SQL: one row per UTC day of the last `days`
    with the three shapes, plus the calls that errored and the successful ones that recorded no
    shape at all — counted apart, never folded into an answer shape. Bounded by the window and
    aggregated in the database, so a year of questions is a few hundred rows, not a fetch of every
    result."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (ts AT TIME ZONE 'UTC')::date AS day,"
            " CASE WHEN outcome <> 'ok' THEN 'errors'"
            f"      WHEN result IS NULL THEN 'unrecorded' ELSE {_SHAPE_SQL} END AS shape, count(*)"
            " FROM audit_log WHERE tool = 'ask' AND ts >= now() - make_interval(days => %s)"
            " GROUP BY 1, 2 ORDER BY 1", (max(1, int(days)),))
        rows = cur.fetchall()
    by_day: dict[str, dict] = {}
    for day, shape, count in rows:
        bucket = by_day.setdefault(day.isoformat(), {
            "day": day.isoformat(), SHAPE_CITED: 0, SHAPE_UNCITED: 0, SHAPE_REFUSED: 0,
            "errors": 0, "unrecorded": 0})
        bucket[shape] = int(count)
    return [by_day[day] for day in sorted(by_day)]


def answer_shape(conn, *, since: datetime | None = None) -> dict:
    """% answered-with-citation vs honest refusal, from `ask`'s `audit_log.result` summary —
    never the question or answer text. Only successful calls with a recorded result count."""
    params = (since,) if since is not None else ()
    query = ("SELECT result FROM audit_log WHERE tool = 'ask' AND outcome = 'ok'"
             " AND result IS NOT NULL" + (" AND ts >= %s" if params else ""))
    with conn.cursor() as cur:
        cur.execute(query, params)
        results = [row[0] for row in cur.fetchall()]

    total = len(results)
    shapes = [shape_of(r) for r in results]
    refused = shapes.count(SHAPE_REFUSED)
    answered_with_citation = shapes.count(SHAPE_CITED)
    answered_no_citation = total - refused - answered_with_citation
    return {
        "total": total,
        "refused": refused,
        "answered_with_citation": answered_with_citation,
        "answered_no_citation": answered_no_citation,
        "refused_pct": (refused / total * 100) if total else None,
        "answered_with_citation_pct": (answered_with_citation / total * 100) if total else None,
    }


def build_report(conn, *, since: datetime | None = None) -> dict:
    """The whole table as one JSON-able dict — the Activity page renders it, and every section of
    it is a read the console already knows how to draw."""
    filed = latency_mod.summarize(capture_queue.filed_latencies_ms(conn))
    searchable = latency_mod.summarize(
        capture_queue.searchable_latencies_ms(conn, job_name=WEBHOOK_JOB_NAME))
    return {
        "questions_per_identity_per_week": questions_per_identity_per_week(conn, since=since),
        "answer_shape": answer_shape(conn, since=since),
        "capture_to_filed_latency": filed.as_json(),
        "capture_to_searchable_latency": searchable.as_json(),
    }
