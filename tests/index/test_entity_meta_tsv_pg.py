"""Entity-page `role`/`aliases` fold into the tsv source: steward-authored metadata on a
`type: entity` page becomes lexically findable, with no ranking factor added or changed. The fold
changes what is FOUND, never what SCORES.

Its own tiny corpus, isolated from `tests/index/test_pg_integration.py`'s module-scoped fixture.
"""
from datetime import date

import pytest

from stigmergy.index import build, search
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.index.store import read_meta
from tests import testdb

ENTITY_PAGE = "wiki/entities/aurora.md"
PLAIN_PAGE = "wiki/notes/unrelated.md"


def _connect_or_skip():
    return testdb.connect_or_skip("index")


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    root = tmp_path_factory.mktemp("entity-meta-tsv")
    edir = root / "wiki" / "entities"
    edir.mkdir(parents=True)
    (edir / "aurora.md").write_text(
        "---\ntype: entity\ntitle: Aurora Holdings\nentity: [aurora]\n"
        "role: exclusive northern-hemisphere logistics reseller\n"
        "aliases: [AuroraCo, Aurora Logistics Partners]\nverification: verified\n---\n"
        "Aurora Holdings is a governed entity page with no further body detail.")
    ndir = root / "wiki" / "notes"
    ndir.mkdir(parents=True)
    (ndir / "unrelated.md").write_text(
        "---\ntype: note\ntitle: Unrelated Note\nverification: verified\n---\n"
        "A page about something else entirely, with its own separate content.")
    conn = _connect_or_skip()
    stats = build.rebuild(conn, str(root), build_embedder("fake"))
    assert stats["pages"] == 2
    yield conn
    conn.close()


def test_entity_page_role_is_lexically_findable(conn):
    """"reseller" appears ONLY in `aurora.md`'s `role:` frontmatter — nowhere in its title or
    body — so finding it by that word alone proves the field reached `tsv`."""
    fts_config = read_meta(conn)["fts_config"]
    hits = search.fts_ranking(conn, "reseller", fts_config)
    assert ENTITY_PAGE in hits
    assert PLAIN_PAGE not in hits


def test_entity_page_aliases_are_lexically_findable(conn):
    """"AuroraCo" appears ONLY in `aliases:` — not in title, body, or the registered `entity:`
    id itself ("aurora")."""
    fts_config = read_meta(conn)["fts_config"]
    hits = search.fts_ranking(conn, "AuroraCo", fts_config)
    assert ENTITY_PAGE in hits


def test_a_non_entity_pages_role_shaped_frontmatter_never_folds_in(conn):
    """`_entity_meta_text` only fires for `type: entity` pages — `unrelated.md` carries no
    `role`/`aliases` at all, and this pins that the folding is type-gated, not universal."""
    fts_config = read_meta(conn)["fts_config"]
    assert search.fts_ranking(conn, "reseller", fts_config) == [ENTITY_PAGE]


def test_no_new_ranking_factor_is_introduced_for_the_entity_meta_fold(conn):
    """The fold changes what is FOUND, not what SCORES — the entity page's factors list is
    exactly what `rank.contract_factors` produces from its EXISTING fields, nothing new."""
    hits = search.search(conn, "reseller", today=date(2026, 7, 30))
    hit = next(h for h in hits if h["path"] == ENTITY_PAGE)
    assert hit["factors"] == []          # current, and no entity/period boost triggered here
