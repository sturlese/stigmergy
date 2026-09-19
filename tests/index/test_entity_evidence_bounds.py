"""Cardinality guards for entity evidence SQL reads."""

from stigmergy.index import search


class _Cursor:
    def __init__(self):
        self.sql = ""
        self.params = ()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return []


class _Connection:
    def __init__(self):
        self.last_cursor = None

    def cursor(self):
        self.last_cursor = _Cursor()
        return self.last_cursor


def test_visible_page_evidence_caps_and_deduplicates_thousands_of_candidates_before_sql():
    conn = _Connection()
    candidates = [f"wiki/concepts/Page {index}.md" for index in range(5_000)] * 2

    rows, truncated = search.fetch_visible_pages_limited(
        conn, candidates, audiences=None, limit=3
    )

    assert rows == {}
    assert truncated is True
    assert "count(*) over" not in conn.last_cursor.sql.lower()
    assert len(conn.last_cursor.params[0]) == 4
    assert conn.last_cursor.params[0] == conn.last_cursor.params[1]
    assert conn.last_cursor.params[-1] == 4


def test_entity_timeline_uses_a_cap_plus_one_query_not_an_unbounded_total_count():
    conn = _Connection()

    rows, truncated = search.entity_timeline(
        conn, "ent_00000000-0000-4000-8000-000000000001", audiences=None, limit=3
    )

    assert rows == []
    assert truncated is False
    assert "count(*) over" not in conn.last_cursor.sql.lower()
    assert conn.last_cursor.params[-1] == 4
