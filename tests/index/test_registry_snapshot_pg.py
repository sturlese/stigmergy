"""`entity_registry_snapshot`: the derived index's copy of the knowledge repo's
`ops/entity-registry.json`, and `rebuild()`'s reconciliation of it — the nightly half of the two
roads that keep it fresh (issue #74; the incremental half is `server.webhook`, tested there).

Real Postgres, fake embedder, keyless. The table is a SINGLETON row in a database every suite
shares, so every test here clears it on the way out: the server prefers this row over its
`--entity-registry` file, and a leftover changes what an unrelated suite resolves.

The index owns the BYTES and nothing else — `server.entity_aliases` owns what they mean. That is
why every assertion below is byte-level: a second interpretation of a registry down here is
exactly the drift a cache must not introduce.
"""
import json
import os

import pytest

from stigmergy.index import build, store
from stigmergy.index.backends.embedder import build_embedder
from tests import testdb

REGISTRY_RELPATH = "ops/entity-registry.json"
REGISTRY = {"entities": {"acme": {"name": "Acme Corp", "type": "organization",
                                  "aliases": ["Acme"]}}}


def _repo(root: str, registry_text: str | None) -> str:
    """A minimal indexable checkout: one page (a rebuild of an empty corpus is refused), plus an
    `ops/entity-registry.json` only when `registry_text` is given — a knowledge repo before its
    first mint genuinely has none, and that is a state, not an error."""
    repo = os.path.join(root, "repo")
    page = os.path.join(repo, "wiki", "notes", "snapshot-seed.md")
    os.makedirs(os.path.dirname(page), exist_ok=True)
    with open(page, "w", encoding="utf-8") as f:
        f.write("---\ntitle: Snapshot Seed\nentity: acme\nverification: verified\n---\nSeed body.")
    if registry_text is not None:
        os.makedirs(os.path.join(repo, "ops"), exist_ok=True)
        with open(os.path.join(repo, *REGISTRY_RELPATH.split("/")), "w", encoding="utf-8") as f:
            f.write(registry_text)
    return repo


@pytest.fixture()
def conn():
    c = testdb.connect_or_skip("index")
    store.clear_entity_registry(c)
    yield c
    store.clear_entity_registry(c)
    c.close()


def _row(conn) -> tuple[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT content, source FROM entity_registry_snapshot")
        return cur.fetchone()


def test_a_rebuild_caches_the_checkouts_registry_byte_for_byte(conn, tmp_path):
    """Byte-identical, and `source = 'rebuild'`. Byte-identical because the cache's whole job is
    to mean exactly what the file means — a re-serialization here (pretty-printing, key order, a
    dropped trailing newline) is a second interpretation of a contract this package does not own.
    `source` is operator diagnosis: it answers "which road last wrote this", the question you ask
    when a deployed entity looks stale."""
    text = json.dumps(REGISTRY, indent=2) + "\n"        # a real regenerated file's exact shape
    build.rebuild(conn, _repo(str(tmp_path), text), build_embedder("fake"))

    content, source = _row(conn)
    assert content == text
    assert source == "rebuild"
    assert store.read_entity_registry(conn) == text


def test_a_rebuild_from_a_repo_with_no_registry_clears_a_stale_snapshot(conn, tmp_path):
    """The reconciler has to be able to say NO registry. A rebuild is the run that makes the index
    match the checkout; a snapshot left standing from a repo that no longer carries a registry is
    the same deploy-time staleness the snapshot exists to end, and it would outlive every rebuild
    forever because nothing else ever deletes this row."""
    store.write_entity_registry(conn, json.dumps(REGISTRY), "some-pushed-sha")

    build.rebuild(conn, _repo(str(tmp_path), None), build_embedder("fake"))

    assert store.read_entity_registry(conn) is None


def test_a_rebuild_replaces_a_snapshot_the_webhook_wrote_rather_than_appending(conn, tmp_path):
    """One row, always: the two writers share a singleton, so the nightly reconciler overwrites
    the incremental one. A second row would be a registry chosen by whichever `SELECT` won."""
    store.write_entity_registry(conn, '{"entities": {"stale": {}}}', "4b49997aa9a7")
    text = json.dumps(REGISTRY)

    build.rebuild(conn, _repo(str(tmp_path), text), build_embedder("fake"))

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM entity_registry_snapshot")
        assert cur.fetchone()[0] == 1
    assert store.read_entity_registry(conn) == text


def test_the_snapshot_survives_the_rebuild_that_drops_and_recreates_pages_index(conn, tmp_path):
    """`init_schema` DROPs `pages_index` and recreates it; `entity_registry_snapshot` is a
    SURVIVING table like `embedding_cache` and `index_meta`, created `IF NOT EXISTS`. The
    distinction is load-bearing: were it dropped with the pages, a rebuild would blank the
    registry for every reader between the drop and the reconcile at the end of the same
    transaction."""
    text = json.dumps(REGISTRY)
    build.rebuild(conn, _repo(str(tmp_path), text), build_embedder("fake"))
    build.rebuild(conn, _repo(str(tmp_path), text), build_embedder("fake"))

    assert store.read_entity_registry(conn) == text


def test_write_then_read_round_trips_bytes_that_are_not_a_registry_at_all(conn):
    """The store stores TEXT and never parses it. Garbage in, the same garbage out — and the fact
    that nothing here refuses it is exactly why `server` has to fail loudly on the way out (see
    `tests/server/test_registry_freshness_pg.py`). A store that quietly validated would move the
    contract into the package that does not own it, and would silently drop a registry whose shape
    grew a field this cache had never heard of."""
    store.write_entity_registry(conn, "{not json at all", "4b49997aa9a7")
    assert store.read_entity_registry(conn) == "{not json at all"

    store.write_entity_registry(conn, "", "4b49997aa9a7")
    assert store.read_entity_registry(conn) == ""        # empty TEXT, not "no snapshot"


def test_read_answers_none_on_a_database_that_never_had_the_table(conn):
    """A database whose index predates this table must behave exactly like one whose snapshot has
    not been written yet, and both exactly like the server did before any of this existed: `None`
    is the "no snapshot here, use your file" answer, never an `UndefinedTable` crash on the read
    path of every entity tool. Dropping the table is the only faithful way to reproduce that
    database — a mocked cursor would prove nothing about the probe."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS entity_registry_snapshot")

    assert store.read_entity_registry(conn) is None
    store.clear_entity_registry(conn)                    # also inert with no table
    assert store.read_entity_registry(conn) is None

    # And a write CREATES it: the push webhook refreshes against a database whose last
    # `init_schema` may predate this table, so an incremental refresh must never wait for a
    # rebuild to have run since the upgrade.
    store.write_entity_registry(conn, json.dumps(REGISTRY), "4b49997aa9a7")
    assert store.read_entity_registry(conn) == json.dumps(REGISTRY)
