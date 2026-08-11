"""`stigmergy-librarian` — the worker's command surface.

Three subcommands, and the order they were built in is the order they are useful in:

    stigmergy-librarian once      claim ONE item, process it, print what happened, exit
    stigmergy-librarian run       the loop: poll, sweep, drain, until a signal says stop
    stigmergy-librarian status    what is waiting, what is in flight, how fast filing has been

`once` came first deliberately — a loop you cannot single-step is a loop you cannot debug — and it
is still what a manual walk uses. `run` is the same `process_next` inside `worker.Worker`'s signal
handling, so there is exactly one filing path and the loop adds only when to stop.

**`status` writes to nothing at all — and that is now true.** It shipped saying so in three places
while `_connect` called `schema.ensure_capture_schema` for every subcommand, which executes DDL: the
"read-only" command created tables and indexes and needed DDL privileges to run. It is exempt from
that call now (`_connect`, keyed off `ensure_schema` on the subparser) and a database with no schema
gets a sentence naming that, rather than the generic connection failure.

Conventions are `stigmergy-queue`'s, not new ones — the two tools sit side by side in an
operator's terminal and must not speak different dialects:

- **exit 130 on Ctrl-C** (128 + SIGINT), the shell's own convention and what an uncaught
  `KeyboardInterrupt` already returns;
- **`--json` emits the machine-readable value FIRST**, prose after, so a consumer can read the
  leading value with `json.JSONDecoder().raw_decode`;
- **the empty queue prints the byte-identical sentence `stigmergy-queue claim` prints**, because
  two tools describing the same state differently is how an operator learns to distrust both;
- **the depth line and every measured duration come from `capture.cli`** (`depth_line`,
  `format_ms`) — imported, never re-rendered, for the same reason `RECLAIM_NOW` is imported;
- **errors are local and specific** — generic over HTTP, specific in a local CLI: an operator
  staring at a broken config needs the path, not a class name.

**Exit 0 for every terminal state correctly reached.** `rejected`, `triage` and `failed` are the
worker doing its job — a capture was refused, on purpose, with a reason. Non-zero is reserved
for the TOOL failing to run: a bad config, an unreachable database, an unknown flag. A CI step
that treated a correctly-refused secret as a build failure would be wrong about what happened.

**`run` is the one subcommand Ctrl-C does not reach through `KeyboardInterrupt`.** `Worker`
installs its own SIGINT/SIGTERM handlers — Ctrl-C is part of a loop's interface — so an
interrupt sets a flag, the loop says what it is doing and stops after the item
in flight — and exits 0, because a clean requested stop is not a failure. `_report_interrupt`
covers the window BEFORE those handlers are installed, and every other subcommand.
"""
import argparse
import json
import sys

import psycopg

