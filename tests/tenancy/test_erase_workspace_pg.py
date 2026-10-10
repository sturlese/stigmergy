"""TEN-13: deleting a workspace removes its repository, every one of its rows, its HNSW indexes, its
checkout, and the evidence only it referenced; another workspace keeps all of its own, shared
evidence included; and the slug can be used again for a workspace that starts empty."""
import urllib.error
from pathlib import Path

import pytest

from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.index import build, store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.kernel import tenancy
from stigmergy.librarian import config, provision, worker, workspaces
from stigmergy.librarian.errors import LibrarianConfigError
from stigmergy.ops import erase_workspace
from stigmergy.server import platform, usage
from tests import testdb
from tests.roster import seed_roster
from tests.server.conftest import Fixture
from tests.tenancy.conftest import acting

DOOMED = "00000000-0000-4000-8000-0000000000d0"
REBORN = "00000000-0000-4000-8000-0000000000d1"
MEMBER = {"member@example.com": {"display_name": "Member", "groups": [], "default_audience": ["public"]}}


def _count(conn, table: str, tenant: str) -> int:
    with tenancy.platform_scope(conn), conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (tenant,))
        return cur.fetchone()[0]


def _tenant_tables(conn) -> list[str]:
    with conn.cursor() as cur:
        return erase_workspace._tenant_tables(cur)


def _hnsw(conn, tenant: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'page_fragments' "
                    "AND indexname LIKE %s", (store.audience_index(tenant, "public").rsplit("_", 1)[0] + "_%",))
        return sorted(row[0] for row in cur.fetchall())


def _artifact(conn, tenant: str, ref: str) -> None:
    with acting(conn, tenant), conn.cursor() as cur:
        cur.execute("INSERT INTO capture_artifacts (capture_id, position, source_path, original_ref,"
                    " readable_ref) VALUES (gen_random_uuid(), 1, 'sources/2026/10/x.md', %s, %s)",
                    (ref, ref))


def _drop(conn, tenant: str) -> None:
    """Remove a test tenant and every row it owns."""
    with acting(conn, tenant), conn.cursor() as cur:
        for table in _tenant_tables(conn):
            cur.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant,))
        cur.execute("DELETE FROM tenants WHERE id = %s", (tenant,))


@pytest.fixture()
def doomed(conn, tmp_path):
    """A workspace with people, an index and its HNSW indexes, a filed artifact, spend, and a
    checkout, suspended and ready to delete."""
    usage.ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM workspace_erasures WHERE workspace_id = ANY(%s::uuid[])", ([DOOMED, REBORN],))
    _drop(conn, REBORN)
    _drop(conn, DOOMED)
    testdb.ensure_tenant(conn, DOOMED, "doomed")
    corpus = Fixture(str(tmp_path / "brain"))
    with acting(conn, DOOMED):
        build.rebuild(conn, corpus.repo, build_embedder("fake"))
        seed_roster(conn, MEMBER)
        usage.record(conn, operation="answer", model="m", requests=1, input_tokens=10)
    platform.set_tenant_status(conn, DOOMED, "suspended")
    yield corpus
    _drop(conn, REBORN)
    _drop(conn, DOOMED)


def _writer(conn, tmp_path, evidence, removed):
    settings = config.Settings(repo=str(tmp_path / "checkouts"), branch="main", backend="scripted",
                               garden_at="off", rebuild_at="off")
    places = workspaces.Workspaces(conn, settings, evidence)
    loop = worker.Worker(conn, places, on_output=lambda _line: None,
                         repo_remover=lambda repo: removed.append(repo) or "deleted")
    return loop, places


