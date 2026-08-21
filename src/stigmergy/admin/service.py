"""The console's use-cases — every route handler is a thin skin over one method here.

Everything operational lands on a seam another package owns; the ONLY SQL this module owns is
read-side plumbing no library exposes: `job_runs`/`ingest_errors` reads, `audit_log` aggregates,
the `pages_index` zone AGGREGATE (a declared reader exception — counts only, never content), and
`admin_actions` via `admin.schema`.

Untrusted text passes `text.sanitize` on the way out — control characters die at the server,
HTML-escaping is the client's non-negotiable half. Errors: `AdminBadRequest` / `AdminNotFound` /
`AdminRefused` cross with the library's own sentence; anything else becomes the routes' 500,
class name only.
"""
import logging
import os
from collections import Counter

from stigmergy import text as textutil
from stigmergy.admin import schema as admin_schema
from stigmergy.admin.github import ActionsError
from stigmergy.admin.settings import AdminSettings
from stigmergy.capture import decisions, latency, queue, retention
from stigmergy.capture import schema as capture_schema
from stigmergy.capture.errors import CaptureError
from stigmergy.digest import run as digest_run
from stigmergy.digest.settings import DIGEST_CHANNEL_ID_ENV, SLACK_BOT_TOKEN_ENV, DigestSettings
from stigmergy.entities.errors import EntityError
from stigmergy.entities.generator import ENTITY_TYPES, canonical_id_for
from stigmergy.gardener import store as gardener_store
from stigmergy.gardener.schema import JOB_NAME as GARDENER_JOB
from stigmergy.gardener.schema import SEVERITIES as GARDENER_SEVERITIES
from stigmergy.index import check as index_check
from stigmergy.index import store as index_store
from stigmergy.index.errors import StigmergyIndexError
from stigmergy.kernel import registry as kernel_registry
from stigmergy.kernel.normalize import normalize
from stigmergy.librarian import config as librarian_config
from stigmergy.repair import store as repair_store
from stigmergy.repair.errors import RepairError
from stigmergy.repair.schema import ALIAS_OP_NAMES, DELETE_OP_NAMES, KIND_ENTITY_BODY
from stigmergy.repair.schema import JOB_NAME as REPAIR_JOB
from stigmergy.repair.schema import KINDS as REPAIR_KINDS
from stigmergy.review_kinds import ITEM_KINDS, KIND_ALIAS_PROPOSAL, KIND_IDENTITY_PROPOSAL
from stigmergy.server import pilot_report
from stigmergy.server import review as server_review
from stigmergy.server.errors import StartupError
from stigmergy.server.webhook import JOB_NAME as WEBHOOK_JOB

log = logging.getLogger(__name__)

PURGE_JOB, PURGE_DRY_RUN_JOB = "capture-purge", "capture-purge-dry-run"

# The inbox is the doorbell's own read, bounded the way the doorbell bounds it.
INBOX_LIMIT = server_review.DOORBELL_ITEM_LIMIT

# The metrics window: a console chart's x-axis. Clamped, never trusted from the query string.
DEFAULT_METRICS_DAYS, MAX_METRICS_DAYS = 30, 365
# How many capture->filed samples a histogram gets; the percentiles come from the same window.
LATENCY_SAMPLE_LIMIT = 200
# The ledger feed: the newest rows, bounded in SQL (`decisions.recent_decisions`) — the table is
# append-only and never truncated, so every read of it needs a ceiling.
DECISIONS_LIMIT = 200
# A request-scoped page of pending proposals, never the whole table (`repair.store` says the bound
# is the caller's): a nightly proposer can produce a thousand overnight, each carrying its ops.
REPAIR_PENDING_LIMIT = 200
# How deep a cleaned JSON value is walked; past it the subtree is DROPPED — a bound that handed
# back what it declined to clean would be fail-open.
CLEAN_DEPTH = 6

# `entities/resolve` is called as a steward types; one call checks the whole form, never a
# registry-sized list of names.
MAX_RESOLVE_NAMES = 50

# The pre-mint registry check's four verdicts. Two are the gate's own answers (the filing fold and
# the collision fold, in that order — `registered` is strictly inside `collides`); `similar` is an
# ADVISORY listing this module computes for a human to judge and nothing acts on; `clear` is the
# absence of all three. `unchecked` means no registry could be read at all.
VERDICT_REGISTERED = "registered"
VERDICT_COLLIDES = "collides"
VERDICT_SIMILAR = "similar"
VERDICT_CLEAR = "clear"
VERDICT_UNCHECKED = "unchecked"

# Tokens the similarity listing ignores: they match everything and mean nothing about identity.
_SIMILARITY_STOPWORDS = frozenset({"the", "and", "of", "for", "de", "la", "el", "los", "las",
                                   "del", "y", "a", "an", "in", "on", "at", "to"})
_SIMILARITY_MIN_TOKEN = 3
_SIMILARITY_MIN_CONTAINED = 4
_SIMILARITY_LIMIT = 5

# The crons tab's table: file, title, schedule, and WHERE the database truth for "did it run"
# lives — `job_runs` for the two that write one, `index_meta.built_at` for the rebuild, which
# writes none. `schedule_utc` is pinned against the parsed workflow YAML by test.
CRON_WORKFLOWS = (
    {"file": "index-rebuild.yml", "title": "Index rebuild", "schedule_utc": "17 4 * * *",
     "truth": "index_meta.built_at", "dispatch_inputs": ()},
    {"file": "retention-purge.yml", "title": "Retention purge", "schedule_utc": "42 4 * * *",
     "truth": f"job_runs:{PURGE_JOB}", "dispatch_inputs": ("dry_run",)},
    {"file": "gardener.yml", "title": "Gardener", "schedule_utc": "7 5 * * *",
     "truth": f"job_runs:{GARDENER_JOB}", "dispatch_inputs": ()},
    {"file": "repair-propose.yml", "title": "Repair proposer", "schedule_utc": "7 6 * * *",
     "truth": f"job_runs:{REPAIR_JOB}", "dispatch_inputs": ()},
)
DISPATCHABLE = tuple(w["file"] for w in CRON_WORKFLOWS)

# The worker's OWN attempts budget, never the queue CLI's shorter flagless default — comparing a
# long agent item against the CLI's lease calls it dead while its worker is still on it. The one
# declared reach into `stigmergy.librarian` is the config module alone.
WORKER_MAX_ATTEMPTS = queue.DEFAULT_MAX_ATTEMPTS


def worker_visibility_timeout_s() -> int:
    """The lease the deployed worker actually holds, resolved fresh PER CALL — it derives from
    `$STIGMERGY_LIBRARIAN_TIMEOUT_S`, and `fly.toml`'s `[env]` is app-wide, so this process's env
    is the worker's. The meter, the verdicts and Reclaim's default horizon all read THIS, so the
    number the operator sees and the number the button acts on are one number.

    A refused env value falls back to the class default rather than failing the request: `meta()`
    is the console's boot call, and raising would answer a config typo with a login screen. The
    fallback is honest because the same refusal stops the WORKER from booting — there is no live
    lease to misreport."""
    try:
        return librarian_config.resolved_visibility_timeout_s()
    except librarian_config.LibrarianConfigError as ex:   # attribute, never a second import edge
        log.error("admin: the worker lease could not be resolved (%s) — the console falls back to "
                  "the class default %ss; a worker in this environment refuses to boot",
                  ex, librarian_config.DEFAULT_VISIBILITY_TIMEOUT_S)
        return librarian_config.DEFAULT_VISIBILITY_TIMEOUT_S


