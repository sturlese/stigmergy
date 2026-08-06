"""The HTTP transport: the exact tool surface `mcp_server.build_mcp` already exposes over stdio,
served over MCP streamable HTTP with per-user, hashed-bearer-token auth.

The resolution chain is `Authorization: Bearer <token>` → sha256 → the token store (`identity.py`'s
`resolve_email_for_token`) → email → `ops/identities.json` (the SAME `identity.resolve_audiences`
stdio already uses) → audiences. Fail-closed on every step: any failure returns one fixed, generic
401 body (`_UNAUTHORIZED_BODY`) — never an identity list, a path, or a DSN fragment.
`docs/reference/server.md` sweeps every error string reachable through the HTTP boundary.

Architecture note — one shared FastMCP app, not one per identity: `build_mcp(service)` is called
exactly ONCE (stdio's own contract — its test double, `test_mcp_adapter.py`, passes a concrete
`BrainService` and must keep working unchanged). Building a separate FastMCP/Starlette app per
identity would mean separately driving each one's `StreamableHTTPSessionManager` lifespan
(`streamable_http_app()`'s `lifespan=lambda app: self.session_manager.run()`), which uvicorn only
does for the ONE top-level app it was handed — real extra complexity for a single-Fly-machine
staging deployment with 2-3 testers. Instead, `_ScopedServiceProxy` stands in for the concrete
`BrainService` build_mcp() closes over; each attribute access forwards to whichever `BrainService`
the auth middleware resolved, via the `_current_service` contextvar — see the STATELESS note
below for what actually guarantees that contextvar is fresh on every call.

**`stateless_http=True` is mandatory here, not a style choice.** `build_http_app` passes it to
`build_mcp` explicitly. FastMCP's DEFAULT (stateful) streamable-HTTP mode spawns the session's
message-dispatch task ONCE, on the request that creates the session (the one returning an
`mcp-session-id` header) — every LATER request against that same session ID is proxied into that
ALREADY-RUNNING task, which keeps running inside whatever `contextvars.Context` was captured at
its creation. `_BearerAuthMiddleware` still runs its auth check and `_current_service.set(...)`
on every incoming HTTP request either way, but in stateful mode that later `.set()` lands in the
context of a request whose auth outcome is then thrown away — the actual tool call keeps
executing inside the session-creator's frozen context instead. Two concrete failure modes this
caused before the fix: (a) a caller with their OWN valid token, but presenting someone ELSE's
`mcp-session-id`, would execute — and be audited — under the session creator's identity/ACL
scope, a session-hijack-shaped hole; (b) FastMCP's stateful sessions have no idle timeout
(`session_idle_timeout=None` by default) and `initialize` isn't rate-limited pre-auth, so nothing
bounds how many of these persistent dispatch tasks accumulate — an unbounded-task DoS.

With `stateless_http=True`, `StreamableHTTPSessionManager` starts a fresh, request-scoped
dispatch task for EVERY HTTP request instead of one persistent task per session: no
`mcp-session-id` is ever handed out, so there is no session identity for a token to "borrow", and
`_current_service`'s value — set by `_BearerAuthMiddleware` immediately before `await
self.app(...)` in the SAME coroutine, no task hand-off — is guaranteed to be the context the new
per-request task actually inherits (`asyncio.create_task`/`anyio.start_soon` both copy the
CURRENT context at spawn time). This is what makes the contextvar design in this module true; it
was NOT true against FastMCP's stateful default.

Sharing one Postgres connection and one query embedder across every identity is safe here
because FastMCP invokes a sync tool body directly on the event loop (never via a thread pool —
verified against the installed `mcp` package), so at most one blocking DB call is ever in flight
per process — more precisely, no DB helper here holds a cursor open across an `await` (`ask` is
async and awaits the LLM BETWEEN its own read calls, never mid-cursor). That is one half of the actual
invariant. The other half is the one that actually bites, and it went unstated until an audit
named it: **no sync tool body may perform blocking network or subprocess I/O**, because the same
"directly on the event loop" fact that makes the shared cursor safe makes a slow socket a freeze
of the whole process for every identity. `brain_submit`'s object-store write bounds itself for
this reason (`capture.evidence.WORST_CASE_STALL_S`); `review_decide`'s entity mint clones a repo
and shells out to gitleaks and does NOT yet. Anything adding a connection pool, a concurrent write
path, or a new outbound call must preserve both halves explicitly; see
`stigmergy.server.audit.AuditWriter`'s docstring for the same reasoning applied to
the audit writes. `RateLimiter` and `AuditWriter` are constructed once and shared by every
per-request `BrainService`, so the 30/10 req/min budgets are honestly per-identity across the
whole process, not per connection.
"""
import contextvars
import logging
import os

