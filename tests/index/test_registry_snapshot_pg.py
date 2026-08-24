import json
from pathlib import Path

import pytest

from stigmergy.index import build, store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.index.errors import StigmergyIndexError
from tests import testdb

REGISTRY = {
    "version": 1,
    "entities": {},
    "redirects": {},
}
IDENTITIES = {
    "ana@example.com": {
        "display_name": "Ana",
        "groups": ["finance"],
        "default_audience": ["finance"],
    }
}


def _repo(root: Path, *, files: dict[str, str] | None = None, missing=()) -> str:
    repo = root / "repo"
    page = repo / "wiki" / "notes" / "Snapshot seed.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\n"
        "id: page_00000000-0000-4000-8000-000000000001\n"
        "type: note\n"
        "title: Snapshot seed\n"
        "status: developing\n"
        "created: '2026-08-01'\n"
        "updated: '2026-08-01'\n"
        "acl: null\n"
        "entity: []\n"
        "sources: []\n"
        "---\n\n"
        "# Snapshot seed\n\nSeed body.\n",
        encoding="utf-8",
    )
    controls = {
        store.ENTITY_REGISTRY_RELPATH: json.dumps(REGISTRY),
        store.IDENTITIES_RELPATH: json.dumps(IDENTITIES) + "\n",
        store.SLACK_CHANNELS_RELPATH: "{}\n",
        **(files or {}),
    }
    for relpath, text in controls.items():
        if relpath in missing:
            continue
        path = repo.joinpath(*relpath.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return str(repo)


@pytest.fixture()
def conn():
    connection = testdb.connect_or_skip("index")
    for relpath in store.OPS_FILE_RELPATHS:
        store.clear_ops_file(connection, relpath)
    yield connection
    for relpath in store.OPS_FILE_RELPATHS:
        store.clear_ops_file(connection, relpath)
    connection.close()


def _row(conn, relpath: str) -> tuple[str, str]:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT content, source FROM ops_file_snapshot WHERE relpath = %s",
            (relpath,),
        )
        return cursor.fetchone()


def test_rebuild_caches_every_control_file_byte_for_byte(conn, tmp_path):
    files = {
        store.ENTITY_REGISTRY_RELPATH: json.dumps(REGISTRY, indent=2) + "\n",
        store.IDENTITIES_RELPATH: json.dumps(IDENTITIES, separators=(",", ":")) + "\n",
        store.SLACK_CHANNELS_RELPATH: '{"C123":["finance"]}\n',
    }

    stats = build.rebuild(
        conn,
        _repo(tmp_path, files=files),
        build_embedder("fake"),
    )

    for relpath, text in files.items():
        assert _row(conn, relpath) == (text, "rebuild")
        assert store.read_ops_file(conn, relpath) == text
    assert stats["ops_files"] == {
        relpath: "written" for relpath in store.OPS_FILE_RELPATHS
    }


def test_rebuild_replaces_an_incremental_control_snapshot(conn, tmp_path):
    store.write_ops_file(
        conn,
        store.ENTITY_REGISTRY_RELPATH,
        '{"entities":{"stale":{}}}',
        "previous-push",
    )
    text = json.dumps(REGISTRY)

    build.rebuild(
        conn,
        _repo(tmp_path, files={store.ENTITY_REGISTRY_RELPATH: text}),
        build_embedder("fake"),
    )

    assert _row(conn, store.ENTITY_REGISTRY_RELPATH) == (text, "rebuild")


def test_control_snapshots_survive_page_index_replacement(conn, tmp_path):
    repo = _repo(tmp_path)

    build.rebuild(conn, repo, build_embedder("fake"))
    build.rebuild(conn, repo, build_embedder("fake"))

    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) == json.dumps(REGISTRY)


def test_control_store_round_trips_unparsed_bytes(conn):
    content = "{not json at all"
    store.write_ops_file(conn, store.ENTITY_REGISTRY_RELPATH, content, "push")

    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) == content


def test_control_store_handles_an_uninitialized_database(conn):
    with conn.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS ops_file_snapshot")

    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) is None
    assert store.clear_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) is False

    store.write_ops_file(conn, store.ENTITY_REGISTRY_RELPATH, "{}\n", "push")
    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) == "{}\n"


def test_rebuild_refuses_a_missing_control_without_replacing_snapshots(conn, tmp_path):
    previous = '{"ana@example.com":{"groups":["finance"]}}'
    store.write_ops_file(conn, store.IDENTITIES_RELPATH, previous, "previous-push")
    repo = _repo(tmp_path, missing=(store.IDENTITIES_RELPATH,))

    with pytest.raises(StigmergyIndexError, match=store.IDENTITIES_RELPATH):
        build.rebuild(conn, repo, build_embedder("fake"))

    assert store.read_ops_file(conn, store.IDENTITIES_RELPATH) == previous


def test_rebuild_refuses_an_oversized_control_without_replacing_snapshots(conn, tmp_path):
    previous = json.dumps(REGISTRY)
    store.write_ops_file(conn, store.ENTITY_REGISTRY_RELPATH, previous, "previous-push")
    oversized = "x" * (store.MAX_OPS_FILE_BYTES + 1)
    repo = _repo(
        tmp_path,
        files={store.ENTITY_REGISTRY_RELPATH: oversized},
    )

    with pytest.raises(StigmergyIndexError, match="size limit"):
        build.rebuild(conn, repo, build_embedder("fake"))

    assert store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH) == previous
