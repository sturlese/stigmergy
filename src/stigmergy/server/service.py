"""Transport-independent knowledge service."""
import asyncio
import base64
import binascii
import datetime as dt
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import date
from pathlib import Path

from stigmergy import text as textutil
from stigmergy.capture import evidence as evidence_plane
from stigmergy.capture import queue
from stigmergy.capture import schema as capture_schema
from stigmergy.capture import uploads as upload_sessions
from stigmergy.capture.errors import CaptureError, EvidenceError
from stigmergy.capture.extraction import MAX_CAPTURE_EXTRACTED_BYTES
from stigmergy.capture.schema import ensure_capture_schema
from stigmergy.capture.service import CaptureService
from stigmergy.changes.store import ensure_change_schema
from stigmergy.index import fragments, rank, search, store
from stigmergy.index.backends.embedder import embedder_for_model
from stigmergy.index.corpus import ENTITY_TYPES
from stigmergy.index.errors import EmptyIndexError
from stigmergy.kernel import layers, tenancy
from stigmergy.kernel.normalize import resolution_key
from stigmergy.server import identity, platform, roster, routing, usage
from stigmergy.server.audit import AuditWriter, ensure_audit_table
from stigmergy.server.errors import (
    ArgumentLengthError,
    CapabilityUnavailableError,
    IdentityError,
    InvalidArgumentError,
    StartupError,
)
from stigmergy.server.settings import Settings

SLACK_DOOR = "slack"
SubmitRefused = CaptureError

# Identical refusal prevents page-existence disclosure.
NOT_YOURS_TO_REMOVE = "there is nothing for you to remove at those paths"

log = logging.getLogger(__name__)

# ACL filtering happens before visible-result truncation.
_CANDIDATE_HITS = 2 * rank.CANDIDATE_POOL
DEFAULT_MAX_RESULTS = 5
HIT_LINK_CAP = 8
HIT_LINK_TITLE_CHARS = 100
READ_BATCH = 5
PAGE_EXCERPT = 6000

NAV_CAP = 20
BROWSE_CAP = 5000
BROWSE_BODY_CHARS = 200_000
BROWSE_NEIGHBOR_CAP = 500
BROWSE_GRAPH_NODES = 60
_CITED_SOURCE_RE = re.compile(
    r"sources/\d{4}/\d{2}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.md")
ENTITY_EVIDENCE_EXCERPT = 1_200
ENTITY_RELATIONSHIP_EXCERPT = 700
ENTITY_RELATIONSHIP_CAP = 6
ENTITY_ITEM_SOURCE_CAP = 6
ENTITY_SOURCE_CAP = 40

MAX_ARG_CHARS = 8192

MAX_EXPANSION_TERMS = 12
MAX_NAMED_ENTITIES = 4
MIN_ENTITY_NAME_CHARS = 3
MAX_NAME_WORDS = 8
MAX_EXPANSION_TERM_CHARS = 100

DEFAULT_SUBMISSION_LIMIT = 20

LIST_PAGE_SIZE = 50
LIST_TYPES = ("note", "concept", "entity", "project", "source")
PUBLIC_LAYER_DESCRIPTION = "Organization-wide knowledge every identity reads."
_CURSOR_VERSION = 1
MAX_CURSOR_CHARS = 2048

MAX_AUDIT_HINT_KEYS = 32
MAX_AUDIT_DEPTH = 20

MAX_SUBMIT_MATCHES = 12
MIN_MATCH_CHARS = 3


def check_arg_length(name: str, value: str) -> None:
    """Reject oversized arguments with a safe transport error."""
    if len(value) > MAX_ARG_CHARS:
        raise ArgumentLengthError(f"{name} too long (max {MAX_ARG_CHARS} characters)")


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


ENTITY_ABSENCE = {
    "found": False,
    "entity": None,
    "knowledge": [],
    "knowledge_note": "No visible entity was found.",
    "knowledge_state": "not_found",
    "knowledge_truncated": False,
    "knowledge_returned": 0,
    "knowledge_cap": NAV_CAP,
    "sources": [],
    "sources_truncated": False,
    "sources_returned": 0,
    "sources_cap": ENTITY_SOURCE_CAP,
}


