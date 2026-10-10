"""The entity vocabulary and `describe_entity` over entity PAGES: reader-scoped, resolved by path,
title or alias, with the linking pages as the only relation."""
import json
import os

import pytest

from stigmergy.index import build
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server.identity import resolve_audiences
from stigmergy.server.service import ENTITY_ABSENCE, NAV_CAP, BrainService
from stigmergy.server.settings import Settings
from tests.server.conftest import connect_or_skip, write_page

ACME = "wiki/entities/Acme Corp.md"
VAULT = "wiki/entities/Vault Corp.md"              # finance-scoped entity page
VAULT_SYSTEMS = "wiki/entities/Vault Systems.md"   # open; shares the alias "Vault" with Vault Corp
ACME_NEW = "wiki/notes/acme-2026.md"
ACME_OLD = "wiki/concepts/acme-2025.md"
ACME_FINANCE = "wiki/notes/acme-finance.md"
ACME_SOURCE = "sources/2026/08/50000000-0000-4000-8000-000000000001.md"
ACME_NEW_BODY = ("Current public [[Acme Corp]] knowledge. [[acme-2025]] supplies the prior "
                 "context for this update.")


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
        with open(self.identities_path, "w", encoding="utf-8") as handle:
            json.dump({
                self.STEWARD: _principal("Steward", ["brain-admins"], None),
                self.FINANCE: _principal("Finance", ["finance"], ["finance"]),
                self.ENG: _principal("Engineer", ["eng"], ["eng"]),
            }, handle)

        write_page(
            self.repo,
            ACME,
            {"title": "Acme Corp", "aliases": ["Acme"], "updated": "2026-06-01"},
            "Acme Corp is a customer organization.",
        )
        write_page(
            self.repo,
            VAULT,
            {"title": "Vault Corp", "aliases": ["Vault"], "acl": ["finance"], "updated": "2026-06-01"},
            "Vault Corp is a finance counterparty.",
        )
        write_page(
            self.repo,
            VAULT_SYSTEMS,
            {"title": "Vault Systems", "aliases": ["Vault"], "updated": "2026-06-01"},
            "Vault Systems is a public tooling vendor.",
        )
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
                "updated": "2026-06-01",
                "status": "mature",
                "sources": [ACME_SOURCE],
            },
            ACME_NEW_BODY,
        )
        write_page(
            self.repo,
            ACME_OLD,
            {
                "type": "concept",
                "title": "Acme 2025",
                "updated": "2025-01-15",
                "status": "evergreen",
            },
            "Older public [[Acme Corp]] knowledge.",
        )
        write_page(
            self.repo,
            ACME_FINANCE,
            {
                "type": "note",
                "title": "Acme finance",
                "updated": "2026-07-01",
                "status": "developing",
                "acl": ["finance"],
            },
            "Restricted [[Acme Corp]] knowledge.",
        )


@pytest.fixture(scope="module")
def entity_indexed(tmp_path_factory):
    fixture = EntityFixture(str(tmp_path_factory.mktemp("entity-read")))
    conn = connect_or_skip()
    build.rebuild(conn, fixture.repo, build_embedder("fake"))
    yield conn, fixture
    conn.close()


def _service(conn, fixture, subject):
    audiences_value = resolve_audiences(fixture.identities_path, subject)
    audiences = set(audiences_value) if audiences_value is not None else None
    settings = Settings(identity=subject)
    return BrainService(settings, conn, build_embedder("fake"), audiences, identity=subject)


class CappedEntityFixture(EntityFixture):
    def __init__(self, root):
        super().__init__(root)
        for index in range(NAV_CAP + 3):
            write_page(
                self.repo,
                f"wiki/notes/Acme confidential {index:02d}.md",
                {
                    "title": f"Acme confidential {index:02d}",
                    "updated": f"2027-05-{(index % 28) + 1:02d}",
                    "acl": ["finance"],
                },
                f"Restricted [[Acme Corp]] evidence page {index:02d} is not visible to engineering.",
            )
        for index in range(NAV_CAP + 1):
            write_page(
                self.repo,
                f"wiki/notes/Acme evidence {index:02d}.md",
                {
                    "title": f"Acme evidence {index:02d}",
                    "updated": f"2026-05-{(index % 28) + 1:02d}",
                },
                f"[[Acme Corp]] evidence page {index:02d} contains substantive visible knowledge.",
            )


@pytest.fixture()
def entity_capped_indexed(tmp_path):
    fixture = CappedEntityFixture(str(tmp_path))
    conn = connect_or_skip()
    build.rebuild(conn, fixture.repo, build_embedder("fake"))
    try:
        yield conn, fixture
    finally:
        conn.close()


def test_entity_vocabulary_projects_only_the_entity_pages_visible_to_the_reader(entity_indexed):
    conn, fixture = entity_indexed
    unrestricted = _service(conn, fixture, fixture.STEWARD).entity_vocabulary()
    finance = _service(conn, fixture, fixture.FINANCE).entity_vocabulary()["entities"]
    eng = _service(conn, fixture, fixture.ENG).entity_vocabulary()["entities"]

    assert unrestricted["count"] == 3
    assert [item["path"] for item in unrestricted["entities"]] == [ACME, VAULT, VAULT_SYSTEMS]
    assert {item["path"] for item in finance} == {ACME, VAULT, VAULT_SYSTEMS}
    assert {item["path"] for item in eng} == {ACME, VAULT_SYSTEMS}
    acme = next(item for item in eng if item["path"] == ACME)
    assert acme == {"path": ACME, "title": "Acme Corp", "entity_type": "organization",
                    "aliases": ["Acme"], "layers": ["public"]}