def test_deleting_a_workspace_removes_all_of_it_and_nothing_of_another(conn, doomed, tmp_path):
    evidence = MemoryEvidenceStore()
    own, shared = evidence.put(b"only the doomed workspace holds this"), evidence.put(b"both hold this")
    _artifact(conn, DOOMED, own)
    _artifact(conn, DOOMED, shared)
    _artifact(conn, testdb.TEST_TENANT, shared)
    with acting(conn, testdb.TEST_TENANT):
        build.rebuild(conn, doomed.repo, build_embedder("fake"))
    survivor = {table: _count(conn, table, testdb.TEST_TENANT) for table in _tenant_tables(conn)}
    survivor_hnsw = _hnsw(conn, testdb.TEST_TENANT)
    assert _hnsw(conn, DOOMED) and survivor_hnsw

    removed: list[str] = []
    loop, places = _writer(conn, tmp_path, evidence, removed)
    checkout = Path(places.path(DOOMED))
    checkout.mkdir(parents=True)
    (checkout / "README.md").write_text("old knowledge\n")
    tenant = platform.tenant_by_id(conn, DOOMED)
    erase_workspace.request(conn, tenant, confirm="doomed", actor="operator@example.com")

    assert loop._maybe_erase_workspace() is True

    assert removed == [tenant.repo]
    assert platform.tenant_by_id(conn, DOOMED) is None
    assert {table: _count(conn, table, DOOMED) for table in _tenant_tables(conn)} == \
        {table: 0 for table in _tenant_tables(conn)}
    assert _hnsw(conn, DOOMED) == []
    assert not checkout.exists()
    assert not evidence.exists(own) and evidence.exists(shared)
    assert {table: _count(conn, table, testdb.TEST_TENANT) for table in _tenant_tables(conn)} == survivor
    assert _hnsw(conn, testdb.TEST_TENANT) == survivor_hnsw

    finished = erase_workspace.latest(conn, limit=1)[0]
    assert finished["status"] == "succeeded" and finished["slug"] == "doomed"
    receipt = finished["receipt"]
    assert receipt["repository"] == "deleted" and receipt["evidence_deleted"] == 1
    assert receipt["rows"]["roster_principals"] == 1 and receipt["rows"]["pages_index"] > 0
    assert receipt["checkout_removed"] is True and receipt["search_indexes_dropped"] >= 1
    assert loop._maybe_erase_workspace() is False

    # The slug and the people's emails belong to nobody now: a new workspace starts empty.
    testdb.ensure_tenant(conn, REBORN, "doomed")
    with acting(conn, REBORN):
        seed_roster(conn, MEMBER)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pages_index")
            assert cur.fetchone()[0] == 0


def test_only_a_suspended_workspace_without_the_slack_app_can_be_deleted(conn, doomed):
    tenant = platform.tenant_by_id(conn, DOOMED)
    with pytest.raises(erase_workspace.WorkspaceErasureRefused, match="slug exactly"):
        erase_workspace.request(conn, tenant, confirm="doome", actor="op")
    platform.set_tenant_status(conn, DOOMED, "active")
    with pytest.raises(erase_workspace.WorkspaceErasureRefused, match="suspend"):
        erase_workspace.request(conn, platform.tenant_by_id(conn, DOOMED), confirm="doomed", actor="op")
    platform.set_tenant_status(conn, DOOMED, "suspended")
    owner = platform.Tenant(**{**tenant.as_json(), "status": "suspended", "slack_team_id": "T1"})
    with pytest.raises(erase_workspace.WorkspaceErasureRefused, match="Slack"):
        erase_workspace.request(conn, owner, confirm="doomed", actor="op")
    erase_workspace.request(conn, tenant, confirm="doomed", actor="op")
    with pytest.raises(erase_workspace.WorkspaceErasureRefused, match="already being deleted"):
        erase_workspace.request(conn, tenant, confirm="doomed", actor="op")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM workspace_erasures WHERE workspace_id = %s", (DOOMED,))


def test_a_deletion_that_stops_keeps_everything_and_resumes_later(conn, doomed, tmp_path):
    evidence = MemoryEvidenceStore()
    attempts: list[str] = []

    def flaky(repo):
        attempts.append(repo)
        if len(attempts) == 1:
            raise LibrarianConfigError("GitHub refused to delete it (HTTP 502)")
        return "absent"

    settings = config.Settings(repo=str(tmp_path / "checkouts"), branch="main", backend="scripted",
                               garden_at="off", rebuild_at="off")
    loop = worker.Worker(conn, workspaces.Workspaces(conn, settings, evidence),
                         on_output=lambda _line: None, repo_remover=flaky)
    erase_workspace.request(conn, platform.tenant_by_id(conn, DOOMED), confirm="doomed", actor="op")

    assert loop._maybe_erase_workspace() is False
    waiting = erase_workspace.open_requests(conn)[DOOMED]
    assert waiting["status"] == "queued" and "502" in waiting["error"]
    assert platform.tenant_by_id(conn, DOOMED) is not None
    assert _count(conn, "roster_principals", DOOMED) == 1
    assert loop._maybe_erase_workspace() is False          # not due yet

    with conn.cursor() as cur:
        cur.execute("UPDATE workspace_erasures SET next_attempt_at = now() WHERE workspace_id = %s", (DOOMED,))
    assert loop._maybe_erase_workspace() is True
    assert platform.tenant_by_id(conn, DOOMED) is None
    assert erase_workspace.latest(conn, limit=1)[0]["receipt"]["repository"] == "absent"


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b""


def test_the_remover_deletes_only_repositories_of_the_workspace_organization():
    calls = []

    def opener(request, timeout):
        calls.append((request.get_method(), request.full_url))
        if request.full_url.endswith("/gone-brain"):
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
        return _Response()

    assert provision.remove_remote("org/acme-brain", "t", org="org", opener=opener) == "deleted"
    assert provision.remove_remote("org/gone-brain", "t", org="org", opener=opener) == "absent"
    with pytest.raises(LibrarianConfigError, match="refusing to delete"):
        provision.remove_remote("someone-else/acme-brain", "t", org="org", opener=opener)
    assert calls == [("DELETE", "https://api.github.com/repos/org/acme-brain"),
                     ("DELETE", "https://api.github.com/repos/org/gone-brain")]


