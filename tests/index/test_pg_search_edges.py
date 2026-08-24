from pathlib import Path

import pytest

from stigmergy.index import build, search, store
from stigmergy.index.backends.embedder import build_embedder
from tests.index.support import write_controls
from tests.index.test_pg_integration import FIXTURE, FIXTURE_PAGES, _connect_or_skip


@pytest.fixture(scope="module")
def conn():
    connection = _connect_or_skip()
    assert build.rebuild(connection, FIXTURE, build_embedder("fake"))["pages"] == FIXTURE_PAGES
    yield connection
    connection.close()


def test_empty_lexical_arm_still_returns_vector_candidates(conn):
    query = "zzzzz qqqqq xxxxx"
    assert search.fts_ranking(conn, query, store.read_meta(conn)["fts_config"]) == []
    hits = search.search(conn, query)
    assert hits
    assert all(hit["arms"] == ["vec"] for hit in hits)


def test_filter_excluding_lexical_hits_still_returns_filtered_vectors(conn):
    query = "Kestrel Lodge deposit booking"
    assert search.fts_ranking(
        conn,
        query,
        store.read_meta(conn)["fts_config"],
        filters={"zone": "wiki"},
    ) == []
    hits = search.search(conn, query, filters={"zone": "wiki"})
    assert hits
    assert all(hit["zone"] == "wiki" for hit in hits)


def test_identical_pages_share_an_embedding_but_keep_distinct_rows(conn, tmp_path):
    repo = tmp_path / "repo"
    folder = repo / "sources" / "2026" / "08"
    folder.mkdir(parents=True)
    for index in (1, 2):
        capture_id = f"90000000-0000-4000-8000-{index:012d}"
        text = (
            "---\n"
            f"id: {capture_id}\n"
            "type: source\n"
            "acl: null\n"
            "captured_at: '2026-08-01T00:00:00+00:00'\n"
            "---\n\n"
            "# Captured source\n\nidentical searchable body\n"
        )
        (folder / f"{capture_id}.md").write_text(text, encoding="utf-8")
    write_controls(repo)

    with conn.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS embedding_cache")
    stats = build.rebuild(conn, str(repo), build_embedder("fake"))
    assert stats["pages"] == 2
    assert stats["embedded"] == 1
    assert store.page_count(conn) == 2

    build.rebuild(conn, FIXTURE, build_embedder("fake"))
    assert Path(FIXTURE).is_dir()
