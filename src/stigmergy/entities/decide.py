"""A steward's decisions on what the librarian proposed: confirm an identity, merge it into the
entity it turns out to be, decline it; confirm or decline a proposed spelling. Every decision is
ONE function over a checkout that edits pages, regenerates the registry and returns an `Outcome`
naming what changed — nothing here commits, pushes, records a ledger row or checks who is asking.
`apply` wraps a decision in the same commit discipline `entities.mint` gives a birth (preflight,
drift refusal, secrets scan, one commit, bounded rebase-and-retry), and the two doors —
`stigmergy-entities` in a steward's clone, `entities.remote.decide_via_clone` in a throwaway one
— both go through it, so the git history of a decision reads the same whoever made it.

The lifecycle is on the page and nowhere else (`approved_by` empty = proposed; `proposed_aliases`
= spellings waiting on a steward), so a decision is an edit to one entity page plus whatever that
edit implies for the rest of the wiki: a decline or a merge takes the proposed page away, and
every page the librarian anchored to it is re-anchored in the same commit — to the surviving
entity on a merge, to nothing on a decline — because a page whose `entity:` names an id the
registry no longer has is a page the gates would refuse to file today.
"""
import os
from dataclasses import dataclass, field

from stigmergy.entities import birth, clone, generator
from stigmergy.entities.errors import EntityError
from stigmergy.kernel.fsutil import write_text_atomic
from stigmergy.librarian import page as page_policy

APPROVE_ENTITY = "approve-entity"
MERGE_ENTITY = "merge-entity"
DECLINE_ENTITY = "decline-entity"
APPROVE_ALIAS = "approve-alias"
DECLINE_ALIAS = "decline-alias"
KINDS = (APPROVE_ENTITY, MERGE_ENTITY, DECLINE_ENTITY, APPROVE_ALIAS, DECLINE_ALIAS)

WIKI_RELDIR = "wiki"


@dataclass(frozen=True)
class Outcome:
    """What one decision did to the checkout — the facts a commit message, a ledger row and a
    human's confirmation line are all built from."""
    kind: str
    entity_id: str
    into: str = ""
    alias: str = ""
    changed_paths: tuple[str, ...] = ()
    reanchored: tuple[str, ...] = ()
    subject: str = ""
    summary: str = ""
    details: dict = field(default_factory=dict)


# ── the five decisions ────────────────────────────────────────────────────────────────────────
def approve_entity(repo: str, *, entity_id: str, approved_by: str, today: str) -> Outcome:
    """Confirm a proposed identity: `approved_by` becomes the steward's name."""
    entity = _require_proposed(repo, entity_id)
    approver = _approver(approved_by)
    front, tail = _front_and_tail(repo, entity.relpath)
    front = page_policy.with_scalar_field(front, generator.APPROVED_BY_KEY, approver)
    _write(repo, entity.relpath, front, tail, today)
    generator.regenerate(repo)
    return Outcome(APPROVE_ENTITY, entity.canonical_id,
                   changed_paths=(entity.relpath, generator.REGISTRY_RELPATH),
                   subject=f"feat(entity): confirm {entity.name}",
                   summary=f"{entity.name} ({entity.canonical_id}) is a confirmed "
                           f"{entity.entity_type}, approved by {approver}.")


def decline_entity(repo: str, *, entity_id: str, today: str) -> Outcome:
    """Decline a proposed identity: the page goes, and every page anchored to it loses the
    anchor. The ledger (written by the caller) is what keeps the librarian from proposing it
    again — the checkout forgets it on purpose."""
    entity = _require_proposed(repo, entity_id)
    reanchored = _reanchor(repo, entity.canonical_id, into="", today=today)
    os.remove(os.path.join(repo, *entity.relpath.split("/")))
    generator.regenerate(repo)
    return Outcome(DECLINE_ENTITY, entity.canonical_id,
                   changed_paths=(entity.relpath, generator.REGISTRY_RELPATH, *reanchored),
                   reanchored=tuple(reanchored),
                   subject=f"chore(entity): decline {entity.name}",
                   summary=f"{entity.name} ({entity.canonical_id}) was declined: its page is "
                           f"removed and {_pages(len(reanchored))} anchored to it now carry no "
                           f"entity anchor.")


