"""The `/admin` branch — composition, gate, and the HTTP skin over `AdminService`.

`compose(inner, ...)` is the ONE function `transport_http` calls. It returns the inner app
untouched-in-behavior with an outermost branch in front of it:

- not configured (`$STIGMERGY_ADMIN_TOKEN_HASH` unset) → every `/admin*` path answers a plain 404
  and NO admin object exists: no routes, no service, no DDL. The MCP surface cannot tell this
  module is installed.
- configured → `/admin*` routes into the admin app; everything else (lifespan included — the MCP
  session manager lives on it) flows to the inner app exactly as before.

This is deliberately NOT an exemption inside `_BearerAuthMiddleware`: the webhook's "ONE
exemption, exact path match" doctrine stays intact because the admin surface never reaches that
middleware at all — it is a sibling branch with its own fail-closed gate (ADR 029).

The gate, outermost-in on the admin side: foreign `Host` → 421 (when `$STIGMERGY_PUBLIC_HOST` is
configured — the transport's own allowlist, mirrored); `/admin/api/*` without the admin bearer
token → the generic 401; every response carries the CSP and its sibling headers, static assets
included.
"""
import json
import logging
import os
import pathlib

from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from stigmergy.admin import auth
from stigmergy.admin.github import ActionsError, ActionsGateway
from stigmergy.admin.schema import ensure_admin_schema
from stigmergy.admin.service import (
    AdminBadRequest,
    AdminNotFound,
    AdminRefused,
    AdminService,
)
from stigmergy.admin.settings import AdminSettings
from stigmergy.gardener.schema import ensure_gardener_schema
from stigmergy.server import review

log = logging.getLogger(__name__)

ADMIN_PREFIX = "/admin"
API_PREFIX = "/admin/api/"

STATIC_DIR = pathlib.Path(__file__).parent / "static"

# Everything self, nothing inline, nothing remote — the SPA is plain files served by this same
# process, so `'self'` covers all of it and any external fetch is a bug by definition.
_CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'")

_UNAUTHORIZED = {"error": "unauthorized"}
_NOT_FOUND = {"error": "not found"}
_MISDIRECTED = {"error": "misdirected request"}


def compose(inner, *, conn, server_settings, admin_settings: AdminSettings | None = None,
            gateway=None):
    """Build the branch. `admin_settings`/`gateway` are injectable for tests; production resolves
    both from the environment (a malformed token hash raises `StartupError` here, at startup —
    fail closed and loudly, the token store's own posture)."""
    settings = admin_settings if admin_settings is not None else AdminSettings.from_env()
    if not settings.configured():
        return _Branch(inner, None)

    # The console's own table, plus the two schemas its read paths depend on and would otherwise
    # meet as a bare UndefinedTable on a fresh database (the gardener CLI's own lesson):
    # `gardener_findings` for the findings tab, `review_decisions` for the digest's own reads.
    # `ensure_capture_schema`/`ensure_audit_table` already ran in `build_http_app`.
    ensure_admin_schema(conn)
    ensure_gardener_schema(conn)
    review.ensure_review_schema(conn)

    if gateway is None and settings.github_configured():
        gateway = ActionsGateway(settings.github_token, settings.github_repo)
    service = AdminService(conn, server_settings=server_settings, admin_settings=settings,
                           gateway=gateway)
    public_hosts = _public_hosts_from_env()
    admin_app = _AdminGate(_build_admin_app(service), settings, public_hosts)
    return _Branch(inner, admin_app)


def _public_hosts_from_env() -> list[str]:
    """`$STIGMERGY_PUBLIC_HOST`, comma-separated, trimmed — a deliberate local copy of
    `transport_http._public_hosts_from_env`: importing it would make this module import the very
    module that imports this one (the composition point), and two lines of parsing do not earn a
    cycle."""
    raw = os.environ.get("STIGMERGY_PUBLIC_HOST", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


class _Branch:
    """Outermost ASGI: `/admin*` HTTP requests go right (404 when unconfigured), everything else
    — lifespan, websockets, every other path — flows to the inner app untouched.

    Attribute access is DELEGATED to the inner app: `tests/server/conftest.rate_limiter_of` /
    `evidence_store_of` introspect `app.user_middleware` on what `build_http_app` returns, and
    wrapping the app must not break that seam (or any other Starlette attribute a caller relies
    on) — the branch is a router, not a new application object."""

    def __init__(self, inner, admin_app):
        self._inner = inner
        self._admin = admin_app

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _is_admin_path(scope.get("path", "")):
            await self._inner(scope, receive, send)
            return
        if self._admin is None:
            await JSONResponse(_NOT_FOUND, status_code=404)(scope, receive, send)
            return
        await self._admin(scope, receive, send)


def _is_admin_path(path: str) -> bool:
    return path == ADMIN_PREFIX or path.startswith(ADMIN_PREFIX + "/")


class _AdminGate:
    """Host check → API token check → security headers on every response. Raw ASGI for the same
    reason `_BearerAuthMiddleware` is: the check and the downstream app run in one coroutine,
    and the header injection wraps `send` with no task hand-off."""

    def __init__(self, app, settings: AdminSettings, public_hosts: list[str]):
        self._app = app
        self._settings = settings
        self._public_hosts = public_hosts

    async def __call__(self, scope, receive, send):
        headers = scope.get("headers") or []
        if not auth.host_allowed(headers, self._public_hosts):
            log.warning("admin request refused: Host header not allowlisted")
            await self._respond(JSONResponse(_MISDIRECTED, status_code=421), scope, receive, send)
            return
        if scope.get("path", "").startswith(API_PREFIX):
            token = auth.bearer_token(headers)
            if not auth.token_matches(self._settings.token_hash, token):
                log.warning("admin auth refused (path=%s)", scope.get("path", ""))
                await self._respond(JSONResponse(_UNAUTHORIZED, status_code=401),
                                    scope, receive, send)
                return
        await self._app(scope, receive, self._sending(scope, send))

    async def _respond(self, response, scope, receive, send):
        await response(scope, receive, self._sending(scope, send))

    def _sending(self, scope, send):
        api = scope.get("path", "").startswith(API_PREFIX)

        async def send_with_headers(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"content-security-policy", _CSP.encode("ascii")))
                headers.append((b"x-content-type-options", b"nosniff"))
                headers.append((b"referrer-policy", b"no-referrer"))
                if api:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        return send_with_headers


