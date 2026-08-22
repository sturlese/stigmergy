"""`stigmergy-librarian` — the worker's command surface.

    stigmergy-librarian once      claim ONE item, process it, print what happened, exit
    stigmergy-librarian run       the loop: poll, sweep, drain, until a signal says stop
    stigmergy-librarian status    what is waiting, what is in flight, how fast filing has been

`run` is the same `process_next` inside `worker.Worker`'s signal handling — one filing path,
the loop adds only when to stop. `status` writes nothing, including no DDL. Conventions are
`stigmergy-queue`'s: exit 130 on Ctrl-C; `--json` emits the machine-readable value FIRST; the
empty queue prints that tool's byte-identical sentence; the depth line and durations come from
`capture.render`, imported, never re-rendered; errors are local and specific.

**Exit 0 for every terminal state correctly reached** — `rejected`, `triage` and `failed` are
the worker doing its job; non-zero is reserved for the TOOL failing to run. `run` is the one
subcommand Ctrl-C does not reach through `KeyboardInterrupt` (`Worker` installs its own
handlers, stops after the item in flight, exits 0); `_report_interrupt` covers the window
before those handlers exist, and every other subcommand.
"""
import argparse
import json
import sys

import psycopg

from stigmergy.capture import evidence as evidence_plane
from stigmergy.capture import latency, schema
from stigmergy.capture import queue as capture_queue
from stigmergy.capture.render import (  # imported, never retyped — see `_report_interrupt`, `_cmd_status`
    RECLAIM_NOW,
    depth_line,
    format_ms,
)
from stigmergy.index import store
from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import config, report, worker
from stigmergy.librarian.errors import LibrarianConfigError, LibrarianError

_DUMP = {"ensure_ascii": False, "indent": 2}

EXIT_INTERRUPTED = 130      # 128 + SIGINT(2) — identical to stigmergy-queue
EXIT_CONFIG = 2             # the tool cannot run: config, database, missing dependency
EXIT_ERROR = 1              # the tool ran and hit a local error

# Byte-identical to `stigmergy-queue claim`'s empty-queue sentence. If that one ever changes,
# this must change with it — they describe the same state to the same person.
NOTHING_TO_CLAIM = "nothing to claim (the queue has no queued items)"


NO_SCHEMA_YET = ("the capture queue has no schema in this database yet — nothing has been submitted "
                 "or drained against it. Run `stigmergy-librarian once` or `stigmergy-queue` (either "
                 "creates it), or check --dsn: this may not be the database you meant")


def _connect(args):
    """Connect, and create the schema for the subcommands that WRITE. `status` is exempt:
    `ensure_capture_schema` executes DDL, and a command documented as read-only must not require
    DDL privileges to run."""
    conn = store.connect(args.dsn)
    if getattr(args, "ensure_schema", True):
        schema.ensure_capture_schema(conn)   # idempotent; the CLI may be the first thing to run
    return conn


def _cmd_once(conn, args, settings) -> int:
    """Claim and process exactly one item.

    Two things happen before the claim: the base ref is reported (`origin/<branch>` is not what
    an operator assumes while looking at their own working copy, and one line settles "why did
    it not see my commit"), and stranded claims are swept VISIBLY — `once` drains by hand, so an
    item left `claimed` by an interrupted run otherwise looks permanently stuck.
    """
    resolved = worker.startup_checks(settings)
    deps = worker.build_deps(settings, resolved, evidence_plane.store_from_env())
    base = resolved["base"]
    swept = worker.swept_clause(worker.sweep(conn, settings), settings)

    outcome = worker.process_next(conn, deps)
    if outcome is None:
        if not args.json:
            print(_preamble(settings, base, swept))
        print(NOTHING_TO_CLAIM)
        return 0

    item, result = outcome
    if args.json:
        # `diagnostics_path` rides along for the same reason the prose branch prints it: a
        # machine consumer that cannot find the preserved diff has to go and ask a human. The
        # FILE is named, never its contents — the digest inside withholds every added line.
        print(json.dumps({"id": item["id"], "status": result.status,
                          "result_ref": result.result_ref, "report": result.report,
                          "base": {"ref": base.ref, "commit": base.sha},
                          "diagnostics_path": result.diagnostics_path or "",
                          "swept": swept}, **_DUMP), flush=True)
        return 0
    print(_preamble(settings, base, swept))
    print(f"#{item['id']} {report.render_prose(result.report)}", flush=True)
    if result.diagnostics_path:
        # An operator line, on stderr, naming a FILE — never its contents. The digest inside it
        # withholds every added line for the same reason a refusal never echoes a value.
        print(f"  the refused diff is preserved for diagnosis at {result.diagnostics_path}",
              file=sys.stderr)
    return 0


