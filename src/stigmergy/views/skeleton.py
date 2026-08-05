"""views.skeleton — the deterministic half of a view: pure code, no LLM.

Reads the repo directly, through the SAME frontmatter/wikilink parser the index build uses
(`stigmergy.index.corpus`) — never the index itself, because a disposable cache must not be a
generator's input. One parser, so a view's member set can never drift from what a rebuild would
compute for the same commit.
"""
import hashlib
from dataclasses import dataclass
from pathlib import Path

from stigmergy.index import corpus
from stigmergy.kernel.acl import visible_to_view

# Both member zones: `wiki/**` and `sources/**`. Deliberately excludes the third zone
# `index.corpus.ZONES` carries, `views` itself — a view declares `entity: [<id>]` too, and if it
# counted as its own member the staleness hash would change on every write and never converge:
# a self-referential generator that can never report "unchanged".
MEMBER_ZONES = ("wiki", "sources")

# Section caps. No cap is ever silent: a capped section renders the same "showing N of M" lead
# line as the timeline the moment a corpus exceeds it. 10 is the number the timeline's own
# rendered examples state; the rest are generous engineering choices — no real or fixture corpus
# comes close — held to the same no-silent-cap rule.
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


def _member_rows(repo: str) -> list[corpus.PageRow]:
    return [r for r in corpus.load_pages(repo) if r.zone in MEMBER_ZONES]


def members_of(repo: str, entity_id: str) -> list[Member]:
    """Every page whose `entity:` contains `entity_id`, read from the repo, sorted by path
    for determinism. Timeline order is a separate, presentation-only sort (`timeline_order`) —
    this is the one member set every other computation (the staleness hash, the ACL
    intersection, the synthesis prompt, the frontmatter `members:` count) is built from."""
    rows = [r for r in _member_rows(repo) if entity_id in r.entity]
    return [Member(path=r.path, title=r.title, type=r.type, as_of=r.as_of,
                   superseded_by=r.superseded_by, acl=r.acl, content_hash=r.content_hash)
            for r in sorted(rows, key=lambda r: r.path)]


def all_anchored_entity_ids(repo: str) -> set[str]:
    """Every entity id anchored by at least one page in `MEMBER_ZONES` — the `--all` population:
    every entity with at least one anchored page, whether or not it has a view yet."""
    out: set[str] = set()
    for r in _member_rows(repo):
        out.update(r.entity)
    return out


def entity_own_page(members: list[Member]) -> Member | None:
    """The entity's OWN page among its members (`type: entity`), if it has one. Governed entity
    birth (`stigmergy.entities`) mints exactly one such page per registered entity, self-anchored
    (`entity: [<id>]`) — so it is normally present. Used for the view's display title and as
    the backlink target; a `None` here (an entity that somehow has members but no entity page —
    not reachable through governed birth, kept defensive rather than assumed) degrades the
    view's title to the raw id and its Backlinks section to empty, never a crash."""
    for m in members:
        if m.type == "entity":
            return m
    return None


def member_hash(members: list[Member]) -> str:
    """Staleness hash: sha256 over (path, content_hash, superseded_by, acl) per member, sorted by
    path. Its whole purpose is to decide cheaply whether the member set changed at all, without
    reading a single body.

    `path` is the identifier — the repo-relative path IS the pure parser's stable handle for a
    page, and there is no separate file id. `superseded_by` and `acl` are hashed alongside the
    content hash deliberately: `content_hash` covers title+body only
    (`index.corpus.content_hash`), so a frontmatter-only edit — a page gaining `superseded_by`, or
    an ACL narrowing — would otherwise leave the hash unchanged and the view silently stale. Both
    fields are read downstream: the view's own `acl` is the intersection of its members', and the
    synthesis prompt marks superseded members as such. Strengthening the hash costs nothing and
    closes a real false negative; it does not change what "unchanged" means for a page whose
    title/body did not move.
    """
    key = "|".join(
        f"{m.path}:{m.content_hash}:{m.superseded_by}:{','.join(m.acl) if m.acl else ''}"
        for m in members)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def timeline_order(members: list[Member]) -> list[Member]:
    """Newest first — and the rendered lead line says so, rather than leaving a reader to infer
    the order. Members with no `as_of` (common for `entity` pages themselves) sort AFTER every
    dated member, in path order, rather than being dropped: losing a member from its own view's
    timeline because it lacks a date would undercount the section against the frontmatter's own
    `members:` total."""
    dated = sorted((m for m in members if m.as_of), key=lambda m: (m.as_of, m.path), reverse=True)
    undated = sorted((m for m in members if not m.as_of), key=lambda m: m.path)
    return dated + undated


