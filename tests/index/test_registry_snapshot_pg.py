"""`ops_file_snapshot`: the derived index's cache of the knowledge repo's `ops/` control files,
and `rebuild()`'s per-file reconciliation of it — the nightly half of the two roads that keep
them fresh (issues #74 and #79; the incremental half is `server.webhook`, tested there).

Real Postgres, fake embedder, keyless. The rows live in a database every suite shares, so every
test here clears what it wrote on the way out: the server prefers a snapshot over its own file,
and a leftover changes what an unrelated suite resolves.

The index owns the BYTES and nothing else — each file's own reader owns what they mean. That is
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
    for relpath in store.OPS_FILE_RELPATHS:
        store.clear_ops_file(c, relpath)
    yield c
    for relpath in store.OPS_FILE_RELPATHS:
        store.clear_ops_file(c, relpath)
    c.close()


def _row(conn, relpath: str = REGISTRY_RELPATH) -> tuple[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT content, source FROM ops_file_snapshot WHERE relpath = %s",
                    (relpath,))
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
    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) == text


def test_a_rebuild_from_a_repo_with_no_registry_clears_a_stale_snapshot(conn, tmp_path):
    """The reconciler has to be able to say NO registry. A rebuild is the run that makes the index
    match the checkout; a snapshot left standing from a repo that no longer carries a registry is
    the same deploy-time staleness the snapshot exists to end, and it would outlive every rebuild
    forever because nothing else ever deletes this row."""
    store.write_ops_file(conn, store.ENTITY_REGISTRY_RELPATH, json.dumps(REGISTRY), "some-pushed-sha")

    build.rebuild(conn, _repo(str(tmp_path), None), build_embedder("fake"))

    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) is None


def test_a_rebuild_replaces_a_snapshot_the_webhook_wrote_rather_than_appending(conn, tmp_path):
    """One row, always: the two writers share a singleton, so the nightly reconciler overwrites
    the incremental one. A second row would be a registry chosen by whichever `SELECT` won."""
    store.write_ops_file(conn, store.ENTITY_REGISTRY_RELPATH, '{"entities": {"stale": {}}}', "4b49997aa9a7")
    text = json.dumps(REGISTRY)

    build.rebuild(conn, _repo(str(tmp_path), text), build_embedder("fake"))

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ops_file_snapshot WHERE relpath = %s",
                    (store.ENTITY_REGISTRY_RELPATH,))
        assert cur.fetchone()[0] == 1
    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) == text


def test_the_snapshot_survives_the_rebuild_that_drops_and_recreates_pages_index(conn, tmp_path):
    """`init_schema` DROPs `pages_index` and recreates it; `ops_file_snapshot` is a
    SURVIVING table like `embedding_cache` and `index_meta`, created `IF NOT EXISTS`. The
    distinction is load-bearing: were it dropped with the pages, a rebuild would blank the
    registry for every reader between the drop and the reconcile at the end of the same
    transaction."""
    text = json.dumps(REGISTRY)
    build.rebuild(conn, _repo(str(tmp_path), text), build_embedder("fake"))
    build.rebuild(conn, _repo(str(tmp_path), text), build_embedder("fake"))

    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) == text


def test_write_then_read_round_trips_bytes_that_are_not_a_registry_at_all(conn):
    """The store stores TEXT and never parses it. Garbage in, the same garbage out — and the fact
    that nothing here refuses it is exactly why `server` has to fail loudly on the way out (see
    `tests/server/test_registry_freshness_pg.py`). A store that quietly validated would move the
    contract into the package that does not own it, and would silently drop a registry whose shape
    grew a field this cache had never heard of."""
    store.write_ops_file(conn, store.ENTITY_REGISTRY_RELPATH, "{not json at all", "4b49997aa9a7")
    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) == "{not json at all"

    store.write_ops_file(conn, store.ENTITY_REGISTRY_RELPATH, "", "4b49997aa9a7")
    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) == ""        # empty TEXT, not "no snapshot"


def test_read_answers_none_on_a_database_that_never_had_the_table(conn):
    """A database whose index predates this table must behave exactly like one whose snapshot has
    not been written yet, and both exactly like the server did before any of this existed: `None`
    is the "no snapshot here, use your file" answer, never an `UndefinedTable` crash on the read
    path of every entity tool. Dropping the table is the only faithful way to reproduce that
    database — a mocked cursor would prove nothing about the probe."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS ops_file_snapshot")

    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) is None
    store.clear_ops_file(conn, store.ENTITY_REGISTRY_RELPATH)                    # also inert with no table
    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) is None

    # And a write CREATES it: the push webhook refreshes against a database whose last
    # `init_schema` may predate this table, so an incremental refresh must never wait for a
    # rebuild to have run since the upgrade.
    store.write_ops_file(conn, store.ENTITY_REGISTRY_RELPATH, json.dumps(REGISTRY), "4b49997aa9a7")
    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) == json.dumps(REGISTRY)


# ── the per-file reconcile postures (issue #79) ────────────────────────────────────────────────
IDENTITIES = {"steward@example.com": ["brain-admins"], "ana@example.com": ["finance"]}