def merge_entity(repo: str, *, entity_id: str, into: str, approved_by: str, today: str) -> Outcome:
    """A proposed identity turns out to be a registered one: the proposal's name and every
    spelling it carried become confirmed aliases of `into`, its page goes, and every page anchored
    to it is re-anchored to `into`."""
    entity = _require_proposed(repo, entity_id)
    approver = _approver(approved_by)
    entities = _by_id(repo)
    target = entities.get(into)
    if target is None:
        raise EntityError(
            f"cannot merge {entity_id!r} into {into!r}: the registry has no entity with that id — "
            f"merge into a registered entity's own id (list_entities shows them)")
    if target.canonical_id == entity.canonical_id:
        raise EntityError(f"cannot merge {entity_id!r} into itself — confirm it or decline it")
    if target.proposed:
        raise EntityError(
            f"cannot merge {entity_id!r} into {into!r}: {target.name!r} is itself a proposal a "
            f"steward has not confirmed. Confirm {into!r} first, or merge both into the entity "
            f"they are")
    spellings = birth.clean_aliases(
        [entity.name, *entity.aliases, *entity.proposed_aliases, *target.aliases],
        name=target.name)
    # The survivor's registry, with the proposal gone: a spelling that resolves to a THIRD entity
    # is a collision the steward did not decide, and `generator.regenerate` would otherwise
    # publish a registry where one matcher key names two entities.
    others = [e for e in entities.values()
              if e.canonical_id not in (entity.canonical_id, target.canonical_id)]
    registry = generator.registry_of(others)
    for spelling in spellings:
        hit = registry.collision_id(spelling)
        if hit:
            raise EntityError(
                f"cannot merge {entity_id!r} into {into!r}: the spelling {spelling!r} already "
                f"resolves to the registered entity {hit!r}, so it cannot also be an alias of "
                f"{into!r}. Decline {entity_id!r} instead, or merge it into {hit!r}")
    reanchored = _reanchor(repo, entity.canonical_id, into=target.canonical_id, today=today)
    front, tail = _front_and_tail(repo, target.relpath)
    front = page_policy.with_list_field(front, "aliases", list(spellings))
    _write(repo, target.relpath, front, tail, today)
    os.remove(os.path.join(repo, *entity.relpath.split("/")))
    generator.regenerate(repo)
    return Outcome(MERGE_ENTITY, entity.canonical_id, into=target.canonical_id,
                   changed_paths=(entity.relpath, target.relpath, generator.REGISTRY_RELPATH,
                                  *reanchored),
                   reanchored=tuple(reanchored),
                   subject=f"feat(entity): merge {entity.name} into {target.name}",
                   summary=f"{entity.name} ({entity.canonical_id}) is {target.name} "
                           f"({target.canonical_id}): its name and spellings are now aliases of "
                           f"{target.name}, its page is removed and {_pages(len(reanchored))} "
                           f"anchored to it now anchor to {target.name}. Decided by {approver}.",
                   details={"aliases": list(spellings)})


def approve_alias(repo: str, *, entity_id: str, alias: str, approved_by: str, today: str) -> Outcome:
    """Confirm a proposed spelling: it moves from `proposed_aliases:` to `aliases:`."""
    entity, spelling = _require_proposed_alias(repo, entity_id, alias)
    approver = _approver(approved_by)
    front, tail = _front_and_tail(repo, entity.relpath)
    aliases = birth.clean_aliases([*entity.aliases, spelling], name=entity.name)
    front = page_policy.with_list_field(front, "aliases", list(aliases))
    front = page_policy.with_list_field(
        front, generator.PROPOSED_ALIASES_KEY,
        [a for a in entity.proposed_aliases if a != spelling])
    _write(repo, entity.relpath, front, tail, today)
    generator.regenerate(repo)
    return Outcome(APPROVE_ALIAS, entity.canonical_id, alias=spelling,
                   changed_paths=(entity.relpath, generator.REGISTRY_RELPATH),
                   subject=f"feat(entity): {entity.name} is also {spelling}",
                   summary=f"{spelling!r} is a confirmed alias of {entity.name} "
                           f"({entity.canonical_id}), approved by {approver}.")