from stigmergy.capture import evidence as evidence_plane
from stigmergy.capture import latency, schema
from stigmergy.capture import queue as capture_queue
from stigmergy.capture.cli import (  # imported, never retyped — see `_report_interrupt`, `_cmd_status`
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

# Byte-identical to `stigmergy-queue claim`'s empty-queue sentence. If that one ever changes, this
# must change with it — they describe the same state to the same person.
NOTHING_TO_CLAIM = "nothing to claim (the queue has no queued items)"


NO_SCHEMA_YET = ("the capture queue has no schema in this database yet — nothing has been submitted "
                 "or drained against it. Run `stigmergy-librarian once` or `stigmergy-queue` (either "
                 "creates it), or check --dsn: this may not be the database you meant")


def _connect(args):
    """Connect, and create the schema for the subcommands that WRITE.

    `status` is exempt, and that is a correction: `ensure_capture_schema` executes DDL, so a command
    documented three times over as "reads only, writes nothing" created tables and indexes and
    required DDL privileges. On a read-only role it failed with the generic "cannot reach the queue
    database" — precisely the misdiagnosis `_cmd_status`'s own docstring says it exists to avoid.
    """
    conn = store.connect(args.dsn)
    if getattr(args, "ensure_schema", True):
        schema.ensure_capture_schema(conn)   # idempotent; the CLI may be the first thing to run
    return conn


def _cmd_once(conn, args, settings) -> int:
    """Claim and process exactly one item.

    Two things happen before the claim, and both were absent:

    - **the base ref is reported.** The worktree branches from `origin/<branch>` when there is a
      remote, which is correct for a service and is not what an operator assumes while looking at
      their own working copy. Naming it costs one line and settles "why did it not see my commit".
    - **stranded claims are swept, visibly.** `queue.claim_next` sweeps on its own hot path, so the
      recovery was never missing — it was invisible, and `once` is exactly the surface where that
      matters: a walk drains by hand, so an item left `claimed` by an interrupted run looks
      permanently stuck until somebody happens to run the next command.
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
    `worker.startup_checks` `once` uses — a malformed `ops/acl.json` in a long-running loop would
    otherwise become N identical `failed` rows with the real cause buried.

    The preamble is the same line `once` prints, from the same function: an operator who has just
    read "filing into … against origin/main@abc" from one command must not have to learn a second
    layout to read it from the other. The sweep line comes from `Worker.run` rather than from here,
    because the loop sweeps on every pass and printing the first one twice would misreport it.

    Returns 0 for a clean stop — including a stop the operator asked for with Ctrl-C. A loop that
    exited non-zero when told to stop would fail every supervisor's restart policy and every CI
    step that ever drains a queue.
    """
    resolved = worker.startup_checks(settings)
    deps = worker.build_deps(settings, resolved, evidence_plane.store_from_env())

    # Handlers FIRST, before a single line is printed. Everything that waits on this loop —
    # every supervisor, the container e2e, `test_cli_run.py` — waits for a printed line and then
    # signals, so any window between the first line and `install_signal_handlers` is a window in
    # which SIGTERM hits the DEFAULT disposition and kills the process with 143. That is the
    # non-zero exit this function's own docstring says must never happen, and it is what made CI
    # run 30895512061 red. The preamble is the operator's, the handlers are the supervisor's, and
    # the supervisor can arrive first.
    loop = worker.Worker(conn, deps, on_output=lambda line: print(line, flush=True))
    loop.install_signal_handlers()

    print(_preamble(settings, resolved["base"], ""), flush=True)
    # `:g` rather than `worker.human_duration` for the poll interval alone: it is the one tunable
    # here whose sensible values are sub-second (the suite and the e2e use 0.1), and
    # `human_duration` truncates to whole seconds — it would print "polling every 0s".
    print(f"  polling every {settings.poll_interval_s:g}s; "
          f"lease {worker.human_duration(settings.visibility_timeout_s)}; "
          f"Ctrl-C stops after the item in flight", flush=True)

    processed = loop.run()
    print(f"stopped after {processed} item(s)", flush=True)
    return 0


def _cmd_status(conn, args, settings) -> int:
    """What is waiting, what is in flight, and how fast filing has actually been. Writes nothing —
    including no DDL, which is why `_connect` skips `ensure_capture_schema` for this subcommand.

    Deliberately does NOT call `worker.startup_checks`: an operator reaching for `status` is often
    doing so BECAUSE something is misconfigured, and a status command that refuses to answer until
    the config is valid is a status command that is useless exactly when it is needed. It reads the
    queue and reports; the two configured values it needs are the visibility timeout and
    `--max-attempts`, and it says which ones it compared against so a stale-lease verdict can be
    checked.
    """
    try:
        counts = capture_queue.counts_by_status(conn)
        in_flight = capture_queue.query_in_flight(
            conn, visibility_timeout_s=settings.visibility_timeout_s)
        measured = latency.summarize(capture_queue.filed_latencies_ms(conn))
    except psycopg.errors.UndefinedTable:
        # The consequence of reading without creating: an empty database now answers "no schema"
        # rather than crashing on the first SELECT. Named specifically — an operator told "cannot
        # reach the queue database" about a database they are demonstrably connected to goes hunting
        # for a network fault.
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
        # The verdict, then the two numbers it was reached from — never the verdict alone. "looks
        # stale" that cannot be checked is an assertion an operator has to trust; with the age and
        # the lease beside it they can see the arithmetic and act on it.
        #
        # **Three verdicts, not two.** The expired branch used to promise unconditionally that "the
        # next sweep returns it to the queue with an attempt burned", and `queue.release_expired`
        # SPLITS the expired set: a row that has burned every delivery is `finish`ed as `failed`
        # with an `ingest_errors` row and is not returned at all. With the default of 3 attempts
        # that is the most common genuinely-stuck case — a third delivery that died — so the
        # command was most wrong about exactly the row an operator was most likely to be staring
        # at, and the `RECLAIM_NOW` advice below would have failed it too rather than recovering it.
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
            # Suppressed in the exhausted branch: reclaiming a row with no deliveries left fails it
            # too, so offering it there would be advice that does the opposite of what it says.
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
    """What an operator needs after Ctrl-C: what state the queue is in, **how long** until it heals
    itself, how to make that happen now, and the one thing this message must not claim.

    Three properties, each of them a correction:

    - **The duration is named.** "after the visibility timeout" is a duration nobody can act on —
      an operator who does not know it is 900 seconds cannot choose between waiting and reclaiming,
      so they do neither and resubmit instead. The number comes from `settings`, the RESOLVED
      configuration, not from the class default, so a `--visibility-timeout` on this very command
      line is the number printed.
    - **The recovery command is named**, from `capture.cli.RECLAIM_NOW` — imported rather than
      retyped, because `stigmergy-queue` is the tool it invokes and because the value of its
      `--visibility-timeout` argument is subtle enough to have shipped wrong once (the
      configured lease releases nothing at second zero). Two tools printing the same recovery
      advice must not be able to disagree about it. With it, the caveat that makes it safe to
      follow: `--visibility-timeout 0` releases EVERY held claim, so a second worker mid-item
      would have its row pulled out from under it — and since the commit and the push happen
      before `finish`, both would file the capture. One clause is cheaper than that outcome.
    - **It does NOT claim nothing was committed.** An interrupt can land after the push and before
      the row is finished; a message that ruled that out would be telling an operator not to look
      at the one place they need to.
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
    # `choices` is the BACKENDS tuple itself, never a retyped list: a backend argparse rejects at
    # parse time gets "invalid choice", where `startup_checks` would have explained what the value
    # actually does — and every remaining refusal for these two (an unpriced model, a bare id, a
    # missing provider key) is a sentence that has to reach the operator.
    #
    # **A RETIRED value typed here still gets argparse's "invalid choice", not the retirement
    # message.** That is the same accepted trade, and it is the right way round: the value that
    # matters is the CONFIGURED one — `$STIGMERGY_LIBRARIAN_BACKEND` out of a stale `fly.toml` or
    # `.env`, which never passes through `choices` and reaches `startup_checks` with its own
    # sentence (`agent.RETIRED_BACKENDS`). Somebody TYPING `--backend sdk` is reading a list of two
    # in the same breath; a deployment carrying it is not reading anything.
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

    # SAME flag name as `stigmergy-queue`, because both sweep the same column — one flag name
    # meaning two things across subcommands has already caused one defect. The DEFAULT is not
    # the same any more: `stigmergy-queue`'s 300s was chosen for a human-scale claim, and an agent
    # item can run two 300s attempts plus the gates, so inheriting it let the queue redeliver an
    # item this worker was still processing. The librarian's default is computed from its own
    # per-item bounds (`config.minimum_visibility_timeout_s`) and `worker.startup_checks` refuses
    # anything below that.
    #
    # **`default=None`, not the class default** — and that is the fix for a defect this CLI shipped.
    # With the class default pre-filled here, `Settings.from_args`' `args.x or default` could not
    # tell an absent flag from an explicit `--visibility-timeout 0`, so the zero was silently
    # replaced by 900 and the interrupt message then quoted 900 back at an operator who had asked
    # for something else. `None` makes absence expressible, `from_args` resolves flag -> env ->
    # class default on an `is None` test, and an explicit `0` now reaches `worker.startup_checks`,
    # which refuses it out loud with the arithmetic. The default still appears in the help text,
    # which is where an operator actually looks for it.
    #
    # `status` gets the same flag because it REPORTS on the same lease: the staleness verdict is
    # meaningless unless it is computed against the timeout the worker is configured with.
    for parser in (p_once, p_run, p_status):
        parser.add_argument("--visibility-timeout", type=int, default=None,
                            help=f"seconds this worker's claim is held before the queue assumes it "
                                 f"died and returns the item — same column as `stigmergy-queue "
                                 f"reclaim --visibility-timeout`, but must exceed one item's worst "
                                 f"case (default {config.DEFAULT_VISIBILITY_TIMEOUT_S})")
    # `status` gets `--max-attempts` for the same reason it gets `--visibility-timeout`: it REPORTS
    # on the sweep's decision, and the sweep splits the expired set on this exact number. Comparing
    # against the class default while the worker runs with another one is how the verdict came to
    # promise a requeue for a row the sweep was about to fail.
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