def _repo_with_ops(root: str, files: dict[str, str]) -> str:
    repo = _repo(os.path.join(root, "seed"), None)
    for relpath, text in files.items():
        path = os.path.join(repo, *relpath.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return repo


def test_a_rebuild_caches_all_three_ops_files_when_the_checkout_carries_them(conn, tmp_path):
    files = {store.ENTITY_REGISTRY_RELPATH: json.dumps(REGISTRY),
             store.IDENTITIES_RELPATH: json.dumps(IDENTITIES),
             store.SLACK_CHANNELS_RELPATH: '{"C123": ["finance"]}'}

    stats = build.rebuild(conn, _repo_with_ops(str(tmp_path), files), build_embedder("fake"))

    for relpath, text in files.items():
        assert store.read_ops_file(conn, relpath) == text
    assert stats["ops_files"] == {rel: "written" for rel in store.OPS_FILE_RELPATHS}


def test_a_rebuild_never_clears_an_identities_snapshot_over_a_checkout_that_lacks_the_file(
        conn, tmp_path):
    """**The per-file posture, on the file where the direction matters most.** For the registry,
    clear-on-absent is the honest reconcile; for the identity roster it would be a PRIVILEGE
    RESTORATION performed by a cron — every deployed reader falls back to the copy baked at the
    last deploy, silently undoing every revocation pushed since. The snapshot stands, the stats
    say `kept`, and the log says what to do about it."""
    store.write_ops_file(conn, store.IDENTITIES_RELPATH, json.dumps(IDENTITIES), "some-sha")

    stats = build.rebuild(conn, _repo(str(tmp_path), json.dumps(REGISTRY)),
                          build_embedder("fake"))

    assert store.read_ops_file(conn, store.IDENTITIES_RELPATH) == json.dumps(IDENTITIES)
    assert stats["ops_files"][store.IDENTITIES_RELPATH] == "kept"
    assert stats["ops_files"][store.ENTITY_REGISTRY_RELPATH] == "written"


def test_a_checkout_that_never_had_an_access_file_reconciles_quietly_as_absent(conn, tmp_path):
    """The benign twin of `kept`: a deployment that simply never scoped its channels has nothing
    to keep and nothing to warn about — a nightly log that cried wolf here would teach an operator
    to ignore the one night the warning is real."""
    stats = build.rebuild(conn, _repo(str(tmp_path), json.dumps(REGISTRY)),
                          build_embedder("fake"))

    assert stats["ops_files"][store.IDENTITIES_RELPATH] == "absent"
    assert stats["ops_files"][store.SLACK_CHANNELS_RELPATH] == "absent"


def test_an_oversized_file_keeps_the_previous_snapshot_on_the_rebuild_road_too(conn, tmp_path):
    """Red before the fix, and the reviewer's exact finding: the webhook KEEPS the previous
    snapshot when a push carries an oversized file, and the rebuild CLEARED it — two writers of
    one row disagreeing about the same fault. Both now keep: the honest floor is a snapshot that
    is stale, never a fallback that resurrects the deploy-time copy."""
    good = json.dumps(REGISTRY)
    store.write_ops_file(conn, store.ENTITY_REGISTRY_RELPATH, good, "some-sha")
    oversized = '{"entities": {"pad": "' + "x" * store.MAX_OPS_FILE_BYTES + '"}}'

    stats = build.rebuild(conn, _repo(str(tmp_path), oversized), build_embedder("fake"))

    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) == good
    assert stats["ops_files"][store.ENTITY_REGISTRY_RELPATH] == "kept"


def test_a_rebuild_retires_issue_74s_single_purpose_table(conn, tmp_path):
    """The upgrade path is the rebuild, exactly as the store's contract says: the first rebuild
    on a database that still carries `entity_registry_snapshot` drops it and reconciles the new
    cache from the checkout in the same transaction. Rolling BACK stays safe — old code probes
    for its own table, finds none, and falls back to its `--entity-registry` file exactly as on a
    fresh database."""
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS entity_registry_snapshot ("
                    "singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),"
                    "content text NOT NULL, source text NOT NULL DEFAULT '',"
                    "refreshed_at timestamptz NOT NULL DEFAULT now())")
        cur.execute("INSERT INTO entity_registry_snapshot (content, source)"
                    " VALUES ('{\"entities\": {}}', 'old-road') ON CONFLICT DO NOTHING")
    text = json.dumps(REGISTRY)

    build.rebuild(conn, _repo(str(tmp_path), text), build_embedder("fake"))

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('entity_registry_snapshot')")
        assert cur.fetchone()[0] is None, "the retired table is still there"
    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) == text


def test_startup_ensure_creates_and_never_migrates(conn):
    """`ensure_ops_file_table` runs concurrently on a rolling deploy of two process groups, so it
    may CREATE and do nothing else — the concurrent-DROP race the reviewer named would abort a
    process boot. The old table's presence must not disturb it in either direction."""
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS entity_registry_snapshot ("
                    "singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),"
                    "content text NOT NULL, source text NOT NULL DEFAULT '',"
                    "refreshed_at timestamptz NOT NULL DEFAULT now())")

    store.ensure_ops_file_table(conn)
    store.ensure_ops_file_table(conn)      # idempotent, and inert about the old table

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('entity_registry_snapshot')")
        assert cur.fetchone()[0] is not None, "startup must never run the migration"
        cur.execute("SELECT to_regclass('ops_file_snapshot')")
        assert cur.fetchone()[0] is not None
        cur.execute("DROP TABLE entity_registry_snapshot")
