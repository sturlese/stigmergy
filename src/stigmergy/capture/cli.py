"""`stigmergy-queue` — the steward's view of the write path, without a SQL client.

Eight subcommands, each a thin skin over the library (the same seams `stigmergy.server` and the
librarian call — nothing CLI-only):

    stigmergy-queue list                 what is waiting, what is stuck, what needs a decision
    stigmergy-queue show <id>            one submission's trace: created -> claimed -> finished
    stigmergy-queue claim [--hold N]     take one item off the queue (the claim primitive)
    stigmergy-queue reclaim --visibility-timeout N   return timed-out claims (N is mandatory)
    stigmergy-queue requeue <id> --by    a parked row goes back to the librarian
    stigmergy-queue resolve <id> --by    a parked row is closed as `resolved`: handled by hand
    stigmergy-queue reject  <id> --by    a parked row is closed as `rejected`, by a human
    stigmergy-queue purge                retention: delete payload/hints of old terminal rows

**The last three are the steward's drain**, and they are the instrument `triage` was missing:
without them, `triage` is a state the librarian can write and nothing can move a row out of. They
share one guarded transition, refuse a row a worker is holding, never touch `attempts`, and leave
the row claimable — see the section above `_cmd_requeue`, and `queue.dispose` for where the guard
lives.

`claim` deliberately does NOT process anything: draining the queue is the librarian's job, not
this CLI's. It takes an item, prints it, optionally holds the claim for `--hold` seconds and exits
WITHOUT finishing it, which is precisely how a dead worker is simulated: kill it (or let it exit)
and watch the item come back to the queue after the visibility timeout with `attempts`
incremented.

Errors here are LOCAL and may be specific — generic over HTTP, specific in a local CLI. An
operator staring at an unreachable database needs the host in the message, not a class name.

This module is the ONLY place in `stigmergy.capture` that opens a database connection or reads the
environment: the library takes `conn` as an argument, so nothing below it has an opinion about
where the queue lives. That is the rule `store.connect` is here for, and it is unchanged.

It also uses `stigmergy.text`'s `sanitize`/`clamp` — control-character stripping and word-safe
clipping for untrusted text headed to a terminal. That seam is not this module's alone:
`dispositions.clean` takes the same edge, because a steward's `--reason` reaches a SUBMITTER and
`stigmergy-entities` calls `dispositions.reject` without passing through any of this file. A text
seam only one CLI crosses is a seam the next CLI skips, so it lives below both. Every edge is
downward; `capture` never reaches sideways into `server` or up into `answer`.
"""
import argparse
import json
import sys
import time

from stigmergy import text as textutil
from stigmergy.capture import dispositions, evidence, queue, retention, schema
from stigmergy.capture.errors import CaptureError
from stigmergy.index import store

_DUMP = {"ensure_ascii": False, "indent": 2}

# `list`'s `kind` column width, COMPUTED from `schema.KINDS` rather than hand-picked: the field
# used to be a bare `:<5`, sized for `raw`/`page` (both ≤4 chars) and never revisited when
# `meeting` (7 chars) joined the vocabulary — every meeting row broke the column's alignment on a
# tool nobody thought to look at. Deriving the width from the vocabulary is what keeps the NEXT
# kind from repeating the regression.
_KIND_WIDTH = max(len(k) for k in schema.KINDS)

# Ctrl-C exits 130 — 128 + SIGINT(2), the shell's own convention, and already what CPython returns
# for an UNCAUGHT KeyboardInterrupt: catching it here removes the traceback without changing the
# status any wrapper script already observes. 0 was rejected deliberately: an interrupted
# `claim --hold` leaves a real orphaned lease behind, so telling automation "success, nothing left
# to do" would be a lie — the reclaim (or the visibility timeout) still has to happen.
EXIT_INTERRUPTED = 130

# How an operator gets a stranded claim back RIGHT NOW, written in exactly one place because its
# ARGUMENT is a defect this repo has already shipped: `--visibility-timeout 300` (this run's
# configured lease) releases nothing at second zero, so the advice contradicted the word
# "immediately" and taught an operator the recovery path was broken. `_report_orphaned_lease`
# explains the two numbers; `stigmergy-librarian`'s own interrupt message imports THIS constant
# rather than retyping the command, so the same mistake cannot be made a second time in a second
# tool. The command belongs here because this module IS `stigmergy-queue`.
RECLAIM_NOW = "stigmergy-queue reclaim --visibility-timeout 0"

