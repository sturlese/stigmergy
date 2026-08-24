import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from stigmergy.index import build, search, store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.index.errors import EmptyIndexError, StigmergyIndexError
from tests import testdb

FIXTURE = str(Path(__file__).parent / "fixtures" / "repo")
FIXTURE_PAGES = 10
GLOBEX_ID = "ent_30000000-0000-4000-8000-000000000001"
INITECH_ID = "ent_30000000-0000-4000-8000-000000000002"


def _connect_or_skip():
    return testdb.connect_or_skip("index")


@pytest.fixture(scope="module")
def conn():
    connection = _connect_or_skip()
    stats = build.rebuild(connection, FIXTURE, build_embedder("fake"))
    assert stats["pages"] == FIXTURE_PAGES
    yield connection
    connection.close()


def test_rebuild_populates_only_wiki_and_sources(conn):
    assert store.page_count(conn) == FIXTURE_PAGES
    with conn.cursor() as cursor:
        cursor.execute("SELECT zone, count(*) FROM pages_index GROUP BY zone ORDER BY zone")
        assert dict(cursor.fetchall()) == {"sources": 6, "wiki": 4}


def test_acl_links_and_hashes_are_stored(conn):
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT acl, inlinks, links, content_hash FROM pages_index "
            "WHERE path = 'wiki/concepts/Support refunds.md'"
        )
        acl, inlinks, links, content_hash = cursor.fetchone()
    assert acl is None
    assert inlinks == 1
    assert links == []
    assert content_hash.startswith("sha256:")


def test_lexical_and_vector_indexes_exist(conn):
    with conn.cursor() as cursor:
        cursor.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'pages_index'")
        indexes = {name: definition.lower() for name, definition in cursor.fetchall()}
    assert "using gin" in indexes["pages_index_tsv_gin"]
    assert "using hnsw" in indexes["pages_index_embedding_hnsw"]
    assert "halfvec_cosine_ops" in indexes["pages_index_embedding_hnsw"]
    assert "links" in indexes["pages_index_links_gin"]


def test_schema_supports_the_production_embedding_dimension(conn):
    class Rollback(Exception):
        pass

    with pytest.raises(Rollback), conn.transaction():
        store.init_schema(conn, dim=3072, model="text-embedding-3-large", fts_config="english")
        store.create_search_indexes(conn)
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = 'pages_index'::regclass AND attname = 'embedding'"
            )
            assert cursor.fetchone()[0] == "halfvec(3072)"
        raise Rollback()


def test_hybrid_search_returns_explainable_hits(conn):
    hits = search.search(
        conn,
        "How did Globex quarterly revenue perform?",
        today=date(2026, 8, 24),
    )
    assert hits
    assert all(hit["score"] > 0 and hit["arms"] and "snippet" in hit for hit in hits)


def test_filters_apply_to_both_search_arms(conn):
    hits = search.search(conn, "renewal coordination", k=10, filters={"entity": GLOBEX_ID})
    assert hits
    assert all(GLOBEX_ID in hit["entity"] for hit in hits)
    with pytest.raises(ValueError, match="unknown filter"):
        search.search(conn, "query", filters={"body": "value"})


def test_acl_filters_both_rankings_before_candidate_fusion(conn):
    hidden = "wiki/notes/Globex and Initech renewal.md"

    engineering = search.search_arms(
        conn,
        "renewal coordination",
        k=10,
        audiences={"engineering"},
    )
    finance = search.search_arms(
        conn,
        "renewal coordination",
        k=10,
        audiences={"finance"},
    )

    assert hidden not in engineering["fts"]
    assert hidden not in engineering["vec"]
    assert hidden in set(finance["fts"]) | set(finance["vec"])


def test_multi_entity_page_is_found_by_either_anchor(conn):
    expected = "wiki/notes/Globex and Initech renewal.md"
    for entity_id in (GLOBEX_ID, INITECH_ID):
        hits = search.search(conn, "renewal coordination", k=10, filters={"entity": entity_id})
        assert expected in [hit["path"] for hit in hits]


def test_entity_ids_are_part_of_the_lexical_document(conn):
    hits = search.search(conn, INITECH_ID, k=10)
    assert "wiki/notes/Globex and Initech renewal.md" in [hit["path"] for hit in hits]


def test_url_text_does_not_break_full_text_search(conn):
    ranking = search.fts_ranking(
        conn,
        "see https://example.com/globex/report.pdf",
        store.read_meta(conn)["fts_config"],
    )
    assert isinstance(ranking, list)


def test_unchanged_rebuild_reuses_embedding_cache(conn):
    before = search.search(conn, "refund annual plans", k=10)
    stats = build.rebuild(conn, FIXTURE, build_embedder("fake"))
    assert stats["embedded"] == 0
    assert stats["cached"] == FIXTURE_PAGES
    after = search.search(conn, "refund annual plans", k=10)
    assert [(hit["path"], hit["score"]) for hit in before] == [
        (hit["path"], hit["score"]) for hit in after
    ]


def test_rebuild_removes_deleted_pages_from_search(conn, tmp_path):
    repo = tmp_path / "brain"
    shutil.copytree(FIXTURE, repo)
    victim = "wiki/notes/Globex and Initech renewal.md"
    embedder = build_embedder("fake")
    try:
        build.rebuild(conn, str(repo), embedder)
        assert victim in {
            hit["path"] for hit in search.search(conn, "Globex Initech renewal", k=20)
        }

        (repo / victim).unlink()
        build.rebuild(conn, str(repo), embedder)

        assert victim not in store.existing_paths(conn)
        assert victim not in {
            hit["path"] for hit in search.search(conn, "Globex Initech renewal", k=20)
        }
    finally:
        build.rebuild(conn, FIXTURE, embedder)


def test_rebuild_cli(capsys, tmp_path):
    from stigmergy.index import cli

    repo = tmp_path / "brain"
    shutil.copytree(FIXTURE, repo)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Index Test",
            "-c",
            "user.email=index@example.invalid",
            "commit",
            "-qm",
            "test fixture",
        ],
        cwd=repo,
        check=True,
    )

    cli.index_main(["--rebuild", "--repo", str(repo), "--embedder", "fake"])
    assert f"indexed {FIXTURE_PAGES} pages" in capsys.readouterr().out


def test_empty_index_fails_loudly_and_can_be_rebuilt(conn):
    with conn.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS index_meta")
    with pytest.raises(EmptyIndexError):
        search.search(conn, "anything")
    build.rebuild(conn, FIXTURE, build_embedder("fake"))


class _WrongHostEmbedder:
    host = "https://other.example/v1"
    model = "fake-hashed-bow-256"

    def embed(self, _texts):
        raise AssertionError("host mismatch must be checked before embedding")


def test_embedding_host_mismatch_fails_before_query_embedding(conn):
    build.rebuild(conn, FIXTURE, build_embedder("fake"))
    with pytest.raises(StigmergyIndexError, match="not provably the same vector space"):
        search.search_arms(conn, "globex", embedder=_WrongHostEmbedder())
