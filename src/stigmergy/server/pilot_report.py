"""`stigmergy-pilot-report`: the measurement table — answer shape (answered-with-citation vs
honest refusal), capture->filed and capture->searchable latency percentiles, and questions per
identity per week (a per-scope signal, never an adoption metric).

Reads. Writes NOTHING, including no DDL — unlike every other CLI here it skips the idempotent
`ensure_*_schema`, so it works under a read-only database role. Every number comes from a column
something else already wrote (`audit_log.result`, `capture_queue`, `job_runs`) — no new
measurement channel.
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime

import psycopg

from stigmergy.capture import latency as latency_mod
from stigmergy.capture import queue as capture_queue
from stigmergy.index import store
from stigmergy.server.webhook import JOB_NAME as WEBHOOK_JOB_NAME

_PERCENTILES = latency_mod.PERCENTILES


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
    """The whole table as one JSON-able dict — `render` and `--json` both work from it, so the
    two can never disagree about what was measured."""
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

    # Refused before any I/O, as a return code rather than a traceback — the package's
    # console-entry posture (`server/errors.py`).
    try:
        since = datetime.strptime(args.since, "%Y-%m-%d") if args.since else None
    except ValueError:
        print(f"--since must be an ISO date (YYYY-MM-DD), not {args.since!r}", file=sys.stderr)
        return 2

    try:
        conn = store.connect(args.dsn)
    except psycopg.Error as ex:
        print(f"cannot reach the database: {ex.__class__.__name__}", file=sys.stderr)
        return 1
    try:
        # No DDL: a read-only role cannot execute CREATE/ALTER even as a no-op, so a
        # not-yet-provisioned database is a clean refusal below, never an empty report.
        report = build_report(conn, since=since)
    except psycopg.errors.UndefinedTable:
        print("this database has no stigmergy tables yet — run the server once to provision "
              "them, then re-run this report", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
