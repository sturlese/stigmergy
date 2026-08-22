"""BrainService — the transport-agnostic serving core: read primitives, the write half and the
governed deletion door, every entry point riding the ONE `_call`/`call_async` seam for the
rate-limit check
and the audit row. Attribution and scope are decided HERE and nowhere else, because only this
layer knows the caller: `submitted_by` is `self.identity`, read scope is `self.audiences`.
"""
import logging
import time
from datetime import date

from stigmergy import text as textutil
from stigmergy.capture import evidence as evidence_plane
from stigmergy.capture import queue
from stigmergy.capture import schema as capture_schema
from stigmergy.capture.errors import CaptureError
from stigmergy.capture.schema import ensure_capture_schema
from stigmergy.index import rank, search, store
from stigmergy.index.backends.embedder import embedder_for_model
from stigmergy.index.errors import EmptyIndexError
from stigmergy.kernel.normalize import resolution_key
from stigmergy.server import entity_aliases, identity, review
from stigmergy.server.acl import visible
from stigmergy.server.audit import AuditWriter, ensure_audit_table
from stigmergy.server.errors import (
    CapabilityUnavailableError,
    IdentityError,
    RegistryError,
    StartupError,
)
from stigmergy.server.settings import Settings

# Re-exported for `stigmergy.slack`, which may import this package and not `stigmergy.capture`
# (`tests/test_architecture.py`); `stigmergy.capture` stays the owner of both.
SLACK_DOOR = capture_schema.SLACK_DOOR
# The refusal `resolve_submit_audience` raises. The Slack door asks that question itself, before
# it reserves its dedup row, so it needs the type to catch.
SubmitRefused = CaptureError

# What a caller who may not remove pages is told, whatever they named. ONE sentence for "you are
# not allowed" and "there is no such page", so this door is no existence oracle: a scoped identity
# probing for a page it cannot read learns nothing from the difference.
NOT_YOURS_TO_REMOVE = "there is nothing for you to remove at those paths"

# What a caller is told when they ask to file at an audience they do not hold. The door's whole
# authorization rule is `acl.visible()` applied to the WRITER (ADR 045 D2): you may file only what
# you could read afterwards, because a page nobody who wrote it can see is a page nobody can fix.
# It names the groups the CALLER holds, never the ones they asked for — the request is theirs
# already, and echoing a group set back would confirm which groups exist.
NOT_YOURS_TO_FILE_AT = (
    "you cannot file a capture at an audience you could not read afterwards. Your groups are "
    "{holds} — submit with an `audience` you hold, or omit it to file open")

log = logging.getLogger(__name__)

# Ranked hits pulled from the index BEFORE ACL filtering and truncation: an out-of-scope page must
# never steal a ranked slot from a visible one. 2×CANDIDATE_POOL covers the whole fused set.
_CANDIDATE_HITS = 2 * rank.CANDIDATE_POOL
DEFAULT_MAX_RESULTS = 5
PAGE_EXCERPT = 6000

# How many links/backlinks/timeline members are ever SHOWN — never a silent cap: the truncation,
# or its absence, is always stated in an explicit note field.
NAV_CAP = 20

# The ceiling on any single user-controlled string argument, checked BEFORE the DB read, the
# embedder call or the LLM call it would trigger, and before it can bloat an `audit_log` row.
MAX_ARG_CHARS = 8192

# Bounds on entity-first search's alias expansion (`_expansion_terms`): how many of a registry
# record's other names may join the lexical arm's OR-query, and how long one may be. Sized far
# above any real record — a company has a handful of spellings, not dozens — and far below what
# an edited registry could otherwise spend per request.
MAX_EXPANSION_TERMS = 12
MAX_EXPANSION_TERM_CHARS = 100

DEFAULT_SUBMISSION_LIMIT = 20

# What a malformed registry's message names when the bytes came from the index rather than from a
# file — an operator reading it needs to know WHICH copy to fix, and there is no path to give.
_SNAPSHOT_ORIGIN = f"snapshot in the index ({entity_aliases.ENTITY_REGISTRY_RELPATH})"

# Bounds on the audit row's SHAPE, where `MAX_ARG_CHARS` bounds each individual string.
MAX_AUDIT_HINT_KEYS = 32
MAX_AUDIT_DEPTH = 20

# How many registry matches `brain_submit` echoes beside its acknowledgement, and the shortest
# spelling it will look for: a two-letter alias matches prose by accident.
MAX_SUBMIT_MATCHES = 12
MIN_MATCH_CHARS = 3


def check_arg_length(name: str, value: str) -> None:
    """Fail-closed length guard. Raises a PLAIN `ValueError` (the audited `error_class` pins that
    type) carrying `is_arg_length_error`, the marker by which `mcp_server.py` echoes only this
    known-safe message — a `pydantic_core.ValidationError` is also a `ValueError` and can carry
    untrusted LLM output."""
    if len(value) > MAX_ARG_CHARS:
        ex = ValueError(f"{name} too long (max {MAX_ARG_CHARS} characters)")
        ex.is_arg_length_error = True
        raise ex


def _truncate_for_audit(args: dict, depth: int = 0) -> dict:
    """Bound every string (and dict KEY) in `args` to `MAX_ARG_CHARS` before it reaches
    `audit_log`. Depth-bounded: a `RecursionError` here fires inside `_call`'s `finally` and would
    replace the caller's real result or exception."""
    if depth > MAX_AUDIT_DEPTH:
        return "...[nested too deep]"
    if isinstance(args, str):
        if len(args) > MAX_ARG_CHARS:
            return args[:MAX_ARG_CHARS] + f"...[truncated {len(args) - MAX_ARG_CHARS} chars]"
        return args
    if isinstance(args, dict):
        return {_truncate_for_audit(k, depth + 1): _truncate_for_audit(v, depth + 1)
                for k, v in args.items()}
    if isinstance(args, list):
        return [_truncate_for_audit(v, depth + 1) for v in args]
    return args


