"""`stigmergy-queue` — the operator's view of the write path, without a SQL client.

Five subcommands (`list`, `show`, `claim`, `reclaim`, `purge`), each a thin skin over the library —
the same seams the server and the librarian call. Nothing here decides a capture's fate, and
nothing here is waiting to: a capture files on its own, and the identity it introduces is born
confirmed by whoever captured. `claim` deliberately processes nothing (draining is the librarian's
job) and holding a claim is how a dead worker is simulated.

Errors here are LOCAL and may be specific — generic over HTTP, specific in a local CLI. This
module is the ONLY place in `stigmergy.capture` that opens a database connection or reads the
environment: the library takes `conn` as an argument.
"""
import argparse
import json
import sys
import time

import psycopg

from stigmergy import text as textutil
from stigmergy.capture import queue, retention, schema
from stigmergy.capture.errors import CaptureError
from stigmergy.capture.render import (
    RECLAIM_NOW,
    clean_for_terminal,
    depth_line,
    format_ms,
)
from stigmergy.index import store

_DUMP = {"ensure_ascii": False, "indent": 2}

# The worker's DEFAULT lease, for the reclaim refusal's example command. Spelled here rather
# than imported: `librarian.config` (where the derivation lives) imports THIS package's queue,
# so the reverse import would be a cycle — the duplication is declared instead, and
# `tests/capture/test_cli.py` pins this number to
# `librarian.config.DEFAULT_VISIBILITY_TIMEOUT_S` so it cannot drift from the arithmetic it
# copies (2 agent attempts × 300s + 120s gates + 180s headroom).
WORKER_DEFAULT_LEASE_S = 900

# `list`'s `kind` column width, computed from `schema.KINDS` so the next kind cannot break the
# column's alignment (a hand-picked width did, when `meeting` joined).
_KIND_WIDTH = max(len(k) for k in schema.KINDS)

# Ctrl-C exits 130 (128 + SIGINT) — what CPython already returns uncaught, minus the traceback.
# Not 0: an interrupted `claim --hold` leaves a real orphaned lease behind.
EXIT_INTERRUPTED = 130

def connect(dsn: str | None):
    """The connection every operator CLI in this package opens, schema included: each of them may
    be the first thing ever to run against this database."""
    conn = store.connect(dsn)
    schema.ensure_capture_schema(conn)   # idempotent: the CLI may be the first thing to run
    return conn


def _connect(args):
    """`stigmergy-queue`'s own call, taking the parsed namespace its `main` holds."""
    return connect(args.dsn)


# ── list ──────────────────────────────────────────────────────────────────────────────────────
def _cmd_list(conn, args) -> int:
    rows = queue.query_submissions(conn, submitter=args.submitter, statuses=args.status or None,
                                   limit=args.limit)
    if args.json:
        print(json.dumps({"counts": queue.counts_by_status(conn), "submissions": rows}, **_DUMP))
        return 0
    print(depth_line(queue.counts_by_status(conn)))
    if not rows:
        print("no submissions")
        return 0
    for row in rows:
        flags = f" flagged={','.join(row['flagged_hints'])}" if row["flagged_hints"] else ""
        print(f"#{row['id']} {row['status']:<11} {row['kind']:<{_KIND_WIDTH}} {row['submitted_by']}"
              f" attempts={row['attempts']} {row['created_at']}{flags}")
        # Three ways a row has nothing to show, each named. The withheld sentence is the queue's
        # own, not captured text — neither cleaned nor clipped.
        if row["payload_purged"]:
            body = "(payload purged)"
        elif row["withheld_reason"]:
            body = f"({row['withheld_reason']})"
        else:
            body = clean_for_terminal(row["excerpt"], 100)
        print(f"    {body}")
        _print_note(row, one_line=True)
    return 0


def _print_note(row: dict, *, one_line: bool) -> None:
    """The row's `error` line — why it is where it is — word-safe clipped."""
    if row["error"]:
        print(f"    ! {clean_for_terminal(row['error'], 200)}" if one_line else
              f"  note        {clean_for_terminal(row['error'], 300)}")