def decline_alias(repo: str, *, entity_id: str, alias: str, today: str) -> Outcome:
    """Decline a proposed spelling: it leaves `proposed_aliases:` and nothing else changes."""
    entity, spelling = _require_proposed_alias(repo, entity_id, alias)
    front, tail = _front_and_tail(repo, entity.relpath)
    front = page_policy.with_list_field(
        front, generator.PROPOSED_ALIASES_KEY,
        [a for a in entity.proposed_aliases if a != spelling])
    _write(repo, entity.relpath, front, tail, today)
    generator.regenerate(repo)
    return Outcome(DECLINE_ALIAS, entity.canonical_id, alias=spelling,
                   changed_paths=(entity.relpath, generator.REGISTRY_RELPATH),
                   subject=f"chore(entity): {entity.name} is not {spelling}",
                   summary=f"{spelling!r} was declined as a spelling of {entity.name} "
                           f"({entity.canonical_id}).")


# ── the commit discipline both doors share ────────────────────────────────────────────────────
def apply(repo: str, *, action, branch: str, author: tuple[str, str], trailer: str = "",
          on_output=None) -> dict:
    """Run one decision in `repo` and land it as ONE pushed commit, or refuse with the checkout
    exactly as it was.

    The same order as a birth (`entities.mint.mint`): the branch, cleanliness and sync checks,
    the drift refusal (a decision regenerates the registry, and regenerating somebody else's drift
    inside a commit that says "confirm X" is `ensure_clean`'s argument applied to the derived
    file), then the decision, then gitleaks over the files the commit will carry — a merge writes
    a steward-typed spelling into a page — then the commit, rebased and re-derived on a race.
    `action` is `lambda repo: decide.approve_entity(repo, ...)` or any of its four siblings.
    """
    from stigmergy.entities import mint as mint_lib

    clone.ensure_on_branch(repo, branch, action="decide")
    clone.ensure_clean(repo, action="decide")
    clone.ensure_in_sync(repo, branch, action="decide")
    mint_lib.refuse_drift(repo, action="decide")
    before = clone.head(repo)
    try:
        outcome = action(repo)
        present = [p for p in outcome.changed_paths
                   if os.path.exists(os.path.join(repo, *p.split("/")))]
        mint_lib.refuse_secrets(repo, present, action="decide")
    except BaseException:
        # The decision edits tracked files in place and deletes some, so the rollback is git's:
        # unlike a birth, there is nothing untracked of the steward's own to protect —
        # `ensure_clean` refused anything uncommitted before the first edit.
        clone.restore_tracked(repo, before)
        raise
    landed = clone.commit_and_push(
        repo, branch=branch, message=commit_message(outcome, trailer=trailer), author=author,
        regenerate=lambda: generator.regenerate(repo).changed, on_retry=on_output)
    return {"kind": outcome.kind, "entity_id": outcome.entity_id, "into": outcome.into,
            "alias": outcome.alias, "changed_paths": list(outcome.changed_paths),
            "reanchored": list(outcome.reanchored), "summary": outcome.summary,
            "commit": landed, "steward": f"{author[0]} <{author[1]}>", "branch": branch,
            **outcome.details}


def commit_message(outcome: Outcome, *, trailer: str = "") -> str:
    """Conventional-commit shaped like a birth's, so the history reads in one dialect."""
    message = (f"{outcome.subject}\n\n{outcome.summary}\n\n"
               f"Registry regenerated from {generator.ENTITIES_RELDIR}/ in the same commit.\n")
    if trailer:
        message += f"\n{trailer}\n"
    return message


# ── helpers ───────────────────────────────────────────────────────────────────────────────────
def _by_id(repo: str) -> dict:
    return {e.canonical_id: e for e in generator.read_entity_pages(repo)}


