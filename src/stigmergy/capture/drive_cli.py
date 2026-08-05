"""`stigmergy-drive` — the Drive drop CLI (ADR 028): the only door onto the drive flow.

    stigmergy-drive drop <file-id-or-url> [--submitted-by email]

An operator CLI in the `stigmergy-meeting` mold (direct DB and bucket access, no MCP transport,
run by the operator from their own terminal): it fetches ONE Drive file with the operator's own
Google auth (`drive_client.GogDriveClient`), uploads the ORIGINAL BYTES to evidence, and enqueues
exactly one `capture_queue` row with `kind="drive"` — and nothing else. **The door runs no model
and no conversion**: extraction is the worker's
(`librarian.processing.process_drive_item`), filing is the librarian's, and the ack says so.

**Validate → fetch → upload → insert, in that order, for every refusal** (the meeting CLI's own
discipline): the format policy, the size cap and the identity check all run BEFORE any byte is
uploaded, so "no row and no object" holds for every named refusal. The one asymmetry with the
meeting CLI is that the material must be FETCHED before its true size is known — Drive's own
metadata carries a size claim, so the cap is enforced twice: on the claim (before the download)
and on the actual bytes (after it, still before any upload).

**The format policy.** Everything the kernel can convert without a new container passes (pdf ·
txt/md/json · xlsx/xls/csv/tsv · docx); a NATIVE Google file is exported to PDF by Drive itself
at fetch time; an office binary (pptx/ppt/doc/odt/odp/ods/rtf — `converters.EXT_METHOD`'s
`office` method) is refused NAMING its wake condition, because the Gotenberg container it needs
lands when the first real pptx matters, not speculatively.

**The row's material is a deterministic MANIFEST, not the bytes** (ADR 028 D3): name, file id,
url, mime and the bytes' own sha256 — so dedup's existing levels key on content identity (the
same file re-dropped collapses; a file EDITED in Drive is honestly new), while `blob_refs[1]`
carries the original bytes for the worker. `drive_modified` rides in hints, deliberately OUT of
the manifest: Drive bumps it on metadata-only touches, and a re-drop of identical bytes must
dedup. (A native file's export is NOT byte-stable — PDF export embeds timestamps — so a native
re-drop may produce a fresh capture; the agent's own level-3 overlap judgment is the net there,
accepted in the ADR.)
"""
import argparse
import hashlib
import os
import sys
import tempfile

from stigmergy.capture import cli, drive_client, evidence, queue, schema
from stigmergy.capture.errors import CaptureError, SubmissionRejected
from stigmergy.index import store
from stigmergy.kernel.converters import method_for_ext

OPERATOR_EMAIL_ENV = "STIGMERGY_MEETING_OPERATOR_EMAIL"   # one operator identity, every drop CLI

# The door's own cap on a fetched file. Distinct from `schema.MAX_MATERIAL_BYTES` (the cap on
# TEXT material, which here bounds only the manifest): original bytes go to the evidence bucket,
# not a JSONB row, and the real bound is what the worker can convert and what vision OCR accepts
# (Gemini takes ≤~20 MB inline; `vision_extract` switches to the Files API above 14 MB). 25 MB
# covers every real deck by an order of magnitude while refusing the videos-and-archives class.
MAX_DRIVE_FILE_BYTES = 25 * 1024 * 1024

EXIT_INTERRUPTED = 130

# The wake-condition sentence, composed once. `EXT_METHOD`'s `office` method is the Gotenberg
# path, and this door ships the refusal rather than the container (ADR 028 D4).
_OFFICE_REFUSAL = (
    "office formats (pptx/ppt/doc/odt/odp/ods/rtf) wait on their wake condition — the first "
    "real deck that needs the Gotenberg container ships it. Today: export it to PDF in Drive "
    "and drop that, or drop a pdf/docx/sheet/text file directly. Nothing was uploaded and "
    "nothing was queued.")


def _connect(dsn: str | None):
    conn = store.connect(dsn)
    schema.ensure_capture_schema(conn)   # idempotent: this CLI may be the first thing to run
    return conn