class AdminBadRequest(Exception):
    """The caller's input is wrong (unknown status, unlisted workflow, malformed body)."""


class AdminNotFound(Exception):
    """The named row does not exist."""


class AdminRefused(Exception):
    """A domain seam refused — the message is the library's own operator-facing sentence."""


def _clean(value) -> str:
    """Control characters stripped, newlines KEPT — the web renders multi-line questions whole;
    HTML escaping is the client's job, not flattening."""
    return textutil.sanitize(str(value or ""))


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _clean_leaves(value, depth: int):
    """`_clean` over every string leaf of a JSON-shaped value; dicts and lists are walked, every
    other scalar passes through. Past `depth` the subtree is DROPPED (`None`), never returned
    unchecked — the same fail-closed bound `server.service._neutralize_report` holds."""
    if depth < 0:
        return None
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, dict):
        return {key: _clean_leaves(item, depth - 1) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_clean_leaves(item, depth - 1) for item in value]
    return value


class AdminService:
    """One instance per process, sharing the process-wide autocommit connection. Concurrency
    invariant: no cursor is ever held across an `await` — the two async digest methods await
    inside `digest.run`, between statements, never mid-cursor."""

    def __init__(self, conn, *, server_settings, admin_settings: AdminSettings, gateway=None,
                 evidence=None):
        self._conn = conn
        self._server = server_settings
        self._admin = admin_settings
        self._gateway = gateway
        # The evidence store the queue archives material into — the same instance the MCP server
        # submits through; a steward registering an entity (ADR 042) submits a capture too.
        self._evidence = evidence

    # ── meta / overview ───────────────────────────────────────────────────────────────────────
    def meta(self) -> dict:
        return {
            "actor_default": self._admin.actor,
            "github": {"configured": self._gateway is not None, "repo": self._admin.github_repo},
            "digest": self._digest_pieces(),
            "workflows": [dict(w) for w in CRON_WORKFLOWS],
            "worker": {"visibility_timeout_s": worker_visibility_timeout_s(),
                       "max_attempts": WORKER_MAX_ATTEMPTS},
            # Every closed vocabulary the console renders ships from HERE, so the frontend never
            # hardcodes a second copy that could drift: `generator.ENTITY_TYPES` first, then the
            # queue's status machine (its terminal subset, and `resolved` as the one legacy word),
            # the repair kinds, the gardener's severities and the review lane's item kinds, the
            # verdicts each kind takes, and the doors. The HUMAN wording for each word is the
            # frontend's (it is copy), keyed on these.
            "entity_types": list(ENTITY_TYPES),
            "statuses": list(capture_schema.STATUSES),
            "terminal_statuses": [s for s in capture_schema.STATUSES
                                  if s in capture_schema.TERMINAL_STATUSES],
            "legacy_statuses": [capture_schema.RESOLVED],
            "repair_kinds": list(REPAIR_KINDS),
            "gardener_severities": list(GARDENER_SEVERITIES),
            "item_kinds": list(ITEM_KINDS),
            "verdicts_by_kind": {kind: sorted(set(verdicts.values()))
                                 for kind, verdicts in server_review.VERDICTS_BY_KIND.items()},
            "decision_sources": list(server_review.DECISION_SOURCES),
            "metrics": {"default_days": DEFAULT_METRICS_DAYS, "max_days": MAX_METRICS_DAYS},
            # The purge form's consequence sentence names this number; it is the library's, not
            # the frontend's to remember.
            "retention": {"default_days": retention.DEFAULT_RETENTION_DAYS},
        }

    # ── inbox: everything waiting on a steward, one list ──────────────────────────────────────
    def inbox(self, *, limit: int = INBOX_LIMIT) -> dict:
        """The unified work list — `server.review.items_for_doorbell`, the SAME read the Slack
        doorbell rings from, so the console and the doorbell can never disagree about what is
        waiting on a steward: the identities and spellings the librarian proposed (read off the
        registry the index snapshot carries) and the nightly repairs. Every item carries the
        ledger's latest decision on it, which is how a steward sees that a second door got there
        first. `truncated` is conservative: a list exactly `limit` long reports it, because the
        read cannot tell "exactly that many" from "more than that"."""
        limit = max(1, int(limit))
        items = self._inbox_items(limit)
        counts = {kind: 0 for kind in ITEM_KINDS}
        for item in items:
            counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        return {"count": len(items), "counts": counts,
                "truncated": len(items) >= limit, "limit": limit, "items": items}

    def _inbox_items(self, limit: int) -> list[dict]:
        """The lane's open items, derived from the registry THIS console serves — the same copy
        the Entities desk checks names against (`index.check.served_registry`: the snapshot, else
        the `--entity-registry` file). The doorbell reads the snapshot alone because it has only a
        connection; a console on a stack with no snapshot (the local recipe) would otherwise list
        every entity in its browser and no proposal in its inbox."""
        text, origin = index_check.served_registry(self._conn,
                                                   self._server.entity_registry_path or None)
        registry = server_review.registry_records(text, origin)
        return [self._inbox_item(item) for item in
                server_review.items_for_doorbell(self._conn, limit=limit, registry=registry)]

    def _inbox_item(self, item: dict) -> dict:
        """One review item, cleaned for the web — EVERY string leaf, by a walk rather than a list
        of keys: the lane's own docstring (`server.review._neutralize_leaves`) records that
        per-field calls "reliably miss a field", and a key added upstream must arrive cleaned by
        default. The lane already serializes the decision's timestamp."""
        return _clean_leaves(item, CLEAN_DEPTH)

    # ── the entity registry this stack serves, and the pre-create check over it ───────────────
    def entities_registry(self) -> dict:
        """Every registered entity (id, name, type, aliases) as the SERVER resolves them — the
        index's snapshot where this database has one, the `--entity-registry` file where it does
        not: `index.check.served_registry`'s order, the same copy the substrate check lints. It is
        the vocabulary, not the corpus: `ops/entity-registry.json` is an ops control file every
        MCP identity already reads through `list_entities`, and this console reads no page to
        list it."""
        registry, about = self._served_registry()
        entities = []
        if registry is not None:
            entities = sorted((self._registry_entry(registry, cid) for cid in registry.entities),
                              key=lambda e: e["name"].casefold())
        by_type = Counter(e["type"] for e in entities)
        return {**about, "count": len(entities), "by_type": dict(sorted(by_type.items())),
                "entities": entities}

    def entities_resolve(self, names) -> dict:
        """The pre-create registry check over names a steward is about to submit — the same
        question the birth gate will ask, asked BEFORE the clone: `Registry.canonical_id` (does
        the filing fold already resolve this spelling? then there is nothing to create) and
        `Registry.collision_id` (would the gate refuse it as confusable with an existing entity?),
        plus an advisory "looks similar" list for the names neither fold catches. The gate runs
        again, against the registry the commit will publish, so this is a warning that is right
        whenever the snapshot is fresh — never a permission."""
        if not isinstance(names, list | tuple) or not all(isinstance(n, str) for n in names):
            raise AdminBadRequest("'names' must be a JSON list of strings")
        if len(names) > MAX_RESOLVE_NAMES:
            raise AdminBadRequest(f"'names' is capped at {MAX_RESOLVE_NAMES} per call")
        cleaned = [" ".join(_clean(n).split()) for n in names]
        registry, about = self._served_registry()
        index = self._similarity_index(registry)
        return {"registry": about,
                "checks": [self._check_name(name, registry, index) for name in cleaned if name]}

    def _served_registry(self):
        """`(Registry | None, about)` — `about` says which copy answered and how fresh it is, so
        every verdict built on it can say what it was checked against. A snapshot the loader
        refuses is a refusal with the loader's own sentence (the substrate check's posture)."""
        text, origin = index_check.served_registry(self._conn,
                                                   self._server.entity_registry_path or None)
        snapshot = index_store.read_ops_file_meta(self._conn, index_store.ENTITY_REGISTRY_RELPATH)
        about = {"available": text is not None,
                 "road": "none" if text is None else ("snapshot" if snapshot else "file"),
                 "source": _clean(snapshot["source"]) if snapshot else "",
                 "refreshed_at": snapshot["refreshed_at"] if snapshot else None}
        if text is None:
            return None, about
        try:
            return kernel_registry.registry_from_text(text, origin), about
        except ValueError as ex:
            raise AdminRefused(str(ex)) from ex

    def _registry_or_none(self):
        """`_served_registry` for a page that must still render when the registry cannot be read:
        the check is advisory there, so a refused snapshot becomes a sentence on the page rather
        than a blank page."""
        try:
            return self._served_registry()
        except AdminRefused as ex:
            return None, {"available": False, "road": "none", "source": "", "refreshed_at": None,
                          "error": _clean(str(ex))}

    @staticmethod
    def _registry_entry(registry, canonical_id: str) -> dict:
        entry = registry.entities.get(canonical_id) or {}
        return {"id": _clean(canonical_id), "name": _clean(entry.get("name")),
                "type": _clean(entry.get("type")),
                "aliases": [_clean(a) for a in entry.get("aliases") or []],
                "proposed": bool(entry.get(kernel_registry.PROPOSED_KEY)),
                "approved_by": _clean(entry.get(kernel_registry.APPROVED_BY_KEY) or ""),
                "proposed_aliases": [_clean(a) for a in
                                     entry.get(kernel_registry.PROPOSED_ALIASES_KEY) or []]}

    def _check_name(self, name: str, registry, index) -> dict:
        """One name's verdict, as the birth gate would give it."""
        check = {"name": name}
        if registry is None:
            return {**check, "verdict": VERDICT_UNCHECKED, "match": None, "similar": []}
        resolved = registry.canonical_id(name)
        if resolved:
            return {**check, "verdict": VERDICT_REGISTERED,
                    "match": self._registry_entry(registry, resolved), "similar": []}
        collision = registry.collision_id(name)
        if collision:
            return {**check, "verdict": VERDICT_COLLIDES,
                    "match": self._registry_entry(registry, collision), "similar": []}
        similar = self._similar_entities(name, index)
        return {**check, "verdict": VERDICT_SIMILAR if similar else VERDICT_CLEAR,
                "match": None, "similar": similar}

    def _similarity_index(self, registry) -> list[tuple[dict, list[tuple[str, str, set[str]]]]]:
        """Every entity's spellings folded ONCE per request — `(entry, [(spelling, key, tokens)])`
        — so checking N names against M entities folds M sets of spellings plus N names, never
        N × M. `[]` when there is no registry."""
        if registry is None:
            return []
        index = []
        for canonical_id in registry.entities:
            entry = self._registry_entry(registry, canonical_id)
            spellings = []
            for spelling in (entry["name"], *entry["aliases"], canonical_id.replace("-", " ")):
                key = normalize(spelling)
                if key:
                    spellings.append((spelling, key, self._identity_tokens(key)))
            index.append((entry, spellings))
        return index

    def _similar_entities(self, name: str, index) -> list[dict]:
        """ADVISORY, for a human's eyes: the registered entities whose id, name or an alias
        contains this name (or is contained in it), or shares a distinctive word with it, under
        the collision fold. Deliberately NOT a gate and never consulted by a write — the gate's
        answer is `collision_id`, and a second, looser "collides" would be a second policy.
        Bounded and ordered so the best candidate is first."""
        key = normalize(name)
        tokens = self._identity_tokens(key)
        scored = []
        for entry, spellings in index:
            best = None
            for spelling, spelling_key, spelling_tokens in spellings:
                if (len(key) >= _SIMILARITY_MIN_CONTAINED and key in spelling_key) or (
                        len(spelling_key) >= _SIMILARITY_MIN_CONTAINED and spelling_key in key):
                    candidate = (1.0, f"part of «{spelling}»" if key in spelling_key
                                 else f"contains «{spelling}»")
                else:
                    shared = tokens & spelling_tokens
                    if not shared:
                        continue
                    candidate = (len(shared) / len(tokens | spelling_tokens),
                                 "shares " + ", ".join(f"«{t}»" for t in sorted(shared)))
                if best is None or candidate[0] > best[0]:
                    best = candidate
            if best is not None:
                scored.append((best[0], entry["name"].casefold(), {**entry, "why": best[1]}))
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [entry for _score, _name, entry in scored[:_SIMILARITY_LIMIT]]

    @staticmethod
    def _identity_tokens(key: str) -> set[str]:
        return {t for t in key.split() if len(t) >= _SIMILARITY_MIN_TOKEN
                and t not in _SIMILARITY_STOPWORDS}

    # ── metrics: the time series the console draws ────────────────────────────────────────────
    def metrics(self, *, days: int = DEFAULT_METRICS_DAYS) -> dict:
        """Aggregates only, over a clamped window. Captures by arrival day and current status
        (`queue.outcomes_by_day`), capture->filed samples (the same list the percentiles are
        cut from), `ask` outcomes per day (`pilot_report.answer_shape_by_day` — the report's own
        classifier, grouped in SQL), calls per day/tool/identity from `audit_log`, each job's run
        history, the ledger's newest rows and the repair table's status counts. Every read is
        bounded by the window or by a ceiling. Nothing here names a page, a question or a
        payload."""
        window = max(1, min(int(days), MAX_METRICS_DAYS))
        return {
            "days": window,
            "captures_by_day": self._captures_by_day(window),
            "filed_latency_ms": queue.filed_latencies_ms(self._conn, limit=LATENCY_SAMPLE_LIMIT),
            "asks_by_day": pilot_report.answer_shape_by_day(self._conn, days=window),
            "calls_by_day": self._calls_by_day(window),
            "calls_by_tool": self._calls_by_tool(window),
            "calls_by_identity": self._calls_by_identity(window),
            "job_history": {job: self._job_runs((job,), limit=60) for job in (
                GARDENER_JOB, REPAIR_JOB, PURGE_JOB, PURGE_DRY_RUN_JOB, WEBHOOK_JOB,
                digest_run.JOB_NAME, digest_run.JOB_NAME_DRY_RUN)},
            "decisions": self._decisions(),
            "repairs": self._repair_counts(),
        }

    def _captures_by_day(self, days: int) -> list[dict]:
        by_day: dict[str, dict] = {}
        for row in queue.outcomes_by_day(self._conn, days=days):
            bucket = by_day.setdefault(row["day"], {"day": row["day"],
                                                    **{s: 0 for s in capture_schema.STATUSES}})
            bucket[row["status"]] = row["count"]
        return [by_day[day] for day in sorted(by_day)]

    def _calls_by_day(self, days: int) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT (ts AT TIME ZONE 'UTC')::date AS day, tool, count(*),"
                " count(*) FILTER (WHERE outcome <> 'ok') FROM audit_log"
                " WHERE ts >= now() - make_interval(days => %s) GROUP BY 1, 2 ORDER BY 1, 2",
                (days,))
            rows = cur.fetchall()
        return [{"day": day.isoformat(), "tool": _clean(tool), "calls": int(calls),
                 "errors": int(errors)} for day, tool, calls, errors in rows]

    def _calls_by_tool(self, days: int) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT tool, count(*), count(*) FILTER (WHERE outcome <> 'ok'),"
                " percentile_disc(0.5) WITHIN GROUP (ORDER BY duration_ms),"
                " percentile_disc(0.95) WITHIN GROUP (ORDER BY duration_ms), max(ts)"
                " FROM audit_log WHERE ts >= now() - make_interval(days => %s)"
                " GROUP BY tool ORDER BY 2 DESC, 1", (days,))
            rows = cur.fetchall()
        return [{"tool": _clean(tool), "calls": int(calls), "errors": int(errors),
                 "p50_ms": float(p50) if p50 is not None else None,
                 "p95_ms": float(p95) if p95 is not None else None, "last_at": _iso(last)}
                for tool, calls, errors, p50, p95, last in rows]

    def _calls_by_identity(self, days: int) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT identity, count(*), count(*) FILTER (WHERE tool = 'ask'),"
                " count(*) FILTER (WHERE tool = 'brain_submit'),"
                " count(*) FILTER (WHERE error_class = 'RateLimitError'), max(ts)"
                " FROM audit_log WHERE ts >= now() - make_interval(days => %s)"
                " GROUP BY identity ORDER BY 2 DESC, 1", (days,))
            rows = cur.fetchall()
        return [{"identity": _clean(identity), "calls": int(calls), "asks": int(asks),
                 "submits": int(submits), "rate_limited": int(limited), "last_at": _iso(last)}
                for identity, calls, asks, submits, limited, last in rows]

    def _decisions(self) -> list[dict]:
        """The ledger's newest rows — bounded in SQL, every decision rather than the latest per
        item: this is a feed, and the doorbell's per-item read has no ceiling by design."""
        return [{"kind": d["item_kind"], "id": d["item_id"], "verdict": _clean(d["verdict"]),
                 "actor": _clean(d["actor"]), "source": _clean(d["source"]),
                 "created_at": _iso(d["created_at"])}
                for d in decisions.recent_decisions(self._conn, limit=DECISIONS_LIMIT)]

    def _repair_counts(self) -> dict:
        """`by_status` is the store's own aggregate over the WHOLE table; `recent` is a bounded
        page of decided rows for a timeline. The two are not the same population and are not
        summed together."""
        by_status = repair_store.counts_by_status(self._conn)
        decided = repair_store.recent_decided(self._conn, limit=200)
        by_kind = Counter(row.get("kind", "") for row in decided)
        return {"pending": by_status.get("pending", 0), "by_status": by_status,
                "recent_by_kind": dict(by_kind),
                "recent": [{"id": row["id"], "kind": row.get("kind", ""),
                            "status": row["status"], "decided_at": _iso(row.get("decided_at")),
                            "created_at": _iso(row.get("created_at"))}
                           for row in decided]}

    def overview(self) -> dict:
        counts = queue.counts_by_status(self._conn)
        latest = {w["file"]: self._latest_job_run(self._truth_jobs(w)) for w in CRON_WORKFLOWS
                  if w["truth"].startswith("job_runs:")}
        gardener = gardener_store.latest_completed_run(self._conn)
        severity_counts: dict[str, int] = {}
        if gardener is not None:
            for f in gardener_store.findings_for_run(self._conn, gardener["id"]):
                severity_counts[f["severity"]] = severity_counts.get(f["severity"], 0) + 1
        return {
            "queue": {"counts": counts},
            "in_flight": self._in_flight(),
            "ingest_errors": self._ingest_errors(limit=5),
            "crons": {"latest_runs": latest, "index_built_at": self._built_at()},
            "gardener": {"run": self._run_row(gardener), "severity_counts": severity_counts},
            "digest": {"last_window_until": self._digest_watermark()},
            "admin_actions": admin_schema.recent_actions(self._conn, limit=5),
        }

    # ── queue: read-only, plus the two operator acts on the whole queue ───────────────────────
    def queue_list(self, *, statuses: list[str] | None = None, submitter: str | None = None,
                   limit: int = queue.DEFAULT_LIST_LIMIT) -> dict:
        try:
            rows = queue.list_all_submissions(
                self._conn, statuses=statuses or None, submitter=submitter, limit=limit) \
                if submitter else queue.query_submissions(
                    self._conn, statuses=statuses or None, limit=limit)
        except ValueError as ex:   # unknown status — the library's own sentence names the vocabulary
            raise AdminBadRequest(str(ex)) from ex
        return {"counts": queue.counts_by_status(self._conn),
                "submissions": [self._traced_fields(row) for row in rows]}

    def queue_show(self, submission_id: int) -> dict:
        trace = queue.get_submission_trace(self._conn, submission_id)
        if trace is None:
            raise AdminNotFound(f"no submission {submission_id}")
        return self._traced_fields(trace)

    def queue_reclaim(self, *, actor: str, visibility_timeout_s: int | None = None) -> dict:
        # The WORKER's lease, never the queue CLI's shorter one: an unqualified Reclaim means
        # "recover whatever the worker abandoned", and the meter beside this button renders the
        # same number. The clamp wraps BOTH branches — the default carries operator-controlled
        # env data, and a negative horizon inverts `release_expired`'s predicate, reading EVERY
        # claimed row as expired.
        timeout = max(0, int(worker_visibility_timeout_s() if visibility_timeout_s is None
                             else visibility_timeout_s))
        return self._mutate("queue.reclaim", actor, {"visibility_timeout_s": timeout},
                            lambda by: queue.release_expired(
                                self._conn, visibility_timeout_s=timeout,
                                max_attempts=WORKER_MAX_ATTEMPTS))

    def queue_purge(self, *, actor: str, older_than_days: int = retention.DEFAULT_RETENTION_DAYS,
                    dry_run: bool = False) -> dict:
        days = max(0, int(older_than_days))
        if dry_run:   # a preview changes nothing and is not a mutation — no admin_actions row
            return retention.purge(self._conn, older_than_days=days, dry_run=True)
        return self._mutate("queue.purge", actor, {"older_than_days": days},
                            lambda by: retention.purge(self._conn, older_than_days=days,
                                                       dry_run=False))

    # ── gardener ──────────────────────────────────────────────────────────────────────────────
    def gardener_state(self) -> dict:
        run = gardener_store.latest_completed_run(self._conn)
        findings = ([self._finding(f) for f in
                     gardener_store.findings_for_run(self._conn, run["id"])]
                    if run is not None else [])
        return {"run": self._run_row(run), "findings": findings,
                "history": self._job_runs((GARDENER_JOB,), limit=10)}

    # ── digest ────────────────────────────────────────────────────────────────────────────────
    def digest_state(self) -> dict:
        return {"pieces": self._digest_pieces(),
                "last_window_until": self._digest_watermark(),
                "history": self._job_runs((digest_run.JOB_NAME, digest_run.JOB_NAME_DRY_RUN),
                                          limit=10)}

    async def digest_preview(self) -> dict:
        result = await digest_run.run_digest(
            self._conn, settings=self._digest_settings(), channels_path=self._admin.channels_path,
            gateway=None, dry_run=True, since_override=None)
        return {"body": result.body, "since": _iso(result.since), "until": _iso(result.until)}

    async def digest_post(self, *, actor: str) -> dict:
        pieces = self._digest_pieces()
        for key, sentence in (
                ("digest_channel_id", f"${DIGEST_CHANNEL_ID_ENV} is not set — the digest has no "
                                      f"channel to post to"),
                ("bot_token", f"${SLACK_BOT_TOKEN_ENV} is not set — the digest cannot post "
                              f"without the bot token")):
            if not pieces[key]:
                raise AdminRefused(sentence)
        # The Slack SDK is loaded only at the moment a real post is about to happen.
        from stigmergy.slack.bolt_gateway import build_gateway

        gateway = build_gateway(os.environ[SLACK_BOT_TOKEN_ENV])
        settings = self._digest_settings()

        async def _post(_by: str):
            return await digest_run.run_digest(
                self._conn, settings=settings, channels_path=self._admin.channels_path,
                gateway=gateway, dry_run=False, since_override=None)

        result = await self._mutate_async("digest.post", actor, {}, _post)
        response = {"posted": result.posted, "run_id": result.run_id,
                    "since": _iso(result.since), "until": _iso(result.until)}
        if result.posted and result.run_id is None:
            # The digest CLI's own warning, the console edition: the post landed but the watermark
            # write failed, so the next run may re-post this window as a duplicate.
            response["warning"] = ("posted, but the watermark could not be recorded — the next "
                                   "run may re-post this window as a duplicate; check job_runs "
                                   "for a 'digest' row before running again")
        return response

    # ── index ─────────────────────────────────────────────────────────────────────────────────
    def index_state(self) -> dict:
        return {"meta": index_store.read_meta(self._conn), "zones": self._zone_counts(),
                "entity_registry": self._ops_file_state(index_store.ENTITY_REGISTRY_RELPATH),
                "ops_files": {relpath: self._ops_file_state(relpath)
                              for relpath in index_store.OPS_FILE_RELPATHS},
                "webhook": self._job_runs((WEBHOOK_JOB,), limit=10)}

    def _ops_file_state(self, relpath: str) -> dict | None:
        """Which copy of one `ops/` control file this stack is serving, and how fresh — `None`
        when the index carries no snapshot, which is the console's way of saying "every server
        here is answering from its own file". The copies the deployed groups read are database
        rows no operator holds a checkout of, so "is it fresh, and from which sha?" has no other
        surface (issues #74 and #79) — and for `ops/identities.json` the question is who can READ,
        not what ranks first."""
        state = index_store.read_ops_file_meta(self._conn, relpath)
        if state is None:
            return None
        return {"source": _clean(state["source"]), "refreshed_at": state["refreshed_at"]}

    def index_substrate_check(self) -> dict:
        # The copy the SERVER serves — the snapshot where this index has one, the
        # `--entity-registry` file where it does not. Linting the file on a deployed console lints
        # the copy baked at DEPLOY time, so every entity minted since the rollout reports as
        # `anchored-but-unregistered` while the server has full records for it (issue #74).
        registry = index_check.served_registry(self._conn,
                                               self._server.entity_registry_path or None)
        try:
            findings = index_check.run_checks(self._conn, registry=registry)
        except (StigmergyIndexError, ValueError) as ex:
            # `ValueError` too: the registry this check reads is loaded by
            # `kernel.registry.load_registry`, which raises a bare one (a nameless entity, a
            # non-object top level) — exactly the substrate `index.check.registry_ids` exists to
            # stop blessing. Caught here it is a REFUSAL with the loader's own sentence naming the
            # file and the entity; uncaught it was a 500 reading "the operation failed
            # (ValueError)", which tells an operator nothing about a file they can fix.
            raise AdminRefused(str(ex)) from ex
        return {"findings": findings,
                "errors": sum(1 for f in findings if f["severity"] == "error"),
                "warnings": sum(1 for f in findings if f["severity"] == "warn")}

    # ── the proposals: what the librarian created unconfirmed, and the steward's three verbs ──
    def entities_list(self) -> dict:
        """Every identity and spelling the librarian proposed, off the same read as the inbox,
        plus the registry the proposals will join. The proposals carry `merge_candidates` from the
        lane; `checks` adds the registry verdict for the proposed name (already registered under
        another spelling? colliding?), which is the Merge picker's strongest hint."""
        registry, about = self._registry_or_none()
        index = self._similarity_index(registry)
        items = self._inbox_items(INBOX_LIMIT)
        proposals = [self._with_registry_check(item, registry, index)
                     for item in items if item["kind"] == KIND_IDENTITY_PROPOSAL]
        aliases = [item for item in items if item["kind"] == KIND_ALIAS_PROPOSAL]
        return {"proposals": proposals, "aliases": aliases, "registry_check": dict(about)}

    def entities_show(self, entity_id: str) -> dict:
        """One proposed identity, as the list carries it — the detail page's read."""
        for item in self._inbox_items(INBOX_LIMIT):
            if item["kind"] == KIND_IDENTITY_PROPOSAL and str(item["id"]) == str(entity_id):
                registry, _about = self._registry_or_none()
                return self._with_registry_check(item, registry, self._similarity_index(registry))
        raise AdminNotFound(f"no proposed entity {_clean(entity_id)!r} is waiting")

    def _with_registry_check(self, item: dict, registry, index) -> dict:
        """The birth gate's verdict on the proposal's own name, against the registry WITHOUT the
        proposal itself — a proposal always resolves to itself, which says nothing."""
        others = None
        if registry is not None:
            others = kernel_registry.Registry()
            for cid, entry in registry.entities.items():
                if cid != item["id"]:
                    kernel_registry.index_entity(others, cid, entry)
        check = self._check_name(item.get("name", ""), others, self._similarity_index(others))
        return {**item, "check": check}

    def entity_decide(self, item_kind: str, item_id: str, *, actor: str, verdict: str,
                      into: str = "", notes: str = "") -> dict:
        """A steward's decision on a proposal through `server.review.decide_and_record` — the
        same sequence the review lane runs (land the commit through the governed door, then the
        ledger row). `review_decide`'s steward check is deliberately not reached: it is for a
        resolved identity, and `actor` here is free text behind the operator token (ADR 029/030
        D2). Nothing is caught inside `_do`, so `_mutate` records the library's own class name
        before `EntityError` becomes `AdminRefused`."""
        verdicts = server_review.VERDICTS_BY_KIND.get(item_kind)
        if verdicts is None or item_kind not in (KIND_IDENTITY_PROPOSAL, KIND_ALIAS_PROPOSAL):
            raise AdminBadRequest(f"item_kind must be {KIND_IDENTITY_PROPOSAL!r} or "
                                  f"{KIND_ALIAS_PROPOSAL!r}")
        stored = verdicts.get(str(verdict or "").strip().lower())
        if stored is None:
            raise AdminBadRequest(f"verdict for {item_kind} must be one of "
                                  f"{', '.join(sorted(verdicts))}")
        item_id = str(item_id or "").strip()
        target = " ".join(str(into or "").split())
        if stored == server_review.MERGE and not target:
            raise AdminBadRequest("merge needs `into`: the registered entity this proposal really is")
        clean_notes = capture_schema.clean_note(notes)
        args = {"item_kind": item_kind, "item_id": item_id, "verdict": stored, "into": target}

        def _do(by: str) -> dict:
            action, extra = self._decision_action(item_kind, item_id, stored, by, target)
            result = server_review.decide_and_record(
                self._conn, repo_url=self._server.librarian_repo_url, item_kind=item_kind,
                item_id=item_id, verdict=stored, actor=by, source=server_review.SOURCE_ADMIN,
                notes=clean_notes, action=action, extra=extra)
            return {"recorded": stored, "item_kind": item_kind, "item_id": item_id,
                    "commit": result["commit"], "summary": _clean(result["summary"]),
                    "reanchored": list(result.get("reanchored") or []), "into": target}

        try:
            return self._mutate("entities.decide", actor, args, _do)
        except EntityError as ex:
            # Caught HERE, outside `_mutate`: the row already captured the ORIGINAL class name.
            raise AdminRefused(str(ex)) from ex

    @staticmethod
    def _decision_action(item_kind: str, item_id: str, stored: str, by: str, into: str):
        """`(action, ledger extra)` — the `entities.decide` call the door runs in its clone."""
        from datetime import date

        from stigmergy.entities import decide as entities_decide
        from stigmergy.review_kinds import split_alias_item_id
        today = date.today().isoformat()
        if item_kind == KIND_ALIAS_PROPOSAL:
            entity_id, alias = split_alias_item_id(item_id)
            if not entity_id:
                raise AdminBadRequest("an alias item id is `<entity id>:<alias>`")
            if stored == server_review.APPROVE:
                return (lambda repo: entities_decide.approve_alias(
                    repo, entity_id=entity_id, alias=alias, approved_by=by, today=today)), {}
            return (lambda repo: entities_decide.decline_alias(
                repo, entity_id=entity_id, alias=alias, today=today)), {}
        if stored == server_review.MERGE:
            return (lambda repo: entities_decide.merge_entity(
                repo, entity_id=item_id, into=into, approved_by=by, today=today)), {"into": into}
        if stored == server_review.APPROVE:
            return (lambda repo: entities_decide.approve_entity(
                repo, entity_id=item_id, approved_by=by, today=today)), {}
        return (lambda repo: entities_decide.decline_entity(
            repo, entity_id=item_id, today=today)), {}

    def entity_create(self, *, actor: str, name: str, entity_type: str, about: str,
                      entity_id: str = "", aliases: str = "") -> dict:
        """A steward introducing an entity nobody proposed (ADR 042): what they know about it is
        queued as a capture carrying the registration — `server.review.commission_registration` —
        and the librarian writes the page, anchors the note and births the entity confirmed by the
        steward. A name the served registry already resolves is refused here, before anything is
        queued: the entity exists, so the thing to do is capture about it."""
        clean_name = " ".join(str(name or "").split())
        clean_type = str(entity_type or "").strip().lower()
        clean_about = str(about or "").strip()
        missing = [field for field, value in (("name", clean_name), ("entity_type", clean_type),
                                              ("about", clean_about)) if not value]
        if missing:
            raise AdminBadRequest(
                f"registering an entity needs {' and '.join(missing)}: name (its page title), "
                f"entity_type (one of {', '.join(ENTITY_TYPES)}) and about — what it is, in your "
                f"own words, which is what the librarian writes its page from")
        if clean_type not in ENTITY_TYPES:
            raise AdminBadRequest(
                f"entity_type {clean_type!r} is not one of {', '.join(ENTITY_TYPES)}")
        resolved_id = str(entity_id or "").strip() or canonical_id_for(clean_name)
        if resolved_id != canonical_id_for(clean_name):
            raise AdminBadRequest(
                f"entity_id {resolved_id!r} is not the slug of {clean_name!r} "
                f"({canonical_id_for(clean_name)!r}) — the registry is derived from the page, so "
                f"the id is the name's")
        registry, _about = self._registry_or_none()
        known = registry.canonical_id(clean_name) if registry is not None else ""
        if known:
            raise AdminBadRequest(
                f"{clean_name!r} already resolves to the registered entity {known!r} — nothing to "
                f"register; capture what you know about it and the librarian anchors it there")
        alias_list = [a.strip() for a in str(aliases or "").split(",") if a.strip()]
        args = {"entity_id": resolved_id, "name": clean_name, "entity_type": clean_type,
                "about_chars": len(clean_about)}

        def _do(by: str) -> dict:
            return server_review.commission_registration(
                self._conn, self._evidence, name=clean_name, entity_type=clean_type,
                aliases=alias_list, about=clean_about, actor=by, source=server_review.SOURCE_ADMIN)

        try:
            return self._mutate("entities.create", actor, args, _do)
        except (EntityError, CaptureError) as ex:
            raise AdminRefused(str(ex)) from ex

    # ── repair proposals (read, and the second governed Approve — ADR 039) ────────────────────
    def repairs_list(self) -> dict:
        """Everything waiting on a steward, plus what was recently decided.

        Both halves, and the second is not decoration: a REJECTED row is the dismissal memory the
        proposer skips against, so "why does the nightly run not propose this any more" is only
        answerable from the decided list. A `failed` row lives there too — an apply that a gate
        refused stays visible with its reason instead of quietly returning to the queue."""
        pending = repair_store.pending_proposals(self._conn, limit=REPAIR_PENDING_LIMIT)
        return {"pending": [self._proposal(row) for row in pending],
                "pending_truncated": len(pending) >= REPAIR_PENDING_LIMIT,
                "pending_limit": REPAIR_PENDING_LIMIT,
                "counts": repair_store.counts_by_status(self._conn),
                "recent": [self._proposal(row) for row in
                           repair_store.recent_decided(self._conn, limit=20)],
                "history": self._job_runs((REPAIR_JOB,), limit=10)}

    def repair_show(self, proposal_id: int) -> dict:
        row = repair_store.proposal(self._conn, proposal_id)
        if row is None:
            raise AdminNotFound(f"proposal {proposal_id} does not exist")
        return self._proposal(row)

    def repair_approve(self, proposal_id: int, *, actor: str) -> dict:
        """Apply the proposal through `server.review.apply_repair_and_record` (ADR 039) — the same
        ordering the review lane runs, for the same reason `entity_approve` shares the mint
        sequence: two copies of an irreversible ordering are two places for it to be reordered.

        `review_decide`'s per-path steward check is deliberately not reached, exactly as
        `entity_approve` does not reach its steward check: that guard is for a RESOLVED identity,
        and `actor` here is free text behind the operator token (ADR 029/030 D2). The console's
        authorization IS the token.

        Nothing is caught inside `_do`, so `_mutate` records the library's own class name in
        `admin_actions` before `RepairError` becomes `AdminRefused` for the caller — the shape
        `entity_approve` established.
        """
        proposal = repair_store.proposal(self._conn, proposal_id)
        if proposal is None:
            raise AdminNotFound(f"proposal {proposal_id} does not exist")

        def _do(by: str) -> dict:
            return server_review.apply_repair_and_record(
                self._conn, repo_url=self._server.librarian_repo_url, proposal=proposal, actor=by,
                source=server_review.SOURCE_ADMIN)

        try:
            return self._mutate("repairs.approve", actor, {"id": proposal_id}, _do)
        except RepairError as ex:
            # Caught HERE, outside `_mutate`, for `entity_approve`'s reason: the row already
            # captured the ORIGINAL class name. Every sentence `repair.remote` raises is written to
            # be published to a steward, so it crosses as the refusal's own text.
            raise AdminRefused(str(ex)) from ex

    def repair_reject(self, proposal_id: int, *, actor: str, reason: str) -> dict:
        """Decline it, through the same shared writer the review lane rejects with — the reason
        lands on the PROPOSAL and in the ledger, which is what stops the proposer re-deriving the
        same repair tomorrow."""
        proposal = repair_store.proposal(self._conn, proposal_id)
        if proposal is None:
            raise AdminNotFound(f"proposal {proposal_id} does not exist")
        return self._mutate(
            "repairs.reject", actor, {"id": proposal_id},
            lambda by: server_review.reject_repair_and_record(
                self._conn, proposal=proposal, actor=by, source=server_review.SOURCE_ADMIN,
                reason=reason))

    # ── activity ──────────────────────────────────────────────────────────────────────────────
    def activity(self) -> dict:
        return {
            "report": pilot_report.build_report(self._conn),
            "by_identity_tool": self._audit_aggregate(),
            "ask_questions": self._ask_questions(),
            "rate_limited": self._rate_limited(),
            "admin_actions": admin_schema.recent_actions(self._conn, limit=50),
        }

    # ── worker ────────────────────────────────────────────────────────────────────────────────
    def worker_status(self) -> dict:
        measured = latency.summarize(queue.filed_latencies_ms(self._conn))
        return {"counts": queue.counts_by_status(self._conn), "in_flight": self._in_flight(),
                "latency": measured.as_json(),
                "visibility_timeout_s": worker_visibility_timeout_s(),
                "max_attempts": WORKER_MAX_ATTEMPTS}

    # ── crons ─────────────────────────────────────────────────────────────────────────────────
    def crons_state(self) -> dict:
        rows = []
        for w in CRON_WORKFLOWS:
            rows.append({**w,
                         "latest_run": (self._latest_job_run(self._truth_jobs(w))
                                        if w["truth"].startswith("job_runs:") else None),
                         "index_built_at": (self._built_at()
                                            if w["truth"] == "index_meta.built_at" else None)})
        state = {"configured": self._gateway is not None, "workflows": rows}
        if self._gateway is None:
            return state
        try:
            by_path = {w["path"].rsplit("/", 1)[-1]: w for w in self._gateway.workflows()}
            for row in rows:
                remote = by_path.get(row["file"])
                row["state"] = remote["state"] if remote else "unknown"
                row["runs"] = self._gateway.runs(row["file"], limit=5)
        except ActionsError as ex:
            state["github_error"] = str(ex)
        return state

    def cron_dispatch(self, workflow_file: str, *, actor: str, inputs: dict | None = None) -> dict:
        self._require_workflow(workflow_file)
        gateway = self._require_gateway()
        declared = next(w["dispatch_inputs"] for w in CRON_WORKFLOWS
                        if w["file"] == workflow_file)
        cleaned = {}
        for key, value in (inputs or {}).items():
            if key not in declared:
                raise AdminBadRequest(
                    f"workflow {workflow_file} declares no {key!r} input"
                    + (f" (accepted: {', '.join(declared)})" if declared else ""))
            cleaned[key] = "true" if value in (True, "true") else "false"
        self._mutate(f"cron.dispatch:{workflow_file}", actor, {"inputs": cleaned},
                     lambda by: gateway.dispatch(workflow_file, inputs=cleaned or None))
        return {"dispatched": workflow_file, "inputs": cleaned}

    def cron_set_enabled(self, workflow_file: str, *, actor: str, enabled: bool) -> dict:
        self._require_workflow(workflow_file)
        gateway = self._require_gateway()
        verb = "enable" if enabled else "disable"
        self._mutate(f"cron.{verb}:{workflow_file}", actor, {},
                     lambda by: gateway.set_enabled(workflow_file, enabled=enabled))
        return {"workflow": workflow_file, "enabled": enabled}

    # ── internals ─────────────────────────────────────────────────────────────────────────────
    def _mutate(self, action: str, actor: str, args: dict, fn):
        """Run one mutation with `admin_actions` bookkeeping around it. The actor falls back to
        the configured default — attribution, not authorization, exactly `--by`'s contract. Any
        failure (a domain refusal included) records `outcome='error'` with the class and
        re-raises; the bookkeeping write itself may fail without failing the work."""
        by = (actor or "").strip() or self._admin.actor
        try:
            result = fn(by)
        except CaptureError as ex:
            admin_schema.record_action(self._conn, actor=by, action=action, args=args,
                                       outcome="error", error_class=ex.__class__.__name__)
            raise AdminRefused(str(ex)) from ex
        except Exception as ex:
            admin_schema.record_action(self._conn, actor=by, action=action, args=args,
                                       outcome="error", error_class=ex.__class__.__name__)
            raise
        admin_schema.record_action(self._conn, actor=by, action=action, args=args, outcome="ok")
        return result

    async def _mutate_async(self, action: str, actor: str, args: dict, fn):
        """`_mutate` for an awaitable mutation, to the letter — actor fallback, an `admin_actions`
        row on both outcomes, `CaptureError` renamed to `AdminRefused` so a domain refusal reaches
        the operator as the routes' 409 with the library's own sentence, and everything else
        re-raised unrenamed. The bookkeeping write itself may fail without failing the work.

        The two are kept as twins rather than merged: no cursor may be held across an `await`
        (this class's own concurrency invariant), and a shared body would have to be async, pulling
        every sync caller into an event loop it does not have."""
        by = (actor or "").strip() or self._admin.actor
        try:
            result = await fn(by)
        except CaptureError as ex:
            admin_schema.record_action(self._conn, actor=by, action=action, args=args,
                                       outcome="error", error_class=ex.__class__.__name__)
            raise AdminRefused(str(ex)) from ex
        except Exception as ex:
            admin_schema.record_action(self._conn, actor=by, action=action, args=args,
                                       outcome="error", error_class=ex.__class__.__name__)
            raise
        admin_schema.record_action(self._conn, actor=by, action=action, args=args, outcome="ok")
        return result

    def _require_workflow(self, workflow_file: str) -> None:
        if workflow_file not in DISPATCHABLE:
            raise AdminBadRequest(
                f"{workflow_file!r} is not a console-drivable workflow "
                f"(allowed: {', '.join(DISPATCHABLE)})")

    def _require_gateway(self):
        if self._gateway is None:
            raise AdminRefused(
                "GitHub is not configured — set the admin GitHub token to drive workflows from "
                "here; the database truth on this page stays readable without it")
        return self._gateway

    def _digest_settings(self) -> DigestSettings:
        try:
            return DigestSettings.from_args(None)
        except StartupError as ex:
            raise AdminRefused(str(ex)) from ex

    def _digest_pieces(self) -> dict:
        path = self._admin.channels_path
        return {"digest_channel_id": bool(os.environ.get(DIGEST_CHANNEL_ID_ENV)),
                "bot_token": bool(os.environ.get(SLACK_BOT_TOKEN_ENV)),
                "channels_path": bool(path), "channels_file_exists": bool(path) and
                os.path.exists(path)}

    def _digest_watermark(self) -> str | None:
        return digest_run.last_window_until(self._conn)

    def _truth_jobs(self, workflow: dict) -> tuple[str, ...]:
        job = workflow["truth"].split(":", 1)[1]
        return (job, f"{job}-dry-run") if job == PURGE_JOB else (job,)

    def _latest_job_run(self, jobs: tuple[str, ...]) -> dict | None:
        rows = self._job_runs(jobs, limit=1)
        return rows[0] if rows else None

    def _job_runs(self, jobs: tuple[str, ...], *, limit: int) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, job, status, started_at, finished_at, stats, error FROM job_runs"
                " WHERE job = ANY(%s) ORDER BY started_at DESC LIMIT %s",
                (list(jobs), max(1, min(int(limit), 100))))
            rows = cur.fetchall()
        return [{"id": r[0], "job": r[1], "status": r[2], "started_at": _iso(r[3]),
                 "finished_at": _iso(r[4]), "stats": r[5] or {}, "error": _clean(r[6])}
                for r in rows]

    def _ingest_errors(self, *, limit: int) -> dict:
        with self._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ingest_errors WHERE NOT resolved")
            total = cur.fetchone()[0]
            cur.execute(
                "SELECT id, source, source_doc_id, stage, error, attempts, last_at"
                " FROM ingest_errors WHERE NOT resolved ORDER BY last_at DESC LIMIT %s",
                (max(1, min(int(limit), 100)),))
            rows = cur.fetchall()
        return {"unresolved": total,
                "rows": [{"id": r[0], "source": r[1], "source_doc_id": r[2], "stage": r[3],
                          "error": _clean(r[4]), "attempts": r[5], "last_at": _iso(r[6])}
                         for r in rows]}

    def _built_at(self) -> str | None:
        meta = index_store.read_meta(self._conn)
        return meta.get("built_at") if meta else None

    def _zone_counts(self) -> dict[str, int]:
        """The one `pages_index` read this package makes — an AGGREGATE, no content columns, and
        a declared entry on the architecture test's reader-exception list."""
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT zone, count(*) FROM pages_index GROUP BY zone ORDER BY zone")
                return dict(cur.fetchall())
        except Exception:  # noqa: BLE001 — no index yet is a state, not an error
            return {}

    def _in_flight(self) -> list[dict]:
        rows = queue.query_in_flight(self._conn,
                                     visibility_timeout_s=worker_visibility_timeout_s())
        for row in rows:
            exhausted = int(row["attempts"]) >= WORKER_MAX_ATTEMPTS
            # `stigmergy-librarian status`'s three verdicts, same order, same facts beside them.
            if not row["lease_expired"]:
                row["verdict"] = "within its lease — a worker is presumably on it"
            elif exhausted:
                row["verdict"] = (f"lease expired and every delivery is burned "
                                  f"({row['attempts']}/{WORKER_MAX_ATTEMPTS}) — the next sweep "
                                  f"fails this row and records an ingest error")
            else:
                row["verdict"] = ("lease expired — a live worker would have finished or renewed "
                                  "it by now; the next sweep returns it to the queue with an "
                                  "attempt burned")
        return rows

    def _run_row(self, run: dict | None) -> dict | None:
        if run is None:
            return None
        return {"id": run["id"], "started_at": _iso(run.get("started_at")),
                "finished_at": _iso(run.get("finished_at")), "stats": run.get("stats") or {}}

    def _finding(self, finding: dict) -> dict:
        """`**finding` carries every column forward, so a NEW one arrives here unsanitized by
        default — `subjects` did exactly that when the repair loop needed the subject pages as
        data. A page path is a filename somebody chose, so it is cleaned element by element, on
        the same reasoning as `subject`, which is the comma-joined form of this same list."""
        return {**finding, "subject": _clean(finding["subject"]),
                "subjects": [_clean(s) for s in (finding.get("subjects") or ())],
                "detail": _clean(finding["detail"]),
                "suggested_action": _clean(finding["suggested_action"]),
                "created_at": _iso(finding.get("created_at"))}

    def _proposal(self, row: dict) -> dict:
        """A `repair_proposals` row, cleaned for the web. **Everything here is untrusted**, and by a
        longer road than most: `rationale` and every `note` were written by a model that had just
        read pages somebody else wrote, and `path`/`link` are filenames.

        `**row` carries every column forward and the free-text ones are then named and cleaned over
        the top. The columns NOT named here ride through unsanitized on purpose — `kind`, `status`
        and `content_key` are constrained or derived, and `model_id` is operator configuration —
        so a new free-text column is a new line in the override list, not a silent pass-through.

        `error` is the sentence `repair.remote` raised, which is written to be published; it is
        cleaned anyway, on the same reasoning every other operator-facing string here is."""
        return {**row,
                "id": row["id"], "created_at": _iso(row.get("created_at")),
                "decided_at": _iso(row.get("decided_at")),
                "rationale": _clean(row.get("rationale")), "notes": _clean(row.get("notes")),
                "error": _clean(row.get("error")), "decided_by": _clean(row.get("decided_by")),
                "target_paths": [_clean(p) for p in (row.get("target_paths") or ())],
                "ops": [self._op(o) for o in (row.get("ops") or ())]}

    @staticmethod
    def _op(op: dict) -> dict:
        """One op, cleaned — and shaped by its own KIND rather than by one fixed field list.

        The additive kinds carry a link and a note; `entity-body` carries the drafted prose and,
        sometimes, a role. A single four-field reshape dropped the draft entirely, which for that
        kind removes the only thing a steward has to judge — and it would do it silently, since a
        missing key renders as an empty cell. Every value goes through `_clean`, which strips
        control characters and KEEPS newlines: a body flattened to one line is a body nobody can
        read as the page it would become.

        `delete` and `entity-alias` are the shapes that reach the console with LESS than they were
        stored with: `planned_after` is a whole page per op, and it is the apply's contract with
        its own recomputation rather than something a steward reads. What the console owes here is
        which pages stop existing or which identity absorbs which, and the op NAME plus the path is
        all of it.

        Both are matched as GROUPS, imported from `repair.schema` rather than rebuilt from the
        individual names here: matching one name at a time is how one op of a kind gets a link
        column nobody asked for, and a fifth op added to either kind must not reach this console
        still wearing the additive shape. `tests/test_architecture.py` pins these two tuples
        against the CLI preview's own tables for that reason."""
        kind = _clean(op.get("op"))
        common = {"op": kind, "path": _clean(op.get("path"))}
        if kind == KIND_ENTITY_BODY:
            return {**common, "body_markdown": _clean(op.get("body_markdown")),
                    "role": _clean(op.get("role"))}
        if kind in DELETE_OP_NAMES or kind in ALIAS_OP_NAMES:
            return common
        return {**common, "link": _clean(op.get("link")), "note": _clean(op.get("note"))}

    def _traced_fields(self, row: dict) -> dict:
        """Sanitize every untrusted string a queue row carries, structure untouched.

        `report` and `hints` are JSONB with no CHECK constraint under them, so both are shaped only
        when they actually ARE objects: a scalar left there by any writer this console does not own
        travels through unshaped rather than taking the whole detail view down with it."""
        out = dict(row)
        for key in ("excerpt", "error"):
            if key in out:
                out[key] = _clean(out[key])
        if out.get("events"):
            out["events"] = [{**e, "actor": _clean(e.get("actor")), "note": _clean(e.get("note")),
                              "event": _clean(e.get("event")), "at": _clean(e.get("at"))}
                             for e in out["events"]]
        if out.get("report") and isinstance(out["report"], dict):
            out["report"] = {k: (_clean(v) if isinstance(v, str) else v)
                             for k, v in out["report"].items()}
        if out.get("hints") and isinstance(out["hints"], dict):
            out["hints"] = {k: (_clean(v) if isinstance(v, str) else v)
                            for k, v in out["hints"].items()}
        return out

    def _audit_aggregate(self) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT identity, tool, count(*), avg(duration_ms), max(ts) FROM audit_log"
                " GROUP BY identity, tool ORDER BY identity, tool")
            rows = cur.fetchall()
        return [{"identity": r[0], "tool": r[1], "calls": r[2],
                 "avg_duration_ms": float(r[3]) if r[3] is not None else None,
                 "last_at": _iso(r[4])} for r in rows]

    def _ask_questions(self, *, limit: int = 200) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT args ->> 'question' FROM audit_log"
                " WHERE tool = 'ask' AND outcome = 'ok' AND args ->> 'question' IS NOT NULL"
                " ORDER BY 1 LIMIT %s", (max(1, min(int(limit), 1000)),))
            return [_clean(row[0]) for row in cur.fetchall()]

    def _rate_limited(self, *, limit: int = 50) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT ts, identity, tool FROM audit_log WHERE error_class = 'RateLimitError'"
                " ORDER BY ts DESC LIMIT %s", (max(1, min(int(limit), 500)),))
            rows = cur.fetchall()
        return [{"ts": _iso(r[0]), "identity": r[1], "tool": r[2]} for r in rows]
