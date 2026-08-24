"""Authenticated, stateless MCP-over-HTTP transport with request-scoped identity."""
import contextvars
import functools
import logging
import os

import anyio.to_thread
from starlette.requests import Request
from starlette.responses import JSONResponse

from stigmergy.admin import routes as admin_routes
from stigmergy.capture import evidence as evidence_plane
from stigmergy.capture.errors import CaptureError
from stigmergy.capture.schema import ensure_capture_schema
from stigmergy.capture.uploads import ensure_upload_schema
from stigmergy.changes.store import ensure_change_schema
from stigmergy.index import store
from stigmergy.server import ops_files, webhook
from stigmergy.server.audit import AuditWriter, ensure_audit_table
from stigmergy.server.errors import IdentityError
from stigmergy.server.identity import load_token_store, resolve_email_for_token
from stigmergy.server.mcp_server import build_mcp
from stigmergy.server.ratelimit import RateLimiter
from stigmergy.server.service import BrainService, open_scoped_resources

log = logging.getLogger(__name__)

_UNAUTHORIZED_BODY = {"error": "unauthorized"}

# Enforced while streaming, before the MCP SDK buffers or parses the body.
MAX_REQUEST_BODY_BYTES = 256 * 1024
_TOO_LARGE_BODY = {"error": "request too large"}

_LOCALHOST_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_LOCALHOST_ALLOWED_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def _public_hosts_from_env() -> list[str]:
    """Return configured public hostnames."""
    raw = os.environ.get("STIGMERGY_PUBLIC_HOST", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def _build_transport_security(public_hosts: list[str]):
    """Build DNS-rebinding protection for local and deployed hosts."""
    from mcp.server.transport_security import TransportSecuritySettings

    allowed_hosts = list(_LOCALHOST_ALLOWED_HOSTS)
    allowed_origins = list(_LOCALHOST_ALLOWED_ORIGINS)
    for host in public_hosts:
        allowed_hosts += [host, f"{host}:443"]
        allowed_origins.append(f"https://{host}")
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=allowed_hosts, allowed_origins=allowed_origins)


def _transport_security_for_env():
    """Use FastMCP's localhost defaults when no public host is configured."""
    public_hosts = _public_hosts_from_env()
    return _build_transport_security(public_hosts) if public_hosts else None

_current_service: contextvars.ContextVar[BrainService | None] = contextvars.ContextVar(
    "stigmergy_http_current_service", default=None)


class _ScopedServiceProxy:
    """Forward tool access to the request-scoped service."""

    def __getattr__(self, name):
        service = _current_service.get()
        if service is None:  # pragma: no cover — defensive; the middleware always sets it first
            raise RuntimeError("no request-scoped BrainService (auth middleware did not run)")
        return getattr(service, name)


async def _refuse(scope, receive, send, body, status) -> None:
    """Send a fixed refusal without reflecting rejected input."""
    response = JSONResponse(body, status_code=status)
    await response(scope, receive, send)


class _BearerAuthMiddleware:
    """Resolve a principal and bind its service in the same ASGI coroutine."""

    def __init__(self, app, *, settings, token_store, connection_factory, embedder,
                 rate_limiter, evidence=None):
        self.app = app
        self._settings = settings
        self._token_store = token_store
        self._connection_factory = connection_factory
        self._embedder = embedder
        self._rate_limiter = rate_limiter
        self._evidence = evidence

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # The webhook authenticates its exact route with an HMAC over the raw body.
        if scope.get("path") == webhook.WEBHOOK_PATH:
            await self.app(scope, receive, send)
            return

        auth_values = [v for k, v in (scope.get("headers") or []) if k.lower() == b"authorization"]
        if len(auth_values) > 1:
            log.warning("HTTP auth refused: %d Authorization headers presented", len(auth_values))
            await _refuse(scope, receive, send, _UNAUTHORIZED_BODY, 401)
            return
        raw = auth_values[0].decode("latin-1") if auth_values else ""
        scheme, _, rest = raw.partition(" ")
        token = rest.strip() if scheme.lower() == "bearer" else ""
        try:
            email = resolve_email_for_token(self._token_store, token)
        except IdentityError as ex:
            log.warning("HTTP auth refused (%s)", ex.__class__.__name__)
            await _refuse(scope, receive, send, _UNAUTHORIZED_BODY, 401)
            return

        declared = _declared_body_length(scope)
        if declared is not None and declared > MAX_REQUEST_BODY_BYTES:
            log.warning("HTTP request refused: content-length %d exceeds %d bytes",
                        declared, MAX_REQUEST_BODY_BYTES)
            await _refuse(scope, receive, send, _TOO_LARGE_BODY, 413)
            return

        bounded_receive, exceeded = await _bounded_receive(
            receive, MAX_REQUEST_BODY_BYTES
        )
        if exceeded:
            log.warning(
                "HTTP request body exceeded %d bytes while streaming",
                MAX_REQUEST_BODY_BYTES,
            )
            await _refuse(scope, receive, send, _TOO_LARGE_BODY, 413)
            return

        conn = self._connection_factory()
        try:
            try:
                principal = ops_files.resolve_identity_principal(
                    conn, self._settings.identities_path, email
                )
            except IdentityError as ex:
                log.warning("HTTP auth refused (%s)", ex.__class__.__name__)
                await _refuse(scope, bounded_receive, send, _UNAUTHORIZED_BODY, 401)
                return
            audiences_tuple = principal.audiences
            audiences = set(audiences_tuple) if audiences_tuple is not None else None
            service = BrainService(
                self._settings,
                conn,
                self._embedder,
                audiences,
                identity=email,
                rate_limiter=self._rate_limiter,
                audit=AuditWriter(conn),
                evidence=self._evidence,
                principal=principal,
            )
            reset_token = _current_service.set(service)
            try:
                await self.app(scope, bounded_receive, send)
            finally:
                _current_service.reset(reset_token)
        finally:
            conn.close()


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


