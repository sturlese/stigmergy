"""The backoffice: personal sign-in, role-gated operations, and the permission-scoped wiki.

Every API request authenticates with a session cookie opened by a person's token and resolves its
principal from the roster on that request. Every state change also needs the page's
anti-CSRF header and a same-host Origin. Operational endpoints exist only for administrators: for
anyone else they answer exactly like an unknown route.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import re
import secrets
from pathlib import Path

from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Match, Mount, Route
from starlette.staticfiles import StaticFiles

from stigmergy.admin import auth
from stigmergy.admin import schema as admin_schema
from stigmergy.admin.console import ConsoleRefused, ConsoleService
from stigmergy.admin.schema import ensure_admin_schema
from stigmergy.admin.service import (
    AdminBadRequest,
    AdminNotFound,
    AdminRefused,
    AdminService,
    AdminUnavailable,
)
from stigmergy.admin.wiki import WikiNotFound, WikiService
from stigmergy.capture.errors import CaptureError, FetchRejected, FetchUnavailable
from stigmergy.capture.schema import MAX_ARTIFACT_BYTES
from stigmergy.kernel import layers, tenancy
from stigmergy.kernel.blocking import run_blocking
from stigmergy.server import platform, roster
from stigmergy.server.errors import (
    ArgumentLengthError,
    CapabilityUnavailableError,
    IdentityError,
    RateLimitError,
)
from stigmergy.server.identity import Principal
from stigmergy.server.ratelimit import AuthFailureThrottle, client_address

log = logging.getLogger(__name__)

ADMIN_PREFIX = "/admin"
API_PREFIX = "/admin/api/"
STATIC_DIR = Path(__file__).parent / "static"
MAX_ADMIN_BODY_BYTES = MAX_ARTIFACT_BYTES + 2 * 1024 * 1024
MAX_JSON_BODY_BYTES = 256 * 1024
MAX_SIGN_IN_BODY_BYTES = 4 * 1024
UPLOAD_PATHS = ("captures/file", "contradictions/resolve-file")
SESSION_PATH = "session"
PLATFORM_PATH = "platform/"
# The landing page's request-access form: the one write anyone may make without a session.
ACCESS_PATH = "access-requests"

# The console's components add small style elements at runtime; they carry the per-page nonce the
# shell hands them, so no inline style is allowed without it.
NONCE_PLACEHOLDER = "__CSP_NONCE__"


def _csp(style_nonce: str | None = None) -> str:
    styles = f"'self' 'nonce-{style_nonce}'" if style_nonce else "'self'"
    return (
        f"default-src 'none'; style-src {styles}; script-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    )


_CSP = _csp()
_NOT_FOUND = {"error": "not found"}
_UNAUTHORIZED = {"error": "unauthorized"}
_SIGN_IN_FAILED = {"error": "sign-in failed: the token is not valid"}
_SIGN_IN_THROTTLED = {"error": "too many failed sign-ins from this address; wait and retry"}
_CSRF_REFUSED = {"error": "request refused: it did not come from this console"}


def compose(
    inner,
    *,
    conn,
    connection_factory=None,
    server_settings,
    evidence=None,
    embedder=None,
    rate_limiter=None,
    throttle: AuthFailureThrottle | None = None,
):
    platform.ensure_platform_schema(conn)
    ensure_admin_schema(conn)
    roster.ensure_roster_schema(conn)
    services = _Services(
        connection_factory or (lambda: conn),
        owns_connections=connection_factory is not None,
        server_settings=server_settings,
        evidence=evidence,
        embedder=embedder,
        rate_limiter=rate_limiter,
    )
    app, member_routes = _build_admin_app(services, throttle or AuthFailureThrottle())
    return _Branch(inner, _AdminGate(app, member_routes, services, _public_hosts_from_env()))


class _Services:
    """Open one connection per operation and bind it to the request's principal."""

    def __init__(self, connection_factory, *, owns_connections: bool, server_settings, evidence,
                 embedder, rate_limiter):
        self._connection_factory = connection_factory
        self._owns = owns_connections
        self._server = server_settings
        self._evidence = evidence
        self._embedder = embedder
        self.rate_limiter = rate_limiter

    def run(self, operation, *, tenant: str | None = None):
        """Run `operation` on one connection, acting for `tenant` when one is named."""
        conn = self._connection_factory()
        try:
            if tenant:
                with tenancy.tenant_scope(conn, tenant):
                    return operation(conn)
            return operation(conn)
        finally:
            if self._owns:
                conn.close()

    def admin(self, principal, name: str, *args, **kwargs):
        return self.run(lambda conn: getattr(
            AdminService(conn, principal=principal, server_settings=self._server,
                         evidence=self._evidence, rate_limiter=self.rate_limiter),
            name)(*args, **kwargs),
            tenant=_tenant_of(principal))

    def wiki(self, principal, name: str, *args, **kwargs):
        return self.run(lambda conn: getattr(
            WikiService(conn, principal=principal, server_settings=self._server,
                        embedder=self._embedder, rate_limiter=self.rate_limiter),
            name)(*args, **kwargs), tenant=_tenant_of(principal))


