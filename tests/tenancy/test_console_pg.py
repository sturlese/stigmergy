"""TEN-04, TEN-09, TEN-10, TEN-11 through the real backoffice routes: the master console is the
operator's alone, an operator acts in one tenant at a time, a tenant is created with its first
administrator, and the landing page's form stores an access request and nothing more."""
import uuid
from dataclasses import dataclass

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from stigmergy.admin.routes import compose
from stigmergy.admin.schema import ensure_admin_schema
from stigmergy.server import platform, roster
from stigmergy.server.audit import ensure_audit_table
from stigmergy.server.settings import Settings
from tests import testdb
from tests.roster import seed_roster
from tests.tenancy.conftest import acting

CSRF = {"X-Stigmergy-CSRF": "1", "Origin": "http://testserver"}
OPERATOR = "operator@example.com"
ADMIN = "admin@example.com"
ROSTER = {ADMIN: {"display_name": "Admin", "groups": ["brain-admins"], "default_audience": ["public"]}}


@dataclass
class Console:
    client: TestClient
    conn: object
    operator_token: str
    admin_token: str

    def sign_in(self, token: str) -> dict:
        """The session cookie is Secure, so the plain-HTTP test client carries it by hand."""
        self.client.headers.pop("Cookie", None)
        response = self.client.post("/admin/api/session", json={"token": token}, headers=CSRF)
        assert response.status_code == 200, response.text
        self.client.headers["Cookie"] = response.headers["set-cookie"].split(";", 1)[0]
        return response.json()


def _operator_token(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO platform_operators (subject, display_name) VALUES (%s, 'Operator') "
                    "ON CONFLICT (subject) DO UPDATE SET disabled_at = NULL", (OPERATOR,))
    return platform.mint_operator_token(conn, OPERATOR, actor="test").token


@pytest.fixture()
def console(conn, twin, monkeypatch):
    monkeypatch.delenv("STIGMERGY_PUBLIC_HOST", raising=False)
    ensure_admin_schema(conn)
    ensure_audit_table(conn)
    tokens = seed_roster(conn, ROSTER, admins={ADMIN})
    with acting(conn, twin):
        seed_roster(conn, {"twin-admin@example.com": {"display_name": "Twin admin",
                                                      "groups": ["brain-admins"],
                                                      "default_audience": ["public"]}},
                    admins={"twin-admin@example.com"})
    app = compose(Starlette(), conn=conn,
                  server_settings=Settings(dsn=testdb.dsn(), embedder="fake", llm="fake"))
    with TestClient(app) as client:
        yield Console(client, conn, _operator_token(conn), tokens[ADMIN])


def test_only_an_operator_reaches_the_master_console(console):
    console.sign_in(console.admin_token)
    assert console.client.get("/admin/api/platform/tenants").status_code == 404
    assert console.client.get("/admin/api/people").status_code == 200

    me = console.sign_in(console.operator_token)
    assert me["operator"] is True and me["tenant"] is None and me["is_admin"] is False
    listed = console.client.get("/admin/api/platform/tenants")
    assert listed.status_code == 200
    assert testdb.TEST_TENANT_SLUG in {row["slug"] for row in listed.json()["tenants"]}
    # Outside every workspace the operator reaches no workspace screen.
    assert console.client.get("/admin/api/people").status_code == 404
    assert console.client.get("/admin/api/wiki/pages").status_code == 404


def test_an_operator_acts_in_one_tenant_at_a_time(console, twin):
    console.sign_in(console.operator_token)
    mine = console.client.post("/admin/api/platform/acting",
                               json={"tenant_id": testdb.TEST_TENANT}, headers=CSRF)
    assert mine.status_code == 200 and mine.json()["tenant"]["slug"] == testdb.TEST_TENANT_SLUG
    assert mine.json()["is_admin"] is True
    people = {row["subject"] for row in console.client.get("/admin/api/people").json()["people"]}
    assert people == {ADMIN}

    console.client.post("/admin/api/platform/acting", json={"tenant_id": twin}, headers=CSRF)
    people = {row["subject"] for row in console.client.get("/admin/api/people").json()["people"]}
    assert people == {"twin-admin@example.com"}
    with console.conn.cursor() as cur:
        cur.execute("SELECT action, tenant_slug FROM platform_actions WHERE actor = %s "
                    "ORDER BY id DESC LIMIT 2", (OPERATOR,))
        assert cur.fetchall() == [("tenant.act", "twin"), ("tenant.act", testdb.TEST_TENANT_SLUG)]


