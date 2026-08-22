"""views.staleness — the READ-ONLY half of view regeneration. Staleness has one definition, and
it lives here: TWO signals, compared as a pair against the ones the view recorded on itself —
`skeleton.member_hash` over the current member set, and `skeleton.backlink_hash` over the
backlinks it would render now. One name each, because `member_hash` covers the member set and
nothing else, and a view's Backlinks section is fed by pages that are not members (#85).

This module must not import `views.regenerate` (or `writer`/`synthesis`): `gardener.checks`
imports THIS module, and `regenerate` module-level-imports the git write stack the gardener must
never load. Pinned, direct and transitive, in `tests/test_architecture.py`. `regenerate.py`
re-exports every name below.
"""
import os
import re
from dataclasses import dataclass

from stigmergy.index import corpus
from stigmergy.kernel.acl import view_acl
from stigmergy.views import skeleton
from stigmergy.views.errors import ViewError

VIEWS_RELDIR = "views"

# `entity_id` reaches a filesystem path — asserted at the one choke point every view path is
# built through, so an id carrying a separator or `..` segment can never escape `views/`.
_ENTITY_ID_RE = re.compile(r"[a-z0-9-]+")

# The refusal's own wording, shared by the predicate's callers so an id is described the same way
# whether a path was being built from it or a population was being screened for it.
ENTITY_ID_RULE = "entity ids must be lowercase letters, digits and hyphens only"


def is_usable_view_id(entity_id: str) -> bool:
    """Can a view file be named from this id at all? The ONE spelling of that question.

    Two kinds of caller need it and they need opposite things from it: `view_relpath` below raises
    (it is the choke point a path must not escape), while a caller holding a POPULATION — a
    directory listing, or an `entity:` value read off a page — needs to screen an id it did not
    author without taking the raise. Both go through this predicate rather than through a second
    copy of the regex, so "usable id" cannot come to mean two things.
    """
    return bool(_ENTITY_ID_RE.fullmatch(entity_id))


def view_relpath(entity_id: str) -> str:
    if not is_usable_view_id(entity_id):
        raise ViewError(
            f"refusing to build a view path from entity id {entity_id!r} — {ENTITY_ID_RULE}")
    return f"{VIEWS_RELDIR}/{entity_id}.md"


def view_path(repo: str, entity_id: str) -> str:
    """The view's OS path, from its relpath."""
    return os.path.join(repo, *view_relpath(entity_id).split("/"))


@dataclass(frozen=True)
class ViewSignals:
    """A view's staleness signals, always read and computed as a PAIR — one per feed the page
    renders: `member_hash` for the member set, `backlink_hash` for the backlinks.

    The two `None`s mean different things and neither means "matches nothing". A `member_hash` of
    `None` is "there is no view here, or no member set to build one from". A `backlink_hash` of
    `None` is "a view written before that field existed", which `view_is_current` reads as STALE:
    every view on a real deployment predates the field, and the one pass that regenerates each of
    them is precisely what computes the missing signal.
    """
    member_hash: str | None = None
    backlink_hash: str | None = None


def _frontmatter_str(fm: dict, key: str) -> str | None:
    value = fm.get(key)
    return str(value) if value else None


def existing_signals(repo: str, entity_id: str) -> ViewSignals:
    """Both signals the view recorded on itself, off ONE read of its frontmatter — a pass opens
    one file per entity and this keeps it at one. Absent view or absent field is `None`."""
    path = view_path(repo, entity_id)
    if not os.path.exists(path):
        return ViewSignals()
    with open(path, encoding="utf-8") as f:
        fm, _ = corpus.split_frontmatter(f.read())
    return ViewSignals(member_hash=_frontmatter_str(fm, "member_hash"),
                       backlink_hash=_frontmatter_str(fm, "backlink_hash"))


def existing_member_hash(repo: str, entity_id: str) -> str | None:
    """The view's recorded `member_hash:` frontmatter field, or `None` when no view exists yet.

    Kept as its own name because it is read as an EXISTENCE probe — "is there a view here at all"
    — where the answer decides between a refusal and a removal, never between fresh and stale.
    The staleness question is `view_is_current`'s, over the pair."""
    return existing_signals(repo, entity_id).member_hash


def current_signals(repo: str, entity_id: str, members: list[skeleton.Member], *,
                    rows=None) -> ViewSignals:
    """What a view generated from `members` RIGHT NOW would record — the ONE computation of the
    pair, so `list_stale_entities` (which is also the gardener's population) and
    `regenerate.regenerate_entity` cannot come to disagree about what "stale" means.

    The backlink half is computed exactly as the section is RENDERED: the same audience
    (`view_acl` over the members, the value the page's own `acl:` is), the same self-exclusion,
    and the same `visible_to_view` gate. That is what makes a narrowed or deleted source register
    — it simply stops being one of these rows.

    An empty member set is `ViewSignals()`: no view is generated from nothing, which never equals
    a stored pair, and that is what puts an orphaned view into the removal population.

    `rows` is the batch's shared corpus parse. It reaches `backlinks_of` HERE and must not on the
    write path — see that function for the one-interval lag it buys and why the trade is the
    right way round.
    """
    if not members:
        return ViewSignals()
    audience = view_acl([m.acl for m in members])
    backlinks = skeleton.backlinks_of(repo, skeleton.entity_own_page(members),
                                      view_acl=audience,
                                      exclude_path=view_relpath(entity_id), rows=rows)
    return ViewSignals(member_hash=skeleton.member_hash(members),
                       backlink_hash=skeleton.backlink_hash(backlinks))


