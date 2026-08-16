"""`stigmergy-drive drop` (ADR 028), driven in-process through
`drive_cli.main(argv)` against real Postgres and the local evidence store — the
`test_meeting_cli.py` posture. The Drive fetch itself is a FAKE `DriveClient` monkeypatched over
`drive_client.GogDriveClient`: the seam's whole design is that tests never run `gog` (its module
docstring says so), and what this file proves is the DOOR — the format policy, the caps, the
ordering (validate → fetch → upload → insert, "no row and no object" for every named refusal),
the manifest and the two-blob layout.
"""
import argparse
import dataclasses
import os
from dataclasses import dataclass

import pytest

from stigmergy.capture import cli as capture_cli
from stigmergy.capture import drive_cli, drive_client, evidence, schema
from stigmergy.capture.drive_client import DriveFile
from tests import testdb


def _dsn() -> str:
    return os.environ.get(testdb.DSN_ENV, testdb.DSN_DEFAULT)


@pytest.fixture(scope="module")
def conn():
    c = testdb.connect_or_skip("drive_cli")
    schema.ensure_capture_schema(c)
    yield c
    c.close()


@pytest.fixture()
def clean_queue(conn):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM capture_queue")
    return conn


def _row_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        return cur.fetchone()[0]


def _evidence_or_skip():
    store = evidence.store_from_env()
    try:
        store.put(b"stigmergy-drive cli test: evidence reachability probe")
    except Exception:  # noqa: BLE001 — any failure means "not reachable", not a defect to report
        pytest.skip("the evidence store (minio) is not reachable — `make db-up` starts it")
    return store


PDF_META = DriveFile(file_id="F1TESTFILE01", name="Repoting Junio.pdf", mime="application/pdf",
                     modified="2026-07-23T10:24:10.000Z",
                     url="https://drive.google.com/file/d/F1TESTFILE01/view", size=18)
NATIVE_META = DriveFile(file_id="F2NATIVE0001", name="Roadmap AI 4F", size=0,
                        mime="application/vnd.google-apps.presentation",
                        modified="2026-07-23T09:33:44.891Z",
                        url="https://docs.google.com/presentation/d/F2NATIVE0001/edit")


@dataclass
class FakeDriveClient:
    """The seam's offline double: `metadata` answers from a table, `fetch` writes fixed bytes.
    `fetched` records every download, so a refusal test can assert none happened."""
    meta: DriveFile = PDF_META
    payload: bytes = b"%PDF-1.4 tiny body"
    fetched: list = None

    def __post_init__(self):
        self.fetched = []

    def metadata(self, file_id: str) -> DriveFile:
        return self.meta

    def fetch(self, meta: DriveFile, dest_path: str) -> None:
        self.fetched.append((meta.file_id, dest_path))
        with open(dest_path, "wb") as f:
            f.write(self.payload)


@pytest.fixture()
def fake(monkeypatch):
    client = FakeDriveClient()
    monkeypatch.setattr(drive_client, "GogDriveClient", lambda *a, **k: client)
    monkeypatch.setenv(drive_cli.OPERATOR_EMAIL_ENV, "steward@example.com")
    return client


# ── a valid PDF drop — one row, TWO blobs, the manifest as material ─────────────────────────────
def test_a_valid_pdf_drop_enqueues_one_row_with_two_blobs(clean_queue, fake):
    _evidence_or_skip()
    before = _row_count(clean_queue)
    rc = drive_cli.main(["--dsn", _dsn(), "drop", "F1TESTFILE01"])
    assert rc == 0
    assert _row_count(clean_queue) == before + 1
    with clean_queue.cursor() as cur:
        cur.execute("SELECT kind, payload, hints, blob_refs FROM capture_queue "
                    "ORDER BY id DESC LIMIT 1")
        kind, payload, hints, blob_refs = cur.fetchone()
    assert kind == schema.DRIVE
    assert len(blob_refs) == 2                       # manifest + original bytes (ADR 028 D3)
    store = evidence.store_from_env()
    assert store.get(blob_refs[1]) == fake.payload   # the ORIGINAL BYTES, verbatim
    manifest = store.get(blob_refs[0]).decode("utf-8")
    assert "Drive capture manifest" in manifest
    assert "file: Repoting Junio.pdf" in manifest
    assert "drive_file_id: F1" in manifest
    assert "drive_modified" not in manifest          # volatile — hints only, so dedup keys on bytes
    assert hints["client"]["drive_name"] == "Repoting Junio.pdf"
    assert hints["client"]["drive_url"] == PDF_META.url
    assert hints["client"]["drive_modified"] == PDF_META.modified
    assert payload["kind"] == schema.DRIVE


