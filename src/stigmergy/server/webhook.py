"""Authenticated GitHub push webhook for incremental index convergence."""
import hashlib
import hmac
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace

from starlette.requests import Request
from starlette.responses import JSONResponse

from stigmergy.capture import ops
from stigmergy.index import build, corpus, health, measures, store
from stigmergy.kernel import layers, tenancy
from stigmergy.kernel.blocking import run_blocking
from stigmergy.librarian import githubapp
from stigmergy.librarian.errors import LibrarianConfigError
from stigmergy.server import platform

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/webhook/github"

WEBHOOK_SECRET_ENV = "STIGMERGY_GITHUB_WEBHOOK_SECRET"
WEBHOOK_BRANCH_ENV = "STIGMERGY_GITHUB_BRANCH"
WEBHOOK_FILE_CAP_ENV = "STIGMERGY_GITHUB_WEBHOOK_FILE_CAP"
DEFAULT_BRANCH = "main"
DEFAULT_FILE_CAP = 50

JOB_NAME = "webhook-index-upsert"

_UNAUTHORIZED_BODY = {"error": "unauthorized"}


@dataclass(frozen=True)
class WebhookSettings:
    """Webhook authentication; `repo` is the pushed tenant repository a delivery is applied to."""
    secret: str = ""
    repo: str = ""
    branch: str = DEFAULT_BRANCH
    file_cap: int = DEFAULT_FILE_CAP


def _parse_file_cap(cap_raw: str) -> int:
    """Parse a positive file cap or use the bounded default."""
    if not cap_raw:
        return DEFAULT_FILE_CAP
    if cap_raw.isdigit() and int(cap_raw) > 0:
        return int(cap_raw)
    log.warning("webhook: %s=%r is not a valid positive integer file cap — falling back to the "
               "default of %d", WEBHOOK_FILE_CAP_ENV, cap_raw, DEFAULT_FILE_CAP)
    return DEFAULT_FILE_CAP


def webhook_settings_from_env(env: dict | None = None) -> WebhookSettings:
    env = os.environ if env is None else env
    return WebhookSettings(
        secret=env.get(WEBHOOK_SECRET_ENV, ""),
        branch=env.get(WEBHOOK_BRANCH_ENV) or DEFAULT_BRANCH,
        file_cap=_parse_file_cap(env.get(WEBHOOK_FILE_CAP_ENV, "")),
    )


def verify_signature(secret: str, raw_body: bytes, header_value: str | None) -> bool:
    """Verify the raw request body with a constant-time HMAC check."""
    if not secret or not header_value:
        return False
    scheme, _, digest = header_value.partition("=")
    if scheme != "sha256" or not digest:
        return False
    if not digest.isascii():
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest)


def changed_paths_from_push(payload: dict) -> dict[str, str]:
    """Collapse a push to each path's final status."""
    status: dict[str, str] = {}
    for commit in payload.get("commits") or []:
        for path in commit.get("added") or []:
            status[path] = "added"
        for path in commit.get("modified") or []:
            status[path] = "modified"
        for path in commit.get("removed") or []:
            status[path] = "removed"
    return status


def in_zone_changes(changes: dict[str, str]) -> dict[str, str]:
    """Select the same indexable paths used by a full corpus rebuild."""
    return {path: status for path, status in changes.items() if corpus.is_indexable_page(path)}


def push_commit_list_complete(payload: dict) -> bool:
    reported = payload.get("size")
    if reported is None:
        return True
    return isinstance(reported, int) and not isinstance(reported, bool) and reported >= 0 and (
        reported <= len(payload.get("commits") or [])
    )