def _name_words(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", resolution_key(text)))


def _display_title(title: str) -> str:
    """Untrusted title -> a safe display string; never empty, never the raw path."""
    return neutralize_fence(title) if title else "(untitled)"


def _bounded_entity_paths(paths, *, limit: int) -> list[str]:
    """Keep a reader projection bounded before page metadata can expand in Python."""
    selected = []
    seen = set()
    for path in paths:
        if not isinstance(path, str) or path in seen:
            continue
        seen.add(path)
        selected.append(path)
        if len(selected) == limit:
            return selected
    return selected


def _bounded_entity_text(value: str, limit: int) -> tuple[str, bool]:
    """Sanitize untrusted Markdown and bound an evidence field without inventing a summary."""
    clean = textutil.sanitize(value or "").strip()
    if len(clean) <= limit:
        return clean, False
    boundary = clean.rfind(" ", 0, limit)
    return clean[:boundary if boundary > 0 else limit].rstrip() + "...", True


def _relationship_statement(body: str, target_path: str, target_title: str) -> tuple[str, bool] | None:
    """Return the authored line or paragraph that explains a visible page relationship."""
    needles = (Path(target_path).stem.casefold(), target_title.casefold())
    for part in re.split(r"\n\s*\n|\n", textutil.sanitize(body or "")):
        candidate = part.strip()
        if candidate.startswith("#"):
            continue
        lowered = candidate.casefold()
        if any(needle and needle in lowered for needle in needles):
            return _bounded_entity_text(candidate, ENTITY_RELATIONSHIP_EXCERPT)
    return None


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
        self._entities_memo: list[dict] | None = None

    def _resolved_principal(self) -> identity.Principal:
        """The caller's principal: the one bound at construction, else the roster's current one."""
        return self.principal or roster.principal(self.conn, self.identity)

    @property
    def unrestricted(self) -> bool:
        """Return whether queue reads include every identity."""
        return self.audiences is None

    @property
    def _limit_key(self) -> str:
        """Budgets are per identity per tenant: one email in two tenants is two callers."""
        tenant = self.principal.tenant if self.principal is not None else ""
        return f"{tenant}:{self.identity}" if tenant else str(self.identity)

    def _call(self, tool: str, args: dict, fn, *, summarize=None):
        """Run one rate-limited, audited tool call."""
        self._entities_memo = None
        start = time.monotonic()
        outcome, error_class, value = "ok", "", None
        try:
            if self.rate_limiter is not None:
                self.rate_limiter.check(self._limit_key, tool)
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
        self._entities_memo = None
        start = time.monotonic()
        outcome, error_class, value = "ok", "", None
        try:
            if self.rate_limiter is not None:
                self.rate_limiter.check(self._limit_key, tool)
            value = await coro_fn()
            return value
        except asyncio.CancelledError:
            outcome, error_class = "error", "CancelledError"
            raise
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
            if tool == "ask" and isinstance(value, dict):
                usage.record_answer(self.conn, value.get("usage"), self.settings.model)

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

        entities: tuple[str, ...] = ()
        if not (filters and "entity" in filters):
            entities = self._entities_named_in(query)
        return self._run_search(query, filters, max_results, entity_hints=entities)

    def _entity_rows(self) -> list[dict]:
        """Every entity name visible to this reader (path, title, type, aliases, layers), one fused
        record per virtual path and no page body, memoized for one tool call."""
        if self._entities_memo is None:
            clause, params = search.positional_audience_clause(self.audiences)
            with self.conn.cursor() as cur:
                cur.execute("SELECT path, vpath, acl, title, entity_type, aliases FROM pages_index"
                            f" WHERE type = 'entity'{clause} ORDER BY vpath, path", params)
                found = cur.fetchall()
            grouped: dict[str, list[dict]] = {}
            for path, vpath, acl, title, entity_type, aliases in found:
                layer = search.layer_of(acl)
                if layer:
                    grouped.setdefault(vpath, []).append({
                        "path": path, "vpath": vpath, "acl": acl, "layer": layer, "title": title,
                        "entity_type": entity_type or "", "aliases": list(aliases or ())})
            self._entities_memo = [layers.fuse_rows(rows) for rows in grouped.values()]
        return self._entities_memo

    def _entity_names(self) -> list[tuple[str, dict]]:
        """`(normalized name, row)` for every visible title and alias, longest names first."""
        pairs = []
        for row in self._entity_rows():
            for name in (row["title"], *(row.get("aliases") or ())):
                key = resolution_key(name)
                if key:
                    pairs.append((key, row))
        pairs.sort(key=lambda item: -len(item[0]))
        return pairs

    def _resolve_entity(self, reference: str) -> dict | None:
        """One visible entity page by exact path, title, or alias; ambiguity resolves to none."""
        rows = self._entity_rows()
        by_path = next((row for row in rows if row["path"] == reference), None)
        if by_path is not None:
            return by_path
        key = resolution_key(reference)
        matches = {row["path"]: row for name, row in self._entity_names() if name == key}
        return next(iter(matches.values())) if len(matches) == 1 else None

    def _entities_named_in(self, question: str) -> tuple[str, ...]:
        """The visible entity pages the question names, as paths in order of first mention.

        Names and the question are compared as runs of letters and digits, so punctuation never
        hides a name. Longer names claim their words first, so a name inside a longer one never
        resolves on its own; a name shared by two entity pages resolves to neither; names shorter
        than `MIN_ENTITY_NAME_CHARS` never resolve; at most `MAX_NAMED_ENTITIES` resolve."""
        words = _name_words(question)
        if not words:
            return ()
        owners: dict[tuple[str, ...], set[str]] = {}
        for name, row in self._entity_names():
            key = _name_words(name)
            if len("".join(key)) >= MIN_ENTITY_NAME_CHARS:
                owners.setdefault(key, set()).add(row["path"])
        longest = min(MAX_NAME_WORDS, max((len(key) for key in owners), default=0))
        matches = [(n, start, words[start:start + n])
                   for start in range(len(words))
                   for n in range(1, min(longest, len(words) - start) + 1)
                   if words[start:start + n] in owners]
        taken = [False] * len(words)
        found: list[tuple[int, str]] = []
        for n, start, key in sorted(matches, key=lambda m: (-m[0], -len("".join(m[2])), m[1])):
            if any(taken[start:start + n]):
                continue
            taken[start:start + n] = [True] * n
            if len(owners[key]) == 1:
                found.append((start, next(iter(owners[key]))))
        paths = tuple(dict.fromkeys(path for _, path in sorted(found)))
        return paths[:MAX_NAMED_ENTITIES]

    def _expansion_terms(self, entity_paths: tuple[str, ...]) -> tuple[str, ...]:
        """Bounded visible names of the resolved entities for lexical query expansion: every
        title first, then aliases taken in turn, so one entity cannot use every slot."""
        rows = {row["path"]: row for row in self._entity_rows()}
        names = [[rows[path]["title"], *(rows[path].get("aliases") or ())]
                 for path in entity_paths if path in rows]
        terms = (
            name
            for rank_ in range(max((len(n) for n in names), default=0))
            for group in names if rank_ < len(group)
            for name in (group[rank_],)
            if name and len(name) <= MAX_EXPANSION_TERM_CHARS
        )
        return tuple(dict.fromkeys(terms))[:MAX_EXPANSION_TERMS]

    def _run_search(self, query: str, filters: dict | None, max_results: int,
                    entity_hints: tuple[str, ...] = ()) -> dict:
        """Rank candidates, apply ACLs, then truncate visible results."""
        result = search.search_arms(self.conn, query, embedder=self.embedder, k=_CANDIDATE_HITS,
                                    filters=filters, today=date.today(), entity_hints=entity_hints,
                                    fts_expansion=self._expansion_terms(entity_hints),
                                    audiences=self.audiences)
        visible_hits = [h for h in result["hits"] if h.get("layers")]
        meta = store.read_meta(self.conn) or {}
        shown = visible_hits[:max_results]
        hits = [_shape_hit(h, links) for h, links in zip(shown, self._hit_links(shown), strict=True)]
        ranked = max(result["ranked"], len(hits))
        return {
            "query": query,
            "built_at": meta.get("built_at"),
            "embedding_model": meta.get("model"),
            "vector_search": result["vector_search"],
            "count": len(hits),
            "ranked": ranked,
            "note": (f"The {len(hits)} most similar of {ranked} ranked pages. Search is not "
                     "exhaustive; list_pages returns every visible page of a kind, period, or "
                     "connection."),
            "hits": hits,
        }

    def _hit_links(self, hits: list[dict]) -> list[list[dict]]:
        """Each hit's reader-visible outbound links, in index order and capped after visibility, so
        a restricted target neither takes a slot nor reveals its title."""
        targets = search.fetch_link_targets(
            self.conn, (path for hit in hits for path in hit.get("links") or ()), audiences=self.audiences)
        shown = []
        for hit in hits:
            links = []
            for path in hit.get("links") or ():
                target = targets.get(path)
                if target is None or path == hit["path"]:
                    continue
                links.append({"path": path, "title": textutil.clamp(
                    _display_title(target["title"]), HIT_LINK_TITLE_CHARS)})
                if len(links) == HIT_LINK_CAP:
                    break
            shown.append(links)
        return shown

    def fetch_page_raw(self, path: str) -> dict | None:
        """One page's raw fields (sanitized, excerpted, UNFENCED), fused from the layers the reader
        may see — None when the path is nonexistent OR out of scope, since existence itself is
        scoped. The single fetch+ACL base shared by `read_page` and the answer layer's verifier."""
        page = search.fused_pages(self.conn, [_virtual(path)], audiences=self.audiences).get(_virtual(path))
        if page is None:
            return None
        body = textutil.sanitize(page.get("body") or "")
        return {
            "path": page["path"],
            "title": page.get("title", ""),
            "type": page.get("type", ""),
            "entity_type": page.get("entity_type") or "",
            "aliases": page.get("aliases") or [],
            "status": page.get("status", ""),
            "updated": page.get("updated", ""),
            "layers": page.get("layers") or [],
            "links": page.get("links") or [],
            "sources": page.get("sources") or [],
            "body": body[:PAGE_EXCERPT],
            "truncated": len(body) > PAGE_EXCERPT,
        }

    def read_page(self, path: str) -> dict:
        """One page, trust signals first, body fenced as UNTRUSTED-DATA. An out-of-scope or
        nonexistent path returns the SAME shape — existence itself must not leak.
        Rate-limited + audited — see `_call`."""
        return self._call("read_page", {"path": path}, lambda: self._read_page(path))

    def read_pages(self, paths: list[str]) -> list[dict]:
        """Up to `READ_BATCH` pages in one rate-limited, audited call, each shaped exactly as
        `read_page` shapes it, so a hidden and a missing path stay indistinguishable."""
        batch = [p for p in dict.fromkeys(paths) if isinstance(p, str)][:READ_BATCH]
        return self._call("read_pages", {"paths": batch},
                          lambda: [self._read_page(path) for path in batch],
                          summarize=lambda r: {"pages": len(r)})

    def _read_page(self, path: str) -> dict:
        check_arg_length("path", path)   # before the DB read
        page_path, number = fragments.parse_reference(path)
        page = self.fetch_page_raw(page_path)
        if page is None:
            return {"error": f"unknown page: {path}"}
        if number is not None:
            window = self._fragment_window(page["path"], number)
            if window is None:
                return {"error": f"unknown page: {path}"}
            page.update(window)
        elif page["truncated"]:
            # A reader that cannot tell the head from the page answers from half of it.
            found = search.fragment_window(self.conn, page["path"], 1, audiences=self.audiences)
            total = found[1] if found else 0
            page["fragments"] = total
            page["excerpt_note"] = (f"This body is the first {PAGE_EXCERPT} characters of a longer page; read "
                                    f"`{page['path']}#<n>` for fragment n of {total}, e.g. `{page['path']}#2`.")
        path = page["path"]
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

    def _fragment_window(self, vpath: str, number: int) -> dict | None:
        """Fragment `number` of a visible page with one neighbour on each side, within the excerpt
        budget and rendered as the fused page renders its rows: the part a search found, instead
        of the head of the page."""
        parts = self._window_parts(vpath, number)
        if parts is None:
            return None
        shown, total = parts
        body = "\n\n".join(f"[{label}]\n{text}" for label, text in shown)
        return {"body": body, "fragment": number, "fragments": total}

    def fragment_text(self, path: str, number: int) -> str | None:
        """The text a read of `path#number` shows, without its labels: what a quote from that
        window is checked against."""
        parts = self._window_parts(_virtual(path), number)
        return None if parts is None else "\n\n".join(text for _label, text in parts[0])

    def _window_parts(self, vpath: str, number: int) -> tuple[list[tuple[str, str]], int] | None:
        found = search.fragment_window(self.conn, vpath, number, audiences=self.audiences)
        if found is None:
            return None
        rows, total = found
        text = {row["number"]: textutil.sanitize(layers.reading_text(row["layer"], row["text"])) for row in rows}
        trail = {row["number"]: textutil.sanitize(row["trail"]) for row in rows}
        center = text[number][:PAGE_EXCERPT]
        room = (PAGE_EXCERPT - len(center)) // 2
        shown = []
        if room and number - 1 in text:
            shown.append((f"… fragment {number - 1} of {total}, its end", text[number - 1][-room:]))
        heading = f": {trail[number]}" if trail[number] else ""
        shown.append((f"fragment {number} of {total}{heading}", center))
        if room and number + 1 in text:
            shown.append((f"fragment {number + 1} of {total}, its start …", text[number + 1][:room]))
        return shown, total

    def _outbound_rows(self, paths: list[str]) -> list[tuple[str, str]]:
        """`(path, title)` for a page's visible outbound `links` — one batched fetch."""
        rows = search.fetch_link_targets(self.conn, paths, audiences=self.audiences)
        return [(p, rows[p]["title"]) for p in paths if p in rows]

    def _inbound_rows(self, path: str) -> list[tuple[str, str]]:
        """`(path, title)` for every visible page linking TO `path`, one per virtual path — the GIN
        containment lookup `pages_index_links_gin` exists for, never a scan."""
        clause, params = search.positional_audience_clause(self.audiences)
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT ON (vpath) vpath, title FROM pages_index"
                f" WHERE links @> ARRAY[%s]::text[] AND vpath <> %s{clause}"
                " ORDER BY vpath, (acl IS NOT NULL), path",
                (path, path, *params))
            return cur.fetchall()

    @staticmethod
    def _capped(rows: list[tuple]) -> tuple[list[tuple], int]:
        """Cap already reader-visible `rows` to `NAV_CAP`, returning `(shown rows, total count)`."""
        return rows[:NAV_CAP], len(rows)

    @staticmethod
    def _cap_note(total: int, shown: int, *, subject: str, empty: str) -> str:
        """No silent cap: the truncation, or its absence, is always stated."""
        if total == 0:
            return empty
        if total > shown:
            return f"More than {shown} page(s) {subject} — showing the first {shown}."
        return f"{total} page(s) {subject} — showing all {total}."

    def _nav_section(self, rows: list[tuple[str, str]], *,
                     subject: str, empty: str) -> tuple[list[dict], str]:
        """`(path, title)` pairs -> capped `{path, title}` entries + the truncation note."""
        shown, total = self._capped(rows)
        items = [{"path": p, "title": _display_title(t)} for p, t in shown]
        return items, self._cap_note(total, len(items), subject=subject, empty=empty)

    def browse_pages(self) -> dict:
        """The backoffice wiki catalog: every page this reader may see, lightest fields only."""
        return self._call("browse_pages", {}, self._browse_pages,
                          summarize=lambda r: {"pages": r["count"]})

    def _browse_pages(self) -> dict:
        clause, params = search.positional_audience_clause(self.audiences)
        with self.conn.cursor() as cur:
            cur.execute("SELECT vpath, title, type, entity_type, aliases, updated, acl, "
                        f"left(body, %s) FROM pages_index WHERE true{clause} ORDER BY vpath, path",
                        (rank.SUMMARY_SCAN_CHARS, *params))
            rows = cur.fetchall()
        grouped: dict[str, list[dict]] = {}
        for path, title, kind, entity_type, aliases, updated, acl, head in rows:
            grouped.setdefault(path, []).append({
                "vpath": path, "layer": layers.layer_of_acl(acl), "title": title, "type": kind,
                "entity_type": entity_type or "", "aliases": list(aliases or ()), "updated": updated or "",
                "head": head or ""})
        pages = []
        for group in grouped.values():
            page = layers.fuse_rows(group)
            pages.append({
                "path": page["path"], "title": _display_title(page["title"]), "type": page["type"],
                "entity_type": page["entity_type"], "aliases": page["aliases"], "updated": page["updated"],
                "layers": page["layers"], "summary": textutil.sanitize(rank.summary_of(page["head"])),
            })
        return {"count": len(pages), "pages": pages[:BROWSE_CAP],
                "truncated": len(pages) > BROWSE_CAP}

    def browse_graph(self) -> dict:
        """Every visible page and every link between two visible pages, for the global graph."""
        return self._call("browse_graph", {}, self._browse_graph,
                          summarize=lambda r: {"nodes": len(r["nodes"])})

    def _browse_graph(self) -> dict:
        clause, params = search.positional_audience_clause(self.audiences)
        with self.conn.cursor() as cur:
            cur.execute("SELECT vpath, title, type, entity_type, acl, links, sources "
                        f"FROM pages_index WHERE true{clause} ORDER BY vpath, (acl IS NOT NULL), path",
                        params)
            rows = cur.fetchall()
        nodes: dict[str, dict] = {}
        targets: dict[str, set[str]] = {}
        for path, title, kind, entity_type, acl, links, sources in rows:
            node = nodes.setdefault(path, {"path": path, "title": _display_title(title), "type": kind,
                                           "entity_type": entity_type or "", "layers": []})
            node["layers"].append(layers.layer_of_acl(acl))
            targets.setdefault(path, set()).update((*(links or ()), *(sources or ())))
        shown = list(nodes)[:BROWSE_CAP]
        visible_paths = set(shown)
        edges = sorted({(path, target) for path in shown for target in targets[path]
                        if target in visible_paths and target != path})
        return {
            "nodes": [nodes[path] for path in shown],
            "edges": [{"from": a, "to": b} for a, b in edges],
            "truncated": len(nodes) > BROWSE_CAP,
        }

    def browse_page(self, path: str) -> dict | None:
        """One visible page with its full body, visible neighbors, and cited sources; a hidden and
        a missing path both return None."""
        return self._call("browse_page", {"path": path}, lambda: self._browse_page(path))

    def _browse_page(self, path: str) -> dict | None:
        check_arg_length("path", path)
        path = _virtual(path)
        page = search.fused_pages(self.conn, [path], audiences=self.audiences).get(path)
        if page is None:
            return None
        body = page.get("body") or ""
        outbound = self._browse_rows(page.get("links") or [])
        clause, params = search.positional_audience_clause(self.audiences)
        with self.conn.cursor() as cur:
            cur.execute("SELECT DISTINCT vpath FROM pages_index "
                        f"WHERE links @> ARRAY[%s]::text[] AND vpath <> %s{clause} ORDER BY vpath",
                        (path, path, *params))
            inbound = self._browse_rows([row[0] for row in cur.fetchall()])
        cited = self._browse_rows(sorted(set(_CITED_SOURCE_RE.findall(body))))
        neighbors = {row[0]: row for row in [*outbound, *inbound, *cited][:BROWSE_GRAPH_NODES]}
        own_links = [*(page.get("links") or []), *(row[0] for row in cited)]
        nodes = {path: (page.get("title") or "", page.get("type") or "", own_links)}
        nodes.update({p: (row[1], row[2], row[3]) for p, row in neighbors.items()})
        edges = sorted({(a, b) for a, (_t, _k, links) in nodes.items() for b in links
                        if b in nodes and a != b})
        return {
            "path": path,
            "title": _display_title(page.get("title") or ""),
            "type": page.get("type") or "",
            "entity_type": page.get("entity_type") or "",
            "aliases": list(page.get("aliases") or ()),
            "status": page.get("status") or "",
            "updated": page.get("updated") or "",
            "layers": page.get("layers") or [],
            "body": textutil.sanitize(body[:BROWSE_BODY_CHARS]),
            "truncated": len(body) > BROWSE_BODY_CHARS,
            "links": [self._browse_item(row) for row in outbound[:BROWSE_NEIGHBOR_CAP]],
            "backlinks": [self._browse_item(row) for row in inbound[:BROWSE_NEIGHBOR_CAP]],
            "cited": [self._browse_item(row) for row in cited],
            "graph": {
                "nodes": [{"path": p, "title": _display_title(t), "type": k}
                          for p, (t, k, _links) in nodes.items()],
                "edges": [{"from": a, "to": b} for a, b in edges],
            },
        }

    def _browse_rows(self, paths: list[str]) -> list[tuple]:
        """`(path, title, type, links)` for the visible pages among `paths`, sorted by path."""
        paths = sorted({p for p in paths if isinstance(p, str)})
        pages = search.fused_pages(self.conn, paths, audiences=self.audiences,
                                   columns=("title", "type", "links", "sources"))
        return [(path, page.get("title") or "", page.get("type") or "",
                 [*(page.get("links") or ()), *(page.get("sources") or ())])
                for path, page in sorted(pages.items())]

    @staticmethod
    def _browse_item(row: tuple) -> dict:
        return {"path": row[0], "title": _display_title(row[1]), "type": row[2]}

    def entity_vocabulary(self) -> dict:
        """The ACL-scoped entity vocabulary in one response, for the backoffice: every visible
        entity page with its type, aliases, and layers."""
        return self._call("entity_vocabulary", {}, self._entity_vocabulary,
                          summarize=lambda r: {"entities": r["count"]})

    def list_pages(self, *, type: str | None = None, entity_type: str | None = None,
                   layer: str | None = None, since: str | None = None, until: str | None = None,
                   links_to: str | None = None, sort: str = "recent",
                   cursor: str | None = None) -> dict:
        """Every visible page matching the filters, fused by virtual path, 50 per call."""
        args = {"type": type, "entity_type": entity_type, "layer": layer, "since": since,
                "until": until, "links_to": links_to, "sort": sort, "cursor": cursor}
        return self._call("list_pages", {k: v for k, v in args.items() if v is not None},
                          lambda: self._list_pages(**args),
                          summarize=lambda r: {"pages": r["count"], "total": r["total"]})

    def _list_pages(self, *, type, entity_type, layer, since, until, links_to, sort, cursor) -> dict:
        for name, value in (("type", type), ("entity_type", entity_type), ("layer", layer),
                            ("since", since), ("until", until), ("links_to", links_to),
                            ("sort", sort), ("cursor", cursor)):
            if value is not None and not isinstance(value, str):
                raise InvalidArgumentError(f"{name} must be a string")
            if value is not None:
                check_arg_length(name, value)
        readable = self._readable_layers()
        page_type = type or None
        if page_type is not None and page_type not in LIST_TYPES:
            raise InvalidArgumentError(f"type must be one of {', '.join(LIST_TYPES)}")
        if entity_type:
            if entity_type not in ENTITY_TYPES:
                raise InvalidArgumentError(f"entity_type must be one of {', '.join(ENTITY_TYPES)}")
            if page_type not in (None, "entity"):
                raise InvalidArgumentError("entity_type applies to entity pages only")
            page_type = "entity"
        if layer and layer not in {item["name"] for item in readable}:
            raise InvalidArgumentError("layer must be one of the layers you may read: "
                                       + ", ".join(item["name"] for item in readable))
        since_day, until_day = _list_day("since", since), _list_day("until", until)
        if since_day and until_day and since_day > until_day:
            raise InvalidArgumentError("since must not be after until")
        if sort not in search.LIST_SORTS:
            raise InvalidArgumentError(f"sort must be one of {', '.join(search.LIST_SORTS)}")
        listing = _listing_key(type=type, entity_type=entity_type, layer=layer, since=since,
                               until=until, links_to=links_to, sort=sort)
        after = _decode_cursor(cursor, listing) if cursor else None
        response = {"total": 0, "count": 0, "pages": [], "next": None, "layers": readable,
                    "note": ("Every visible page that matches, in pages of "
                             f"{LIST_PAGE_SIZE}; pass `next` as `cursor` for the rest.")}
        target = None
        if links_to:
            target = self._list_target(links_to)
            if target is None:
                return response
        found, total = search.list_pages(
            self.conn, audiences=self.audiences, sort=sort, after=after, limit=LIST_PAGE_SIZE + 1,
            page_type=page_type, entity_type=entity_type or None, layer=layer or None,
            links_to=target, since=since_day, until=until_day)
        shown = found[:LIST_PAGE_SIZE]
        records = search.listing_records(self.conn, [path for path, _key in shown],
                                         audiences=self.audiences)
        cited = search.visible_sources(
            self.conn, (source for record in records.values() for source in record["sources"]),
            audiences=self.audiences)
        response["pages"] = [_listing_row(records[path], cited) for path, _key in shown
                             if path in records]
        response["count"] = len(response["pages"])
        response["total"] = total
        if len(found) > LIST_PAGE_SIZE:
            response["next"] = _encode_cursor(listing, shown[-1][1], shown[-1][0])
        return response

    def _list_target(self, reference: str) -> str | None:
        """The virtual path `links_to` names: a page path as given, else a visible entity's title
        or alias resolved as `describe_entity` resolves it. Unknown and hidden read alike."""
        if reference.strip().casefold().endswith(".md"):
            return _virtual(reference.strip())
        row = self._resolve_entity(reference)
        return row["path"] if row is not None else None

    def _readable_layers(self) -> list[dict]:
        """The layers this reader may read, `public` first, each with its description."""
        described = roster.layer_descriptions(self.conn)
        names = [name for name in described if layers.is_layer_name(name)
                 and name not in layers.RESERVED_NAMES]
        if self.audiences is not None:
            names = [name for name in names if name in self.audiences]
        return [{"name": layers.PUBLIC, "description": PUBLIC_LAYER_DESCRIPTION},
                *({"name": name, "description": described[name] or ""} for name in sorted(names))]

    def _entity_vocabulary(self) -> dict:
        entities = [
            {
                "path": row["path"],
                "title": _display_title(row["title"]),
                "entity_type": neutralize_fence(row.get("entity_type") or ""),
                "aliases": [neutralize_fence(alias) for alias in row.get("aliases") or []],
                "layers": row.get("layers") or [],
            }
            for row in sorted(self._entity_rows(), key=lambda row: row["title"].casefold())
        ]
        return {"count": len(entities), "entities": entities}

    def describe_entity(self, entity: str) -> dict:
        """Return the reader-scoped entity page and the visible pages that link it."""
        return self._call("describe_entity", {"entity": entity},
                          lambda: self._describe_entity(entity))

    def _describe_entity(self, entity: str) -> dict:
        check_arg_length("entity", entity)   # before any DB read
        named = self._resolve_entity(entity)
        row = (search.fused_pages(self.conn, [named["path"]], audiences=self.audiences).get(named["path"])
               if named is not None else None)
        if row is None:
            return dict(ENTITY_ABSENCE)
        body = re.sub(r"\A\s*#[^\n]*\n", "", row.get("body") or "")
        excerpt, excerpt_truncated = _bounded_entity_text(body, PAGE_EXCERPT)
        record = {
            "path": row["path"],
            "title": _display_title(row["title"]),
            "entity_type": neutralize_fence(row.get("entity_type") or ""),
            "aliases": [neutralize_fence(alias) for alias in row.get("aliases") or []],
            "layers": row.get("layers") or [],
            "status": neutralize_fence(row.get("status") or ""),
            "updated": neutralize_fence(row.get("updated") or ""),
            "excerpt": neutralize_fence(excerpt),
            "excerpt_truncated": excerpt_truncated,
        }
        (
            timeline_items,
            timeline_note,
            source_paths,
            knowledge_truncated,
            source_paths_truncated,
        ) = self._timeline_section(row["path"])
        own_sources = list(row.get("sources") or ())
        sources, sources_truncated = self._entity_sources(
            [*own_sources, *source_paths], candidates_truncated=source_paths_truncated
        )
        knowledge_returned = len(timeline_items)
        sources_returned = len(sources)

        return {
            "found": True,
            "entity": record,
            "knowledge": timeline_items,
            "knowledge_note": timeline_note,
            "knowledge_state": (
                "no_visible_knowledge" if knowledge_returned == 0
                else "capped" if knowledge_truncated
                else "available"
            ),
            "knowledge_truncated": knowledge_truncated,
            "knowledge_returned": knowledge_returned,
            "knowledge_cap": NAV_CAP,
            "sources": sources,
            "sources_truncated": sources_truncated,
            "sources_returned": sources_returned,
            "sources_cap": ENTITY_SOURCE_CAP,
        }

    def _timeline_section(self, entity_path: str) -> tuple[list[dict], str, list[str], bool, bool]:
        shown, has_more = search.entity_timeline(
            self.conn, entity_path, audiences=self.audiences, limit=NAV_CAP
        )
        source_paths = _bounded_entity_paths(
            (source for page in shown for source in page.get("sources") or ()),
            limit=ENTITY_SOURCE_CAP + 1,
        )
        source_paths_truncated = len(source_paths) == ENTITY_SOURCE_CAP + 1
        items = []
        for page in shown:
            path = page["path"]
            title = page["title"]
            page_type = page["type"]
            status = page["status"]
            updated = page["updated"]
            page_sources = page.get("sources") or ()
            excerpt, excerpt_truncated = _bounded_entity_text(
                page.get("body", ""), ENTITY_EVIDENCE_EXCERPT
            )
            relationship_candidates = _bounded_entity_paths(
                (
                    linked
                    for linked in page.get("links") or ()
                    if linked != entity_path and not linked.startswith("sources/")
                ),
                limit=ENTITY_RELATIONSHIP_CAP + 1,
            )
            relationship_candidates_truncated = (
                len(relationship_candidates) == ENTITY_RELATIONSHIP_CAP + 1
            )
            linked_rows, relationship_has_more = search.fetch_visible_pages_limited(
                self.conn,
                relationship_candidates,
                audiences=self.audiences,
                limit=ENTITY_RELATIONSHIP_CAP,
            )
            relationships = []
            for linked_path in relationship_candidates:
                linked = linked_rows.get(linked_path)
                if linked is None:
                    continue
                statement = _relationship_statement(
                    page.get("body", ""), linked_path, linked.get("title", "")
                )
                if statement is None:
                    continue
                text, statement_truncated = statement
                relationships.append({
                    "path": linked_path,
                    "title": _display_title(linked.get("title", "")),
                    "statement": text,
                    "statement_truncated": statement_truncated,
                })
            source_candidates, source_candidates_truncated = search.bounded_unique_paths(
                page_sources, limit=ENTITY_ITEM_SOURCE_CAP + 1
            )
            source_rows, local_source_has_more = search.fetch_visible_pages_limited(
                self.conn,
                source_candidates,
                audiences=self.audiences,
                limit=ENTITY_ITEM_SOURCE_CAP,
            )
            local_sources = [
                {"path": source, "title": _display_title(source_rows[source].get("title", ""))}
                for source in source_candidates if source in source_rows and source_rows[source].get("type") == "source"
            ]
            items.append({
                "path": path,
                "title": _display_title(title),
                "type": neutralize_fence(page_type),
                "status": neutralize_fence(status),
                "updated": neutralize_fence(updated),
                "excerpt": excerpt,
                "excerpt_truncated": excerpt_truncated,
                "sources": local_sources,
                "sources_truncated": local_source_has_more or source_candidates_truncated,
                "sources_returned": len(local_sources),
                "sources_cap": ENTITY_ITEM_SOURCE_CAP,
                "relationships": relationships,
                "relationships_truncated": relationship_has_more or relationship_candidates_truncated,
                "relationships_returned": len(relationships),
                "relationships_cap": ENTITY_RELATIONSHIP_CAP,
            })
        return (
            items,
            self._cap_note(len(items) + int(has_more), len(items), subject="link this entity",
                           empty="No pages link this entity."),
            source_paths,
            has_more,
            source_paths_truncated,
        )

    def _entity_sources(
        self, paths: list[str], *, candidates_truncated: bool = False
    ) -> tuple[list[dict], bool]:
        """Return only visible source records for already visible, capped knowledge evidence."""
        candidates, locally_truncated = search.bounded_unique_paths(
            paths, limit=ENTITY_SOURCE_CAP + 1
        )
        rows, has_more = search.fetch_visible_pages_limited(
            self.conn, candidates, audiences=self.audiences, limit=ENTITY_SOURCE_CAP
        )
        visible_sources = [
            {"path": path, "title": _display_title(rows[path].get("title", ""))}
            for path in candidates
            if path in rows
            and rows[path].get("type") == "source"
        ]
        return visible_sources, has_more or candidates_truncated or locally_truncated

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
            principal = self._resolved_principal()
            routed = self._route(audience)
            receipt = CaptureService(self.conn, self.evidence).capture_bytes(
                actor=capture_schema.Actor(
                    subject=principal.subject,
                    display_name=principal.display_name,
                ),
                audience=routed.acl,
                adapter=self.door or "mcp",
                artifact_values=artifact_values,
                idempotency_key=idempotency_key,
                title=title,
                occurred_at=occurred_at,
                locator=locator,
                participants=participants,
                acquisition=acquisition,
                layer_decision=routed.decision,
            )
            return {
                "id": receipt["id"],
                "status": receipt["status"],
                "submitted_by": receipt["submitted_by"],
                "created_at": receipt["created_at"],
                "created": receipt["created"],
                **routing.receipt_fields(receipt),
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
        resolution_of: str | None = None,
    ) -> dict:
        intent = _resolution_intent(resolution_of)

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
            principal = self._resolved_principal()
            routed = self._route(audience, lambda: self._verified_upload_state(
                normalized_upload_ids, title=title, locator=locator))
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
                        audience=routed.acl,
                        adapter="mcp",
                        references=references,
                        idempotency_key=idempotency_key,
                        title=title,
                        occurred_at=_occurred_at(occurred_at),
                        locator=locator,
                        intent=intent,
                        acquisition=provenance,
                        layer_decision=routed.decision,
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
                **routing.receipt_fields(receipt),
            }

        return self._call(
            "brain_upload_finalize",
            {"uploads": len(upload_ids) if isinstance(upload_ids, list) else 0,
             "audience": _audit_audience(audience),
             **_resolution_audit_args(intent)},
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
        resolution_of: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        values = {"text": text, "path": path, "url": url}
        present = [name for name, value in values.items() if value is not None]
        intent = _resolution_intent(resolution_of)
        audit_args = {
            **_submit_audit_args(values, audience=audience),
            **_resolution_audit_args(intent),
        }

        def queue_capture():
            if len(present) != 1:
                raise CaptureError("provide exactly one of text, path, or url")
            if path is not None:
                raise CaptureError("local paths require the official Stigmergy MCP bridge")
            if self.evidence is None or not self.identity:
                raise CaptureError("capture is unavailable for this identity")
            principal = self._resolved_principal()
            routed = self._route(audience, lambda: routing.capture_state(
                title=title, locator=url, text=text or ""))
            service = CaptureService(self.conn, self.evidence)
            common = {
                "actor": capture_schema.Actor(
                    subject=principal.subject,
                    display_name=principal.display_name,
                ),
                "audience": routed.acl,
                "adapter": self.door or "mcp",
                "idempotency_key": idempotency_key or str(uuid.uuid4()),
                "title": title,
                "occurred_at": _occurred_at(occurred_at),
                "layer_decision": routed.decision,
            }
            if text is not None:
                receipt = service.capture_text(text=text, intent=intent, **common)
            else:
                receipt = service.capture_public_url(url=url or "", intent=intent, **common)
            return {
                "id": receipt["id"],
                "status": receipt["status"],
                "submitted_by": receipt["submitted_by"],
                "created_at": receipt["created_at"],
                "created": receipt["created"],
                **routing.receipt_fields(receipt),
                "message": (
                    f"Capture {receipt['id']} is queued. "
                    "Use brain_submissions to follow it."
                ),
            }

        return self._call("brain_submit", audit_args, queue_capture)

    def check_submit_audience(self, audience) -> list[str] | None:
        """Audit and validate an adapter's audience before it reserves submission state."""

        def check():
            acl = self._route(audience).acl
            return None if acl is None else list(acl)

        return self._call("brain_submit_audience", {"audience": _audit_audience(audience)}, check)

    def _route(self, audience, state=dict) -> routing.Routed:
        """The one layer a capture files in and how it was decided. `state` is read only when the
        classifier would run; without one, an unnamed layer is the default."""
        if isinstance(audience, list) and not audience:
            raise CaptureError(
                "an empty `audience` names no layer: send one layer name, or omit `audience` to "
                "file in your default layer")
        try:
            return routing.route(
                self._resolved_principal(),
                audience,
                descriptions=lambda: roster.layer_descriptions(self.conn),
                state=state,
                fixed_by_door=self.door == SLACK_DOOR,
            )
        except (identity.LayerRefused, IdentityError) as ex:
            raise CaptureError(str(ex)) from ex

    def _verified_upload_state(self, upload_ids: list[str], *, title: str | None,
                               locator: str | None) -> dict:
        """Verify the uploads before the classifier reads them; the capture's own finalize then
        finds them verified."""
        with self.conn.transaction():
            references = upload_sessions.finalize_uploads(self.conn, self.evidence, actor=self.identity,
                                                          upload_ids=upload_ids)
        return routing.capture_state(
            title=title,
            files=[reference.original_name for reference in references if reference.original_name],
            locator=locator,
            text=_readable_text(self.evidence, references),
        )

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
            "layer": (layers.layer_of_acl(row["acl"])
                      if row["operation"] == capture_schema.CAPTURE else None),
            "layer_decision": row["layer_decision"],
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
        principal = self._resolved_principal()
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
        """Paths of every entity page visible to this reader."""
        return [row["path"] for row in self._entity_rows()]


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


def _readable_text(evidence, references) -> str:
    """The UTF-8 text of verified text and Markdown artifacts within the worker's readable bound;
    other formats need a parser and contribute nothing."""
    texts = []
    for reference in references:
        if (reference.media_type not in capture_schema.TEXT_MEDIA_TYPES
                or reference.bytes > MAX_CAPTURE_EXTRACTED_BYTES):
            continue
        try:
            texts.append(evidence.get_limited(reference.blob_ref, max_bytes=reference.bytes).decode("utf-8"))
        except (EvidenceError, UnicodeDecodeError):
            continue
    return "\n\n".join(texts)


def _resolution_intent(resolution_of: str | None) -> capture_schema.CaptureIntent | None:
    """Validate the optional target before it reaches capture storage or audit."""
    if resolution_of is None:
        return None
    return capture_schema.CaptureIntent(
        resolution_of=resolution_of,
        rationale="The submitting agent explicitly targeted this contradiction.",
    )


def _resolution_audit_args(intent: capture_schema.CaptureIntent | None) -> dict:
    """Expose only the validated contradiction identifier to audit storage."""
    return {} if intent is None else {"resolution_of": intent.resolution_of}


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
            occurred_at = dt.datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
                raise CaptureError(
                    "occurred_at must be an ISO date or timezone-aware timestamp")
            return occurred_at
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


def _virtual(path: str) -> str:
    """Readers address pages by virtual path; a layer's repository path names the same page."""
    try:
        return layers.virtual_path(path)
    except layers.LayerError:
        return path


def _list_day(name: str, value: str | None) -> str | None:
    if not value:
        return None
    try:
        if len(value) != 10:
            raise ValueError
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        raise InvalidArgumentError(f"{name} must be an ISO date (YYYY-MM-DD)") from None


def _listing_key(**arguments) -> str:
    """The listing a cursor belongs to: a digest of the caller's own arguments, never of what they
    resolved to, so a cursor's validity cannot depend on stored data."""
    raw = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _encode_cursor(listing: str, key: str, path: str) -> str:
    raw = json.dumps({"v": _CURSOR_VERSION, "l": listing, "k": key, "p": path}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, listing: str) -> tuple[str, str]:
    """`(sort key, virtual path)` from a cursor this service issued for the same listing."""
    invalid = InvalidArgumentError("cursor is not valid for this listing; start again without one")
    if len(cursor) > MAX_CURSOR_CHARS:
        raise invalid
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError, RecursionError):
        raise invalid from None
    if (not isinstance(data, dict) or data.get("v") != _CURSOR_VERSION or data.get("l") != listing
            or not isinstance(data.get("k"), str) or not isinstance(data.get("p"), str)):
        raise invalid
    return data["k"], data["p"]


