"""views.staleness — the READ-ONLY half of view regeneration: which entities have an existing
view, whether it is stale, and which entities are anchored at all. Staleness has one definition,
and it lives here: `skeleton.member_hash` over the entity's current member set, compared against
the view's own recorded `member_hash:` frontmatter field.

**This module holds NO write path and imports NEITHER `writer` NOR `synthesis`.** That is
load-bearing rather than tidiness. `gardener.checks` needs these staleness reads for its
stale-view and dead-vocabulary checks, and `regenerate.py` module-level-imports `views.writer` —
the commit-and-push path, and through it `librarian.gitcmd`/`.githubapp` and App-credential
minting. A gardener that imported `regenerate` would therefore load the whole git write stack
into every gardener process, with `writer.commit_and_push` one attribute access away inside that
namespace, while an AST-level check of `checks.py`'s own imports saw nothing at all. Both halves
of that are pinned: `tests/test_architecture.py::test_gardener_never_touches_git_plumbing` is the
direct-import half, and `test_gardener_transitive_views_reach_is_a_named_declared_exception`
beside it is the transitive half a source-level check cannot see. `gardener.checks` imports THIS
module, never `regenerate`.

The dependencies are exactly what reading a staleness signal needs: `os`, `re`, `views.skeleton`
(member sets, pure code over the repo checkout) and `index.corpus` (frontmatter parsing, the same
pure parser the index build itself uses). `regenerate.py` imports and re-exports every name
below, so `views/cli.py` and the librarian worker reach them under either module.
"""
import os
import re

from stigmergy.index import corpus
from stigmergy.views import skeleton
from stigmergy.views.errors import ViewError

VIEWS_RELDIR = "views"

# `entity_id` reaches a filesystem path, so it gets a shape assertion at the one choke point
# every view path is built through: an id carrying a path separator or a `..` segment would
# otherwise escape `views/`. Entity ids are a governed value, and this pattern is the shape they
# already have in practice — `stigmergy.entities` mints lowercase-hyphenated ids exclusively — so
# asserting it here costs nothing and needs no caller to remember it.
_ENTITY_ID_RE = re.compile(r"[a-z0-9-]+")


def view_relpath(entity_id: str) -> str:
    if not _ENTITY_ID_RE.fullmatch(entity_id):
        raise ViewError(
            f"refusing to build a view path from entity id {entity_id!r} — entity ids must be "
            "lowercase letters, digits and hyphens only")
    return f"{VIEWS_RELDIR}/{entity_id}.md"


def view_path(repo: str, entity_id: str) -> str:
    """The view's OS path, from its relpath. Public rather than private because `regenerate.py`
    is a genuine cross-module consumer of it."""
    return os.path.join(repo, *view_relpath(entity_id).split("/"))


def existing_member_hash(repo: str, entity_id: str) -> str | None:
    """The view's OWN recorded `member_hash:` frontmatter field, or `None` when no view
    exists yet — public for the same reason as `view_path` above."""
    path = view_path(repo, entity_id)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        fm, _ = corpus.split_frontmatter(f.read())
    value = fm.get("member_hash")
    return str(value) if value else None


def existing_view_ids(repo: str) -> set[str]:
    """The GENERATED views on disk. A stem that is not a well-formed entity id is not one of them.

    `views/` is an indexed corpus zone, so a hand-written page (`views/README.md`) may legitimately
    sit beside the generated files — and every id here goes on to `view_relpath`, whose assertion
    then refused it and took the caller down. That assertion guards a CALLER-supplied id from
    escaping `views/`; a name read back out of that directory cannot traverse anywhere, so the
    population is what needed narrowing, not the guard. Nothing legitimate is lost: `view_relpath`
    is the ONE place view files are named, so a view it would refuse to build was never written by
    this system.
    """
    d = os.path.join(repo, VIEWS_RELDIR)
    if not os.path.isdir(d):
        return set()
    return {stem for name in os.listdir(d) if name.endswith(".md")
            for stem in (name[:-3],) if _ENTITY_ID_RE.fullmatch(stem)}


def list_stale_entities(repo: str) -> list[str]:
    """`--stale`'s population: entities with an EXISTING view whose member set no longer matches.
    Also the population `gardener.checks.check_stale_views` reports on, reused verbatim so the
    two can never disagree about what "stale" means."""
    out = []
    for entity_id in sorted(existing_view_ids(repo)):
        members = skeleton.members_of(repo, entity_id)
        h = skeleton.member_hash(members) if members else None
        if h != existing_member_hash(repo, entity_id):
            out.append(entity_id)
    return out


def list_all_anchored_entities(repo: str) -> list[str]:
    """`--all`'s population: every entity with at least one anchored page. Also the population
    `gardener.checks.check_dead_vocabulary` reports on, reused verbatim, never re-derived."""
    return sorted(skeleton.all_anchored_entity_ids(repo))