def _tenant_of(principal) -> str:
    if not principal.tenant:
        raise AdminNotFound("open a workspace first")
    return principal.tenant


def resolve_session(conn, value: str | None) -> Principal:
    """The principal behind a backoffice session: a tenant member, or an operator acting in the
    session's tenant (or in none yet)."""
    try:
        return roster.session_principal(conn, value).principal
    except IdentityError:
        operator = platform.operator_session(conn, value)
    tenant = operator.acting_tenant
    if tenant:
        found = platform.tenant_by_id(conn, tenant)
        if found is not None:
            with tenancy.tenant_scope(conn, tenant):
                acting = platform.operator_principal(conn, operator.subject, tenant)
            if acting is not None:
                return acting
    return Principal(subject=operator.subject, display_name=operator.display_name, groups=(),
                     default_audience=None, kind="person", is_admin=False, operator=True)


def _public_hosts_from_env() -> list[str]:
    return [
        value.strip()
        for value in os.environ.get("STIGMERGY_PUBLIC_HOST", "").split(",")
        if value.strip()
    ]


class _Branch:
    def __init__(self, inner, admin_app):
        self._inner = inner
        self._admin = admin_app

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            send = _unindexed(send)
        path = scope.get("path", "")
        readable = scope["type"] == "http" and scope.get("method") in ("GET", "HEAD")
        if readable and path.startswith(LEGAL_PREFIX):
            name = path[len(LEGAL_PREFIX):]
            response = (JSONResponse({"html": legal_html(name)}, headers={"cache-control": "no-cache"})
                        if name in legal_pages() else PlainTextResponse("Not Found", status_code=404))
            await response(scope, receive, send)
            return
        if readable and landing_enabled() and path.removeprefix("/") in legal_pages():
            await _landing()(scope, receive, send)
            return
        if scope["type"] == "http" and path == "/" and scope.get("method") in ("GET", "HEAD"):
            # The bare host is the hosted service's landing page where the operator turns it on, and
            # the backoffice sign-in everywhere else; MCP and the backoffice have their own paths.
            response = _landing() if landing_enabled() else RedirectResponse(f"{ADMIN_PREFIX}/", status_code=302)
            await response(scope, receive, send)
            return
        if scope["type"] != "http" or not (
            path == ADMIN_PREFIX or path.startswith(f"{ADMIN_PREFIX}/")
        ):
            await self._inner(scope, receive, send)
            return
        await self._admin(scope, receive, send)


