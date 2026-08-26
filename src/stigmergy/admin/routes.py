from __future__ import annotations

import functools
import json
import logging
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from stigmergy.admin import auth
from stigmergy.admin.schema import ensure_admin_schema
from stigmergy.admin.service import (
    AdminBadRequest,
    AdminNotFound,
    AdminRefused,
    AdminService,
)
from stigmergy.admin.settings import AdminSettings
from stigmergy.capture.errors import CaptureError
from stigmergy.capture.schema import MAX_ARTIFACT_BYTES
from stigmergy.kernel.blocking import run_blocking

log = logging.getLogger(__name__)

ADMIN_PREFIX = "/admin"
API_PREFIX = "/admin/api/"
STATIC_DIR = Path(__file__).parent / "static"
MAX_ADMIN_BODY_BYTES = MAX_ARTIFACT_BYTES + 2 * 1024 * 1024

_CSP = (
    "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


def compose(
    inner,
    *,
    conn,
    connection_factory=None,
    server_settings,
    admin_settings: AdminSettings | None = None,
    evidence=None,
):
    settings = admin_settings if admin_settings is not None else AdminSettings.from_env()
    if not settings.configured():
        return _Branch(inner, None)
    ensure_admin_schema(conn)
    if connection_factory is None:
        service = AdminService(
            conn,
            server_settings=server_settings,
            admin_settings=settings,
            evidence=evidence,
        )
    else:
        service = _AdminServiceProxy(
            connection_factory,
            server_settings=server_settings,
            admin_settings=settings,
            evidence=evidence,
        )
    app = _AdminGate(_build_admin_app(service), settings, _public_hosts_from_env())
    return _Branch(inner, app)


class _AdminServiceProxy:
    def __init__(self, connection_factory, **service_kwargs):
        self._connection_factory = connection_factory
        self._service_kwargs = service_kwargs

    def __getattr__(self, name):
        def call(*args, **kwargs):
            conn = self._connection_factory()
            try:
                method = getattr(AdminService(conn, **self._service_kwargs), name)
                return method(*args, **kwargs)
            finally:
                conn.close()

        return call


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
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            path == ADMIN_PREFIX or path.startswith(f"{ADMIN_PREFIX}/")
        ):
            await self._inner(scope, receive, send)
            return
        if self._admin is None:
            await JSONResponse({"error": "not found"}, status_code=404)(scope, receive, send)
            return
        await self._admin(scope, receive, send)