def test_creating_a_tenant_creates_its_first_administrator_and_shows_the_token_once(console,
                                                                                     monkeypatch):
    monkeypatch.setenv("STIGMERGY_GITHUB_ORG", "stigmergy-test")
    console.sign_in(console.operator_token)
    slug = f"t{uuid.uuid4().hex[:10]}"
    created = console.client.post("/admin/api/platform/tenants", headers=CSRF, json={
        "name": "Acme Labs", "slug": slug, "admin_email": "Founder@Acme.example",
        "admin_name": "Founder"})
    assert created.status_code == 200, created.text
    body = created.json()
    tenant = platform.tenant_by_slug(console.conn, slug)
    try:
        assert tenant.status == "provisioning" and tenant.repo == f"stigmergy-test/{slug}-brain"
        assert body["admin"] == "founder@acme.example" and body["token"]["token"].startswith("stg_")
        who, _token_id = roster.principal_for_token(console.conn, body["token"]["token"])
        assert who.tenant == tenant.id and who.administers
        again = console.client.post("/admin/api/platform/tenants", headers=CSRF, json={
            "name": "Acme Labs", "slug": slug, "admin_email": "x@acme.example", "admin_name": "X"})
        assert again.status_code == 400
    finally:
        with console.conn.cursor() as cur:
            cur.execute("UPDATE tenants SET status = 'suspended' WHERE id = %s", (tenant.id,))


def test_a_suspended_tenants_administrator_cannot_sign_in(console, twin):
    with acting(console.conn, twin):
        token = roster.issue_token(console.conn, "twin-admin@example.com", "test", actor="test").token
    console.sign_in(console.operator_token)
    suspended = console.client.put(f"/admin/api/platform/tenants/{twin}/status",
                                   json={"status": "suspended"}, headers=CSRF)
    assert suspended.status_code == 200 and suspended.json()["tenant"]["status"] == "suspended"
    operator_cookie = console.client.headers.pop("Cookie")
    refused = console.client.post("/admin/api/session", json={"token": token}, headers=CSRF)
    assert refused.status_code == 401
    console.client.headers["Cookie"] = operator_cookie
    console.client.put(f"/admin/api/platform/tenants/{twin}/status", json={"status": "active"},
                       headers=CSRF)
    console.client.headers.pop("Cookie")
    assert console.client.post("/admin/api/session", json={"token": token},
                               headers=CSRF).status_code == 200


def test_the_landing_form_stores_an_email_and_answers_alike(console):
    email = f"lead-{uuid.uuid4().hex[:8]}@example.com"
    first = console.client.post("/admin/api/access-requests", headers=CSRF,
                                json={"email": email, "company": "Acme", "note": "Meetings"})
    second = console.client.post("/admin/api/access-requests", headers=CSRF, json={"email": email})
    assert first.status_code == second.status_code == 202 and first.json() == second.json()
    assert console.client.post("/admin/api/access-requests", headers=CSRF,
                               json={"email": "not an email"}).status_code == 400
    trap = console.client.post("/admin/api/access-requests", headers=CSRF,
                               json={"email": f"bot-{email}", "website": "https://spam.example"})
    assert trap.status_code == 202
    assert console.client.post("/admin/api/access-requests", json={"email": email}).status_code == 403

    console.sign_in(console.operator_token)
    requests = console.client.get("/admin/api/platform/access-requests").json()["requests"]
    mine = [row for row in requests if row["email"] == email]
    assert len(mine) == 2 and {row["company"] for row in mine} == {"Acme", ""}
    assert not any(row["email"] == f"bot-{email}" for row in requests)
    handled = console.client.put(f"/admin/api/platform/access-requests/{mine[0]['id']}",
                                 json={"status": "handled"}, headers=CSRF)
    assert handled.status_code == 200


