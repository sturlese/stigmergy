"""The evidence ledger — `AnswerBrain`, a text view of one `BrainService`.

The service speaks structured JSON; the agent and the verifier speak TEXT: the renderers here
produce exactly the evidence corpus the run is judged against, without changing the service
surface. `read_paths` is populated from BOTH search hits and read_page results — a citation to a
page the run merely found is legitimate, its quote still checked verbatim against the page body.

ENTITY-FIRST resolution lives in `BrainService._search`, NOT here: every client gets it, so
`search_text` is a thin renderer that resolves nothing itself.
"""
from stigmergy.server.service import BrainService, fence, neutralize_fence

SEARCH_RESULTS = 8          # candidate hits the agent's search tool surfaces per call

# The three absence strings, carrying NONE of the argument that produced them. Everything a
# renderer returns enters the evidence ledger the verifier traces against — an absence string
# echoing its own argument makes any figure the model puts in a missed query evidence for itself,
# voiding the untraced-figure invariant with no attacker needed. `ctx.note_query` is the channel
# for "what the model asked". Argument-free also means absence cannot differ between "does not
# exist" and "you may not see it" — the same reasoning as `BrainService`'s byte-identical absence
# shapes one layer down.
NO_RESULTS = "no results for that query"
UNKNOWN_PAGE = "unknown page"
UNKNOWN_ENTITY = "unknown entity"


def _render_nav(label: str, entries: list[dict], note: str) -> str:
    """A `links`/`backlinks` section of `page_text`'s head — the service's already-ACL-filtered,
    already-neutralized `{path, title}` entries, rendered VERBATIM: never re-derived from `body`,
    never re-neutralized, never a second fence."""
    lines = [f"{label}: {note}"]
    lines.extend(f"  - {e['path']} — {e['title']}" for e in entries)
    return "\n".join(lines)


class AnswerBrain:
    """A text view of one `BrainService`, scoped to the caller's identity."""

    def __init__(self, service: BrainService):
        self.service = service

    # ── textual renderings (what the agent's tools return) ──────────────────
    def search_text(self, query: str, ctx=None, filters: dict | None = None) -> str:
        """The search listing IS untrusted document data (page-derived title/entity/snippet), so
        the whole block is fenced UNTRUSTED-DATA with in-band tokens neutralized — a hostile
        title/snippet can neither close the fence nor read as an instruction. Records the literal
        query on `ctx.searched` (the refusal composer's bookkeeping, never asked of the model).
        `filters` passes straight through to `BrainService.search`; entity-first resolution lives
        there, not here."""
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
            flags = []
            if h.get("superseded_by"):
                flags.append("SUPERSEDED — prefer the current version")
            # `entity` is a list — joined for display.
            entity_text = ", ".join(h.get("entity") or ())
            # `type` joins the meta line: `ANSWER_SYS` tells the agent to identify "type: entity"
            # pages, and a search hit is the first place it sees one.
            meta = " · ".join(x for x in (h.get("type"), entity_text, h.get("as_of")) if x)
            lines.append(f"- {h['path']}\n  {h.get('title', '')} ({meta})"
                         + (f" [{'; '.join(flags)}]" if flags else "")
                         + f"\n  {(h.get('snippet') or '')[:200]}")
        return fence("\n".join(lines))

    def page_text(self, path: str, ctx=None) -> str:
        """One page as the agent reads it: trust signals and the links/backlinks navigation
        surface in the head (outside the fence — an agent told to walk a graph it cannot see will
        not walk it), body already fenced UNTRUSTED-DATA by the service. Page-derived head fields
        are neutralized so a hostile title cannot forge a fence delimiter; nav entries arrive
        already scoped and neutralized and are used verbatim."""
        page = self.service.read_page(path)
        if "error" in page:
            return UNKNOWN_PAGE
        if ctx is not None:
            ctx.note_page(path)
        # `entity` is a list — joined before neutralizing, which expects text.
        entity_text = ", ".join(page.get("entity") or ())
        head = (f"path: {page['path']}\ntitle: {neutralize_fence(page['title'])}"
                f"\ntype: {neutralize_fence(page['type'])}\nstatus: {neutralize_fence(page['status'])}"
                f"\nentity: {neutralize_fence(entity_text)}"
                f"\nas_of: {page['as_of']}"
                f"\nsuperseded_by: {page['superseded_by'] or '(no — current)'}"
                f"\n{_render_nav('links', page['links'], page['links_note'])}"
                f"\n{_render_nav('backlinks', page['backlinks'], page['backlinks_note'])}")
        return f"{head}\n{page['body']}"

    def entity_text(self, entity: str, ctx=None) -> str:
        """One entity's territory as the agent reads it: the service's `describe_entity` dict laid
        out as text — registry identity, its own page, the dated timeline with the
        service's truncation note. Every field arrives already ACL-scoped and neutralized and is
        rendered verbatim (`_render_nav` posture). Bookkeeping mirrors `search_text`: the lookup
        lands on `ctx.searched`, and every page reference shown counts as SURFACED."""
        if ctx is not None:
            ctx.note_query(entity)
        result = self.service.describe_entity(entity)
        if "error" in result:
            return UNKNOWN_ENTITY
        ent = result["entity"]
        lines = [f"entity: {ent['id']}",
                 f"name: {ent['name'] or '(unregistered)'}",
                 f"type: {ent['type'] or '(none)'}",
                 f"aliases: {', '.join(ent['aliases']) or '(none)'}"]
        page = ent.get("page")
        if page is not None:
            if ctx is not None:
                ctx.note_page(page["path"])
            lines.append(f"page: {page['path']} — {page['title']}")
        else:
            lines.append("page: (none)")
        lines.append(f"timeline: {result['timeline_note']}")
        for item in result["timeline"]:
            if ctx is not None:
                ctx.note_page(item["path"])
            lines.append(f"  - {item['as_of'] or '(undated)'} · {item['path']} — "
                         f"{item['title']} ({item['type']}, {item['status']})")
        return "\n".join(lines)

    # ── primitives for the verifier and the offline fake ────────────────────
    def get_page(self, path: str) -> dict | None:
        """Raw (UNFENCED) page for the verifier's verbatim-quote check and the fake — ACL-scoped;
        out-of-scope or nonexistent → None (existence itself is scoped)."""
        return self.service.fetch_page_raw(path)

    def known_entities(self) -> list[str]:
        """Entities with at least one page THIS client may see — existence is scoped too."""
        return self.service.scoped_entities()