def _refuse(message: str) -> "SubmissionRejected":
    return SubmissionRejected(message)


def _resolve_submitted_by(args) -> str:
    submitted_by = args.submitted_by or os.environ.get(OPERATOR_EMAIL_ENV, "")
    if not submitted_by:
        raise _refuse(
            f"--submitted-by is required (or set ${OPERATOR_EMAIL_ENV}) — attribution comes "
            f"from a resolved identity on every other capture surface, and this operator CLI "
            f"has none to resolve. Nothing was uploaded and nothing was queued.")
    return submitted_by


def _effective_name(meta: "drive_client.DriveFile") -> str:
    """The filename the WORKER will dispatch conversion on. A native file is exported to PDF at
    fetch time, so its effective name gains `.pdf`; a binary keeps Drive's display name."""
    if drive_client.is_native(meta.mime):
        return f"{meta.name}.pdf"
    return meta.name


def _check_format(meta: "drive_client.DriveFile") -> None:
    """The format policy, refused BEFORE the download — the earliest honest no."""
    if drive_client.is_native(meta.mime):
        return   # exported to PDF by Drive itself — always convertible
    ext = os.path.splitext(meta.name)[1]
    if not ext:
        raise _refuse(
            f"refusing {meta.name!r} — it has no extension, so the worker cannot know how to "
            f"convert it. Nothing was uploaded and nothing was queued.")
    method = method_for_ext(ext)
    if method == "office":
        raise _refuse(f"refusing {meta.name!r} — {_OFFICE_REFUSAL}")
    # `text` is `method_for_ext`'s FALLBACK for any unknown extension — an .mp4 would "convert"
    # as prose. Only the extensions that genuinely ARE text pass through it here.
    if method == "text" and ext.lower() not in (".txt", ".md", ".json"):
        raise _refuse(
            f"refusing {meta.name!r} — {ext} is not a format the worker converts (pdf, "
            f"xlsx/xls/csv/tsv, docx, txt/md/json, or any native Google file). Nothing was "
            f"uploaded and nothing was queued.")


def _check_size(meta: "drive_client.DriveFile", *, claimed: bool, n_bytes: int) -> None:
    stage = "Drive reports" if claimed else "the download is"
    if n_bytes > MAX_DRIVE_FILE_BYTES:
        cap_mb = MAX_DRIVE_FILE_BYTES // (1024 * 1024)
        raise _refuse(
            f"refusing {meta.name!r} — {stage} {n_bytes:,} bytes, over the door's cap of "
            f"{cap_mb} MB. Nothing was uploaded and nothing was queued.")
    if not claimed and n_bytes == 0:
        raise _refuse(f"refusing {meta.name!r} — the fetched file is empty (0 bytes). Nothing "
                      f"was uploaded and nothing was queued.")


def _manifest(meta: "drive_client.DriveFile", name: str, digest: str, n_bytes: int) -> str:
    """The row's text material: deterministic, content-identifying, volatile-field-free (the
    module docstring says why `drive_modified` stays out). What dedup keys on."""
    lines = [
        "Drive capture manifest",
        f"file: {name}",
        f"drive_file_id: {meta.file_id}",
        f"url: {meta.url}",
        f"mime: {meta.mime}",
        f"bytes_sha256: {digest}",
        f"bytes: {n_bytes}",
    ]
    if drive_client.is_native(meta.mime):
        lines.append("exported_as: pdf")
    return "\n".join(lines) + "\n"



