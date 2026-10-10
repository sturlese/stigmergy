"""`stigmergy-admin`: bootstrap, break-glass mint, and the one-time import of a file roster."""
import json

import pytest

from stigmergy.admin import cli
from stigmergy.index import store
from stigmergy.server import roster
from stigmergy.server.identity import hash_token, resolve_principal
from tests import testdb
from tests.roster import reset_roster, seed_roster

IDENTITIES = {
    "_comment": "the legacy file roster",
    "owner@example.com": {"display_name": "Owner", "groups": ["brain-admins"],
                          "default_audience": None},
    "analyst@example.com": {"display_name": "Analyst", "groups": ["finance", "product"],
                            "default_audience": ["finance"]},
    "bot@example.com": {"display_name": "Bot", "groups": ["product"],
                        "default_audience": ["product"]},
}
CHANNELS = {"C0FINANCE1": ["finance"], "C0GENERAL1": None}


@pytest.fixture()
def conn(monkeypatch):
    conn = testdb.connect_or_skip("admin-cli")
    monkeypatch.setenv(store.DSN_ENV, testdb.dsn())
    reset_roster(conn)
    yield conn
    conn.close()


def _token(capsys) -> str:
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("stg_")]
    assert len(lines) == 1
    return lines[0]


def test_bootstrap_creates_the_first_administrator_once(conn, capsys):
    assert cli.main(["bootstrap", "owner@example.com", "--name", "Owner"]) == 0
    token = _token(capsys)

    who = roster.open_session(conn, token)[1].principal
    assert who.subject == "owner@example.com" and who.administers and who.audiences is None
    assert cli.main(["bootstrap", "second@example.com", "--name", "Second"]) == 2
    assert "already exists" in capsys.readouterr().err


def test_mint_issues_tokens_only_to_an_enabled_administrator(conn, capsys):
    seed_roster(conn, {k: v for k, v in IDENTITIES.items() if not k.startswith("_")},
                admins={"owner@example.com"})
    capsys.readouterr()

    assert cli.main(["mint", "analyst@example.com"]) == 2
    assert cli.main(["mint", "owner@example.com"]) == 0
    assert roster.open_session(conn, _token(capsys))[1].principal.subject == "owner@example.com"


def test_import_preserves_audiences_tokens_and_channels(conn, tmp_path, monkeypatch, capsys):
    identities = tmp_path / "identities.json"
    identities.write_text(json.dumps(IDENTITIES), encoding="utf-8")
    channels = tmp_path / "channels.json"
    channels.write_text(json.dumps(CHANNELS), encoding="utf-8")
    legacy = {"owner@example.com": "legacy-owner-token", "bot@example.com": "legacy-bot-token"}
    monkeypatch.setenv("STIGMERGY_TOKEN_STORE",
                       json.dumps({hash_token(t): s for s, t in legacy.items()}))
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS ops_file_snapshot (relpath text PRIMARY KEY)")

    assert cli.main(["import", "--identities", str(identities), "--channels", str(channels),
                     "--admin", "owner@example.com", "--agent", "bot@example.com"]) == 0

    for subject in (k for k in IDENTITIES if not k.startswith("_")):
        from_file = resolve_principal(str(identities), subject)
        from_db = roster.principal(conn, subject)
        # A legacy roster's organization-wide default becomes the public default layer.
        assert (from_db.audiences, from_db.default_audience) == (
            from_file.audiences, from_file.default_audience or ("public",))
    for subject, token in legacy.items():
        assert roster.principal_for_token(conn, token)[0].subject == subject
    assert roster.principal(conn, "bot@example.com").kind == "agent"
    assert roster.principal(conn, "owner@example.com").is_admin
    assert roster.channel_map(conn) == {"C0FINANCE1": ("finance",), "C0GENERAL1": None}
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('ops_file_snapshot')")
        assert cur.fetchone()[0] is None
    assert "legacy-owner-token" not in capsys.readouterr().out
    assert cli.main(["import", "--identities", str(identities), "--channels", str(channels),
                     "--admin", "owner@example.com"]) == 2


def test_import_refuses_an_agent_administrator_and_changes_nothing(conn, tmp_path, monkeypatch):
    identities = tmp_path / "identities.json"
    identities.write_text(json.dumps(IDENTITIES), encoding="utf-8")
    channels = tmp_path / "channels.json"
    channels.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("STIGMERGY_TOKEN_STORE", "{}")

    assert cli.main(["import", "--identities", str(identities), "--channels", str(channels),
                     "--admin", "bot@example.com", "--agent", "bot@example.com"]) == 2
    assert roster.admin_count(conn) == 0
