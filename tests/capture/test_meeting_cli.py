"""`stigmergy-meeting drop`, driven in-process through
`meeting_cli.main(argv)` against real Postgres and the local evidence store — the same posture
`test_cli.py` uses for `stigmergy-queue`. Every refusal asserts "no row and no object": the queue
count before and after must be equal, and (for the two size refusals, checked directly) nothing
was uploaded either.
"""
import os

import pytest

from stigmergy.capture import cli as capture_cli
from stigmergy.capture import evidence, meeting_cli, schema
from tests import testdb


def _dsn() -> str:
    return os.environ.get(testdb.DSN_ENV, testdb.DSN_DEFAULT)


@pytest.fixture(scope="module")
def conn():
    c = testdb.connect_or_skip("meeting_cli")
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
        store.put(b"stigmergy-meeting cli test: evidence reachability probe")
    except Exception:  # noqa: BLE001 — any failure means "not reachable", not a defect to report
        pytest.skip("the evidence store (minio) is not reachable — `make db-up` starts it")
    return store


@pytest.fixture()
def transcript(tmp_path):
    path = tmp_path / "transcript.txt"
    path.write_text("Alice: let's ship the Q3 pricing floor for Acme.\n"
                    "Bob: agreed, and the renewal terms apply company-wide.\n")
    return str(path)


# ── a valid drop enqueues exactly one meeting row, uploads exactly one object ───────────────────
def test_a_valid_drop_enqueues_exactly_one_meeting_row(clean_queue, transcript):
    _evidence_or_skip()
    before = _row_count(clean_queue)
    rc = meeting_cli.main(["--dsn", _dsn(), "drop", transcript, "--title", "Q3 sync",
                           "--date", "2026-07-29", "--attendees", "Alice, Bob",
                           "--submitted-by", "steward@example.com"])
    assert rc == 0
    assert _row_count(clean_queue) == before + 1
    with clean_queue.cursor() as cur:
        cur.execute("SELECT kind, hints, payload FROM capture_queue ORDER BY id DESC LIMIT 1")
        kind, hints, payload = cur.fetchone()
    assert kind == schema.MEETING
    assert hints["client"]["title"] == "Q3 sync"
    assert hints["client"]["meeting_date"] == "2026-07-29"
    assert hints["client"]["attendees"] == "Alice, Bob"
    assert hints["client"]["source_label"] == "granola-manual"
    assert payload["bytes"] > 0


# ── every named refusal leaves no row and no object ─────────────────────────────────────────────
def test_missing_title_is_refused_by_argparse_before_any_row(clean_queue, transcript, capsys):
    before = _row_count(clean_queue)
    with pytest.raises(SystemExit) as ex:
        meeting_cli.main(["--dsn", _dsn(), "drop", transcript, "--date", "2026-07-29"])
    assert ex.value.code == 2
    assert _row_count(clean_queue) == before
    assert "--title" in capsys.readouterr().err


def test_missing_date_is_refused_by_argparse_before_any_row(clean_queue, transcript, capsys):
    before = _row_count(clean_queue)
    with pytest.raises(SystemExit) as ex:
        meeting_cli.main(["--dsn", _dsn(), "drop", transcript, "--title", "Q3 sync"])
    assert ex.value.code == 2
    assert _row_count(clean_queue) == before
    assert "--date" in capsys.readouterr().err


def test_a_malformed_date_is_refused_naming_the_flag(clean_queue, transcript, capsys):
    before = _row_count(clean_queue)
    rc = meeting_cli.main(["--dsn", _dsn(), "drop", transcript, "--title", "Q3 sync",
                           "--date", "07-29-2026"])
    assert rc == 1
    assert _row_count(clean_queue) == before
    assert "--date" in capsys.readouterr().err


def test_an_empty_file_is_refused_naming_the_path(clean_queue, tmp_path, capsys):
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    before = _row_count(clean_queue)
    rc = meeting_cli.main(["--dsn", _dsn(), "drop", str(empty), "--title", "Q3 sync",
                           "--date", "2026-07-29"])
    assert rc == 1
    assert _row_count(clean_queue) == before
    err = capsys.readouterr().err
    assert str(empty) in err
    assert "empty" in err


def test_an_over_cap_file_is_refused_naming_the_numbers(clean_queue, tmp_path, capsys):
    big = tmp_path / "huge.txt"
    big.write_text("x" * (schema.MAX_MATERIAL_BYTES + 1))
    before = _row_count(clean_queue)
    rc = meeting_cli.main(["--dsn", _dsn(), "drop", str(big), "--title", "Q3 sync",
                           "--date", "2026-07-29"])
    assert rc == 1
    assert _row_count(clean_queue) == before
    err = capsys.readouterr().err
    assert f"{schema.MAX_MATERIAL_BYTES:,}" in err
    # must NOT reuse prepare_submission's "submit the part worth keeping" advice — the dropped
    # file IS the evidence here, so there is no part to leave behind.
    assert "submit the part worth keeping" not in err


def test_a_missing_submitted_by_is_refused_naming_the_flag(monkeypatch, clean_queue, transcript,
                                                            capsys):
    monkeypatch.delenv(meeting_cli.OPERATOR_EMAIL_ENV, raising=False)
    before = _row_count(clean_queue)
    rc = meeting_cli.main(["--dsn", _dsn(), "drop", transcript, "--title", "Q3 sync",
                           "--date", "2026-07-29"])
    assert rc == 1
    assert _row_count(clean_queue) == before
    assert "--submitted-by" in capsys.readouterr().err


# ── the same guard, on the door the walkthrough hit FIRST ──────────────────────────────────────
# The split-stores mistake bit the staging walkthrough twice, once per drop door — which is why
# the guard is on both rather than on the one the issue happened to be filed against.
def test_a_remote_queue_with_a_local_evidence_store_is_refused_before_the_transcript_is_read(
        clean_queue, transcript, capsys, monkeypatch):
    monkeypatch.setenv(evidence.ENDPOINT_ENV, "http://127.0.0.1:9000")
    before = _row_count(clean_queue)
    rc = meeting_cli.main(["--dsn", "postgresql://u:p@db.abcdef.supabase.co:5432/postgres",
                           "drop", str(transcript), "--title", "Q3 sync", "--date", "2026-07-29",
                           "--submitted-by", "steward@example.com"])
    err = capsys.readouterr().err
    assert rc == capture_cli.EXIT_SPLIT_STORES
    assert "db.abcdef.supabase.co" in err and "127.0.0.1" in err
    assert "--allow-split-stores" in err
    assert _row_count(clean_queue) == before


def test_the_success_line_names_the_store_it_uploaded_to(clean_queue, transcript, capsys):
    store = _evidence_or_skip()
    rc = meeting_cli.main(["--dsn", _dsn(), "drop", str(transcript), "--title", "Q3 sync",
                           "--date", "2026-07-29", "--submitted-by", "steward@example.com"])
    out = capsys.readouterr().out
    assert rc == 0
    assert store.bucket in out
    assert evidence.host_of(store.endpoint_url) in out
