"""Graph-measure snapshots: one per scope for every indexed commit, kept as history, read back with
the current values and the value seven days earlier (spec retrieval-and-curation §3.4, BQ-13)."""
import datetime as dt

import pytest

from stigmergy.index import build, measures
from stigmergy.index.backends.embedder import build_embedder
from tests import testdb
from tests.server.conftest import write_page


@pytest.fixture()
def indexed(tmp_path):
    repo = str(tmp_path / "repo")
    write_page(repo, "wiki/entities/Acme.md", {"title": "Acme"}, "# Acme\n\nA customer.")
    write_page(repo, "wiki/notes/Kickoff.md", {"title": "Kickoff"}, "# Kickoff\n\nWith [[Acme]].")
    write_page(repo, "wiki/notes/Ledger.md", {"title": "Ledger", "acl": ["finance"]}, "# Ledger\n\n[[Acme]].")
    conn = testdb.connect_or_skip("index")
    measures.ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM graph_measures")
    build.rebuild(conn, repo, build_embedder("fake"))
    yield conn
    conn.close()


def _snapshots(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT indexed_commit, scope, measures FROM graph_measures ORDER BY id")
        return cur.fetchall()


def test_a_rebuild_stores_one_snapshot_per_scope(indexed):
    stored = _snapshots(indexed)
    assert [scope for _commit, scope, _measures in stored] == ["brain", "public", "finance"]
    assert stored[0][2]["pages"] == 3 and stored[1][2]["pages"] == 2


def test_an_indexed_commit_is_snapshotted_once_and_history_is_kept(indexed):
    assert measures.record_snapshot(indexed, "a" * 40) == 3
    assert measures.record_snapshot(indexed, "a" * 40) == 0
    assert measures.record_snapshot(indexed, "b" * 40) == 3
    commits = [commit for commit, scope, _m in _snapshots(indexed) if scope == "brain"]
    assert commits == ["", "a" * 40, "b" * 40]


def test_a_snapshot_failure_never_fails_the_index_update(indexed, monkeypatch):
    monkeypatch.setattr(measures, "index_rows", lambda conn: 1 / 0)
    assert measures.record_snapshot(indexed, "c" * 40) == 0


def test_read_returns_current_values_the_series_and_the_week_old_value(indexed):
    with indexed.cursor() as cur:
        cur.execute("UPDATE graph_measures SET taken_at = now() - interval '9 days',"
                    " measures = jsonb_set(measures, '{pages}', '1') WHERE scope = 'public'")
    measures.record_snapshot(indexed, "d" * 40)
    result = measures.read(indexed, "public", now=dt.datetime.now(dt.UTC))
    assert result["scope"] == "public" and result["scopes"] == ["brain", "public", "finance"]
    assert result["measures"]["pages"] == 2
    assert [point["measures"]["pages"] for point in result["series"]] == [1, 2]
    assert result["series"][-1]["indexed_commit"] == "d" * 40
    assert result["week_ago"]["pages"] == 1
    assert {"hubs", "lists", "constellation"} <= set(result)
    with pytest.raises(measures.MeasureScopeError, match="scope must be one of brain, public, finance"):
        measures.read(indexed, "nowhere")
