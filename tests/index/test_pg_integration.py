"""Integration against the REAL composed stack — a real postgres+pgvector, brought up with
`docker compose up`, never a stand-in.

Skips cleanly when no database is reachable, so `make test` stays green on a machine without
docker; CI always brings the composition up, so these run there. The fake embedder keeps
everything keyless.

Skip guard: when `$STIGMERGY_TEST_DSN` is explicitly set — as CI does — an unreachable database is
a FAILURE, not a skip. A check that stops running must be impossible to miss, and a green CI
whose pg suites silently skipped is exactly that failure. Both that guard and the refusal to run
against any database but `stigmergy_test` live in `tests.testdb`; this module just names itself
to it.
"""
import json
from datetime import date
from pathlib import Path

import pytest

from stigmergy.index import build, search, store
from stigmergy.index.backends.embedder import build_embedder
from tests import testdb

FIXTURE = str(Path(__file__).parent / "fixtures" / "repo")
# 4 wiki + 6 source + 1 view — asserted against the fixture repo. The 4th wiki page,
# `globex-initech-partnership.md`, carries `entity: [globex, initech]`: the fixture's only
# multi-element `entity:` page, and so the plural `entity:` contract's only witness at the
# Postgres level.
FIXTURE_PAGES = 11


def _connect_or_skip():
    """This module's name for the shared seam (also imported by test_pg_search_edges.py)."""
    return testdb.connect_or_skip("index")


@pytest.fixture(scope="module")
def conn():
    conn = _connect_or_skip()
    stats = build.rebuild(conn, FIXTURE, build_embedder("fake"))
    assert stats["pages"] == FIXTURE_PAGES
    yield conn
    conn.close()


def test_rebuild_populates_exactly_the_included_zones(conn):
    assert store.page_count(conn) == FIXTURE_PAGES
    with conn.cursor() as cur:
        cur.execute("SELECT zone, count(*) FROM pages_index GROUP BY zone ORDER BY zone")
        assert dict(cur.fetchall()) == {"views": 1, "sources": 6, "wiki": 4}
        cur.execute("SELECT count(*) FROM pages_index WHERE path LIKE '%excluded%'")
        assert cur.fetchone()[0] == 0


def test_filter_and_acl_columns_land_in_the_schema(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT status, owner, acl, inlinks, content_hash FROM pages_index"
                    " WHERE path = 'wiki/decisions/refund-policy.md'")
        status, owner, acl, inlinks, content_hash = cur.fetchone()
    assert status == "canonical" and owner == "steward"
    assert acl is None                      # no acl -> NULL (open); stored here, enforced on read
    assert inlinks == 2
    assert content_hash.startswith("sha256:")
    with conn.cursor() as cur:
        cur.execute("SELECT acl FROM pages_index WHERE path = 'views/globex.md'")
        assert cur.fetchone()[0] == ["sales"]    # text[] — labels survive verbatim, no CSV


def test_links_column_and_its_gin_index_land_in_the_schema(conn):
    """`links` is resolved repo-relative paths (never stems), and a GIN index exists so
    `read_page`'s backlinks query is a containment lookup, never a scan."""
    with conn.cursor() as cur:
        cur.execute("SELECT links FROM pages_index"
                    " WHERE path = 'wiki/decisions/refund-policy.md'")
        assert cur.fetchone()[0] == ["wiki/playbooks/support-playbook.md"]
        cur.execute("SELECT indexdef FROM pg_indexes"
                    " WHERE tablename = 'pages_index' AND indexname = 'pages_index_links_gin'")
        indexdef = cur.fetchone()
    assert indexdef is not None, "pages_index_links_gin is missing from the rebuilt schema"
    assert "using gin" in indexdef[0].lower()
    assert "links" in indexdef[0]


