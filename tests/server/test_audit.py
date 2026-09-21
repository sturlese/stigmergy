"""Privacy and retention contract for the shared audit writer."""

import json
import uuid

from stigmergy.server import audit
from stigmergy.server.audit import (
    AuditWriter,
    ensure_audit_table,
    minimize_audit_args,
    minimize_audit_result,
)


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

    assert minimized["query"] == {"chars": len(raw["query"])}
    assert minimized["filters"] == {"fields": 2}
    assert minimized["audiences"] == {"items": 2}
    assert minimized["capture_id"] == opaque_id
    assert minimized["text_sha256"] == digest
    assert minimized["question"] == {"chars": 64}
    assert minimized["max_results"] == 7
    assert minimized["include_archived"] is False
    assert minimized["optional"] is None
    serialized = json.dumps(minimized)
    assert "confidential" not in serialized.lower()
    assert "private:board" not in serialized


def test_minimize_audit_args_fingerprints_untrusted_field_names():
    minimized = minimize_audit_args({"person@example.com": "secret"})

    assert list(minimized) == ["field_1"]
    assert "person@example.com" not in json.dumps(minimized)
    assert "secret" not in json.dumps(minimized)


def test_minimize_audit_result_keeps_numeric_telemetry_without_free_text():
    minimized = minimize_audit_result({
        "hits": 3,
        "usage": {"input_tokens": 40, "output_tokens": 12, "model": "private-model"},
        "summary": "private result summary",
        "paths": ["wiki/private.md"],
    })

    assert minimized == {
        "hits": 3,
        "usage": {"input_tokens": 40, "output_tokens": 12, "model": {"chars": 13}},
        "summary": {"chars": 22},
        "paths": {"items": 1},
    }
    serialized = json.dumps(minimized)
    assert "private-model" not in serialized
    assert "private result summary" not in serialized
    assert "wiki/private.md" not in serialized


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
        result={"hits": 1, "usage": {"input_tokens": 8, "model": "private-model"}},
    )

    with conn.cursor() as cur:
        cur.execute("SELECT identity, args, result FROM audit_log ORDER BY identity")
        rows = cur.fetchall()
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE tablename = 'audit_log' "
            "AND indexname = 'audit_log_ts_idx'"
        )
        retention_index = cur.fetchone()

    assert [row[0] for row in rows] == ["current@example.com", "recent@example.com"]
    assert rows[0][1]["query"] == {"chars": len("sensitive search")}
    assert rows[0][2] == {
        "hits": 1,
        "usage": {"input_tokens": 8, "model": {"chars": len("private-model")}},
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


def test_cleanup_failure_retries_after_five_minutes(monkeypatch):
    clock = iter((100.0, 100.0, 399.0, 400.0, 400.0))
    monkeypatch.setattr(audit, "_next_purge_at", 0.0)
    monkeypatch.setattr(audit.time, "monotonic", lambda: next(clock))
    attempts = []

    class FailingCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, statement, params):
            attempts.append((statement, params))
            raise RuntimeError("database unavailable")

    class FailingConnection:
        def cursor(self):
            return FailingCursor()

    conn = FailingConnection()
    audit._purge_expired_if_due(conn)
    audit._purge_expired_if_due(conn)
    audit._purge_expired_if_due(conn)

    assert len(attempts) == 2
    assert audit._next_purge_at == 400.0 + audit._PURGE_RETRY_SECONDS
