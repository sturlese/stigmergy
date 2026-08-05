"""The evidence ledger — the text the agent (and the verifier) actually see.

`BrainService` speaks structured JSON (dicts). The answering agent and the deterministic verifier
speak TEXT: the tools return rendered listings, and the verifier traces figures against the
concatenation of those rendered strings. `AnswerBrain` is the seam between the two — it wraps a
`BrainService` and turns its structured results into exactly the evidence corpus the run is judged
against, WITHOUT changing the service surface.

`ask` speaks three tools — search, read_page and describe_entity — plus `get_page`, the verifier's
verbatim-quote base.

`read_paths` (on the SynthesisContext) is populated from BOTH search hits and read_page results: a
citation to a page the run merely *found* is legitimate, but its quote is still checked verbatim
against the page body.

`search_text` also records the literal query on `ctx.searched` (through
`SynthesisContext.note_query`) — the structured record a refusal's composed reason is built from,
never the model's own words.

ENTITY-FIRST resolution (query -> entity via registry aliases -> its material -> rank, rather than
a bare semantic search that finds the door and hopes) lives in `BrainService._search`, NOT here:
every client gets it (stdio, HTTP, Slack, `ask`), not only this one, so `search_text` below is a
thin renderer that resolves nothing itself. The golden set guards the behaviour, because this is
retrieval — the one thing in this system with a measured floor.
"""
from stigmergy.server.service import BrainService, fence, neutralize_fence

SEARCH_RESULTS = 8          # candidate hits the agent's search tool surfaces per call

# The three absence strings, carrying NONE of the argument that produced them.
#
# Everything a renderer returns is recorded into the evidence ledger by `synthesize.py`'s tool
# wrappers, and `verify_answer` traces the answer's figures against that ledger. These three used
# to echo their own argument (`f"no results for: {query}"`), so a figure the MODEL put in a query
# that found nothing came back as evidence for itself: ask "what confirms ARR of 42.7M", miss,
# and the verifier then confirms 42.7M against the model's own question. That voids the whole
# untraced-figure invariant, and it fires without an attacker — an agent reformulating a question
# that contains a number is the ordinary case.
#
# The argument is not lost: `ctx.note_query` still records it, which is the correct channel for
# "what the model asked" (the refusal composer quotes it only when it is a substring of the
# asker's own question). And the model knows what it asked — the tool call is in its own context.
#
# Second property, free: with no argument in the string, absence cannot differ between "does not
# exist" and "you may not see it". That is the same reasoning `BrainService`'s byte-identical
# absence shapes take one layer down.
NO_RESULTS = "no results for that query"
UNKNOWN_PAGE = "unknown page"
UNKNOWN_ENTITY = "unknown entity"