def test_the_two_retrieval_indexes_exist_and_match_the_operators_the_arms_use(conn):
    """GIN for the lexical arm, HNSW for the semantic one. Both arms once seq-scanned — only the
    links GIN existed — and a cold-start rebuild is what made that visible.

    The opclass is the half worth asserting by name. `search.VEC_SQL` orders by `embedding <=>
    ...` (cosine) on a `halfvec` column; an HNSW built with an L2 opclass — or with `vector_*`
    against a `halfvec` column — would be a perfectly valid index the planner never uses for that
    operator: decoration that looks like coverage."""
    with conn.cursor() as cur:
        cur.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'pages_index'")
        by_name = {n: d.lower() for n, d in cur.fetchall()}

    assert "pages_index_tsv_gin" in by_name, f"missing; have {sorted(by_name)}"
    assert "using gin" in by_name["pages_index_tsv_gin"] and "tsv" in by_name["pages_index_tsv_gin"]

    assert "pages_index_embedding_hnsw" in by_name, f"missing; have {sorted(by_name)}"
    hnsw = by_name["pages_index_embedding_hnsw"]
    assert "using hnsw" in hnsw
    assert "halfvec_cosine_ops" in hnsw, (
        "the HNSW opclass must match the column type AND `<=>` (cosine), or the planner ignores it")


def test_the_schema_and_its_indexes_build_at_the_PRODUCTION_embedding_dimension(conn):
    """The test the previous one could not be. Every other test here runs the FAKE embedder at
    256 dimensions, so `create_search_indexes` passed locally and then failed on the first real
    rebuild: pgvector refuses HNSW above 2000 dimensions and `text-embedding-3-large` is 3072.
    A suite that only ever sees 256 cannot see that ceiling.

    So this builds the schema at the production dimension explicitly. It is the cheapest possible
    guard — no embedder, no corpus, no API key — against a class of defect that otherwise only
    surfaces in CI against a real database.
    """
    from stigmergy.index import store as _store

    with pytest.raises(_Rollback), conn.transaction():
        _store.init_schema(conn, dim=3072, model="text-embedding-3-large", fts_config="english")
        _store.create_search_indexes(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT indexdef FROM pg_indexes"
                        " WHERE indexname = 'pages_index_embedding_hnsw'")
            assert "halfvec_cosine_ops" in cur.fetchone()[0].lower()
            cur.execute("SELECT format_type(atttypid, atttypmod) FROM pg_attribute"
                        " WHERE attrelid = 'pages_index'::regclass AND attname = 'embedding'")
            assert cur.fetchone()[0] == "halfvec(3072)"
        raise _Rollback()      # leave the fixture's own index untouched — see the class below


class _Rollback(Exception):
    """Rolls the probe out of the SESSION-scoped fixture connection.

    The probe has to DROP and recreate `pages_index` (that is what `init_schema` does), and every
    other test in this module reads the fixture's own built index. Raising out of
    `conn.transaction()` rolls the whole thing back — the schema, the indexes, everything — so the
    probe costs the fixture nothing. `pytest.raises` catches it at the call site."""


def test_spanish_query_returns_hits_with_factors(conn):
    """A whole question, not a keyword bag, returns top-k hits with their factors attached."""
    hits = search.search(conn, "How did the quarter go for Globex? quarterly revenue impact",
                         today=date(2026, 7, 19))
    assert hits and len(hits) <= 5
    for h in hits:
        assert isinstance(h["factors"], list)
        assert h["arms"] and h["score"] > 0 and "snippet" in h


def test_superseded_draft_ranks_below_its_successor_end_to_end(conn):
    # TOLD, not inferred: this layer never infers "globex" from the query — the hint is what the
    # service resolves and passes down. The boost lands on both globex pages equally, so the
    # supersession ordering this test pins is measured with the factor live.
    hits = search.search(conn, "globex quarterly revenue impact report", k=10,
                         today=date(2026, 7, 19), entity_hint="globex")
    paths = [h["path"] for h in hits]
    draft = "sources/entities/globex/quarterly-report-q1-2026-draft-aaaaaa.md"
    final = "sources/entities/globex/quarterly-report-q1-2026-final-bbbbbb.md"
    assert final in paths and draft in paths
    assert paths.index(final) < paths.index(draft)
    assert "superseded" in next(h for h in hits if h["path"] == draft)["factors"]


