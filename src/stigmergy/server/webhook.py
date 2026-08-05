"""`POST /webhook/github` — incremental index upsert on merge.

This is the ONE path on the public server exempt from the bearer-auth middleware
(`transport_http._BearerAuthMiddleware`), because it authenticates a different way entirely:
GitHub signs the raw request body with `X-Hub-Signature-256`, verified here in constant time
against `STIGMERGY_GITHUB_WEBHOOK_SECRET`. `WEBHOOK_PATH` is the ONE constant both the middleware's
exemption and this module's route registration read, so the exemption is an EXACT path match in
ONE place — never a prefix, never a regex.

**Failure never breaks the write path**: a page the librarian just filed is already committed to
git before this endpoint ever sees it. If this endpoint fails — GitHub's API is down, the embedder
is unavailable, this process crashes mid-request — the page is still filed, still on `main`, and
the nightly `index-rebuild.yml` reconciles it regardless. Nothing here can undo a commit or make a
filed page un-filed.

**One parser, one set of store primitives**: `corpus.page_row` is the SAME function
`index.corpus.load_pages`'s full directory walk calls per file, and `store.upsert_pages`/
`delete_pages` sit beside `insert_pages` rather than reimplementing its row shape. Two code paths
writing `pages_index` is exactly the drift this reuse exists to prevent, rather than growing a
second implementation of either.

**One declared exception to "the server never imports the librarian"** (`tests/test_architecture.
py::test_server_never_imports_the_librarian`): `githubapp.installation_token`/`githubapp.
configured` are the App-credential primitives the librarian worker already reads from this
process's environment — the public server's environment also carries the App private key.
Webhooks land on the existing server, and reimplementing JWT/token minting a second time would
duplicate security-sensitive credential logic instead of reusing it.
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

# The ONE path this route is mounted at, and the ONE string the bearer middleware's exemption
# compares against: the middleware exempts this exact path and nothing else. Imported by
# `transport_http.py`, never re-typed there.
WEBHOOK_PATH = "/webhook/github"

WEBHOOK_SECRET_ENV = "STIGMERGY_GITHUB_WEBHOOK_SECRET"
WEBHOOK_REPO_ENV = "STIGMERGY_GITHUB_REPO"              # "owner/name", e.g. "your-org/stigmergy"
WEBHOOK_BRANCH_ENV = "STIGMERGY_GITHUB_BRANCH"
WEBHOOK_FILE_CAP_ENV = "STIGMERGY_GITHUB_WEBHOOK_FILE_CAP"
DEFAULT_BRANCH = "main"
DEFAULT_FILE_CAP = 50

JOB_NAME = "webhook-index-upsert"

# The SAME generic body every other HTTP auth failure returns — indistinguishable from any other
# 401. Duplicated as a literal rather than imported from `transport_http` on purpose: importing it
# back would create a two-way dependency between this module and the one that imports it; both
# modules independently carry the ONE fixed string, and a test pins them equal
# (`test_webhook.py`).
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
    """`"0".isdigit()` is `True`, so a naive parse reads a cap of exactly 0 for
    `STIGMERGY_GITHUB_WEBHOOK_FILE_CAP=0` — and `len(changes) > file_cap` is then true for ANY
    non-empty push, silently deferring every single one to the nightly rebuild forever, with no
    operator-visible signal that the configured value was nonsensical. Absent (`""`) is normal
    (not configured — no warning); anything present that is not a POSITIVE integer (a non-numeric
    string, `"0"`, or a negative number) is invalid configuration, logged loudly, and falls back
    to `DEFAULT_FILE_CAP` exactly like a non-numeric value does."""
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
    """Constant-time HMAC-SHA256 check over the RAW body, before any JSON parse.
    Never raises — every malformed shape (no secret configured, no header,
    a header with no `sha256=` prefix) fails closed to `False`, the same outcome a wrong digest
    gets, so there is no second, more-specific error an attacker could distinguish."""
    if not secret or not header_value:
        return False
    scheme, _, digest = header_value.partition("=")
    if scheme != "sha256" or not digest:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest)


def changed_paths_from_push(payload: dict) -> dict[str, str]:
    """`path -> final status` ('added' | 'modified' | 'removed') across every commit in this ONE
    push, in the order GitHub lists them — a path touched more than once nets to its LAST state,
    which is what makes this safe against a push that adds a file and then renames it away again
    in the same batch."""
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
    """Filtered to `corpus.ZONES` — bound to the constant `index.corpus` already declares, never a
    fresh list this module would have to keep in sync by hand."""
    prefixes = tuple(f"{zone}/" for zone in corpus.ZONES)
    return {path: status for path, status in changes.items() if path.startswith(prefixes)}


def fetch_file_content(repo_slug: str, path: str, sha: str, token: str, *, opener=None) -> str:
    """One file's text AT THE PUSHED SHA, via the GitHub Contents API — no clone, no checkout.
    `Accept: application/vnd.github.raw+json` asks GitHub to hand back the raw bytes directly
    rather than a base64-JSON envelope."""
    url = (f"https://api.github.com/repos/{repo_slug}/contents/{urllib.parse.quote(path)}"
          f"?ref={urllib.parse.quote(sha)}")
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github.raw+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "stigmergy-server-webhook"})
    with (opener or urllib.request.urlopen)(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def _resolve_outbound_links(conn, rows: list) -> None:
    """Outbound `links`, resolved against `pages_index`'s OWN existing paths — ONE query. Outbound
    links CAN be recomputed for a single file, unlike the whole-corpus `inlinks` count. Reuses
    `corpus.resolve_links`/`by_stem_index` — the SAME resolution `corpus.load_pages`'s full
    rebuild runs in memory, so there is provably one algorithm behind both snapshots (a parity
    test pins it), never a second one that could quietly drift from it.

    A page added or renamed in THIS SAME push cannot resolve yet against a sibling ALSO new to
    this push (this snapshot is read before the transaction that will land either of them) — the
    inbound view (backlinks) reconciles at the next nightly rebuild exactly as `inlinks` already
    does; documented here, not discovered later.

    **The analogous gap for `superseded_by`**: see `_propagate_split_chain_supersession` below —
    a push that stamps or clears a split chain's PRIMARY propagates to its already-indexed
    `#p<n>` siblings immediately, but a push that edits ONLY a sibling PART (its primary
    untouched, outside this push) does not; that direction reconciles at the next nightly rebuild
    the same way this function's own gap does."""
    by_stem = corpus.by_stem_index(store.existing_paths(conn))
    for r in rows:
        r.links = corpus.resolve_links(r.path, r.links, by_stem)


