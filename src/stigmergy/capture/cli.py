"""`stigmergy-queue` — the steward's view of the write path, without a SQL client.

Eight subcommands (`list`, `show`, `claim`, `reclaim`, `requeue`, `resolve`, `reject`, `purge`),
each a thin skin over the library — the same seams the server and the librarian call. The three
dispositions are the steward's drain out of a park; `claim` deliberately processes nothing
(draining is the librarian's job) and holding a claim is how a dead worker is simulated.

Errors here are LOCAL and may be specific — generic over HTTP, specific in a local CLI. This
module is the ONLY place in `stigmergy.capture` that opens a database connection or reads the
environment: the library takes `conn` as an argument.
"""
import argparse
import json
import os
import sys
import time

import psycopg

from stigmergy import text as textutil
from stigmergy.capture import dispositions, evidence, queue, retention, schema
from stigmergy.capture.errors import CaptureError, SubmissionRejected
from stigmergy.capture.render import (
    RECLAIM_NOW,
    clean_for_terminal,
    depth_line,
    format_age,
    format_ms,
)
from stigmergy.index import store

_DUMP = {"ensure_ascii": False, "indent": 2}

# The worker's DEFAULT lease, for the reclaim refusal's example command. Spelled here rather
# than imported: `librarian.config` (where the derivation lives) imports THIS package's queue,
# so the reverse import would be a cycle — the duplication is declared instead, and
# `tests/capture/test_cli.py` pins this number to
# `librarian.config.DEFAULT_VISIBILITY_TIMEOUT_S` so it cannot drift from the arithmetic it
# copies (2 agent attempts × 300s + 120s gates + 390s conversion + 180s headroom).
WORKER_DEFAULT_LEASE_S = 1290

# `list`'s `kind` column width, computed from `schema.KINDS` so the next kind cannot break the
# column's alignment (a hand-picked width did, when `meeting` joined).
_KIND_WIDTH = max(len(k) for k in schema.KINDS)

# Ctrl-C exits 130 (128 + SIGINT) — what CPython already returns uncaught, minus the traceback.
# Not 0: an interrupted `claim --hold` leaves a real orphaned lease behind.
EXIT_INTERRUPTED = 130

# ── the drop doors' shared configuration guard ────────────────────────────────────────────────
# Below BOTH drop CLIs, so no future door can skip it. Distinct from `main`'s catch-all 2: a
# wrapper must be able to tell "refused by policy, nothing happened" from "infrastructure down".
EXIT_SPLIT_STORES = 3

# The operator identity `--submitted-by` defaults to. Single-operator traffic: one env var is
# the whole of "configured" — there is no identity service to resolve against, and every drop
# door answers to the SAME one.
OPERATOR_EMAIL_ENV = "STIGMERGY_MEETING_OPERATOR_EMAIL"


def add_split_stores_flag(parser) -> None:
    """The escape hatch, spelled once. The predicate is a heuristic — a tailnet-reachable store is
    conceivable — so an override exists; it is loud, never silent."""
    parser.add_argument("--allow-split-stores", action="store_true",
                        help="proceed even when the queue is remote and the evidence store is on "
                             "this machine — the combination that files a row whose bytes the "
                             "worker can never read")


def refuse_split_stores(args, prog: str, ev) -> int:
    """`0` to proceed, `EXIT_SPLIT_STORES` when the queue and the evidence plane provably belong
    to different deployments and the operator has not said they mean it. Called FIRST in a drop,
    before anything is read, fetched, uploaded or inserted; building the store does no I/O, so
    asking it where it points is free."""
    reason = evidence.split_stores_reason(
        db_host=store.host_of_dsn(getattr(args, "dsn", None) or store.dsn()),
        endpoint_url=ev.endpoint_url)
    if not reason:
        return 0
    if not getattr(args, "allow_split_stores", False):
        print(f"{prog}: {reason}", file=sys.stderr)
        return EXIT_SPLIT_STORES
    print(f"{prog}: --allow-split-stores: {reason.splitlines()[0]} Proceeding anyway — expect "
          f"`stigmergy-queue show <id>` to report an evidence failure once it is claimed.",
          file=sys.stderr)
    return 0


def connect(dsn: str | None):
    """The connection every operator CLI in this package opens, schema included: each of them may
    be the first thing ever to run against this database."""
    conn = store.connect(dsn)
    schema.ensure_capture_schema(conn)   # idempotent: the CLI may be the first thing to run
    return conn