def _listing_row(record: dict, cited: set[str]) -> dict:
    """One `list_pages` row; the body and raw acl labels never ship."""
    row = {
        "path": record["path"],
        "title": _display_title(record.get("title") or ""),
        "type": record.get("type") or "",
        "layers": record.get("layers") or [],
        "summary": textutil.sanitize(rank.summary_of(record.get("head") or "")),
        "first_source": (record.get("source_dates") or [""])[0],
        "last_source": (record.get("source_dates") or [""])[-1],
        "sources": len({source for source in record.get("sources") or () if source in cited}),
    }
    if row["type"] == "entity":
        row["entity_type"] = neutralize_fence(record.get("entity_type") or "")
        row["aliases"] = [neutralize_fence(alias) for alias in record.get("aliases") or ()]
    return row


def _shape_hit(h: dict, links: list[dict] | None = None) -> dict:
    """A search hit as JSON; the body and the raw acl labels never ship."""
    return {
        "path": h["path"],
        "title": h.get("title", ""),
        "type": h.get("type", ""),
        "summary": h.get("summary", ""),
        "snippet": h.get("snippet", ""),
        "links": links or [],
        "layers": h.get("layers") or [],
        "score": h["score"],
        "arms": h["arms"],
        "factors": h["factors"],
        "updated": h.get("updated", ""),
        **({"fragment": h["fragment"], "fragments": h["fragments"]} if h.get("fragment") else {}),
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
            f"list_pages and describe_entity need no embedder and are working normally.")


