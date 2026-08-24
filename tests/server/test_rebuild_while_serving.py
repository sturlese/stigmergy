"""A live server remains available and observes completed index rebuilds."""
import asyncio
import threading
import time

from stigmergy.index import build
from stigmergy.index.backends.embedder import build_embedder
from tests import testdb
from tests.server.conftest import call_json, mcp_session, write_page

_NOVEL = "zebranovel"   # a token absent from the fixture corpus until we add a page carrying it


def test_rebuild_refreshes_the_live_server_without_restart(indexed):
    conn, fx = indexed

    async def go():
        async with mcp_session(fx, fx.STEWARD) as session:
            before = await call_json(session, "search_brain", query=f"{_NOVEL} unique topic")
            assert not any(_NOVEL in h["path"] for h in before["hits"])

            # a rebuild that ADDS a page (what `stigmergy-index --rebuild` does), server still up
            write_page(fx.repo, "wiki/notes/zebranovel-note.md",
                       {"type": "note", "title": "Zebranovel note", "entity": "initech",
                        "verification": "verified"},
                       f"A brand new note about {_NOVEL} unique topic added while serving.")
            build.rebuild(conn, fx.repo, build_embedder("fake"))

            after = await call_json(session, "search_brain", query=f"{_NOVEL} unique topic")
            assert any(_NOVEL in h["path"] for h in after["hits"])   # seen without a restart
    asyncio.run(go())


def test_searches_during_a_rebuild_do_not_error(indexed):
    _, fx = indexed
    stop = threading.Event()
    errors: list = []

    def hammer_rebuilds():
        # a SEPARATE connection (never share a psycopg conn across threads); repeatedly rebuild.
        # Through the shared seam, not `store.connect(store.dsn())`: this thread DROPs and
        # recreates `pages_index` in a loop, so it must be as unable to reach a non-test database
        # as any fixture is.
        rc = testdb.connect_or_skip("server")
        try:
            while not stop.is_set():
                build.rebuild(rc, fx.repo, build_embedder("fake"))
        except Exception as ex:  # noqa: BLE001 — surface any rebuild-side failure to the test
            errors.append(("rebuild", ex))
        finally:
            rc.close()

    async def go():
        async with mcp_session(fx, fx.STEWARD) as session:
            worker = threading.Thread(target=hammer_rebuilds, daemon=True)
            worker.start()
            try:
                deadline = time.time() + 2.0
                calls = 0
                while time.time() < deadline:
                    out = await call_json(session, "search_brain", query="quarterly revenue")
                    assert "hits" in out and "error" not in out
                    calls += 1
                assert calls > 0
            finally:
                stop.set()
                worker.join(timeout=10)
    asyncio.run(go())
    assert not errors, f"a concurrent rebuild raised while the server was reading: {errors}"
