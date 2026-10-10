"""TEN-03, TEN-04, TEN-07, TEN-08: two tenants with byte-identical knowledge, rosters, and layer
names. Each tenant reads exactly what it would read alone; its credentials, rebuilds, pushes, and
evidence never reach the other."""
import hashlib
import hmac
import json
import uuid

import pytest

from stigmergy.capture import references
from stigmergy.index import build, store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server import platform, roster, webhook
from stigmergy.server.errors import IdentityError
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings
from tests import testdb
from tests.roster import seed_roster
from tests.server.conftest import Fixture, write_page
from tests.tenancy.conftest import acting, clear

QUERIES = ("Initech KPI revenue", "Vault Corp Quill Industries", "payroll Acme")


@pytest.fixture()
def corpus(tmp_path):
    return Fixture(str(tmp_path / "brain"))


def _service(conn, subject: str) -> BrainService:
    who = roster.principal(conn, subject)
    audiences = set(who.audiences) if who.audiences is not None else None
    return BrainService(Settings(identity=subject), conn, build_embedder("fake"), audiences,
                        identity=subject, principal=who)


def _outcome(call):
    try:
        return call()
    except Exception as error:  # noqa: BLE001 — the refusal's class is part of the answer
        return {"refused": error.__class__.__name__, "message": str(error)}


def _reader_answers(service: BrainService, corpus: Fixture) -> dict:
    return {
        "search": [_outcome(lambda q=q: service.search(q)) for q in QUERIES],
        "read": _outcome(lambda: service.read_page(corpus.OPEN_PAGE)),
        "hidden": _outcome(lambda: service.read_page(corpus.ACME_PAGE)),
        "pages": _outcome(lambda: service.list_pages()),
        "entities": _outcome(lambda: service.list_pages(type="entity")),
        "describe": _outcome(lambda: service.describe_entity("Globex")),
        "browse": _outcome(service.browse_pages),
        "graph": _outcome(service.browse_graph),
    }


def _timeless(value):
    if isinstance(value, dict):
        return {key: _timeless(item) for key, item in value.items() if key != "built_at"}
    if isinstance(value, list):
        return [_timeless(item) for item in value]
    return value


def _answers(conn, corpus: Fixture, *, timeless: bool = False) -> str:
    answers = {subject: _reader_answers(_service(conn, subject), corpus)
               for subject in corpus.IDENTITIES}
    return json.dumps(_timeless(answers) if timeless else answers, sort_keys=True, default=str)


def _index(conn, tenant: str, corpus: Fixture) -> dict[str, str]:
    with acting(conn, tenant):
        build.rebuild(conn, corpus.repo, build_embedder("fake"))
        return seed_roster(conn, corpus.IDENTITIES)


def test_a_tenant_reads_the_same_whether_or_not_its_twin_exists(conn, twin, corpus):
    _index(conn, testdb.TEST_TENANT, corpus)
    alone = _answers(conn, corpus)

    _index(conn, twin, corpus)
    assert _answers(conn, corpus) == alone          # to the instant its own index was built
    with acting(conn, twin):
        assert _answers(conn, corpus, timeless=True) == json.dumps(
            _timeless(json.loads(alone)), sort_keys=True, default=str)

    # The twin's own page reaches the twin's readers only.
    write_page(corpus.repo, "wiki/notes/twin-only.md", {"title": "Twin only"},
               "Zephyr reconciliation figures for the twin.")
    with acting(conn, twin):
        build.rebuild(conn, corpus.repo, build_embedder("fake"))
        assert "twin-only" in json.dumps(_service(conn, corpus.STEWARD).search("Zephyr reconciliation"))
    assert "twin-only" not in json.dumps(_service(conn, corpus.STEWARD).search("Zephyr reconciliation"))
    assert _answers(conn, corpus) == alone


def test_rebuilding_one_tenant_leaves_the_other_untouched(conn, twin, corpus):
    _index(conn, testdb.TEST_TENANT, corpus)
    _index(conn, twin, corpus)
    with acting(conn, twin), conn.cursor() as cur:
        cur.execute("SELECT count(*), sum(length(body)) FROM pages_index")
        before = cur.fetchone()
        cur.execute("SELECT count(*) FROM page_fragments")
        fragments = cur.fetchone()[0]
    with conn.cursor() as cur:
        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'page_fragments' "
                    "AND indexname LIKE %s", (store.audience_index(twin, "public").rsplit("_", 1)[0] + "_%",))
        twin_indexes = sorted(row[0] for row in cur.fetchall())
    assert twin_indexes

    build.rebuild(conn, corpus.repo, build_embedder("fake"))

    with acting(conn, twin), conn.cursor() as cur:
        cur.execute("SELECT count(*), sum(length(body)) FROM pages_index")
        assert cur.fetchone() == before
        cur.execute("SELECT count(*) FROM page_fragments")
        assert cur.fetchone()[0] == fragments
    with conn.cursor() as cur:
        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'page_fragments' "
                    "AND indexname LIKE %s", (store.audience_index(twin, "public").rsplit("_", 1)[0] + "_%",))
        assert sorted(row[0] for row in cur.fetchall()) == twin_indexes