def _resolve_embedder(settings: Settings, model: str):
    """The query embedder must embed in the same space the documents did, so it defaults to the
    model the index was built with. A missing OPENROUTER_API_KEY yields an `UnavailableEmbedder`; an
    unknown embedder NAME raises, so a typo never presents as a missing credential."""
    if settings.embedder not in (None, "", "fake", "openrouter"):
        raise StartupError(
            f"unknown embedder {settings.embedder!r} (use 'openrouter' or 'fake')"
        )
    try:
        if settings.embedder == "fake":
            from stigmergy.index.backends.embedder import build_embedder
            return build_embedder("fake")
        if settings.embedder == "openrouter":
            from stigmergy.index.backends.embedder import build_embedder
            return build_embedder("openrouter", model)
        return embedder_for_model(model)          # match the built index (the usual path)
    except RuntimeError as ex:
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
    """Wire the stdio service, fail-closed: an identity the roster does not know never starts the
    server. Deliberately NO `RateLimiter`: the budgets protect spend behind a PUBLIC url."""
    conn = conn or store.connect(settings.dsn)
    platform.ensure_platform_schema(conn)
    tenancy.set_tenant(conn, platform.resolve_tenant(conn, settings.tenant).id)
    conn, embedder = open_scoped_resources(settings, conn)
    roster.ensure_roster_schema(conn)
    principal = roster.principal(conn, settings.identity)
    audiences = set(principal.audiences) if principal.audiences is not None else None

    ensure_audit_table(conn)
    ensure_capture_schema(conn)
    upload_sessions.ensure_upload_schema(conn)
    ensure_change_schema(conn)
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
