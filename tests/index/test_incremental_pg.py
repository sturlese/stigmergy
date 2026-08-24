import pytest

from stigmergy.index import build, corpus, store
from stigmergy.index.backends.embedder import build_embedder
from tests import testdb
from tests.index.support import write_controls

NOTE_A = "wiki/notes/Note A.md"
NOTE_B = "wiki/notes/Note B.md"
NOTE_C = "wiki/notes/Note C.md"


def _text(title: str, body: str, *, page_id: str) -> str:
    return (
        "---\n"
        f"id: {page_id}\n"
        "type: note\n"
        f"title: {title}\n"
        "status: developing\n"
        "created: '2026-08-01'\n"
        "updated: '2026-08-02'\n"
        "acl: null\n"
        "entity: []\n"
        "sources: []\n"
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )


@pytest.fixture()
def indexed(tmp_path):
    repo = tmp_path / "repo"
    folder = repo / "wiki" / "notes"
    folder.mkdir(parents=True)
    (folder / "Note A.md").write_text(
        _text("Note A", "Original body.", page_id="page_a"), encoding="utf-8"
    )
    (folder / "Note B.md").write_text(
        _text("Note B", "Related to [[Note A]].", page_id="page_b"), encoding="utf-8"
    )
    write_controls(repo)
    conn = testdb.connect_or_skip("index")
    build.rebuild(conn, str(repo), build_embedder("fake"))
    yield conn, repo
    conn.close()


def _row(conn, path: str):
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT path, title, body, content_hash, inlinks, links FROM pages_index WHERE path = %s",
            (path,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return dict(zip(("path", "title", "body", "content_hash", "inlinks", "links"), row, strict=True))


def _upsert(conn, path: str, text: str):
    row = corpus.page_row(path, "wiki", text)
    embedder = build_embedder("fake")
    store.upsert_pages(
        conn,
        [row],
        {row.content_hash: embedder.embed([row.embed_text])[0]},
        "english",
    )
    return row


def test_current_hashes_return_only_existing_paths(indexed):
    conn, _repo = indexed
    hashes = store.current_content_hashes(conn, [NOTE_A, NOTE_C])
    assert hashes == {NOTE_A: _row(conn, NOTE_A)["content_hash"]}
    assert store.current_content_hashes(conn, []) == {}


def test_upsert_updates_in_place_without_clobbering_rebuild_inlinks(indexed):
    conn, _repo = indexed
    assert _row(conn, NOTE_A)["inlinks"] == 1
    row = _upsert(conn, NOTE_A, _text("Note A", "Revised body.", page_id="page_a"))
    updated = _row(conn, NOTE_A)
    assert updated["content_hash"] == row.content_hash
    assert "Revised body" in updated["body"]
    assert updated["inlinks"] == 1
    assert store.page_count(conn) == 2


def test_upsert_inserts_a_new_page(indexed):
    conn, _repo = indexed
    _upsert(conn, NOTE_C, _text("Note C", "New body.", page_id="page_c"))
    assert _row(conn, NOTE_C)["title"] == "Note C"
    assert store.page_count(conn) == 3


def test_upsert_refreshes_resolved_outbound_links(indexed):
    conn, _repo = indexed
    row = corpus.page_row(
        NOTE_C,
        "wiki",
        _text("Note C", "Now links to [[Note A]].", page_id="page_c"),
    )
    row.links = corpus.resolve_links(
        row.path,
        row.links,
        corpus.by_stem_index(store.existing_paths(conn)),
    )
    embedder = build_embedder("fake")
    store.upsert_pages(
        conn,
        [row],
        {row.content_hash: embedder.embed([row.embed_text])[0]},
        "english",
    )
    assert _row(conn, NOTE_C)["links"] == [NOTE_A]


def test_delete_is_idempotent(indexed):
    conn, _repo = indexed
    _upsert(conn, NOTE_C, _text("Note C", "New body.", page_id="page_c"))
    assert store.delete_pages(conn, [NOTE_C]) == 1
    assert store.delete_pages(conn, [NOTE_C]) == 0
    assert _row(conn, NOTE_C) is None


def test_index_schema_contains_only_current_page_contract_columns(indexed):
    conn, _repo = indexed
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'pages_index'"
        )
        columns = {row[0] for row in cursor.fetchall()}
    assert {"path", "type", "status", "entity", "updated", "acl", "sources"} <= columns
    assert not {"owner", "tier", "as_of", "supersedes", "superseded_by"} & columns