def _audit_args(args: dict) -> dict:
    """`_truncate_for_audit` behind a fail-safe: it runs in `_call`'s `finally`, outside
    `AuditWriter.write`'s own try, so a raise here would clobber the caller's own result."""
    try:
        return _truncate_for_audit(args)
    except Exception:  # noqa: BLE001 — audit shaping must never surface through the served call
        log.error("audit arg shaping failed; writing the row without arguments", exc_info=True)
        return {"args_unavailable": "audit shaping failed"}


def _result_for(summarize, value, outcome: str) -> dict | None:
    """`audit_log.result`: None unless `summarize` was supplied AND the call succeeded."""
    if summarize is None or outcome != "ok":
        return None
    try:
        return summarize(value)
    except Exception:  # noqa: BLE001 — summarizing must never surface through the served call
        log.error("audit result summarizing failed; writing the row without a result",
                  exc_info=True)
        return None


# The UNTRUSTED-DATA fence is built in `stigmergy.text` ONLY; re-exported here as its one home.
neutralize_fence = textutil.neutralize_fence
fence = textutil.fence


def _neutralize_entity_record(record: dict) -> dict:
    """A registry record with every authored string neutralized; `id` is a KEY. `approved_by`
    rides along: it names the person whose capture introduced the identity (ADR 044)."""
    return {
        "id": record["id"],
        "name": neutralize_fence(record.get("name", "")),
        "type": neutralize_fence(record.get("type", "")),
        "aliases": [neutralize_fence(a) for a in record.get("aliases") or []],
        "approved_by": neutralize_fence(record.get("approved_by", "") or ""),
    }


def _display_title(title: str) -> str:
    """Untrusted title -> a safe display string; never empty, never the raw path."""
    return neutralize_fence(title) if title else "(untitled)"


