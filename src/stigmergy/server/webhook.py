"""`POST /webhook/github` — incremental index upsert on merge.

The ONE path on the public server exempt from bearer auth: it authenticates by HMAC instead —
GitHub signs the raw body with `X-Hub-Signature-256`, verified in constant time against
`STIGMERGY_GITHUB_WEBHOOK_SECRET`. `WEBHOOK_PATH` is the one constant both the middleware's
exemption and the route mount read: an EXACT path match in ONE place, never a prefix or regex.

Failure never breaks the write path: a filed page is already committed to git before this endpoint
sees it, and the nightly rebuild reconciles regardless. One parser and one set of store primitives
(`corpus.page_row`, `store.upsert_pages`/`delete_pages`) — a second `pages_index` writer is
exactly the drift this reuse prevents. `githubapp` is this module's one declared librarian
import: reimplementing JWT/token minting would duplicate security-sensitive credential logic.
"""
import hashlib
import hmac
import json
import logging
import os
import posixpath
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse

from stigmergy.capture import ops
from stigmergy.index import corpus, store
from stigmergy.librarian import githubapp
from stigmergy.librarian.errors import LibrarianConfigError

log = logging.getLogger(__name__)

# The ONE path this route is mounted at AND the string the bearer middleware's exemption compares
# against — imported by `transport_http.py`, never re-typed there.
WEBHOOK_PATH = "/webhook/github"

WEBHOOK_SECRET_ENV = "STIGMERGY_GITHUB_WEBHOOK_SECRET"
WEBHOOK_REPO_ENV = "STIGMERGY_GITHUB_REPO"              # "owner/name", e.g. "your-org/stigmergy"
WEBHOOK_BRANCH_ENV = "STIGMERGY_GITHUB_BRANCH"
WEBHOOK_FILE_CAP_ENV = "STIGMERGY_GITHUB_WEBHOOK_FILE_CAP"
DEFAULT_BRANCH = "main"
DEFAULT_FILE_CAP = 50

JOB_NAME = "webhook-index-upsert"

# The SAME generic body every other HTTP auth failure returns. Duplicated rather than imported
# from `transport_http` (which imports this module — the reverse edge would be a cycle); a test
# pins the two literals equal.
_UNAUTHORIZED_BODY = {"error": "unauthorized"}


@dataclass(frozen=True)
class WebhookSettings:
    """`secret=""` (not configured) or `repo=""` (never matches any incoming repository) both
    fail every request closed — this endpoint is inert, not merely unauthenticated, until an
    operator sets both."""
    secret: str = ""
    repo: str = ""
    branch: str = DEFAULT_BRANCH
    file_cap: int = DEFAULT_FILE_CAP


def _parse_file_cap(cap_raw: str) -> int:
    """Absent (`""`) is normal — no warning. Anything present that is not a POSITIVE integer is
    invalid configuration, logged loudly, and falls back to `DEFAULT_FILE_CAP`. `"0"` is invalid,
    not unlimited: a cap of 0 makes `len(changes) > file_cap` true for ANY non-empty push,
    silently deferring every one to the nightly rebuild forever."""
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
    """Constant-time HMAC-SHA256 check over the RAW body, before any JSON parse. Never raises —
    every malformed shape (no secret configured, no header, no `sha256=` prefix, a non-ASCII
    digest) fails closed to `False`, the same outcome a wrong digest gets, so there is no second,
    more-specific error an attacker could distinguish."""
    if not secret or not header_value:
        return False
    scheme, _, digest = header_value.partition("=")
    if scheme != "sha256" or not digest:
        return False
    # `hmac.compare_digest` raises TypeError on a str with any non-ASCII char, and `digest` is
    # attacker-supplied (a latin-1 header decode) — one byte >= 0x80 would turn the generic 401
    # into a 500. `isascii()` rather than an encode: it cannot raise for ANY str (a lone
    # surrogate would still blow up `.encode`), and a valid digest is hex, so nothing real is
    # rejected.
    if not digest.isascii():
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest)


def changed_paths_from_push(payload: dict) -> dict[str, str]:
    """`path -> final status` ('added' | 'modified' | 'removed') across every commit in this ONE
    push: a path touched more than once nets to its LAST state, so a push that adds a file and
    renames it away again in the same batch stays safe."""
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
    """The same population `corpus.load_pages` walks — zone (`corpus.ZONES`), extension and
    dot-files alike, with the extension-and-dot-name test being `corpus.is_indexable_page`
    itself, never a second spelling of it. A wider filter here indexes files the rebuild never
    produces a row for (no frontmatter → `acl` None → OPEN at `visible()`); a narrower one drops
    edits and DELETIONS for pages the rebuild still indexes. Two walkers over one population is
    the thing to avoid; asserting they agree in a test does not make them one."""
    prefixes = tuple(f"{zone}/" for zone in corpus.ZONES)
    return {path: status for path, status in changes.items()
            if path.startswith(prefixes) and corpus.is_indexable_page(path)}