def render_timeline(members: list[Member], *, cap: int = TIMELINE_CAP) -> str:
    """The Timeline section's markdown: a lead line stating the total and how much of it is
    shown, then one bullet per member shown."""
    ordered = timeline_order(members)
    if not ordered:
        return "No anchored pages."
    total = len(ordered)
    shown = ordered[:cap]
    if total > cap:
        lead = (f"{total} page(s) anchored to this entity, most recent first — showing the "
                f"{len(shown)} most recent, {total - len(shown)} older not shown:")
    else:
        lead = f"{total} page(s) anchored to this entity, most recent first — showing all {total}:"
    lines = [lead, ""]
    for m in shown:
        date = f"**{m.as_of}**" if m.as_of else "*(as_of not recorded)*"
        # Link by file STEM, never by title: every wikilink resolver in this codebase resolves
        # by stem (`index.corpus.by_stem_index`/`resolve_links`, and the contract linter's own
        # stem index), and a page's stem is its SLUGIFIED title — see
        # `librarian.processing._decision_stems` for the meeting flow's version. Title and stem
        # therefore diverge for any page whose title is not already slug-shaped, and linking by
        # `m.title` yields a dead link on every one of them. `Path(m.path).stem` is the same
        # convention `backlinks_of` below and every other wikilink writer here already use.
        lines.append(f"- {date} — [[{Path(m.path).stem}]] (`{m.path}`)")
    return "\n".join(lines)


def backlinks_of(repo: str, entity_page: Member | None, *,
                 view_acl: list[str] | None = None) -> list[corpus.PageRow]:
    """Pages whose wikilinks resolve to the entity's OWN page (not to any member), computed at
    generation time from a fresh repo parse rather than read back out of the index.

    Scans every indexed zone (`index.corpus.ZONES`, including `views/` itself), because a
    backlink source is not restricted to `MEMBER_ZONES` the way a member is — another entity's
    view, or any page anywhere in the corpus, may legitimately wikilink this entity's page.

    **Filtered to rows the view's own audience may read**: a backlink is a governed source
    OUTSIDE the member set, so `view_acl` (the members-only intersection, `kernel.acl.view_acl`)
    never sees it — without this filter, a backlinking page's title and path would render on an
    open view even when the backlink's own `acl` restricts it, exactly the existence/title leak
    the ACL rule promises against. `acl.visible_to_view` is the read gate; it is NEVER folded
    into the intersection computation itself — a backlink must never NARROW `view_acl`, only be
    excluded from THIS list. `view_acl` defaults to `None` (open): the fail-CLOSED default when a
    caller forgets to pass the real value is to show only equally-open backlinks, never to widen
    exposure by omission.

    The match is `entity_page.path in r.links` — `corpus.load_pages` stores each row's outbound
    wikilinks as RESOLVED repo-relative paths, so this is exact even on an ambiguous stem (one
    resolving to several pages stores every match), where a bare stem comparison would not be."""
    if entity_page is None:
        return []
    rows = corpus.load_pages(repo)
    return sorted((r for r in rows if entity_page.path in r.links and r.path != entity_page.path
                  and visible_to_view(r.acl, view_acl)),
                 key=lambda r: r.path)


def render_backlinks(rows: list[corpus.PageRow], *, entity_title: str,
                     cap: int = BACKLINKS_CAP) -> str:
    """The Backlinks section's markdown. `backlinks_of` sorts by PATH, never by recency, so the
    truncation note states that order literally — claiming "most recent" here, the way the
    timeline legitimately does, would describe an order this list does not have."""
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
        # Same reasoning as `render_timeline` above — link by file stem, the convention every
        # wikilink resolver in this codebase reads.
        lines.append(f"- [[{Path(r.path).stem}]] (`{r.path}`)")
    return "\n".join(lines)
