"""`stigmergy-meeting` — the drop CLI: the only door onto the meeting flow, and the webhook's
future target.

    stigmergy-meeting drop <file> --title <t> --date <YYYY-MM-DD> [--attendees a,b] \\
                                 [--submitted-by email]

An operator CLI in the `stigmergy-queue` mold: direct DB and bucket access, no MCP transport.
It uploads the transcript as evidence and enqueues exactly one `kind="meeting"` row — filing is
the librarian's, and the ack says so. Validate -> upload -> insert, so "no row and no object"
holds for every refusal this CLI names. Attendees are hints, never identities.

The oversize refusal deliberately does NOT reuse `prepare_submission`'s string: "submit the part
worth keeping" contradicts the meeting flow's whole-transcript premise, so this CLI checks the
size first and its own refusal is the one an operator sees.
"""
import argparse
import sys

from stigmergy.capture import cli, evidence, queue, schema
from stigmergy.capture.errors import SubmissionRejected
from stigmergy.index import store

# Referenced through this module by name (`meeting_cli.OPERATOR_EMAIL_ENV`); the value is the
# drop doors' shared one, so there is nothing here for a second spelling to drift from.
OPERATOR_EMAIL_ENV = cli.OPERATOR_EMAIL_ENV

PROG = "stigmergy-meeting"


def _refuse(message: str) -> "SubmissionRejected":
    """Unprefixed — `main()`'s `except CaptureError` adds the prefix exactly once."""
    return SubmissionRejected(message)


def _read_transcript(path: str) -> bytes:
    """The file's raw bytes, refused with a local sentence naming the path before any upload."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as ex:
        raise _refuse(f"cannot read {path} ({ex.__class__.__name__}) — nothing was uploaded and "
                      f"nothing was queued") from ex


def _check_size(path: str, data: bytes) -> str:
    """Empty and over-cap refused with the file's own name and size, before any upload or
    insert. Returns the decoded text."""
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


def _cmd_drop(args) -> int:
    ev = evidence.store_from_env()
    refused = cli.refuse_split_stores(args, PROG, ev)
    if refused:
        return refused

    # Refuses before any file is touched. The seam validator also runs inside
    # `prepare_submission` for every caller; this early copy exists to name the flag.
    try:
        meeting_date = schema.validate_meeting_date(args.date)
    except SubmissionRejected as ex:
        raise _refuse(f"--date: {ex}") from ex
    data = _read_transcript(args.file)
    text = _check_size(args.file, data)
    submitted_by = cli.resolve_submitted_by(args)

    attendees = ", ".join(a.strip() for a in (args.attendees or "").split(",") if a.strip())
    hints = {
        "title": args.title,
        "meeting_date": meeting_date,
        "source_label": "granola-manual",
    }
    if attendees:
        hints["attendees"] = attendees

    conn = cli.connect(args.dsn)
    ack = queue.submit(conn, ev, kind=schema.MEETING, material=text, hints=hints,
                       submitted_by=submitted_by)

    # The store is NAMED, so the operator can tell the bytes did not go to their own laptop
    # while the row went to the deployment. Operator-CLI posture: local and specific.
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
        prog=PROG,
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
    cli.add_submitted_by_flag(p_drop)
    p_drop.set_defaults(fn=_cmd_drop)
    return ap


def main(argv=None) -> int:
    return cli.drop_main(argv, parser=build_parser(), prog=PROG,
                         on_interrupt=lambda: cli.drop_interrupted(
                             PROG, "while dropping the transcript"))


if __name__ == "__main__":
    sys.exit(main())
