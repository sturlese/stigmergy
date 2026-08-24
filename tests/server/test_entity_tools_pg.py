import json
import os
from pathlib import Path

import pytest

from stigmergy.index import build
from stigmergy.index import store as index_store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server.errors import RegistryError
from stigmergy.server.identity import resolve_audiences
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings
from tests.index.support import write_controls
from tests.server.conftest import connect_or_skip, write_page

ACME_ID = "ent_10000000-0000-4000-8000-000000000001"
VAULT_ID = "ent_10000000-0000-4000-8000-000000000002"
OLD_ACME_ID = "ent_10000000-0000-4000-8000-000000000003"
ACME_NEW = "wiki/notes/acme-2026.md"
ACME_OLD = "wiki/concepts/acme-2025.md"
ACME_FINANCE = "wiki/notes/acme-finance.md"
ACME_SOURCE = "sources/2026/08/50000000-0000-4000-8000-000000000001.md"


def _claim(claim_id, value, *, kind="preferred", acl=None, at="2026-08-24T00:00:00Z"):
    return {
        "claim_id": claim_id,
        "value": value,
        "normalized": value.casefold(),
        "kind": kind,
        "acl": acl,
        "source": ACME_SOURCE,
        "actor": "steward@example.com",
        "introduced_at": at,
    }


def _entity(*claims, entity_type="organization", absorbed_ids=()):
    return {
        "entity_type": entity_type,
        "created_at": "2026-08-24T00:00:00Z",
        "updated_at": "2026-08-24T00:00:00Z",
        "claims": list(claims),
        "external_ids": [],
        "absorbed_ids": list(absorbed_ids),
    }


def _principal(name, groups, default):
    return {"display_name": name, "groups": groups, "default_audience": default}


class EntityFixture:
    STEWARD = "steward@example.com"
    FINANCE = "finance@example.com"
    ENG = "eng@example.com"

    def __init__(self, root):
        self.repo = os.path.join(root, "repo")
        ops = os.path.join(self.repo, "ops")
        os.makedirs(ops, exist_ok=True)
        self.identities_path = os.path.join(ops, "identities.json")
        self.entity_registry_path = os.path.join(ops, "entity-registry.json")

        registry = {
            "version": 1,
            "entities": {
                ACME_ID: _entity(
                    _claim("acme-name", "Acme Corp"),
                    _claim("acme-alias", "Acme", kind="alias"),
                    _claim("acme-finance-name", "Acme Capital", kind="alias", acl=["finance"]),
                    absorbed_ids=[OLD_ACME_ID],
                ),
                VAULT_ID: _entity(_claim("vault-name", "Vault Corp", acl=["finance"])),
            },
            "redirects": {OLD_ACME_ID: ACME_ID},
        }
        with open(self.entity_registry_path, "w", encoding="utf-8") as handle:
            json.dump(registry, handle)
        with open(self.identities_path, "w", encoding="utf-8") as handle:
            json.dump({
                self.STEWARD: _principal("Steward", ["brain-admins"], None),
                self.FINANCE: _principal("Finance", ["finance"], ["finance"]),
                self.ENG: _principal("Engineer", ["eng"], ["eng"]),
            }, handle)

        write_page(
            self.repo,
            ACME_SOURCE,
            {"type": "source", "title": "Acme evidence", "acl": "['finance']"},
            "Exact captured evidence.",
        )
        write_page(
            self.repo,
            ACME_NEW,
            {
                "type": "note",
                "title": "Acme 2026",
                "entity": [ACME_ID],
                "updated": "2026-06-01",
                "status": "mature",
                "sources": [ACME_SOURCE],
            },
            "Current public Acme knowledge.",
        )
        write_page(
            self.repo,
            ACME_OLD,
            {
                "type": "concept",
                "title": "Acme 2025",
                "entity": [ACME_ID],
                "updated": "2025-01-15",
                "status": "evergreen",
            },
            "Older public Acme knowledge.",
        )
        write_page(
            self.repo,
            ACME_FINANCE,
            {
                "type": "note",
                "title": "Acme finance",
                "entity": [ACME_ID],
                "updated": "2026-07-01",
                "status": "developing",
                "acl": ["finance"],
            },
            "Restricted Acme knowledge.",
        )
        write_controls(Path(self.repo))


