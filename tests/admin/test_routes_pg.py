"""The composed `/admin` branch, driven end to end over `httpx.ASGITransport` — real middleware
order, real static files, real Postgres underneath. The inner app is a marker so every test can
prove non-admin traffic still reaches it untouched."""
import asyncio

import httpx
import pytest
from starlette.responses import JSONResponse

from stigmergy.admin.routes import compose
from stigmergy.admin.settings import AdminSettings
from stigmergy.server.settings import Settings
from tests.admin.conftest import ADMIN_TOKEN, park, submit_one, unresolved_entity_report


async def _inner(scope, receive, send):
    if scope["type"] == "http":
        await JSONResponse({"inner": True})(scope, receive, send)


@pytest.fixture()
def app(conn, server_settings, admin_settings, fake_gateway):
    return compose(_inner, conn=conn, server_settings=server_settings,
                   admin_settings=admin_settings, gateway=fake_gateway)


def _request(app, method, path, *, token=ADMIN_TOKEN, headers=None, json_body=None):
    async def go():
        if isinstance(headers, list):
            request_headers = list(headers)   # verbatim, duplicates included (the smuggling test)
        else:
            request_headers = dict(headers or {})
            if token is not None:
                request_headers.setdefault("Authorization", f"Bearer {token}")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://localhost") as client:
            return await client.request(method, path, headers=request_headers, json=json_body)

    return asyncio.run(go())


# ── inert until configured ────────────────────────────────────────────────────────────────────
def test_unconfigured_console_is_404_everywhere_and_inner_traffic_flows(conn, server_settings):
    app = compose(_inner, conn=conn, server_settings=server_settings,
                  admin_settings=AdminSettings())
    for path in ("/admin", "/admin/", "/admin/api/meta", "/admin/assets/styles.css"):
        assert _request(app, "GET", path).status_code == 404, path
    assert _request(app, "GET", "/anything-else").json() == {"inner": True}


def test_an_unconfigured_console_runs_no_admin_ddl(conn, server_settings):
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS admin_actions")
    compose(_inner, conn=conn, server_settings=server_settings, admin_settings=AdminSettings())
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('admin_actions')")
        assert cur.fetchone()[0] is None, "inert must mean NO DDL, not quiet DDL"
    from stigmergy.admin.schema import ensure_admin_schema
    ensure_admin_schema(conn)   # restore for the module's other tests


# ── auth ──────────────────────────────────────────────────────────────────────────────────────
def test_the_shell_is_tokenless_and_the_api_is_not(app):
    page = _request(app, "GET", "/admin/", token=None)
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert _request(app, "GET", "/admin/assets/app.js", token=None).status_code == 200
    refused = _request(app, "GET", "/admin/api/meta", token=None)
    assert refused.status_code == 401
    assert refused.json() == {"error": "unauthorized"}


def test_wrong_token_and_smuggled_headers_get_the_generic_401(app):
    assert _request(app, "GET", "/admin/api/meta", token="wrong").status_code == 401
    doubled = _request(app, "GET", "/admin/api/meta", token=None,
                       headers=[("Authorization", f"Bearer {ADMIN_TOKEN}"),
                                ("Authorization", "Bearer other")])
    assert doubled.status_code == 401


def test_the_right_token_reaches_the_handler_with_the_security_headers(app):
    response = _request(app, "GET", "/admin/api/meta")
    assert response.status_code == 200
    assert response.json()["actor_default"] == "suite-default-actor"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


def test_the_root_path_redirects_into_the_shell(app):
    assert _request(app, "GET", "/admin", token=None).status_code == 307


# ── host defense ──────────────────────────────────────────────────────────────────────────────
def test_a_foreign_host_is_421_and_the_configured_one_passes(conn, server_settings,
                                                             admin_settings, monkeypatch):
    monkeypatch.setenv("STIGMERGY_PUBLIC_HOST", "brain.example.com")
    app = compose(_inner, conn=conn, server_settings=server_settings,
                  admin_settings=admin_settings)
    foreign = _request(app, "GET", "/admin/api/meta", headers={"host": "evil.example"})
    assert foreign.status_code == 421
    configured = _request(app, "GET", "/admin/api/meta",
                          headers={"host": "brain.example.com"})
    assert configured.status_code == 200                      # the benign twin
    localhost = _request(app, "GET", "/admin/api/meta", headers={"host": "localhost:8080"})
    assert localhost.status_code == 200


# ── the queue surface over HTTP: the wire shape ───────────────────────────────────────────────
def test_queue_flow_over_http(conn, app):
    ack = submit_one(conn)
    park(conn, ack["id"])
    listed = _request(app, "GET", "/admin/api/queue").json()
    assert listed["counts"]["triage"] == 1
    shown = _request(app, "GET", f"/admin/api/queue/{ack['id']}").json()
    assert shown["status"] == "triage"
    requeued = _request(app, "POST", f"/admin/api/queue/{ack['id']}/requeue",
                        json_body={"actor": "steward", "note": "again"})
    assert requeued.status_code == 200 and requeued.json()["attempts"] == 1


def test_the_error_mapping_carries_the_librarys_sentences(conn, app):
    assert _request(app, "GET", "/admin/api/queue/424242").status_code == 404
    ack = submit_one(conn)   # queued — not parked, so a disposition is refused
    refused = _request(app, "POST", f"/admin/api/queue/{ack['id']}/reject",
                       json_body={"actor": "steward", "reason": "no"})
    assert refused.status_code == 409 and refused.json()["error"]
    bad = _request(app, "GET", "/admin/api/queue?status=bogus")
    assert bad.status_code == 400 and "unknown status" in bad.json()["error"]
    empty_reason = _request(app, "POST", f"/admin/api/queue/{ack['id']}/reject",
                            json_body={"actor": "steward", "reason": "  "})
    assert empty_reason.status_code == 400