def ops_files_pushed(changes: dict[str, str]) -> list[str]:
    """The cached ops files this push touched — the relpaths to refresh, in the store's order.

    Asked of the RAW changed paths, never of `in_zone_changes`: `ops/` is not one of `corpus.ZONES`,
    so these files are invisible to every filter written for pages — precisely how issue #74 stayed
    hidden. A `removed` status is excluded: no governed door deletes any of them, so a removal is
    either a rename away (the nightly rebuild reconciles from the checkout, per file) or an
    accident, and blanking an entity roster — or an identity roster — the moment one lands is the
    worse of the two answers.
    """
    return [rel for rel in store.OPS_FILE_RELPATHS
            if changes.get(rel) in ("added", "modified")]


def _fetch_file_content(repo_slug: str, path: str, ref: str, token: str, *, opener=None) -> str:
    """One file's text at `ref` — the delivery's own commit sha for pages, the BRANCH NAME for ops
    files (see `process_push`) — via the GitHub Contents API: no clone, no checkout.
    `Accept: application/vnd.github.raw+json` asks GitHub to hand back the raw bytes directly
    rather than a base64-JSON envelope."""
    url = (f"https://api.github.com/repos/{repo_slug}/contents/{urllib.parse.quote(path)}"
          f"?ref={urllib.parse.quote(ref)}")
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github.raw+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "stigmergy-server-webhook"})
    with (opener or urllib.request.urlopen)(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def _resolve_outbound_links(conn, rows: list) -> None:
    """Outbound `links` resolved against `pages_index`'s existing paths — ONE query, reusing
    `corpus.resolve_links`/`by_stem_index` so one algorithm sits behind both this and the full
    rebuild. Known residual: a page new in THIS push cannot resolve against a sibling also new to
    it (the snapshot is read before the landing transaction); the nightly rebuild reconciles, as
    it does for `_propagate_split_chain_supersession`'s analogous gap."""
    by_stem = corpus.by_stem_index(store.existing_paths(conn))
    for r in rows:
        r.links = corpus.resolve_links(r.path, r.links, by_stem)


def _propagate_split_chain_supersession(conn, rows: list) -> None:
    """A push that upserts a split chain's PRIMARY stamps or clears `superseded_by` on its
    already-indexed part siblings immediately (the incoming value verbatim, empty included) —
    rank-time reconstruction does not exist, so parts must not wait for the nightly rebuild.
    Marker-gated like `corpus.load_pages`'s build-time rule: only a chain base
    (`corpus.is_chain_primary`) donates, only rows matching `corpus.chain_part_pattern(base)`
    receive — the regex decides, never a heuristic. The `LIKE` prefetch narrows candidates
    cheaply and its SQL lives in `index/store.py`, the one module that owns `pages_index` writes.
    Known residual: a push editing ONLY a part re-upserts its always-empty `superseded_by`, so
    that part reverts until the nightly rebuild re-propagates it — same reconciler as backlinks."""
    for r in rows:
        if not corpus.is_chain_primary(r.page_id):
            continue
        pattern = corpus.chain_part_pattern(r.page_id)
        # BOTH part conventions are prefetched — historical `#p<n>` AND the live `-p<n>` the
        # meeting splitter writes; a `#p`-only prefetch leaves this propagation silently inert
        # over every real split. The regex stays the one decider; the directory gate mirrors
        # `corpus.load_pages`, so an id-less `-p2`-stemmed twin elsewhere never inherits.
        candidates = (store.pages_with_page_id_prefix(conn, f"{r.page_id}#p")
                      + store.pages_with_page_id_prefix(conn, f"{r.page_id}-p"))
        donor_dir = posixpath.dirname(r.path)
        targets = sorted(path for path, page_id in candidates
                         if pattern.fullmatch(page_id)
                         and posixpath.dirname(path) == donor_dir)
        store.set_superseded_by(conn, targets, r.superseded_by)


def process_push(conn, embedder, payload: dict, settings: WebhookSettings, *,
                 delivery_id: str = "", opener=None) -> dict:
    """The core logic, called ONLY after the event/repo/branch checks passed (the caller's job).
    Returns the stats dict written to `job_runs`.

    Bounded: above `settings.file_cap` in-zone changed files the push is deferred wholesale — a
    half-applied bulk change is worse than a stale one, and the ENTITY REGISTRY rides that same
    deferral even though it is not an in-zone file: the cap's promise is that a push lands whole or
    not at all, and the nightly rebuild that reconciles the pages now reconciles the registry with
    them. (A mint's own push is two files, so it never approaches the cap; a registry-only push
    counts zero in-zone files and cannot trip it at all, which is why the registry carries its own
    bound — a SIZE, `store.MAX_ENTITY_REGISTRY_BYTES`, since what an oversized one costs is a parse
    on every tool call rather than one ingest.) Two phases for the same reason: phase 1
    is every network call and read-only lookup with NO database write; phase 2 is exactly one
    `with conn.transaction():` around delete + fresh embeddings + upsert + the registry snapshot. A
    phase-1 failure never touches `pages_index`; a phase-2 failure rolls the delete back WITH the
    upsert, so a rename that fails mid-run loses NEITHER page.

    GitHub does NOT auto-redeliver on a 5xx — the 500 is operator visibility (and manual
    redelivery), never a retry mechanism; the nightly rebuild is the only automatic reconciler.
    """
    sha = payload.get("after") or (payload.get("head_commit") or {}).get("id") or ""
    raw_changes = changed_paths_from_push(payload)
    changes = in_zone_changes(raw_changes)
    ops_files_to_refresh = ops_files_pushed(raw_changes)
    stats = {"sha": sha, "upserted": 0, "deleted": 0, "skipped": 0, "embedded": 0}

    if len(changes) > settings.file_cap:
        stats.update({"deferred": True, "changed_files": len(changes)})
        log.warning("webhook: push %s touches %d in-zone file(s), above the cap of %d — "
                   "deferring to the nightly rebuild rather than a partial apply",
                   sha, len(changes), settings.file_cap)
        return stats

    to_delete = sorted(p for p, s in changes.items() if s == "removed")
    to_upsert_paths = sorted(p for p, s in changes.items() if s != "removed")

    # ── phase 1: network I/O + read-only lookups, NO database write ────────────────────────────
    rows: list = []
    model = ""
    fts_config = "english"
    embeddings: dict[str, list[float]] = {}
    fresh: dict[str, list[float]] = {}
    skipped = 0
    # ONE token per delivery, minted only when something will actually be fetched. It is minted
    # here rather than beside the page fetches because a push can carry the registry and no page at
    # all (a registry regenerate, or a re-mint of an already-indexed entity page), and that push
    # must still be able to fetch.
    token = githubapp.installation_token() if (to_upsert_paths or ops_files_to_refresh) else ""
    if to_upsert_paths:
        before_hashes = store.current_content_hashes(conn, to_upsert_paths)   # SELECT only
        for path in to_upsert_paths:
            zone = path.split("/", 1)[0]
            text = _fetch_file_content(settings.repo, path, sha, token, opener=opener)  # network
            rows.append(corpus.page_row(path, zone, text))

        skipped = sum(1 for r in rows if before_hashes.get(r.path) == r.content_hash)
        _resolve_outbound_links(conn, rows)   # SELECT only

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
    for relpath in ops_files_to_refresh:
        # AT THE BRANCH REF, deliberately not at the pushed sha, and the difference is the whole
        # replay defense for these files (issue #79): a replayed or delayed delivery re-fetches
        # what the branch says NOW, so no historical roster or registry is ever installable
        # through this endpoint — the worst a captured delivery can do is refresh the cache to
        # the present. Pages keep the sha fetch (their consistency story is the delivery's own
        # path list); their replay window is closed by the delivery-id dedupe instead.
        fetched = _fetch_file_content(                                        # network
            settings.repo, relpath, settings.branch, token, opener=opener)
        size = len(fetched.encode("utf-8"))
        if size > store.MAX_OPS_FILE_BYTES:
            # Refused, not raised: the PAGES in this same push must still land, and this endpoint's
            # failure mode is never to break the write path. The previous snapshot stays — the
            # honest floor is a snapshot that is stale, not one that costs every identity a
            # multi-megabyte parse on every tool call (see `store.MAX_OPS_FILE_BYTES`).
            log.error("webhook: %s at %s is %d bytes, above the %d-byte cap — NOT cached; the "
                      "previous snapshot stands and the nightly rebuild reconciles",
                      relpath, settings.branch, size, store.MAX_OPS_FILE_BYTES)
            stats.setdefault("ops_files_refused", {})[relpath] = size
        else:
            ops_file_texts[relpath] = fetched

    # ── phase 2: one transaction — delete + store any fresh embeddings + upsert, or none of it ──
    with conn.transaction():
        if to_delete:
            stats["deleted"] = store.delete_pages(conn, to_delete)
        if to_upsert_paths:
            if fresh:
                store.store_embeddings(conn, model, fresh)
            store.upsert_pages(conn, rows, embeddings, fts_config)
            # a stamped/cleared PRIMARY propagates to its already-indexed `#p<n>` siblings in the
            # SAME transaction as the upsert — atomic with it, so a failure here rolls back with
            # everything else phase 2 already guards.
            _propagate_split_chain_supersession(conn, rows)
            stats["upserted"] = len(rows)
        for relpath, text in ops_file_texts.items():
            # In the SAME transaction as the entity page a mint pushes beside it: the two land
            # together or neither does, so no reader ever sees the page without its metadata.
            # `source` records the delivery's sha for the operator even though the BYTES came
            # from the branch ref — "which push refreshed this" is the diagnostic question.
            store.write_ops_file(conn, relpath, text, sha)
        if delivery_id:
            with conn.cursor() as cur:
                store.record_delivery(cur, delivery_id)

    if ops_file_texts:
        # Only when a refresh actually happened — an ordinary page push's stats dict stays exactly
        # what it has always been, and `job_runs` says which deliveries touched which files.
        stats["ops_files_refreshed"] = sorted(ops_file_texts)
        if store.ENTITY_REGISTRY_RELPATH in ops_file_texts:
            stats["registry_refreshed"] = True
    stats["skipped"] = skipped
    stats["embedded"] = len(fresh)
    return stats


# A hard cap on this endpoint's body — the one bearer-auth-exempt path, so the one path an
# unauthenticated caller can throw large concurrent bodies at, on the same small single-process
# machine serving every identity. Sized to what this handler will ever ACT on (a file-cap'd path
# list; a real push is kilobytes), NOT to GitHub's 25 MB delivery ceiling — a bound on the
# protocol instead of the work is an OOM amplifier. The same reasoning bounds the one FILE this
# endpoint fetches and stores whole — `store.MAX_ENTITY_REGISTRY_BYTES`, which lives beside its
# table because the rebuild road has to refuse exactly what this one refuses.
MAX_BODY_BYTES = 1024 * 1024


async def _read_body_capped(request: Request, max_bytes: int) -> bytes | None:
    """Stream the body, counting bytes as they arrive — never trusting `Content-Length` (free to
    lie or be absent). Returns `None` the moment the total exceeds `max_bytes`, without buffering
    the rest: the caller refuses BEFORE `verify_signature`, so an HMAC check never runs over (and
    never leaks anything about) an oversized body."""
    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def webhook_endpoint(request: Request, *, conn, embedder, settings: WebhookSettings) -> JSONResponse:
    """The handler at `WEBHOOK_PATH`. Size cap first (an oversized body gets the same generic 401
    WITHOUT `verify_signature` ever running — refusing on size leaks nothing about the signature),
    then HMAC over the RAW body before any JSON parse; any invalid signature returns the same
    generic 401 as every other auth failure. Every other event, branch or repository: 200 and
    ignore, never an error (GitHub disables endpoints that fail); only the configured repo+branch
    push does work and writes a `job_runs` row."""
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
        # A signature that verified over UNPARSEABLE JSON is not a plausible GitHub delivery —
        # still 200 (never an error to GitHub), simply nothing to act on.
        log.warning("webhook: signature verified but the body did not parse as JSON")
        return JSONResponse({"ok": True, "ignored": "unparseable body"})
    if not isinstance(payload, dict):
        # `json.loads` accepts any JSON VALUE (`123`, `[]`, `null`) and none of them has `.get` —
        # same verdict as above: nothing to act on, never an error to GitHub.
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

    # Replay protection for the PAGE road (the ops files defend themselves by fetching at the
    # branch ref): a delivery id this database has already applied is acknowledged and not
    # re-applied — a replayed delivery would re-fetch page content at its own OLD sha, silently
    # downgrading rows and re-performing old deletions until the nightly rebuild. 200, never an
    # error: the legitimate sender of a repeat is GitHub's own redelivery button, and the id is
    # only recorded when an apply COMMITS, so redelivering a failed delivery still works.
    delivery_id = request.headers.get("x-github-delivery", "")
    if store.delivery_already_applied(conn, delivery_id):
        log.warning("webhook: delivery %s was already applied — acknowledged, not re-applied",
                    delivery_id)
        return JSONResponse({"ok": True, "ignored": "duplicate delivery"})

    try:
        with ops.job_run(conn, JOB_NAME) as job_stats:
            stats = process_push(conn, embedder, payload, settings, delivery_id=delivery_id)
            job_stats.update(stats)
    except LibrarianConfigError as ex:
        # The App credential is absent or half-configured — an operator-visible configuration
        # fault. 500 for delivery-log visibility and manual redelivery, NOT a retry (GitHub does
        # not auto-redeliver on 5xx); filing is unaffected either way.
        log.error("webhook: App credential fault: %s", ex)
        return JSONResponse({"error": "webhook processing failed"}, status_code=500)
    except Exception:  # noqa: BLE001 — never leak an internal detail to a public endpoint
        log.error("webhook: push processing failed", exc_info=True)
        return JSONResponse({"error": "webhook processing failed"}, status_code=500)

    return JSONResponse({"ok": True, **stats})