def _cmd_run(conn, args, settings) -> int:
    """The loop. One worker active by configuration, draining until a signal says stop.

    Everything expensive is validated ONCE, before the first claim, by the same
    `worker.startup_checks` `once` uses — a malformed `ops/acl.json` in a long-running loop
    would otherwise become N identical `failed` rows. The preamble is the same line `once`
    prints, from the same function; the sweep line comes from `Worker.run`, because the loop
    sweeps every pass and printing the first one twice would misreport it. Returns 0 for a clean
    stop, Ctrl-C included — a loop that exited non-zero when told to stop would fail every
    supervisor's restart policy.
    """
    resolved = worker.startup_checks(settings)
    deps = worker.build_deps(settings, resolved, evidence_plane.store_from_env())

    # Handlers FIRST, before a single line is printed. Everything that waits on this loop waits
    # for a printed line and then signals, so any window between the first line and
    # `install_signal_handlers` is one in which SIGTERM hits the DEFAULT disposition and kills
    # the process with 143 — the non-zero exit this function's own docstring forbids.
    loop = worker.Worker(conn, deps, on_output=lambda line: print(line, flush=True))
    loop.install_signal_handlers()

    print(_preamble(settings, resolved["base"], ""), flush=True)
    # `:g` rather than `worker.human_duration` for the poll interval alone: it is the one
    # tunable whose sensible values are sub-second, and `human_duration` truncates to whole
    # seconds — it would print "polling every 0s".
    print(f"  polling every {settings.poll_interval_s:g}s; "
          f"lease {worker.human_duration(settings.visibility_timeout_s)}; "
          f"Ctrl-C stops after the item in flight", flush=True)

    processed = loop.run()
    print(f"stopped after {processed} item(s)", flush=True)
    return 0


def _cmd_status(conn, args, settings) -> int:
    """What is waiting, what is in flight, and how fast filing has actually been. Writes
    nothing — including no DDL, which is why `_connect` skips `ensure_capture_schema` here.

    Deliberately does NOT call `worker.startup_checks`: an operator reaching for `status` is
    often doing so BECAUSE something is misconfigured, and a status command that refuses to
    answer until the config is valid is useless exactly when it is needed. It says which
    configured values it compared against, so a stale-lease verdict can be checked.
    """
    try:
        counts = capture_queue.counts_by_status(conn)
        in_flight = capture_queue.query_in_flight(
            conn, visibility_timeout_s=settings.visibility_timeout_s)
        measured = latency.summarize(capture_queue.filed_latencies_ms(conn))
    except psycopg.errors.UndefinedTable:
        # The consequence of reading without creating: an empty database answers "no schema"
        # rather than crashing on the first SELECT — an operator told "cannot reach the queue
        # database" about a database they are demonstrably connected to goes hunting for a
        # network fault.
        print(f"stigmergy-librarian: {NO_SCHEMA_YET}", file=sys.stderr)
        return EXIT_CONFIG

    if args.json:
        print(json.dumps({
            "counts": counts,
            "visibility_timeout_s": settings.visibility_timeout_s,
            "max_attempts": settings.max_attempts,
            "in_flight": in_flight,
            "latency": measured.as_json(),
        }, **_DUMP))
        return 0

    print(depth_line(counts))
    if not in_flight:
        print("in flight: nothing claimed")
    for row in in_flight:
        # The verdict, then the two numbers it was reached from — never the verdict alone: with
        # the age and the lease beside it an operator can see the arithmetic and act on it.
        #
        # THREE verdicts, not two: `queue.release_expired` SPLITS the expired set, and a row
        # that has burned every delivery is `finish`ed as `failed` rather than returned — so the
        # expired sentence must not promise a requeue for the row the sweep is about to fail.
        held = format_ms(row["claimed_age_ms"])
        exhausted = int(row["attempts"]) >= int(settings.max_attempts)
        if not row["lease_expired"]:
            verdict = "within its lease — a worker is presumably on it"
        elif exhausted:
            verdict = (f"LEASE EXPIRED and every delivery is burned "
                       f"({row['attempts']}/{settings.max_attempts}) — the next sweep FAILS this "
                       f"row rather than returning it to the queue, and records an ingest error")
        else:
            verdict = ("LEASE EXPIRED — a live worker would have finished or renewed it by now; "
                       "the next sweep returns it to the queue with an attempt burned")
        print(f"in flight: #{row['id']} ({row['kind']}) by {row['submitted_by']} "
              f"attempts={row['attempts']}/{settings.max_attempts} held {held} of "
              f"{worker.human_duration(settings.visibility_timeout_s)}")
        print(f"  {verdict}")
        if row["lease_expired"] and not exhausted:
            # Suppressed in the exhausted branch: reclaiming a row with no deliveries left fails
            # it too, so offering it there would be advice that does the opposite of what it says.
            print(f"  to return it right now, with no librarian running:  {RECLAIM_NOW}")
    print(latency.render(measured))
    return 0


