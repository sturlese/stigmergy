"""Entity pages are searchable knowledge: indexed like notes, aliases inside the lexical document."""

from stigmergy.index import build, search, store
from stigmergy.index.backends.embedder import build_embedder
from tests import testdb

ENTITY = "wiki/entities/Kestrel Lodge.md"
NOTE = "wiki/notes/Knowledge.md"


def test_entity_page_is_indexed_with_its_aliases_in_the_tsvector(tmp_path):
    entity_dir = tmp_path / "wiki" / "entities"
    entity_dir.mkdir(parents=True)
    (entity_dir / "Kestrel Lodge.md").write_text(
        "---\n"
        "id: page_00000000-0000-4000-8000-000000000002\n"
        "type: entity\n"
        "title: Kestrel Lodge\n"
        "status: developing\n"
        "created: '2026-08-01'\n"
        "updated: '2026-08-01'\n"
        "acl: null\n"
        "sources: []\n"
        "entity_type: organization\n"
        "aliases:\n"
        "- Kestrel\n"
        "- Ridgeline Resorts\n"
        "---\n\n"
        "# Kestrel Lodge\n\nA mountain hotel operator.\n",
        encoding="utf-8",
    )
    notes_dir = tmp_path / "wiki" / "notes"
    notes_dir.mkdir(parents=True)
    (notes_dir / "Knowledge.md").write_text(
        "---\n"
        "id: page_00000000-0000-4000-8000-000000000001\n"
        "type: note\n"
        "title: Knowledge\n"
        "status: developing\n"
        "created: '2026-08-01'\n"
        "updated: '2026-08-01'\n"
        "acl: null\n"
        "sources: []\n"
        "---\n\n"
        "# Knowledge\n\nUseful content about [[Kestrel Lodge]].\n",
        encoding="utf-8",
    )

    conn = testdb.connect_or_skip("index")
    try:
        stats = build.rebuild(conn, str(tmp_path), build_embedder("fake"))
        assert stats["pages"] == 2
        assert sorted(store.existing_paths(conn)) == [ENTITY, NOTE]

        rows = search.fetch_pages(conn, [ENTITY, NOTE])
        assert rows[ENTITY]["type"] == "entity"
        assert rows[ENTITY]["entity_type"] == "organization"
        assert rows[ENTITY]["aliases"] == ["Kestrel", "Ridgeline Resorts"]
        assert rows[ENTITY]["inlinks"] == 1
        assert rows[NOTE]["links"] == [ENTITY]
        assert rows[NOTE]["entity_type"] == "" and rows[NOTE]["aliases"] == []

        # "Ridgeline" appears in no title or body: only the alias can make the page a hit.
        fts_config = store.read_meta(conn)["fts_config"]
        assert search.fts_ranking(conn, "Ridgeline", fts_config) == [ENTITY]
    finally:
        conn.close()
