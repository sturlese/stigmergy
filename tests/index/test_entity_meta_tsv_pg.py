"""Entity identity pages stay outside retrieval."""

from stigmergy.index import build, store
from stigmergy.index.backends.embedder import build_embedder
from tests import testdb
from tests.index.support import write_controls


def test_entity_identity_page_is_not_indexed(tmp_path):
    entity_dir = tmp_path / "wiki" / "entities"
    entity_dir.mkdir(parents=True)
    (entity_dir / "ent_00000000-0000-4000-8000-000000000001.md").write_text(
        "---\nid: ent_00000000-0000-4000-8000-000000000001\ntype: entity\n---\n"
        "# ent_00000000-0000-4000-8000-000000000001\n"
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
        "entity: []\n"
        "sources: []\n"
        "---\n\n"
        "# Knowledge\n\nUseful content\n"
    )
    write_controls(tmp_path)

    conn = testdb.connect_or_skip("index")
    try:
        stats = build.rebuild(conn, str(tmp_path), build_embedder("fake"))
        assert stats["pages"] == 1
        assert store.existing_paths(conn) == ["wiki/notes/Knowledge.md"]
    finally:
        conn.close()
