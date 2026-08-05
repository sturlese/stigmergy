"""Search edge cases against a real postgres+pgvector.

Same skip discipline as test_pg_integration: green without a database locally, loud in CI.
Covers the gaps that file leaves open: an FTS arm emptied by the query or by filters must not
empty the results — the vector arm still serves; `--current-only` combined with filters; and the
two-identical-pages case, one cached embedding and two inserted rows.

`evals/run_retrieval.py`'s golden-set witness does not pass `filters=` to `search.search_arms`,
so it is structurally BLIND to the `entity` filter — this file, plus `test_pg_integration.py`'s
plural-entity tests, is where that filter's real coverage lives, not the golden run. See
`run_retrieval.py`'s own module docstring for the full framing.
"""
import json
from datetime import date
from pathlib import Path

import pytest

from stigmergy.index import build, search, store
from stigmergy.index.backends.embedder import build_embedder
from tests.index.test_pg_integration import FIXTURE, FIXTURE_PAGES, _connect_or_skip


@pytest.fixture(scope="module")
def conn():
    conn = _connect_or_skip()
    stats = build.rebuild(conn, FIXTURE, build_embedder("fake"))
    assert stats["pages"] == FIXTURE_PAGES
    yield conn
    conn.close()


# --- an empty FTS arm must not empty the results ---------------------------------------------

def test_query_matching_no_lexemes_still_returns_vector_candidates(conn):
    """Every word of the query is absent from the corpus: the lexical arm returns nothing and
    the semantic arm must carry the answer alone."""
    meta = store.read_meta(conn)
    assert search.fts_ranking(conn, "zzzzz qqqqq xxxxx", meta["fts_config"]) == []
    hits = search.search(conn, "zzzzz qqqqq xxxxx", today=date(2026, 7, 19))
    assert hits, "vector arm abandoned the query when FTS came back empty"
    assert all(h["arms"] == ["vec"] for h in hits)


def test_filter_that_excludes_every_fts_hit_still_returns_vec_candidates(conn):
    """The query's words live only in sources/ pages; a knowledge-only filter empties the
    lexical arm, but both arms share the filter so the semantic arm still serves knowledge
    pages."""
    meta = store.read_meta(conn)
    q = "Kestrel Lodge deposit booking"         # matches ingested pages only
    assert search.fts_ranking(conn, q, meta["fts_config"], filters={"zone": "wiki"}) == []
    hits = search.search(conn, q, filters={"zone": "wiki"}, today=date(2026, 7, 19))
    assert hits
    assert all(h["zone"] == "wiki" for h in hits)
    assert all(h["arms"] == ["vec"] for h in hits)


# --- current-only combined with filters -------------------------------------------------------

def test_current_only_and_entity_filter_combined(conn):
    hits = search.search(conn, "globex quarterly revenue impact report", k=10,
                         filters={"entity": "globex"}, include_superseded=False,
                         today=date(2026, 7, 19))
    assert hits
    # `entity` is a LIST, matched by MEMBERSHIP — see
    # test_pg_integration.test_frontmatter_filters_scope_both_arms for why this is membership,
    # not equality: a page anchored to several entities must still pass.
    assert all("globex" in h["entity"] for h in hits)
    assert all(not h["superseded_by"] for h in hits)
    paths = [h["path"] for h in hits]
    assert "sources/entities/globex/quarterly-report-q1-2026-final-bbbbbb.md" in paths
    assert "sources/entities/globex/quarterly-report-q1-2026-draft-aaaaaa.md" not in paths


def test_cli_current_only_and_filter_flags_combined(conn, capsys):
    from stigmergy.index import cli
    cli.search_main(["globex quarterly revenue impact report", "-k", "10",
                     "--filter", "entity=globex", "--current-only", "--json"])
    hits = json.loads(capsys.readouterr().out)
    assert hits
    assert all("globex" in h["entity"] and not h["superseded_by"] for h in hits)


# --- identical pages: one cached embedding, every row inserted -------------------------------
# (LAST in the file: it rebuilds the index from a scratch corpus.)

def test_two_identical_pages_share_one_embedding_but_both_rows_insert(conn, tmp_path):
    page = "---\ntitle: Same Title\n---\n# Same Title\nidentical body text\n"
    kdir = tmp_path / "wiki"
    kdir.mkdir()
    (kdir / "copy-one.md").write_text(page)
    (kdir / "copy-two.md").write_text(page)
    (kdir / "other.md").write_text("---\ntitle: Other\n---\n# Other\ndifferent body\n")

    with conn.cursor() as cur:                  # cold cache: the counts below assume it
        cur.execute("DROP TABLE IF EXISTS embedding_cache")
    stats = build.rebuild(conn, str(tmp_path), build_embedder("fake"))
    assert stats["pages"] == 3
    assert stats["embedded"] == 2, "identical title+body must embed once, distinct once"
    assert store.page_count(conn) == 3
    with conn.cursor() as cur:
        cur.execute("SELECT path, content_hash FROM pages_index ORDER BY path")
        rows = dict(cur.fetchall())
    assert rows["wiki/copy-one.md"] == rows["wiki/copy-two.md"]
    assert rows["wiki/other.md"] != rows["wiki/copy-one.md"]

    # leave the shared database as the sibling suites expect it: fixture corpus indexed
    build.rebuild(conn, FIXTURE, build_embedder("fake"))
    assert Path(FIXTURE).is_dir()