def test_describe_entity_is_a_dynamic_reader_scoped_projection(entity_indexed):
    conn, fixture = entity_indexed
    finance = _service(conn, fixture, fixture.FINANCE).describe_entity("Acme")
    eng = _service(conn, fixture, fixture.ENG).describe_entity("Acme")

    assert set(finance) == {
        "found", "entity", "knowledge", "knowledge_note", "knowledge_state",
        "knowledge_truncated", "knowledge_returned", "knowledge_cap", "sources",
        "sources_truncated", "sources_returned", "sources_cap",
    }
    assert finance["entity"]["path"] == ACME
    assert [item["path"] for item in finance["knowledge"]] == [
        ACME_FINANCE,
        ACME_NEW,
        ACME_OLD,
    ]
    assert [item["path"] for item in eng["knowledge"]] == [ACME_NEW, ACME_OLD]
    assert finance["sources"] == [{"path": ACME_SOURCE, "title": "Acme evidence"}]
    assert eng["sources"] == []
    item = next(item for item in eng["knowledge"] if item["path"] == ACME_NEW)
    assert item["excerpt"] == ACME_NEW_BODY
    assert item["sources"] == []
    # the entity itself is the subject, never one of its own relationships
    assert item["relationships"] == [{
        "path": ACME_OLD,
        "title": "Acme 2025",
        "statement": ACME_NEW_BODY,
        "statement_truncated": False,
    }]
    assert eng["knowledge_state"] == "available"
    assert eng["knowledge_truncated"] is False


def test_describe_entity_resolves_by_path_title_or_alias(entity_indexed):
    conn, fixture = entity_indexed
    service = _service(conn, fixture, fixture.STEWARD)
    for reference in (ACME, "Acme Corp", "acme corp", "Acme", "ACME"):
        assert service.describe_entity(reference)["entity"]["path"] == ACME, reference


def test_describe_entity_carries_the_entity_page_and_keeps_it_out_of_the_knowledge(entity_indexed):
    conn, fixture = entity_indexed
    result = _service(conn, fixture, fixture.STEWARD).describe_entity(ACME)

    assert result["entity"] == {
        "path": ACME,
        "title": "Acme Corp",
        "entity_type": "organization",
        "aliases": ["Acme"],
        "layers": ["public"],
        "status": "developing",
        "updated": "2026-06-01",
        "excerpt": "Acme Corp is a customer organization.",
        "excerpt_truncated": False,
    }
    assert all(not item["path"].startswith("wiki/entities/") for item in result["knowledge"])


def test_describe_entity_marks_a_visible_entity_page_without_linking_knowledge(entity_indexed):
    conn, fixture = entity_indexed

    result = _service(conn, fixture, fixture.FINANCE).describe_entity("Vault Corp")

    assert result["found"] is True
    assert result["entity"]["path"] == VAULT
    assert result["knowledge"] == []
    assert result["knowledge_state"] == "no_visible_knowledge"
    assert result["knowledge_truncated"] is False


def test_describe_entity_caps_only_after_visibility_filtering(entity_capped_indexed):
    conn, fixture = entity_capped_indexed

    result = _service(conn, fixture, fixture.STEWARD).describe_entity("Acme")

    assert result["knowledge_state"] == "capped"
    assert result["knowledge_truncated"] is True
    assert result["knowledge_returned"] == NAV_CAP
    assert result["knowledge_cap"] == NAV_CAP


def test_describe_entity_does_not_let_hidden_rows_consume_the_sql_evidence_cap(
    entity_capped_indexed,
):
    conn, fixture = entity_capped_indexed

    result = _service(conn, fixture, fixture.ENG).describe_entity("Acme")

    assert result["knowledge_state"] == "capped"
    assert result["knowledge_returned"] == NAV_CAP
    assert all("confidential" not in item["path"] for item in result["knowledge"])


def test_an_ambiguous_name_is_an_absence_and_ambiguity_is_judged_among_visible_pages(entity_indexed):
    """Two visible entity pages claim "Vault": the reference names neither. For a reader who can
    see only one of them, the same word is unambiguous."""
    conn, fixture = entity_indexed

    assert _service(conn, fixture, fixture.STEWARD).describe_entity("Vault") == ENTITY_ABSENCE
    assert _service(conn, fixture, fixture.FINANCE).describe_entity("Vault") == ENTITY_ABSENCE
    eng = _service(conn, fixture, fixture.ENG).describe_entity("Vault")
    assert eng["found"] is True and eng["entity"]["path"] == VAULT_SYSTEMS


def test_unknown_and_hidden_entities_share_the_absence_shape(entity_indexed):
    conn, fixture = entity_indexed
    service = _service(conn, fixture, fixture.ENG)
    by_title = service.describe_entity("Vault Corp")
    by_path = service.describe_entity(VAULT)
    unknown = service.describe_entity("does-not-exist")
    assert by_title == by_path == unknown == ENTITY_ABSENCE
    assert unknown["found"] is False