def _connect(args):
    """`stigmergy-queue`'s own call, taking the parsed namespace its `main` holds."""
    return connect(args.dsn)


def add_submitted_by_flag(parser) -> None:
    """`--submitted-by`, spelled once for every drop door — the flag and its default are the same
    fact on all of them."""
    parser.add_argument("--submitted-by", default="",
                        help=f"defaults to ${OPERATOR_EMAIL_ENV}; who this drop is attributed to")


def resolve_submitted_by(args) -> str:
    """The operator identity a drop is attributed to, or a refusal. Attribution is never taken
    from the material: an operator CLI has no identity service to resolve against, so the flag or
    the environment is the whole of it."""
    submitted_by = args.submitted_by or os.environ.get(OPERATOR_EMAIL_ENV, "")
    if not submitted_by:
        raise SubmissionRejected(
            f"--submitted-by is required (or set ${OPERATOR_EMAIL_ENV}) — attribution comes from "
            f"a resolved identity on every other capture surface, and this operator CLI has none "
            f"to resolve. Nothing was uploaded and nothing was queued.")
    return submitted_by


def drop_interrupted(prog: str, during: str = "") -> int:
    """Ctrl-C during a drop: what may and may not be left behind, in the one paragraph both doors
    tell it in. The insert is a single statement, so the queue is never half-written; an upload
    that already finished leaves an object no row points at, which is inert."""
    said = f" {during}" if during else ""
    print(f"{prog}: interrupted{said} — no queue row was written (the insert is a single "
          f"statement), but if the upload had already finished, an evidence object with nothing "
          f"pointing at it may exist; that costs nothing and nothing will ever read it without a "
          f"row. Re-run `{prog} drop` when ready.", file=sys.stderr)
    return EXIT_INTERRUPTED


