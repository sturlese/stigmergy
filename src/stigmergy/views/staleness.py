"""views.staleness — the READ-ONLY half of view regeneration. Staleness has one definition, and
it lives here: `skeleton.member_hash` over the current member set vs the view's recorded
`member_hash:` frontmatter.

This module must not import `views.regenerate` (or `writer`/`synthesis`): `gardener.checks`
imports THIS module, and `regenerate` module-level-imports the git write stack the gardener must
never load. Pinned, direct and transitive, in `tests/test_architecture.py`. `regenerate.py`
re-exports every name below.
"""
import os
import re

from stigmergy.index import corpus
from stigmergy.views import skeleton
from stigmergy.views.errors import ViewError

VIEWS_RELDIR = "views"

# `entity_id` reaches a filesystem path — asserted at the one choke point every view path is
# built through, so an id carrying a separator or `..` segment can never escape `views/`.
_ENTITY_ID_RE = re.compile(r"[a-z0-9-]+")


def view_relpath(entity_id: str) -> str:
    if not _ENTITY_ID_RE.fullmatch(entity_id):
        raise ViewError(
            f"refusing to build a view path from entity id {entity_id!r} — entity ids must be "
            "lowercase letters, digits and hyphens only")
    return f"{VIEWS_RELDIR}/{entity_id}.md"


def view_path(repo: str, entity_id: str) -> str:
    """The view's OS path, from its relpath."""
    return os.path.join(repo, *view_relpath(entity_id).split("/"))


def existing_member_hash(repo: str, entity_id: str) -> str | None:
    """The view's recorded `member_hash:` frontmatter field, or `None` when no view exists
    yet."""
    path = view_path(repo, entity_id)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        fm, _ = corpus.split_frontmatter(f.read())
    value = fm.get("member_hash")
    return str(value) if value else None


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
            for stem in (name[:-3],) if _ENTITY_ID_RE.fullmatch(stem)}


def list_stale_entities(repo: str, *, rows=None) -> list[str]:
    """`--stale`'s population: entities with an EXISTING view whose member set no longer
    matches. Also `gardener.checks.check_stale_views`'s population, reused verbatim so the two
    can never disagree about what "stale" means.

    The repo is parsed ONCE for the whole sweep and handed down to `skeleton.members_of`: a parse
    per entity is O(views x corpus). `rows` lets a caller that already holds a parse pass it in;
    the parser itself is deliberately NOT memoized, because a stale cache under a writer is worse
    than a re-parse."""
    rows = corpus.load_pages(repo) if rows is None else rows
    out = []
    for entity_id in sorted(existing_view_ids(repo)):
        members = skeleton.members_of(repo, entity_id, rows=rows)
        h = skeleton.member_hash(members) if members else None
        if h != existing_member_hash(repo, entity_id):
            out.append(entity_id)
    return out


def list_all_anchored_entities(repo: str) -> list[str]:
    """`--all`'s population: every entity with at least one anchored page. Also
    `gardener.checks.check_dead_vocabulary`'s population, reused verbatim."""
    return sorted(skeleton.all_anchored_entity_ids(repo))
