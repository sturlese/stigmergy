"""views.skeleton — the deterministic half of a view: pure code, no LLM.

Reads the repo directly through the SAME parser the index build uses (`stigmergy.index.corpus`) —
never the index itself: a disposable cache must not be a generator's input.
"""
import hashlib
from dataclasses import dataclass
from pathlib import Path

from stigmergy.index import corpus
from stigmergy.kernel.acl import visible_to_view

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
    """Every page whose `entity:` contains `entity_id`, sorted by path — the one member set
    every other computation (staleness hash, ACL intersection, synthesis prompt, `members:`
    count) is built from.

    `rows` lets a caller sweeping many entities hand in ONE `corpus.load_pages` parse instead of
    paying for a fresh one per entity; `None` parses the repo here, as every single-entity caller
    wants."""
    rows = [r for r in _member_rows(repo, rows) if entity_id in r.entity]
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
    """Staleness hash: sha256 over (path, content_hash, type, as_of, superseded_by, acl) per
    member, sorted by path — decides cheaply whether the member set changed, without reading a
    body. Every field beyond `content_hash` is deliberate: `content_hash` covers title+body only,
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
                 exclude_path: str = "") -> list[corpus.PageRow]:
    """Pages whose wikilinks resolve to the entity's OWN page, from a fresh repo parse. Scans
    every indexed zone (`views/` included — another entity's view may legitimately link here).

    A backlink is a governed source OUTSIDE the member set: `visible_to_view` gates whether it
    renders, and is NEVER folded into the intersection itself — a backlink must never NARROW
    `view_acl`, only be excluded from this list. The `None` default fail-closes to showing only
    equally-open backlinks. The match is against RESOLVED link paths, exact even on an ambiguous
    stem.

    `exclude_path` is the view being generated: it always links its own entity page and always
    passes the filter, so without the exclusion the rollup cites itself and the count runs one
    high.
    """
    if entity_page is None:
        return []
    rows = corpus.load_pages(repo)
    excluded = {entity_page.path, exclude_path} - {""}
    return sorted((r for r in rows if entity_page.path in r.links and r.path not in excluded
                  and visible_to_view(r.acl, view_acl)),
                 key=lambda r: r.path)


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
