"""`stigmergy-pilot-report`: the measurement table, produced by a command rather than assembled by
hand.

Three parts:

  * **the answer-shape split** — % answered-with-citation vs honest refusal. Both are healthy
    numbers: a system that never refuses is the failure, not the success. This is the honesty
    figure every golden run is compared against.
  * **capture -> filed latency percentiles**, and capture -> searchable latency, which is a
    different number because the incremental webhook exists.
  * **questions per identity per week** — a per-scope signal, not an adoption metric. Read it as
    "which credential is spending the budget", never as "how many people use this".

**Reads. Writes nothing, including no DDL** — no flag here mutates anything, and unlike every
other CLI in this repo this one does NOT run the idempotent `ensure_*_schema` at startup either,
so it works under a read-only database role. See `main` for what a missing table looks like.

Every number here comes from a column something else already wrote for a different reason —
`audit_log.result` (written at the SAME `_call`/`call_async` seam every row already goes through),
`capture_queue.created_at`/`finished_at`/`result_ref`, and `job_runs` (the webhook's own
bookkeeping). Nothing here is a new measurement channel; it is the first thing that reads the ones
that already exist and puts them next to each other.
"""
import argparse
import json
from collections import defaultdict
from datetime import datetime

from stigmergy.capture import latency as latency_mod
from stigmergy.capture import queue as capture_queue
from stigmergy.index import store
from stigmergy.server.webhook import JOB_NAME as WEBHOOK_JOB_NAME

_PERCENTILES = latency_mod.PERCENTILES


def _week_bucket(ts: datetime) -> str:
    """ISO year-week (`2026-W30`) — stable, sortable, and the same bucket a human reading a report
    would draw by hand from a calendar."""
    iso = ts.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def questions_per_identity_per_week(conn, *, since: datetime | None = None) -> dict[str, dict[str, int]]:
    """`identity -> {week -> count}`, from every `ask` call `audit_log` recorded (regardless of
    outcome — a rate-limited or errored `ask` is still a question somebody asked)."""
    with conn.cursor() as cur:
        if since is not None:
            cur.execute("SELECT identity, ts FROM audit_log WHERE tool = 'ask' AND ts >= %s",
                        (since,))
        else:
            cur.execute("SELECT identity, ts FROM audit_log WHERE tool = 'ask'")
        rows = cur.fetchall()
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for identity, ts in rows:
        counts[identity or "(unknown)"][_week_bucket(ts)] += 1
    return {identity: dict(weeks) for identity, weeks in counts.items()}


def answer_shape(conn, *, since: datetime | None = None) -> dict:
    """% answered-with-citation vs honest refusal, from `ask`'s `audit_log.result` — the per-tool
    outcome summary `answer.service.audit_summary` writes, never the question or answer text
    itself. Only successful `ask` calls with a recorded result count towards the percentages — an
    errored call answered nothing, in either sense."""
    with conn.cursor() as cur:
        query = ("SELECT result FROM audit_log WHERE tool = 'ask' AND outcome = 'ok'"
                " AND result IS NOT NULL")
        if since is not None:
            cur.execute(query + " AND ts >= %s", (since,))
        else:
            cur.execute(query)
        results = [row[0] for row in cur.fetchall()]

    total = len(results)
    refused = sum(1 for r in results if r.get("refused"))
    answered_with_citation = sum(1 for r in results if not r.get("refused") and r.get("citations"))
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
    """The whole table, as one JSON-able dict — the shape both `render` (a human) and
    `--json` (the record) work from, so the two can never disagree about what was measured."""
    filed = latency_mod.summarize(capture_queue.filed_latencies_ms(conn))
    searchable = latency_mod.summarize(
        capture_queue.searchable_latencies_ms(conn, job_name=WEBHOOK_JOB_NAME))
    return {
        "questions_per_identity_per_week": questions_per_identity_per_week(conn, since=since),
        "answer_shape": answer_shape(conn, since=since),
        "capture_to_filed_latency": filed.as_json(),
        "capture_to_searchable_latency": searchable.as_json(),
    }


def _render_latency(summary_json: dict) -> str:
    if not summary_json["enough_data"]:
        return (f"not enough data yet — {summary_json['samples']} sample(s), "
               f"{summary_json['min_samples']} needed before p50/p95 mean anything")
    parts = " · ".join(f"p{q}={summary_json[f'p{q}_ms'] / 1000:.1f}s"
                       for q in _PERCENTILES if summary_json.get(f"p{q}_ms") is not None)
    return f"{parts} over {summary_json['samples']} sample(s)"


def render(report: dict) -> str:
    """The human-readable table; `--json` is the same report for the record."""
    lines = ["# stigmergy-pilot-report", ""]

    lines.append("## Questions per identity per week")
    lines.append("(a per-scope signal, not adoption — see the module docstring)")
    per_identity = report["questions_per_identity_per_week"]
    if not per_identity:
        lines.append("(no `ask` calls recorded yet)")
    else:
        for identity in sorted(per_identity):
            weeks = per_identity[identity]
            per_week = ", ".join(f"{week}={count}" for week, count in sorted(weeks.items()))
            lines.append(f"- {identity}: {per_week} (total {sum(weeks.values())})")

    lines.append("")
    lines.append("## Answered-with-citation vs honest refusal")
    shape = report["answer_shape"]
    if shape["total"] == 0:
        lines.append("(no successful `ask` calls with a recorded result yet)")
    else:
        lines.append(f"- total: {shape['total']}")
        lines.append(f"- answered with citation: {shape['answered_with_citation']} "
                     f"({shape['answered_with_citation_pct']:.1f}%)")
        lines.append(f"- honest refusal: {shape['refused']} ({shape['refused_pct']:.1f}%)")
        if shape["answered_no_citation"]:
            lines.append(f"- answered with NO citation (worth investigating): "
                         f"{shape['answered_no_citation']}")

    lines.append("")
    lines.append("## Capture latency")
    lines.append(f"- capture -> filed:      {_render_latency(report['capture_to_filed_latency'])}")
    lines.append(f"- capture -> searchable: "
                 f"{_render_latency(report['capture_to_searchable_latency'])}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="stigmergy-pilot-report",
        description="The pilot-checkpoint table, from real audit_log/capture_queue rows. "
                    "Reads only — no flag here writes anything.")
    ap.add_argument("--dsn", default=None, help=f"Postgres DSN (default: ${store.DSN_ENV})")
    ap.add_argument("--since", default=None,
                    help="ISO date (YYYY-MM-DD) — only ask calls/questions from this date "
                        "onward; default: all history")
    ap.add_argument("--json", action="store_true", help="machine-readable output for the record")
    args = ap.parse_args(argv)

    since = datetime.strptime(args.since, "%Y-%m-%d") if args.since else None

    conn = store.connect(args.dsn)
    try:
        # No DDL here. "It reads; it writes nothing" rules out `ensure_audit_table`/
        # `ensure_capture_schema` too, because a read-only database role cannot execute
        # `CREATE TABLE`/`ALTER TABLE` even when it is a no-op against a table that already
        # exists. A missing table surfaces as an honest "no data yet" (`read_meta`-style absence,
        # not a crash) rather than this command silently provisioning schema on a read-only
        # reporting run.
        report = build_report(conn, since=since)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
