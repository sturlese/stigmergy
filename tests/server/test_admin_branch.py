"""The admin branch on the REAL production wiring (`build_http_app`) — spec criteria 1/2/6:
inert-by-default 404s, live console behind its own token, and the MCP surface unchanged either
way. Driven over `httpx.ASGITransport`: the bearer middleware refuses tokenless MCP calls before
any session/lifespan machinery is involved, so no uvicorn is needed for what this file proves."""
import asyncio

import httpx

from stigmergy.admin.settings import ACTOR_ENV, TOKEN_HASH_ENV
from stigmergy.server.identity import hash_token
from tests.server.conftest import build_test_http_app, issue_test_token, rate_limiter_of

ADMIN_TOKEN = "full-stack-admin-token"


def _get(app, path, headers=None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://localhost") as client:
            return await client.get(path, headers=headers or {})
    return asyncio.run(go())


def test_without_the_env_the_console_does_not_exist_and_mcp_stays_fail_closed(indexed,
                                                                              monkeypatch):
    monkeypatch.delenv(TOKEN_HASH_ENV, raising=False)
    _conn, fixture = indexed
    token, digest = issue_test_token("steward@example.com")
    app = build_test_http_app(fixture, {digest: "steward@example.com"})
    assert _get(app, "/admin/").status_code == 404
    assert _get(app, "/admin/api/meta").status_code == 404
    refused = _get(app, "/mcp")
    assert refused.status_code == 401
    assert refused.json() == {"error": "unauthorized"}


def test_with_the_env_the_console_serves_and_mcp_auth_is_untouched(indexed, monkeypatch):
    monkeypatch.setenv(TOKEN_HASH_ENV, hash_token(ADMIN_TOKEN))
    monkeypatch.setenv(ACTOR_ENV, "steward@example.com")
    _conn, fixture = indexed
    token, digest = issue_test_token("steward@example.com")
    app = build_test_http_app(fixture, {digest: "steward@example.com"})

    assert _get(app, "/admin/").status_code == 200
    assert _get(app, "/admin/api/meta").status_code == 401, "the MCP token store must not open it"
    opened = _get(app, "/admin/api/meta", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    assert opened.status_code == 200

    mcp_refused = _get(app, "/mcp")
    assert mcp_refused.status_code == 401
    assert mcp_refused.json() == {"error": "unauthorized"}

    admin_on_mcp = _get(app, "/mcp", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    assert admin_on_mcp.status_code == 401, "the admin token must open NOTHING on the MCP surface"


def test_the_branch_still_exposes_the_starlette_introspection_seams(indexed, monkeypatch):
    """`rate_limiter_of`/`evidence_store_of` reach `app.user_middleware` on what
    `build_http_app` returns — the branch wrapper must stay transparent to them."""
    monkeypatch.delenv(TOKEN_HASH_ENV, raising=False)
    _conn, fixture = indexed
    app = build_test_http_app(fixture, {})
    assert rate_limiter_of(app) is not None