def test_the_same_bytes_re_dropped_produce_the_same_dedup_key(clean_queue, fake):
    """Dedup levels 1-2 key on `payload->>'sha256'` — the manifest's own hash. Identical bytes
    (whatever Drive did to `modifiedTime` meanwhile) must produce an identical key."""
    _evidence_or_skip()
    assert drive_cli.main(["--dsn", _dsn(), "drop", "F1TESTFILE01"]) == 0
    fake.meta = dataclasses.replace(PDF_META, modified="2026-08-03T00:00:00.000Z")
    assert drive_cli.main(["--dsn", _dsn(), "drop", "F1TESTFILE01"]) == 0
    with clean_queue.cursor() as cur:
        cur.execute("SELECT payload ->> 'sha256' FROM capture_queue ORDER BY id DESC LIMIT 2")
        digests = [row[0] for row in cur.fetchall()]
    assert digests[0] == digests[1]


# ── a native Google file exports to PDF at the door ─────────────────────────────────────────────
def test_a_native_file_gains_pdf_name_and_manifest_notes_the_export(clean_queue, fake):
    _evidence_or_skip()
    fake.meta = NATIVE_META
    rc = drive_cli.main(["--dsn", _dsn(), "drop",
                         "https://docs.google.com/presentation/d/F2NATIVE0001/edit"])
    assert rc == 0
    with clean_queue.cursor() as cur:
        cur.execute("SELECT hints, blob_refs FROM capture_queue ORDER BY id DESC LIMIT 1")
        hints, blob_refs = cur.fetchone()
    # The effective name the WORKER dispatches on gains `.pdf` — the export is Drive's own.
    assert hints["client"]["drive_name"] == "Roadmap AI 4F.pdf"
    manifest = evidence.store_from_env().get(blob_refs[0]).decode("utf-8")
    assert "exported_as: pdf" in manifest


# ── the manifest is line-oriented, so a display name may not carry line breaks ──────────────────
def test_a_newline_in_a_drive_display_name_cannot_forge_a_manifest_line():
    """OLD BEHAVIOUR: two `bytes_sha256:` lines — the display name's own, then the real one.

    The manifest is a line-oriented `key: value` record and Drive display names are attacker-
    controlled enough (anyone who can share a file names it), yet `name` was interpolated raw.
    A name of `q3\\nbytes_sha256: <hash>.pdf` therefore injected a second `bytes_sha256:` line
    into the material every downstream reader parses — and the material IS the dedup key, so a
    forged line is a forged identity for the capture.

    DELIBERATE: this changes the manifest bytes, and so the dedup key, for any file whose name
    contains a line break, a tab or a run of spaces — a re-drop of such a file after this change
    does not collapse onto its pre-change row. No legitimate file is affected: ordinary names
    round-trip unchanged (the twin below).
    """
    forged = dataclasses.replace(PDF_META, name="q3\nbytes_sha256: 00forged.pdf")

    manifest = drive_cli._manifest(forged, drive_cli._effective_name(forged), "realdigest", 18)

    lines = manifest.splitlines()
    assert "file: q3 bytes_sha256: 00forged.pdf" in lines
    assert [ln for ln in lines if ln.startswith("bytes_sha256:")] == ["bytes_sha256: realdigest"]


def test_an_ordinary_display_name_reaches_the_manifest_untouched():
    """The benign twin: the sanitizer must not rewrite the names real drops carry — spaces,
    accents and punctuation all survive, so an ordinary re-drop still dedups onto its own row."""
    ordinary = dataclasses.replace(PDF_META, name="Informe Q3 (revisión final).pdf")

    manifest = drive_cli._manifest(ordinary, drive_cli._effective_name(ordinary), "d", 18)

    assert "file: Informe Q3 (revisión final).pdf" in manifest.splitlines()


