"""Text renderers for the answer agent's evidence ledger."""
from stigmergy.index import fragments
from stigmergy.server.service import READ_BATCH, BrainService, fence, neutralize_fence

SEARCH_RESULTS = 16
NO_RESULTS = "no results for that query"
UNKNOWN_PAGE = "unknown page"
UNKNOWN_ENTITY = "unknown entity"


def _render_nav(label: str, entries: list[dict], note: str) -> str:
    """Render already-scoped navigation entries."""
    lines = [f"{label}: {note}"]
    lines.extend(f"  - {e['path']} — {e['title']}" for e in entries)
    return "\n".join(lines)


class AnswerBrain:
    """A text view of one `BrainService`, scoped to the caller's identity."""

    def __init__(self, service: BrainService):
        self.service = service

    def search_text(self, query: str, ctx=None, filters: dict | None = None) -> str:
        """Render fenced search results and record surfaced evidence."""
        if ctx is not None:
            ctx.note_query(query)
        result = self.service.search(query, filters=filters, max_results=SEARCH_RESULTS)
        hits = result["hits"]
        if not hits:
            return NO_RESULTS
        lines = [f"{len(hits)} most similar of {result.get('ranked', len(hits))} ranked pages; "
                 "search is not exhaustive, list_pages is"]
        for h in hits:
            if ctx is not None:
                ctx.note_page(h["path"])                # a found page counts as surfaced
            meta = " · ".join(x for x in (h.get("type"), h.get("updated")) if x)
            lines.append(f"- {h['path']}\n  {h.get('title', '')} ({meta})")
            if h.get("summary"):
                lines.append(f"  {neutralize_fence(h['summary'])}")
            if h.get("fragment"):
                lines.append(f"  found in: {h['fragment']} (fragment of {h['fragments']}; read_pages it)")
            if h.get("snippet"):
                lines.append(f"  match: {h['snippet'][:400 if h.get('fragment') else 200]}")
            if h.get("links"):
                lines.append("  links: " + "; ".join(neutralize_fence(link["path"])
                                                     for link in h["links"]))
        return fence("\n".join(lines))

    def list_text(self, ctx=None, **filters) -> str:
        """Render one fenced `list_pages` response and record every listed page as surfaced."""
        if ctx is not None:
            ctx.note_query(" ".join(f"{key}={value}" for key, value in filters.items() if value))
        result = self.service.list_pages(**filters)
        lines = [f"total: {result['total']} matching page(s); showing {result['count']}"
                 + (f"; next cursor: {result['next']}" if result["next"] else "; no more pages")]
        for page in result["pages"]:
            if ctx is not None:
                ctx.note_page(page["path"])
            span = " to ".join(dict.fromkeys(day for day in (page["first_source"], page["last_source"])
                                             if day)) or "undated"
            kind = page["type"] + (f", {page['entity_type']}" if page.get("entity_type") else "")
            lines.append(f"- {page['path']}\n  {neutralize_fence(page['title'])} ({kind} · {span} · "
                         f"{page['sources']} source(s))")
            if page.get("aliases"):
                lines.append("  aliases: " + ", ".join(page["aliases"]))
            if page.get("summary"):
                lines.append(f"  {neutralize_fence(page['summary'])}")
        return fence("\n".join(lines))

    def page_text(self, path: str, ctx=None) -> str:
        """Render one scoped page and record it as surfaced evidence."""
        return self._render_page(path, self.service.read_page(path), ctx)

    def pages_text(self, paths: list[str], ctx=None) -> list[str]:
        """Render a batch of scoped pages read through one service call."""
        batch = [p for p in dict.fromkeys(paths) if isinstance(p, str)][:READ_BATCH]
        return [self._render_page(path, page, ctx)
                for path, page in zip(batch, self.service.read_pages(batch), strict=True)]

    def _render_page(self, path: str, page: dict, ctx) -> str:
        if "error" in page:
            return UNKNOWN_PAGE
        if ctx is not None:
            ctx.note_page(page["path"])
            if page.get("fragment"):
                ctx.note_window(page["path"], page["fragment"])
        alias_text = ", ".join(page.get("aliases") or ())
        head = (f"path: {page['path']}"
                + (f"\nshowing: fragment {page['fragment']} of {page['fragments']}" if page.get("fragment") else "")
                + f"\ntitle: {neutralize_fence(page['title'])}"
                f"\ntype: {neutralize_fence(page['type'])}\nstatus: {neutralize_fence(page['status'])}"
                + (f"\naliases: {neutralize_fence(alias_text)}" if alias_text else "")
                + f"\nupdated: {page['updated']}"
                f"\n{_render_nav('links', page['links'], page['links_note'])}"
                f"\n{_render_nav('backlinks', page['backlinks'], page['backlinks_note'])}")
        return f"{head}\n{page['body']}"

    def entity_text(self, entity: str, ctx=None) -> str:
        """Render the reader-scoped entity projection as model evidence."""
        if ctx is not None:
            ctx.note_query(entity)
        result = self.service.describe_entity(entity)
        if not result.get("found") or result.get("entity") is None:
            return UNKNOWN_ENTITY
        ent = result["entity"]
        if ctx is not None:
            ctx.note_page(ent["path"])
        lines = [f"entity: {ent['path']}",
                 f"name: {ent['title']}",
                 f"type: {ent['entity_type'] or '(none)'}",
                 f"aliases: {', '.join(ent['aliases']) or '(none)'}",
                 f"page: {ent['excerpt']}"]
        lines.append(f"knowledge: {result['knowledge_note']}")
        lines.append(f"knowledge state: {result.get('knowledge_state', 'available')}")
        for item in result["knowledge"]:
            if ctx is not None:
                ctx.note_page(item["path"])
            lines.append(f"  - {item['updated'] or '(undated)'} · {item['path']} — "
                         f"{item['title']} ({item['type']}, {item['status']})")
            if item.get("excerpt"):
                lines.append(f"    evidence: {item['excerpt']}")
            for relationship in item.get("relationships", ()):
                lines.append(f"    connection: {relationship['statement']} "
                             f"[{relationship['path']}]")
            for source in item.get("sources", ()):
                if ctx is not None:
                    ctx.note_page(source["path"])
        lines.append("sources:")
        for item in result["sources"]:
            if ctx is not None:
                ctx.note_page(item["path"])
            lines.append(f"  - {item['path']} — {item['title']}")
        if not result["sources"]:
            lines.append("  (none)")
        return "\n".join(lines)

    def get_page(self, path: str, *, windows=()) -> dict | None:
        """The scoped raw page used by verification: its head as `read_page` returns it, followed by
        the text of each fragment window in `windows` (fragment numbers the run read)."""
        page_path, _number = fragments.parse_reference(path)
        page = self.service.fetch_page_raw(page_path)
        if page is None:
            return None
        texts = [text for number in sorted(set(windows))
                 if (text := self.service.fragment_text(page_path, number)) is not None]
        return {**page, "body": "\n\n".join([page["body"], *texts])} if texts else page

    def known_entities(self) -> list[str]:
        """Return the paths of the visible entity pages."""
        return self.service.scoped_entities()