def view_is_current(*, existing: ViewSignals, current: ViewSignals) -> bool:
    """Is the view on disk what the corpus would produce right now? BOTH signals must match.

    Every early return is a "no", and each one is a different fact: no view (or nothing to build
    one from), and a view predating `backlink_hash:`. The last one is stated rather than left to
    fall out of the comparison, because `None == None` would have quietly reported `unchanged`
    and shipped #85's leak forever — a view whose backlink source was narrowed after generation
    would have kept citing it, title and path, and every pass would have called that current.
    """
    if existing.member_hash is None or current.member_hash is None:
        return False
    if existing.backlink_hash is None:
        return False
    return existing == current


def existing_view_ids(repo: str) -> set[str]:
    """The GENERATED views on disk. A stem that is not a well-formed entity id is excluded: a
    hand-written page (`views/README.md`) may sit beside the generated files, and every id here
    goes on to `view_relpath`, whose assertion would otherwise refuse it and take the caller
    down. Nothing legitimate is lost — a view `view_relpath` would refuse was never written by
    this system.
    """
    d = os.path.join(repo, VIEWS_RELDIR)
    if not os.path.isdir(d):
        return set()
    return {stem for name in os.listdir(d) if name.endswith(".md")
            for stem in (name[:-3],) if is_usable_view_id(stem)}


def list_stale_entities(repo: str, *, rows=None) -> list[str]:
    """`--stale`'s population: entities with an EXISTING view that is no longer what the corpus
    would produce — its member set moved, or the backlinks it cites did. Also
    `gardener.checks.check_stale_views`'s population, reused verbatim so the detector and the
    actor can never disagree about what "stale" means, and the reason the gardener sees the
    backlink half for free.

    The repo is parsed ONCE for the whole sweep and handed down to `skeleton.members_of` and
    `current_signals`: a parse per entity is O(views x corpus). `rows` lets a caller that already
    holds a parse pass it in; the parser itself is deliberately NOT memoized, because a stale
    cache under a writer is worse than a re-parse."""
    rows = corpus.load_pages(repo) if rows is None else rows
    out = []
    for entity_id in sorted(existing_view_ids(repo)):
        members = skeleton.members_of(repo, entity_id, rows=rows)
        if not view_is_current(existing=existing_signals(repo, entity_id),
                               current=current_signals(repo, entity_id, members, rows=rows)):
            out.append(entity_id)
    return out


def list_all_anchored_entities(repo: str, *, rows=None) -> list[str]:
    """`--all`'s population: every entity with at least one anchored page. Also
    `gardener.checks.check_dead_vocabulary`'s population, reused verbatim.

    `rows` is `list_stale_entities`'s, for the same reason — see `list_sweep_entities`, which is
    the caller that needs both populations off ONE parse."""
    return sorted(skeleton.all_anchored_entity_ids(repo, rows=rows))


def list_sweep_entities(repo: str, *, rows=None) -> list[str]:
    """`--sweep`'s population: the UNION of the two above, which is the only population that
    converges `views/` to the corpus.

    Neither half is a superset of the other, and that is the whole reason this function exists:

    - `list_all_anchored_entities` includes an entity that has never had a view (the case a
      newly-minted entity with one anchored page lands in), and MISSES an orphaned view whose
      members have all disappeared — that entity has no anchored pages left to be found by.
    - `list_stale_entities` catches those removals (an empty member set hashes to `None`, which
      never equals a stored hash), and MISSES every entity with no view yet, because it iterates
      the views on disk.

    So a sweep built on `--stale` alone — the obvious choice, and the one
    `gardener.checks.check_stale_views` reuses — would silently never CREATE a missing view.

    ONE parse serves both halves: a parse per entity is O(population x corpus), and the two
    populations overlap almost entirely.

    **An id `is_usable_view_id` would reject is NOT filtered out here**, and that asymmetry with
    `existing_view_ids` is deliberate. That half is a directory listing, where a foreign stem was
    never written by this system and means nothing. This half is `entity:` read off a page a human
    can hand edit, where an unusable id is somebody's typo for a real anchor: dropping it silently
    would converge the pass and leave whoever wrote it waiting forever for a rollup nobody is
    building.
    It travels into the population so `regenerate_entity` can refuse it BY NAME, once per pass.
    """
    rows = corpus.load_pages(repo) if rows is None else rows
    return sorted(set(list_all_anchored_entities(repo, rows=rows))
                  | set(list_stale_entities(repo, rows=rows)))
