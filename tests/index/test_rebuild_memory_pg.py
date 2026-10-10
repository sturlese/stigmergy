"""A full rebuild over a warm embedding cache copies the cached vectors inside Postgres: a workspace
of a few thousand fragments at production's width must not hold them all in the writer's memory."""
import dataclasses
import hashlib
import json
import tracemalloc

import pytest

from stigmergy.index import build, corpus, fragments, search, store
from stigmergy.index.backends.embedder import DEFAULT_DIMENSIONS
from stigmergy.index.errors import StigmergyIndexError
from tests import testdb
from tests.server.conftest import write_page

PAGES = 120
DIM = DEFAULT_DIMENSIONS


class WideEmbedder:
    model, host, dim = f"fake-wide-{DIM}", "", DIM

    def __init__(self):
        self.calls = 0

    def embed(self, texts, **_kwargs):
        self.calls += 1
        out = []
        for text in texts:
            seed = hashlib.sha256(text.encode("utf-8")).digest()
            out.append([(seed[i % 32] + i) % 251 / 251 - 0.5 for i in range(DIM)])
        return out


def test_a_rebuild_over_a_warm_cache_keeps_the_cached_vectors_in_postgres(tmp_path):
    repo = str(tmp_path / "repo")
    for index in range(PAGES):
        write_page(repo, f"wiki/notes/Note {index}.md", {"title": f"Note {index}"},
                   f"# Note {index}\n\nThe pilot at site {index} closed its review {index % 7}.")
    conn = testdb.connect_or_skip("index")
    try:
        embedder = WideEmbedder()
        build.rebuild(conn, repo, embedder)   # warms the cache; the database may already hold it
        before = search.search(conn, "pilot site 42 review", embedder=embedder, k=5)
        calls = embedder.calls

        tracemalloc.start()
        try:
            warm = build.rebuild(conn, repo, embedder)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        assert (warm["embedded"], warm["cached"], embedder.calls) == (0, PAGES, calls)
        # Fewer bytes than one double per cached vector element: the vectors never came through.
        assert peak < PAGES * DIM * 8
        after = search.search(conn, "pilot site 42 review", embedder=embedder, k=5)
        assert [(hit["path"], hit["score"]) for hit in after] == [(hit["path"], hit["score"]) for hit in before]
        assert store.read_meta(conn)["dim"] == DIM

        # The copied vector is the one the embedder returned, exactly as a direct write stores it.
        note = next(row for row in corpus.load_pages(repo) if row.path == "wiki/notes/Note 7.md")
        piece = fragments.page_fragments([note])[0]
        with conn.cursor() as cur:
            cur.execute("SELECT embedding = %s::halfvec FROM page_fragments WHERE path = %s",
                        (json.dumps(embedder.embed([piece.embed_text])[0]), note.path))
            assert cur.fetchone() == (True,)

        # A fragment whose vector was never cached fails the write and keeps the page's fragments.
        uncached = dataclasses.replace(piece, content_hash="0" * 64)
        with pytest.raises(StigmergyIndexError, match="no cached"):
            store.replace_fragments(conn, [note.path], [uncached], embedder.model)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM page_fragments WHERE path = %s", (note.path,))
            assert cur.fetchone() == (1,)
    finally:
        conn.close()