def _propagate_split_chain_supersession(conn, rows: list) -> None:
    """The webhook's own supersession window: rank-time reconstruction does not exist (`rank.py`),
    so once a push upserts a split chain's PRIMARY, its already-indexed part siblings (`#p<n>`
    historical, `-p<n>` live — both conventions, because the meeting flow writes the latter) must
    not wait for the nightly rebuild to learn the new `superseded_by` — stamped or cleared,
    symmetrically (the incoming value is used verbatim, empty string included).

    Marker-gated exactly like `corpus.load_pages`'s build-time rule: only a row upserted THIS push
    whose own `page_id` is its chain's base (`corpus.is_chain_primary` — no `#p<n>` marker, the
    donor) propagates, and only to rows matching `corpus.chain_part_pattern(base)` (the receiver)
    — never a heuristic, never "whichever came first".

    `store.pages_with_page_id_prefix`'s `LIKE` prefetch narrows the candidate rows cheaply — the
    raw SQL against `pages_index` lives THERE, not here: `index/store.py` is the one module that
    owns SQL DDL and writes, and the architecture test's ACL exception list already names it a
    library with no identity to scope to. The exact marker regex (Python, the same one `corpus.py`
    uses) then decides which of the prefetched candidates really are `#p<n>` parts before anything
    is written, so a page_id that merely STARTS WITH "<base>#p" (never digits) is never touched.

    Residual, matching `_resolve_outbound_links`'s own honesty about its window: a push that edits
    ONLY a `#p<n>` PART (its primary untouched, outside this push) re-upserts that part's own
    frontmatter — always empty `superseded_by` by contract (`versions.py` stamps the PRIMARY
    only) — so the part's row reverts to unsuperseded until the next nightly rebuild
    re-propagates it. Closing that direction needs a read of the (possibly unrelated, unchanged)
    primary's CURRENT value for every part a push merely touches, which is a different and heavier
    query than this fix adds; the nightly rebuild is the reconciler for it, exactly as it is for
    backlinks."""
    for r in rows:
        if not corpus.is_chain_primary(r.page_id):
            continue
        pattern = corpus.chain_part_pattern(r.page_id)
        # BOTH part conventions are prefetched — the historical `#p<n>` AND the live `-p<n>` the
        # meeting flow's splitter actually writes. A prefetch narrowed to `#p` alone means
        # `pattern.fullmatch` never sees a live part and the incremental propagation stays inert
        # over every real split, silently. The regex stays the one decider; the directory gate
        # mirrors `corpus.load_pages`: a part receives only from the primary in its OWN folder, so
        # an id-less `-p2`-stemmed twin in another directory never inherits.
        candidates = (store.pages_with_page_id_prefix(conn, f"{r.page_id}#p")
                      + store.pages_with_page_id_prefix(conn, f"{r.page_id}-p"))
        donor_dir = posixpath.dirname(r.path)
        targets = sorted(path for path, page_id in candidates
                         if pattern.fullmatch(page_id)
                         and posixpath.dirname(path) == donor_dir)
        store.set_superseded_by(conn, targets, r.superseded_by)