def _render_nav(label: str, entries: list[dict], note: str) -> str:
    """A `links`/`backlinks` section of `page_text`'s head — `entries` are the SERVICE-SHAPED
    `{path, title}` list `BrainService.read_page` already returned
    (ACL-filtered, titles already `neutralize_fence`d by `_display_title`), rendered VERBATIM.
    Never re-derived from `body` text, never re-neutralized, never a second fence: the service
    decided existence-scoping and safety once, and this only lays the result out as text."""
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
        the whole block is wrapped in the UNTRUSTED-DATA fence with in-band tokens neutralized —
        the same discipline read_page gives bodies. A hostile title/snippet can neither close the
        fence nor read as an instruction to the agent.

        Also records the literal query text on `ctx.searched` — the one piece of structured
        bookkeeping a refusal's composed reason cites ("searched X, Y"), never asked of the model.

        `filters` is a plain passthrough to `BrainService.search` — `synthesize.py`'s agent-facing
        search tool is the caller that gives the model a `filters` argument (e.g.
        `{"entity": <id>}` once an id is known from a previous result). ENTITY-FIRST resolution is
        NOT done here; it lives in `BrainService._search`, so every client gets it rather than only
        `ask`, and this method is a thin renderer over whatever the service already returns."""
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
            # `entity` is a list — joined for display, same as any other multi-valued field this
            # renderer already flattens to one line.
            entity_text = ", ".join(h.get("entity") or ())
            # `type` joins the meta line too: `ANSWER_SYS` tells the agent to identify
            # "type: entity" pages, and a search hit is the FIRST place it sees a page, often
            # before any read_page call at all.
            meta = " · ".join(x for x in (h.get("type"), entity_text, h.get("as_of")) if x)
            lines.append(f"- {h['path']}\n  {h.get('title', '')} ({meta})"
                         + (f" [{'; '.join(flags)}]" if flags else "")
                         + f"\n  {(h.get('snippet') or '')[:200]}")
        return fence("\n".join(lines))

    def page_text(self, path: str, ctx=None) -> str:
        """One page as the agent reads it: trust signals first, body already fenced UNTRUSTED-DATA
        by the service. Page-derived title/entity in the head are neutralized so a hostile title
        cannot forge a fence delimiter; the body arrives fenced from read_page, so ACL and
        body-fencing are decided in exactly one place (BrainService).

        The head also carries `type`/`status` and the `links`/`backlinks` navigation surface
        `read_page` serves — OUTSIDE the fence, before the body, exactly the way title/entity sit.
        `ANSWER_SYS` tells the agent to identify "type: entity" pages and follow one hop of
        links/backlinks, and an agent instructed to walk a graph it cannot see will not walk
        it. `links`/`backlinks` are
        the service's already-ACL-filtered, already-neutralized `{path, title}` entries, used
        verbatim (never re-derived from `body`, never fenced a second time) — `type`/`status` are
        page-contract frontmatter like `title`, so they are neutralized here the same way."""
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
        """One entity's territory as the agent reads it (`ask`'s third tool) — the
        service's `describe_entity` dict laid out as text: registry identity, its own page,
        its view reference, and the dated-first timeline with the service's own truncation
        note (never a silent cap).

        Every field arrives ALREADY decided by the service — ACL-scoped (out-of-scope and
        unknown are the byte-identical absence shape), titles/registry strings already
        neutralized (`_display_title`/`_neutralize_entity_record`) — so this renderer lays
        results out verbatim, the exact `_render_nav` posture: never re-derived, never
        re-neutralized, never fenced a second time.

        Bookkeeping mirrors `search_text`: the lookup text lands on `ctx.searched` (the
        refusal composer only ever quotes it if it is a substring of the asker's own
        question), and every page reference shown counts as SURFACED (`note_page`), the
        same standing search hits get — a citation to a timeline page is legitimate, and its
        quote is still checked verbatim against the page body by the verifier."""
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
        view = result.get("view")
        if view is not None:
            if ctx is not None:
                ctx.note_page(view["path"])
            lines.append(f"view: {view['path']} — {view['title']}"
                         f" (generated {view['generated_at'] or 'unknown'})")
        else:
            lines.append("view: (none)")
        lines.append(f"timeline: {result['timeline_note']}")
        for item in result["timeline"]:
            if ctx is not None:
                ctx.note_page(item["path"])
            lines.append(f"  - {item['as_of'] or '(undated)'} · {item['path']} — "
                         f"{item['title']} ({item['type']}, {item['status']})")
        return "\n".join(lines)

    # ── primitives for the verifier and the offline fake ────────────────────
    def get_page(self, path: str) -> dict | None:
        """Raw (UNFENCED) page for the verifier's verbatim-quote check and the fake's snippet
        builder — the shared, ACL-scoped read base on BrainService. Out-of-scope
        or nonexistent → None (existence itself is scoped)."""
        return self.service.fetch_page_raw(path)

    def known_entities(self) -> list[str]:
        """Entities with at least one page THIS client may see — existence is scoped too. Reuses
        the service's scoped-entity discovery."""
        return self.service.scoped_entities()
