"""Authenticated GitHub push webhook for incremental index convergence."""
import hashlib
import hmac
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse

from stigmergy.capture import ops
from stigmergy.index import corpus, health, store
from stigmergy.librarian import githubapp
from stigmergy.librarian.errors import LibrarianConfigError
from stigmergy.server import controls as control_contract

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/webhook/github"

WEBHOOK_SECRET_ENV = "STIGMERGY_GITHUB_WEBHOOK_SECRET"
WEBHOOK_REPO_ENV = "STIGMERGY_GITHUB_REPO"              # "owner/name", e.g. "your-org/stigmergy"
WEBHOOK_BRANCH_ENV = "STIGMERGY_GITHUB_BRANCH"
WEBHOOK_FILE_CAP_ENV = "STIGMERGY_GITHUB_WEBHOOK_FILE_CAP"
DEFAULT_BRANCH = "main"
DEFAULT_FILE_CAP = 50

JOB_NAME = "webhook-index-upsert"

_UNAUTHORIZED_BODY = {"error": "unauthorized"}


@dataclass(frozen=True)
class WebhookSettings:
    """Webhook authentication and repository scope."""
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
        repo=env.get(WEBHOOK_REPO_ENV, ""),
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
    prefixes = tuple(f"{zone}/" for zone in corpus.ZONES)
    return {path: status for path, status in changes.items()
            if path.startswith(prefixes) and corpus.is_indexable_page(path)}


def ops_files_pushed(changes: dict[str, str]) -> list[str]:
    """Return added or modified repository control files in canonical order."""
    return [rel for rel in store.OPS_FILE_RELPATHS
            if changes.get(rel) in ("added", "modified")]


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
    inbound: dict[str, int] = {}
    for targets in links.values():
        for target in targets:
            inbound[target] = inbound.get(target, 0) + 1
    with conn.cursor() as cursor:
        for path in links:
            cursor.execute(
                "UPDATE pages_index SET links = %s, inlinks = %s WHERE path = %s",
                (links[path], inbound.get(path, 0), path),
            )