def process_push(conn, embedder, payload: dict, settings: WebhookSettings, *, opener=None) -> dict:
    """The core logic, called ONLY after the event/repo/branch checks already passed (the caller's
    job, not this function's — see `webhook_endpoint`). Returns the stats dict written to
    `job_runs`: files upserted, deleted, skipped, embedded, and the sha.

    **Bounded**: above `settings.file_cap` in-zone changed files, this does NOT attempt a partial
    job — a half-applied bulk change is worse than a stale one, and the nightly rebuild is what
    reconciles a deferred push.

    **Two phases**, for the SAME reason the file-cap path above refuses wholesale rather than
    partially: phase 1 does every network call (GitHub's Contents API, the embedder) and every
    READ-only lookup (content-hash/embedding-cache/index-meta SELECTs) with NO database WRITE in
    it at all; phase 2 is exactly one `with conn.transaction():` around the delete and the upsert
    (which itself stores any freshly-computed embeddings). A failure anywhere in phase 1 —
    GitHub's API down, the embedder unavailable — never touches `pages_index`, and a failure
    anywhere in phase 2 rolls the delete back WITH the upsert, so a rename (`a.md` -> `b.md`) that
    fails mid-run loses NEITHER page rather than both. Splitting the two apart, with `delete_pages`
    committing on its own independently of whether the upsert that followed it ever landed, is what
    made that data loss possible.

    **GitHub does NOT automatically redeliver a webhook delivery whose endpoint responded with a
    5xx.** The 500 `webhook_endpoint` returns on a caught exception exists for OPERATOR visibility
    (and manual redelivery from the GitHub UI/API) — it is not itself a retry mechanism. The
    nightly `index-rebuild.yml` full rebuild is the only AUTOMATIC reconciler for a push this
    function never finished applying.
    """
    sha = payload.get("after") or (payload.get("head_commit") or {}).get("id") or ""
    changes = in_zone_changes(changed_paths_from_push(payload))
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
    if to_upsert_paths:
        before_hashes = store.current_content_hashes(conn, to_upsert_paths)   # SELECT only
        token = githubapp.installation_token()
        for path in to_upsert_paths:
            zone = path.split("/", 1)[0]
            text = fetch_file_content(settings.repo, path, sha, token, opener=opener)  # network
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

    stats["skipped"] = skipped
    stats["embedded"] = len(fresh)
    return stats