async def _bounded_receive(receive, limit: int):
    """Read one bounded request body, then replay it to the application."""
    messages = []
    total = 0
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            messages.append(message)
            break
        total += len(message.get("body") or b"")
        if total > limit:
            return receive, True
        messages.append(message)
        if not message.get("more_body", False):
            break

    position = 0

    async def replay():
        nonlocal position
        if position < len(messages):
            message = messages[position]
            position += 1
            return message
        return await receive()

    return replay, False


def token_store_from_env() -> dict[str, str]:
    """The token store deploy secret: inline JSON (`$STIGMERGY_TOKEN_STORE`) or a file path
    (`$STIGMERGY_TOKEN_STORE_FILE`). Fail-closed, resolved once at startup — a misconfigured
    store refuses to serve rather than starting HTTP auth open."""
    return load_token_store(os.environ.get("STIGMERGY_TOKEN_STORE"),
                            os.environ.get("STIGMERGY_TOKEN_STORE_FILE"))


async def _json_service_call(request: Request, method_name: str) -> JSONResponse:
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise CaptureError("request body must be a JSON object")
        service = _current_service.get()
        if service is None:
            raise RuntimeError("request identity is unavailable")
        method = getattr(service, method_name)
        result = await anyio.to_thread.run_sync(functools.partial(method, **body))
        return JSONResponse(result)
    except CaptureError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    except Exception as error:  # noqa: BLE001
        log.error(
            "bridge request failed",
            extra={"operation": method_name, "error_class": error.__class__.__name__},
        )
        return JSONResponse({"error": f"request failed ({error.__class__.__name__})"}, status_code=500)


def build_http_app(settings, *, token_store: dict[str, str]):
    """Build the HTTP app with one isolated database connection per request."""
    startup_conn, embedder = open_scoped_resources(settings)
    ensure_audit_table(startup_conn)
    ensure_capture_schema(startup_conn)
    ensure_upload_schema(startup_conn)
    ensure_change_schema(startup_conn)
    store.ensure_ops_file_table(startup_conn)
    store.ensure_webhook_dedupe_table(startup_conn)
    rate_limiter = RateLimiter()
    evidence = evidence_plane.store_from_env()
    connection_factory = functools.partial(store.connect, settings.dsn)

    mcp = build_mcp(_ScopedServiceProxy(), stateless_http=True,
                    transport_security=_transport_security_for_env(), json_response=True)

    webhook_settings = webhook.webhook_settings_from_env()

    @mcp.custom_route(webhook.WEBHOOK_PATH, methods=["POST"])
    async def _github_webhook(request):
        conn = connection_factory()
        try:
            return await webhook.webhook_endpoint(
                request,
                conn=conn,
                embedder=embedder,
                settings=webhook_settings,
            )
        finally:
            conn.close()

    @mcp.custom_route("/bridge/uploads", methods=["POST"])
    async def _create_bridge_upload(request: Request):
        return await _json_service_call(request, "create_upload")

    @mcp.custom_route("/bridge/captures", methods=["POST"])
    async def _finalize_bridge_capture(request: Request):
        return await _json_service_call(request, "finalize_upload_capture")

    app = mcp.streamable_http_app()
    app.add_middleware(
        _BearerAuthMiddleware,
        settings=settings,
        token_store=token_store,
        connection_factory=connection_factory,
        embedder=embedder,
        rate_limiter=rate_limiter,
        evidence=evidence,
    )
    try:
        return admin_routes.compose(
            app,
            conn=startup_conn,
            connection_factory=connection_factory,
            server_settings=settings,
            evidence=evidence,
        )
    finally:
        startup_conn.close()


def serve_http(settings, host: str, port: int) -> None:
    """`stigmergy-server --transport http`'s entry point. Any startup failure propagates as the
    SAME exception types `mcp_server.main`'s stdio path catches — one error-formatting path."""
    import uvicorn

    token_store = token_store_from_env()   # IdentityError on any failure (fail-closed)
    app = build_http_app(settings, token_store=token_store)
    uvicorn.run(app, host=host, port=port)
