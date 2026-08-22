"""The `/admin` branch — composition, gate, and the HTTP skin over `AdminService`.

`compose(inner, ...)` is the ONE function `transport_http` calls. Unconfigured
(`$STIGMERGY_ADMIN_TOKEN_HASH` unset), every `/admin*` path answers a plain 404 and no admin
object exists — no routes, no service, no DDL. Configured, `/admin*` routes into the admin app as
a sibling BRANCH in front of `_BearerAuthMiddleware`, never an exemption inside it; everything
else (lifespan included) flows to the inner app untouched.

Gate order on the admin side: foreign `Host` -> 421, `/admin/api/*` without the admin bearer
token -> generic 401, security headers on every response, static assets included.

**The handlers that touch the knowledge repo and `metrics` run their service call in a worker
thread** (`run_in_threadpool`), and they are the only ones that do. A repair approve and a page
removal clone the repo, run the nine gates — `git` and `gitleaks` subprocesses — and push: seconds
of blocking work, on the event loop of a process that is also serving the MCP tools; `metrics` is a
dozen aggregate queries the dashboard polls. The rejects stay inline because each is one
statement. `AdminService`'s "no cursor across an `await`" invariant is untouched: the whole
synchronous call happens inside the one thread, and the connection is the same autocommit one
`slack.review` already reaches through `asyncio.to_thread`.
"""
import json
import logging
import os
import pathlib

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from stigmergy.admin import auth
from stigmergy.admin.github import ActionsError, ActionsGateway
from stigmergy.admin.schema import ensure_admin_schema
from stigmergy.admin.service import (
    DEFAULT_METRICS_DAYS,
    AdminBadRequest,
    AdminNotFound,
    AdminRefused,
    AdminService,
)
from stigmergy.admin.settings import AdminSettings
from stigmergy.gardener.schema import ensure_gardener_schema
from stigmergy.repair.schema import ensure_repair_schema

log = logging.getLogger(__name__)

ADMIN_PREFIX = "/admin"
API_PREFIX = "/admin/api/"

STATIC_DIR = pathlib.Path(__file__).parent / "static"

# Everything self, nothing inline, nothing remote — any external fetch is a bug by definition.
_CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'")

_UNAUTHORIZED = {"error": "unauthorized"}
_NOT_FOUND = {"error": "not found"}
_MISDIRECTED = {"error": "misdirected request"}


def compose(inner, *, conn, server_settings, admin_settings: AdminSettings | None = None,
            gateway=None, evidence=None):
    """Build the branch. `admin_settings`/`gateway` are injectable for tests; production
    resolves both from the environment (a malformed token hash raises `StartupError` at startup —
    fail closed and loudly)."""
    settings = admin_settings if admin_settings is not None else AdminSettings.from_env()
    if not settings.configured():
        return _Branch(inner, None)

    # The console's own table, plus the two schemas its read paths would otherwise meet as a bare
    # UndefinedTable on a fresh database. `ensure_capture_schema`/`ensure_audit_table` already
    # ran in `build_http_app`.
    ensure_admin_schema(conn)
    ensure_gardener_schema(conn)
    ensure_repair_schema(conn)

    if gateway is None and settings.github_configured():
        gateway = ActionsGateway(settings.github_token, settings.github_repo)
    service = AdminService(conn, server_settings=server_settings, admin_settings=settings,
                           gateway=gateway, evidence=evidence)
    public_hosts = _public_hosts_from_env()
    admin_app = _AdminGate(_build_admin_app(service), settings, public_hosts)
    return _Branch(inner, admin_app)