def test_an_operator_acting_in_a_tenant_queues_nothing_the_writer_could_not_file(console):
    """The writer finds a capture's author in the tenant's roster at filing; an operator is not
    there, so the backoffice refuses the capture instead of queueing a job bound to fail."""
    console.sign_in(console.operator_token)
    console.client.post("/admin/api/platform/acting", json={"tenant_id": testdb.TEST_TENANT},
                        headers=CSRF)
    with console.conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        before = cur.fetchone()[0]
    refused = console.client.post("/admin/api/captures/text",
                                  json={"text": "a note from the operator", "audience": ["public"]},
                                  headers=CSRF)
    assert refused.status_code == 400
    assert "operators manage workspaces" in refused.text
    with console.conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == before


def test_answered_and_year_old_access_requests_expire(console):
    platform.record_access_request(console.conn, email="fresh@example.com")
    platform.record_access_request(console.conn, email="answered@example.com")
    platform.record_access_request(console.conn, email="forgotten@example.com")
    with console.conn.cursor() as cur:
        cur.execute("UPDATE access_requests SET status = 'handled', "
                    "handled_at = now() - interval '91 days' WHERE email = 'answered@example.com'")
        cur.execute("UPDATE access_requests SET created_at = now() - interval '366 days' "
                    "WHERE email = 'forgotten@example.com'")
    platform.purge_access_requests(console.conn)
    left = {row["email"] for row in platform.list_access_requests(console.conn)}
    assert "fresh@example.com" in left
    assert not left & {"answered@example.com", "forgotten@example.com"}


def test_an_operator_queues_a_workspace_deletion_and_sees_it_in_progress(console, twin):
    console.sign_in(console.admin_token)
    assert console.client.post(f"/admin/api/platform/tenants/{twin}/erase", json={"confirm": "twin"},
                               headers=CSRF).status_code == 404
    console.sign_in(console.operator_token)
    active = console.client.post(f"/admin/api/platform/tenants/{twin}/erase",
                                 json={"confirm": "twin"}, headers=CSRF)
    assert active.status_code == 400 and "suspend" in active.text
    platform.set_tenant_status(console.conn, twin, "suspended")
    try:
        queued = console.client.post(f"/admin/api/platform/tenants/{twin}/erase",
                                     json={"confirm": "twin"}, headers=CSRF)
        assert queued.status_code == 200 and queued.json()["erasure"]["status"] == "queued"
        rows = {row["slug"]: row for row in console.client.get("/admin/api/platform/tenants").json()["tenants"]}
        assert rows["twin"]["erasure"]["status"] == "queued"
        assert rows[testdb.TEST_TENANT_SLUG]["erasure"] is None
    finally:
        with console.conn.cursor() as cur:
            cur.execute("DELETE FROM workspace_erasures WHERE workspace_id = %s", (twin,))
        platform.set_tenant_status(console.conn, twin, "active")


def test_an_operator_token_expires_and_is_refused_afterwards(console):
    import datetime as dt

    grant = platform.mint_operator_token(console.conn, OPERATOR, actor="test")
    left = dt.datetime.fromisoformat(grant.expires_at) - dt.datetime.now(dt.UTC)
    assert dt.timedelta(days=platform.OPERATOR_TOKEN_DAYS - 1) < left <= \
        dt.timedelta(days=platform.OPERATOR_TOKEN_DAYS)
    with console.conn.cursor() as cur:
        cur.execute("UPDATE platform_tokens SET expires_at = now() - interval '1 second' "
                    "WHERE id = %s", (grant.id,))
    with pytest.raises(platform.IdentityError):
        platform.operator_for_token(console.conn, grant.token)
