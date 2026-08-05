"""Fail-closed startup through the real console entry point `main`, and the degraded keyless
start. The identity paths need no database — resolution runs before any DB work; the missing-key
path is DB-backed (a forged index_meta model) and skips without postgres.
"""

import pytest

from stigmergy.index import store
from stigmergy.server.errors import CapabilityUnavailableError, StartupError
from stigmergy.server.mcp_server import main
from stigmergy.server.service import UnavailableEmbedder, _resolve_embedder
from stigmergy.server.settings import Settings


# ── no path starts the server without resolved audiences ───────────────────────────────────────
def test_no_identity_exits_nonzero_with_message(fixture, monkeypatch, capsys):
    monkeypatch.delenv("STIGMERGY_IDENTITY", raising=False)
    rc = main(["--identities", fixture.identities_path])
    assert rc == 2
    assert "no identity given" in capsys.readouterr().err


def test_unknown_identity_exits_nonzero_with_message(fixture, monkeypatch, capsys):
    monkeypatch.delenv("STIGMERGY_IDENTITY", raising=False)
    rc = main(["--identity", "ghost", "--identities", fixture.identities_path])
    assert rc == 2
    assert "unknown identity 'ghost'" in capsys.readouterr().err


def test_malformed_identities_file_exits_nonzero_with_message(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("STIGMERGY_IDENTITY", raising=False)
    bad = tmp_path / "identities.json"
    bad.write_text("{not json", encoding="utf-8")
    rc = main(["--identity", "steward", "--identities", str(bad)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "stigmergy-server:" in err and "malformed" in err


# ── no OPENAI_API_KEY is a DEGRADED START, not a refusal to start ──────────────────────────────
# A real embedder configured with no OPENAI_API_KEY used to be a clean StartupError with exit 2.
# It degrades instead now: the property that behaviour protected — no traceback, an actionable
# message — still holds; what changed is WHERE it surfaces.
#
# Why: under the old behaviour an expired embedding key, a spent quota or an OpenAI outage took
# `brain_submit` down together with `search_brain`, and capture is the one thing that must
# survive. The old exit path is not preserved in parallel (no expand-contract) because the two
# behaviors are mutually exclusive at one code point — a process either starts or it does not —
# and the only consumers are this repo's own CLI and its tests.
def test_resolve_embedder_missing_key_degrades_instead_of_refusing_to_start(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    embedder = _resolve_embedder(Settings(embedder=None), "text-embedding-3-large")

    assert isinstance(embedder, UnavailableEmbedder)
    assert "OPENAI_API_KEY" in embedder.unavailable_reason
    # it names the missing CAPABILITY and says the write path is fine, so a reader does not
    # conclude the brain is down and stop capturing
    assert "cannot search" in embedder.unavailable_reason
    assert "brain_submit" in embedder.unavailable_reason
    # ...and using it anyway raises rather than returning a vector in the wrong space
    with pytest.raises(CapabilityUnavailableError):
        embedder.embed(["anything"])


def test_an_unknown_embedder_name_is_still_a_clean_startup_error(monkeypatch):
    """The half that does NOT degrade. A typo is not an outage: degrading a server whose operator
    believes they configured an embedder would hide the mistake behind a refusal message about a
    missing key that is not the problem."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    with pytest.raises(StartupError, match="unknown embedder"):
        _resolve_embedder(Settings(embedder="voyage"), "text-embedding-3-large")


def test_startup_without_openai_key_starts_and_serves_the_write_path(indexed, fixture, monkeypatch):
    """Full startup path: an index built with the real model and no key STARTS.

    Asserted through `build_service`, the same function `main` calls, rather than through `main`
    itself — `main` would go on to `mcp.run()` and block on stdio. The tool-level halves (which
    tools work and which refuse) are `tests/server/test_keyless_capability.py`.
    """
    from stigmergy.server.service import build_service

    conn, _ = indexed
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with conn.cursor() as cur:
        cur.execute("UPDATE index_meta SET model = 'text-embedding-3-large'")
    try:
        service = build_service(
            Settings(identity=fixture.STEWARD, identities_path=fixture.identities_path), conn=conn)
        assert isinstance(service.embedder, UnavailableEmbedder)
    finally:
        with conn.cursor() as cur:
            cur.execute("UPDATE index_meta SET model = 'fake-hashed-bow-256'")


# ── DB-side startup failures keep the no-traceback posture ─────────────────────────────────────
def test_postgres_unreachable_exits_cleanly_without_leaking_credentials(fixture, monkeypatch, capsys):
    """Postgres down at startup must not escape as a raw psycopg traceback: exit non-zero with an
    actionable message and the rebuild path. And the message must NOT print the DSN verbatim — a
    DSN commonly embeds a password and this line is persisted to log files. Needs no database
    (connection refused)."""
    monkeypatch.delenv("STIGMERGY_IDENTITY", raising=False)
    rc = main(["--identity", fixture.STEWARD, "--identities", fixture.identities_path,
               "--dsn", "postgresql://dbuser:sup3rs3cret@127.0.0.1:1/stigmergy?connect_timeout=1"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "stigmergy-server:" in err and "stigmergy-index --rebuild" in err
    assert "Traceback" not in err
    # credentials never reach stderr; the credential-free location still helps the operator
    assert "sup3rs3cret" not in err and "dbuser" not in err
    assert "127.0.0.1:1/stigmergy" in err


def test_index_meta_without_built_at_exits_with_rebuild_hint(indexed, fixture, capsys):
    """An `index_meta` from before the `built_at` column existed — a database nobody has rebuilt
    since — must not crash with a raw UndefinedColumn: `read_meta` tolerates the missing column
    and returns None, so the empty-index path surfaces the clean, actionable rebuild hint
    instead."""
    conn, _ = indexed
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE index_meta DROP COLUMN built_at")
    try:
        assert store.read_meta(conn) is None          # tolerant: a missing column is not a crash
        rc = main(["--identity", fixture.STEWARD, "--identities", fixture.identities_path,
                   "--embedder", "fake"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "stigmergy-index --rebuild" in err and "Traceback" not in err
    finally:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE index_meta ADD COLUMN built_at timestamptz"
                        " NOT NULL DEFAULT now()")
