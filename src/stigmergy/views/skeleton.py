"""views.skeleton — the deterministic half of a view: pure code, no LLM.

Reads the repo directly through the SAME parser the index build uses (`stigmergy.index.corpus`) —
never the index itself: a disposable cache must not be a generator's input.
"""
import hashlib
from dataclasses import dataclass
from pathlib import Path

from stigmergy.index import corpus
from stigmergy.kernel.acl import flows_into

# Deliberately excludes the `views` zone itself: a view declares `entity: [<id>]` too, and
# counting it as its own member would change the staleness hash on every write — a
# self-referential generator that can never report "unchanged".
MEMBER_ZONES = ("wiki", "sources")

# Section caps — never silent: a capped section renders a "showing N of M" lead line.
TIMELINE_CAP = 10
BACKLINKS_CAP = 20


@dataclass(frozen=True)
class Member:
    """One page anchored to the entity, as the skeleton needs it."""
    path: str
    title: str
    type: str
    as_of: str
    superseded_by: str
    acl: list[str] | None
    content_hash: str


def _member_rows(repo: str, rows=None) -> list[corpus.PageRow]:
    return [r for r in (corpus.load_pages(repo) if rows is None else rows)
            if r.zone in MEMBER_ZONES]


def members_of(repo: str, entity_id: str, *, rows=None) -> list[Member]:
    """Every page whose `entity:` contains `entity_id` and that may be rendered onto an OPEN page,
    sorted by path — the one member set every other computation (staleness hash, synthesis
    prompt, `members:` count) is built from.

    **A view carries no audience of its own**, so the filter is `flows_into(member.acl, None)`:
    open members only. Before that, a view's
    audience was the INTERSECTION of its members' — which never widened, correctly, but
    COLLAPSED: one leadership-only note anchored to a popular entity made that entity's view
    vanish for everyone else, and the timeline still named every member's path and title on the
    way. The finance reader now finds the finance notes about the entity through search and
    `describe_entity`, per reader, which is where a per-reader answer belongs; the view is the
    open rollup and says so by having nothing to say about them.

    Filtered HERE rather than at each consumer, because every one of them — the render, the
    staleness hashes, the synthesis agent's own readable set — has to agree about what a member
    is. A member excluded here is excluded from all three at once.

    `rows` lets a caller sweeping many entities hand in ONE `corpus.load_pages` parse instead of
    paying for a fresh one per entity; `None` parses the repo here, as every single-entity caller
    wants."""
    rows = [r for r in _member_rows(repo, rows)
            if entity_id in r.entity and flows_into(r.acl, None)]
    return [Member(path=r.path, title=r.title, type=r.type, as_of=r.as_of,
                   superseded_by=r.superseded_by, acl=r.acl, content_hash=r.content_hash)
            for r in sorted(rows, key=lambda r: r.path)]


def all_anchored_entity_ids(repo: str, *, rows=None) -> set[str]:
    """Every entity id anchored by at least one page in `MEMBER_ZONES` — the `--all`
    population.

    `rows` is `members_of`'s, for the same reason: a sweep that has already parsed the repo hands
    the parse in rather than paying for a second one."""
    out: set[str] = set()
    for r in _member_rows(repo, rows):
        out.update(r.entity)
    return out


def entity_own_page(members: list[Member]) -> Member | None:
    """The entity's OWN page among its members (`type: entity`) — the view's display title and
    backlink target. `None` (not reachable through governed birth, kept defensive) degrades the
    title to the raw id and Backlinks to empty, never a crash."""
    for m in members:
        if m.type == "entity":
            return m
    return None


def member_hash(members: list[Member]) -> str:
    """The MEMBER SET's staleness signal — one of the two a view carries (`backlink_hash` is the
    other, and the pair is compared together): sha256 over (path, content_hash, type, as_of,
    superseded_by, acl) per member, sorted by path — decides cheaply whether the member set
    changed, without reading a body.

    Every field beyond `content_hash` is deliberate: `content_hash` covers title+body only,
    so a frontmatter-only edit (`superseded_by`, an ACL narrowing, an `as_of` or `type` move —
    each rendered or read downstream) would otherwise leave the view silently stale forever, with
    only `--force` to recover it.
    """
    key = "|".join(
        f"{m.path}:{m.content_hash}:{m.type}:{m.as_of}:{m.superseded_by}:"
        f"{','.join(m.acl) if m.acl else ''}"
        for m in members)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def timeline_order(members: list[Member]) -> list[Member]:
    """Newest first. Undated members sort AFTER every dated one, in path order, never dropped —
    dropping one would undercount the section against the frontmatter's `members:` total."""
    dated = sorted((m for m in members if m.as_of), key=lambda m: (m.as_of, m.path), reverse=True)
    undated = sorted((m for m in members if not m.as_of), key=lambda m: m.path)
    return dated + undated


def timeline_shown(n_members: int, cap: int = TIMELINE_CAP) -> int:
    """How many timeline entries a view of `n_members` actually renders. One answer, so
    `render_timeline`'s slice and the count a caller reports cannot disagree."""
    return min(n_members, cap)