def _fetch_file_content(repo_slug: str, path: str, ref: str, token: str, *, opener=None) -> str:
    """Fetch one file as raw UTF-8 text through GitHub's Contents API."""
    url = (f"https://api.github.com/repos/{repo_slug}/contents/{urllib.parse.quote(path)}"
          f"?ref={urllib.parse.quote(ref)}")
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github.raw+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "stigmergy-server-webhook"})
    with (opener or urllib.request.urlopen)(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def _resolve_outbound_links(conn, rows: list, deleted: set[str]) -> None:
    """Resolve outbound links against the candidate index snapshot."""
    paths = (set(store.existing_paths(conn)) - deleted) | {row.path for row in rows}
    by_stem = corpus.by_stem_index(sorted(paths))
    for r in rows:
        r.links = corpus.resolve_links(r.path, r.links, by_stem)


def _refresh_link_graph(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute("SELECT path, body FROM pages_index ORDER BY path")
        pages = cursor.fetchall()
    by_stem = corpus.by_stem_index([path for path, _body in pages])
    links = {
        path: corpus.resolve_links(path, corpus.link_targets(body), by_stem)
        for path, body in pages
    }
    inbound: dict[str, set[str]] = {}
    for path, targets in links.items():
        for target in targets:
            inbound.setdefault(target, set()).add(layers.virtual_path(path))
    with conn.cursor() as cursor:
        for path in links:
            cursor.execute(
                "UPDATE pages_index SET links = %s, inlinks = %s WHERE path = %s",
                (links[path], len(inbound.get(layers.virtual_path(path), ())), path),
            )


def process_push(conn, embedder, payload: dict, settings: WebhookSettings, *,
                 delivery_id: str = "", opener=None) -> dict:
    """Apply a bounded push atomically or defer it to the full rebuild."""
    sha = payload.get("after") or (payload.get("head_commit") or {}).get("id") or ""
    before = payload.get("before")
    raw_changes = changed_paths_from_push(payload)
    changes = in_zone_changes(raw_changes)
    stats = {"sha": sha, "upserted": 0, "deleted": 0, "skipped": 0, "embedded": 0}

    state = health.read(conn)
    if sha and sha == state["indexed_commit_sha"]:
        with conn.transaction(), conn.cursor() as cursor:
            if delivery_id:
                store.record_delivery(cursor, delivery_id)
        stats["already_current"] = True
        return stats
    if state["dirty"]:
        return _defer(conn, stats, sha, delivery_id, "index_dirty")
    if not isinstance(before, str) or before != state["indexed_commit_sha"]:
        return _defer(conn, stats, sha, delivery_id, "non_linear_push")
    if not push_commit_list_complete(payload):
        stats["reported_commits"] = payload.get("size")
        stats["included_commits"] = len(payload.get("commits") or [])
        return _defer(conn, stats, sha, delivery_id, "truncated_push")
    if len(changes) > settings.file_cap:
        stats["changed_files"] = len(changes)
        log.warning("webhook: push %s touches %d in-zone file(s), above the cap of %d — "
                   "deferring to the nightly rebuild rather than a partial apply",
                   sha, len(changes), settings.file_cap)
        return _defer(conn, stats, sha, delivery_id, "overflow")

    to_delete = sorted(p for p, s in changes.items() if s == "removed")
    to_upsert_paths = sorted(p for p, s in changes.items() if s != "removed")

    rows: list = []
    pieces: list = []
    model = ""
    fts_config = "english"
    fresh: dict[str, list[float]] = {}
    skipped = 0
    if to_upsert_paths:
        token = githubapp.installation_token(permissions={"contents": "read"},
                                             repositories=[settings.repo.split("/", 1)[-1]])
        before_hashes = store.current_content_hashes(conn, to_upsert_paths)   # SELECT only
        for path in to_upsert_paths:
            zone = corpus.zone_of(path)
            text = _fetch_file_content(settings.repo, path, sha, token, opener=opener)  # network
            rows.append(corpus.page_row(path, zone, text))

        skipped = sum(1 for r in rows if before_hashes.get(r.path) == r.content_hash)
        _resolve_outbound_links(conn, rows, set(to_delete))   # SELECT only
        corpus.attach_source_dates(rows, store.source_dates(   # SELECT only
            conn, (source for r in rows for source in r.sources), exclude=to_delete))

        meta = store.read_meta(conn) or {}   # SELECT only
        model = meta.get("model") or getattr(embedder, "model", "")
        fts_config = meta.get("fts_config") or "english"

        # SELECT only, then the embedder over the network
        pieces, _cached, fresh = build.embed_fragments(conn, embedder, rows, model=model)

    with conn.transaction():
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT dirty, indexed_commit_sha FROM index_health "
                "WHERE singleton FOR UPDATE"
            )
            locked = cursor.fetchone()
        if not locked or locked[0] or locked[1] != before:
            health.mark_dirty(conn, sha)
            if delivery_id:
                with conn.cursor() as cursor:
                    store.record_delivery(cursor, delivery_id)
            stats.update({"deferred": True, "deferred_reason": "concurrent_push"})
            return stats
        if to_delete:
            stats["deleted"] = store.delete_pages(conn, to_delete)
        if to_upsert_paths:
            if fresh:
                store.store_embeddings(conn, model, fresh)
            store.upsert_pages(conn, rows, fts_config)
            store.replace_fragments(conn, [r.path for r in rows], pieces, model)
            store.ensure_audience_indexes(conn, {layers.layer_of_acl(r.acl) for r in rows})
            stats["upserted"] = len(rows)
        if to_delete or to_upsert_paths:
            _refresh_link_graph(conn)
        if delivery_id:
            with conn.cursor() as cur:
                store.record_delivery(cur, delivery_id)
        health.record_incremental(conn, sha, indexed_rows=store.page_count(conn))
    measures.record_snapshot(conn, sha)

    stats["skipped"] = skipped
    stats["embedded"] = len(fresh)
    return stats


def _defer(conn, stats: dict, sha: str, delivery_id: str, reason: str) -> dict:
    with conn.transaction():
        health.mark_dirty(conn, sha)
        if delivery_id:
            with conn.cursor() as cursor:
                store.record_delivery(cursor, delivery_id)
    stats.update({"deferred": True, "deferred_reason": reason})
    return stats


# This bearer-exempt route is bounded before HMAC verification.
MAX_BODY_BYTES = 1024 * 1024


async def _read_body_capped(request: Request, max_bytes: int) -> bytes | None:
    """Buffer at most ``max_bytes`` from the request stream."""
    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _process_webhook(conn, embedder, settings: WebhookSettings, *, raw_body: bytes,
                     signature: str | None, event: str, delivery_id: str) -> JSONResponse:
    """Authenticate and process buffered webhook bytes on a worker-owned connection."""
    if not verify_signature(settings.secret, raw_body, signature):
        log.warning("webhook: signature check failed")
        return JSONResponse(_UNAUTHORIZED_BODY, status_code=401)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.warning("webhook: signature verified but the body did not parse as JSON")
        return JSONResponse({"ok": True, "ignored": "unparseable body"})
    if not isinstance(payload, dict):
        log.warning("webhook: signature verified but the body is not a JSON object")
        return JSONResponse({"ok": True, "ignored": "body is not a JSON object"})

    if event != "push":
        return JSONResponse({"ok": True, "ignored": f"event={event!r}"})

    # The App's one webhook delivers every tenant repository's pushes; the repository names the
    # tenant, and the delivery is applied as that tenant alone.
    repo_full_name = (payload.get("repository") or {}).get("full_name", "")
    tenant = platform.tenant_by_repo(conn, repo_full_name) if repo_full_name else None
    if tenant is None or tenant.status == "suspended":
        return JSONResponse({"ok": True, "ignored": f"repository={repo_full_name!r}"})

    ref = payload.get("ref", "")
    if ref != f"refs/heads/{settings.branch}":
        return JSONResponse({"ok": True, "ignored": f"ref={ref!r}"})

    with tenancy.tenant_scope(conn, tenant.id):
        return _apply_push(conn, embedder, replace(settings, repo=tenant.repo), payload=payload,
                           delivery_id=delivery_id)


def _apply_push(conn, embedder, settings: WebhookSettings, *, payload: dict,
                delivery_id: str) -> JSONResponse:
    if store.delivery_already_applied(conn, delivery_id):
        log.warning("webhook: delivery %s was already applied — acknowledged, not re-applied",
                    delivery_id)
        return JSONResponse({"ok": True, "ignored": "duplicate delivery"})

    try:
        with ops.job_run(conn, JOB_NAME) as run:
            stats = process_push(conn, embedder, payload, settings, delivery_id=delivery_id)
            run.stats.update(stats)
            run.head_commit_sha = str(stats.get("sha") or "")
    except LibrarianConfigError as error:
        log.error("webhook: App credential fault (%s)", error.__class__.__name__)
        _mark_dirty(conn, str(payload.get("after") or ""))
        return JSONResponse({"error": "webhook processing failed"}, status_code=500)
    except Exception as error:  # noqa: BLE001 — public error boundary
        log.error("webhook: push processing failed (%s)", error.__class__.__name__)
        _mark_dirty(conn, str(payload.get("after") or ""))
        return JSONResponse({"error": "webhook processing failed"}, status_code=500)

    return JSONResponse({"ok": True, **stats})


async def webhook_endpoint(
    request: Request,
    *,
    embedder,
    settings: WebhookSettings,
    conn=None,
    connection_factory=None,
) -> JSONResponse:
    """Buffer a webhook asynchronously, then process it in one bounded blocking unit."""
    raw_body = await _read_body_capped(request, MAX_BODY_BYTES)
    if raw_body is None:
        log.warning("webhook: body exceeded the %d-byte cap — refused before signature "
                   "verification", MAX_BODY_BYTES)
        return JSONResponse(_UNAUTHORIZED_BODY, status_code=401)

    kwargs = {
        "raw_body": raw_body,
        "signature": request.headers.get("x-hub-signature-256"),
        "event": request.headers.get("x-github-event", ""),
        "delivery_id": request.headers.get("x-github-delivery", ""),
    }
    if connection_factory is None:
        if conn is None:
            raise TypeError("webhook_endpoint requires conn or connection_factory")
        return await run_blocking(_process_webhook, conn, embedder, settings, **kwargs)

    def run():
        worker_conn = connection_factory()
        try:
            return _process_webhook(worker_conn, embedder, settings, **kwargs)
        finally:
            worker_conn.close()

    return await run_blocking(run)


def _mark_dirty(conn, commit_sha: str) -> None:
    try:
        health.mark_dirty(conn, commit_sha)
    except Exception as error:
        log.error(
            "webhook could not mark the index dirty (%s)",
            error.__class__.__name__,
        )
