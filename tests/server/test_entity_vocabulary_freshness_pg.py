"""The entity vocabulary is the set of entity pages in the index: a pushed entity page is
listable, describable and linkable seconds later, without a deploy, and every tool call reads
the vocabulary fresh."""
import json
import os

import pytest

from stigmergy.index import build
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server import webhook
from stigmergy.server.service import ENTITY_ABSENCE, BrainService
from stigmergy.server.settings import Settings
from tests.server.conftest import write_page

NEXUS_ENTITY = "wiki/entities/Kestrel Nexus.md"
NOTE_PAGE = "wiki/notes/nexus-kickoff.md"
STEWARD = "steward@example.com"


class Fixture:
    def __init__(self, root):
        self.repo = os.path.join(root, "repo")
        ops = os.path.join(self.repo, "ops")
        os.makedirs(ops, exist_ok=True)
        self.identities_path = os.path.join(ops, "identities.json")
        with open(self.identities_path, "w", encoding="utf-8") as handle:
            json.dump({
                STEWARD: {
                    "display_name": "Steward",
                    "groups": ["brain-admins"],
                    "default_audience": ["public"],
                },
            }, handle)
        # The note links an entity page that does not exist yet: a dangling link until the
        # entity page is pushed, when the incremental link-graph refresh resolves it.
        write_page(
            self.repo,
            NOTE_PAGE,
            {"type": "note", "title": "Kestrel Nexus kickoff", "updated": "2026-08-17"},
            "The kickoff note about [[Kestrel Nexus]].",
        )


@pytest.fixture()
def freshness(tmp_path_factory):
    from tests import testdb

    conn = testdb.connect_or_skip("index")
    fixture = Fixture(str(tmp_path_factory.mktemp("entity-freshness")))
    build.rebuild(conn, fixture.repo, build_embedder("fake"))
    yield conn, fixture
    conn.close()


def _service(conn, fixture):
    settings = Settings(identity=STEWARD)
    return BrainService(settings, conn, build_embedder("fake"), None, identity=STEWARD)


def _entity_page_text():
    return (
        "---\n"
        "id: page_20000000-0000-4000-8000-000000000002\n"
        "type: entity\n"
        "title: Kestrel Nexus\n"
        "status: developing\n"
        "created: '2026-08-24'\n"
        "updated: '2026-08-24'\n"
        "acl: null\n"
        "sources: []\n"
        "entity_type: organization\n"
        "aliases:\n"
        "- Nexus\n"
        "---\n\n"
        "# Kestrel Nexus\n\nAn infrastructure venture.\n"
    )


@pytest.fixture(autouse=True)
def fake_installation_token(monkeypatch):
    monkeypatch.setattr(
        "stigmergy.librarian.githubapp.installation_token", lambda *args, **kwargs: "token"
    )


def _push_entity_page(conn):
    from tests.server.test_webhook import _opener

    payload = {
        "ref": "refs/heads/main",
        "before": "",
        "after": "4b49997aa9a7",
        "repository": {"full_name": "acme/knowledge"},
        "commits": [{"added": [NEXUS_ENTITY], "modified": [], "removed": []}],
    }
    return webhook.process_push(
        conn,
        build_embedder("fake"),
        payload,
        webhook.WebhookSettings(secret="s", repo="acme/knowledge", branch="main"),
        opener=_opener({NEXUS_ENTITY: _entity_page_text()}),
    )


def test_a_pushed_entity_page_is_describable_by_alias_without_a_deploy(freshness):
    conn, fixture = freshness
    service = _service(conn, fixture)
    assert service.describe_entity("Nexus") == ENTITY_ABSENCE

    stats = _push_entity_page(conn)

    assert stats["upserted"] == 1
    by_name = service.describe_entity("Kestrel Nexus")
    by_alias = service.describe_entity("Nexus")
    assert by_name["found"] is True and by_name["entity"]["path"] == NEXUS_ENTITY
    assert by_name["entity"]["entity_type"] == "organization"
    assert by_name["entity"]["aliases"] == ["Nexus"]
    assert by_alias["entity"]["path"] == NEXUS_ENTITY
    assert NEXUS_ENTITY in {entity["path"] for entity in service.entity_vocabulary()["entities"]}


def test_a_pushed_entity_page_resolves_the_links_already_pointing_at_it(freshness):
    """The note linked `[[Kestrel Nexus]]` before the page existed; the push's link-graph
    refresh turns that dangling link into the relation `describe_entity` reads."""
    conn, fixture = freshness
    _push_entity_page(conn)

    result = _service(conn, fixture).describe_entity(NEXUS_ENTITY)

    assert [item["path"] for item in result["knowledge"]] == [NOTE_PAGE]
    assert result["knowledge_state"] == "available"


def test_one_tool_call_reads_the_entity_vocabulary_once_and_the_next_call_reads_it_again(freshness):
    """Search resolves the named entity and expands its spellings from ONE vocabulary read per
    call (the memo), and a later call sees a vocabulary pushed in between (the memo is per call,
    never per process)."""
    conn, fixture = freshness

    class CountingConnection:
        def __init__(self, inner):
            self.inner = inner
            self.vocabulary_reads = 0

        def cursor(self):
            outer = self
            real = self.inner.cursor()

            class Cursor:
                def __enter__(self):
                    real.__enter__()
                    return self

                def __exit__(self, *args):
                    return real.__exit__(*args)

                def execute(self, sql, *args, **kwargs):
                    if "type = 'entity'" in sql:
                        outer.vocabulary_reads += 1
                    return real.execute(sql, *args, **kwargs)

                def __getattr__(self, name):
                    return getattr(real, name)

            return Cursor()

        def __getattr__(self, name):
            return getattr(self.inner, name)

    counting = CountingConnection(conn)
    service = BrainService(
        Settings(identity=STEWARD),
        counting, build_embedder("fake"), None, identity=STEWARD,
    )

    first = service.search("Nexus kickoff")
    assert counting.vocabulary_reads == 1
    assert not any(f.startswith("entity:") for h in first["hits"] for f in h["factors"])

    _push_entity_page(conn)

    second = service.search("Nexus kickoff")
    assert counting.vocabulary_reads == 2
    nexus = next(h for h in second["hits"] if h["path"] == NOTE_PAGE)
    assert f"entity:{NEXUS_ENTITY}" in nexus["factors"]


def test_a_full_rebuild_installs_the_committed_vocabulary(freshness):
    """The pushed page lives only in the index; the repository on disk never had it, so the
    nightly rebuild from the repository drops it again."""
    conn, fixture = freshness
    _push_entity_page(conn)
    assert _service(conn, fixture).describe_entity("Nexus")["found"] is True

    build.rebuild(conn, fixture.repo, build_embedder("fake"))

    assert _service(conn, fixture).describe_entity("Nexus") == ENTITY_ABSENCE
    assert _service(conn, fixture).entity_vocabulary() == {"count": 0, "entities": []}