from starlette.responses import JSONResponse

from stigmergy.admin import routes as admin_routes
from stigmergy.capture import evidence as evidence_plane
from stigmergy.capture.schema import ensure_capture_schema
from stigmergy.server import review, webhook
from stigmergy.server.audit import AuditWriter, ensure_audit_table
from stigmergy.server.errors import IdentityError
from stigmergy.server.identity import (
    load_token_store,
    resolve_audiences,
    resolve_email_for_token,
)
from stigmergy.server.mcp_server import build_mcp
from stigmergy.server.ratelimit import RateLimiter
from stigmergy.server.service import BrainService, open_scoped_resources

log = logging.getLogger(__name__)

# The ONE body every HTTP auth failure returns — no identity list, no path, no DSN fragment ever
# crosses this boundary. The real reason is logged server-side only.
_UNAUTHORIZED_BODY = {"error": "unauthorized"}

# The ceiling on a request body, enforced BEFORE the body is buffered.
#
# Nothing below this middleware bounds it: the MCP SDK does `body = await request.body()` with no
# limit, uvicorn imposes none, and the 256 KB material cap in `capture.schema` fires only AFTER
# the whole body has been read into memory, JSON-parsed and hashed. An authenticated caller could
# therefore post hundreds of megabytes and have the server buffer all of it before deciding it was
# too big — an OOM lever on a single small machine. The capture write path is what legitimizes
# large bodies at all, so the contract goes AHEAD of the buffering rather than behind it.
#
# 1 MiB is 4x the material cap plus room for JSON-RPC envelope, hints and escaping, so no
# legitimate capture comes near it while the worst case stays a known constant.
MAX_REQUEST_BODY_BYTES = 1024 * 1024
_TOO_LARGE_BODY = {"error": "request too large"}

# `FastMCP.__init__` auto-builds a `TransportSecuritySettings` (DNS-rebinding protection) ONLY
# when its OWN `host` ctor param — unrelated to uvicorn's real bind host — is a localhost
# spelling, allowlisting exactly these three (the bug this closed is pinned by
# tests/server/test_host_header.py). We mirror that exact default here rather than relying on it,
# so the real deployed host can be added alongside it explicitly.
_LOCALHOST_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_LOCALHOST_ALLOWED_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def _public_hosts_from_env() -> list[str]:
    """`$STIGMERGY_PUBLIC_HOST`: the real hostname(s) this server is deployed at (e.g.
    `brain.example.com`), comma-separated, trimmed. Not a secret — `fly.toml`'s
    `[env]` sets it directly. Empty/unset means "not configured" — `_build_transport_security`
    then leaves FastMCP's own localhost-only auto-default untouched (local dev unchanged)."""
    raw = os.environ.get("STIGMERGY_PUBLIC_HOST", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def _build_transport_security(public_hosts: list[str]):
    """DNS-rebinding protection stays ON either way (ADR 013 — a missing public host is a config
    gap, not a reason to weaken the check): the SDK's own localhost allowlist, PLUS each
    configured public host (bare and `:443`, matching how a browser/client's default-HTTPS `Host`
    header arrives with no explicit port) and its `https://` origin."""
    from mcp.server.transport_security import TransportSecuritySettings

    allowed_hosts = list(_LOCALHOST_ALLOWED_HOSTS)
    allowed_origins = list(_LOCALHOST_ALLOWED_ORIGINS)
    for host in public_hosts:
        allowed_hosts += [host, f"{host}:443"]
        allowed_origins.append(f"https://{host}")
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=allowed_hosts, allowed_origins=allowed_origins)


def _transport_security_for_env():
    """`None` when `$STIGMERGY_PUBLIC_HOST` is unset — `build_mcp` then passes `None` straight
    through to `FastMCP(...)`, so its own auto-localhost default fires unchanged. Local dev is
    unaffected."""
    public_hosts = _public_hosts_from_env()
    return _build_transport_security(public_hosts) if public_hosts else None

_current_service: contextvars.ContextVar[BrainService | None] = contextvars.ContextVar(
    "stigmergy_http_current_service", default=None)


