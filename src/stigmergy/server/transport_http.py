"""The HTTP transport: the exact tool surface `mcp_server.build_mcp` exposes over stdio, served
over MCP streamable HTTP. Auth chain, fail-closed on every step: `Authorization: Bearer <token>` →
sha256 → token store → email → `identity.resolve_audiences` → audiences; any failure returns one
fixed, generic 401 body — never an identity list, a path, or a DSN fragment.

One shared FastMCP app, not one per identity: `build_mcp(service)` is called exactly once with
`_ScopedServiceProxy`, whose every attribute access forwards to the per-request `BrainService` the
auth middleware stored in the `_current_service` contextvar.

`stateless_http=True` is MANDATORY, not a style choice. FastMCP's stateful default runs every
later request of a session inside the session CREATOR's frozen `contextvars.Context`: a caller
presenting someone else's `mcp-session-id` would execute — and be audited — under that identity's
scope, and the unbounded persistent dispatch tasks are a DoS. Stateless mode starts a fresh
request-scoped task per request, which inherits the context the middleware set in the SAME
coroutine — that is what makes the contextvar design true.

Sharing one Postgres connection and one embedder across identities rests on two invariants: no DB
helper holds a cursor open across an `await` (see `audit.AuditWriter`), and no sync tool body may
perform blocking network or subprocess I/O — sync bodies run directly on the event loop, so a slow
socket freezes the whole process for every identity (`brain_submit`'s object-store write bounds
itself; `review_decide`'s entity mint clones a repo and shells out to gitleaks and does NOT yet).
Anything adding a pool, a concurrent write path or a new outbound call must preserve both.
`RateLimiter`/`AuditWriter` are constructed once and shared, so the 30/10 req/min budgets are
per-identity across the whole process, not per connection.
"""
import contextvars
import logging
import os

from starlette.responses import JSONResponse

from stigmergy.admin import routes as admin_routes
from stigmergy.capture import evidence as evidence_plane
from stigmergy.capture.schema import ensure_capture_schema
from stigmergy.index import store
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

# The ceiling on a request body, enforced BEFORE the body is buffered: nothing below this
# middleware bounds it (the MCP SDK reads the whole body unbounded; the 256 KB material cap fires
# only after buffering, JSON-parsing and hashing), so an authenticated caller could otherwise OOM
# the machine. 1 MiB is 4x the material cap plus JSON-RPC envelope room — no legitimate capture
# comes near it.
MAX_REQUEST_BODY_BYTES = 1024 * 1024
_TOO_LARGE_BODY = {"error": "request too large"}

# `FastMCP.__init__` auto-builds DNS-rebinding protection ONLY when its OWN `host` ctor param
# (unrelated to uvicorn's real bind host) is a localhost spelling, allowlisting exactly these
# three. Mirrored here rather than relied on, so the real deployed host can be added beside them.
_LOCALHOST_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_LOCALHOST_ALLOWED_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def _public_hosts_from_env() -> list[str]:
    """`$STIGMERGY_PUBLIC_HOST`: the deployed hostname(s), comma-separated, trimmed; not a
    secret. Empty/unset = not configured — FastMCP's localhost-only auto-default then stands."""
    raw = os.environ.get("STIGMERGY_PUBLIC_HOST", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def _build_transport_security(public_hosts: list[str]):
    """DNS-rebinding protection stays ON either way (a missing public host is a config gap, not a
    reason to weaken the check): the localhost allowlist PLUS each public host — bare and `:443`,
    how a default-HTTPS `Host` header arrives — and its `https://` origin."""
    from mcp.server.transport_security import TransportSecuritySettings

    allowed_hosts = list(_LOCALHOST_ALLOWED_HOSTS)
    allowed_origins = list(_LOCALHOST_ALLOWED_ORIGINS)
    for host in public_hosts:
        allowed_hosts += [host, f"{host}:443"]
        allowed_origins.append(f"https://{host}")
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=allowed_hosts, allowed_origins=allowed_origins)


def _transport_security_for_env():
    """`None` when `$STIGMERGY_PUBLIC_HOST` is unset — FastMCP's own auto-localhost default then
    fires unchanged; local dev is unaffected."""
    public_hosts = _public_hosts_from_env()
    return _build_transport_security(public_hosts) if public_hosts else None

_current_service: contextvars.ContextVar[BrainService | None] = contextvars.ContextVar(
    "stigmergy_http_current_service", default=None)


class _ScopedServiceProxy:
    """A `BrainService` look-alike for `build_mcp()`'s tool closures: every attribute access
    forwards to the per-request `BrainService` in `_current_service`. HTTP only — stdio hands
    `build_mcp()` a concrete `BrainService`."""

    def __getattr__(self, name):
        service = _current_service.get()
        if service is None:  # pragma: no cover — defensive; the middleware always sets it first
            raise RuntimeError("no request-scoped BrainService (auth middleware did not run)")
        return getattr(service, name)


