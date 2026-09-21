"""Privacy and retention contract for the shared audit writer."""

import json
import uuid

from stigmergy.server import audit
from stigmergy.server.audit import AuditWriter, ensure_audit_table, minimize_audit_args


def test_minimize_audit_args_removes_free_text_without_tool_specific_rules():
    opaque_id = str(uuid.uuid4())
    digest = "a" * 64
    raw = {
        "query": "confidential acquisition target",
        "path": "wiki/entities/Confidential Person.md",
        "filters": {"entity": "Confidential Person", "topic": "acquisition"},
        "audiences": ["private:board", "private:finance"],
        "capture_id": opaque_id,
        "text_sha256": digest,
        "question": digest,
        "max_results": 7,
        "include_archived": False,
        "optional": None,
    }

    minimized = minimize_audit_args(raw)

    assert minimized["query"] == {
        "chars": len(raw["query"]),
        "sha256": audit._sha256(raw["query"]),
    }
    assert minimized["filters"]["fields"] == 2
    assert minimized["audiences"]["items"] == 2
    assert minimized["capture_id"] == opaque_id
    assert minimized["text_sha256"] == digest
    assert minimized["question"] == {"chars": 64, "sha256": audit._sha256(digest)}
    assert minimized["max_results"] == 7
    assert minimized["include_archived"] is False
    assert minimized["optional"] is None
    serialized = json.dumps(minimized)
    assert "confidential" not in serialized.lower()
    assert "private:board" not in serialized


def test_minimize_audit_args_fingerprints_untrusted_field_names():
    minimized = minimize_audit_args({"person@example.com": "secret"})

    assert list(minimized) == [f"field_{audit._sha256('person@example.com')[:12]}"]
    assert "person@example.com" not in json.dumps(minimized)
    assert "secret" not in json.dumps(minimized)


def test_writer_removes_expired_rows_and_keeps_recent_rows(indexed, monkeypatch):
    conn, _fixture = indexed
    ensure_audit_table(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM audit_log")
        cur.execute(
            """
            INSERT INTO audit_log (ts, identity, tool, args, duration_ms, outcome)
            VALUES
                (now() - interval '31 days', 'old@example.com', 'ask', '{}', 1, 'ok'),
                (now() - interval '29 days', 'recent@example.com', 'ask', '{}', 1, 'ok')
            """
        )
    monkeypatch.setattr(audit, "_next_purge_at", 0.0)

    AuditWriter(conn).write(
        identity="current@example.com",
        tool="search_brain",
        args={"query": "sensitive search"},
        duration_ms=2,
        outcome="ok",
    )

    with conn.cursor() as cur:
        cur.execute("SELECT identity, args FROM audit_log ORDER BY identity")
        rows = cur.fetchall()
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE tablename = 'audit_log' "
            "AND indexname = 'audit_log_ts_idx'"
        )
        retention_index = cur.fetchone()

    assert [row[0] for row in rows] == ["current@example.com", "recent@example.com"]
    assert rows[0][1]["query"] == {
        "chars": len("sensitive search"),
        "sha256": audit._sha256("sensitive search"),
    }
    assert retention_index == (1,)


def test_cleanup_runs_at_most_once_per_day(indexed, monkeypatch):
    conn, _fixture = indexed
    ensure_audit_table(conn)
    clock = iter((100.0, 100.0, 101.0, 86_501.0, 86_501.0))
    monkeypatch.setattr(audit, "_next_purge_at", 0.0)
    monkeypatch.setattr(audit.time, "monotonic", lambda: next(clock))
    statements = []

    class CursorProxy:
        def __init__(self, cursor):
            self.cursor = cursor

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.cursor.close()

        def execute(self, statement, params):
            statements.append((statement, params))
            self.cursor.execute(statement, params)

    class ConnectionProxy:
        def cursor(self):
            return CursorProxy(conn.cursor())

    proxy = ConnectionProxy()
    audit._purge_expired_if_due(proxy)
    audit._purge_expired_if_due(proxy)
    audit._purge_expired_if_due(proxy)

    assert len(statements) == 2
    assert all(params == (audit.AUDIT_RETENTION_DAYS,) for _statement, params in statements)