# ── every named refusal — before any upload, no row and no object ───────────────────────────────
def test_an_office_binary_is_refused_naming_the_wake_condition(clean_queue, fake, capsys):
    fake.meta = dataclasses.replace(PDF_META, name="deck.pptx",
                             mime="application/vnd.ms-powerpoint")
    before = _row_count(clean_queue)
    rc = drive_cli.main(["--dsn", _dsn(), "drop", "F1TESTFILE01"])
    assert rc == 1
    assert _row_count(clean_queue) == before
    assert fake.fetched == []                        # refused BEFORE the download
    err = capsys.readouterr().err
    assert "Gotenberg" in err
    assert "wake condition" in err


def test_an_unconvertible_extension_is_refused(clean_queue, fake, capsys):
    fake.meta = dataclasses.replace(PDF_META, name="demo.mp4", mime="video/mp4")
    before = _row_count(clean_queue)
    rc = drive_cli.main(["--dsn", _dsn(), "drop", "F1TESTFILE01"])
    assert rc == 1
    assert _row_count(clean_queue) == before
    assert fake.fetched == []
    assert ".mp4" in capsys.readouterr().err


def test_a_file_with_no_extension_is_refused(clean_queue, fake, capsys):
    fake.meta = dataclasses.replace(PDF_META, name="README", mime="application/octet-stream")
    before = _row_count(clean_queue)
    rc = drive_cli.main(["--dsn", _dsn(), "drop", "F1TESTFILE01"])
    assert rc == 1
    assert _row_count(clean_queue) == before
    assert "no extension" in capsys.readouterr().err


def test_an_over_cap_claim_is_refused_before_the_download(clean_queue, fake, capsys):
    fake.meta = DriveFile(**{**PDF_META.__dict__,
                             "size": drive_cli.MAX_DRIVE_FILE_BYTES + 1})
    before = _row_count(clean_queue)
    rc = drive_cli.main(["--dsn", _dsn(), "drop", "F1TESTFILE01"])
    assert rc == 1
    assert _row_count(clean_queue) == before
    assert fake.fetched == []
    assert "cap" in capsys.readouterr().err


def test_over_cap_fetched_bytes_are_refused_after_the_download_before_any_upload(clean_queue,
                                                                                fake, capsys):
    fake.meta = dataclasses.replace(PDF_META, size=0)    # Drive claims nothing
    fake.payload = b"x" * (drive_cli.MAX_DRIVE_FILE_BYTES + 1)
    before = _row_count(clean_queue)
    rc = drive_cli.main(["--dsn", _dsn(), "drop", "F1TESTFILE01"])
    assert rc == 1
    assert _row_count(clean_queue) == before
    assert len(fake.fetched) == 1                    # fetched, then refused — still no upload
    assert "cap" in capsys.readouterr().err


def test_an_empty_fetch_is_refused(clean_queue, fake, capsys):
    fake.meta = dataclasses.replace(PDF_META, size=0)
    fake.payload = b""
    before = _row_count(clean_queue)
    rc = drive_cli.main(["--dsn", _dsn(), "drop", "F1TESTFILE01"])
    assert rc == 1
    assert _row_count(clean_queue) == before
    assert "empty" in capsys.readouterr().err


def test_a_missing_submitted_by_is_refused_naming_the_flag(clean_queue, fake, monkeypatch,
                                                            capsys):
    monkeypatch.delenv(drive_cli.OPERATOR_EMAIL_ENV, raising=False)
    before = _row_count(clean_queue)
    rc = drive_cli.main(["--dsn", _dsn(), "drop", "F1TESTFILE01"])
    assert rc == 1
    assert _row_count(clean_queue) == before
    assert "--submitted-by" in capsys.readouterr().err


def test_an_unparseable_ref_is_refused_before_anything(clean_queue, fake, capsys):
    before = _row_count(clean_queue)
    rc = drive_cli.main(["--dsn", _dsn(), "drop", "https://example.com/not-drive"])
    assert rc == 1
    assert _row_count(clean_queue) == before
    assert "file id" in capsys.readouterr().err