# ── the drop doors' shared configuration guard ────────────────────────────────────────────────
# Lives here, below BOTH drop CLIs, for the reason this package already paid for once: a steward's
# `--reason` cleaning was written into one CLI and not the other, and the fix was to move it below
# both where no future caller can skip it. The next door (a webhook is named as future work in
# `meeting_cli`'s own docstring) inherits this instead of having to remember it.
#
# Distinct from `main`'s catch-all 2 ("cannot reach the queue database or evidence store"): a
# wrapper has to be able to tell "refused by policy, nothing happened" from "infrastructure is
# down", and a shared exit code that means both is a code that means neither.
EXIT_SPLIT_STORES = 3


def add_split_stores_flag(parser) -> None:
    """The escape hatch, spelled once. The predicate is a heuristic — a tailnet-reachable store is
    conceivable — so an override exists; it is loud, never silent."""
    parser.add_argument("--allow-split-stores", action="store_true",
                        help="proceed even when the queue is remote and the evidence store is on "
                             "this machine — the combination that files a row whose bytes the "
                             "worker can never read")


def refuse_split_stores(args, prog: str, ev) -> int:
    """`0` to proceed, `EXIT_SPLIT_STORES` when the queue and the evidence plane provably belong
    to different deployments and the operator has not said they mean it.

    Called FIRST in a drop, before anything is read, fetched, uploaded or inserted — the door's own
    "no row and no object" discipline, and refusing before a fetch also spares the operator a
    download they would only have to repeat. Building the store does no I/O, so asking it where it
    points is free."""
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


def _connect(args):
    conn = store.connect(args.dsn)
    schema.ensure_capture_schema(conn)   # idempotent: the CLI may be the first thing to run
    return conn


# ── the two shared renderings `stigmergy-librarian` reuses ──────────────────────────────────────
# Public, and here rather than in the librarian, for the same reason `RECLAIM_NOW` is here: this
# module IS `stigmergy-queue`, and these are its vocabulary. The two tools sit side by side in one
# operator's terminal, so `stigmergy-librarian status` prints the byte-identical depth line and the
# byte-identical duration format `stigmergy-queue list`/`show` print. A second dialect for the same
# two facts is how an operator learns to distrust both.
def depth_line(counts: dict[str, int]) -> str:
    """`queue: queued=3 · claimed=1` — non-zero statuses only, or `queue: empty`.

    Zeroes are dropped on purpose: `counts_by_status` returns every declared status so no caller has
    to guess whether a missing key means zero, and printing all eight of them would bury the one or
    two that are not.
    """
    depth = " · ".join(f"{status}={n}" for status, n in counts.items() if n)
    return f"queue: {depth or 'empty'}"


def format_ms(value) -> str:
    """A measured duration in milliseconds, as one number a person reads: `4.2s`, or `—` when there
    is nothing to report.

    Deliberately NOT `worker.human_duration`, which renders `900s (15 min)`. That one describes a
    CONFIGURED value that has to match a flag and a column, so it prints the raw seconds too; this
    one describes something that was measured, where the machine value has no second life.
    """
    return "—" if value is None else f"{value / 1000:.1f}s"


def _clean(text: str, width: int = 0) -> str:
    """Untrusted captured text on its way to a terminal: control characters stripped (a capture
    can contain ANSI escapes), newlines flattened, optionally clipped.

    The clip is `stigmergy.text.clamp` — word-safe — and that is a fix rather than a tidy-up. It was
    a hard byte slice, which was merely ugly on an excerpt and became a real defect once `error`
    started carrying the ask-back question: a hard cut through
    `brain_reply(submission_id=14, answer=…)` produces a string that is not a valid call, printed
    under a line telling the reader to run it. The clip is now word-safe everywhere AND the two
    renderers below never clip a question at all (see `_note_line`); belt and braces, because a
    message containing a command is an executable promise.
    """
    return textutil.clamp(textutil.sanitize(text or "").replace("\n", " ⏎ "), width)


# A STEWARD's own words, on their way into a submitter's report — the one string in this system not
# built from a fixed vocabulary. The cleaning used to live HERE, and that was the defect: a seam a
# CLI has to remember to call is one `stigmergy-entities reject --reason` can skip, and did. It now
# lives in `dispositions.clean`, below every CLI, where the three functions this module calls run it
# whether or not anybody remembered. Kept as a local name because the call sites read better with it
# and because a reader following `--note` from the parser lands here first.
_note = dispositions.clean


