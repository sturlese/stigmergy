"""The composed `/admin` branch, driven end to end over `httpx.ASGITransport` — real middleware
order, real static files, real Postgres underneath. The inner app is a marker so every test can
prove non-admin traffic still reaches it untouched."""
import asyncio

import httpx
import pytest
from starlette.responses import JSONResponse

from stigmergy.admin.routes import compose
from stigmergy.admin.settings import AdminSettings
from stigmergy.capture import ops
from stigmergy.capture import schema as capture_schema
from stigmergy.gardener.schema import JOB_NAME as GARDENER_JOB
from stigmergy.server.settings import Settings
from tests.admin.conftest import (
    ADMIN_TOKEN,
    finish_one,
    landed_repair,
    submit_one,
)


async def _inner(scope, receive, send):
    if scope["type"] == "http":
        await JSONResponse({"inner": True})(scope, receive, send)


@pytest.fixture()
def app(conn, server_settings, admin_settings):
    return compose(_inner, conn=conn, server_settings=server_settings,
                   admin_settings=admin_settings)


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
    assert _request(app, "GET", "/admin/api/repairs/424242").status_code == 404
    bad = _request(app, "GET", "/admin/api/queue?status=bogus")
    assert bad.status_code == 400 and "unknown status" in bad.json()["error"]
    # `app` carries no evidence store: a removal is refused by name as a 409, nothing queued
    refused = _request(app, "POST", "/admin/api/pages/delete",
                       json_body={"actor": "steward", "paths": ["wiki/notes/Old.md"],
                                  "why": "superseded"})
    assert refused.status_code == 409 and "evidence store" in refused.json()["error"]
    no_paths = _request(app, "POST", "/admin/api/pages/delete",
                        json_body={"actor": "steward", "paths": [], "why": "superseded"})
    assert no_paths.status_code == 400


def test_a_malformed_body_is_a_400_not_a_traceback(conn, app):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://localhost") as client:
            return await client.post(
                "/admin/api/pages/delete", content=b"not json",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}",
                         "Content-Type": "application/json"})
    response = asyncio.run(go())
    assert response.status_code == 400
    assert response.json() == {"error": "request body must be valid JSON"}


def test_entities_create_commissions_over_http(conn, admin_settings):
    """ADR 042: the route queues the steward's account as a capture carrying the registration
    and answers with the row — no commit, no ledger row, the librarian does the writing."""
    from stigmergy.capture.evidence import MemoryEvidenceStore
    app = compose(_inner, conn=conn, server_settings=Settings(), admin_settings=admin_settings,
                  evidence=MemoryEvidenceStore())

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


def test_entities_create_requires_the_token(conn, app):
    refused = _request(app, "POST", "/admin/api/entities/create", token=None,
                       json_body={"actor": "x", "name": "Acme Corp",
                                  "entity_type": "organization"})
    assert refused.status_code == 401
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
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


# ── the night shift over HTTP: the wire shape ─────────────────────────────────────────────────
def test_the_jobs_endpoint_is_read_only_over_http(app, conn):
    """One GET, and nothing else. The three POSTs this page used to expose — dispatch, enable,
    disable — are gone with the crons themselves (ADR 044), so the assertion is not only that the
    read works but that the writes are NOT routed: a console that still accepted a dispatch would
    be accepting it for a workflow file that no longer exists anywhere."""
    ops.record_job_run(conn, GARDENER_JOB, status="ok", stats={"findings": 1})
    ok = _request(app, "GET", "/admin/api/jobs")
    assert ok.status_code == 200
    files = [job["file"] for job in ok.json()["jobs"]]
    assert files == ["gardener", "retention-purge", "index-rebuild"]
    for path in ("/admin/api/jobs/gardener/dispatch", "/admin/api/crons/gardener.yml/dispatch",
                 "/admin/api/crons/gardener.yml/enable", "/admin/api/crons/gardener.yml/disable"):
        gone = _request(app, "POST", path, json_body={"actor": "steward"})
        assert gone.status_code == 404, f"{path} still answers — a cron lever survived the removal"


def test_an_unexpected_failure_names_the_class_only(conn, app, monkeypatch):
    from stigmergy.admin import service as service_module

    def boom(self):
        raise RuntimeError("secret detail that must not cross")

    monkeypatch.setattr(service_module.AdminService, "worker_status", boom)
    response = _request(app, "GET", "/admin/api/worker")
    assert response.status_code == 500
    assert response.json() == {"error": "the operation failed (RuntimeError)"}