def drop_main(argv, *, parser: argparse.ArgumentParser, prog: str, during: str = "") -> int:
    """Every drop door's entry point: parse, dispatch, and map the three failure classes to the
    exit codes a wrapper reads — 1 a named refusal, 130 an interruption, 2 the stack being down.
    `during` names what the door was in the middle of, for the interrupt sentence."""
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except CaptureError as ex:
        print(f"{prog}: {ex}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return drop_interrupted(prog, during)
    except Exception as ex:  # noqa: BLE001 — a local operator needs the real reason
        print(f"{prog}: cannot reach the queue database or evidence store ({ex}); is the stack up "
              f"(`make db-up`)?", file=sys.stderr)
        return 2


# A steward's own words, headed for a submitter's report. The cleaning lives in
# `dispositions.clean`, below every CLI, so no CLI can skip it; the local name keeps call sites
# readable.
_steward_note = dispositions.clean


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
        # Who is being waited on, and for how long — the two facts a steward triages a list on.
        parked = (f" waiting on: {row['waiting_on']} · parked {format_age(row['parked_age_ms'])}"
                  if row["waiting_on"] else "")
        print(f"#{row['id']} {row['status']:<11} {row['kind']:<{_KIND_WIDTH}} {row['submitted_by']}"
              f" attempts={row['attempts']} {row['created_at']}{flags}{parked}")
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
    """The row's `error`/`question` line — and the one place a `needs_input` row is NOT clipped:
    a parked question ENDS in the exact command to run, and a clip cuts `brain_reply(...)`
    mid-call. `show` prints the question whole; `list` prints only the invocation, from
    `schema.reply_invocation` — the function that built the sentence, so it cannot drift or be
    cut. Every other status keeps a word-safe clipped one-liner."""
    if row["status"] == schema.NEEDS_INPUT:
        invocation = schema.reply_invocation(row["id"])
        if one_line:
            print(f"    ? waiting on {row['waiting_on']} — answer with:  {invocation}")
            print(f"      (the full question: `stigmergy-queue show {row['id']}`)")
        else:
            print("  question")
            for line in textutil.sanitize(row["error"] or "").splitlines():
                print(f"    {line}")
            print(f"  answer with {invocation}")
        return
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
    if trace["waiting_on"]:
        print(f"  parked      {format_age(trace['parked_age_ms'])} ago — waiting on: "
              f"{trace['waiting_on']}")
    _print_note(trace, one_line=False)
    if trace["reply"]:
        print(f"  reply       {clean_for_terminal(trace['reply'], 500)}")
    elif trace["withheld_reason"]:
        # Say why the reply is suppressed: an unexplained empty line reads as "they never
        # answered" — a false story. The sentence is the queue's own; neither cleaned nor clipped.
        print(f"  reply       ({trace['withheld_reason']})")
    for event in trace["events"]:
        # Sanitized but not clipped: a note cut in half misinforms the steward reading it before
        # disposing. The `asked` note IS the question — suppressed while the row is still
        # `needs_input`, because the block above just printed it in full.
        kind = str(event.get("event", ""))
        print(f"  · {clean_for_terminal(event.get('at', ''), 40)}  {clean_for_terminal(kind, 20)}"
              f"  by {clean_for_terminal(event.get('actor', ''), 80) or '—'}")
        if kind == schema.EVENT_ASKED and trace["status"] == schema.NEEDS_INPUT:
            print("      (the question, printed above)")
            continue
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


# ── the steward's drain: requeue / resolve / reject ────────────────────────────────────────────
# Three commands over one guarded transition (`queue.dispose`); the state check is the
# DATABASE's, so a disposition typed a second after a worker claimed the row fails loudly.
# `--by` is ATTRIBUTION, not authorization: recorded, never checked — checking would be theatre
# on a local CLI whose operator already has the DSN.
def _cmd_requeue(conn, args) -> int:
    result = dispositions.requeue(conn, args.id, actor=args.by, note=_steward_note(args.note))
    if args.json:
        print(json.dumps(result, **_DUMP))
        return 0
    print(f"requeued #{result['id']} — back in the queue for the librarian to try again "
          f"(attempts unchanged at {result['attempts']}; it is claimable now)")
    return 0


def _cmd_resolve(conn, args) -> int:
    """Close a parked row as `resolved` — a steward handled it outside the fast lane.

    `resolve` with neither `--page` nor `--commit` leaves the submitter's report permanently
    silent about where the material went, on the one state whose point is that it WAS used —
    warned about, never prompted for: a blocking prompt in a scriptable tool hangs automation.
    """
    note = _steward_note(args.note)
    result = dispositions.resolve(conn, args.id, actor=args.by, note=note,
                                  page=args.page or "", commit=args.commit or "")
    warning = ("" if (args.page or args.commit) else
               f"resolved #{args.id} with no --page and no --commit — the submitter's report will "
               f"say only what your --note said, with no pointer to where the material went")
    if args.json:
        print(json.dumps({**result, "warning": warning}, **_DUMP))
        return 0
    print(f"resolved #{result['id']} — the submitter's report now says so")
    print(f"  page:   {args.page or '(none recorded)'}")
    print(f"  commit: {args.commit or '(none recorded)'}")
    if warning:
        print(f"stigmergy-queue: {warning}", file=sys.stderr)
    return 0


def _cmd_reject(conn, args) -> int:
    result = dispositions.reject(conn, args.id, actor=args.by, reason=_steward_note(args.reason))
    if args.json:
        print(json.dumps(result, **_DUMP))
        return 0
    print(f"rejected #{result['id']} (by: {args.by}) — reason recorded in the submitter's report")
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

    # ── the drain: `--by` and its help text identical on all three ───────────────────────────
    p_requeue = sub.add_parser(
        "requeue", help="send a parked row back to the queue for the librarian to try again")
    p_requeue.add_argument("--note", default="",
                           help="why, for the row's own history (not shown to the submitter)")
    p_requeue.set_defaults(fn=_cmd_requeue)

    p_resolve = sub.add_parser(
        "resolve", help=f"close a parked row as '{schema.RESOLVED}': you handled it by hand")
    p_resolve.add_argument("--note", required=True,
                           help="what you did with it, in the SUBMITTER's own report, verbatim — "
                                "never include a secret or personal data here")
    p_resolve.add_argument("--page", default="",
                           help="the page the material ended up in, echoed to the submitter")
    p_resolve.add_argument("--commit", default="",
                           help="the commit that carried it, echoed to the submitter")
    p_resolve.set_defaults(fn=_cmd_resolve)

    p_reject = sub.add_parser(
        "reject", help=f"close a parked row as '{schema.REJECTED}', with your name on the decision")
    p_reject.add_argument("--reason", required=True,
                          help="why this is declined, in the SUBMITTER's own report, verbatim — "
                               "never include a secret or personal data here")
    p_reject.set_defaults(fn=_cmd_reject)

    for parser in (p_requeue, p_resolve, p_reject):
        parser.add_argument("id", type=int)
        parser.add_argument("--by", required=True,
                            help="who is answering for this decision — recorded on the row's "
                                 "history, and named to the submitter by `resolve`/`reject`. "
                                 "Attribution, not authorization: this tool records who you say "
                                 "you are, it does not check it")

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