def _cmd_drop(args) -> int:
    ev = evidence.store_from_env()
    refused = cli.refuse_split_stores(args, "stigmergy-drive", ev)
    if refused:
        return refused

    file_id = drive_client.file_id_from(args.ref)
    submitted_by = _resolve_submitted_by(args)
    client = drive_client.GogDriveClient(args.gog_bin or None)

    meta = client.metadata(file_id)
    _check_format(meta)
    if meta.size:
        _check_size(meta, claimed=True, n_bytes=meta.size)

    name = _effective_name(meta)
    with tempfile.TemporaryDirectory(prefix="stigmergy-drive-") as tmp:
        dest = os.path.join(tmp, "fetched" + os.path.splitext(name)[1].lower())
        client.fetch(meta, dest)
        with open(dest, "rb") as f:
            data = f.read()
    _check_size(meta, claimed=False, n_bytes=len(data))

    digest = hashlib.sha256(data).hexdigest()
    hints = {
        "title": os.path.splitext(name)[0],
        "drive_file_id": meta.file_id,
        "drive_name": name,
        "drive_url": meta.url,
        "drive_mime": meta.mime,
        "drive_modified": meta.modified,
    }

    conn = _connect(args.dsn)
    bytes_key = ev.put(data)
    ack = queue.submit(conn, ev, kind=schema.DRIVE,
                       material=_manifest(meta, name, digest, len(data)),
                       hints=hints, submitted_by=submitted_by,
                       extra_blob_refs=(bytes_key,))

    exported = " (exported to PDF by Drive)" if drive_client.is_native(meta.mime) else ""
    print(f"fetched {meta.name!r}{exported} — {len(data):,} bytes, sha256 {digest[:12]}")
    # The store is NAMED, not merely acknowledged. "uploaded the original bytes as evidence
    # bbf1d8f94915" was true and useless: an operator reading it could not tell that the bytes had
    # gone to their own laptop while the row went to Fly. Operator-CLI posture — local and
    # specific, the deliberate opposite of the wire's, which names no bucket or endpoint ever.
    print(f"uploaded the original bytes as evidence {bytes_key.rsplit('/', 1)[-1][:12]} to "
          f"{ev.bucket} at {evidence.host_of(ev.endpoint_url)}; the "
          f"binary itself stays in Drive ({meta.url})")
    print(f"queued #{ack['id']} (drive) — attributed to {submitted_by}")
    print(f"exiting WITHOUT converting or filing #{ack['id']} — the worker extracts the text "
          f"and the librarian files a synthesis page plus verbatim source parts, or parks it; "
          f"nothing is in the brain until it does. `stigmergy-queue show {ack['id']}` to check.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="stigmergy-drive",
        description="Drop one Drive file onto the queue for the librarian's drive flow. The "
                    "door fetches with YOUR local Google auth (gog), archives the original "
                    "bytes as evidence, and enqueues exactly one row — no model, no conversion.")
    ap.add_argument("--dsn", default=None,
                    help=f"Postgres DSN (default: ${store.DSN_ENV} or {store.DSN_DEFAULT})")
    sub = ap.add_subparsers(dest="command", required=True)

    p_drop = sub.add_parser("drop", help="fetch one Drive file and enqueue exactly one drive row")
    p_drop.add_argument("ref", help="the Drive file's share URL or bare file id")
    p_drop.add_argument("--submitted-by", default="",
                        help=f"defaults to ${OPERATOR_EMAIL_ENV}; who this drop is attributed to")
    cli.add_split_stores_flag(p_drop)
    p_drop.add_argument("--gog-bin", default="",
                        help=f"the gog binary (default: ${drive_client.GOG_BIN_ENV} or "
                             f"{drive_client.GOG_BIN_DEFAULT!r})")
    p_drop.set_defaults(fn=_cmd_drop)
    return ap


def _interrupted() -> int:
    print("stigmergy-drive: interrupted — no queue row was written (the insert is a single "
          "statement), but if the upload had already finished, an evidence object with nothing "
          "pointing at it may exist; that costs nothing and nothing will ever read it without a "
          "row. Re-run `stigmergy-drive drop` when ready.", file=sys.stderr)
    return EXIT_INTERRUPTED


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except CaptureError as ex:
        print(f"stigmergy-drive: {ex}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return _interrupted()
    except Exception as ex:  # noqa: BLE001 — a local operator needs the real reason
        print(f"stigmergy-drive: cannot reach the queue database or evidence store ({ex}); is "
              f"the stack up (`make db-up`)?", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