def test_a_malformed_body_is_a_400_not_a_traceback(conn, app):
    ack = submit_one(conn)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://localhost") as client:
            return await client.post(
                f"/admin/api/queue/{ack['id']}/requeue", content=b"not json",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}",
                         "Content-Type": "application/json"})
    response = asyncio.run(go())
    assert response.status_code == 400
    assert response.json() == {"error": "request body must be valid JSON"}


# ── the entities surface over HTTP: a real Approve, mints (ADR 030) ────────────────────────────
def test_entities_approve_mints_over_http(conn, admin_settings, fake_gateway, entity_mint_repo,
                                          require_gitleaks):
    """The wire-level end-to-end proof: POSTing the form's own field shape through the REAL
    `compose` product mints for real and reports the entity + commit, over HTTP."""
    app = compose(_inner, conn=conn, server_settings=Settings(librarian_repo_url=entity_mint_repo),
                  admin_settings=admin_settings, gateway=fake_gateway)
    ack = submit_one(conn, submitted_by="steward@example.com")
    park(conn, ack["id"], report=unresolved_entity_report("Globex Robotics"))

    response = _request(app, "POST", f"/admin/api/entities/{ack['id']}/approve", json_body={
        "actor": "steward@example.com", "name": "Globex Robotics", "entity_type": "organization",
        "aliases": "Globex, Globex Robotics Inc", "requeue": True,
    })

    assert response.status_code == 200
    body = response.json()
    assert body["entity_id"] == "globex-robotics" and body["requeued"] is True
    assert len(body["commit"]) == 40
    with conn.cursor() as cur:
        cur.execute("SELECT verdict, extra FROM review_decisions WHERE item_id = %s",
                    (str(ack["id"]),))
        verdict, extra = cur.fetchone()
    assert verdict == "approve" and extra["entity_id"] == "globex-robotics"


def test_entities_approve_requires_the_token(conn, app):
    ack = submit_one(conn)
    park(conn, ack["id"], report=unresolved_entity_report("Acme Corp"))

    refused = _request(app, "POST", f"/admin/api/entities/{ack['id']}/approve", token=None,
                       json_body={"actor": "x", "name": "Acme Corp",
                                  "entity_type": "organization"})

    assert refused.status_code == 401
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review_decisions")
        assert cur.fetchone()[0] == 0, "an unauthorized request must never reach the mint"


def test_entities_approve_error_mapping_over_http(conn, app):
    """`app` (the module fixture) carries no `librarian_repo_url` — exactly the "not yet a
    console-drivable capability" shape the 409 case below needs; the 400 case never reaches that
    far at all."""
    ack = submit_one(conn, submitted_by="steward@example.com")
    park(conn, ack["id"], report=unresolved_entity_report("Globex Robotics"))

    bad = _request(app, "POST", f"/admin/api/entities/{ack['id']}/approve",
                   json_body={"actor": "steward", "name": "", "entity_type": ""})
    assert bad.status_code == 400 and "missing" in bad.json()["error"]

    not_boolean = _request(app, "POST", f"/admin/api/entities/{ack['id']}/approve", json_body={
        "actor": "steward", "name": "Globex Robotics", "entity_type": "organization",
        "requeue": "yes",
    })
    assert not_boolean.status_code == 400 and "boolean" in not_boolean.json()["error"]

    refused = _request(app, "POST", f"/admin/api/entities/{ack['id']}/approve", json_body={
        "actor": "steward@example.com", "name": "Globex Robotics", "entity_type": "organization",
    })
    assert refused.status_code == 409
    assert "STIGMERGY_LIBRARIAN_REPO_URL" in refused.json()["error"]
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review_decisions")
        assert cur.fetchone()[0] == 0


# ── crons over HTTP: the wire shape ───────────────────────────────────────────────────────────
def test_cron_dispatch_and_the_allowlist_over_http(app, fake_gateway):
    ok = _request(app, "POST", "/admin/api/crons/gardener.yml/dispatch",
                  json_body={"actor": "steward"})
    assert ok.status_code == 200
    assert ("dispatch", "gardener.yml", "main", None) in fake_gateway.calls
    refused = _request(app, "POST", "/admin/api/crons/rm-rf.yml/dispatch",
                       json_body={"actor": "steward"})
    assert refused.status_code == 400


def test_a_github_failure_is_a_502_with_the_gateways_sentence(app, fake_gateway):
    from stigmergy.admin.github import ActionsError
    fake_gateway.fail_with = ActionsError("GitHub answered 403 for PUT x", status=403)
    response = _request(app, "POST", "/admin/api/crons/gardener.yml/enable",
                        json_body={"actor": "steward"})
    assert response.status_code == 502
    assert "403" in response.json()["error"]


def test_an_unexpected_failure_names_the_class_only(conn, app, monkeypatch):
    from stigmergy.admin import service as service_module

    def boom(self):
        raise RuntimeError("secret detail that must not cross")

    monkeypatch.setattr(service_module.AdminService, "worker_status", boom)
    response = _request(app, "GET", "/admin/api/worker")
    assert response.status_code == 500
    assert response.json() == {"error": "the operation failed (RuntimeError)"}
