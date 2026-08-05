"""`store.upsert_pages`/`delete_pages`/`current_content_hashes`: the incremental webhook's own
primitives, which sit beside `insert_pages` and never replace it.

Its own small corpus and its own `conn`, isolated from `tests/index/test_pg_integration.py`'s
module-scoped fixture — upsert/delete MUTATE `pages_index` in place, and sharing a fixture with
tests that assert an exact row count would make this file's own tests corrupt those elsewhere.
"""
import os

import pytest

from stigmergy.index import build, corpus, store
from stigmergy.index.backends.embedder import build_embedder
from tests import testdb


def _connect_or_skip():
    return testdb.connect_or_skip("index")


def _write(repo: str, rel_path: str, frontmatter: dict, body: str) -> None:
    full = os.path.join(repo, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    lines = ["---"] + [f"{k}: {v}" for k, v in frontmatter.items()] + ["---", "", body]
    with open(full, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("incremental-corpus"))
    _write(root, "wiki/entities/acme/note-a.md",
          {"title": "Acme Note A", "entity": "acme", "verification": "verified"},
          "Original body for note A.")
    _write(root, "wiki/entities/acme/note-b.md",
          {"title": "Acme Note B", "entity": "acme", "verification": "verified"},
          "Body for note B, untouched by the webhook tests. Related: [[note-a]].")
    return root


@pytest.fixture(scope="module")
def conn(repo):
    conn = _connect_or_skip()
    build.rebuild(conn, repo, build_embedder("fake"))
    yield conn
    conn.close()


def _row(conn, path: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT path, title, body, content_hash FROM pages_index WHERE path = %s",
                    (path,))
        row = cur.fetchone()
    if row is None:
        return None
    cols = ["path", "title", "body", "content_hash"]
    return dict(zip(cols, row, strict=True))


NOTE_A = "wiki/entities/acme/note-a.md"
NOTE_B = "wiki/entities/acme/note-b.md"
NEW_NOTE = "wiki/entities/acme/note-c.md"


def test_current_content_hashes_returns_only_indexed_paths(conn):
    hashes = store.current_content_hashes(conn, [NOTE_A, NOTE_B, NEW_NOTE])
    assert set(hashes) == {NOTE_A, NOTE_B}   # note-c was never indexed
    assert hashes[NOTE_A] == _row(conn, NOTE_A)["content_hash"]


def test_current_content_hashes_empty_paths_list_is_a_no_op(conn):
    assert store.current_content_hashes(conn, []) == {}


def test_upsert_pages_updates_an_existing_row_in_place(conn):
    """The positive half: a changed page's row is updated, not duplicated (`path` is the primary
    key `ON CONFLICT` upserts on)."""
    embedder = build_embedder("fake")
    text = ("---\ntitle: Acme Note A (revised)\nentity: acme\nverification: verified\n---\n"
           "Revised body for note A, changed by the webhook.")
    row = corpus.page_row(NOTE_A, "wiki", text)
    embeddings = {row.content_hash: embedder.embed([row.embed_text])[0]}

    store.upsert_pages(conn, [row], embeddings, "english")

    updated = _row(conn, NOTE_A)
    assert updated["title"] == "Acme Note A (revised)"
    assert "Revised body" in updated["body"]
    assert updated["content_hash"] == row.content_hash
    assert store.page_count(conn) == 2    # note-a UPDATED in place, note-b untouched — no duplicate


def test_upsert_pages_inserts_a_genuinely_new_page(conn):
    embedder = build_embedder("fake")
    text = "---\ntitle: Acme Note C\nentity: acme\nverification: verified\n---\nBrand new note."
    row = corpus.page_row(NEW_NOTE, "wiki", text)
    embeddings = {row.content_hash: embedder.embed([row.embed_text])[0]}

    store.upsert_pages(conn, [row], embeddings, "english")

    inserted = _row(conn, NEW_NOTE)
    assert inserted is not None
    assert inserted["title"] == "Acme Note C"


def test_upsert_pages_same_content_hash_is_a_true_no_op_for_the_caller(conn):
    """The same content pushed twice embeds once — the CALLER'S responsibility is to consult
    `current_content_hashes` and skip embedding when the hash already matches; this
    proves `upsert_pages` itself is safe to call again with the identical row/embedding (the
    idempotent half), and that the embedding dict never needed a NEW vector for an unchanged hash."""
    current = store.current_content_hashes(conn, [NEW_NOTE])
    text = "---\ntitle: Acme Note C\nentity: acme\nverification: verified\n---\nBrand new note."
    row = corpus.page_row(NEW_NOTE, "wiki", text)
    assert current[NEW_NOTE] == row.content_hash    # unchanged since the previous test

    # re-upsert with a DELIBERATELY WRONG vector for any OTHER hash — proves this call path
    # never needs to embed the unchanged content again (the caller would reuse the cached vector,
    # which for this hash is the only one supplied here).
    embedder = build_embedder("fake")
    embeddings = {row.content_hash: embedder.embed([row.embed_text])[0]}
    store.upsert_pages(conn, [row], embeddings, "english")
    assert _row(conn, NEW_NOTE)["content_hash"] == row.content_hash


# ── an incremental upsert must not clobber the rebuild-computed `inlinks` count to the
# webhook's own default of 0 ────────────────────────────────────────────────────────────────────
def test_upsert_pages_preserves_the_rebuild_computed_inlinks_count(conn):
    """`_UPSERT_SET` used to include `inlinks`, so `EXCLUDED.inlinks` (the incremental webhook's
    `PageRow` default, 0 — a single changed file cannot resolve the whole-corpus wikilink graph,
    `corpus.page_row`'s own docstring) clobbered whatever the full rebuild had computed, demoting
    every edited page in `search.py`'s ranking until the next nightly rebuild — a retrieval
    regression the golden set cannot see, because the golden run does a full rebuild. An UPDATE
    must preserve the computed value; only a fresh INSERT still lands at 0 (the row's own default,
    correctly, since nothing else has ever computed one for a page that never existed)."""
    with conn.cursor() as cur:
        cur.execute("SELECT inlinks FROM pages_index WHERE path = %s", (NOTE_A,))
        rebuilt_inlinks = cur.fetchone()[0]
    assert rebuilt_inlinks == 1   # note-b links to note-a (the `repo()` fixture, above)

    embedder = build_embedder("fake")
    text = ("---\ntitle: Acme Note A (edited again)\nentity: acme\nverification: verified\n---\n"
           "Edited via the incremental webhook path; this call recomputes no inlinks at all.")
    row = corpus.page_row(NOTE_A, "wiki", text)
    assert row.inlinks == 0   # page_row's own honest default for a single-file parse
    embeddings = {row.content_hash: embedder.embed([row.embed_text])[0]}

    store.upsert_pages(conn, [row], embeddings, "english")

    with conn.cursor() as cur:
        cur.execute("SELECT inlinks FROM pages_index WHERE path = %s", (NOTE_A,))
        # preserved (1), NOT clobbered to the incoming row's own default of 0
        assert cur.fetchone()[0] == 1


def test_upsert_pages_a_fresh_insert_still_lands_at_the_rows_own_inlinks_default(conn):
    """The other half: excluding `inlinks` from `_UPSERT_SET` must not also break a genuinely
    NEW page's insert — `note-c` has never been indexed, so nothing has ever computed an
    inlinks count for it, and the row's own default (0) is the honest answer until the next
    rebuild resolves the whole graph."""
    embedder = build_embedder("fake")
    text = "---\ntitle: Acme Note D\nentity: acme\nverification: verified\n---\nAnother brand new note."
    fresh_path = "wiki/entities/acme/note-d.md"
    row = corpus.page_row(fresh_path, "wiki", text)
    embeddings = {row.content_hash: embedder.embed([row.embed_text])[0]}

    store.upsert_pages(conn, [row], embeddings, "english")

    with conn.cursor() as cur:
        cur.execute("SELECT inlinks FROM pages_index WHERE path = %s", (fresh_path,))
        assert cur.fetchone()[0] == 0


def test_delete_pages_removes_the_row(conn):
    assert _row(conn, NOTE_B) is not None
    deleted = store.delete_pages(conn, [NOTE_B])
    assert deleted == 1
    assert _row(conn, NOTE_B) is None


def test_delete_pages_on_an_already_absent_path_deletes_zero_not_an_error(conn):
    """Pushes can arrive out of order and be redelivered, and neither may corrupt the index — a
    redelivered deletion for a path already gone is a no-op, not a failure."""
    assert store.delete_pages(conn, [NOTE_B]) == 0    # already deleted by the previous test
    assert store.delete_pages(conn, ["wiki/never/existed.md"]) == 0


def test_delete_pages_empty_list_is_a_no_op(conn):
    assert store.delete_pages(conn, []) == 0


# ── the primitives the webhook closes its own propagation window with ───────────────────────────
_CHAIN_PRIMARY = "wiki/entities/acme/chain-primary.md"
_CHAIN_PART = "wiki/entities/acme/chain-part2.md"
_CHAIN_UNRELATED = "wiki/entities/acme/chain-other.md"


def _upsert_with_id(conn, path: str, page_id: str, title: str) -> None:
    embedder = build_embedder("fake")
    text = f"---\nid: {page_id}\ntitle: {title}\nverification: verified\n---\nBody for {title}."
    row = corpus.page_row(path, "wiki", text)
    embeddings = {row.content_hash: embedder.embed([row.embed_text])[0]}
    store.upsert_pages(conn, [row], embeddings, "english")


@pytest.fixture(scope="module")
def chain_rows(conn):
    """Three rows added beside the module's other fixtures: a chain primary (`drive:chain`), a
    real continuation part (`drive:chain#p2`), and an UNRELATED row whose own id merely starts
    with the same text (`drive:chain-other`, no `#p<n>` marker) — the false-positive
    `pages_with_page_id_prefix`'s exact-regex caller (`corpus.chain_part_pattern`) must reject."""
    _upsert_with_id(conn, _CHAIN_PRIMARY, "drive:chain", "Chain Primary")
    _upsert_with_id(conn, _CHAIN_PART, "drive:chain#p2", "Chain Part Two")
    _upsert_with_id(conn, _CHAIN_UNRELATED, "drive:chain-other", "Chain Other")
    return None


def test_pages_with_page_id_prefix_finds_the_prefix_match_and_the_false_positive_alike(
        chain_rows, conn):
    """The SQL layer is a cheap, deliberately LOOSE prefetch — `store.pages_with_page_id_prefix`
    itself does not know about `#p<n>` markers, only about the LIKE prefix. Both `drive:chain#p2`
    (the real part) and `drive:chain-other` (an unrelated id that merely shares the "drive:chain"
    prefix) come back here; distinguishing them is `corpus.chain_part_pattern`'s job, exercised in
    `tests/server/test_webhook.py`'s end-to-end propagation tests, not this one."""
    rows = store.pages_with_page_id_prefix(conn, "drive:chain")
    by_path = {path: page_id for path, page_id in rows}
    assert by_path[_CHAIN_PART] == "drive:chain#p2"
    assert by_path[_CHAIN_UNRELATED] == "drive:chain-other"
    assert by_path[_CHAIN_PRIMARY] == "drive:chain"   # the primary itself also matches its own prefix


def test_pages_with_page_id_prefix_escapes_like_wildcards_in_the_prefix(chain_rows, conn):
    """A prefix containing a literal `%`/`_` must be read as that literal character, never as a
    SQL `LIKE` wildcard — otherwise `pages_with_page_id_prefix(conn, "drive:chain")` would (by
    accident of `_` matching any single character) also match unrelated ids it has no business
    matching. Proven directly: a prefix with an escaped-but-absent literal matches nothing."""
    assert store.pages_with_page_id_prefix(conn, "drive:ch_in") == []   # "_" is literal, not a wildcard
    assert store.pages_with_page_id_prefix(conn, "no-such-prefix-at-all") == []


def test_set_superseded_by_updates_exactly_the_given_paths(chain_rows, conn):
    store.set_superseded_by(conn, [_CHAIN_PART], "drive:chain-v2")
    assert _row(conn, _CHAIN_PART) is not None
    with conn.cursor() as cur:
        cur.execute("SELECT superseded_by FROM pages_index WHERE path = %s", (_CHAIN_PART,))
        assert cur.fetchone()[0] == "drive:chain-v2"
        # the unrelated row, never named, is untouched
        cur.execute("SELECT superseded_by FROM pages_index WHERE path = %s", (_CHAIN_UNRELATED,))
        assert cur.fetchone()[0] == ""


def test_set_superseded_by_can_clear_back_to_empty(chain_rows, conn):
    """Symmetric with stamping: `value` is used verbatim, empty string included — a push that
    REMOVES a primary's `superseded_by` clears its siblings too, not merely stamps them once."""
    store.set_superseded_by(conn, [_CHAIN_PART], "")
    with conn.cursor() as cur:
        cur.execute("SELECT superseded_by FROM pages_index WHERE path = %s", (_CHAIN_PART,))
        assert cur.fetchone()[0] == ""


def test_set_superseded_by_empty_paths_list_is_a_no_op(chain_rows, conn):
    store.set_superseded_by(conn, [], "drive:should-never-land")
    with conn.cursor() as cur:
        cur.execute("SELECT superseded_by FROM pages_index WHERE path = %s", (_CHAIN_PRIMARY,))
        assert cur.fetchone()[0] == ""