def test_frontmatter_filters_scope_both_arms(conn):
    hits = search.search(conn, "quarterly revenue report", k=10,
                         filters={"entity": "globex"})
    assert hits
    # `entity` is a LIST, matched by MEMBERSHIP — a page anchored to SEVERAL entities
    # (`globex-initech-partnership.md`) still passes this filter without that entity being the
    # only one on the page. Asserting equality here would re-test "equivalent to equality", not
    # the membership contract this filter actually promises.
    assert all("globex" in h["entity"] for h in hits)
    with pytest.raises(ValueError):
        search.search(conn, "q", filters={"body": "x"})   # not a filter column


def test_the_plural_entity_page_is_found_by_either_of_its_two_entities(conn):
    """The structural witness the plural `entity:` contract needs at the Postgres level — every
    OTHER exercise of the `entity` filter in this suite uses a single-element page, where
    membership is indistinguishable from equality."""
    page = "wiki/decisions/globex-initech-partnership.md"
    for entity_filter in ("globex", "initech"):
        hits = search.search(conn, "cross-account renewal coordination", k=10,
                             filters={"entity": entity_filter})
        assert page in [h["path"] for h in hits], entity_filter


def test_an_fts_query_naming_only_the_second_entity_finds_the_plural_page(conn):
    """Proves `array_to_string` folding, not merely storage: "initech" appears NOWHERE in this
    page's title, body or tags — only in its `entity:` frontmatter — so finding it by that word
    alone proves the second array element reached the `tsv` column."""
    hits = search.search(conn, "initech", k=10)
    assert "wiki/decisions/globex-initech-partnership.md" in [h["path"] for h in hits]


def test_url_bearing_query_does_not_crash_the_lexical_arm(conn):
    """Lexemes can retain tsquery syntax characters (':' and '/' in URLs); each lexeme is
    quoted before to_tsquery, so a URL-bearing question must not crash the FTS arm."""
    meta = store.read_meta(conn)
    ranking = search.fts_ranking(conn, "see https://example.com/globex/q1-final.pdf report",
                                 meta["fts_config"])
    assert isinstance(ranking, list)
    hits = search.search(conn, "revenue at https://example.com/globex", k=5)
    assert isinstance(hits, list)


def test_current_only_drops_the_superseded_page(conn):
    hits = search.search(conn, "globex quarterly revenue impact report", k=10,
                         include_superseded=False)
    assert all(not h["superseded_by"] for h in hits)


def test_rebuild_reuses_the_embedding_cache_and_is_idempotent(conn):
    """The economics: an unchanged corpus re-embeds nothing, and an in-place rebuild returns
    identical hit lists (the full-wipe variant is scripts/e2e.sh)."""
    before = search.search(conn, "refund policy annual plans", k=10)
    stats = build.rebuild(conn, FIXTURE, build_embedder("fake"))
    assert stats["embedded"] == 0
    assert stats["cached"] == FIXTURE_PAGES
    after = search.search(conn, "refund policy annual plans", k=10)
    assert [(h["path"], h["score"]) for h in before] == [(h["path"], h["score"]) for h in after]


def test_cli_end_to_end_json(capsys):
    """The two console entry points, against the running database."""
    from stigmergy.index import cli
    _connect_or_skip().close()
    cli.index_main(["--rebuild", "--repo", FIXTURE, "--embedder", "fake"])
    out = capsys.readouterr().out
    assert f"indexed {FIXTURE_PAGES} pages" in out
    cli.search_main(["How much was the deposit on the Kestrel Lodge booking?", "--json"])
    hits = json.loads(capsys.readouterr().out)
    assert hits and all("factors" in h for h in hits)
    assert any("kestrel" in h["path"] for h in hits)


def test_search_on_an_empty_index_fails_loudly():
    from stigmergy.index.errors import EmptyIndexError
    conn = _connect_or_skip()
    try:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS index_meta")
        with pytest.raises(EmptyIndexError):
            search.search(conn, "anything")
    finally:
        conn.close()