# A hard cap on this endpoint's body — this is the one path on the public server exempt from
# bearer auth (module docstring), so it is the one path an unauthenticated
# caller can throw arbitrarily large, concurrent bodies at, exhausting the memory of the SAME
# process serving every bearer-authenticated identity. GitHub's own delivery ceiling is 25 MB;
# 26 MB leaves it exactly one MB of slack for framing, never enough to matter for a real delivery.
MAX_BODY_BYTES = 26 * 1024 * 1024


async def _read_body_capped(request: Request, max_bytes: int) -> bytes | None:
    """Stream the body, counting bytes as they actually arrive — NEVER trusting `Content-Length`
    alone (Starlette does not cap body size, and a header is free to lie or be absent). Returns
    `None` the moment the running total exceeds `max_bytes`, without buffering the rest of the
    stream: the caller must refuse before ever handing an oversized body to `verify_signature`, so
    an HMAC check never runs over (and so never leaks anything about) a body this large."""
    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def webhook_endpoint(request: Request, *, conn, embedder, settings: WebhookSettings) -> JSONResponse:
    """The ASGI handler mounted at `WEBHOOK_PATH`. HMAC over the RAW body, before any JSON parse;
    an invalid signature (absent, malformed, wrong, or the endpoint simply not configured) returns
    the SAME generic 401 body every other auth failure on this server returns.

    Every OTHER event, branch or repository: 200 and ignore, never an error (GitHub disables
    endpoints that fail). Only a request that names the configured repo's push to the configured
    branch does any work at all, and only THAT case writes a `job_runs` row.

    **The body is read through `_read_body_capped`, streamed with a hard cap (`MAX_BODY_BYTES`),
    before anything else — including signature verification.** An oversized body gets the SAME
    generic 401 every other auth failure here returns, and gets it WITHOUT ever calling
    `verify_signature`: refusing on size before HMAC runs leaks nothing about whether the
    (never-checked) signature would have been valid.
    """
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

    event = request.headers.get("x-github-event", "")
    if event != "push":
        return JSONResponse({"ok": True, "ignored": f"event={event!r}"})

    repo_full_name = (payload.get("repository") or {}).get("full_name", "")
    if not settings.repo or repo_full_name != settings.repo:
        return JSONResponse({"ok": True, "ignored": f"repository={repo_full_name!r}"})

    ref = payload.get("ref", "")
    if ref != f"refs/heads/{settings.branch}":
        return JSONResponse({"ok": True, "ignored": f"ref={ref!r}"})

    try:
        with ops.job_run(conn, JOB_NAME) as job_stats:
            stats = process_push(conn, embedder, payload, settings)
            job_stats.update(stats)
    except LibrarianConfigError as ex:
        # The App credential is absent or half-configured (`githubapp.configured`/
        # `installation_token`'s own fail-closed check) — an operator-visible configuration
        # fault, not a GitHub-side problem. 500 so the failure is visible in GitHub's delivery
        # log and can be redelivered by hand, NOT because a 5xx triggers any automatic retry
        # (it does not — see `process_push`); the write path (filing) is entirely unaffected
        # either way (module docstring).
        log.error("webhook: App credential fault: %s", ex)
        return JSONResponse({"error": "webhook processing failed"}, status_code=500)
    except Exception:  # noqa: BLE001 — never leak an internal detail to a public endpoint
        log.error("webhook: push processing failed", exc_info=True)
        return JSONResponse({"error": "webhook processing failed"}, status_code=500)

    return JSONResponse({"ok": True, **stats})