_EVERY_RESPONSE = (
    (b"x-robots-tag", b"noindex, nofollow"),
    (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
    (b"x-content-type-options", b"nosniff"),
)
_EVERY_RESPONSE_KEYS = {key for key, _value in _EVERY_RESPONSE}


def _unindexed(send):
    """Every response stays out of search engines, is HTTPS-only, and is never content-sniffed."""
    async def sending(message):
        if message["type"] == "http.response.start":
            headers = [(k, v) for k, v in message.get("headers", [])
                       if k.lower() not in _EVERY_RESPONSE_KEYS]
            message = {**message, "headers": [*headers, *_EVERY_RESPONSE]}
        await send(message)
    return sending


LANDING_ENV = "STIGMERGY_LANDING"


def landing_enabled() -> bool:
    """The operator turned its landing page on and built one into the image (the platform ships none)."""
    return (os.environ.get(LANDING_ENV, "").strip().lower() in ("on", "1", "true")
            and (STATIC_DIR / "landing.html").is_file())


# The operator's own legal pages (sub-processors, privacy, terms): one Markdown file per page in
# this directory, served at `/<name>`. A deployment that sets none serves none.
LEGAL_DIR_ENV = "STIGMERGY_LEGAL_DIR"
LEGAL_PREFIX = "/legal/"
_LEGAL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


def legal_pages() -> frozenset[str]:
    root = os.environ.get(LEGAL_DIR_ENV, "")
    if not root or not Path(root).is_dir():
        return frozenset()
    return frozenset(path.stem for path in Path(root).glob("*.md") if _LEGAL_NAME.match(path.stem))


def legal_html(name: str) -> str:
    return _legal_html(os.environ.get(LEGAL_DIR_ENV, ""), name)


@functools.cache
def _legal_html(root: str, name: str) -> str:
    """One legal page as HTML; raw HTML in the Markdown is escaped, not rendered."""
    from markdown_it import MarkdownIt

    text = (Path(root) / f"{name}.md").read_text(encoding="utf-8")
    return MarkdownIt("commonmark", {"html": False}).enable("table").render(text)


def _landing() -> HTMLResponse:
    nonce = secrets.token_urlsafe(18)
    page = (STATIC_DIR / "landing.html").read_text(encoding="utf-8")
    return HTMLResponse(page.replace(NONCE_PLACEHOLDER, nonce),
                        headers={"content-security-policy": _csp(nonce), "cache-control": "no-cache"})


def _body_limit(relative: str) -> int:
    if relative in (SESSION_PATH, ACCESS_PATH):
        return MAX_SIGN_IN_BODY_BYTES
    return MAX_ADMIN_BODY_BYTES if relative in UPLOAD_PATHS else MAX_JSON_BODY_BYTES


class _AdminGate:
    def __init__(self, app, member_routes: list[Route], services: _Services,
                 public_hosts: list[str]):
        self._app = app
        self._member_routes = member_routes
        self._services = services
        self._public_hosts = public_hosts

    def _member_route(self, scope) -> bool:
        return any(route.matches(scope)[0] == Match.FULL for route in self._member_routes)

    def _allowed(self, principal, scope, relative: str) -> bool:
        """The master console is the operator's alone; an operator outside any workspace reaches
        nothing else but its own session; inside one it administers."""
        if relative.startswith(PLATFORM_PATH):
            return principal.operator
        if principal.operator and not principal.tenant:
            return relative in ("me", SESSION_PATH)
        return principal.administers or self._member_route(scope)

    async def __call__(self, scope, receive, send):
        headers = scope.get("headers") or []
        if not auth.host_allowed(headers, self._public_hosts):
            await self._respond(JSONResponse({"error": "misdirected request"}, status_code=421),
                                scope, receive, send)
            return
        path = scope.get("path", "")
        if not path.startswith(API_PREFIX):
            await self._app(scope, receive, self._sending(scope, send))
            return
        relative = path[len(API_PREFIX):]
        limit = _body_limit(relative)
        length = _content_length(headers)
        if length is not None and length > limit:
            await self._respond(JSONResponse({"error": "request too large"}, status_code=413),
                                scope, receive, send)
            return
        method = scope.get("method", "GET")
        if method not in ("GET", "HEAD") and not auth.same_origin_request(
            headers, self._public_hosts
        ):
            await self._respond(JSONResponse(_CSRF_REFUSED, status_code=403), scope, receive, send)
            return
        scope.setdefault("state", {})
        if relative in (SESSION_PATH, ACCESS_PATH) and method == "POST":
            await self._app(scope, _bounded_receive(receive, limit), self._sending(scope, send))
            return
        session = auth.session_value(headers)
        try:
            principal = await run_blocking(
                self._services.run, lambda conn: resolve_session(conn, session))
        except IdentityError:
            await self._respond(JSONResponse(_UNAUTHORIZED, status_code=401), scope, receive, send)
            return
        if not self._allowed(principal, scope, relative):
            if principal.tenant:
                await run_blocking(functools.partial(
                    self._services.run,
                    functools.partial(_record_denial, self._services.rate_limiter,
                                      principal.subject, method, relative),
                    tenant=principal.tenant))
            await self._respond(JSONResponse(_NOT_FOUND, status_code=404), scope, receive, send)
            return
        scope["state"]["principal"] = principal
        scope["state"]["session"] = session
        await self._app(scope, _bounded_receive(receive, limit), self._sending(scope, send))

    async def _respond(self, response, scope, receive, send):
        await response(scope, receive, self._sending(scope, send))

    def _sending(self, scope, send):
        api = scope.get("path", "").startswith(API_PREFIX)

        async def secured(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                if not any(key.lower() == b"content-security-policy" for key, _value in headers):
                    headers.append((b"content-security-policy", _CSP.encode("ascii")))
                headers.extend(
                    (
                        (b"referrer-policy", b"no-referrer"),
                        (b"cache-control", b"no-store" if api else b"no-cache"),
                    )
                )
                message = {**message, "headers": headers}
            await send(message)

        return secured


def _content_length(headers) -> int | None:
    values = [value for key, value in headers if key.lower() == b"content-length"]
    if len(values) != 1:
        return None
    try:
        return int(values[0])
    except ValueError:
        return None


class _BodyTooLarge(Exception):
    pass


def _bounded_receive(receive, limit: int):
    seen = 0

    async def bounded():
        nonlocal seen
        message = await receive()
        if message.get("type") == "http.request":
            seen += len(message.get("body") or b"")
            if seen > limit:
                raise _BodyTooLarge
        return message

    return bounded


def _record_denial(rate_limiter, subject: str, method: str, relative: str, conn) -> None:
    """Audit a refused operational request, at most at the caller's ordinary request rate."""
    if rate_limiter is not None:
        try:
            rate_limiter.check(f"{tenancy.current_tenant(conn) or ''}:{subject}", "backoffice.denied")
        except RateLimitError:
            return
    admin_schema.record_action(conn, actor=subject, action="backoffice.denied",
                               args={"method": method, "path": relative[:200]}, outcome="refused")


def _endpoint(function):
    async def handler(request):
        try:
            return JSONResponse(await function(request))
        except _BodyTooLarge:
            return JSONResponse({"error": "request too large"}, status_code=413)
        except AdminBadRequest as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        except FetchUnavailable:
            return JSONResponse({"error": "public URL is temporarily unavailable"}, status_code=503)
        except FetchRejected:
            return JSONResponse({"error": "public URL was rejected"}, status_code=400)
        except CaptureError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        except AdminNotFound as error:
            return JSONResponse({"error": str(error)}, status_code=404)
        except WikiNotFound:
            return JSONResponse(_NOT_FOUND, status_code=404)
        except RateLimitError as error:
            return JSONResponse({"error": str(error)}, status_code=429)
        except ArgumentLengthError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        except CapabilityUnavailableError:
            return JSONResponse({"error": "search is unavailable on this server"}, status_code=503)
        except AdminUnavailable as error:
            return JSONResponse({"error": str(error)}, status_code=503)
        except AdminRefused as error:
            return JSONResponse({"error": str(error)}, status_code=403)
        except (ConsoleRefused, platform.PlatformRefused, roster.RosterRefused) as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        except Exception as error:  # noqa: BLE001
            log.error(
                "admin endpoint failed",
                extra={"path": request.url.path, "error_class": error.__class__.__name__},
            )
            return JSONResponse(
                {"error": f"operation failed ({error.__class__.__name__})"},
                status_code=500,
            )

    return handler


async def _json(request) -> dict:
    try:
        value = await request.json()
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise AdminBadRequest("request body must be valid JSON") from error
    if not isinstance(value, dict):
        raise AdminBadRequest("request body must be a JSON object")
    return value


def _text(value, field: str, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise AdminBadRequest(f"{field} must be a string")
    cleaned = value.strip()
    if required and not cleaned:
        raise AdminBadRequest(f"{field} is required")
    return cleaned


async def _form(request) -> dict:
    try:
        form = await request.form(
            max_files=1,
            max_fields=20,
            max_part_size=MAX_ARTIFACT_BYTES,
        )
    except _BodyTooLarge:
        raise
    except Exception as error:
        raise AdminBadRequest("multipart form is invalid or too large") from error
    return dict(form)


async def _upload_bytes(value) -> tuple[bytes, str | None, str | None]:
    if not isinstance(value, UploadFile):
        raise AdminBadRequest("file is required")
    data = await value.read(MAX_ARTIFACT_BYTES + 1)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise AdminBadRequest("file exceeds the 50 MiB limit")
    return data, value.content_type, value.filename


def _audience_from_form(value):
    if value in (None, "", "null"):
        return None
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise AdminBadRequest("audience must be a JSON list or null") from error
    return parsed


def _build_admin_app(services: _Services, throttle: AuthFailureThrottle) -> Starlette:
    access_throttle = AuthFailureThrottle()

    async def _console(request, method: str, *args, **kwargs):
        def call(conn):
            return getattr(ConsoleService(conn, operator=request.state.principal), method)(
                *args, **kwargs)
        return await run_blocking(services.run, call)

    async def _admin(request, name: str, *args, **kwargs):
        return await run_blocking(functools.partial(
            services.admin, request.state.principal, name, *args, **kwargs))

    async def _wiki(request, name: str, *args, **kwargs):
        return await run_blocking(functools.partial(
            services.wiki, request.state.principal, name, *args, **kwargs))

    async def index(_request):
        nonce = secrets.token_urlsafe(18)
        shell = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(shell.replace(NONCE_PLACEHOLDER, nonce),
                            headers={"content-security-policy": _csp(nonce)})

    async def root(_request):
        return RedirectResponse(f"{ADMIN_PREFIX}/", status_code=307)

    @_endpoint
    async def meta(request):
        return await _admin(request, "meta")

    @_endpoint
    async def overview(request):
        return await _admin(request, "overview")

    @_endpoint
    async def captures(request):
        statuses = request.query_params.getlist("status") or None
        layer_decisions = request.query_params.getlist("decision") or None
        submitter = request.query_params.get("submitter") or None
        return await _admin(request, "captures",
            statuses=statuses,
            layer_decisions=layer_decisions,
            submitter=submitter,
            limit=_integer(request.query_params.get("limit", "50"), "limit"),
            offset=_integer(request.query_params.get("offset", "0"), "offset"),
        )

    @_endpoint
    async def capture_detail(request):
        return await _admin(request, "capture", request.path_params["capture_id"])

    @_endpoint
    async def capture_retry(request):
        return await _admin(request, "retry_capture", request.path_params["capture_id"])

    @_endpoint
    async def capture_text(request):
        data = await _json(request)
        return await _admin(request, "submit_text",
            text=_text(data.get("text"), "text", required=True),
            title=_text(data.get("title"), "title"),
            occurred_at=_text(data.get("occurred_at"), "occurred_at"),
            audience=data.get("audience"),
            idempotency_key=_text(data.get("idempotency_key"), "idempotency_key") or None,
        )

    @_endpoint
    async def capture_url(request):
        data = await _json(request)
        return await _admin(request, "submit_url",
            url=_text(data.get("url"), "url", required=True),
            title=_text(data.get("title"), "title"),
            occurred_at=_text(data.get("occurred_at"), "occurred_at"),
            audience=data.get("audience"),
            idempotency_key=_text(data.get("idempotency_key"), "idempotency_key") or None,
        )

    @_endpoint
    async def capture_file(request):
        form = await _form(request)
        data, media_type, filename = await _upload_bytes(form.get("file"))
        return await _admin(request, "submit_file",
            data=data,
            filename=filename or "upload",
            media_type=media_type,
            title=_text(form.get("title"), "title"),
            occurred_at=_text(form.get("occurred_at"), "occurred_at"),
            audience=_audience_from_form(form.get("audience")),
            idempotency_key=_text(form.get("idempotency_key"), "idempotency_key") or None,
        )

    @_endpoint
    async def changes(request):
        return await _admin(request, "changes",
            trigger=request.query_params.get("trigger") or None,
            limit=_integer(request.query_params.get("limit", "50"), "limit"),
            offset=_integer(request.query_params.get("offset", "0"), "offset"),
        )

    @_endpoint
    async def change_detail(request):
        return await _admin(request, "change", request.path_params["change_id"])

    @_endpoint
    async def contradictions_list(request):
        return await _admin(request, "contradictions")

    @_endpoint
    async def contradiction_resolve(request):
        data = await _json(request)
        return await _admin(request, "resolve_contradiction",
            contradiction_id=_text(data.get("contradiction_id"), "contradiction_id", required=True),
            decision=_text(data.get("decision"), "decision", required=True),
            resolution=_text(data.get("resolution"), "resolution", required=True),
            rationale=_text(data.get("rationale"), "rationale", required=True),
            support_url=_text(data.get("support_url"), "support_url") or None,
        )

    @_endpoint
    async def contradiction_resolve_file(request):
        form = await _form(request)
        support = await _upload_bytes(form.get("file"))
        return await _admin(request, "resolve_contradiction",
            contradiction_id=_text(form.get("contradiction_id"), "contradiction_id", required=True),
            decision=_text(form.get("decision"), "decision", required=True),
            resolution=_text(form.get("resolution"), "resolution", required=True),
            rationale=_text(form.get("rationale"), "rationale", required=True),
            support_file=support,
        )

    @_endpoint
    async def entities(request):
        return await _admin(request, "entities")

    @_endpoint
    async def entity_dossier(request):
        return await _admin(request, "entity_dossier", request.path_params["entity"])

    @_endpoint
    async def entity_operation(request):
        data = await _json(request)
        entities = data.get("entities")
        if not isinstance(entities, list):
            raise AdminBadRequest("entities must be a list")
        evidence = data.get("evidence")
        if evidence is not None and not isinstance(evidence, dict):
            raise AdminBadRequest("evidence must be an object")
        return await _admin(request, "entity_operation",
            action=_text(data.get("action"), "action", required=True),
            entities=entities,
            rationale=_text(data.get("rationale"), "rationale", required=True),
            evidence=evidence,
            new_title=_text(data.get("new_title"), "new_title") or None,
        )

    @_endpoint
    async def delete_pages(request):
        data = await _json(request)
        paths = data.get("paths")
        if not isinstance(paths, list):
            raise AdminBadRequest("paths must be a list")
        return await _admin(request, "delete_pages",
            paths=paths,
            rationale=_text(data.get("rationale"), "rationale", required=True),
        )

    @_endpoint
    async def gardener(request):
        return await _admin(request, "gardener")

    @_endpoint
    async def gardener_trigger(request):
        data = await _json(request)
        return await _admin(request, "trigger_garden",
            rationale=_text(data.get("rationale"), "rationale"),
        )

    @_endpoint
    async def recompile_trigger(request):
        data = await _json(request)
        return await _admin(request, "trigger_recompile",
            rationale=_text(data.get("rationale"), "rationale"),
        )

    @_endpoint
    async def index_state(request):
        return await _admin(request, "index_state")

    @_endpoint
    async def graph_measures(request):
        return await _admin(request, "graph_measures",
                            _text(request.query_params.get("scope"), "scope") or "")

    @_endpoint
    async def worker(request):
        return await _admin(request, "worker_status")

    @_endpoint
    async def activity(request):
        return await _admin(request, "activity")

    @_endpoint
    async def platform_tenants(request):
        return await _console(request, "tenants")

    @_endpoint
    async def platform_tenant_create(request):
        data = await _json(request)
        return await _console(request, "create_tenant",
                              name=_text(data.get("name"), "name", required=True),
                              slug=_text(data.get("slug"), "slug", required=True),
                              admin_email=_text(data.get("admin_email"), "admin_email", required=True),
                              admin_name=_text(data.get("admin_name"), "admin_name", required=True))

    @_endpoint
    async def platform_tenant_status(request):
        data = await _json(request)
        return await _console(request, "set_status", request.path_params["tenant_id"],
                              _text(data.get("status"), "status", required=True))

    @_endpoint
    async def platform_tenant_rebuild(request):
        return await _console(request, "rebuild", request.path_params["tenant_id"])

    @_endpoint
    async def platform_tenant_erase(request):
        data = await _json(request)
        return await _console(request, "erase", request.path_params["tenant_id"],
                              str(data.get("confirm") or ""))

    @_endpoint
    async def platform_act(request):
        data = await _json(request)
        tenant_id = data.get("tenant_id")
        if tenant_id is not None and not isinstance(tenant_id, str):
            raise AdminBadRequest("tenant_id must be a string or null")
        session = request.state.session

        def act(conn):
            ConsoleService(conn, operator=request.state.principal).act(session, tenant_id)
            return _me(conn, resolve_session(conn, session))
        return await run_blocking(services.run, act)

    @_endpoint
    async def platform_access_requests(request):
        return await _console(request, "access_requests")

    @_endpoint
    async def platform_access_request_status(request):
        data = await _json(request)
        return await _console(request, "set_access_request", request.path_params["request_id"],
                              _text(data.get("status"), "status", required=True))

    @_endpoint
    async def platform_worker(request):
        return await _console(request, "worker")

    async def access_request(request):
        """Store an email left on the landing page. The answer is the same whether or not it was
        seen before; an address that sends too many is told to wait."""
        address = client_address(request.scope)
        if access_throttle.blocked(address):
            return JSONResponse({"error": "too many requests from this address; try later"},
                                status_code=429)
        access_throttle.failed(address)
        try:
            data = await _json(request)
        except _BodyTooLarge:
            return JSONResponse({"error": "request too large"}, status_code=413)
        except AdminBadRequest as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        if str(data.get("website") or ""):
            return JSONResponse({"received": True}, status_code=202)

        def store(conn):
            platform.record_access_request(conn, email=str(data.get("email") or ""),
                                           company=str(data.get("company") or ""),
                                           note=str(data.get("note") or ""))
        try:
            await run_blocking(services.run, store)
        except platform.PlatformRefused as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        return JSONResponse({"received": True}, status_code=202)

    async def sign_in(request):
        address = client_address(request.scope)
        try:
            data = await _json(request)
        except _BodyTooLarge:
            return JSONResponse({"error": "request too large"}, status_code=413)
        except AdminBadRequest as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        token = data.get("token")
        previous = auth.session_value(request.headers.raw)

        def open_session(conn):
            presented = token if isinstance(token, str) else ""
            try:
                value, session = roster.open_session(conn, presented)
            except IdentityError:
                value, operator = platform.open_operator_session(conn, presented)
                if previous:
                    _close_any(conn, previous)
                platform.record_action(conn, actor=operator.subject, action="session.open",
                                       outcome="ok")
                return value, resolve_session(conn, value)
            if previous:
                _close_any(conn, previous)
            with tenancy.tenant_scope(conn, session.principal.tenant):
                admin_schema.record_action(conn, actor=session.principal.subject,
                                           action="session.open", args={}, outcome="ok")
            return value, session.principal

        try:
            value, principal = await run_blocking(services.run, open_session)
        except IdentityError:
            if throttle.blocked(address):
                return JSONResponse(_SIGN_IN_THROTTLED, status_code=429)
            throttle.failed(address)
            log.warning("backoffice sign-in refused")
            return JSONResponse(_SIGN_IN_FAILED, status_code=401)
        response = JSONResponse(await run_blocking(services.run, lambda conn: _me(conn, principal)))
        response.headers["set-cookie"] = auth.session_cookie(
            value, max_age=int(roster.SESSION_ABSOLUTE.total_seconds()))
        return response

    async def sign_out(request):
        session = request.state.session
        principal = request.state.principal

        def close(conn):
            _close_any(conn, session)
            if principal.operator:
                platform.record_action(conn, actor=principal.subject, action="session.close",
                                       outcome="ok")
                return
            with tenancy.tenant_scope(conn, principal.tenant):
                admin_schema.record_action(conn, actor=principal.subject, action="session.close",
                                           args={}, outcome="ok")

        await run_blocking(services.run, close)
        response = JSONResponse({"signed_out": True})
        response.headers["set-cookie"] = auth.cleared_session_cookie()
        return response

    async def me(request):
        principal = request.state.principal
        return JSONResponse(await run_blocking(services.run, lambda conn: _me(conn, principal)))

    @_endpoint
    async def wiki_pages(request):
        return await _wiki(request, "pages")

    @_endpoint
    async def wiki_page(request):
        return await _wiki(request, "page", _text(request.query_params.get("path"), "path",
                                                  required=True))

    @_endpoint
    async def wiki_search(request):
        return await _wiki(request, "search", _text(request.query_params.get("q"), "q",
                                                    required=True))

    @_endpoint
    async def wiki_graph(request):
        return await _wiki(request, "graph")

    @_endpoint
    async def wiki_status(request):
        return await _wiki(request, "status")

    @_endpoint
    async def people(request):
        return await _admin(request, "people")

    @_endpoint
    async def person_create(request):
        return await _admin(request, "create_person", _person_fields(await _json(request)))

    @_endpoint
    async def person_detail(request):
        return await _admin(request, "person", request.path_params["subject"])

    @_endpoint
    async def person_update(request):
        return await _admin(request, "update_person", request.path_params["subject"],
                            _person_fields(await _json(request), partial=True))

    @_endpoint
    async def person_disable(request):
        data = await _json(request)
        disabled = data.get("disabled")
        if not isinstance(disabled, bool):
            raise AdminBadRequest("disabled must be true or false")
        return await _admin(request, "set_person_disabled", request.path_params["subject"],
                            disabled)

    @_endpoint
    async def person_delete(request):
        return await _admin(request, "delete_person", request.path_params["subject"])

    @_endpoint
    async def token_issue(request):
        data = await _json(request)
        return await _admin(request, "issue_token", request.path_params["subject"],
                            label=_text(data.get("label"), "label", required=True),
                            expires_at=_text(data.get("expires_at"), "expires_at") or None)

    @_endpoint
    async def token_revoke(request):
        return await _admin(request, "revoke_token", request.path_params["token_id"])

    @_endpoint
    async def groups(request):
        return await _admin(request, "groups")

    @_endpoint
    async def group_create(request):
        data = await _json(request)
        return await _admin(request, "create_group",
                            _text(data.get("name"), "name", required=True),
                            _text(data.get("description"), "description"))

    @_endpoint
    async def group_update(request):
        data = await _json(request)
        return await _admin(request, "update_group", request.path_params["name"],
                            _text(data.get("description"), "description"))

    @_endpoint
    async def group_erasure(request):
        return await _admin(request, "layer_erasure", request.path_params["name"])

    @_endpoint
    async def group_erase(request):
        data = await _json(request)
        return await _admin(request, "erase_layer", request.path_params["name"],
                            _text(data.get("confirm"), "confirm", required=True))

    @_endpoint
    async def group_delete(request):
        return await _admin(request, "delete_group", request.path_params["name"])

    @_endpoint
    async def channels(request):
        return await _admin(request, "channels")

    @_endpoint
    async def channel_set(request):
        data = await _json(request)
        return await _admin(request, "set_channel", request.path_params["channel_id"],
                            _group_list(data.get("groups"), "groups", nullable=True))

    @_endpoint
    async def channel_delete(request):
        return await _admin(request, "delete_channel", request.path_params["channel_id"])

    routes = [
        Route(ADMIN_PREFIX, root, methods=["GET"]),
        Route(f"{ADMIN_PREFIX}/", index, methods=["GET"]),
        Mount(f"{ADMIN_PREFIX}/assets", StaticFiles(directory=STATIC_DIR / "assets")),
    ]
    member_routes = [
        Route(f"{API_PREFIX}{ACCESS_PATH}", access_request, methods=["POST"]),
        Route(f"{API_PREFIX}session", sign_in, methods=["POST"]),
        Route(f"{API_PREFIX}session", sign_out, methods=["DELETE"]),
        Route(f"{API_PREFIX}me", me, methods=["GET"]),
        Route(f"{API_PREFIX}wiki/pages", wiki_pages, methods=["GET"]),
        Route(f"{API_PREFIX}wiki/page", wiki_page, methods=["GET"]),
        Route(f"{API_PREFIX}wiki/search", wiki_search, methods=["GET"]),
        Route(f"{API_PREFIX}wiki/status", wiki_status, methods=["GET"]),
        Route(f"{API_PREFIX}wiki/graph", wiki_graph, methods=["GET"]),
    ]
    platform_routes = [
        Route(f"{API_PREFIX}platform/tenants", platform_tenants, methods=["GET"]),
        Route(f"{API_PREFIX}platform/tenants", platform_tenant_create, methods=["POST"]),
        Route(f"{API_PREFIX}platform/tenants/{{tenant_id:str}}/status", platform_tenant_status,
              methods=["PUT"]),
        Route(f"{API_PREFIX}platform/tenants/{{tenant_id:str}}/rebuild", platform_tenant_rebuild,
              methods=["POST"]),
        Route(f"{API_PREFIX}platform/tenants/{{tenant_id:str}}/erase", platform_tenant_erase,
              methods=["POST"]),
        Route(f"{API_PREFIX}platform/acting", platform_act, methods=["POST"]),
        Route(f"{API_PREFIX}platform/access-requests", platform_access_requests, methods=["GET"]),
        Route(f"{API_PREFIX}platform/access-requests/{{request_id:str}}",
              platform_access_request_status, methods=["PUT"]),
        Route(f"{API_PREFIX}platform/worker", platform_worker, methods=["GET"]),
    ]
    admin_routes = [
        Route(f"{API_PREFIX}people", people, methods=["GET"]),
        Route(f"{API_PREFIX}people", person_create, methods=["POST"]),
        Route(f"{API_PREFIX}people/{{subject:str}}", person_detail, methods=["GET"]),
        Route(f"{API_PREFIX}people/{{subject:str}}", person_update, methods=["PUT"]),
        Route(f"{API_PREFIX}people/{{subject:str}}", person_delete, methods=["DELETE"]),
        Route(f"{API_PREFIX}people/{{subject:str}}/disabled", person_disable, methods=["PUT"]),
        Route(f"{API_PREFIX}people/{{subject:str}}/tokens", token_issue, methods=["POST"]),
        Route(f"{API_PREFIX}tokens/{{token_id:str}}", token_revoke, methods=["DELETE"]),
        Route(f"{API_PREFIX}groups", groups, methods=["GET"]),
        Route(f"{API_PREFIX}groups", group_create, methods=["POST"]),
        Route(f"{API_PREFIX}groups/{{name:str}}", group_update, methods=["PUT"]),
        Route(f"{API_PREFIX}groups/{{name:str}}", group_delete, methods=["DELETE"]),
        Route(f"{API_PREFIX}groups/{{name:str}}/erasure", group_erasure, methods=["GET"]),
        Route(f"{API_PREFIX}groups/{{name:str}}/erasure", group_erase, methods=["POST"]),
        Route(f"{API_PREFIX}channels", channels, methods=["GET"]),
        Route(f"{API_PREFIX}channels/{{channel_id:str}}", channel_set, methods=["PUT"]),
        Route(f"{API_PREFIX}channels/{{channel_id:str}}", channel_delete, methods=["DELETE"]),
        Route(f"{API_PREFIX}meta", meta, methods=["GET"]),
        Route(f"{API_PREFIX}overview", overview, methods=["GET"]),
        Route(f"{API_PREFIX}captures", captures, methods=["GET"]),
        Route(f"{API_PREFIX}captures/text", capture_text, methods=["POST"]),
        Route(f"{API_PREFIX}captures/url", capture_url, methods=["POST"]),
        Route(f"{API_PREFIX}captures/file", capture_file, methods=["POST"]),
        Route(f"{API_PREFIX}captures/{{capture_id:str}}/retry", capture_retry, methods=["POST"]),
        Route(f"{API_PREFIX}captures/{{capture_id:str}}", capture_detail, methods=["GET"]),
        Route(f"{API_PREFIX}changes", changes, methods=["GET"]),
        Route(f"{API_PREFIX}changes/{{change_id:str}}", change_detail, methods=["GET"]),
        Route(f"{API_PREFIX}contradictions", contradictions_list, methods=["GET"]),
        Route(f"{API_PREFIX}contradictions/resolve", contradiction_resolve, methods=["POST"]),
        Route(
            f"{API_PREFIX}contradictions/resolve-file",
            contradiction_resolve_file,
            methods=["POST"],
        ),
        Route(f"{API_PREFIX}entities", entities, methods=["GET"]),
        Route(f"{API_PREFIX}entities/{{entity:str}}/dossier", entity_dossier, methods=["GET"]),
        Route(f"{API_PREFIX}entities/operation", entity_operation, methods=["POST"]),
        Route(f"{API_PREFIX}knowledge/delete", delete_pages, methods=["POST"]),
        Route(f"{API_PREFIX}gardener", gardener, methods=["GET"]),
        Route(f"{API_PREFIX}gardener/trigger", gardener_trigger, methods=["POST"]),
        Route(f"{API_PREFIX}knowledge/recompile", recompile_trigger, methods=["POST"]),
        Route(f"{API_PREFIX}index", index_state, methods=["GET"]),
        Route(f"{API_PREFIX}graph-measures", graph_measures, methods=["GET"]),
        Route(f"{API_PREFIX}worker", worker, methods=["GET"]),
        Route(f"{API_PREFIX}activity", activity, methods=["GET"]),
    ]
    app = Starlette(routes=[*routes, *member_routes, *platform_routes, *admin_routes],
                    exception_handlers={
        404: _not_found, 405: _not_found})
    app.state.admin_routes = admin_routes
    return app, member_routes


async def _not_found(_request, _error: HTTPException):
    return JSONResponse(_NOT_FOUND, status_code=404)


def _close_any(conn, value: str) -> None:
    roster.close_session(conn, value)
    platform.close_operator_session(conn, value)


def _me(conn, principal) -> dict:
    tenant = platform.tenant_by_id(conn, principal.tenant) if principal.tenant else None
    return {
        "subject": principal.subject,
        "display_name": principal.display_name,
        "kind": principal.kind,
        "is_admin": principal.administers,
        "groups": list(principal.groups),
        "unrestricted": principal.audiences is None,
        "default_layer": principal.default_layer,
        "layers": _publishable(principal) if principal.tenant and not principal.operator else [],
        "operator": principal.operator,
        "tenant": ({"id": tenant.id, "slug": tenant.slug, "name": tenant.name}
                   if tenant is not None else None),
    }


def _publishable(principal) -> list[str]:
    return sorted(principal.publishable_layers, key=lambda layer: (layer != layers.PUBLIC, layer))


def _group_list(value, field: str, *, nullable: bool = False):
    if value is None and nullable:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AdminBadRequest(f"{field} must be a list of group names")
    return value


def _person_fields(data: dict, *, partial: bool = False) -> dict:
    """The person fields a request names; a partial update omits what it leaves unchanged."""
    fields = {}
    if not partial:
        fields["subject"] = _text(data.get("subject"), "subject")
    if not partial or "display_name" in data:
        fields["display_name"] = _text(data.get("display_name"), "display_name", required=True)
    if not partial or "kind" in data:
        fields["kind"] = _text(data.get("kind"), "kind") or "person"
    if not partial or "is_admin" in data:
        if not isinstance(data.get("is_admin", False), bool):
            raise AdminBadRequest("is_admin must be true or false")
        fields["is_admin"] = data.get("is_admin", False)
    if not partial or "groups" in data:
        fields["groups"] = _group_list(data.get("groups", []), "groups")
    if not partial or "default_audience" in data:
        fields["default_audience"] = _group_list(data.get("default_audience"),
                                                 "default_audience", nullable=True)
    return fields


def _integer(value: str, field: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise AdminBadRequest(f"{field} must be an integer") from error