# ── show ──────────────────────────────────────────────────────────────────────────────────────
def _cmd_show(conn, args) -> int:
    trace = queue.get_submission_trace(conn, args.id)
    if trace is None:
        print(f"stigmergy-queue: no submission {args.id}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(trace, **_DUMP))
        return 0
    print(f"#{trace['id']} {trace['status']} ({trace['kind']}) by {trace['submitted_by']}")
    print(f"  created_at  {trace['created_at'] or '—'}")
    print(f"  claimed_at  {trace['claimed_at'] or '—'}  (queue wait: "
          f"{format_ms(trace['queue_wait_ms'])})")
    print(f"  finished_at {trace['finished_at'] or '—'}  (total: "
          f"{format_ms(trace['total_latency_ms'])})")
    print(f"  attempts    {trace['attempts']}")
    print(f"  blob_refs   {', '.join(trace['blob_refs']) or '(none)'}")
    if trace["result_ref"]:
        print(f"  result_ref  {trace['result_ref']}")
    _print_note(trace, one_line=False)
    if trace["withheld_reason"]:
        print(f"  material    ({trace['withheld_reason']})")
    for event in trace["events"]:
        # The row's own history — what was done to it while captures could still park, and the
        # migration that returned it to the queue. Sanitized but not clipped: a note cut in half
        # misinforms the person reading it.
        kind = str(event.get("event", ""))
        print(f"  · {clean_for_terminal(event.get('at', ''), 40)}  {clean_for_terminal(kind, 20)}"
              f"  by {clean_for_terminal(event.get('actor', ''), 80) or '—'}")
        for line in textutil.sanitize(str(event.get("note") or "")).splitlines():
            print(f"      {line}")
    if trace["payload_purged"]:
        print("  payload     (purged by retention; the evidence blob is unaffected)")
    return 0


# ── claim / reclaim ───────────────────────────────────────────────────────────────────────────
def _cmd_claim(conn, args) -> int:
    item = queue.claim_next(conn, visibility_timeout_s=args.visibility_timeout,
                            max_attempts=args.max_attempts)
    if item is None:
        print("nothing to claim (the queue has no queued items)")
        return 0
    if args.json:
        print(json.dumps(item, **_DUMP), flush=True)
    else:
        print(f"claimed #{item['id']} ({item['kind']}) by {item['submitted_by']} "
              f"attempts={item['attempts']}", flush=True)
        # Deliberately outside the withheld rule: this hands the operator exactly what a WORKER
        # receives; the secrets/PII gate runs downstream of delivery, not before it.
        print(f"    {clean_for_terminal((item['payload'] or {}).get('text', ''), 200)}", flush=True)
    if args.hold:
        print(f"holding the claim for {args.hold}s — kill this process to simulate a dead worker; "
              f"the item returns to the queue {args.visibility_timeout}s after it was claimed",
              flush=True)
        try:
            time.sleep(args.hold)
        except KeyboardInterrupt:
            # The ONE invited interruption, caught here where we still know WHICH submission now
            # holds an orphaned lease — a fact `main`'s generic handler cannot reconstruct.
            return _report_orphaned_lease(item, args)
    print(f"exiting WITHOUT finishing #{item['id']} — this command drains nothing; the librarian "
          "is what will file it", flush=True)
    return 0


def _report_orphaned_lease(item: dict, args) -> int:
    """What the operator needs after interrupting a held claim: which row is stranded and the two
    ways it comes back. `--json` gets a JSON object, never prose. The recovery command names
    `--visibility-timeout 0`, NOT this run's configured timeout: on `claim` the value is how long
    the lease lasts, on `reclaim` how old a claim must be to be released — echoing the lease
    releases nothing at second zero."""
    recovery = RECLAIM_NOW
    if args.json:
        print(json.dumps({
            "event": "claim_interrupted",
            "id": item["id"],
            "status": schema.CLAIMED,
            "attempts": item["attempts"],
            "visibility_timeout_s": args.visibility_timeout,
            "orphaned_lease": True,
            "recovers": f"automatically once the {args.visibility_timeout}s visibility timeout "
                        f"elapses, or immediately via `{recovery}`",
        }, **_DUMP), flush=True)
        return EXIT_INTERRUPTED
    print(f"\ninterrupted while holding the claim on #{item['id']} — which is exactly what a dead "
          f"worker leaves behind, so this is the demo working, not a failure.", flush=True)
    print(f"  #{item['id']} is still '{schema.CLAIMED}' (delivery {item['attempts']}) and nothing "
          f"will finish it: the lease is orphaned.", flush=True)
    print(f"  it returns to the queue on its own {args.visibility_timeout}s after it was claimed, "
          f"or immediately with:  {recovery}", flush=True)
    return EXIT_INTERRUPTED


def _cmd_reclaim(conn, args) -> int:
    # No default, deliberately: this CLI cannot see the worker's lease, and a wrong guess
    # requeues a capture out from under a process still filing it.
    if args.visibility_timeout is None:
        print(f"stigmergy-queue: reclaim needs --visibility-timeout — how old a claim must be "
              f"before its worker is presumed dead. There is no safe default: this command "
              f"cannot see the worker's configured lease, and a horizon shorter than that lease "
              f"requeues captures out from under running workers.\n"
              f"  after killing a worker, to force redelivery now:\n"
              f"    stigmergy-queue reclaim --visibility-timeout 0\n"
              f"  to sweep genuinely abandoned claims, pass the worker's own lease — read it "
              f"with `stigmergy-librarian status --json` (.visibility_timeout_s; "
              f"{WORKER_DEFAULT_LEASE_S} by default, "
              f"derived from $STIGMERGY_LIBRARIAN_TIMEOUT_S):\n"
              f"    stigmergy-queue reclaim --visibility-timeout {WORKER_DEFAULT_LEASE_S}",
              file=sys.stderr)
        return 2
    result = queue.release_expired(conn, visibility_timeout_s=args.visibility_timeout,
                                   max_attempts=args.max_attempts)
    print(json.dumps(result, **_DUMP) if args.json else
          f"released {result['released']} expired claim(s); failed {result['failed']} "
          f"item(s) that exhausted {args.max_attempts} attempts")
    return 0


# ── purge ─────────────────────────────────────────────────────────────────────────────────────
def _cmd_purge(conn, args) -> int:
    result = retention.purge(conn, older_than_days=args.older_than_days, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, **_DUMP))
        return 0
    verb = "would purge" if args.dry_run else "purged"
    print(f"{verb} payload+hints of {result['purged']} terminal submission(s): the retention "
          f"window ({args.older_than_days} days) plus any secrets/personal-data rejection, "
          f"whatever its age"
          + (f": {', '.join(str(i) for i in result['ids'])}" if result["ids"] else ""))
    print("(id, submitter, timestamps, status and result_ref survive; the evidence blobs have "
          "their own lifecycle and were not touched)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="stigmergy-queue",
        description="Operate the durable capture queue: inspect, claim, reclaim, purge.")
    ap.add_argument("--dsn", default=None,
                    help=f"Postgres DSN (default: ${store.DSN_ENV} or {store.DSN_DEFAULT})")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="submissions, newest first, with queue depth per status")
    p_list.add_argument("--status", action="append", choices=schema.STATUSES,
                        help="filter by status, repeatable")
    p_list.add_argument("--submitter", default=None, help="only this identity's submissions")
    p_list.add_argument("--limit", type=int, default=queue.DEFAULT_LIST_LIMIT)
    p_list.set_defaults(fn=_cmd_list)

    p_show = sub.add_parser("show", help="one submission's trace and latencies")
    p_show.add_argument("id", type=int)
    p_show.set_defaults(fn=_cmd_show)

    p_claim = sub.add_parser("claim",
                             help="claim one item (does NOT process it — this command files "
                                  "nothing)")
    p_claim.add_argument("--hold", type=float, default=0.0,
                         help="hold the claim this many seconds before exiting (kill the process "
                              "mid-hold to simulate a dead worker)")
    p_claim.set_defaults(fn=_cmd_claim)

    p_reclaim = sub.add_parser(
        "reclaim", help="return timed-out claims to the queue (--visibility-timeout 0 = all of "
                        "them, now)")
    p_reclaim.set_defaults(fn=_cmd_reclaim)

    # Same flag name, two meanings: on `claim` it is how long THIS lease lasts, on `reclaim` how
    # old a claim must be to be released — which is also why only `claim` can default: `reclaim`
    # states when someone else's work may be seized, and this CLI cannot see that worker's lease.
    p_claim.add_argument("--visibility-timeout", type=int,
                         default=queue.DEFAULT_VISIBILITY_TIMEOUT_S,
                         help="seconds this claim is held before the queue assumes the worker died")
    p_reclaim.add_argument("--visibility-timeout", type=int, default=None,
                           help="REQUIRED: release claims older than this many seconds; 0 releases "
                                "EVERY claimed row right now, which is what you want after killing "
                                "a held claim")
    for parser in (p_claim, p_reclaim):
        parser.add_argument("--max-attempts", type=int, default=queue.DEFAULT_MAX_ATTEMPTS,
                            help="deliveries before an item is failed instead of requeued")

    p_purge = sub.add_parser("purge", help="retention: delete payload+hints of old terminal rows")
    p_purge.add_argument("--older-than-days", type=int, default=retention.DEFAULT_RETENTION_DAYS)
    p_purge.add_argument("--dry-run", action="store_true", help="list what would go, change nothing")
    p_purge.set_defaults(fn=_cmd_purge)
    return ap