class BrainService:
    def __init__(self, settings: Settings, conn, embedder, audiences: set[str] | None, *,
                 identity: str | None = None, rate_limiter=None, audit=None, evidence=None,
                 door: str = ""):
        self.settings = settings
        self.conn = conn
        self.embedder = embedder
        # the client's ACL scope: every read path filters through it (None = unrestricted)
        self.audiences = audiences
        self.identity = identity
        self.rate_limiter = rate_limiter
        self.audit = audit
        # which capture DOOR built this service ("" for every client-facing one): `_submit` reads
        # it to tell the Slack transport's own provenance hints from a client's forgery of them.
        self.door = door
        # None = no write path wired; `submit` refuses cleanly so reads still serve.
        self.evidence = evidence
        # `_registry_source`'s memo, dropped at every `_call`/`call_async` — see its docstring.
        self._registry_memo: tuple[str | None, str] | None = None

    @property
    def unrestricted(self) -> bool:
        """Does this client's scope widen a queue read to every identity's rows? The spelling of
        `audiences is None` lives here so the widening decision reads the same in every caller.
        NOT an ACL decision: page visibility is `acl.visible()`'s alone, never this."""
        return self.audiences is None

    # ── the service-layer wrapper (rate limit + audit), shared by every entry point ────────────
    def _call(self, tool: str, args: dict, fn, *, summarize=None):
        """`summarize`: an optional `(return_value) -> dict | None` callback, invoked only on a
        SUCCESSFUL call, whose result is written to `audit_log.result` — a summary, never a
        transcript."""
        self._registry_memo = None      # see `_registry_source`: the memo lives for this call
        start = time.monotonic()
        outcome, error_class, value = "ok", "", None
        try:
            if self.rate_limiter is not None:
                self.rate_limiter.check(self.identity, tool)
            value = fn()
            return value
        except Exception as ex:
            outcome, error_class = "error", ex.__class__.__name__
            raise
        finally:
            self._write_audit_row(tool=tool, args=args, start=start, outcome=outcome,
                                  error_class=error_class, value=value, summarize=summarize)

    async def call_async(self, tool: str, args: dict, coro_fn, *, summarize=None):
        """The same wrapper, async. Public because `ask` lives one layer ABOVE this service and
        `service.py` may never import `stigmergy.answer`."""
        self._registry_memo = None      # see `_registry_source`: the memo lives for this call
        start = time.monotonic()
        outcome, error_class, value = "ok", "", None
        try:
            if self.rate_limiter is not None:
                self.rate_limiter.check(self.identity, tool)
            value = await coro_fn()
            return value
        except Exception as ex:
            outcome, error_class = "error", ex.__class__.__name__
            raise
        finally:
            self._write_audit_row(tool=tool, args=args, start=start, outcome=outcome,
                                  error_class=error_class, value=value, summarize=summarize)

    def _write_audit_row(self, *, tool: str, args: dict, start: float, outcome: str,
                         error_class: str, value, summarize) -> None:
        """The audit tail both seams share — one row per call, whatever the outcome. It runs from a
        `finally` and is deliberately NOT wrapped in a try of its own: `_audit_args` and
        `_result_for` already absorb their own failures, and a raise from anywhere else here would
        replace the caller's real result or exception."""
        if self.audit is not None:
            self.audit.write(identity=self.identity, tool=tool, args=_audit_args(args),
                             duration_ms=(time.monotonic() - start) * 1000,
                             outcome=outcome, error_class=error_class,
                             result=_result_for(summarize, value, outcome))

    # ── capability guard ─────────────────────────────────────────────────────
    def require_embedder(self) -> None:
        """Refuse a call needing a query embedder this process does not have; the write tools and
        `read_page` must not consult it. `isinstance`, not an attribute probe: the arg-length
        suite drives a double that raises on ANY attribute access."""
        if isinstance(self.embedder, UnavailableEmbedder):
            raise CapabilityUnavailableError(self.embedder.unavailable_reason)

    # ── search ───────────────────────────────────────────────────────────────
    def search(self, query: str, filters: dict | None = None,
               max_results: int = DEFAULT_MAX_RESULTS, include_superseded: bool = True) -> dict:
        """Contract-ranked hits scoped to the client. Unknown filter names raise ValueError.
        Superseded pages are demoted but reachable; `include_superseded=False` drops them."""
        return self._call(
            "search_brain",
            {"query": query, "filters": filters, "max_results": max_results,
             "include_superseded": include_superseded},
            lambda: self._search(query, filters, max_results, include_superseded),
            summarize=lambda r: {"hits": r["count"]})

    def _search(self, query: str, filters: dict | None, max_results: int,
                include_superseded: bool) -> dict:
        check_arg_length("query", query)   # before the DB read AND the embedder call
        # An unrecognized filter key is echoed VERBATIM into the unknown-filter error, so keys are
        # bounded too — under a FIXED name, so a huge key is never embedded into an error first.
        for _k, _v in (filters or {}).items():
            check_arg_length("filters key", str(_k))
            check_arg_length(f"filters.{_k}", str(_v))
        self.require_embedder()
        # clamp first: max_results=-1 would drop the last hit AND report count=-1.
        max_results = max(1, min(int(max_results), _CANDIDATE_HITS))

        # Fires only when the caller passed NO explicit `entity` filter (key PRESENCE, not
        # truthiness), and only LAYERS on the ranking: a mis-resolution costs rank positions,
        # never a page's membership in the result set.
        entity_id = None
        if not (filters and "entity" in filters):
            aliases = self._registry_aliases()
            entity_id = entity_aliases.resolve_entity(aliases, query)
        return self._run_search(query, filters, max_results, include_superseded,
                                entity_hint=entity_id)

    def _registry_source(self) -> tuple[str | None, str]:
        """The registry TEXT this service answers from, and the origin naming it in the parser's
        operator-facing error. The index's snapshot WINS wherever the database has one; the
        `--entity-registry` file is the fallback.

        That order is the whole of issue #74. The deployed `app` and `slack` groups hold no
        checkout — their registry is a copy baked into the image at deploy time — so an entity
        minted after a rollout was served with no name, no type and no aliases, and its aliases
        resolved nowhere, until the next deploy. The snapshot is refreshed by the same push webhook
        that refreshes `pages_index` and reconciled by the same nightly `--rebuild`, so both groups
        see a mint within seconds of the push. The file road stays for a database with no snapshot:
        a local `stigmergy-server --repo`, or an index built before that table existed.

        Memoized between `_call`/`call_async` RESETS, which is not the same as once per tool call
        and must not be read as it: `ask` rides `call_async` and its inner `_call`s (every
        `service.search` it runs) drop the memo again, so one `ask` can resolve against a registry
        the webhook replaced mid-answer. That is a rank perturbation at worst — the alternative,
        pinning the registry for a whole `ask`, would trade it for a memo whose lifetime nothing
        bounds. What the memo IS for: one `describe_entity` reading the source once instead of
        three times. The reset is what keeps that true on the stdio door too, where `build_service`
        builds ONE service for the whole process (HTTP and Slack build one per request, so there
        the memo would have expired anyway).
        """
        if self._registry_memo is None:
            snapshot = store.read_ops_file(self.conn, store.ENTITY_REGISTRY_RELPATH)
            path = self.settings.entity_registry_path
            self._registry_memo = ((snapshot, _SNAPSHOT_ORIGIN) if snapshot is not None
                                   else (entity_aliases.read_file(path), path or ""))
        return self._registry_memo

    def _registry_aliases(self) -> dict[str, str]:
        """`aliases_from_text` over `_registry_source`, malformed content surfaced as
        `RegistryError`: the parser's `ValueError` names the registry PATH, and `search_brain`
        echoes `ValueError` verbatim."""
        # The raw parser is reachable only from closures that collapse ValueError to a class name;
        # adding ValueError to any of those echo sets would leak the registry path.
        try:
            return entity_aliases.aliases_from_text(*self._registry_source())
        except ValueError as ex:
            raise RegistryError("the entity registry could not be read") from ex

    def _registry_records(self) -> dict[str, dict]:
        """`registry_from_text`, same source and same posture as `_registry_aliases` above."""
        try:
            return entity_aliases.registry_from_text(*self._registry_source())
        except ValueError as ex:
            raise RegistryError("the entity registry could not be read") from ex

    def _expansion_terms(self, entity_id: str | None) -> tuple[str, ...]:
        """The registry's OTHER names for an entity, as extra OR-lexemes for the lexical arm only
        — the vector arm embeds the raw query untouched.

        BOUNDED, by count and by term length, because a registry is content somebody edits: an
        entity with ten thousand aliases is a legal file, and unbounded every one of them became
        an OR-lexeme in the tsquery of every search that resolved the entity — a per-request cost
        an editor can set for everyone (issue #79). The first N in registry order, so which
        aliases expand is stable and inspectable rather than dependent on anything here."""
        if not entity_id:
            return ()
        record = self._registry_records().get(entity_id)
        if not record:
            return ()
        terms = (t for t in (record.get("name") or "", *(record.get("aliases") or ()))
                 if t and len(t) <= MAX_EXPANSION_TERM_CHARS)
        return tuple(terms)[:MAX_EXPANSION_TERMS]

    def _run_search(self, query: str, filters: dict | None, max_results: int,
                    include_superseded: bool, entity_hint: str | None = None) -> dict:
        """The ONE search sequence: arms -> ACL filter -> shape -> truncate. The ACL filter runs
        BEFORE the `max_results` slice, so an invisible page never steals a ranked slot."""
        result = search.search_arms(self.conn, query, embedder=self.embedder, k=_CANDIDATE_HITS,
                                    filters=filters, include_superseded=include_superseded,
                                    today=date.today(), entity_hint=entity_hint,
                                    fts_expansion=self._expansion_terms(entity_hint))
        visible_hits = [h for h in result["hits"] if visible(h.get("acl"), self.audiences)]
        meta = store.read_meta(self.conn) or {}
        hits = [_shape_hit(h) for h in visible_hits[:max_results]]
        return {
            "query": query,
            "built_at": meta.get("built_at"),
            "embedding_model": meta.get("model"),
            "count": len(hits),
            "hits": hits,
        }

    # ── page read (the shared base + its two semantic renderings) ─────────────
    def may_read_page(self, path: str) -> bool:
        """Is this page in THIS client's audience? `acl.visible()`'s question, asked of one path
        and answered nowhere else — the deletion door hands this in as its `can_read` seam, because
        the diffs it returns are page bytes, and being allowed to delete a page is not being in the
        audience of every page that referred to it.

        A page the index does not carry answers False, the same fail-closed reading
        `fetch_page_raw` gives: existence itself is scoped, and a page removed by the very sweep
        being reported is one this cannot ask about any more.
        """
        page = search.fetch_pages(self.conn, [str(path)]).get(str(path))
        return page is not None and visible(page.get("acl"), self.audiences)

    def fetch_page_raw(self, path: str) -> dict | None:
        """One page's raw fields (sanitized, excerpted, UNFENCED), ACL-scoped — None when the path
        is nonexistent OR out of scope, since existence itself is scoped. The single fetch+ACL
        base shared by `read_page` and the answer layer's verifier."""
        page = search.fetch_pages(self.conn, [path]).get(path)
        if page is None or not visible(page.get("acl"), self.audiences):
            return None
        return {
            "path": page["path"],
            "title": page.get("title", ""),
            # `pages_index.entity` is a list — the default follows the column's own type.
            "entity": page.get("entity") or [],
            "as_of": page.get("as_of", ""),
            "type": page.get("type", ""),
            "status": page.get("status", ""),
            "supersedes": page.get("supersedes", ""),
            "superseded_by": page.get("superseded_by", ""),
            "links": page.get("links") or [],
            "body": textutil.sanitize(page.get("body") or "")[:PAGE_EXCERPT],
        }

    def read_page(self, path: str) -> dict:
        """One page, trust signals first, body fenced as UNTRUSTED-DATA. An out-of-scope or
        nonexistent path returns the SAME shape — existence itself must not leak.
        Rate-limited + audited — see `_call`."""
        return self._call("read_page", {"path": path}, lambda: self._read_page(path))

    def _read_page(self, path: str) -> dict:
        check_arg_length("path", path)   # before the DB read
        page = self.fetch_page_raw(path)
        if page is None:
            return {"error": f"unknown page: {path}"}
        outbound_links, outbound_note = self._nav_section(
            self._outbound_rows(page["links"]), subject="linked from this page",
            empty="This page links to no other pages.")
        inbound_links, inbound_note = self._nav_section(
            self._inbound_rows(path), subject="link to this page",
            empty="No pages link to this page.")
        return {
            **page,   # `links` (the raw resolved-path list) is overwritten below by the shaped one
            "banner": ("SUPERSEDED — a newer version exists; prefer it"
                       if page["superseded_by"] else None),
            "links": outbound_links, "links_note": outbound_note,
            "backlinks": inbound_links, "backlinks_note": inbound_note,
            "body": fence(page["body"]),
        }

    def _outbound_rows(self, paths: list[str]) -> list[tuple[str, str, list | None]]:
        """`(path, title, acl)` for a page's resolved outbound `links` — one batched fetch."""
        if not paths:
            return []
        rows = search.fetch_pages(self.conn, paths)
        return [(p, rows[p].get("title", ""), rows[p].get("acl")) for p in paths if p in rows]

    def _inbound_rows(self, path: str) -> list[tuple[str, str, list | None]]:
        """`(path, title, acl)` for every row linking TO `path` — the GIN containment lookup
        `pages_index_links_gin` exists for, never a scan."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT path, title, acl FROM pages_index"
                " WHERE links @> ARRAY[%s]::text[] AND path <> %s ORDER BY path",
                (path, path))
            return cur.fetchall()

    # ── the shared cap+note base (read_page's links/backlinks, describe_entity's timeline) ─────
    def _capped(self, rows: list[tuple]) -> tuple[list[tuple], int]:
        """ACL-scope `rows` (ACL is always the LAST tuple element) and cap to `NAV_CAP`, returning
        `(shown rows, total VISIBLE count)`. `visible()` runs BEFORE the cap, so an out-of-scope
        row never steals a shown slot nor hints at itself through a count discrepancy."""
        visible_rows = [r for r in rows if visible(r[-1], self.audiences)]
        return visible_rows[:NAV_CAP], len(visible_rows)

    @staticmethod
    def _cap_note(total: int, shown: int, *, subject: str, empty: str) -> str:
        """No silent cap: the truncation, or its absence, is always stated."""
        if total == 0:
            return empty
        if total > shown:
            return f"{total} page(s) {subject} — showing the first {shown}, {total - shown} more not shown."
        return f"{total} page(s) {subject} — showing all {total}."

    def _nav_section(self, rows: list[tuple[str, str, list | None]], *,
                     subject: str, empty: str) -> tuple[list[dict], str]:
        """`(path, title, acl)` triples -> capped `{path, title}` entries + the truncation note."""
        shown, total = self._capped(rows)
        items = [{"path": p, "title": _display_title(t)} for p, t, _acl in shown]
        return items, self._cap_note(total, len(items), subject=subject, empty=empty)

    # ── entity navigation ──────────────────────────────────────────────────────
    def list_entities(self) -> dict:
        """The ACL-scoped entity vocabulary: `scoped_entities()`'s ids, enriched from the registry;
        an id absent from it (or a missing registry) serves as `{id}` alone, a malformed registry
        raises `RegistryError`."""
        return self._call("list_entities", {}, lambda: self._list_entities())

    def _list_entities(self) -> dict:
        registry = self._registry_records()
        entities = []
        for eid in self.scoped_entities():
            record = registry.get(eid)
            entities.append(_neutralize_entity_record(record) if record else {"id": eid})
        return {"count": len(entities), "entities": entities}

    def describe_entity(self, entity: str) -> dict:
        """Everything anchored to one entity: registry metadata, its own page, its view reference
        and a dated-first timeline. An unknown entity and an out-of-scope one return the
        byte-identical absence shape — entity existence itself is scoped."""
        return self._call("describe_entity", {"entity": entity},
                          lambda: self._describe_entity(entity))

    def _describe_entity(self, entity: str) -> dict:
        check_arg_length("entity", entity)   # before any DB read
        # Computed UNCONDITIONALLY, before resolution: paying for the DB read in only one branch
        # is a timing oracle — latency alone would tell a caller which case applied.
        scoped = set(self.scoped_entities())
        aliases = self._registry_aliases()
        # Registry match first, falling back to EXACT membership of `scoped`. The registry hit
        # only WINS when in scope: a bare `or` would refuse a scoped raw id the registry folds to
        # an out-of-scope canonical id.
        resolved = entity_aliases.resolve_exact(aliases, entity)
        entity_id = resolved if resolved in scoped else (entity if entity in scoped else None)
        absence = {"error": f"unknown entity: {entity}"}
        if entity_id is None or entity_id not in scoped:
            return absence

        registry = self._registry_records()
        record = _neutralize_entity_record(
            registry.get(entity_id) or {"id": entity_id, "name": "", "type": "", "aliases": []})

        own_page = self._entity_own_page_row(entity_id)   # unscoped lookup — see its own docstring
        page_ref = None
        if own_page is not None and visible(own_page[2], self.audiences):
            page_ref = {"path": own_page[0], "title": _display_title(own_page[1])}

        # views/<id>.md is `views.regenerate.view_relpath`'s contract, recomputed because
        # `stigmergy.server` has no other reach into `stigmergy.views`.
        view_path = f"views/{entity_id}.md"
        view_row = search.fetch_pages(self.conn, [view_path]).get(view_path)
        view_ref = None
        if view_row is not None and visible(view_row.get("acl"), self.audiences):
            view_ref = {
                "path": view_path,
                "title": _display_title(view_row.get("title") or ""),
                "generated_at": neutralize_fence(view_row.get("generated_at", "")),
            }

        excluded = [p for p in (own_page[0] if own_page else None, view_path) if p]
        timeline_items, timeline_note = self._timeline_section(
            self._entity_timeline_rows(entity_id, excluded))

        return {
            "entity": {"id": record["id"], "name": record["name"], "type": record["type"],
                      "aliases": record["aliases"],
                      "approved_by": record["approved_by"], "page": page_ref},
            "view": view_ref,
            "timeline": timeline_items, "timeline_note": timeline_note,
        }

    def _entity_own_page_row(self, entity_id: str) -> tuple[str, str, list | None] | None:
        """The entity's OWN self-anchored page, UNSCOPED deliberately: callers need the path even
        when invisible, to exclude it from the timeline structurally."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT path, title, acl FROM pages_index"
                        " WHERE type = 'entity' AND %s = ANY(entity) ORDER BY path LIMIT 1",
                        (entity_id,))
            return cur.fetchone()

    def _entity_timeline_rows(self, entity_id: str,
                              excluded_paths: list[str]) -> list[tuple]:
        """Pages anchored to `entity_id` minus `excluded_paths` — dated first by `as_of` desc,
        undated after by path (`as_of = ''` sorts false-before-true)."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT path, title, type, status, as_of, acl FROM pages_index"
                " WHERE %s = ANY(entity) AND path <> ALL(%s)"
                " ORDER BY (as_of = ''), as_of DESC, path ASC",
                (entity_id, excluded_paths))
            return cur.fetchall()

    def _timeline_section(self, rows: list[tuple]) -> tuple[list[dict], str]:
        """Rows -> capped timeline entries + the truncation note, on `_nav_section`'s base."""
        shown, total = self._capped(rows)
        items = [{"path": p, "title": _display_title(t), "type": neutralize_fence(ty),
                 "status": neutralize_fence(st), "as_of": neutralize_fence(ao)}
                for p, t, ty, st, ao, _acl in shown]
        return items, self._cap_note(total, len(items), subject="anchored to this entity",
                                     empty="No anchored pages.")

    # ── the write path (the fast lane's front half) ──────────────────────────
    def submit(self, kind: str, material: str, hints: dict | None = None,
               audience: list[str] | None = None,
               submitted_by: str | None = None, verification: str | None = None,
               acl=None, content_hash: str | None = None) -> dict:
        """Queue a capture, attributed to THIS service's resolved identity.

        `audience` is the one thing about a capture a CALLER decides (ADR 045 D2): the groups this
        material is for, omitted to file it open. It is a REQUEST — the door resolves it, checks it
        against the caller's own groups and stores the answer on the row — which is why `acl`, the
        resolved label, stays in the four fields below.

        Those four are server-computed and declared here ONLY so they can be REFUSED: FastMCP's
        argument model uses `extra="ignore"`, so an undeclared field is dropped silently.
        """
        # Built ONCE and threaded through both consumers (audit args and the refusal).
        server_owned = {"submitted_by": submitted_by, "verification": verification,
                        "acl": acl, "content_hash": content_hash}
        return self._call("brain_submit",
                          _submit_audit_args(kind, material, hints, server_owned,
                                             audience=audience),
                          lambda: self._submit(kind, material, hints, server_owned,
                                               audience=audience))

    def check_submit_audience(self, audience) -> list[str] | None:
        """`resolve_submit_audience` through the audited seam — what a DOOR calls when it needs
        the answer before it commits to anything else.

        The Slack door asks before it reserves its dedup row, so a refusal reads as a refusal
        rather than as a capture that failed. Routed through `_call` because `_submit_audit_args`
        makes an argument of it: "who asked to restrict what, and when" has to be answerable from
        the audit trail rather than from a row a refusal never wrote — and a refusal is exactly
        the case where no row exists to answer it.
        """
        return self._call("brain_submit_audience", {"audience": _audit_audience(audience)},
                          lambda: self.resolve_submit_audience(audience))

    def resolve_submit_audience(self, audience) -> list[str] | None:
        """The DOOR's audience decision for one capture: the label the row carries, or `None` for
        open. Raises `CaptureError` when the caller may not file there.

        Two checks, and the second is the whole of the authorization. The names must be a legal
        group list (`identity.check_group_names` — the same rule the roster and the channel map are
        held to, so a group cannot be spellable at the door and refused in the file). Then
        `acl.visible()` — the ONE read predicate — is asked of the WRITER: you may file only what
        you could read afterwards. An unrestricted caller passes it by construction; a caller with
        no groups may file open and nothing else.

        Public because the Slack door asks the same question BEFORE it reserves its dedup row, so
        a refusal there reads as a refusal rather than as a capture that failed.
        """
        if audience is None:
            return None
        if isinstance(audience, list) and not audience:
            # NOT silently "open". `[]` is the corpus's spelling for NOBODY, so a caller sending it
            # may mean the exact opposite of what the old short-circuit did — and a request whose
            # two readings are "everyone" and "no one" is not one to guess at. Omitting the
            # argument is the unambiguous way to say open.
            raise CaptureError(
                "an empty `audience` is not a request: `[]` means NOBODY where audiences are "
                "read, and omitting `audience` is how you file a capture open. Send the groups "
                "this material is for, or leave the argument out")
        if isinstance(audience, str):
            raise CaptureError(
                f"audience must be a list of group names, not a single string — send "
                f'["{audience}"]')
        try:
            labels = list(identity.check_group_names(
                audience, origin="this submission", subject="audience",
                remedy=identity.DOOR_REMEDY))
        except IdentityError as ex:
            raise CaptureError(str(ex)) from ex
        if not labels:
            return None
        # SORTED, so one audience has one spelling wherever it is compared: `capture_queue.acl` is
        # a `text[]` and dedup's `IS NOT DISTINCT FROM` is element-wise ordered, so two callers
        # naming the same groups in different orders would otherwise defeat both dedup levels and
        # file the same material twice.
        labels = sorted(labels)
        if not visible(labels, self.audiences):
            raise CaptureError(NOT_YOURS_TO_FILE_AT.format(
                holds=", ".join(sorted(self.audiences)) if self.audiences else "none"))
        return labels

    def _submit(self, kind: str, material: str, hints: dict | None,
                server_owned: dict, *, audience=None) -> dict:
        capture_schema.reject_server_owned_arguments(server_owned)
        # The two source hints the fast lane trusts — refused for every door but Slack's own.
        capture_schema.reject_source_provenance_hints(hints, door=self.door)
        # `kind` is MODEL-CHOSEN and `prepare_submission` refuses one outside `KINDS` by name.
        # This door takes the SUBMITTABLE ones (ADR 044 D4) — the queue's vocabulary is wider by
        # exactly one, and `delete` is queued by the door that authorized it, never asked for here.
        capture_schema.reject_unsubmittable_kind(kind)
        if self.evidence is None:
            raise CaptureError("the capture queue is not available on this server")
        if not self.identity:
            # Fail-closed: the governance model rests on a named person standing behind a page.
            raise CaptureError("no resolved identity — a capture cannot be submitted unattributed")
        acl = self.resolve_submit_audience(audience)
        ack = queue.submit(self.conn, self.evidence, kind=kind, material=material, hints=hints,
                           submitted_by=self.identity, acl=acl)
        # Echoed at once, before the librarian runs: which registered entities this material
        # names, so the submitter sees on the spot that the brain recognises them — and, when it
        # names none, that the librarian will PROPOSE what it finds. Scoped like `list_entities`:
        # a match is an existence claim about an entity.
        entities = self._registry_matches(material)
        return {**ack, "entities": entities, "message": _ack_message(ack, entities)}

    def _registry_matches(self, material: str) -> list[dict]:
        """The registered entities whose name or an alias the material spells, word-bounded and
        accent/case-folded — the same folding `resolve_entity` uses. A hint, never a resolution:
        the librarian judges, this only tells the submitter what the registry already knows."""
        haystack = f" {resolution_key(material)} "
        scoped = set(self.scoped_entities())
        out = []
        for cid, record in self._registry_records().items():
            if cid not in scoped:
                continue
            for spelling in (record.get("name", ""), *record.get("aliases", ())):
                needle = resolution_key(spelling)
                if len(needle) >= MIN_MATCH_CHARS and f" {needle} " in haystack:
                    out.append({"id": cid, "name": neutralize_fence(record.get("name", ""))})
                    break
            if len(out) >= MAX_SUBMIT_MATCHES:
                break
        return out

    def submissions(self, limit: int = DEFAULT_SUBMISSION_LIMIT, status: str | None = None) -> dict:
        """The caller's own submissions — or the whole queue for an unrestricted identity. The
        scope decision is made HERE; this tool has no `submitted_by` filter at all."""
        return self._call("brain_submissions", {"limit": limit, "status": status},
                          lambda: self._submissions(limit, status))

    def _submissions(self, limit: int, status: str | None) -> dict:
        statuses = [status] if status else None
        unrestricted = self.unrestricted
        rows = (queue.list_all_submissions(self.conn, statuses=statuses, limit=limit)
                if unrestricted
                else queue.list_own_submissions(self.conn, self.identity, statuses=statuses,
                                                limit=limit))
        return {
            "identity": self.identity,
            "scope": "all" if unrestricted else "own",
            "count": len(rows),
            "submissions": [self._shape_submission(row) for row in rows],
        }

    def _shape_submission(self, row: dict) -> dict:
        """One listed submission: the excerpt goes through the page-body fence, every other
        free-text field is neutralized so it cannot close that fence in-band. Suppression of
        withheld material is decided in `capture.queue`'s query, so this method receives empty
        fields plus the server's own `withheld_reason` — unfenced, being the server's own text."""
        excerpt, note = row["excerpt"], row["error"]
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "submitted_by": row["submitted_by"],
            "mine": row["submitted_by"] == self.identity,
            "attempts": row["attempts"],
            "created_at": row["created_at"],
            "claimed_at": row["claimed_at"],
            "finished_at": row["finished_at"],
            "result_ref": row["result_ref"],
            "events": _neutralize_report(row["events"]),
            "error": neutralize_fence(note),
            "blob_refs": row["blob_refs"],
            "content_sha256": row["content_sha256"],
            "bytes": row["bytes"],
            "payload_purged": row["payload_purged"],
            "withheld_reason": row["withheld_reason"],
            "hints": {k: neutralize_fence(v) for k, v in (row["hints"] or {}).items()},
            "flagged_hints": row["flagged_hints"],
            "excerpt": fence(excerpt) if excerpt else "",
            "report": self._shape_report(row["report"]),
        }

    def _shape_report(self, raw) -> dict:
        """The librarian's own report, ready to go over the wire.

        One kind of row needs more than the shared neutralizing: a performed REMOVAL carries the
        unified diff of every page it rewrote, and those are page BYTES. So they obey the two rules
        every other surface that echoes a page obeys — `acl.visible()` decides who may read one,
        asked per path, and what survives is FENCED, because it carries both a page's own bytes and
        fresh model output and neither is an instruction to whoever reads this. A page whose diff is
        withheld is NAMED rather than dropped: it changed, the commit says so, and a reader who
        cannot see why must not be left thinking nothing happened to it.
        """
        shaped = _without_operator_telemetry(_neutralize_report(raw))
        rewritten = shaped.get("rewritten")
        if not isinstance(rewritten, dict):
            return shaped
        readable = {path: fence(text) for path, text in rewritten.items()
                    if self.may_read_page(path)}
        return {**shaped, "rewritten": readable,
                "withheld": sorted(set(rewritten) - set(readable))}
    # ── the write lane's governed door ─────────────────────────────────────────
    # The queueing lives in `server.review`, which is the ONE seam both removal doors cross (this
    # one and the console's) — so which door a person removed from changes the row's
    # `delete_source` and nothing else. Nothing here reaches the knowledge repo: since ADR 044 D3
    # the worker is the only writer, and this process holds neither the checkout nor the credential.

    def delete_pages(self, paths, why: str = "", *, source: str) -> dict:
        """QUEUE a removal: the pages that go and the reason, as a `delete` row this service's
        resolved identity is on. The worker performs it (ADR 044 D3) — this process holds no git
        credential and writes nothing to the corpus.

        **Authorization is the one question this door can answer, and it answers it here.** A
        removal touches the pages it names AND every page that refers to them, a set nothing knows
        before the tree is read — so the only honest question at the door is "may this caller see
        the whole corpus": an identity with no audience restriction can, a scoped one cannot,
        whatever those paths turn out to be. The refusal is the lane's anonymous sentence, so it is
        no existence oracle either.

        `source` is REQUIRED and never defaulted: the row names the DOOR, and a default would
        attribute one door's act to another the day a third arrives.

        The audit row keeps the shape and never the reason: `why` is free text a person wrote. Both
        free-text arguments are length-checked INSIDE the `_call` seam, so an over-long one is
        refused before anything is queued AND recorded as the caller behaviour it is — a check run
        in the tool closure instead would be invisible to `audit_log`.
        """
        def _checked():
            check_arg_length("why", why or "")
            for path in paths or ():
                check_arg_length("path", str(path))
            return self._queue_delete(paths, why, source=source)

        return self._call(
            "brain_delete",
            {"paths": [str(p) for p in (paths or ())], "why_chars": len(why or ""),
             "source": source},
            _checked)

    def _queue_delete(self, paths, why: str, *, source: str) -> dict:
        """The authorization, then the shared queueing seam. The refusal is the ONE sentence for
        both "you may not" and "there is no such page", so a scoped identity probing for a page it
        cannot read learns nothing from the difference."""
        if not self.identity:
            raise CaptureError("no resolved identity — a removal cannot be queued unattributed")
        if not self.unrestricted:
            raise CaptureError(NOT_YOURS_TO_REMOVE)
        return review.queue_deletion(self.conn, self.evidence, paths=paths, why=why,
                                     actor=self.identity, source=source)

    # ── scoped read helpers (reused by the answer layer) ──────────────────────
    def scoped_entities(self) -> list[str]:
        """Distinct entities on pages THIS client may see — the ONE place that SQL lives."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT DISTINCT unnest(entity), acl FROM pages_index"
                        " WHERE entity <> '{}'")
            return sorted({e for e, acl in cur.fetchall() if visible(acl, self.audiences)})


def _submit_audit_args(kind: str, material: str, hints: dict | None,
                       server_owned: dict, *, audience=None) -> dict:
    """What `audit_log` records about a submit: sizes, hint KEYS and the sha256 the evidence key
    is built from — never the captured text, and never a server-owned argument's VALUE.

    `audience` IS recorded by value, unlike the server-owned four: it is the caller's own request
    about who may read what they filed, so "who asked to restrict what, and when" has to be
    answerable from the audit trail rather than from the row a refusal never wrote."""
    digest, size = capture_schema.material_digest(material if isinstance(material, str) else "")
    return {
        "kind": kind,
        "material_bytes": size,
        "material_sha256": digest if size else "",
        "hint_keys": _audit_hint_keys(hints),
        "audience": _audit_audience(audience),
        "server_owned_args_present": sorted(k for k, v in server_owned.items() if v is not None),
    }


def _audit_audience(audience) -> list[str] | None:
    """The audience a caller asked for, as the audit records it: sorted, deduplicated, bounded.

    Recorded BY VALUE, unlike the server-owned four beside it, because it is the caller's own
    request about who may read what they filed. Bounded like every other audited argument — the
    row is written whatever the outcome, refusals included, so an unbounded list would land a
    large row on a call that was rejected anyway."""
    if not isinstance(audience, list):
        return None
    return sorted({str(a)[:MAX_ARG_CHARS] for a in audience[:MAX_AUDIT_HINT_KEYS]})


def _audit_hint_keys(hints) -> list[str]:
    """Hint key names, bounded by COUNT as well as length: the audit row is written whatever the
    outcome, so 100k keys would land a multi-MB row on a call rejected anyway."""
    keys = sorted(str(k) for k in (hints if isinstance(hints, dict) else {}))
    if len(keys) <= MAX_AUDIT_HINT_KEYS:
        return keys
    return keys[:MAX_AUDIT_HINT_KEYS] + [f"...[{len(keys) - MAX_AUDIT_HINT_KEYS} more keys]"]


def _ack_message(ack: dict, entities: list[dict] = ()) -> str:
    """Promises exactly what happened — queued and attributed — and says what the registry
    already recognises in the material, so the submitter knows what to expect: an anchor to a
    named entity, or a proposal the librarian will make."""
    line = (f"queued as submission #{ack['id']} and attributed to {ack['submitted_by']}. "
            "The librarian files it; nothing is in the brain until it does — "
            "check with brain_submissions.")
    if entities:
        names = ", ".join(e["name"] for e in entities)
        line += f" The registry already knows {names}; the librarian will anchor to what fits."
    else:
        line += (" The registry recognises no entity in this material; if it is about one, the "
                 "librarian introduces it, confirmed by you.")
    if ack.get("flagged_hints"):
        line += (f" Note: the material declares {', '.join(ack['flagged_hints'])} in its "
                 "frontmatter; recorded as a hint and ignored — those fields are the server's.")
    return line


def _neutralize_report(report, depth: int = 0):
    """The librarian's report, made safe to hand a reader: every free-text field is DERIVED from
    captured material. Neutralized rather than fenced — that is what stops a value closing the
    excerpt's fence in-band. Depth-bounded; beyond it the subtree is dropped.

    The ONE string-leaf walker in this package: a second copy had already drifted apart from this
    one at the depth bound before it was collapsed to this."""
    if depth > MAX_AUDIT_DEPTH:
        return None
    if isinstance(report, str):
        return neutralize_fence(report)
    if isinstance(report, dict):
        return {str(k): _neutralize_report(v, depth + 1) for k, v in report.items()}
    if isinstance(report, list | tuple):
        return [_neutralize_report(v, depth + 1) for v in report]
    return report


def _without_operator_telemetry(report):
    """`cost_usd` stays on the STORED report and leaves the client wire here: per-item dollar
    spend is operator telemetry. A COPY: the caller's report may be a row read back out of
    Postgres that another reader still holds."""
    return ({k: v for k, v in report.items() if k != "cost_usd"}
            if isinstance(report, dict) else report)


def _shape_hit(h: dict) -> dict:
    """A search hit as JSON; the body and the raw acl labels never ship."""
    return {
        "path": h["path"],
        "title": h.get("title", ""),
        "type": h.get("type", ""),
        "snippet": h.get("snippet", ""),
        "score": h["score"],
        "arms": h["arms"],
        "factors": h["factors"],
        "entity": h.get("entity") or [],   # a list, not a scalar
        "as_of": h.get("as_of", ""),
        "superseded": bool(h.get("superseded_by")),
        "superseded_by": h.get("superseded_by", ""),
    }


class UnavailableEmbedder:
    """The "embedder" a keyless process gets: it holds the reason it could not be built and
    RAISES rather than returning a vector — a degraded embedder would embed into a space the index
    was not built in and report success."""

    def __init__(self, reason: str):
        self.unavailable_reason = reason

    def embed(self, texts):
        raise CapabilityUnavailableError(self.unavailable_reason)


def missing_embedder_reason(detail: str) -> str:
    """The one sentence every embedder-needing tool refuses with. All three parts are load-bearing:
    which capability is missing, that the write path is unaffected, and `detail`."""
    return (f"unavailable: this server is running without a query embedder, so it cannot search. "
            f"{detail} Capture is unaffected — brain_submit, brain_submissions, read_page, "
            f"list_entities and describe_entity need no embedder and are working normally.")


def _resolve_embedder(settings: Settings, model: str):
    """The query embedder must embed in the same space the documents did, so it defaults to the
    model the index was built with. A missing OPENAI_API_KEY yields an `UnavailableEmbedder`; an
    unknown embedder NAME raises, so a typo never presents as a missing credential."""
    if settings.embedder not in (None, "", "fake", "openai"):
        raise StartupError(f"unknown embedder {settings.embedder!r} (use 'openai' or 'fake')")
    try:
        if settings.embedder == "fake":
            from stigmergy.index.backends.embedder import build_embedder
            return build_embedder("fake")
        if settings.embedder == "openai":
            from stigmergy.index.backends.embedder import build_embedder
            return build_embedder("openai", model)
        return embedder_for_model(model)          # match the built index (the usual path)
    except RuntimeError as ex:                     # e.g. OPENAI_API_KEY is not set
        log.warning("starting without a query embedder; search_brain and ask will refuse: %s", ex)
        return UnavailableEmbedder(missing_embedder_reason(str(ex)))


def open_scoped_resources(settings: Settings, conn=None):
    """`(conn, embedder)`, fail-closed on an empty index — the ONE place the connect / read
    `index_meta` / resolve-the-embedder sequence is written. Every transport builds through it."""
    conn = conn or store.connect(settings.dsn)
    meta = store.read_meta(conn)
    if meta is None:
        raise EmptyIndexError(
            "the index is empty — run `stigmergy-index --rebuild --repo <dir>` before serving")
    embedder = _resolve_embedder(settings, meta["model"])
    return conn, embedder


def build_service(settings: Settings, conn=None) -> BrainService:
    """Wire the stdio service, fail-closed. Identity resolves FIRST, so an identity failure never
    touches Postgres and never starts the server open. Deliberately NO `RateLimiter`: the budgets
    protect spend behind a PUBLIC url."""
    audiences_tuple = identity.resolve_audiences(settings.identities_path, settings.identity)
    conn, embedder = open_scoped_resources(settings, conn)
    audiences = set(audiences_tuple) if audiences_tuple is not None else None

    ensure_audit_table(conn)
    ensure_capture_schema(conn)
    # Created single-threaded here so the webhook's own `IF NOT EXISTS` has nothing left to race:
    # losing that race inside its phase-2 transaction would roll the pushed pages back with it.
    store.ensure_ops_file_table(conn)
    store.ensure_webhook_dedupe_table(conn)
    # `store_from_env` does no I/O, so a server whose bucket is unreachable still serves reads.
    return BrainService(settings, conn, embedder, audiences, identity=settings.identity,
                        audit=AuditWriter(conn), evidence=evidence_plane.store_from_env())
