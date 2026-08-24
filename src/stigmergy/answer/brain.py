"""Text renderers for the answer agent's evidence ledger."""
from stigmergy.server.service import BrainService, fence, neutralize_fence

SEARCH_RESULTS = 8
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
        lines = []
        for h in hits:
            if ctx is not None:
                ctx.note_page(h["path"])                # a found page counts as surfaced
            entity_text = ", ".join(h.get("entity") or ())
            meta = " · ".join(x for x in (h.get("type"), entity_text, h.get("updated")) if x)
            lines.append(f"- {h['path']}\n  {h.get('title', '')} ({meta})"
                         + f"\n  {(h.get('snippet') or '')[:200]}")
        return fence("\n".join(lines))

    def page_text(self, path: str, ctx=None) -> str:
        """Render one scoped page and record it as surfaced evidence."""
        page = self.service.read_page(path)
        if "error" in page:
            return UNKNOWN_PAGE
        if ctx is not None:
            ctx.note_page(path)
        entity_text = ", ".join(page.get("entity") or ())
        head = (f"path: {page['path']}\ntitle: {neutralize_fence(page['title'])}"
                f"\ntype: {neutralize_fence(page['type'])}\nstatus: {neutralize_fence(page['status'])}"
                f"\nentity: {neutralize_fence(entity_text)}"
                f"\nupdated: {page['updated']}"
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
        lines = [f"entity: {ent['id']}",
                 f"name: {ent['name']}",
                 f"type: {ent['type'] or '(none)'}",
                 f"aliases: {', '.join(ent['aliases']) or '(none)'}"]
        lines.append(f"knowledge: {result['knowledge_note']}")
        for item in result["knowledge"]:
            if ctx is not None:
                ctx.note_page(item["path"])
            lines.append(f"  - {item['updated'] or '(undated)'} · {item['path']} — "
                         f"{item['title']} ({item['type']}, {item['status']})")
        lines.append("sources:")
        for item in result["sources"]:
            if ctx is not None:
                ctx.note_page(item["path"])
            lines.append(f"  - {item['path']} — {item['title']}")
        if not result["sources"]:
            lines.append("  (none)")
        return "\n".join(lines)

    def get_page(self, path: str) -> dict | None:
        """Return the scoped raw page used by verification."""
        return self.service.fetch_page_raw(path)

    def known_entities(self) -> list[str]:
        """Return entity IDs with at least one visible claim."""
        return self.service.scoped_entities()