def test_a_workspace_resumed_after_the_request_is_never_deleted(conn, doomed, tmp_path):
    """The console refuses to resume a workspace being deleted, and the writer re-checks before it
    touches anything: a workspace made active by any other path keeps all of it."""
    from stigmergy.admin.console import ConsoleRefused, ConsoleService
    from stigmergy.server.identity import Principal

    operator = Principal(subject="op@example.com", display_name="Op", groups=frozenset(),
                         default_audience=(), is_admin=True, operator=True)
    erase_workspace.request(conn, platform.tenant_by_id(conn, DOOMED), confirm="doomed", actor="op")
    with pytest.raises(ConsoleRefused, match="being deleted"):
        ConsoleService(conn, operator=operator).set_status(DOOMED, "active")

    platform.set_tenant_status(conn, DOOMED, "active")
    removed: list[str] = []
    loop, _places = _writer(conn, tmp_path, MemoryEvidenceStore(), removed)
    assert loop._maybe_erase_workspace() is False
    assert removed == []
    assert platform.tenant_by_id(conn, DOOMED).status == "active"
    assert _count(conn, "roster_principals", DOOMED) == 1
    stopped = erase_workspace.latest(conn, limit=1)[0]
    assert stopped["status"] == "failed" and "no longer suspended" in stopped["error"]


def test_once_the_repository_is_gone_a_deletion_never_gives_up(conn, doomed):
    erase_workspace.request(conn, platform.tenant_by_id(conn, DOOMED), confirm="doomed", actor="op")
    with conn.cursor() as cur:
        cur.execute("UPDATE workspace_erasures SET repo_deleted = TRUE, attempts = %s "
                    "WHERE workspace_id = %s", (erase_workspace.MAX_ATTEMPTS, DOOMED))
    item = erase_workspace.open_requests(conn)[DOOMED]
    assert erase_workspace.fail(conn, item, RuntimeError("statement timeout")) == "queued"


def test_evidence_a_stopped_layer_erasure_still_owed_is_deleted_with_the_workspace(conn, doomed,
                                                                                  tmp_path):
    from psycopg.types.json import Jsonb

    evidence = MemoryEvidenceStore()
    owed = evidence.put(b"bytes of an erased layer the stopped erasure had not deleted yet")
    with acting(conn, DOOMED), conn.cursor() as cur:
        cur.execute("INSERT INTO layer_erasures (id, layer, requested_by, status, garbage) "
                    "VALUES (gen_random_uuid(), 'finance', 'op', 'failed', %s)", (Jsonb([owed]),))
    loop, _places = _writer(conn, tmp_path, evidence, [])
    erase_workspace.request(conn, platform.tenant_by_id(conn, DOOMED), confirm="doomed", actor="op")
    assert loop._maybe_erase_workspace() is True
    assert not evidence.exists(owed)


def test_a_repository_already_gone_counts_as_deleted(monkeypatch):
    """GitHub refuses a token scoped to a repository that no longer exists; the remover then checks
    the repository itself instead of failing for ever."""
    minted = []

    def mint(env=None, **scope):
        minted.append(scope)
        if scope.get("repositories"):
            raise LibrarianConfigError("GitHub refused an installation token (HTTP 422)")
        return "t"

    def opener(request, timeout):
        assert request.get_method() == "GET" and request.full_url.endswith("/repos/org/gone-brain")
        raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)

    monkeypatch.setattr(provision.githubapp, "installation_token", mint)
    monkeypatch.setattr(provision.githubapp, "configured", lambda env=None: True)
    remove = provision.remover_from_env({provision.ORG_ENV: "org"}, opener=opener)
    assert remove("org/gone-brain") == "absent"
    assert minted[0]["repositories"] == ["gone-brain"]


def test_a_request_retried_for_months_still_computes_its_next_attempt(conn, doomed):
    erase_workspace.request(conn, platform.tenant_by_id(conn, DOOMED), confirm="doomed", actor="op")
    with conn.cursor() as cur:
        cur.execute("UPDATE workspace_erasures SET attempts = 2000 WHERE workspace_id = %s", (DOOMED,))
    claimed = erase_workspace.claim(conn)
    assert claimed["attempts"] == 2001
    with conn.cursor() as cur:
        cur.execute("DELETE FROM workspace_erasures WHERE workspace_id = %s", (DOOMED,))


def test_the_remover_refuses_a_repository_outside_the_organization_before_asking_github(monkeypatch):
    monkeypatch.setattr(provision.githubapp, "configured", lambda env=None: True)
    monkeypatch.setattr(provision.githubapp, "installation_token",
                        lambda *a, **k: pytest.fail("no token for a repository outside the org"))
    remove = provision.remover_from_env({provision.ORG_ENV: "org"})
    with pytest.raises(LibrarianConfigError, match="refusing to delete"):
        remove("someone-else/acme-brain")