async def _refuse(scope, receive, send, body, status) -> None:
    """One refusal path for every pre-service rejection: the body is a module constant, never
    composed from what was rejected — the real reason is logged server-side only."""
    response = JSONResponse(body, status_code=status)
    await response(scope, receive, send)


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

        # The ONE exemption, and it is an EXACT path match — never a prefix, never a regex.
        # `webhook.WEBHOOK_PATH` is the SAME constant the route is mounted at, so the two files
        # cannot silently disagree. The webhook authenticates by HMAC over the raw body inside
        # its own endpoint; no identity is resolved and no `_current_service` is touched here.
        if scope.get("path") == webhook.WEBHOOK_PATH:
            await self.app(scope, receive, send)
            return

        # Every Authorization header value, never dict()-collapsed to the last: a second header
        # is adversarial-shaped and must be refused, not silently resolved to whichever won.
        auth_values = [v for k, v in (scope.get("headers") or []) if k.lower() == b"authorization"]
        if len(auth_values) > 1:
            log.warning("HTTP auth refused: %d Authorization headers presented", len(auth_values))
            await _refuse(scope, receive, send, _UNAUTHORIZED_BODY, 401)
            return
        raw = auth_values[0].decode("latin-1") if auth_values else ""
        # Scheme match case-insensitive per RFC 9110 §11.1; split on the first space so a token
        # containing spaces is not mangled.
        scheme, _, rest = raw.partition(" ")
        token = rest.strip() if scheme.lower() == "bearer" else ""
        try:
            email = resolve_email_for_token(self._token_store, token)
            audiences_tuple = resolve_audiences(self._settings.identities_path, email)
        except IdentityError as ex:
            log.warning("HTTP auth refused: %s", ex)   # server-side only — never in the response
            await _refuse(scope, receive, send, _UNAUTHORIZED_BODY, 401)
            return

        # Size check AFTER auth (neither branch has read a byte of body yet): a declared
        # `content-length` over the cap is refused outright; a chunked body is capped as it
        # streams, by `_capped_receive`.
        declared = _declared_body_length(scope)
        if declared is not None and declared > MAX_REQUEST_BODY_BYTES:
            log.warning("HTTP request refused: content-length %d exceeds %d bytes",
                        declared, MAX_REQUEST_BODY_BYTES)
            await _refuse(scope, receive, send, _TOO_LARGE_BODY, 413)
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
    """The request's `content-length`, or None when absent or unparseable — unparseable counts as
    "not declared" because the streaming cap is the backstop either way, so a malformed header
    never becomes a second refusal path with its own error shape."""
    for key, value in scope.get("headers") or []:
        if key.lower() == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _capped_receive(receive, limit: int):
    """Wrap an ASGI `receive` so a body with no declared length cannot exceed `limit` either:
    past the cap it reports `http.disconnect`, so the process never buffers beyond the bound.
    Deliberately less polite than the declared-length path's 413 — a client that will not say how
    big its body is has opted out of being told; real clients always set `content-length`."""
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
    """The token store deploy secret: inline JSON (`$STIGMERGY_TOKEN_STORE`) or a file path
    (`$STIGMERGY_TOKEN_STORE_FILE`). Fail-closed, resolved once at startup — a misconfigured
    store refuses to serve rather than starting HTTP auth open."""
    return load_token_store(os.environ.get("STIGMERGY_TOKEN_STORE"),
                            os.environ.get("STIGMERGY_TOKEN_STORE_FILE"))


def build_http_app(settings, *, token_store: dict[str, str]):
    """Wire the process-wide resources ONCE — conn, embedder, rate limiter, audit writer — and
    mount the bearer-auth middleware in front of the ONE FastMCP app every identity shares."""
    conn, embedder = open_scoped_resources(settings)
    ensure_audit_table(conn)
    ensure_capture_schema(conn)   # the durable write-path tables, same startup as the audit one
    review.ensure_review_schema(conn)   # the review lane's table, same startup pattern
    # Created here rather than only on the webhook's write path: `CREATE TABLE IF NOT EXISTS` is
    # not race-free, and losing that race INSIDE phase 2 rolls the pushed pages back with it.
    store.ensure_entity_registry_table(conn)
    audit = AuditWriter(conn)
    rate_limiter = RateLimiter()
    # One evidence store for the whole process: constructing it does no I/O, and its boto3 client
    # is thread-safe. Every per-request `BrainService` shares it.
    evidence = evidence_plane.store_from_env()

    mcp = build_mcp(_ScopedServiceProxy(), stateless_http=True,  # see module docstring — mandatory
                    transport_security=_transport_security_for_env(), json_response=True)

    # The incremental-index webhook, mounted on this SAME FastMCP instance so the middleware
    # below wraps both routes and its ONE exemption applies to exactly one path.
    webhook_settings = webhook.webhook_settings_from_env()

    @mcp.custom_route(webhook.WEBHOOK_PATH, methods=["POST"])
    async def _github_webhook(request):
        return await webhook.webhook_endpoint(request, conn=conn, embedder=embedder,
                                              settings=webhook_settings)

    app = mcp.streamable_http_app()
    app.add_middleware(_BearerAuthMiddleware, settings=settings, token_store=token_store,
                       conn=conn, embedder=embedder, rate_limiter=rate_limiter, audit=audit,
                       evidence=evidence)
    # The admin console rides in front as an ASGI BRANCH, not a middleware exemption: `/admin*`
    # never reaches `_BearerAuthMiddleware`, everything else flows through unchanged, and with
    # `$STIGMERGY_ADMIN_TOKEN_HASH` unset the branch is inert 404s — so the ONE webhook exemption
    # keeps meaning exactly one path.
    return admin_routes.compose(app, conn=conn, server_settings=settings)


def serve_http(settings, host: str, port: int) -> None:
    """`stigmergy-server --transport http`'s entry point. Any startup failure propagates as the
    SAME exception types `mcp_server.main`'s stdio path catches — one error-formatting path."""
    import uvicorn

    token_store = token_store_from_env()   # IdentityError on any failure (fail-closed)
    app = build_http_app(settings, token_store=token_store)
    uvicorn.run(app, host=host, port=port)