def _interrupted(during: str) -> int:
    """The generic Ctrl-C net: one honest line, no traceback, on STDERR — a diagnostic, so a
    `--json` invocation keeps a parseable stdout. `_report_orphaned_lease` is the deliberate
    exception: its interruption is invited and its message is the run's useful result."""
    print(f"stigmergy-queue: interrupted {during} — nothing was left half-written (every queue "
          f"transition is a single statement); re-run when ready", file=sys.stderr)
    return EXIT_INTERRUPTED


def _stack_down(ex: Exception) -> int:
    """This door's ONE sentence for the stack being unreachable, at connect time and mid-command
    alike — the same fault must not read as two different problems depending on which statement
    hit it. Errors are LOCAL and specific here, so the real reason travels in the parentheses."""
    print(f"stigmergy-queue: cannot reach the queue database ({ex}); is Postgres up "
          f"(`make db-up`)?", file=sys.stderr)
    return 2


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        conn = _connect(args)
    except KeyboardInterrupt:
        return _interrupted("while connecting to the queue database")
    except Exception as ex:  # noqa: BLE001 — a local operator needs the real reason, not a class
        return _stack_down(ex)
    try:
        return args.fn(conn, args)
    except CaptureError as ex:
        print(f"stigmergy-queue: {ex}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # The net for every command `_cmd_claim` does not handle specifically.
        return _interrupted(f"during `{args.command}`")
    except (psycopg.OperationalError, psycopg.InterfaceError) as ex:
        # The connect is not where the stack goes away: Postgres restarting, a stopped container
        # or a dropped socket lands HERE, inside the command body. Unguarded, it escaped as a
        # traceback and exit 1 — the code a named refusal uses, so a wrapper read "bad input".
        return _stack_down(ex)
    except Exception as ex:  # noqa: BLE001 — a local operator needs the real reason, not a crash
        # Everything else. This used to share the arm above, so a `KeyError` in a command body
        # told the operator Postgres was down — a DIAGNOSIS, and a wrong one: they went and checked
        # a database that was up while the real fault stayed unnamed. Same exit 2 and same "no
        # traceback" posture; only the sentence stops claiming to know what happened.
        print(f"stigmergy-queue: unexpected fault during `{args.command}` "
              f"({ex.__class__.__name__}: {ex})", file=sys.stderr)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