def test_a_credential_names_its_own_tenant_and_a_suspended_tenant_refuses_it(conn, twin, corpus):
    mine = _index(conn, testdb.TEST_TENANT, corpus)
    theirs = _index(conn, twin, corpus)
    subject = corpus.STEWARD
    assert mine[subject] != theirs[subject]

    assert roster.principal_for_token(conn, mine[subject])[0].tenant == testdb.TEST_TENANT
    assert roster.principal_for_token(conn, theirs[subject])[0].tenant == twin
    value, session = roster.open_session(conn, theirs[subject])
    assert session.principal.tenant == twin
    assert roster.session_principal(conn, value).principal.tenant == twin

    platform.set_tenant_status(conn, twin, "suspended")
    with pytest.raises(IdentityError):
        roster.principal_for_token(conn, theirs[subject])
    with pytest.raises(IdentityError):
        roster.session_principal(conn, value)
    assert roster.principal_for_token(conn, mine[subject])[0].tenant == testdb.TEST_TENANT
    platform.set_tenant_status(conn, twin, "active")


def test_evidence_another_tenant_still_references_is_live(conn, twin):
    reference = "sha256/ab/cd/" + "e" * 64
    with acting(conn, twin), conn.cursor() as cur:
        cur.execute("INSERT INTO capture_artifacts (capture_id, position, source_path, original_ref,"
                    " readable_ref) VALUES (gen_random_uuid(), 1, 'sources/2026/10/x.md', %s, %s)",
                    (reference, reference))
    assert references.is_live(conn, reference)
    clear(conn, twin)
    assert not references.is_live(conn, reference)


def test_a_push_is_applied_to_the_tenant_whose_repository_it_names(conn, twin, corpus, monkeypatch,
                                                                    tmp_path):
    _index(conn, testdb.TEST_TENANT, corpus)
    _index(conn, twin, corpus)
    monkeypatch.setattr("stigmergy.librarian.githubapp.installation_token", lambda *_a, **_k: "t")
    path = "wiki/notes/pushed.md"
    staged = tmp_path / "staged"
    write_page(str(staged), path, {"title": "Pushed"}, "Pushed into the twin alone.")
    page = (staged / path).read_text(encoding="utf-8")
    monkeypatch.setattr(webhook, "_fetch_file_content", lambda *_a, **_k: page)
    payload = {"ref": "refs/heads/main", "before": "", "after": "f" * 40,
               "repository": {"full_name": platform.tenant_by_id(conn, twin).repo},
               "commits": [{"added": [path], "modified": [], "removed": []}], "size": 1}
    body = json.dumps(payload).encode()
    secret = "webhook-secret"
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    response = webhook._process_webhook(conn, build_embedder("fake"),
                                        webhook.WebhookSettings(secret=secret), raw_body=body,
                                        signature=signature, event="push",
                                        delivery_id=f"d-{uuid.uuid4()}")
    assert response.status_code == 200 and b'"upserted":1' in response.body
    with acting(conn, twin):
        assert path in store.existing_paths(conn)
    assert path not in store.existing_paths(conn)


def test_a_subject_in_two_tenants_resolves_only_inside_one(conn, twin):
    from stigmergy.kernel import tenancy

    shared = {"same@example.com": {"display_name": "Same", "groups": [], "default_audience": []}}
    with acting(conn, testdb.TEST_TENANT):
        seed_roster(conn, shared)
    with acting(conn, twin):
        seed_roster(conn, shared)
        assert roster.principal(conn, "same@example.com").tenant == twin
    with acting(conn, testdb.TEST_TENANT):
        assert roster.principal(conn, "same@example.com").tenant == testdb.TEST_TENANT
    for tenant in (twin, testdb.TEST_TENANT):
        with acting(conn, tenant), tenancy.platform_scope(conn):
            assert roster.principal(conn, "same@example.com").tenant == tenant
    bound = tenancy.current_tenant(conn)
    tenancy._set(conn, tenancy.TENANT_SETTING, "")
    try:
        with tenancy.platform_scope(conn), pytest.raises(tenancy.TenancyError):
            roster.principal(conn, "same@example.com")
    finally:
        tenancy.set_tenant(conn, bound)