def process_push(conn, embedder, payload: dict, settings: WebhookSettings, *,
                 delivery_id: str = "", opener=None) -> dict:
    """Apply a bounded push atomically or defer it to the full rebuild."""
    sha = payload.get("after") or (payload.get("head_commit") or {}).get("id") or ""
    before = payload.get("before")
    raw_changes = changed_paths_from_push(payload)
    changes = in_zone_changes(raw_changes)
    ops_files_to_refresh = ops_files_pushed(raw_changes)
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
    if any(raw_changes.get(relpath) == "removed" for relpath in store.OPS_FILE_RELPATHS):
        return _defer(conn, stats, sha, delivery_id, "control_file_removed")
    if len(changes) > settings.file_cap:
        stats["changed_files"] = len(changes)
        log.warning("webhook: push %s touches %d in-zone file(s), above the cap of %d — "
                   "deferring to the nightly rebuild rather than a partial apply",
                   sha, len(changes), settings.file_cap)
        return _defer(conn, stats, sha, delivery_id, "overflow")

    to_delete = sorted(p for p, s in changes.items() if s == "removed")
    to_upsert_paths = sorted(p for p, s in changes.items() if s != "removed")

    rows: list = []
    model = ""
    fts_config = "english"
    embeddings: dict[str, list[float]] = {}
    fresh: dict[str, list[float]] = {}
    skipped = 0
    token = githubapp.installation_token() if (to_upsert_paths or ops_files_to_refresh) else ""
    if to_upsert_paths:
        before_hashes = store.current_content_hashes(conn, to_upsert_paths)   # SELECT only
        for path in to_upsert_paths:
            zone = path.split("/", 1)[0]
            text = _fetch_file_content(settings.repo, path, sha, token, opener=opener)  # network
            rows.append(corpus.page_row(path, zone, text))

        skipped = sum(1 for r in rows if before_hashes.get(r.path) == r.content_hash)
        _resolve_outbound_links(conn, rows, set(to_delete))   # SELECT only

        meta = store.read_meta(conn) or {}   # SELECT only
        model = meta.get("model") or getattr(embedder, "model", "")
        fts_config = meta.get("fts_config") or "english"

        hashes = [r.content_hash for r in rows]
        cached = store.cached_embeddings(conn, model, hashes)   # SELECT only
        unique: dict[str, str] = {}
        for r in rows:
            if r.content_hash not in cached:
                unique.setdefault(r.content_hash, r.embed_text)
        if unique:
            keys = list(unique)
            vectors = embedder.embed([unique[h] for h in keys])   # network (the embedder)
            fresh = dict(zip(keys, vectors, strict=True))
        embeddings = {**cached, **fresh}

    ops_file_texts: dict[str, str] = {}
    control_paths = store.OPS_FILE_RELPATHS if ops_files_to_refresh else ()
    for relpath in control_paths:
        fetched = _fetch_file_content(                                        # network
            settings.repo, relpath, sha, token, opener=opener)
        size = len(fetched.encode("utf-8"))
        if size > store.MAX_OPS_FILE_BYTES:
            log.error("webhook: %s at %s is %d bytes, above the %d-byte cap — NOT cached; the "
                      "previous snapshot stands and the nightly rebuild reconciles",
                      relpath, sha, size, store.MAX_OPS_FILE_BYTES)
            stats.setdefault("ops_files_refused", {})[relpath] = size
        else:
            ops_file_texts[relpath] = fetched

    if stats.get("ops_files_refused"):
        return _defer(conn, stats, sha, delivery_id, "control_file_refused")
    if ops_file_texts:
        try:
            control_contract.validate_texts(ops_file_texts)
        except control_contract.ControlError:
            return _defer(conn, stats, sha, delivery_id, "control_file_invalid")

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
            store.upsert_pages(conn, rows, embeddings, fts_config)
            stats["upserted"] = len(rows)
        if to_delete or to_upsert_paths:
            _refresh_link_graph(conn)
        for relpath, text in ops_file_texts.items():
            store.write_ops_file(conn, relpath, text, sha)
        if delivery_id:
            with conn.cursor() as cur:
                store.record_delivery(cur, delivery_id)
        health.record_incremental(conn, sha, indexed_rows=store.page_count(conn))

    if ops_file_texts:
        stats["ops_files_refreshed"] = sorted(ops_file_texts)
        if store.ENTITY_REGISTRY_RELPATH in ops_file_texts:
            stats["registry_refreshed"] = True
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


async def webhook_endpoint(request: Request, *, conn, embedder, settings: WebhookSettings) -> JSONResponse:
    """Authenticate and process one configured repository push."""
    raw_body = await _read_body_capped(request, MAX_BODY_BYTES)
    if raw_body is None:
        log.warning("webhook: body exceeded the %d-byte cap — refused before signature "
                   "verification", MAX_BODY_BYTES)
        return JSONResponse(_UNAUTHORIZED_BODY, status_code=401)

    signature = request.headers.get("x-hub-signature-256")
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

    event = request.headers.get("x-github-event", "")
    if event != "push":
        return JSONResponse({"ok": True, "ignored": f"event={event!r}"})

    repo_full_name = (payload.get("repository") or {}).get("full_name", "")
    if not settings.repo or repo_full_name != settings.repo:
        return JSONResponse({"ok": True, "ignored": f"repository={repo_full_name!r}"})

    ref = payload.get("ref", "")
    if ref != f"refs/heads/{settings.branch}":
        return JSONResponse({"ok": True, "ignored": f"ref={ref!r}"})

    delivery_id = request.headers.get("x-github-delivery", "")
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


def _mark_dirty(conn, commit_sha: str) -> None:
    try:
        health.mark_dirty(conn, commit_sha)
    except Exception as error:
        log.error(
            "webhook could not mark the index dirty (%s)",
            error.__class__.__name__,
        )