@pytest.fixture(scope="module")
def entity_indexed(tmp_path_factory):
    fixture = EntityFixture(str(tmp_path_factory.mktemp("entity-read")))
    conn = connect_or_skip()
    build.rebuild(conn, fixture.repo, build_embedder("fake"))
    yield conn, fixture
    index_store.clear_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH)
    conn.close()


def _service(conn, fixture, subject, *, registry_path=None):
    audiences_value = resolve_audiences(fixture.identities_path, subject)
    audiences = set(audiences_value) if audiences_value is not None else None
    settings = Settings(
        identity=subject,
        identities_path=fixture.identities_path,
        entity_registry_path=registry_path or fixture.entity_registry_path,
    )
    return BrainService(
        settings,
        conn,
        build_embedder("fake"),
        audiences,
        identity=subject,
    )


def test_list_entities_projects_only_names_visible_to_the_reader(entity_indexed):
    conn, fixture = entity_indexed
    unrestricted = _service(conn, fixture, fixture.STEWARD).list_entities()["entities"]
    finance = _service(conn, fixture, fixture.FINANCE).list_entities()["entities"]
    eng = _service(conn, fixture, fixture.ENG).list_entities()["entities"]

    assert {item["id"] for item in unrestricted} == {ACME_ID, VAULT_ID}
    assert {item["id"] for item in finance} == {ACME_ID, VAULT_ID}
    assert {item["id"] for item in eng} == {ACME_ID}
    assert "Acme Capital" in next(item for item in finance if item["id"] == ACME_ID)["aliases"]
    assert "Acme Capital" not in eng[0]["aliases"]


def test_describe_entity_is_a_dynamic_reader_scoped_projection(entity_indexed):
    conn, fixture = entity_indexed
    finance = _service(conn, fixture, fixture.FINANCE).describe_entity("Acme")
    eng = _service(conn, fixture, fixture.ENG).describe_entity("Acme")

    assert set(finance) == {"found", "entity", "knowledge", "knowledge_note", "sources"}
    assert finance["entity"]["id"] == ACME_ID
    assert [item["path"] for item in finance["knowledge"]] == [
        ACME_FINANCE,
        ACME_NEW,
        ACME_OLD,
    ]
    assert [item["path"] for item in eng["knowledge"]] == [ACME_NEW, ACME_OLD]
    assert finance["sources"] == [{"path": ACME_SOURCE, "title": "Acme evidence"}]
    assert eng["sources"] == []


def test_describe_entity_has_no_stored_dossier_or_entity_page(entity_indexed):
    conn, fixture = entity_indexed
    result = _service(conn, fixture, fixture.STEWARD).describe_entity(ACME_ID)

    assert "page" not in result["entity"]
    assert all(not item["path"].startswith("wiki/entities/") for item in result["knowledge"])


def test_redirected_absorbed_id_resolves_to_the_live_entity(entity_indexed):
    conn, fixture = entity_indexed
    result = _service(conn, fixture, fixture.STEWARD).describe_entity(OLD_ACME_ID)
    assert result["entity"]["id"] == ACME_ID


def test_absorbed_ids_are_not_an_existence_oracle_for_members(entity_indexed):
    conn, fixture = entity_indexed
    service = _service(conn, fixture, fixture.ENG)

    assert service.describe_entity(OLD_ACME_ID) == service.describe_entity(
        "ent_ffffffff-ffff-4fff-8fff-ffffffffffff"
    )


def test_unknown_and_confidential_entities_share_the_absence_shape(entity_indexed):
    conn, fixture = entity_indexed
    service = _service(conn, fixture, fixture.ENG)
    confidential = service.describe_entity("Vault Corp")
    unknown = service.describe_entity("does-not-exist")
    assert confidential == unknown
    assert confidential["found"] is False


def test_malformed_registry_fails_loudly_without_exposing_its_path(entity_indexed, tmp_path):
    conn, fixture = entity_indexed
    index_store.clear_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH)
    bad = tmp_path / "registry.json"
    bad.write_text("{}", encoding="utf-8")
    service = _service(conn, fixture, fixture.STEWARD, registry_path=str(bad))

    with pytest.raises(RegistryError, match="could not be read") as error:
        service.list_entities()
    assert str(bad) not in str(error.value)
