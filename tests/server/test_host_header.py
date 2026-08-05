"""A LIVE BUG, FIXED — this file is its regression guard:

    Claude Code -> https://brain.example.com/mcp failed for EVERY request, with a
    perfectly valid tester bearer token. Fly server logs:
        WARNING  Invalid Host header: brain.example.com (transport_security.py:64)
        POST /mcp -> 421 Misdirected Request

ROOT CAUSE — confirmed directly against the installed `mcp==1.28.1` SDK (not just read, executed):
`mcp/server/fastmcp/server.py` (`FastMCP.__init__`, around line 177-179 in this install):

    if transport_security is None and host in ("127.0.0.1", "localhost", "::1"):
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"], ...)

The `host` there is `FastMCP.__init__`'s OWN constructor parameter — UNRELATED to the real uvicorn
bind host (`transport_http.serve_http` binds uvicorn separately, to `0.0.0.0` in production). It
defaults to `"127.0.0.1"`, and `stigmergy.server.mcp_server.build_mcp` never passed `host=`
explicitly, so this heuristic always fired and auto-enabled DNS-rebinding protection scoped to
exactly those three localhost spellings — regardless of what host the process is actually reachable
at. `mcp.server.transport_security.TransportSecurityMiddleware._validate_host` then rejected any
OTHER `Host` header with 421 before the request ever reached our own `_BearerAuthMiddleware`'s
outcome mattering — a REAL client at the REAL public URL sends `Host:
brain.example.com`, which matched none of the three baked-in patterns.

Every other test in `tests/server/` runs its httpx client against a literal `127.0.0.1:<port>` URL
with no explicit Host override, so this was structurally invisible to the whole existing suite —
this file's own tests deliberately FORGE the `Host` header to close that blind spot, and are the
only ones in the suite that need `$STIGMERGY_PUBLIC_HOST` at all (set per-test via `monkeypatch`,
never ambient — see each test).

THE FIX: `transport_http.build_http_app` now builds an explicit
`TransportSecuritySettings` mirroring the SDK's own localhost defaults PLUS the real host(s) read
from `$STIGMERGY_PUBLIC_HOST` (comma-separated; `fly.toml`'s `[env]` sets it to
`brain.example.com` in production). Unset — the default everywhere except the real
deployment — reproduces the exact pre-fix, localhost-only behavior, so local dev and every other
test stay unaffected. A second, independent precondition surfaced verifying the fix against this
file's raw-httpx assertions: `build_mcp` now also passes `json_response=True` for the HTTP
transport (stdio: still `False`, inert) — without it every streamable-HTTP response is SSE-framed
regardless of the client's `Accept` header (no per-request negotiation, only this server-wide
flag), invisible to the real MCP client SDK (which parses either transparently) but required for
a raw `httpx.post(...).json()` caller — this file's own reproduction, and, in production, any
plain HTTP health probe or `curl` — to get a directly decodable body.

CONTRACT VERDICT (unchanged by the fix): this WAS a bug, not intended behavior. A real MCP client
completing tool calls against the deployed server, at its real public HTTPS URL, is the whole
point of the HTTP transport. The DNS-rebinding protection itself was always correct and stays ON
after the fix (see the negative-case tests below) — the bug was that nobody told it the real host.
"""
import httpx

from tests.server.conftest import build_test_http_app, issue_test_token, run_http_server

# The exact host from the live symptom — the deployed Fly app (ADR 013 §7), and the value
# `fly.toml`'s [env] STIGMERGY_PUBLIC_HOST is actually set to in production.
STAGING_HOST = "brain.example.com"
# A host that must NEVER be legitimately allowlisted — the companion/negative case.
UNKNOWN_HOST = "evil-dns-rebind-attempt.example.com"


def _initialize_body(msg_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "method": "initialize",
           "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                      "clientInfo": {"name": "test", "version": "0"}}}


def _post_with_host(url: str, token: str, host: str) -> httpx.Response:
    return httpx.post(url, json=_initialize_body(),
                      headers={"Authorization": f"Bearer {token}",
                              "Accept": "application/json, text/event-stream",
                              "Content-Type": "application/json",
                              "Host": host}, timeout=5)


def test_a_valid_token_at_the_real_deployed_host_reaches_the_normal_auth_and_tool_path(
        indexed, monkeypatch):
    """FIXED: with `$STIGMERGY_PUBLIC_HOST` configured (set here, self-contained — never depends on
    ambient env), a real client, a valid token, the real deployed host — reaches the normal
    auth/tool path (200, a real `initialize` response), no longer 421'd. This is the test that was
    committed `xfail(strict=True)` in the reproduction step; the marker is gone now that the fix
    landed (an xfail that started passing would XPASS-fail the suite — the correct next action was
    exactly this: remove it, not silence it further)."""
    monkeypatch.setenv("STIGMERGY_PUBLIC_HOST", STAGING_HOST)
    _, fx = indexed
    token, digest = issue_test_token(fx.STEWARD)
    app = build_test_http_app(fx, {digest: fx.STEWARD})
    with run_http_server(app) as url:
        r = _post_with_host(url, token, STAGING_HOST)

    assert r.status_code == 200, (
        f"got {r.status_code} for the allowlisted deployed Host {STAGING_HOST!r} with a VALID "
        f"token — expected a normal served response: {r.text[:300]!r}")
    body = r.json()   # json_response=True (the fix's second precondition): a plain JSON body,
    # never SSE-framed, is exactly what a raw (non-SDK) HTTP caller like this test needs.
    assert body["jsonrpc"] == "2.0" and "result" in body
    assert body["result"]["serverInfo"]["name"] == "stigmergy-brain"


def test_a_host_outside_the_allowlist_still_gets_421_with_the_public_host_configured(
        indexed, monkeypatch):
    """Companion/regression guard: the DNS-rebinding protection itself must stay ON — configuring
    the legitimate deployed host must NOT open the door to every other host too. Same
    `$STIGMERGY_PUBLIC_HOST` configuration as the fixed test above (the realistic production state),
    a DIFFERENT, never-allowlisted host — still 421. Must keep passing forever; it is not expected
    to ever flip."""
    monkeypatch.setenv("STIGMERGY_PUBLIC_HOST", STAGING_HOST)
    _, fx = indexed
    token, digest = issue_test_token(fx.STEWARD)
    app = build_test_http_app(fx, {digest: fx.STEWARD})
    with run_http_server(app) as url:
        r = _post_with_host(url, token, UNKNOWN_HOST)

    assert r.status_code == 421
    assert "Invalid Host header" in r.text


def test_an_unconfigured_deployment_keeps_the_original_localhost_only_behavior(indexed, monkeypatch):
    """The fix's own stated backward-compatibility claim, verified: `$STIGMERGY_PUBLIC_HOST` UNSET
    (explicitly cleared here, never assumed from ambient env) must reproduce the exact pre-fix
    behavior — even the REAL deployed host name gets 421'd, because nothing told this
    unconfigured process what its own public host is. This is what keeps local dev and every
    OTHER test in this suite (none of which touch `$STIGMERGY_PUBLIC_HOST`) unaffected by the fix."""
    monkeypatch.delenv("STIGMERGY_PUBLIC_HOST", raising=False)
    _, fx = indexed
    token, digest = issue_test_token(fx.STEWARD)
    app = build_test_http_app(fx, {digest: fx.STEWARD})
    with run_http_server(app) as url:
        r = _post_with_host(url, token, STAGING_HOST)

    assert r.status_code == 421
    assert "Invalid Host header" in r.text