# ── repairs over HTTP: a read-only page (ADR 044) ─────────────────────────────────────────────
def test_repairs_list_and_show_over_http(conn, app):
    """The two routes that survive, and there are no others: nothing on this page decides anything
    any more, so the console reads what the worker already did."""
    repair_id = landed_repair(conn)

    listed = _request(app, "GET", "/admin/api/repairs")
    shown = _request(app, "GET", f"/admin/api/repairs/{repair_id}")

    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["recent"]] == [repair_id]
    assert shown.status_code == 200
    assert shown.json()["ops"][0]["path"] == "wiki/notes/Renewals.md"
    assert shown.json()["diff"].startswith("diff --git"), (
        "the diff is the whole reason this route exists — nobody read the change before it landed")
    assert _request(app, "GET", "/admin/api/repairs/999999").status_code == 404


@pytest.mark.parametrize("verb, path", [
    ("POST", "/admin/api/repairs/{id}/approve"),
    ("POST", "/admin/api/repairs/{id}/reject"),
])
def test_the_doors_that_decided_a_repair_are_gone_from_the_router(conn, app, verb, path):
    """Asked of the ROUTER rather than of the code that used to be behind it. A repair is applied
    by the worker without anybody being asked (ADR 044), so a console still offering Approve and
    Decline would be offering a decision that changes nothing — and a route left mapped to a
    handler nobody calls is how one comes back."""
    repair_id = landed_repair(conn)

    response = _request(app, verb, path.format(id=repair_id),
                        json_body={"actor": "steward@example.com", "reason": "no"})

    assert response.status_code == 404


# ── the handler that clones, and where it runs ────────────────────────────────────────────────
# Removing pages reaches code that clones a repo, runs the nine gates and pushes — seconds of
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


def test_the_deletion_that_clones_never_runs_on_the_event_loop(conn, app, monkeypatch):
    """Red before the fix: the handler awaited nothing and called the blocking service method
    inline, so the whole clone-gate-push sat on the loop and every concurrent request waited on it.

    The response SHAPE is asserted too: moving the call to a worker thread must not change what the
    route returns, or the console's own JavaScript stops reading it."""
    _on_the_event_loop_probe(monkeypatch, "pages_delete")

    response = _request(app, "POST", "/admin/api/pages/delete",
                        json_body={"actor": "marc@example.com", "paths": ["wiki/notes/Old.md"],
                                   "why": "stale"})

    assert response.status_code == 200
    assert response.json() == {"on_the_event_loop": False}


def test_pages_delete_needs_the_token_and_a_non_empty_paths_list(conn, app, monkeypatch):
    """The console's most consequential control (ADR 043 D2): its token IS the authorization, so
    the tokenless call must never reach the queueing seam at all — and an empty `paths` is a 400
    rather than a row the worker would claim and find nothing to do with."""
    from stigmergy.server import review as server_review

    def never(*_a, **_k):
        raise AssertionError("a removal was queued for a request that should have been refused")

    monkeypatch.setattr(server_review, "queue_deletion", never)

    tokenless = _request(app, "POST", "/admin/api/pages/delete", token=None,
                         json_body={"actor": "ops@example.com", "paths": ["wiki/notes/X.md"],
                                    "why": "stale"})
    empty = _request(app, "POST", "/admin/api/pages/delete",
                     json_body={"actor": "ops@example.com", "paths": [], "why": "stale"})

    assert tokenless.status_code in (401, 404)
    assert empty.status_code == 400
    assert "paths" in empty.json()["error"]


def test_pages_delete_on_a_deployment_with_no_evidence_store_is_the_409(conn, app):
    """The deployment shape a console served by a process whose object store is unconfigured is in:
    a removal is a queued capture, and a capture needs somewhere to archive its material. The
    refusal names what is missing, and it is a 409 — never a 500 naming a class."""
    response = _request(app, "POST", "/admin/api/pages/delete",
                        json_body={"actor": "ops@example.com", "paths": ["wiki/notes/X.md"],
                                   "why": "stale"})

    assert response.status_code == 409
    assert "evidence store" in response.json()["error"]
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 0


def test_pages_delete_over_http_queues_the_removal_when_the_queue_is_wired(conn, server_settings,
                                                                          admin_settings):
    """The benign twin for both refusals above, over the real route: with the queue wired, the
    console's Remove lands a `delete` row attributed to the operator who pressed it. Without this,
    the two 409s would only measure how easily this route says no."""
    from stigmergy.capture.evidence import MemoryEvidenceStore

    wired = compose(_inner, conn=conn, server_settings=server_settings,
                    admin_settings=admin_settings,
                    evidence=MemoryEvidenceStore())

    response = _request(wired, "POST", "/admin/api/pages/delete",
                        json_body={"actor": "ops@example.com", "paths": ["wiki/notes/Old.md"],
                                   "why": "superseded"})

    assert response.status_code == 200, response.json()
    assert response.json()["status"] == capture_schema.QUEUED
    with conn.cursor() as cur:
        cur.execute("SELECT kind, submitted_by FROM capture_queue WHERE id = %s",
                    (response.json()["id"],))
        assert cur.fetchone() == (capture_schema.DELETE, "ops@example.com")
