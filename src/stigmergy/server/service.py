"""Transport-independent knowledge service."""
import datetime as dt
import hashlib
import logging
import time
import uuid
from datetime import date

from stigmergy import text as textutil
from stigmergy.capture import evidence as evidence_plane
from stigmergy.capture import queue
from stigmergy.capture import schema as capture_schema
from stigmergy.capture import uploads as upload_sessions
from stigmergy.capture.errors import CaptureError
from stigmergy.capture.schema import ensure_capture_schema
from stigmergy.capture.service import CaptureService
from stigmergy.changes.store import ensure_change_schema
from stigmergy.index import rank, search, store
from stigmergy.index.backends.embedder import embedder_for_model
from stigmergy.index.errors import EmptyIndexError
from stigmergy.server import entity_aliases, identity, ops_files
from stigmergy.server.acl import visible
from stigmergy.server.audit import AuditWriter, ensure_audit_table
from stigmergy.server.errors import (
    CapabilityUnavailableError,
    IdentityError,
    RegistryError,
    StartupError,
)
from stigmergy.server.settings import Settings

SLACK_DOOR = "slack"
SubmitRefused = CaptureError

# Identical refusal prevents page-existence disclosure.
NOT_YOURS_TO_REMOVE = "there is nothing for you to remove at those paths"

NOT_YOURS_TO_FILE_AT = (
    "you cannot file a capture at an audience you could not read afterwards. Your groups are "
    "{holds} — submit with an `audience` you hold, or omit it to file open")

log = logging.getLogger(__name__)

# ACL filtering happens before visible-result truncation.
_CANDIDATE_HITS = 2 * rank.CANDIDATE_POOL
DEFAULT_MAX_RESULTS = 5
PAGE_EXCERPT = 6000

NAV_CAP = 20

MAX_ARG_CHARS = 8192

MAX_EXPANSION_TERMS = 12
MAX_EXPANSION_TERM_CHARS = 100

DEFAULT_SUBMISSION_LIMIT = 20

_SNAPSHOT_ORIGIN = f"snapshot in the index ({entity_aliases.ENTITY_REGISTRY_RELPATH})"

MAX_AUDIT_HINT_KEYS = 32
MAX_AUDIT_DEPTH = 20

MAX_SUBMIT_MATCHES = 12
MIN_MATCH_CHARS = 3


def check_arg_length(name: str, value: str) -> None:
    """Reject oversized arguments with a safe transport marker."""
    if len(value) > MAX_ARG_CHARS:
        ex = ValueError(f"{name} too long (max {MAX_ARG_CHARS} characters)")
        ex.is_arg_length_error = True
        raise ex


def _truncate_for_audit(args: dict, depth: int = 0) -> dict:
    """Bound strings, keys, and nesting in audit arguments."""
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
    """Shape audit arguments without affecting the served call."""
    try:
        return _truncate_for_audit(args)
    except Exception as error:  # noqa: BLE001 — audit shaping must not fail the served call
        log.error(
            "audit arg shaping failed; writing the row without arguments (%s)",
            error.__class__.__name__,
        )
        return {"args_unavailable": "audit shaping failed"}


def _result_for(summarize, value, outcome: str) -> dict | None:
    """Return a success summary when configured."""
    if summarize is None or outcome != "ok":
        return None
    try:
        return summarize(value)
    except Exception as error:  # noqa: BLE001 — summarizing must not fail the served call
        log.error(
            "audit result summarizing failed; writing the row without a result (%s)",
            error.__class__.__name__,
        )
        return None


neutralize_fence = textutil.neutralize_fence
fence = textutil.fence


def _neutralize_entity_record(record: dict, *, include_claims: bool = False) -> dict:
    return {
        "id": record["id"],
        "name": neutralize_fence(record.get("name", "")),
        "type": neutralize_fence(record.get("type", "")),
        "aliases": [neutralize_fence(a) for a in record.get("aliases") or []],
        **(
            {
                "claims": [
                    {
                        "value": neutralize_fence(claim["value"]),
                        "kind": claim["kind"],
                        "acl": claim.get("acl"),
                        "source": claim["source"],
                        "introduced_at": claim["introduced_at"],
                    }
                    for claim in record.get("claims", ())
                ]
            }
            if include_claims
            else {}
        ),
    }


