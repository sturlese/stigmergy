import asyncio
import json
import os
from pathlib import Path

import pytest

from stigmergy.index import build, store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server import webhook
from stigmergy.server.errors import RegistryError
from stigmergy.server.mcp_server import build_mcp
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings
from tests.index.support import write_controls
from tests.server.conftest import write_page

REGISTRY_RELPATH = "ops/entity-registry.json"
COFERS_ID = "ent_20000000-0000-4000-8000-000000000001"
NEXUS_ID = "ent_20000000-0000-4000-8000-000000000002"
NOTE_PAGE = "wiki/notes/nexus-kickoff.md"
STEWARD = "steward@example.com"


def _claim(claim_id, value, kind="preferred"):
    return {
        "claim_id": claim_id,
        "value": value,
        "normalized": value.casefold(),
        "kind": kind,
        "acl": None,
        "source": "sources/2026/08/capture.md",
        "actor": STEWARD,
        "introduced_at": "2026-08-24T00:00:00Z",
    }


def _entity(entity_id, name, aliases=()):
    return {
        "entity_type": "organization",
        "created_at": "2026-08-24T00:00:00Z",
        "updated_at": "2026-08-24T00:00:00Z",
        "claims": [
            _claim(f"{entity_id}-preferred", name),
            *(_claim(f"{entity_id}-alias-{index}", alias, "alias")
              for index, alias in enumerate(aliases)),
        ],
        "external_ids": [],
        "absorbed_ids": [],
    }


def _registry(entities):
    return {"version": 1, "entities": entities, "redirects": {}}


BAKED_REGISTRY = _registry({COFERS_ID: _entity(COFERS_ID, "Cofers", ["Cofers SL"])})
PUSHED_REGISTRY = _registry({
    **BAKED_REGISTRY["entities"],
    NEXUS_ID: _entity(NEXUS_ID, "Ferrovial Nexus", ["Nexus"]),
})


class Fixture:
    def __init__(self, root):
        self.repo = os.path.join(root, "repo")
        ops = os.path.join(self.repo, "ops")
        os.makedirs(ops, exist_ok=True)
        self.baked_registry_path = os.path.join(root, "baked-entity-registry.json")
        with open(self.baked_registry_path, "w", encoding="utf-8") as handle:
            json.dump(BAKED_REGISTRY, handle)
        self.identities_path = os.path.join(ops, "identities.json")
        with open(self.identities_path, "w", encoding="utf-8") as handle:
            json.dump({
                STEWARD: {
                    "display_name": "Steward",
                    "groups": ["brain-admins"],
                    "default_audience": None,
                },
            }, handle)
        write_page(
            self.repo,
            NOTE_PAGE,
            {
                "type": "note",
                "title": "Ferrovial Nexus kickoff",
                "entity": f"['{NEXUS_ID}']",
                "as_of": "2026-08-17",
            },
            "The kickoff note anchored to Ferrovial Nexus.",
        )
        write_controls(Path(self.repo))


@pytest.fixture()
def freshness(tmp_path_factory):
    from tests import testdb

    conn = testdb.connect_or_skip("index")
    fixture = Fixture(str(tmp_path_factory.mktemp("registry-freshness")))
    build.rebuild(conn, fixture.repo, build_embedder("fake"))
    store.clear_ops_file(conn, store.ENTITY_REGISTRY_RELPATH)
    yield conn, fixture
    store.clear_ops_file(conn, store.ENTITY_REGISTRY_RELPATH)
    conn.close()


def _service(conn, fixture):
    settings = Settings(
        identity=STEWARD,
        identities_path=fixture.identities_path,
        entity_registry_path=fixture.baked_registry_path,
    )
    return BrainService(settings, conn, build_embedder("fake"), None, identity=STEWARD)


def _push(registry_text):
    return {
        "ref": "refs/heads/main",
        "before": "",
        "after": "4b49997aa9a7",
        "repository": {"full_name": "acme/knowledge"},
        "commits": [{"added": [], "modified": [REGISTRY_RELPATH], "removed": []}],
    }, {REGISTRY_RELPATH: registry_text}


def _settings():
    return webhook.WebhookSettings(secret="s", repo="acme/knowledge", branch="main")


