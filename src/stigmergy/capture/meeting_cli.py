"""`stigmergy-meeting` — the drop CLI: the only door onto the meeting flow, and the webhook's
future target.

    stigmergy-meeting drop <file> --title <t> --date <YYYY-MM-DD> [--attendees a,b] \\
                                 [--submitted-by email]

An operator CLI in the `stigmergy-queue` mold: direct DB and bucket access, no MCP transport, run
by the operator from their own terminal right after a meeting. It does exactly one thing —
upload the transcript as evidence and enqueue **exactly one** `capture_queue` row with
`kind="meeting"` — and nothing else. Filing is the librarian's job (`librarian.processing`);
this command does not claim, does not file, and prints so honestly.

**Validate -> upload -> insert, in that order, for every refusal.** Every check below runs BEFORE
`queue.submit` (which itself uploads before it inserts — `queue.submit`'s own docstring), so "no
row and no object" is true by construction for every refusal this CLI names: a missing
`--title`/`--date`, an empty file, an over-cap file, and a malformed `--date`
(`schema.validate_meeting_date`).

**Attendees are hints, never identities**: they ride in `hints["attendees"]` exactly like
`SOURCE_HINT_KEYS`'s Slack provenance fields do, and the drop ack says so explicitly at the
moment the belief they "resolve" something would otherwise form.

**The oversize message deliberately does NOT reuse `capture.schema.prepare_submission`'s own
string.** That one says "submit the part worth keeping, not the whole transcript" — advice that
directly contradicts the meeting flow's premise: the source page carries the whole transcript
extraction, and every page in the set is anchored to THAT run's text. This CLI checks the size
itself, before `prepare_submission` ever runs, so its own honest refusal is the one an operator
sees.
"""
import argparse
import os
import sys

from stigmergy.capture import cli, evidence, queue, schema
from stigmergy.capture.errors import CaptureError, SubmissionRejected
from stigmergy.index import store

# The operator identity `--submitted-by` defaults to when not given. This surface carries
# single-operator traffic, so one env var is the whole of "configured" — there is no identity
# service to resolve against, unlike the MCP transport's token-derived identity
# (`server.service`).
OPERATOR_EMAIL_ENV = "STIGMERGY_MEETING_OPERATOR_EMAIL"

EXIT_INTERRUPTED = 130


def _connect(dsn: str | None):
    conn = store.connect(dsn)
    schema.ensure_capture_schema(conn)   # idempotent: this CLI may be the first thing to run
    return conn


def _refuse(message: str) -> "SubmissionRejected":
    """One `SubmissionRejected`, unprefixed — `main()`'s `except CaptureError` adds the
    `stigmergy-meeting: ` prefix exactly once, the same split `capture.cli`'s own refusals use."""
    return SubmissionRejected(message)


