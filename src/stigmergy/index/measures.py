"""Graph measures: the one definition the backoffice panel, the tests, and the evaluations read.

A scope is the graph one kind of reader sees: `brain` every row, `public` the organization-wide rows,
and a layer its own rows with `public`'s, fused by virtual path as readers see them. The measures
describe the graph; none is a corpus-health finding and none creates a task.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from stigmergy.index import corpus
from stigmergy.kernel import layers, tenancy

log = logging.getLogger(__name__)

BRAIN = "brain"
HUB_COUNT = 10
LIST_CAP = 200
CONSTELLATION_NEIGHBORS = 12
CONSTELLATION_ORPHANS = 40
SERIES_POINTS = 400
KNOWLEDGE_TYPES = ("note", "concept", "entity", "project")
_CITATION = re.compile(r"sources/\d{4}/\d{2}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.md")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_ITEM = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,9}[.)])\s+\S")
_UNCITED_LISTS = frozenset({"entities", "connections", "people and organizations", "notes"})

_DDL = f"""
CREATE TABLE IF NOT EXISTS graph_measures (
    id BIGSERIAL PRIMARY KEY,
    {tenancy.TENANT_COLUMN},
    taken_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    indexed_commit TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL,
    measures JSONB NOT NULL
)
"""
_INDEX_DDL = ("CREATE INDEX IF NOT EXISTS graph_measures_scope_taken "
              "ON graph_measures (tenant_id, scope, taken_at)")


class MeasureScopeError(ValueError):
    """The requested scope holds no row; the message names the scopes that do."""


@dataclass
class Page:
    """One fused page of a scope."""

    path: str
    title: str
    type: str
    entity_type: str = ""
    aliases: list[str] = field(default_factory=list)
    links: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    stems: set[str] = field(default_factory=set)
    body: str = ""
    layers: list[str] = field(default_factory=list)


def scopes_of(rows: list[dict]) -> list[str]:
    """`brain`, `public`, then every layer that holds a row, by name."""
    held = sorted({layer for row in rows if (layer := _layer(row["acl"])) not in ("", layers.PUBLIC)})
    return [BRAIN, layers.PUBLIC, *held]


def _layer(acl) -> str:
    try:
        return layers.layer_of_acl(acl)
    except layers.LayerError:
        return ""


def _in_scope(row: dict, scope: str) -> bool:
    layer = _layer(row["acl"])
    if scope == BRAIN:
        return bool(layer)
    return layer in (layers.PUBLIC, scope)


def _pages(rows: list[dict], scope: str, by_stem) -> dict[str, Page]:
    """The fused pages of `scope`, with the wikilink stems that resolve to nothing in each row's
    own scope (its layer and `public`) counted as dead."""
    selected = sorted((row for row in rows if _in_scope(row, scope)),
                      key=lambda row: (row["acl"] is not None, row["path"]))
    pages: dict[str, Page] = {}
    for row in selected:
        page = pages.setdefault(row["vpath"], Page(path=row["vpath"], title=row["title"], type=row["type"]))
        page.layers.append(_layer(row["acl"]))
        page.entity_type = page.entity_type or (row.get("entity_type") or "")
        page.aliases.extend(alias for alias in row.get("aliases") or () if alias not in page.aliases)
        page.links.update(row.get("links") or ())
        page.sources.update(row.get("sources") or ())
        page.body += "\n" + (row.get("body") or "")
        layer, _vpath = layers.split_path(row["path"])
        for stem in set(corpus.link_targets(row.get("body") or "")):
            targets = {path for each in dict.fromkeys((layer, layers.PUBLIC))
                       for path in by_stem.get((each, stem), ())}
            if len(targets) != 1:
                page.stems.add(stem)
    return pages


def _paragraphs(body: str) -> list[bool]:
    """Whether each paragraph or list of the body cites a source, as the librarian cites: once per
    paragraph, once after a list whose items share it, and never in the Entities and Connections
    lists. A block holding only a citation cites the block before it in the same section."""
    cited: list[bool] = []
    section, block, foldable = "", [], False

    def flush():
        nonlocal foldable
        text = "\n".join(block)
        block.clear()
        if not text or section in _UNCITED_LISTS:
            return
        if not _CITATION.sub("", re.sub(r"\(Sources?:|[`();,\s]", "", text)):
            if foldable:
                cited[-1] = True
            return
        cited.append(bool(_CITATION.search(text)))
        foldable = True

    for line in body.splitlines():
        if _HEADING.match(line) or not line.strip():
            flush()
            if line.strip():
                section, foldable = line.strip().lstrip("#").strip().casefold(), False
        else:
            block.append(line)
    flush()
    return cited


def _facts(body: str) -> int:
    """List items under every `## Facts` heading of the body."""
    count, inside = 0, False
    for line in body.splitlines():
        if _HEADING.match(line):
            inside = line.strip().lstrip("#").strip().casefold() == "facts"
        elif inside and _ITEM.match(line):
            count += 1
    return count


def compute(rows: list[dict], scope: str, by_stem=None) -> dict:
    """The measures of one scope, with the hubs, the drill-down lists, and the constellation."""
    if by_stem is None:
        by_stem = corpus.by_stem_index([row["path"] for row in rows])
    pages = _pages(rows, scope, by_stem)
    inbound: Counter[str] = Counter()
    for page in pages.values():
        for target in page.links:
            if target != page.path and target in pages:
                inbound[target] += 1
    knowledge = [page for page in pages.values() if page.type in KNOWLEDGE_TYPES]
    entities = [page for page in knowledge if page.type == "entity"]
    paragraphs = [cited for page in knowledge for cited in _paragraphs(page.body)]
    orphans = [page for page in entities if not inbound[page.path]]
    thin = [page for page in entities if _facts(page.body) <= 1]
    unaliased = [page for page in entities if not page.aliases]
    hubs = sorted((page for page in pages.values() if inbound[page.path]),
                  key=lambda page: (-inbound[page.path], page.path))[:HUB_COUNT]
    by_type = Counter(page.type for page in pages.values())
    measures = {
        "pages": sum(by_type.values()),
        "pages_by_type": {kind: by_type.get(kind, 0) for kind in (*KNOWLEDGE_TYPES, "source")},
        "links_per_page": round(sum(len(page.links - {page.path}) for page in knowledge)
                                / len(knowledge), 2) if knowledge else 0.0,
        "dead_links": sum(len(page.stems) for page in pages.values()),
        "orphan_entities": len(orphans),
        "multi_source_entities": sum(1 for page in entities if len(page.sources) >= 2),
        "cited_paragraphs": round(sum(paragraphs) / len(paragraphs), 4) if paragraphs else 0.0,
        "entities_without_aliases": len(unaliased),
        "thin_entities": len(thin),
        "entities": len(entities),
    }
    return {
        "measures": measures,
        "hubs": [_ref(page, inbound) for page in hubs],
        "lists": {
            "orphan_entities": [_ref(page, inbound) for page in sorted(orphans, key=_by_title)][:LIST_CAP],
            "thin_entities": [{**_ref(page, inbound), "facts": _facts(page.body)}
                              for page in sorted(thin, key=_by_title)][:LIST_CAP],
            "entities_without_aliases": [_ref(page, inbound)
                                         for page in sorted(unaliased, key=_by_title)][:LIST_CAP],
        },
        "constellation": _constellation(pages, hubs, orphans, inbound),
    }


def _by_title(page: Page) -> tuple[str, str]:
    return page.title.casefold(), page.path


def _ref(page: Page, inbound: Counter) -> dict:
    return {"path": page.path, "title": page.title, "type": page.type,
            "entity_type": page.entity_type, "inbound": inbound[page.path], "layers": page.layers}


def _constellation(pages: dict[str, Page], hubs: list[Page], orphans: list[Page], inbound: Counter) -> dict:
    """The hubs, their most-linked one-hop neighbours, and the orphan entities, with the links
    among them."""
    chosen: dict[str, Page] = {page.path: page for page in hubs}
    for hub in hubs:
        neighbours = {path for path in hub.links if path in pages} | {
            page.path for page in pages.values() if hub.path in page.links}
        neighbours.discard(hub.path)
        for path in sorted(neighbours, key=lambda path: (-inbound[path], path))[:CONSTELLATION_NEIGHBORS]:
            chosen[path] = pages[path]
    for page in sorted(orphans, key=_by_title)[:CONSTELLATION_ORPHANS]:
        chosen[page.path] = page
    edges = sorted({tuple(sorted((page.path, target))) for page in chosen.values()
                    for target in page.links if target in chosen and target != page.path})
    return {
        "nodes": [{"path": page.path, "title": page.title, "type": page.type,
                   "inbound": inbound[page.path], "orphan": page.type == "entity" and not inbound[page.path]}
                  for page in chosen.values()],
        "edges": [{"from": a, "to": b} for a, b in edges],
    }


_ROW_COLUMNS = ("path", "vpath", "acl", "title", "type", "entity_type", "aliases", "links", "sources", "body")


def index_rows(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_ROW_COLUMNS)} FROM pages_index ORDER BY path")
        return [dict(zip(_ROW_COLUMNS, row, strict=True)) for row in cur.fetchall()]


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        tenancy.ensure_tenants(cur)
        cur.execute(_DDL)
        cur.execute(_INDEX_DDL)
        tenancy.protect(cur, "graph_measures")


def record_snapshot(conn, indexed_commit: str) -> int:
    """Store one snapshot per scope of the index as it stands at `indexed_commit`, once per commit;
    never raises, so a measure can never fail the index update that triggered it."""
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT indexed_commit FROM graph_measures WHERE scope = %s"
                        " ORDER BY taken_at DESC, id DESC LIMIT 1", (BRAIN,))
            latest = cur.fetchone()
        if indexed_commit and latest is not None and latest[0] == indexed_commit:
            return 0
        rows = index_rows(conn)
        by_stem = corpus.by_stem_index([row["path"] for row in rows])
        snapshots = [(scope, compute(rows, scope, by_stem)["measures"]) for scope in scopes_of(rows)]
        with conn.transaction(), conn.cursor() as cur:
            for scope, measured in snapshots:
                cur.execute("INSERT INTO graph_measures (indexed_commit, scope, measures) VALUES (%s, %s, %s)",
                            (indexed_commit or "", scope, json.dumps(measured)))
        return len(snapshots)
    except Exception as error:  # noqa: BLE001 — analytics never fail indexing
        log.warning("graph measures snapshot failed (%s)", error.__class__.__name__)
        return 0



def read(conn, scope: str, *, now: dt.datetime | None = None) -> dict:
    """The current measures of `scope` computed from the index, its stored series, the value seven
    days earlier, and the drill-down lists and constellation."""
    rows = index_rows(conn)
    available = scopes_of(rows)
    if scope not in available:
        raise MeasureScopeError(f"scope must be one of {', '.join(available)}")
    ensure_schema(conn)
    current = compute(rows, scope)
    moment = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    with conn.cursor() as cur:
        cur.execute("SELECT taken_at, indexed_commit, measures FROM ("
                    " SELECT taken_at, indexed_commit, measures FROM graph_measures WHERE scope = %s"
                    " ORDER BY taken_at DESC, id DESC LIMIT %s) AS recent ORDER BY taken_at",
                    (scope, SERIES_POINTS))
        series = [{"taken_at": taken.isoformat(), "indexed_commit": commit, "measures": measures}
                  for taken, commit, measures in cur.fetchall()]
        cur.execute("SELECT measures FROM graph_measures WHERE scope = %s AND taken_at <= %s"
                    " ORDER BY taken_at DESC, id DESC LIMIT 1", (scope, moment - dt.timedelta(days=7)))
        week = cur.fetchone()
    return {"scope": scope, "scopes": available, **current, "series": series,
            "week_ago": week[0] if week else None}