class _ScopedServiceProxy:
    """A `BrainService` look-alike for `build_mcp()`'s tool closures: every attribute access
    forwards to the real, per-request `BrainService` held in `_current_service`. Used ONLY by
    this module; stdio hands `build_mcp()` a concrete `BrainService` and never touches this."""

    def __getattr__(self, name):
        service = _current_service.get()
        if service is None:  # pragma: no cover — defensive; the middleware always sets it first
            raise RuntimeError("no request-scoped BrainService (auth middleware did not run)")
        return getattr(service, name)


class _BearerAuthMiddleware:
    """Raw ASGI middleware (not `BaseHTTPMiddleware`): running the auth check and the downstream
    app in the SAME coroutine, with no task hand-off, is what guarantees the `_current_service`
    contextvar set below is visible to the tool closure that eventually runs inside `call_next`.

    Per request: `Authorization: Bearer <token>` → email → audiences (fail-closed) → a
    per-request `BrainService` sharing the process-wide conn/embedder/rate limiter/audit writer
    (cheap — no I/O in its constructor)."""

    def __init__(self, app, *, settings, token_store, conn, embedder, rate_limiter, audit,
                 evidence=None):
        self.app = app
        self._settings = settings
        self._token_store = token_store
        self._conn = conn
        self._embedder = embedder
        self._rate_limiter = rate_limiter
        self._audit = audit
        self._evidence = evidence

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # The ONE exemption from this middleware, and it is an EXACT path match — never a prefix,
        # never a regex. `webhook.WEBHOOK_PATH` is the SAME constant the
        # route is mounted at, so this file and `webhook.py` cannot silently disagree about which
        # path is exempt. The webhook authenticates a DIFFERENT way entirely (HMAC over the raw
        # body, inside `webhook.webhook_endpoint` itself) — nothing about identity/audiences is
        # resolved here for it, and no `BrainService`/`_current_service` is touched on this path.
        if scope.get("path") == webhook.WEBHOOK_PATH:
            await self.app(scope, receive, send)
            return

        # Collect every Authorization header value (never dict()-collapse to the last one — a
        # request smuggling a SECOND Authorization header is itself adversarial-shaped and must
        # be refused, not silently resolved against whichever value happened to win the dict).
        auth_values = [v for k, v in (scope.get("headers") or []) if k.lower() == b"authorization"]
        if len(auth_values) > 1:
            log.warning("HTTP auth refused: %d Authorization headers presented", len(auth_values))
            response = JSONResponse(_UNAUTHORIZED_BODY, status_code=401)
            await response(scope, receive, send)
            return
        raw = auth_values[0].decode("latin-1") if auth_values else ""
        # Scheme match is case-insensitive per RFC 9110 §11.1 ("Bearer"/"bearer"/"BEARER" are all
        # valid); split on the first space so a token containing spaces (never true for tokens
        # this server issues, but not this parser's job to assume) doesn't get mangled.
        scheme, _, rest = raw.partition(" ")
        token = rest.strip() if scheme.lower() == "bearer" else ""
        try:
            email = resolve_email_for_token(self._token_store, token)
            audiences_tuple = resolve_audiences(self._settings.identities_path, email)
        except IdentityError as ex:
            log.warning("HTTP auth refused: %s", ex)   # server-side only — never in the response
            response = JSONResponse(_UNAUTHORIZED_BODY, status_code=401)
            await response(scope, receive, send)
            return

        # Size check AFTER auth: an unauthenticated caller is refused on headers alone either way,
        # and neither branch has read a single byte of the body yet. A declared `content-length`
        # over the cap is refused outright; a chunked body (no declared length) is capped as it
        # streams, by `_capped_receive` below.
        declared = _declared_body_length(scope)
        if declared is not None and declared > MAX_REQUEST_BODY_BYTES:
            log.warning("HTTP request refused: content-length %d exceeds %d bytes",
                        declared, MAX_REQUEST_BODY_BYTES)
            response = JSONResponse(_TOO_LARGE_BODY, status_code=413)
            await response(scope, receive, send)
            return

        audiences = set(audiences_tuple) if audiences_tuple is not None else None
        service = BrainService(self._settings, self._conn, self._embedder, audiences,
                               identity=email, rate_limiter=self._rate_limiter, audit=self._audit,
                               evidence=self._evidence)
        reset_token = _current_service.set(service)
        try:
            await self.app(scope, _capped_receive(receive, MAX_REQUEST_BODY_BYTES), send)
        finally:
            _current_service.reset(reset_token)