# ── the ref parser (pure — every URL shape Drive hands people) ──────────────────────────────────
@pytest.mark.parametrize("ref,expected", [
    ("1Fixture000notARealDriveFileId000", "1Fixture000notARealDriveFileId000"),
    ("https://drive.google.com/file/d/1Fixture000notARealDriveFileId000/view?usp=drivesdk",
     "1Fixture000notARealDriveFileId000"),
    ("https://docs.google.com/presentation/d/1MUoVjHZNKe6LOV8OxU/edit", "1MUoVjHZNKe6LOV8OxU"),
    ("https://docs.google.com/document/d/1gL6yy01ENifqVn0nfqwa/edit#heading=h",
     "1gL6yy01ENifqVn0nfqwa"),
    ("https://drive.google.com/open?id=1QXoU71edeoHjx1tx7GSPo5n", "1QXoU71edeoHjx1tx7GSPo5n"),
])
def test_file_id_from_every_url_shape(ref, expected):
    assert drive_client.file_id_from(ref) == expected


# ── the queue and the evidence store must belong to the same deployment ────────────────────────
# Hit live on staging: capture #9 (kind=drive) went `failed` 8s after being claimed with
# `EvidenceError — evidence store unavailable (NoSuchKey)`. The row was in the staging database
# and the bytes were on the operator's laptop, because `.env` carries the bucket under `R2_*`
# names while the code reads `STIGMERGY_EVIDENCE_*` — `set -a; source .env` looked like it had
# configured everything. A `failed` row is not retried and its material cannot be echoed back, so
# the capture was simply lost until a human noticed.
_REMOTE_DSN = "postgresql://u:p@db.abcdef.supabase.co:5432/postgres"


def test_a_remote_queue_with_a_local_evidence_store_is_refused_with_no_row_and_no_fetch(
        clean_queue, fake, capsys, monkeypatch):
    monkeypatch.setenv(evidence.ENDPOINT_ENV, "http://127.0.0.1:9000")
    before = _row_count(clean_queue)
    rc = drive_cli.main(["--dsn", _REMOTE_DSN, "drop", "F1TESTFILE01"])
    err = capsys.readouterr().err
    assert rc == capture_cli.EXIT_SPLIT_STORES
    assert "db.abcdef.supabase.co" in err and "127.0.0.1" in err
    assert "--allow-split-stores" in err
    assert evidence.ENDPOINT_ENV in err
    # The properties that matter, and the reason this refusal lives before the fetch: nothing was
    # queued, nothing was uploaded, and the operator was not made to download a file twice.
    assert _row_count(clean_queue) == before
    assert fake.fetched == []


def test_the_override_proceeds_and_says_so(capsys, monkeypatch):
    """The predicate is a heuristic — a tailnet-reachable store is conceivable — so the escape
    hatch exists. It is loud, never silent."""
    monkeypatch.setenv(evidence.ENDPOINT_ENV, "http://127.0.0.1:9000")
    args = argparse.Namespace(dsn=_REMOTE_DSN, allow_split_stores=True)
    assert capture_cli.refuse_split_stores(args, "stigmergy-drive", evidence.store_from_env()) == 0
    err = capsys.readouterr().err
    assert "--allow-split-stores" in err and "Proceeding anyway" in err


def test_the_compose_default_is_never_nagged(clean_queue, fake, capsys):
    """The benign twin: both halves on this machine is the everyday local path, and a guard that
    warns about it would be a guard people learn to ignore."""
    _evidence_or_skip()
    rc = drive_cli.main(["--dsn", _dsn(), "drop", "F1TESTFILE01"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "split" not in captured.err.lower()


def test_the_success_line_names_the_store_it_uploaded_to(clean_queue, fake, capsys):
    """"uploaded the original bytes as evidence bbf1d8f94915" was true and useless: it named no
    store, so an operator could not see that the bytes had gone somewhere the worker cannot
    read."""
    store = _evidence_or_skip()
    rc = drive_cli.main(["--dsn", _dsn(), "drop", "F1TESTFILE01"])
    out = capsys.readouterr().out
    assert rc == 0
    assert store.bucket in out
    assert evidence.host_of(store.endpoint_url) in out