def format_age(ms) -> str:
    """How long something has been waiting, as a person says it: `12 min`, `3h`, `1d 2h`.

    The third shared rendering, beside `depth_line` and `format_ms`, and separate from both on
    purpose. `format_ms` renders a MEASURED latency (`4.2s`) where sub-second precision is the whole
    point; this renders an AGE, where it is noise — a steward scanning parked rows is choosing
    between "this morning" and "last week", and `93847.2s` makes them do arithmetic to find out
    which. `worker.human_duration` is the third and renders a CONFIGURED value (`900s (15 min)`),
    which has to keep the machine number because it must match a flag.
    """
    if ms is None:
        return "—"
    minutes = int(max(0.0, float(ms)) // 60000)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if not minutes else f"{hours}h {minutes} min"
    days, hours = divmod(hours, 24)
    return f"{days}d" if not hours else f"{days}d {hours}h"


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
        # Who is being waited on, and for how long — the two facts a steward triages a list on, and
        # the reason `needs_input` and `triage` must not look alike here. Without them, a steward
        # scanning for what needs THEIR attention has to open `show` on every parked row just to
        # learn it is not theirs to act on yet.
        parked = (f" waiting on: {row['waiting_on']} · parked {format_age(row['parked_age_ms'])}"
                  if row["waiting_on"] else "")
        print(f"#{row['id']} {row['status']:<11} {row['kind']:<{_KIND_WIDTH}} {row['submitted_by']}"
              f" attempts={row['attempts']} {row['created_at']}{flags}{parked}")
        # Three ways a row has nothing to show, and each says which one it is. The withheld case
        # is NOT `_clean`ed or clipped: it is the queue's own sentence (`schema`), not captured
        # text, and it is the whole answer to "why is this line empty".
        if row["payload_purged"]:
            body = "(payload purged)"
        elif row["withheld_reason"]:
            body = f"({row['withheld_reason']})"
        else:
            body = _clean(row["excerpt"], 100)
        print(f"    {body}")
        _print_note(row, one_line=True)
    return 0


def _print_note(row: dict, *, one_line: bool) -> None:
    """The row's `error`/`question` line — and the one place a `needs_input` row is NOT clipped.

    A parked question is a multi-line, code-built message that ENDS in the exact command the
    submitter has to run. Both renderers here were written when this column held a one-line
    refusal, and both clipped it (200 characters in `list`, 300 in `show`) — which on this content
    truncates the promised `brain_reply(...)` mid-call. So:

    - `show` prints the question WHOLE, indented, newlines intact. It is what a steward reads
      before deciding whether to reply on somebody's behalf, and there is nothing in it that is not
      worth reading.
    - `list` prints no question at all — a fifteen-line message per row would make the list
      unreadable — and instead prints the ONE thing a scanning steward needs, the invocation,
      structurally rather than as a slice of prose. It comes from `schema.reply_invocation`, the
      same function that built the sentence, so it cannot drift from it and cannot be cut.

    Every other status keeps the old clipped one-liner: those really are one-line refusals, and the
    clip is now word-safe (`_clean`).
    """
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
        print(f"    ! {_clean(row['error'], 200)}" if one_line else
              f"  note        {_clean(row['error'], 300)}")


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
    print(f"  claimed_at  {trace['claimed_at'] or '—'}  (queue wait: {_ms(trace['queue_wait_ms'])})")
    print(f"  finished_at {trace['finished_at'] or '—'}  (total: {_ms(trace['total_latency_ms'])})")
    print(f"  attempts    {trace['attempts']}")
    print(f"  blob_refs   {', '.join(trace['blob_refs']) or '(none)'}")
    if trace["result_ref"]:
        print(f"  result_ref  {trace['result_ref']}")
    if trace["waiting_on"]:
        print(f"  parked      {format_age(trace['parked_age_ms'])} ago — waiting on: "
              f"{trace['waiting_on']}")
    _print_note(trace, one_line=False)
    if trace["reply"]:
        print(f"  reply       {_clean(trace['reply'], 500)}")
    elif trace["withheld_reason"]:
        # The query suppressed the reply, so SAY so. A capture can be asked, answered, and only then
        # refused for a secret the gates found in the drafted page — the answer is the submitter's
        # own free text, scanned by nothing, and it is withheld with the rest of the material. An
        # empty line here with no explanation reads as "they never answered", which is a different
        # and false story. The sentence is the queue's own (`schema`), so it is neither cleaned nor
        # clipped — the same treatment `list` gives it.
        print(f"  reply       ({trace['withheld_reason']})")
    for event in trace["events"]:
        # The row's own history: who did what to it, and what they said about it. Sanitized but not
        # clipped for the same reason `show`'s question is not — this is the surface a steward reads
        # before disposing of a row, and a note cut in half is a note that misinforms them.
        #
        # The `asked` event's note IS the question, so it is suppressed while the row is still
        # `needs_input` and the question block above has just printed it in full. Once the row has
        # moved on the block is gone and this is the only surviving copy, which is exactly why it
        # is recorded — but printing it twice on the one screen where both exist reads as a bug.
        kind = str(event.get("event", ""))
        print(f"  · {_clean(event.get('at', ''), 40)}  {_clean(kind, 20)}"
              f"  by {_clean(event.get('actor', ''), 80) or '—'}")
        if kind == schema.EVENT_ASKED and trace["status"] == schema.NEEDS_INPUT:
            print("      (the question, printed above)")
            continue
        for line in textutil.sanitize(str(event.get("note") or "")).splitlines():
            print(f"      {line}")
    if trace["payload_purged"]:
        print("  payload     (purged by retention; the evidence blob is unaffected)")
    return 0


_ms = format_ms   # the local name `show` already reads with; one implementation, above


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
        print(f"    {_clean((item['payload'] or {}).get('text', ''), 200)}", flush=True)
    if args.hold:
        print(f"holding the claim for {args.hold}s — kill this process to simulate a dead worker; "
              f"the item returns to the queue {args.visibility_timeout}s after it was claimed",
              flush=True)
        try:
            time.sleep(args.hold)
        except KeyboardInterrupt:
            # The ONE interruption this tool explicitly invites, one line above. Answering an
            # invited action with a stack trace teaches an operator that something broke at the
            # exact moment everything worked as designed — so it is caught here, where we still
            # know WHICH submission is now holding an orphaned lease. That fact is the whole
            # point of the message and cannot be reconstructed from `main`'s generic handler.
            return _report_orphaned_lease(item, args)
    print(f"exiting WITHOUT finishing #{item['id']} — this command drains nothing; the librarian "
          "is what will file it", flush=True)
    return 0


def _report_orphaned_lease(item: dict, args) -> int:
    """What the operator needs after interrupting a held claim: which row is stranded, that this
    is precisely what a dead worker leaves behind, and the two ways it comes back.

    `--json` gets a JSON object, never prose — same convention `claim` already follows, where a
    JSON value is emitted first and any advisory text after it (a consumer reads the leading value
    with `json.JSONDecoder().raw_decode`).

    The recovery command names `--visibility-timeout 0`, NOT this run's configured timeout. They
    are different numbers meaning different things: on `claim` it is how long the lease lasts, on
    `reclaim` it is how old a claim must be to be released. Echoing the configured value here (the
    first cut did) produces advice that contradicts the word "immediately" — at the default,
    `reclaim --visibility-timeout 300` releases claims older than 300s, so run at second zero it
    does nothing and the operator concludes the recovery path is broken."""
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
    # No default, deliberately. `reclaim` decides how dead a worker must be before its work is
    # taken away, and this CLI cannot see the worker's lease — a wrong guess here requeues a
    # capture out from under a process that is still filing it. So the operator states the
    # horizon, and the refusal names the two values that are almost always the right ones.
    if args.visibility_timeout is None:
        print("stigmergy-queue: reclaim needs --visibility-timeout — how old a claim must be "
              "before its worker is presumed dead. There is no safe default: this command "
              "cannot see the worker's configured lease, and a horizon shorter than that lease "
              "requeues captures out from under running workers.\n"
              "  after killing a worker, to force redelivery now:\n"
              "    stigmergy-queue reclaim --visibility-timeout 0\n"
              "  to sweep genuinely abandoned claims, pass the worker's own lease — read it "
              "with `stigmergy-librarian status --json` (.visibility_timeout_s; 900 by default, "
              "derived from $STIGMERGY_LIBRARIAN_TIMEOUT_S):\n"
              "    stigmergy-queue reclaim --visibility-timeout 900", file=sys.stderr)
        return 2
    result = queue.release_expired(conn, visibility_timeout_s=args.visibility_timeout,
                                   max_attempts=args.max_attempts)
    print(json.dumps(result, **_DUMP) if args.json else
          f"released {result['released']} expired claim(s); failed {result['failed']} "
          f"item(s) that exhausted {args.max_attempts} attempts")
    return 0


# ── the steward's drain: requeue / resolve / reject ────────────────────────────────────────────
# Three commands, one guarded transition underneath (`queue.dispose`), and one rule they share:
# the state check is the DATABASE's, so a disposition typed a second after a worker claimed the row
# fails loudly instead of silently doing nothing. Every refusal an operator can hit here comes back
# through `main`'s `except CaptureError` as one `stigmergy-queue: …` line naming which of the three
# refusals it was — nonexistent, claimed, or not parked.
#
# `--by` is required on all three and is ATTRIBUTION, not authorization: this tool does not know
# who is running it and does not pretend to. Recording who said they did it is what makes
# a drained queue auditable; checking it would be security theatre on a local CLI whose operator
# already has the DSN.
def _cmd_requeue(conn, args) -> int:
    result = dispositions.requeue(conn, args.id, actor=args.by, note=_note(args.note))
    if args.json:
        print(json.dumps(result, **_DUMP))
        return 0
    print(f"requeued #{result['id']} — back in the queue for the librarian to try again "
          f"(attempts unchanged at {result['attempts']}; it is claimable now)")
    return 0


def _cmd_resolve(conn, args) -> int:
    """Close a parked row as `resolved` — a steward handled it outside the fast lane.

    **The missing-pointer warning is a real finding, not decoration.** `resolve` with neither
    `--page` nor `--commit` leaves the submitter's report permanently silent about where their
    material went — on the one state whose entire point (unlike `rejected`) is that the material WAS
    used. It is warned about rather than prompted for: `stigmergy-queue` is documented in a runbook
    and run non-interactively, and a blocking prompt in a scriptable tool is how automation hangs
    forever on a question nobody is there to answer. So the command still does what it was asked,
    and says what that costs — in the JSON payload for a scripted run, on stderr for a human one.
    """
    note = _note(args.note)
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
    result = dispositions.reject(conn, args.id, actor=args.by, reason=_note(args.reason))
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
    print(f"{verb} payload+hints of {result['purged']} terminal submission(s) older than "
          f"{args.older_than_days} days"
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

    # SAME flag name, two genuinely different meanings — which is what made the interrupted-claim
    # message wrong before: on `claim` the value is how long THIS lease lasts, on `reclaim` it is
    # how old a claim must be to be released. The wording is per-command, because a single
    # sentence covering both is exactly the ambiguity an operator then acts on.
    #
    # The DEFAULTS differ for the same reason. `claim` states the lease it is taking, so it knows
    # the number and can default. `reclaim` states when someone else's work may be seized, and
    # this CLI cannot see that worker's lease — so it has no default and `_cmd_reclaim` refuses
    # with the two values that are almost always right.
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

    # ── the drain ────────────────────────────────────────────────────────────────────────────
    # `--by` on all three, and the SAME help text on all three, because it means the same thing on
    # all three (see the section comment above `_cmd_requeue`).
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
    """The generic Ctrl-C net: one honest line, no traceback, on STDERR.

    Stderr rather than stdout on purpose — it is a diagnostic about the run, not the command's
    output, so a `--json` invocation keeps a clean, parseable stdout without this handler needing
    to know anything about output modes. `_report_orphaned_lease` is the deliberate exception: the
    interruption it handles is INVITED, its message is the useful result of the run, and it is
    mode-aware for exactly that reason."""
    print(f"stigmergy-queue: interrupted {during} — nothing was left half-written (every queue "
          f"transition is a single statement); re-run when ready", file=sys.stderr)
    return EXIT_INTERRUPTED


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        conn = _connect(args)
    except KeyboardInterrupt:
        return _interrupted("while connecting to the queue database")
    except Exception as ex:  # noqa: BLE001 — a local operator needs the real reason, not a class
        print(f"stigmergy-queue: cannot reach the queue database ({ex}); is Postgres up "
              f"(`make db-up`)?", file=sys.stderr)
        return 2
    try:
        return args.fn(conn, args)
    except CaptureError as ex:
        print(f"stigmergy-queue: {ex}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # The net for every command `_cmd_claim` does not already handle specifically: a `purge`
        # over a large backlog, a `reclaim` waiting on a row lock, a `list` against a slow
        # database. None of them INVITE a Ctrl-C, but all of them can receive one.
        return _interrupted(f"during `{args.command}`")
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