@pytest.fixture(autouse=True)
def fake_installation_token(monkeypatch):
    monkeypatch.setattr(
        "stigmergy.librarian.githubapp.installation_token", lambda *args, **kwargs: "token"
    )


def _apply_registry_push(conn, registry):
    from tests.server.test_webhook import _opener

    payload, contents = _push(json.dumps(registry))
    return webhook.process_push(
        conn,
        build_embedder("fake"),
        payload,
        _settings(),
        opener=_opener(contents),
    )


def test_push_refreshes_names_aliases_and_types_without_a_deploy(freshness):
    conn, fixture = freshness
    stats = _apply_registry_push(conn, PUSHED_REGISTRY)

    by_name = _service(conn, fixture).describe_entity("Ferrovial Nexus")
    by_alias = _service(conn, fixture).describe_entity("Nexus")

    assert stats["registry_refreshed"] is True
    assert by_name["entity"]["id"] == NEXUS_ID
    assert by_name["entity"]["type"] == "organization"
    assert by_alias["entity"]["id"] == NEXUS_ID


def test_registry_snapshot_wins_and_file_is_the_fallback(freshness):
    conn, fixture = freshness
    before = _service(conn, fixture).describe_entity("Nexus")
    assert before["found"] is False

    _apply_registry_push(conn, PUSHED_REGISTRY)
    assert _service(conn, fixture).describe_entity("Nexus")["found"] is True

    store.clear_ops_file(conn, store.ENTITY_REGISTRY_RELPATH)
    assert _service(conn, fixture).describe_entity("Cofers SL")["entity"]["id"] == COFERS_ID
    assert _service(conn, fixture).describe_entity("Nexus")["found"] is False


def test_malformed_snapshot_fails_loudly_and_mcp_leaks_no_content(freshness):
    conn, fixture = freshness
    broken = '{"version":1,"entities":{"secret-name"'
    store.write_ops_file(conn, store.ENTITY_REGISTRY_RELPATH, broken, "sha")
    service = _service(conn, fixture)

    with pytest.raises(RegistryError):
        service.list_entities()

    blocks, _ = asyncio.run(build_mcp(service).call_tool("list_entities", {}))
    response = blocks[0].text
    assert "failed (RegistryError)" in response
    assert "secret-name" not in response
    assert fixture.baked_registry_path not in response


def test_long_lived_service_reads_a_fresh_snapshot_on_each_tool_call(freshness):
    conn, fixture = freshness
    service = _service(conn, fixture)
    assert service.describe_entity("Nexus")["found"] is False

    store.write_ops_file(
        conn,
        store.ENTITY_REGISTRY_RELPATH,
        json.dumps(PUSHED_REGISTRY),
        "sha",
    )

    assert service.describe_entity("Nexus")["entity"]["id"] == NEXUS_ID


def test_one_tool_call_reads_the_registry_snapshot_once(freshness, monkeypatch):
    conn, fixture = freshness
    store.write_ops_file(
        conn,
        store.ENTITY_REGISTRY_RELPATH,
        json.dumps(PUSHED_REGISTRY),
        "sha",
    )
    real = store.read_ops_file
    calls = []

    def counting(connection, relpath):
        if relpath == store.ENTITY_REGISTRY_RELPATH:
            calls.append(relpath)
        return real(connection, relpath)

    monkeypatch.setattr(store, "read_ops_file", counting)
    service = _service(conn, fixture)
    assert service.describe_entity("Nexus")["found"] is True
    assert len(calls) == 1
    service.describe_entity("Nexus")
    assert len(calls) == 2


def test_nightly_rebuild_installs_the_committed_empty_registry(freshness):
    conn, fixture = freshness
    store.write_ops_file(
        conn,
        store.ENTITY_REGISTRY_RELPATH,
        json.dumps(PUSHED_REGISTRY),
        "sha",
    )
    assert _service(conn, fixture).describe_entity("Nexus")["found"] is True

    build.rebuild(conn, fixture.repo, build_embedder("fake"))

    assert json.loads(store.read_ops_file(conn, store.ENTITY_REGISTRY_RELPATH)) == {
        "version": 1,
        "entities": {},
        "redirects": {},
    }
    assert _service(conn, fixture).describe_entity("Nexus")["found"] is False