def _require_proposed(repo: str, entity_id: str):
    entity = _by_id(repo).get(str(entity_id or "").strip())
    if entity is None:
        raise EntityError(
            f"the registry has no entity {entity_id!r} — nothing to decide. If it was proposed "
            f"a moment ago, another steward may have decided it first (review_queue shows what "
            f"is still open)")
    if not entity.proposed:
        raise EntityError(
            f"{entity.name!r} ({entity.canonical_id}) is a confirmed entity, not a proposal — "
            f"there is nothing to confirm or decline. A confirmed identity retires through "
            f"`superseded_by` on its page, never through this door")
    return entity


def _require_proposed_alias(repo: str, entity_id: str, alias: str):
    entity = _by_id(repo).get(str(entity_id or "").strip())
    if entity is None:
        raise EntityError(f"the registry has no entity {entity_id!r} — nothing to decide")
    spelling = " ".join(str(alias or "").split())
    if spelling not in entity.proposed_aliases:
        listed = ", ".join(repr(a) for a in entity.proposed_aliases) or "none"
        raise EntityError(
            f"{spelling!r} is not a proposed spelling of {entity.name!r} ({entity.canonical_id}) "
            f"— its proposed aliases are: {listed}. If it was proposed a moment ago, another "
            f"steward may have decided it first")
    return entity, spelling


def _approver(approved_by: str) -> str:
    approver = " ".join(str(approved_by or "").split())
    if not approver:
        raise EntityError("a decision needs a non-empty approver — `approved_by` is the one field "
                          "that says who confirmed this identity")
    birth._refuse_control_characters(
        approver, subject="approved_by",
        consequence="it is the one field that says who confirmed this identity, read by every "
                    "person who opens the page")
    return approver


def _front_and_tail(repo: str, relpath: str) -> tuple[list[str], str]:
    with open(os.path.join(repo, *relpath.split("/")), encoding="utf-8") as f:
        text = f.read()
    try:
        return page_policy.front_and_tail(text)
    except ValueError as ex:
        raise EntityError(f"{relpath} has no frontmatter block, so its lifecycle cannot be "
                          f"edited — repair the page first") from ex


def _write(repo: str, relpath: str, front: list[str], tail: str, today: str) -> None:
    # `updated:` is a bare date on every page the template and the stamp write, so it stays one
    # here rather than going through the quoting scalar writer.
    start, _raw = page_policy.top_level_key_line(front, "updated")
    line = f"updated: {str(today).strip()}"
    if start < 0:
        front = [*front, line]
    else:
        _start, end = page_policy.top_level_key_span(front, "updated")
        front = front[:start] + [line] + front[end:]
    write_text_atomic(os.path.join(repo, *relpath.split("/")), page_policy.rebuild(front, tail))


def _reanchor(repo: str, entity_id: str, *, into: str, today: str) -> list[str]:
    """Every `wiki/**` page whose `entity:` names `entity_id`, rewritten: the id replaced by
    `into`, or dropped when `into` is empty. Returns the repo-relative paths rewritten."""
    changed = []
    wiki = os.path.join(repo, WIKI_RELDIR)
    entities_dir = os.path.join(repo, *generator.ENTITIES_RELDIR.split("/"))
    for dirpath, _dirs, files in os.walk(wiki):
        if os.path.abspath(dirpath).startswith(os.path.abspath(entities_dir)):
            continue
        for filename in sorted(files):
            if not filename.endswith(".md"):
                continue
            full = os.path.join(dirpath, filename)
            with open(full, encoding="utf-8") as f:
                text = f.read()
            try:
                front, tail = page_policy.front_and_tail(text)
            except ValueError:
                continue
            anchors = page_policy.list_field_values(front, "entity")
            if entity_id not in anchors:
                continue
            kept = []
            for anchor in anchors:
                replacement = into if anchor == entity_id else anchor
                if replacement and replacement not in kept:
                    kept.append(replacement)
            front = page_policy.with_list_field(front, "entity", kept)
            relpath = os.path.relpath(full, repo).replace(os.sep, "/")
            _write(repo, relpath, front, tail, today)
            changed.append(relpath)
    return changed


def _pages(count: int) -> str:
    return "1 page" if count == 1 else f"{count} pages"