def _read_transcript(path: str) -> bytes:
    """The file's raw bytes, refusing BEFORE any upload — a clear local sentence naming the path
    beats a bare traceback, for the same reason every other refusal here names the path."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as ex:
        raise _refuse(f"cannot read {path} ({ex.__class__.__name__}) — nothing was uploaded and "
                      f"nothing was queued") from ex


def _check_size(path: str, data: bytes) -> str:
    """Empty and over-cap, both refused with the FILE's own name and size — LOCAL and specific,
    before `evidence.put` or `queue.submit` ever run. Returns the decoded text."""
    if len(data) == 0:
        raise _refuse(f"refusing to drop {path} — the file is empty (0 bytes). Nothing was "
                      f"uploaded and nothing was queued; check the export and re-run once it has "
                      f"content.")
    if len(data) > schema.MAX_MATERIAL_BYTES:
        cap_kb = schema.MAX_MATERIAL_BYTES // 1024
        raise _refuse(f"refusing to drop {path} — {len(data):,} bytes exceeds the queue's cap of "
                      f"{schema.MAX_MATERIAL_BYTES:,} bytes ({cap_kb} KB). Nothing was uploaded "
                      f"and nothing was queued.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as ex:
        raise _refuse(f"refusing to drop {path} — it is not valid UTF-8 text ({ex}); a Granola "
                      f"transcript export is text, and this file is not. Nothing was uploaded and "
                      f"nothing was queued.") from ex


def _resolve_submitted_by(args) -> str:
    submitted_by = args.submitted_by or os.environ.get(OPERATOR_EMAIL_ENV, "")
    if not submitted_by:
        raise _refuse(
            f"--submitted-by is required (or set ${OPERATOR_EMAIL_ENV}) — attribution comes from "
            f"a resolved identity on every other capture surface, and this operator CLI has none "
            f"to resolve. Nothing was uploaded and nothing was queued.")
    return submitted_by



def _cmd_drop(args) -> int:
    ev = evidence.store_from_env()
    refused = cli.refuse_split_stores(args, "stigmergy-meeting", ev)
    if refused:
        return refused

    # Refuses before any file is touched. `schema.validate_meeting_date` is the SEAM-level
    # validator — it also runs, unconditionally, inside `prepare_submission` for every caller of
    # `queue.submit`, so its own message is transport-neutral and does not name a flag. This CLI's
    # early copy exists to name the flag, so it puts the flag back on here rather than in the
    # shared seam.
    try:
        meeting_date = schema.validate_meeting_date(args.date)
    except SubmissionRejected as ex:
        raise _refuse(f"--date: {ex}") from ex
    data = _read_transcript(args.file)
    text = _check_size(args.file, data)
    submitted_by = _resolve_submitted_by(args)

    attendees = ", ".join(a.strip() for a in (args.attendees or "").split(",") if a.strip())
    hints = {
        "title": args.title,
        "meeting_date": meeting_date,
        "source_label": "granola-manual",
    }
    if attendees:
        hints["attendees"] = attendees

    conn = _connect(args.dsn)
    ack = queue.submit(conn, ev, kind=schema.MEETING, material=text, hints=hints,
                       submitted_by=submitted_by)

    # The store is NAMED: an operator reading "uploaded … as evidence <hash>" could not tell
    # whether the bytes went to the deployment's bucket or to their own laptop while the row went
    # to Fly. Operator-CLI posture — local and specific, the deliberate opposite of the wire's.
    print(f"uploaded {args.file} as evidence {ack['content_sha256'][:12]} "
          f"({ack['bytes']:,} bytes) to {ev.bucket} at {evidence.host_of(ev.endpoint_url)}")
    print(f"queued #{ack['id']} (meeting) — \"{args.title}\", {meeting_date}, attributed to "
          f"{submitted_by}")
    print(f"  attendees hint: {attendees or '(none given)'} — a hint for the agent, not an "
          f"identity: it resolves nothing and authorizes nothing")
    print(f"exiting WITHOUT filing #{ack['id']} — the librarian files it as a page set (one "
          f"source page, one meeting page, any decision pages) or parks it; nothing is in the "
          f"brain until it does. `stigmergy-queue show {ack['id']}` to check.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="stigmergy-meeting",
        description="Drop a meeting transcript onto the queue for the librarian's meeting flow. "
                    "This is a webhook simulation — a future webhook handler calls the same "
                    "enqueue seam with a different transport.")
    ap.add_argument("--dsn", default=None,
                    help=f"Postgres DSN (default: ${store.DSN_ENV} or {store.DSN_DEFAULT})")
    sub = ap.add_subparsers(dest="command", required=True)

    p_drop = sub.add_parser("drop", help="upload a transcript and enqueue exactly one meeting row")
    p_drop.add_argument("file", help="path to the transcript export (Granola, manual)")
    p_drop.add_argument("--title", required=True,
                        help="the meeting's title — becomes this capture's source and meeting "
                             "page identity")
    p_drop.add_argument("--date", required=True,
                        help="the meeting's date, YYYY-MM-DD — becomes `as_of` on every decision "
                             "page this meeting files")
    p_drop.add_argument("--attendees", default="",
                        help="comma-separated attendee names — a HINT for the agent, never an "
                             "identity: it resolves nothing and authorizes nothing")
    cli.add_split_stores_flag(p_drop)
    p_drop.add_argument("--submitted-by", default="",
                        help=f"defaults to ${OPERATOR_EMAIL_ENV}; who this drop is attributed to")
    p_drop.set_defaults(fn=_cmd_drop)
    return ap


def _interrupted(during: str) -> int:
    print(f"stigmergy-meeting: interrupted {during} — no queue row was written (the insert is a "
          f"single statement), but if the upload had already finished, an evidence object with "
          f"nothing pointing at it may exist; that costs nothing and nothing will ever read it "
          f"without a row. Re-run `stigmergy-meeting drop` when ready.", file=sys.stderr)
    return EXIT_INTERRUPTED


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except CaptureError as ex:
        print(f"stigmergy-meeting: {ex}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return _interrupted("while dropping the transcript")
    except Exception as ex:  # noqa: BLE001 — a local operator needs the real reason
        print(f"stigmergy-meeting: cannot reach the queue database or evidence store ({ex}); is "
              f"the stack up (`make db-up`)?", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