def _public_hosts_from_env() -> list[str]:
    """`$STIGMERGY_PUBLIC_HOST`, comma-separated, trimmed — a deliberate local copy of
    `transport_http._public_hosts_from_env`: importing it would close a cycle through the
    composition point."""
    raw = os.environ.get("STIGMERGY_PUBLIC_HOST", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


class _Branch:
    """Outermost ASGI: `/admin*` HTTP requests go right (404 when unconfigured); everything else
    — lifespan, websockets, every other path — flows to the inner app untouched. Attribute access
    DELEGATES to the inner app: test conftests introspect `app.user_middleware`, and wrapping
    must not break that seam — the branch is a router, not a new application object."""

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
    """Host check -> API token check -> security headers on every response. Raw ASGI: the check
    and the downstream app run in one coroutine, the header injection wraps `send` with no task
    hand-off."""

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
                # The admin token is typed into a form on this origin, and `force_https` only
                # redirects — the first request of a session can still leave over http; HSTS is
                # what stops there being a first time.
                headers.append((b"strict-transport-security",
                                b"max-age=31536000; includeSubDomains"))
                if api:
                    headers.append((b"cache-control", b"no-store"))
                else:
                    # The shell and its modules: revalidate on every load (an ETag round trip),
                    # never the heuristic freshness a header-less response gets — a deploy that
                    # renames or splits a module would otherwise leave a browser running the old
                    # `app.js` against new imports for hours, which renders as a blank page.
                    headers.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": headers}
            await send(message)

        return send_with_headers


# ── the HTTP skin ─────────────────────────────────────────────────────────────────────────────
def _json_endpoint(fn):
    """Wrap one service call: domain exceptions become their status codes with the library's own
    sentence; anything unexpected becomes a 500 naming the CLASS only — a raised message can
    carry captured content, so it never crosses this boundary."""

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
    async def metrics(request):
        try:
            days = int(request.query_params.get("days", str(DEFAULT_METRICS_DAYS)))
        except ValueError as ex:
            raise AdminBadRequest("'days' must be an integer") from ex
        # A dozen aggregate queries, polled by the dashboard: off the event loop, like the two
        # Approve handlers — the MCP tools share this process. The service holds no cursor
        # across the call boundary, so the autocommit connection is safe to use from the thread.
        return await run_in_threadpool(service.metrics, days=days)

    @_json_endpoint
    async def entities_registry(_request):
        return service.entities_registry()

    @_json_endpoint
    async def entities_resolve(request):
        data = await _body(request)
        return service.entities_resolve(data.get("names"))

    @_json_endpoint
    async def entities_create(request):
        data = await _body(request)
        return await run_in_threadpool(
            service.entity_create, actor=_str(data, "actor"), name=_str(data, "name"),
            entity_type=_str(data, "entity_type"), about=_str(data, "about"),
            entity_id=_str(data, "entity_id"), aliases=_str(data, "aliases"))

    @_json_endpoint
    async def repairs_list(_request):
        return service.repairs_list()

    @_json_endpoint
    async def repairs_show(request):
        return service.repair_show(request.path_params["id"])

    @_json_endpoint
    async def pages_delete(request):
        data = await _body(request)
        paths = [p for p in (data.get("paths") or []) if str(p).strip()]
        if not paths:
            raise AdminBadRequest("'paths' is required — a deletion names the pages that go")
        return await run_in_threadpool(service.pages_delete, actor=_str(data, "actor"),
                                       paths=[str(p).strip() for p in paths],
                                       why=_str(data, "why"))

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
        Route(API_PREFIX + "gardener", gardener, methods=["GET"]),
        Route(API_PREFIX + "digest", digest, methods=["GET"]),
        Route(API_PREFIX + "digest/preview", digest_preview, methods=["POST"]),
        Route(API_PREFIX + "digest/post", digest_post, methods=["POST"]),
        Route(API_PREFIX + "index", index_state, methods=["GET"]),
        Route(API_PREFIX + "index/check", index_check, methods=["POST"]),
        Route(API_PREFIX + "metrics", metrics, methods=["GET"]),
        Route(API_PREFIX + "entities/registry", entities_registry, methods=["GET"]),
        Route(API_PREFIX + "entities/resolve", entities_resolve, methods=["POST"]),
        Route(API_PREFIX + "entities/create", entities_create, methods=["POST"]),
        Route(API_PREFIX + "repairs", repairs_list, methods=["GET"]),
        Route(API_PREFIX + "repairs/{id:int}", repairs_show, methods=["GET"]),
        Route(API_PREFIX + "pages/delete", pages_delete, methods=["POST"]),
        Route(API_PREFIX + "activity", activity, methods=["GET"]),
        Route(API_PREFIX + "worker", worker, methods=["GET"]),
        Route(API_PREFIX + "crons", crons, methods=["GET"]),
        Route(API_PREFIX + "crons/{workflow_file}/dispatch", cron_dispatch, methods=["POST"]),
        Route(API_PREFIX + "crons/{workflow_file}/enable", cron_enable, methods=["POST"]),
        Route(API_PREFIX + "crons/{workflow_file}/disable", cron_disable, methods=["POST"]),
    ]
    return Starlette(routes=routes)
