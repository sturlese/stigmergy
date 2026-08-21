"""The composed `/admin` branch, driven end to end over `httpx.ASGITransport` — real middleware
order, real static files, real Postgres underneath. The inner app is a marker so every test can
prove non-admin traffic still reaches it untouched."""
import asyncio

import httpx
import pytest
from starlette.responses import JSONResponse

from stigmergy.admin.routes import compose
from stigmergy.admin.settings import AdminSettings
from stigmergy.capture import schema as capture_schema
from stigmergy.server.settings import Settings
from tests.admin.conftest import (
    ADMIN_TOKEN,
    finish_one,
    propose_identity,
    propose_repair,
    register_entity,
    remote_registry,
    submit_one,
)


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
    # The admin token is TYPED INTO A FORM on this origin. Fly's `force_https` only REDIRECTS, so
    # without this the first request of a session can still leave the browser over http; HSTS is
    # what stops there being a first time. Added after a pre-publication audit named it.
    assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_the_shell_and_its_modules_revalidate_on_every_load(app):
    """OLD BEHAVIOUR: `/admin/` and `/admin/assets/*` carried no `cache-control` at all, so a
    browser applied heuristic freshness and kept running a deployed-over `app.js` against new
    imports for hours — a module the new bundle had renamed came back 404 and the console rendered
    as a blank page. `no-cache` makes every load an ETag round trip (a 304 when nothing moved);
    the API keeps its stricter `no-store` (the benign twin, two lines down), because a response
    there can carry captured text."""
    for path in ("/admin/", "/admin/assets/app.js", "/admin/assets/views/entities.js",
                 "/admin/assets/styles.css"):
        response = _request(app, "GET", path, token=None)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-cache", path
        assert "etag" in response.headers or "last-modified" in response.headers, path
    assert _request(app, "GET", "/admin/api/meta").headers["cache-control"] == "no-store"


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
def test_queue_flow_over_http_is_read_only(conn, app):
    """The queue is read: list, show, and the two acts on the whole queue. The drain routes
    (requeue, resolve, reject) are gone with the parks — a row is never acted on by hand."""
    ack = submit_one(conn)
    finish_one(conn, ack["id"], status=capture_schema.FAILED,
               report={"status": "failed", "summary": "failed — the librarian could not finish"})
    listed = _request(app, "GET", "/admin/api/queue").json()
    assert listed["counts"]["failed"] == 1
    shown = _request(app, "GET", f"/admin/api/queue/{ack['id']}").json()
    assert shown["status"] == "failed" and "waiting_on" not in shown
    for gone in ("requeue", "resolve", "reject"):
        assert _request(app, "POST", f"/admin/api/queue/{ack['id']}/{gone}",
                        json_body={"actor": "steward"}).status_code == 404, gone


def test_the_error_mapping_carries_the_librarys_sentences(conn, app):
    assert _request(app, "GET", "/admin/api/queue/424242").status_code == 404
    assert _request(app, "GET", "/admin/api/entities/ghost").status_code == 404
    bad = _request(app, "GET", "/admin/api/queue?status=bogus")
    assert bad.status_code == 400 and "unknown status" in bad.json()["error"]
    # `app` carries no knowledge-repo URL: a decision is refused by name as a 409, nothing written
    refused = _request(app, "POST", "/admin/api/entities/decide",
                       json_body={"actor": "steward", "item_kind": "identity-proposal",
                                  "item_id": "globex", "verdict": "approve"})
    assert refused.status_code == 409 and "STIGMERGY_LIBRARIAN_REPO_URL" in refused.json()["error"]
    bad_verdict = _request(app, "POST", "/admin/api/entities/decide",
                           json_body={"actor": "steward", "item_kind": "identity-proposal",
                                      "item_id": "globex", "verdict": "requeue"})
    assert bad_verdict.status_code == 400


def test_a_malformed_body_is_a_400_not_a_traceback(conn, app):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://localhost") as client:
            return await client.post(
                "/admin/api/entities/decide", content=b"not json",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}",
                         "Content-Type": "application/json"})
    response = asyncio.run(go())
    assert response.status_code == 400
    assert response.json() == {"error": "request body must be valid JSON"}


