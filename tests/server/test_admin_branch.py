"""The backoffice on the REAL production wiring (`build_http_app`): one roster serves MCP and the
backoffice, one token opens both for a person, and the MCP surface stays fail-closed.
Driven over `httpx.ASGITransport`: the bearer middleware refuses tokenless MCP calls before any
session/lifespan machinery is involved, so no uvicorn is needed for what this file proves."""
import asyncio

import httpx

from stigmergy.server.identity import hash_token
from tests.server.conftest import build_test_http_app, issue_test_token, rate_limiter_of

CSRF = {"X-Stigmergy-CSRF": "1", "Origin": "http://localhost"}


def _request(app, method, path, **kwargs):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://localhost") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(go())


def test_the_console_serves_its_shell_and_its_api_refuses_anonymous_calls(indexed, monkeypatch):
    monkeypatch.delenv("STIGMERGY_PUBLIC_HOST", raising=False)
    _conn, fixture = indexed
    app = build_test_http_app(fixture, {})
    assert _request(app, "GET", "/admin/").status_code == 200
    refused = _request(app, "GET", "/admin/api/meta")
    assert refused.status_code == 401
    assert refused.json() == {"error": "unauthorized"}
    mcp_refused = _request(app, "GET", "/mcp")
    assert mcp_refused.status_code == 401
    assert mcp_refused.json() == {"error": "unauthorized"}


def test_one_token_opens_mcp_and_signs_an_administrator_in(indexed, monkeypatch):
    monkeypatch.delenv("STIGMERGY_PUBLIC_HOST", raising=False)
    conn, fixture = indexed
    token, digest = issue_test_token(fixture.STEWARD)
    app = build_test_http_app(fixture, {digest: fixture.STEWARD})
    with conn.cursor() as cur:
        cur.execute("UPDATE roster_principals SET is_admin = TRUE WHERE subject = %s",
                    (fixture.STEWARD,))

    signed_in = _request(app, "POST", "/admin/api/session", json={"token": token}, headers=CSRF)
    cookie = {"Cookie": signed_in.headers["set-cookie"].split(";", 1)[0]}
    opened = _request(app, "GET", "/admin/api/meta", headers=cookie)
    as_bearer = _request(app, "GET", "/admin/api/meta",
                         headers={"Authorization": f"Bearer {token}"})
    on_mcp = _request(app, "GET", "/health", headers={"Authorization": f"Bearer {token}"})

    assert signed_in.status_code == 200
    assert opened.status_code == 200 and opened.json()["actor"]["subject"] == fixture.STEWARD
    assert as_bearer.status_code == 401, "the backoffice API takes sessions, never a bearer"
    assert on_mcp.status_code == 200
    with conn.cursor() as cur:
        cur.execute("SELECT last_used_at IS NOT NULL FROM roster_tokens WHERE sha256 = %s",
                    (hash_token(token),))
        assert cur.fetchone()[0] is True


def test_the_branch_still_exposes_the_starlette_introspection_seams(indexed):
    """`rate_limiter_of`/`evidence_store_of` reach `app.user_middleware` on what
    `build_http_app` returns — the branch wrapper must stay transparent to them."""
    _conn, fixture = indexed
    app = build_test_http_app(fixture, {})
    assert rate_limiter_of(app) is not None