def _preamble(settings, base, swept: str) -> str:
    """The one context line `once` prints before it works: where it files and what it repaired."""
    lines = [f"filing into {settings.repo} against {base.describe()}"]
    if swept:
        lines.append(f"  {swept}")
    return "\n".join(lines)


def _report_interrupt(args, settings) -> int:
    """What an operator needs after Ctrl-C, with three properties:

    - **the duration is named**, from `settings` — the RESOLVED configuration, so a
      `--visibility-timeout` on this very command line is the number printed;
    - **the recovery command is named**, from `capture.render.RECLAIM_NOW` — imported rather than
      retyped, because two tools printing the same recovery advice must not disagree about it —
      with the caveat that makes it safe to follow: `--visibility-timeout 0` releases EVERY held
      claim, and a second worker mid-item would then double-file the capture;
    - **it does NOT claim nothing was committed** — an interrupt can land after the push and
      before the row is finished, and ruling that out would tell an operator not to look at the
      one place they need to.
    """
    print(f"\nstigmergy-librarian: interrupted during `{args.command}` — any item claimed by this "
          f"run returns to the queue "
          f"{worker.visibility_timeout_clause(settings.visibility_timeout_s)}.\n"
          f"  if the interrupt landed after the push, the page is already committed: check "
          f"`git log` on the knowledge repo before resubmitting.\n"
          f"  to get the item back sooner, with no librarian running:  {RECLAIM_NOW}\n"
          f"  (that releases every held claim, so never run it while another worker is mid-item)",
          file=sys.stderr)
    return EXIT_INTERRUPTED


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="stigmergy-librarian",
        description="Drain the capture queue: claim one item, file it, commit it.")
    ap.add_argument("--dsn", default=None,
                    help=f"Postgres DSN (default: ${store.DSN_ENV} or {store.DSN_DEFAULT})")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--repo", default=None,
                    help=f"the knowledge repo to file into (default: ${config.REPO_ENV} or "
                         f"{config.REPO_DEFAULT})")
    ap.add_argument("--branch", default=None, help="branch to commit to (default: main)")
    # `choices` is the BACKENDS tuple itself, never a retyped list. A RETIRED value TYPED here
    # gets argparse's "invalid choice", and that is the right trade: the value that matters is
    # the CONFIGURED one — `$STIGMERGY_LIBRARIAN_BACKEND` out of a stale `fly.toml` or `.env`
    # never passes through `choices` and reaches `startup_checks` with its own sentence
    # (`agent.RETIRED_BACKENDS`). Somebody typing the flag is reading a list of two in the same
    # breath; a deployment carrying it is not reading anything.
    ap.add_argument("--backend", default=None, choices=agent_module.BACKENDS,
                    help="'pydantic' runs both flows structured — no tools, a gathered context, "
                         "code writes the page (ADR 033); it needs a provider-prefixed, priced "
                         "model and that provider's key. 'double' runs the offline double "
                         "(default: double)")
    sub = ap.add_subparsers(dest="command", required=True)

    p_once = sub.add_parser(
        "once", help="claim and process exactly ONE item, then exit (what a manual walk uses)")
    p_once.set_defaults(fn=_cmd_once)

    p_run = sub.add_parser(
        "run", help="the worker loop: poll, sweep, drain, until SIGINT/SIGTERM says stop")
    p_run.set_defaults(fn=_cmd_run)
    p_run.add_argument("--poll-interval", type=float, default=None,
                       help=f"seconds to wait before polling an empty queue again (default "
                            f"{config.DEFAULT_POLL_INTERVAL_S})")

    p_once.set_defaults(ensure_schema=True)
    p_run.set_defaults(ensure_schema=True)

    p_status = sub.add_parser(
        "status", help="queue depth, the item in flight and whether its lease looks stale, and "
                       "the measured capture->filed p50/p95 (reads only — no rows, no DDL)")
    # `ensure_schema=False`: this subcommand is documented as writing nothing, and
    # `ensure_capture_schema` is DDL. See `_connect`.
    p_status.set_defaults(fn=_cmd_status, ensure_schema=False)

    # SAME flag name as `stigmergy-queue`, because both sweep the same column. The DEFAULT is
    # not the same: an agent item can run two full agent attempts plus the gates, so the
    # librarian's is computed from its own per-item bounds (`config.minimum_visibility_timeout_s`)
    # and `worker.startup_checks` refuses anything below that.
    #
    # `default=None`, not the class default: with the default pre-filled, `from_args` cannot
    # tell an absent flag from an explicit `--visibility-timeout 0`, and the zero would be
    # silently replaced. `None` makes absence expressible; an explicit `0` reaches
    # `worker.startup_checks`, which refuses it out loud with the arithmetic. The default still
    # appears in the help text, where an operator actually looks for it.
    #
    # `status` gets the same flag because it REPORTS on the same lease: the staleness verdict is
    # meaningless unless computed against the timeout the worker is configured with.
    for parser in (p_once, p_run, p_status):
        parser.add_argument("--visibility-timeout", type=int, default=None,
                            help=f"seconds this worker's claim is held before the queue assumes it "
                                 f"died and returns the item — same column as `stigmergy-queue "
                                 f"reclaim --visibility-timeout`, but must exceed one item's worst "
                                 f"case (default {config.DEFAULT_VISIBILITY_TIMEOUT_S})")
    # `status` gets `--max-attempts` for the same reason: the sweep splits the expired set on
    # this exact number, and comparing against the class default while the worker runs with
    # another one makes the verdict promise a requeue for a row the sweep is about to fail.
    for parser in (p_once, p_run, p_status):
        parser.add_argument("--max-attempts", type=int, default=None,
                            help=f"deliveries before an item is failed instead of requeued "
                                 f"(default {config.Settings.max_attempts})")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = config.Settings.from_args(args)
    except (LibrarianConfigError, ValueError) as ex:
        print(f"stigmergy-librarian: {ex}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        conn = _connect(args)
    except KeyboardInterrupt:
        print("stigmergy-librarian: interrupted while connecting to the queue database",
              file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as ex:  # noqa: BLE001 — a local operator needs the real reason
        print(f"stigmergy-librarian: cannot reach the queue database ({ex}); is Postgres up "
              f"(`make db-up`)?", file=sys.stderr)
        return EXIT_CONFIG

    try:
        return args.fn(conn, args, settings)
    except LibrarianConfigError as ex:
        # Fail-closed startup validation: the loudest, most actionable line we have.
        print(f"stigmergy-librarian: {ex}", file=sys.stderr)
        return EXIT_CONFIG
    except LibrarianError as ex:
        print(f"stigmergy-librarian: {ex}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return _report_interrupt(args, settings)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