# ── the HTTP skin ─────────────────────────────────────────────────────────────────────────────
def _json_endpoint(fn):
    """Wrap one service call: domain exceptions become their status codes with the library's own
    sentence; anything unexpected becomes a 500 naming the CLASS only — a raised message can carry
    captured content, so it never crosses this boundary."""

    async def handler(request):
        try:
            return JSONResponse(await fn(request))
        except AdminBadRequest as ex:
            return JSONResponse({"error": str(ex)}, status_code=400)
        except AdminNotFound as ex:
            return JSONResponse({"error": str(ex)}, status_code=404)
        except AdminRefused as ex:
            return JSONResponse({"error": str(ex)}, status_code=409)
        except ActionsError as ex:
            return JSONResponse({"error": str(ex)}, status_code=502)
        except Exception as ex:  # noqa: BLE001 — the class name is the whole disclosure
            log.exception("admin endpoint failed (path=%s)", request.url.path)
            return JSONResponse(
                {"error": f"the operation failed ({ex.__class__.__name__})"}, status_code=500)

    return handler


async def _body(request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as ex:
        raise AdminBadRequest("request body must be valid JSON") from ex
    if not isinstance(data, dict):
        raise AdminBadRequest("request body must be a JSON object")
    return data


def _str(data: dict, key: str, *, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise AdminBadRequest(f"{key!r} must be a string")
    return value


def _build_admin_app(service: AdminService) -> Starlette:
    async def index(_request):
        return FileResponse(STATIC_DIR / "index.html")

    async def root(_request):
        return RedirectResponse(url=ADMIN_PREFIX + "/", status_code=307)

    @_json_endpoint
    async def meta(_request):
        return service.meta()

    @_json_endpoint
    async def overview(_request):
        return service.overview()

    @_json_endpoint
    async def queue_list(request):
        statuses = request.query_params.getlist("status")
        submitter = request.query_params.get("submitter") or None
        try:
            limit = int(request.query_params.get("limit", "50"))
        except ValueError as ex:
            raise AdminBadRequest("'limit' must be an integer") from ex
        return service.queue_list(statuses=statuses or None, submitter=submitter, limit=limit)

    @_json_endpoint
    async def queue_show(request):
        return service.queue_show(request.path_params["id"])

    @_json_endpoint
    async def queue_requeue(request):
        data = await _body(request)
        return service.queue_requeue(request.path_params["id"], actor=_str(data, "actor"),
                                     note=_str(data, "note"))

    @_json_endpoint
    async def queue_resolve(request):
        data = await _body(request)
        note = _str(data, "note")
        if not note.strip():
            raise AdminBadRequest("'note' is required — it is the submitter's whole report of "
                                  "what happened to their material")
        return service.queue_resolve(request.path_params["id"], actor=_str(data, "actor"),
                                     note=note, page=_str(data, "page"),
                                     commit=_str(data, "commit"))

    @_json_endpoint
    async def queue_reject(request):
        data = await _body(request)
        reason = _str(data, "reason")
        if not reason.strip():
            raise AdminBadRequest("'reason' is required — it reaches the submitter verbatim")
        return service.queue_reject(request.path_params["id"], actor=_str(data, "actor"),
                                    reason=reason)

    @_json_endpoint
    async def queue_reclaim(request):
        data = await _body(request)
        timeout = data.get("visibility_timeout_s")
        if timeout is not None and not isinstance(timeout, int):
            raise AdminBadRequest("'visibility_timeout_s' must be an integer")
        return service.queue_reclaim(actor=_str(data, "actor"), visibility_timeout_s=timeout)

    @_json_endpoint
    async def queue_purge(request):
        data = await _body(request)
        days = data.get("older_than_days")
        if days is not None and not isinstance(days, int):
            raise AdminBadRequest("'older_than_days' must be an integer")
        kwargs = {} if days is None else {"older_than_days": days}
        return service.queue_purge(actor=_str(data, "actor"),
                                   dry_run=bool(data.get("dry_run", False)), **kwargs)

    @_json_endpoint
    async def gardener(_request):
        return service.gardener_state()

    @_json_endpoint
    async def digest(_request):
        return service.digest_state()

    @_json_endpoint
    async def digest_preview(_request):
        return await service.digest_preview()

    @_json_endpoint
    async def digest_post(request):
        data = await _body(request)
        return await service.digest_post(actor=_str(data, "actor"))

    @_json_endpoint
    async def index_state(_request):
        return service.index_state()

    @_json_endpoint
    async def index_check(_request):
        return service.index_substrate_check()

    @_json_endpoint
    async def entities_list(_request):
        return {"situations": service.entities_list()}

    @_json_endpoint
    async def entities_show(request):
        return service.entities_show(request.path_params["id"])

    @_json_endpoint
    async def entities_approve(request):
        data = await _body(request)
        requeue = data.get("requeue", True)
        if not isinstance(requeue, bool):
            raise AdminBadRequest("'requeue' must be a boolean")
        return service.entity_approve(
            request.path_params["id"], actor=_str(data, "actor"), name=_str(data, "name"),
            entity_type=_str(data, "entity_type"), entity_id=_str(data, "entity_id"),
            aliases=_str(data, "aliases"), role=_str(data, "role"), requeue=requeue)

    @_json_endpoint
    async def activity(_request):
        return service.activity()

    @_json_endpoint
    async def worker(_request):
        return service.worker_status()

    @_json_endpoint
    async def crons(_request):
        return service.crons_state()

    @_json_endpoint
    async def cron_dispatch(request):
        data = await _body(request)
        inputs = data.get("inputs") or {}
        if not isinstance(inputs, dict):
            raise AdminBadRequest("'inputs' must be a JSON object")
        return service.cron_dispatch(request.path_params["workflow_file"],
                                     actor=_str(data, "actor"), inputs=inputs)

    @_json_endpoint
    async def cron_enable(request):
        data = await _body(request)
        return service.cron_set_enabled(request.path_params["workflow_file"],
                                        actor=_str(data, "actor"), enabled=True)

    @_json_endpoint
    async def cron_disable(request):
        data = await _body(request)
        return service.cron_set_enabled(request.path_params["workflow_file"],
                                        actor=_str(data, "actor"), enabled=False)

    routes = [
        Route(ADMIN_PREFIX, root, methods=["GET"]),
        Route(ADMIN_PREFIX + "/", index, methods=["GET"]),
        Mount(ADMIN_PREFIX + "/assets", StaticFiles(directory=STATIC_DIR / "assets")),
        Route(API_PREFIX + "meta", meta, methods=["GET"]),
        Route(API_PREFIX + "overview", overview, methods=["GET"]),
        Route(API_PREFIX + "queue", queue_list, methods=["GET"]),
        Route(API_PREFIX + "queue/reclaim", queue_reclaim, methods=["POST"]),
        Route(API_PREFIX + "queue/purge", queue_purge, methods=["POST"]),
        Route(API_PREFIX + "queue/{id:int}", queue_show, methods=["GET"]),
        Route(API_PREFIX + "queue/{id:int}/requeue", queue_requeue, methods=["POST"]),
        Route(API_PREFIX + "queue/{id:int}/resolve", queue_resolve, methods=["POST"]),
        Route(API_PREFIX + "queue/{id:int}/reject", queue_reject, methods=["POST"]),
        Route(API_PREFIX + "gardener", gardener, methods=["GET"]),
        Route(API_PREFIX + "digest", digest, methods=["GET"]),
        Route(API_PREFIX + "digest/preview", digest_preview, methods=["POST"]),
        Route(API_PREFIX + "digest/post", digest_post, methods=["POST"]),
        Route(API_PREFIX + "index", index_state, methods=["GET"]),
        Route(API_PREFIX + "index/check", index_check, methods=["POST"]),
        Route(API_PREFIX + "entities", entities_list, methods=["GET"]),
        Route(API_PREFIX + "entities/{id:int}", entities_show, methods=["GET"]),
        Route(API_PREFIX + "entities/{id:int}/approve", entities_approve, methods=["POST"]),
        Route(API_PREFIX + "activity", activity, methods=["GET"]),
        Route(API_PREFIX + "worker", worker, methods=["GET"]),
        Route(API_PREFIX + "crons", crons, methods=["GET"]),
        Route(API_PREFIX + "crons/{workflow_file}/dispatch", cron_dispatch, methods=["POST"]),
        Route(API_PREFIX + "crons/{workflow_file}/enable", cron_enable, methods=["POST"]),
        Route(API_PREFIX + "crons/{workflow_file}/disable", cron_disable, methods=["POST"]),
    ]
    return Starlette(routes=routes)