def _declared_body_length(scope) -> int | None:
    """The request's `content-length`, or None when it is absent (a chunked body) or unparseable.
    Unparseable counts as "not declared" rather than as an error: the streaming cap below is the
    backstop either way, so a malformed header never becomes a second refusal path with its own
    error shape."""
    for key, value in scope.get("headers") or []:
        if key.lower() == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _capped_receive(receive, limit: int):
    """Wrap an ASGI `receive` so a body without a declared length cannot exceed `limit` either.

    Once the running total passes the cap, the wrapper reports `http.disconnect` instead of more
    body: the downstream `await request.body()` stops reading right there (Starlette raises
    `ClientDisconnect`), so the process never buffers past the bound. That is deliberately less
    polite than the 413 the declared-length path returns — a client that streams a body without
    saying how big it is has already opted out of being told before it sends. Real MCP clients
    (and `httpx`, which the SDK uses) always set `content-length` for a bytes body, so the clean
    413 is what any legitimate oversized call actually gets; this is the backstop for the rest."""
    total = 0

    async def capped():
        nonlocal total
        message = await receive()
        if message.get("type") == "http.request":
            total += len(message.get("body") or b"")
            if total > limit:
                log.warning("HTTP request body exceeded %d bytes mid-stream — read aborted", limit)
                return {"type": "http.disconnect"}
        return message

    return capped


def token_store_from_env() -> dict[str, str]:
    """The token store is a deploy secret: inline JSON via
    `$STIGMERGY_TOKEN_STORE` (the usual Fly-secret shape), or a file path via
    `$STIGMERGY_TOKEN_STORE_FILE`. Fail-closed — resolved once at startup so a misconfigured store
    refuses to serve rather than starting HTTP auth open."""
    return load_token_store(os.environ.get("STIGMERGY_TOKEN_STORE"),
                            os.environ.get("STIGMERGY_TOKEN_STORE_FILE"))


def build_http_app(settings, *, token_store: dict[str, str]):
    """Wire the process-wide resources ONCE — conn, embedder, rate limiter, audit writer — and
    mount the bearer-auth middleware in front of the ONE FastMCP streamable-HTTP app every
    identity shares (see the module docstring for why one app, not one per identity)."""
    conn, embedder = open_scoped_resources(settings)
    ensure_audit_table(conn)
    ensure_capture_schema(conn)   # the durable write-path tables, same startup as the audit one
    review.ensure_review_schema(conn)   # the review lane's table, same startup pattern
    audit = AuditWriter(conn)
    rate_limiter = RateLimiter()
    # One evidence store for the whole process, like the connection and the embedder: constructing
    # it does no I/O, and its boto3 client is thread-safe for the concurrent-put case a future
    # worker pool would introduce. Every per-request `BrainService` shares it, so the write path
    # holds no per-identity resource.
    evidence = evidence_plane.store_from_env()

    mcp = build_mcp(_ScopedServiceProxy(), stateless_http=True,  # see module docstring — mandatory
                    transport_security=_transport_security_for_env(), json_response=True)

    # The incremental-index webhook, mounted on this SAME FastMCP instance via its own
    # `custom_route` seam — one process group, one Starlette app, so the middleware below wraps
    # both the MCP endpoint and this route, and its ONE exemption applies to exactly one path.
    webhook_settings = webhook.webhook_settings_from_env()

    @mcp.custom_route(webhook.WEBHOOK_PATH, methods=["POST"])
    async def _github_webhook(request):
        return await webhook.webhook_endpoint(request, conn=conn, embedder=embedder,
                                              settings=webhook_settings)

    app = mcp.streamable_http_app()
    app.add_middleware(_BearerAuthMiddleware, settings=settings, token_store=token_store,
                       conn=conn, embedder=embedder, rate_limiter=rate_limiter, audit=audit,
                       evidence=evidence)
    # ADR 029: the admin console rides in front as an ASGI BRANCH, not as a middleware exemption
    # — `/admin*` never reaches `_BearerAuthMiddleware`, everything else (lifespan included)
    # flows through unchanged, and with `$STIGMERGY_ADMIN_TOKEN_HASH` unset the branch is inert
    # 404s. The ONE webhook exemption above keeps meaning exactly one path.
    return admin_routes.compose(app, conn=conn, server_settings=settings)


def serve_http(settings, host: str, port: int) -> None:
    """`stigmergy-server --transport http`'s entry point. Any startup failure (token store,
    identities file shape, unreachable Postgres, empty index) propagates as the SAME exception
    types `mcp_server.main`'s stdio path already catches — one error-formatting code path for
    both transports."""
    import uvicorn

    token_store = token_store_from_env()   # IdentityError on any failure (fail-closed)
    app = build_http_app(settings, token_store=token_store)
    uvicorn.run(app, host=host, port=port)