class _AdminGate:
    def __init__(self, app, settings: AdminSettings, public_hosts: list[str]):
        self._app = app
        self._settings = settings
        self._public_hosts = public_hosts

    async def __call__(self, scope, receive, send):
        headers = scope.get("headers") or []
        if not auth.host_allowed(headers, self._public_hosts):
            await self._respond(
                JSONResponse({"error": "misdirected request"}, status_code=421),
                scope,
                receive,
                send,
            )
            return
        path = scope.get("path", "")
        if path.startswith(API_PREFIX):
            token = auth.bearer_token(headers)
            if not auth.token_matches(self._settings.token_hash, token):
                await self._respond(
                    JSONResponse({"error": "unauthorized"}, status_code=401),
                    scope,
                    receive,
                    send,
                )
                return
            length = _content_length(headers)
            if length is not None and length > MAX_ADMIN_BODY_BYTES:
                await self._respond(
                    JSONResponse({"error": "request too large"}, status_code=413),
                    scope,
                    receive,
                    send,
                )
                return
        bounded_receive = _bounded_receive(receive) if path.startswith(API_PREFIX) else receive
        await self._app(scope, bounded_receive, self._sending(scope, send))

    async def _respond(self, response, scope, receive, send):
        await response(scope, receive, self._sending(scope, send))

    def _sending(self, scope, send):
        api = scope.get("path", "").startswith(API_PREFIX)

        async def secured(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.extend(
                    (
                        (b"content-security-policy", _CSP.encode("ascii")),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
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


def _bounded_receive(receive):
    seen = 0

    async def bounded():
        nonlocal seen
        message = await receive()
        if message.get("type") == "http.request":
            seen += len(message.get("body") or b"")
            if seen > MAX_ADMIN_BODY_BYTES:
                raise _BodyTooLarge
        return message

    return bounded


def _endpoint(function):
    async def handler(request):
        try:
            return JSONResponse(await function(request))
        except _BodyTooLarge:
            return JSONResponse({"error": "request too large"}, status_code=413)
        except AdminBadRequest as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        except CaptureError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        except AdminNotFound as error:
            return JSONResponse({"error": str(error)}, status_code=404)
        except AdminRefused as error:
            return JSONResponse({"error": str(error)}, status_code=409)
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
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
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


async def _service_call(method, /, *args, **kwargs):
    return await run_blocking(functools.partial(method, *args, **kwargs))


def _audience_from_form(value):
    if value in (None, "", "null"):
        return None
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise AdminBadRequest("audience must be a JSON list or null") from error
    return parsed


def _build_admin_app(service: AdminService) -> Starlette:
    async def index(_request):
        return FileResponse(STATIC_DIR / "index.html")

    async def root(_request):
        return RedirectResponse(f"{ADMIN_PREFIX}/", status_code=307)

    @_endpoint
    async def meta(_request):
        return await _service_call(service.meta)

    @_endpoint
    async def overview(_request):
        return await _service_call(service.overview)

    @_endpoint
    async def captures(request):
        statuses = request.query_params.getlist("status") or None
        submitter = request.query_params.get("submitter") or None
        return await _service_call(
            service.captures,
            statuses=statuses,
            submitter=submitter,
            limit=_integer(request.query_params.get("limit", "50"), "limit"),
            offset=_integer(request.query_params.get("offset", "0"), "offset"),
        )

    @_endpoint
    async def capture_detail(request):
        return await _service_call(service.capture, request.path_params["capture_id"])

    @_endpoint
    async def capture_retry(request):
        return await _service_call(service.retry_capture, request.path_params["capture_id"])

    @_endpoint
    async def capture_text(request):
        data = await _json(request)
        return await _service_call(
            service.submit_text,
            text=_text(data.get("text"), "text", required=True),
            title=_text(data.get("title"), "title"),
            occurred_at=_text(data.get("occurred_at"), "occurred_at"),
            audience=data.get("audience"),
            idempotency_key=_text(data.get("idempotency_key"), "idempotency_key") or None,
        )

    @_endpoint
    async def capture_url(request):
        data = await _json(request)
        return await _service_call(
            service.submit_url,
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
        return await _service_call(
            service.submit_file,
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
        return await _service_call(
            service.changes,
            trigger=request.query_params.get("trigger") or None,
            limit=_integer(request.query_params.get("limit", "50"), "limit"),
            offset=_integer(request.query_params.get("offset", "0"), "offset"),
        )

    @_endpoint
    async def change_detail(request):
        return await _service_call(service.change, request.path_params["change_id"])

    @_endpoint
    async def contradictions_list(_request):
        return await _service_call(service.contradictions)

    @_endpoint
    async def contradiction_resolve(request):
        data = await _json(request)
        return await _service_call(
            service.resolve_contradiction,
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
        return await _service_call(
            service.resolve_contradiction,
            contradiction_id=_text(form.get("contradiction_id"), "contradiction_id", required=True),
            decision=_text(form.get("decision"), "decision", required=True),
            resolution=_text(form.get("resolution"), "resolution", required=True),
            rationale=_text(form.get("rationale"), "rationale", required=True),
            support_file=support,
        )

    @_endpoint
    async def entities(_request):
        return await _service_call(service.entities)

    @_endpoint
    async def entity_operation(request):
        data = await _json(request)
        entity_ids = data.get("entity_ids")
        if not isinstance(entity_ids, list):
            raise AdminBadRequest("entity_ids must be a list")
        evidence = data.get("evidence")
        if evidence is not None and not isinstance(evidence, dict):
            raise AdminBadRequest("evidence must be an object")
        return await _service_call(
            service.entity_operation,
            action=_text(data.get("action"), "action", required=True),
            entity_ids=entity_ids,
            rationale=_text(data.get("rationale"), "rationale", required=True),
            evidence=evidence,
        )

    @_endpoint
    async def delete_pages(request):
        data = await _json(request)
        paths = data.get("paths")
        if not isinstance(paths, list):
            raise AdminBadRequest("paths must be a list")
        return await _service_call(
            service.delete_pages,
            paths=paths,
            rationale=_text(data.get("rationale"), "rationale", required=True),
        )

    @_endpoint
    async def gardener(_request):
        return await _service_call(service.gardener)

    @_endpoint
    async def gardener_trigger(request):
        data = await _json(request)
        return await _service_call(
            service.trigger_garden,
            rationale=_text(data.get("rationale"), "rationale"),
        )

    @_endpoint
    async def index_state(_request):
        return await _service_call(service.index_state)

    @_endpoint
    async def worker(_request):
        return await _service_call(service.worker_status)

    @_endpoint
    async def activity(_request):
        return await _service_call(service.activity)

    routes = [
        Route(ADMIN_PREFIX, root, methods=["GET"]),
        Route(f"{ADMIN_PREFIX}/", index, methods=["GET"]),
        Mount(f"{ADMIN_PREFIX}/assets", StaticFiles(directory=STATIC_DIR / "assets")),
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
        Route(f"{API_PREFIX}entities/operation", entity_operation, methods=["POST"]),
        Route(f"{API_PREFIX}knowledge/delete", delete_pages, methods=["POST"]),
        Route(f"{API_PREFIX}gardener", gardener, methods=["GET"]),
        Route(f"{API_PREFIX}gardener/trigger", gardener_trigger, methods=["POST"]),
        Route(f"{API_PREFIX}index", index_state, methods=["GET"]),
        Route(f"{API_PREFIX}worker", worker, methods=["GET"]),
        Route(f"{API_PREFIX}activity", activity, methods=["GET"]),
    ]
    return Starlette(routes=routes)


def _integer(value: str, field: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise AdminBadRequest(f"{field} must be an integer") from error
