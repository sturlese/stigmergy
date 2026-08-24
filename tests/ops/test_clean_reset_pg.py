import json

import pytest

from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.ops.reset import (
    ResetRefused,
    confirmation_token,
    reset_environment,
)
from tests import testdb


def _database(conn):
    with conn.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        return cursor.fetchone()[0]


def test_clean_reset_discards_old_state_and_builds_only_the_target_baseline(tmp_path):
    conn = testdb.connect_or_skip("clean-reset")
    database = _database(conn)
    repo = tmp_path / "staging-brain"
    (repo / "wiki" / "views").mkdir(parents=True)
    (repo / "sources" / "meetings").mkdir(parents=True)
    (repo / "ops" / "templates").mkdir(parents=True)
    (repo / "wiki" / "views" / "legacy.md").write_text("old view")
    (repo / "sources" / "meetings" / "legacy.md").write_text("old source")
    (repo / "ops" / "identities.json").write_text('{"master": {}}\n')
    (repo / "ops" / "slack-channels.json").write_text("{}\n")
    (repo / "ops" / "templates" / "note.md").write_text("target note template\n")

    with conn.cursor() as cursor:
        cursor.execute("CREATE TABLE IF NOT EXISTS legacy_repair_queue (payload JSONB)")
        cursor.execute("INSERT INTO legacy_repair_queue VALUES ('{\"kind\": \"raw\"}')")
    evidence = MemoryEvidenceStore()
    evidence.put(b"old object")
    token = confirmation_token(
        environment="test",
        database=database,
        bucket=evidence.bucket,
        repository=repo,
    )

    result = reset_environment(
        conn,
        evidence,
        environment="test",
        database=database,
        bucket=evidence.bucket,
        repository=repo,
        confirmation=token,
        embedding_dim=3,
        embedding_model="fake",
    )

    assert result == {"deleted_objects": 1, "status": "reset"}
    assert evidence.objects == {}
    with conn.cursor() as cursor:
        cursor.execute("SELECT to_regclass('legacy_repair_queue')")
        assert cursor.fetchone()[0] is None
        cursor.execute("SELECT count(*) FROM capture_queue")
        assert cursor.fetchone()[0] == 0
        cursor.execute("SELECT count(*) FROM pages_index")
        assert cursor.fetchone()[0] == 0
    assert not (repo / "wiki" / "views").exists()
    assert not (repo / "sources" / "meetings").exists()
    assert (repo / "wiki" / "notes" / ".gitkeep").is_file()
    assert (repo / "wiki" / "concepts" / ".gitkeep").is_file()
    assert (repo / "wiki" / "entities" / ".gitkeep").is_file()
    assert json.loads((repo / "ops" / "entity-registry.json").read_text()) == {
        "entities": {},
        "redirects": {},
        "version": 1,
    }
    assert (repo / "ops" / "identities.json").read_text() == '{"master": {}}\n'
    assert (repo / "ops" / "templates" / "note.md").read_text() == "target note template\n"
    conn.close()


def test_reset_requires_an_exact_non_production_target(tmp_path):
    conn = testdb.connect_or_skip("clean-reset-guard")
    database = _database(conn)
    evidence = MemoryEvidenceStore()

    with pytest.raises(ResetRefused, match="test and staging"):
        reset_environment(
            conn,
            evidence,
            environment="production",
            database=database,
            bucket=evidence.bucket,
            repository=tmp_path / "brain",
            confirmation="anything",
            embedding_dim=3,
            embedding_model="fake",
        )
    with pytest.raises(ResetRefused, match="confirmation"):
        reset_environment(
            conn,
            evidence,
            environment="test",
            database=database,
            bucket=evidence.bucket,
            repository=tmp_path / "brain",
            confirmation="wrong",
            embedding_dim=3,
            embedding_model="fake",
        )
    with pytest.raises(ResetRefused, match="evidence store"):
        reset_environment(
            conn,
            evidence,
            environment="test",
            database=database,
            bucket="a-different-bucket",
            repository=tmp_path / "brain",
            confirmation="wrong",
            embedding_dim=3,
            embedding_model="fake",
        )
    conn.close()