# ── the entities surface over HTTP ───────────────────────────────────────────────────────────
def test_entities_list_and_show_over_http(conn, app, entity_mint_repo):
    """The list carries the two proposal kinds and the registry's verdict on each identity; the
    detail route takes the entity's registry id — a string, never a capture number."""
    register_entity(entity_mint_repo, conn, "Acme Corp", aliases=["Acme Corporation"])
    propose_identity(entity_mint_repo, conn, "Acme Corporation")

    listed = _request(app, "GET", "/admin/api/entities").json()
    assert [p["id"] for p in listed["proposals"]] == ["acme-corporation"]
    assert listed["proposals"][0]["check"]["verdict"] == "registered"
    assert listed["aliases"] == []
    shown = _request(app, "GET", "/admin/api/entities/acme-corporation").json()
    assert shown["name"] == "Acme Corporation"
    assert shown["merge_candidates"] == [{"id": "acme-corp", "name": "Acme Corp"}]


def test_entities_decide_lands_over_http(conn, admin_settings, fake_gateway, entity_mint_repo,
                                         require_gitleaks):
    """The wire-level end-to-end proof: POSTing the desk's own field shape through the REAL
    `compose` product lands a merge for real and reports the commit, over HTTP."""
    app = compose(_inner, conn=conn, server_settings=Settings(librarian_repo_url=entity_mint_repo),
                  admin_settings=admin_settings, gateway=fake_gateway)
    register_entity(entity_mint_repo, conn, "Acme Corp")
    propose_identity(entity_mint_repo, conn, "Acme Corporation")

    response = _request(app, "POST", "/admin/api/entities/decide", json_body={
        "actor": "steward@example.com", "item_kind": "identity-proposal",
        "item_id": "acme-corporation", "verdict": "merge", "into": "acme-corp",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["recorded"] == "merge" and len(body["commit"]) == 40
    assert "Acme Corporation" in remote_registry(entity_mint_repo)["acme-corp"]["aliases"]
    with conn.cursor() as cur:
        cur.execute("SELECT verdict, extra FROM review_decisions WHERE item_id = %s",
                    ("acme-corporation",))
        verdict, extra = cur.fetchone()
    assert verdict == "merge" and extra["into"] == "acme-corp"


def test_entities_create_commissions_over_http(conn, admin_settings, fake_gateway):
    """ADR 042: the route queues the steward's account as a capture carrying the registration
    and answers with the row — no commit, no ledger row, the librarian does the writing."""
    from stigmergy.capture.evidence import MemoryEvidenceStore
    app = compose(_inner, conn=conn, server_settings=Settings(), admin_settings=admin_settings,
                  gateway=fake_gateway, evidence=MemoryEvidenceStore())

    response = _request(app, "POST", "/admin/api/entities/create", json_body={
        "actor": "steward@example.com", "name": "Stark Industries", "entity_type": "organization",
        "aliases": "Stark", "about": "Stark Industries is the client whose reporting we automate.",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued" and body["entity_id"] == "stark-industries"
    with conn.cursor() as cur:
        cur.execute("SELECT submitted_by, hints FROM capture_queue WHERE id = %s", (body["id"],))
        by, hints = cur.fetchone()
    assert by == "steward@example.com"
    assert capture_schema.registration_from_hints(hints).name == "Stark Industries"


def test_entities_decide_and_create_require_the_token(conn, app):
    for path, body in (("/admin/api/entities/decide",
                        {"actor": "x", "item_kind": "identity-proposal", "item_id": "globex",
                         "verdict": "approve"}),
                       ("/admin/api/entities/create",
                        {"actor": "x", "name": "Acme Corp", "entity_type": "organization"})):
        refused = _request(app, "POST", path, token=None, json_body=body)
        assert refused.status_code == 401, path
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review_decisions")
        assert cur.fetchone()[0] == 0, "an unauthorized request must never reach the door"


def test_entities_create_error_mapping_over_http(conn, app):
    bad = _request(app, "POST", "/admin/api/entities/create",
                   json_body={"actor": "steward", "name": "", "entity_type": "", "about": ""})
    assert bad.status_code == 400 and "name and entity_type and about" in bad.json()["error"]
    # `app` composes no evidence store: the refusal names what is missing, as a 409.
    refused = _request(app, "POST", "/admin/api/entities/create", json_body={
        "actor": "steward@example.com", "name": "Globex Robotics", "entity_type": "organization",
        "about": "A robotics company.",
    })
    assert refused.status_code == 409
    assert "evidence store" in refused.json()["error"]


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


# ── repairs over HTTP (ADR 039) ───────────────────────────────────────────────────────────────
def test_repairs_list_and_show_over_http(conn, app):
    proposal_id = propose_repair(conn)

    listed = _request(app, "GET", "/admin/api/repairs")
    shown = _request(app, "GET", f"/admin/api/repairs/{proposal_id}")

    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["pending"]] == [proposal_id]
    assert shown.status_code == 200
    assert shown.json()["ops"][0]["path"] == "wiki/notes/Renewals.md"
    assert _request(app, "GET", "/admin/api/repairs/999999").status_code == 404


def test_repairs_reject_requires_a_reason_and_records_it(conn, app):
    proposal_id = propose_repair(conn)

    blank = _request(app, "POST", f"/admin/api/repairs/{proposal_id}/reject",
                     json_body={"actor": "steward@example.com", "reason": "   "})
    given = _request(app, "POST", f"/admin/api/repairs/{proposal_id}/reject",
                     json_body={"actor": "steward@example.com", "reason": "already linked"})

    assert blank.status_code == 400
    assert given.status_code == 200
    with conn.cursor() as cur:
        cur.execute("SELECT status, notes FROM repair_proposals WHERE id = %s", (proposal_id,))
        assert cur.fetchone() == ("rejected", "already linked")


def test_repairs_approve_requires_the_token_and_never_reaches_the_apply_without_it(conn, app,
                                                                                   monkeypatch):
    """The benign twin lives at the service level (`test_repair_approve_applies_and_records_both_
    ledgers`); what THIS pins is that an unauthorized POST is refused by the gate, before any of it
    — no clone, no decision, no ledger row."""
    from stigmergy.repair import remote as repair_remote

    def never(*_a, **_k):
        raise AssertionError("apply_via_clone ran on an unauthorized request")

    monkeypatch.setattr(repair_remote, "apply_via_clone", never)
    proposal_id = propose_repair(conn)

    refused = _request(app, "POST", f"/admin/api/repairs/{proposal_id}/approve", token=None,
                       json_body={"actor": "mallory"})

    assert refused.status_code == 401
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM repair_proposals WHERE id = %s", (proposal_id,))
        assert cur.fetchone()[0] == "pending"
        cur.execute("SELECT count(*) FROM review_decisions")
        assert cur.fetchone()[0] == 0


# ── the two approvals that clone, and where they run ──────────────────────────────────────────
# Both Approve handlers reach code that clones a repo, runs the eight gates and pushes — seconds of
# blocking work, and `gitleaks`/`git` are subprocesses. On the event loop that stalls EVERY other
# request the process is serving, the MCP tools included, for as long as the push takes.
def _on_the_event_loop_probe(monkeypatch, method: str):
    """Replace one `AdminService` method with a probe that reports whether it was called ON the
    asyncio event loop. It answers a fact about the CALLER, so it works identically for a handler
    that awaits it directly and for one that hands it to a worker thread."""
    from stigmergy.admin.service import AdminService

    def probe(*_a, **_k):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return {"on_the_event_loop": False}
        return {"on_the_event_loop": True}

    monkeypatch.setattr(AdminService, method, probe)


@pytest.mark.parametrize("method, path, body", [
    ("repair_approve", "/admin/api/repairs/{id}/approve", {"actor": "steward@example.com"}),
    ("entity_decide", "/admin/api/entities/decide",
     {"actor": "steward@example.com", "item_kind": "identity-proposal", "item_id": "globex",
      "verdict": "approve"}),
])
def test_an_approve_that_clones_never_runs_on_the_event_loop(conn, app, monkeypatch, method, path,
                                                             body):
    """Red before the fix: both handlers awaited nothing and called the blocking service method
    inline, so the whole clone-gate-push sat on the loop and every concurrent request waited on it.

    The response SHAPE is asserted too: moving the call to a worker thread must not change what the
    route returns, or the console's own JavaScript stops reading it."""
    proposal_id = propose_repair(conn)
    _on_the_event_loop_probe(monkeypatch, method)

    response = _request(app, "POST", path.format(id=proposal_id), json_body=body)

    assert response.status_code == 200
    assert response.json() == {"on_the_event_loop": False}


def test_repairs_approve_on_an_unconfigured_deployment_is_the_409(conn, app):
    """`app` carries a default `Settings()` — no `librarian_repo_url` — so this is the deployment
    shape an operator meets before configuring one, and it must read as a refusal with the reason
    rather than a 500 naming a class."""
    proposal_id = propose_repair(conn)

    response = _request(app, "POST", f"/admin/api/repairs/{proposal_id}/approve",
                        json_body={"actor": "steward@example.com"})

    assert response.status_code == 409
    assert "STIGMERGY_LIBRARIAN_REPO_URL" in response.json()["error"]