ENTITY_ABSENCE = {
    "found": False,
    "entity": None,
    "knowledge": [],
    "knowledge_note": "No visible entity was found.",
    "sources": [],
}


def _display_title(title: str) -> str:
    """Untrusted title -> a safe display string; never empty, never the raw path."""
    return neutralize_fence(title) if title else "(untitled)"


class BrainService:
    def __init__(self, settings: Settings, conn, embedder, audiences: set[str] | None, *,
                 identity: str | None = None, rate_limiter=None, audit=None, evidence=None,
                 door: str = "", principal: identity.Principal | None = None):
        self.settings = settings
        self.conn = conn
        self.embedder = embedder
        self.audiences = audiences
        self.identity = identity
        self.principal = principal
        self.rate_limiter = rate_limiter
        self.audit = audit
        self.door = door
        self.evidence = evidence
        self._registry_memo: tuple[str | None, str] | None = None

    @property
    def unrestricted(self) -> bool:
        """Return whether queue reads include every identity."""
        return self.audiences is None

    def _call(self, tool: str, args: dict, fn, *, summarize=None):
        """Run one rate-limited, audited tool call."""
        self._registry_memo = None
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
        """Run one asynchronous rate-limited, audited tool call."""
        self._registry_memo = None
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
        """Write one audit row for either a successful or failed call."""
        if self.audit is not None:
            self.audit.write(identity=self.identity, tool=tool, args=_audit_args(args),
                             duration_ms=(time.monotonic() - start) * 1000,
                             outcome=outcome, error_class=error_class,
                             result=_result_for(summarize, value, outcome))

    def require_embedder(self) -> None:
        """Refuse search calls when this process has no query embedder."""
        if isinstance(self.embedder, UnavailableEmbedder):
            raise CapabilityUnavailableError(self.embedder.unavailable_reason)

    def search(self, query: str, filters: dict | None = None,
               max_results: int = DEFAULT_MAX_RESULTS) -> dict:
        """Return ranked hits visible to the caller."""
        return self._call(
            "search_brain",
            {"query": query, "filters": filters, "max_results": max_results},
            lambda: self._search(query, filters, max_results),
            summarize=lambda r: {"hits": r["count"]})

    def _search(self, query: str, filters: dict | None, max_results: int) -> dict:
        check_arg_length("query", query)
        for _k, _v in (filters or {}).items():
            check_arg_length("filters key", str(_k))
            check_arg_length(f"filters.{_k}", str(_v))
        self.require_embedder()
        max_results = max(1, min(int(max_results), _CANDIDATE_HITS))

        entity_id = None
        if not (filters and "entity" in filters):
            aliases = self._registry_aliases()
            entity_id = entity_aliases.resolve_entity(aliases, query)
        return self._run_search(query, filters, max_results, entity_hint=entity_id)

    def _registry_source(self) -> tuple[str | None, str]:
        """Return the request-local registry snapshot, falling back to the configured file."""
        if self._registry_memo is None:
            snapshot = store.read_ops_file(self.conn, store.ENTITY_REGISTRY_RELPATH)
            path = self.settings.entity_registry_path
            self._registry_memo = ((snapshot, _SNAPSHOT_ORIGIN) if snapshot is not None
                                   else (entity_aliases.read_file(path), path or ""))
        return self._registry_memo

    def _registry_aliases(self) -> dict[str, str]:
        """Return visible aliases without exposing parser paths on failure."""
        try:
            return entity_aliases.aliases_from_text(
                *self._registry_source(), audiences=self.audiences
            )
        except ValueError as ex:
            raise RegistryError("the entity registry could not be read") from ex

    def _registry_records(self) -> dict[str, dict]:
        """Return registry records without exposing parser paths on failure."""
        try:
            return entity_aliases.registry_from_text(*self._registry_source())
        except ValueError as ex:
            raise RegistryError("the entity registry could not be read") from ex

    def _registry_redirects(self) -> dict[str, str]:
        try:
            return entity_aliases.redirects_from_text(*self._registry_source())
        except ValueError as ex:
            raise RegistryError("the entity registry could not be read") from ex

    def _expansion_terms(self, entity_id: str | None) -> tuple[str, ...]:
        """Return bounded visible aliases for lexical query expansion."""
        if not entity_id:
            return ()
        record = self._registry_records().get(entity_id)
        projection = entity_aliases.project_record(record, self.audiences) if record else None
        if not projection:
            return ()
        terms = (t for t in (projection.get("name") or "", *(projection.get("aliases") or ()))
                 if t and len(t) <= MAX_EXPANSION_TERM_CHARS)
        return tuple(terms)[:MAX_EXPANSION_TERMS]

    def _run_search(self, query: str, filters: dict | None, max_results: int,
                    entity_hint: str | None = None) -> dict:
        """Rank candidates, apply ACLs, then truncate visible results."""
        result = search.search_arms(self.conn, query, embedder=self.embedder, k=_CANDIDATE_HITS,
                                    filters=filters, today=date.today(), entity_hint=entity_hint,
                                    fts_expansion=self._expansion_terms(entity_hint),
                                    audiences=self.audiences)
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
            "type": page.get("type", ""),
            "status": page.get("status", ""),
            "updated": page.get("updated", ""),
            "links": page.get("links") or [],
            "sources": page.get("sources") or [],
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
            **page,
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
            projection = entity_aliases.project_record(record, self.audiences) if record else None
            if projection:
                entities.append(_neutralize_entity_record(projection))
        return {"count": len(entities), "entities": entities}

    def describe_entity(self, entity: str) -> dict:
        """Return the reader-scoped identity and its anchored knowledge."""
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
        redirected = self._registry_redirects().get(entity) if self.unrestricted else None
        if redirected in scoped:
            resolved = redirected
        entity_id = resolved if resolved in scoped else (entity if entity in scoped else None)
        if entity_id is None or entity_id not in scoped:
            return dict(ENTITY_ABSENCE)

        registry = self._registry_records()
        projection = entity_aliases.project_record(registry.get(entity_id) or {}, self.audiences)
        if projection is None:
            return dict(ENTITY_ABSENCE)
        record = _neutralize_entity_record(projection, include_claims=True)
        timeline_items, timeline_note, source_paths = self._timeline_section(
            self._entity_timeline_rows(entity_id))
        source_rows = search.fetch_pages(self.conn, source_paths)
        sources = [
            {"path": path, "title": _display_title(source_rows[path].get("title", ""))}
            for path in source_paths
            if path in source_rows and visible(source_rows[path].get("acl"), self.audiences)
        ]

        return {
            "found": True,
            "entity": record,
            "knowledge": timeline_items,
            "knowledge_note": timeline_note,
            "sources": sources,
        }

    def _entity_timeline_rows(self, entity_id: str) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT path, title, type, status, updated, sources, acl FROM pages_index"
                " WHERE %s = ANY(entity) AND type IN ('note', 'concept')"
                " ORDER BY (updated = ''), updated DESC, path ASC",
                (entity_id,))
            return cur.fetchall()

    def _timeline_section(self, rows: list[tuple]) -> tuple[list[dict], str, list[str]]:
        shown, total = self._capped(rows)
        items = [{"path": p, "title": _display_title(t), "type": neutralize_fence(ty),
                 "status": neutralize_fence(st), "updated": neutralize_fence(updated)}
                for p, t, ty, st, updated, _sources, _acl in shown]
        source_paths = list(dict.fromkeys(
            source
            for _p, _t, _ty, _st, _updated, sources, _acl in shown
            for source in sources
        ))
        return (
            items,
            self._cap_note(total, len(items), subject="anchored to this entity",
                           empty="No anchored pages."),
            source_paths,
        )

    def submit_artifacts(
        self,
        *,
        artifact_values: tuple[tuple[bytes, str | None, str | None, str | None], ...],
        idempotency_key: str,
        audience: list[str] | None,
        title: str | None = None,
        occurred_at: dt.datetime | dt.date | None = None,
        locator: str | None = None,
        participants: tuple[capture_schema.Participant, ...] = (),
        acquisition: capture_schema.AcquisitionProvenance | None = None,
    ) -> dict:
        def queue_capture():
            if self.evidence is None or not self.identity:
                raise CaptureError("capture is unavailable for this identity")
            principal = self.principal or identity.resolve_principal(
                self.settings.identities_path, self.identity
            )
            acl = self.resolve_submit_audience(audience, use_default=False)
            receipt = CaptureService(self.conn, self.evidence).capture_bytes(
                actor=capture_schema.Actor(
                    subject=principal.subject,
                    display_name=principal.display_name,
                ),
                audience=None if acl is None else tuple(acl),
                adapter=self.door or "mcp",
                artifact_values=artifact_values,
                idempotency_key=idempotency_key,
                title=title,
                occurred_at=occurred_at,
                locator=locator,
                participants=participants,
                acquisition=acquisition,
            )
            return {
                "id": receipt["id"],
                "status": receipt["status"],
                "submitted_by": receipt["submitted_by"],
                "created_at": receipt["created_at"],
                "created": receipt["created"],
            }

        return self._call(
            "brain_submit",
            {
                "artifacts": len(artifact_values),
                "bytes": sum(len(item[0]) for item in artifact_values),
                "audience": _audit_audience(audience),
            },
            queue_capture,
        )

    def create_upload(
        self,
        *,
        idempotency_key: str,
        sha256: str,
        bytes: int,
        media_type: str,
        original_name: str | None = None,
        source_url: str | None = None,
    ) -> dict:
        def create():
            if self.evidence is None or not self.identity:
                raise CaptureError("capture is unavailable for this identity")
            try:
                return upload_sessions.create_upload(
                    self.conn,
                    self.evidence,
                    actor=self.identity,
                    idempotency_key=idempotency_key,
                    sha256=sha256,
                    bytes=bytes,
                    media_type=media_type,
                    original_name=original_name,
                    source_url=source_url,
                )
            except ValueError as error:
                raise CaptureError("upload metadata is invalid") from error

        return self._call(
            "brain_upload_create",
            {"sha256": sha256, "bytes": bytes, "media_type": media_type},
            create,
        )

    def finalize_upload_capture(
        self,
        *,
        upload_ids: list[str],
        idempotency_key: str,
        title: str | None = None,
        occurred_at: str | None = None,
        audience: list[str] | None = None,
        locator: str | None = None,
        acquisition: dict | None = None,
    ) -> dict:
        def finalize():
            if self.evidence is None or not self.identity:
                raise CaptureError("capture is unavailable for this identity")
            if not isinstance(upload_ids, list) or not 1 <= len(upload_ids) <= capture_schema.MAX_ARTIFACTS:
                raise CaptureError("upload_ids must contain between 1 and 20 uploads")
            try:
                normalized_upload_ids = [str(uuid.UUID(value)) for value in upload_ids]
            except (AttributeError, TypeError, ValueError) as error:
                raise CaptureError("upload_ids must contain UUIDs") from error
            if len(set(normalized_upload_ids)) != len(normalized_upload_ids):
                raise CaptureError("upload_ids contains duplicates")
            principal = self.principal or identity.resolve_principal(
                self.settings.identities_path, self.identity
            )
            acl = self.resolve_submit_audience(audience, use_default=True)
            try:
                provenance = (
                    capture_schema.AcquisitionProvenance.model_validate(acquisition)
                    if acquisition is not None
                    else None
                )
                with self.conn.transaction():
                    references = upload_sessions.finalize_uploads(
                        self.conn,
                        self.evidence,
                        actor=self.identity,
                        upload_ids=normalized_upload_ids,
                    )
                    receipt = CaptureService(self.conn, self.evidence).capture_references(
                        actor=capture_schema.Actor(
                            subject=principal.subject,
                            display_name=principal.display_name,
                        ),
                        audience=None if acl is None else tuple(acl),
                        adapter="mcp",
                        references=references,
                        idempotency_key=idempotency_key,
                        title=title,
                        occurred_at=_occurred_at(occurred_at),
                        locator=locator,
                        acquisition=provenance,
                    )
                    upload_sessions.consume_uploads(
                        self.conn,
                        actor=self.identity,
                        upload_ids=normalized_upload_ids,
                        capture_id=receipt["id"],
                    )
            except (TypeError, ValueError) as error:
                raise CaptureError("upload metadata is invalid") from error
            for upload_id in normalized_upload_ids:
                try:
                    self.evidence.delete(upload_sessions.staging_ref(upload_id))
                except CaptureError as error:
                    log.warning(
                        "upload staging cleanup failed",
                        extra={"error_class": error.__class__.__name__},
                    )
            return {
                "id": receipt["id"],
                "status": receipt["status"],
                "submitted_by": receipt["submitted_by"],
                "created_at": receipt["created_at"],
                "created": receipt["created"],
            }

        return self._call(
            "brain_upload_finalize",
            {"uploads": len(upload_ids) if isinstance(upload_ids, list) else 0,
             "audience": _audit_audience(audience)},
            finalize,
        )

    def submit(
        self,
        *,
        text: str | None = None,
        path: str | None = None,
        url: str | None = None,
        title: str | None = None,
        occurred_at: str | None = None,
        audience: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        values = {"text": text, "path": path, "url": url}
        present = [name for name, value in values.items() if value is not None]
        audit_args = _submit_audit_args(values, audience=audience)

        def queue_capture():
            if len(present) != 1:
                raise CaptureError("provide exactly one of text, path, or url")
            if path is not None:
                raise CaptureError("local paths require the official Stigmergy MCP bridge")
            if self.evidence is None or not self.identity:
                raise CaptureError("capture is unavailable for this identity")
            principal = self.principal or identity.resolve_principal(
                self.settings.identities_path, self.identity
            )
            acl = self.resolve_submit_audience(audience, use_default=True)
            service = CaptureService(self.conn, self.evidence)
            common = {
                "actor": capture_schema.Actor(
                    subject=principal.subject,
                    display_name=principal.display_name,
                ),
                "audience": None if acl is None else tuple(acl),
                "adapter": self.door or "mcp",
                "idempotency_key": idempotency_key or str(uuid.uuid4()),
                "title": title,
                "occurred_at": _occurred_at(occurred_at),
            }
            if text is not None:
                receipt = service.capture_text(text=text, **common)
            else:
                receipt = service.capture_public_url(url=url or "", **common)
            return {
                "id": receipt["id"],
                "status": receipt["status"],
                "submitted_by": receipt["submitted_by"],
                "created_at": receipt["created_at"],
                "created": receipt["created"],
                "message": (
                    f"Capture {receipt['id']} is queued. "
                    "Use brain_submissions to follow it."
                ),
            }

        return self._call("brain_submit", audit_args, queue_capture)

    def check_submit_audience(self, audience) -> list[str] | None:
        """Audit and validate an adapter's audience before it reserves submission state."""
        return self._call("brain_submit_audience", {"audience": _audit_audience(audience)},
                          lambda: self.resolve_submit_audience(audience, use_default=False))

    def resolve_submit_audience(
        self, audience, *, use_default: bool = False
    ) -> list[str] | None:
        """Resolve a valid audience the caller may publish to."""
        if audience is None:
            if not use_default:
                return None
            principal = self.principal or identity.resolve_principal(
                self.settings.identities_path, self.identity
            )
            return (
                None
                if principal.default_audience is None
                else list(principal.default_audience)
            )
        if isinstance(audience, list) and not audience:
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
        # Canonical ordering keeps queue idempotency independent of client list order.
        labels = sorted(labels)
        try:
            unknown = sorted(set(labels) - ops_files.known_groups(
                self.conn, self.settings.identities_path))
        except (IdentityError, OSError) as ex:
            raise CaptureError(
                f"the group roster could not be read, so an audience cannot be checked "
                f"against it ({ex.__class__.__name__}) — nothing was queued") from ex
        if unknown:
            raise CaptureError(
                f"no identity holds {', '.join(repr(g) for g in unknown)}, so a capture filed "
                f"there would be readable by nobody — and a filed page's audience cannot be "
                f"changed afterwards. Check the spelling, or omit `audience` to file it open")
        if not self.unrestricted and not set(labels) <= set(self.audiences or ()):
            raise CaptureError(NOT_YOURS_TO_FILE_AT.format(
                holds=", ".join(sorted(self.audiences)) if self.audiences else "none"))
        return labels

    def submissions(self, limit: int = DEFAULT_SUBMISSION_LIMIT, status: str | None = None) -> dict:
        """Return the caller's submissions, or all submissions for the master."""
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
        return {
            "id": row["id"],
            "operation": row["operation"],
            "status": row["status"],
            "submitted_by": row["submitted_by"],
            "mine": row["submitted_by"] == self.identity,
            "attempts": row["attempts"],
            "created_at": row["created_at"],
            "processing_started_at": row["processing_started_at"],
            "finished_at": row["finished_at"],
            "source_path": row["source_path"],
            "commit_sha": row["commit_sha"],
            "change_id": row["change_id"],
            "error_category": row["error_category"],
            "error": neutralize_fence(row["error"]),
            "artifacts": _neutralize_report(row["request"].get("artifacts", [])),
            "report": _neutralize_report(row["report"]),
        }
    def delete_pages(self, paths, why: str = "", *, source: str) -> dict:
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
        if not self.identity:
            raise CaptureError("deletion is unavailable for this identity")
        if not self.unrestricted:
            raise CaptureError(NOT_YOURS_TO_REMOVE)
        principal = self.principal or identity.resolve_principal(
            self.settings.identities_path, self.identity
        )
        request = capture_schema.DeleteRequest(
            idempotency_key=str(uuid.uuid4()),
            actor=capture_schema.Actor(
                subject=principal.subject,
                display_name=principal.display_name,
            ),
            paths=tuple(str(path) for path in paths or ()),
            rationale=why,
        )
        receipt = queue.enqueue_delete(self.conn, request)
        return {
            "id": receipt["id"],
            "status": receipt["status"],
            "submitted_by": receipt["submitted_by"],
            "source": source,
        }

    def scoped_entities(self) -> list[str]:
        records = self._registry_records()
        visible_claim_ids = {
            entity_id
            for entity_id, record in records.items()
            if entity_aliases.display_claim(record, self.audiences)
        }
        return sorted(visible_claim_ids)


def _submit_audit_args(values: dict, *, audience=None) -> dict:
    present = [name for name, value in values.items() if value is not None]
    text = values.get("text")
    data = text.encode("utf-8") if isinstance(text, str) else b""
    return {
        "input": present,
        "text_bytes": len(data),
        "text_sha256": hashlib.sha256(data).hexdigest() if data else "",
        "audience": _audit_audience(audience),
    }


def _audit_audience(audience) -> list[str] | None:
    """Return a sorted, deduplicated, bounded audience for audit storage."""
    if not isinstance(audience, list):
        return None
    return sorted({str(a)[:MAX_ARG_CHARS] for a in audience[:MAX_AUDIT_HINT_KEYS]})


def _occurred_at(value: str | None) -> dt.datetime | dt.date | None:
    if not value:
        return None
    normalized = value.strip()
    try:
        if "T" in normalized:
            return dt.datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        return dt.date.fromisoformat(normalized)
    except ValueError as error:
        raise CaptureError("occurred_at must be an ISO date or timezone-aware timestamp") from error


def _neutralize_report(report, depth: int = 0):
    """Neutralize captured text in a report and bound nested structures."""
    if depth > MAX_AUDIT_DEPTH:
        return None
    if isinstance(report, str):
        return neutralize_fence(report)
    if isinstance(report, dict):
        return {str(k): _neutralize_report(v, depth + 1) for k, v in report.items()}
    if isinstance(report, list | tuple):
        return [_neutralize_report(v, depth + 1) for v in report]
    return report


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
        "entity": h.get("entity") or [],
        "updated": h.get("updated", ""),
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
        log.warning(
            "starting without a query embedder; search_brain and ask will refuse (%s)",
            ex.__class__.__name__,
        )
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
    principal = identity.resolve_principal(settings.identities_path, settings.identity)
    audiences_tuple = principal.audiences
    conn, embedder = open_scoped_resources(settings, conn)
    audiences = set(audiences_tuple) if audiences_tuple is not None else None

    ensure_audit_table(conn)
    ensure_capture_schema(conn)
    upload_sessions.ensure_upload_schema(conn)
    ensure_change_schema(conn)
    # Created single-threaded here so the webhook's own `IF NOT EXISTS` has nothing left to race:
    # losing that race inside its phase-2 transaction would roll the pushed pages back with it.
    store.ensure_ops_file_table(conn)
    store.ensure_webhook_dedupe_table(conn)
    # `store_from_env` does no I/O, so a server whose bucket is unreachable still serves reads.
    return BrainService(
        settings,
        conn,
        embedder,
        audiences,
        identity=settings.identity,
        principal=principal,
        audit=AuditWriter(conn),
        evidence=evidence_plane.store_from_env(),
    )