def render_timeline(members: list[Member], *, cap: int = TIMELINE_CAP) -> str:
    """The Timeline section's markdown: a lead line stating total and shown, one bullet per
    member shown."""
    ordered = timeline_order(members)
    if not ordered:
        return "No anchored pages."
    total = len(ordered)
    shown = ordered[:timeline_shown(total, cap)]
    if total > cap:
        lead = (f"{total} page(s) anchored to this entity, most recent first — showing the "
                f"{len(shown)} most recent, {total - len(shown)} older not shown:")
    else:
        lead = f"{total} page(s) anchored to this entity, most recent first — showing all {total}:"
    lines = [lead, ""]
    for m in shown:
        date = f"**{m.as_of}**" if m.as_of else "*(as_of not recorded)*"
        # Link by file STEM, never by title: every wikilink resolver here resolves by stem, and
        # title and stem diverge for any page whose title is not slug-shaped — a title link is a
        # dead link on every one of them.
        lines.append(f"- {date} — [[{Path(m.path).stem}]] (`{m.path}`)")
    return "\n".join(lines)


def backlinks_of(repo: str, entity_page: Member | None, *,
                 view_acl: list[str] | None = None,
                 exclude_path: str = "", rows=None) -> list[corpus.PageRow]:
    """Pages whose wikilinks resolve to the entity's OWN page. Scans every indexed zone (`views/`
    included — another entity's view may legitimately link here).

    A backlink is a governed source OUTSIDE the member set, so it passes the same gate a member
    does and never contributes to the page's own audience: a backlink is excluded from this list,
    never a reason to narrow anything. **Production always passes `None`** — a view is open
 — and the parameter stays because it is `flows_into`'s own argument and its truth
    table is worth exercising at other audiences rather than only at the one this caller uses. The
    `None` default is also the fail-closed one: it admits equally-open backlinks and nothing else.
    The match is against RESOLVED link paths, exact even on an ambiguous stem.

    `exclude_path` is the view being generated: it always links its own entity page and always
    passes the filter, so without the exclusion the rollup cites itself and the count runs one
    high.

    **`rows` serves the STALENESS SIGNAL only; the WRITE path must keep passing `None`.** The two
    are different moments with different needs. Writing a view has to see a view written earlier
    in the SAME pass — `views/` is an indexed zone, so that view is a real backlink source and a
    snapshot taken before the pass started cannot contain it. Deciding whether a view is stale
    happens once per entity CHECKED, which is the whole population, and paying a fresh corpus
    parse there would undo the single-parse argument that makes a fifteen-minute pass affordable.
    The consequence is bounded and converges: a backlink created by a view written earlier in the
    same pass is noticed on the NEXT pass, one interval later, rather than never.
    """
    if entity_page is None:
        return []
    rows = corpus.load_pages(repo) if rows is None else rows
    excluded = {entity_page.path, exclude_path} - {""}
    return sorted((r for r in rows if entity_page.path in r.links and r.path not in excluded
                  and flows_into(r.acl, view_acl)),
                 key=lambda r: r.path)


def backlink_hash(rows: list[corpus.PageRow]) -> str:
    """The SECOND staleness signal: sha256 over (path, title) per rendered backlink, in
    `backlinks_of`'s own path order.

    Beside `member_hash` and never folded into it — that name says what it hashes, and a backlink
    is not a member. What it covers is the POST-GATE set: `backlinks_of` has already applied
    `flows_into`, so a source whose `acl:` is narrowed to an audience the view does not have
    drops OUT of these rows and the hash moves, which is what makes the narrowing a regeneration.
    Hashing the pre-gate candidates instead would fire on any ACL edit anywhere, including the
    ones that change nothing on the page.

    (path, title) and deliberately nothing body-shaped. A view is itself an indexed backlink
    source and its body carries its own regeneration date, so a content-sensitive key here would
    make two views that cite each other regenerate each other every pass, forever. Path and title
    are also exactly the disclosure this system's existence rule is about: naming a forbidden
    page needs no body.
    """
    key = "|".join(f"{r.path}:{r.title}" for r in rows)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def render_backlinks(rows: list[corpus.PageRow], *, entity_title: str,
                     cap: int = BACKLINKS_CAP) -> str:
    """The Backlinks section's markdown. `backlinks_of` sorts by PATH, so the truncation note
    states that order — never "most recent", an order this list does not have."""
    if not rows:
        return "Nothing links to this entity's own page yet."
    total = len(rows)
    shown = rows[:cap]
    if total > cap:
        lead = (f"{total} page(s) link to {entity_title}'s own entity page — showing the first "
                f"{len(shown)} (sorted by path), {total - len(shown)} more not shown:")
    else:
        lead = f"{total} page(s) link to {entity_title}'s own entity page — showing all {total}:"
    lines = [lead, ""]
    for r in shown:
        # Link by file stem — same reasoning as `render_timeline`.
        lines.append(f"- [[{Path(r.path).stem}]] (`{r.path}`)")
    return "\n".join(lines)
