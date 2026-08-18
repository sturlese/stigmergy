"""The registry generator: `ops/entity-registry.json` derived from `wiki/entities/*.md`.

The output is serialized through `kernel.registry.save_registry` and read back through
`load_registry` — nothing here writes JSON, which is also what makes `regenerate` idempotent (the
one writer sorts and pins separators). Scanned: `wiki/entities/` and nothing else — this module
governs entity BIRTH; a MERGE is a different decision. The canonical id is `slugify(title)`, a
contract: the pages carry no id field, so a free-form id would be a fact no regeneration could
recover — `birth.prepare` verifies the steward's `--id` against it and refuses a mismatch. A page
this cannot read is an ERROR, never a skip: a silently dropped page is a registry that quietly
stops resolving a name the graph anchored on.
"""
import logging
import os
from dataclasses import dataclass, field

from stigmergy.entities.errors import EntityError
from stigmergy.kernel import frontmatter as graph_pages
from stigmergy.kernel.normalize import normalize, slugify
from stigmergy.kernel.registry import Registry, index_entity, load_registry, save_registry

log = logging.getLogger(__name__)

# Repo-relative, slash-separated — git paths first, filesystem paths second.
ENTITIES_RELDIR = "wiki/entities"
REGISTRY_RELPATH = "ops/entity-registry.json"

# The only command this subsystem tells anyone to run — a message containing a command is an
# executable promise. This exact string is also written by hand in the knowledge repo's own
# `stigmergy_lint.py` (stdlib-only, cannot import it); the duplication is declared at both ends.
FIX_COMMAND = "stigmergy-entities regenerate"

# `entity_type` on the page -> `type` in the registry. The vocabulary is `ops/templates/entity.md`'s
# own comment (the human-facing source of truth); the linter does not enum-check the field, so this
# is the only enforcement. `birth.prepare` refuses anything outside it; the generator is LENIENT on
# read — one bad pre-existing page must not make the whole registry unregenerable. `project` is a
# governed identity like the rest (ADR 037 D3): an ongoing initiative earns birth-by-steward and a
# regenerated view, which is why it is an entity here and NOT a page type.
ENTITY_TYPES = ("person", "organization", "product", "tool", "repository", "place", "project")

# `load_registry`'s own default, mirrored rather than re-decided, so a page with no `entity_type`
# and a registry entry with no `type` describe the same entity after a round trip.
DEFAULT_ENTITY_TYPE = "organization"


def canonical_id_for(name: str) -> str:
    """The registry id a page titled `name` will always regenerate as.

    `slugify`, not `normalize`: `normalize` strips legal suffixes and is the ALIAS-matching key,
    while an id is a stable file-safe handle. Using the matcher for the key would give "Acme
    Corp" the id "acme" and make two genuinely different entities fight over one slot.
    """
    return slugify(str(name or "").strip())


@dataclass(frozen=True)
class PageEntity:
    """One entity page's identity claim, as the generator read it."""
    canonical_id: str
    name: str
    entity_type: str
    aliases: tuple[str, ...]
    relpath: str


def entities_dir(repo: str) -> str:
    return os.path.join(repo, *ENTITIES_RELDIR.split("/"))


def registry_path(repo: str) -> str:
    return os.path.join(repo, *REGISTRY_RELPATH.split("/"))


def _aliases_of(front: dict) -> tuple[str, ...]:
    """`aliases` as a tuple of clean strings. A scalar is accepted as a one-element list — the
    linter already flags it, and refusing here would let one lint error block every other
    entity's registration."""
    declared = front.get("aliases")
    if declared is None or declared == "":
        return ()
    values = declared if isinstance(declared, list) else [declared]
    return tuple(str(v).strip() for v in values if str(v).strip())


def read_entity_pages(repo: str) -> list[PageEntity]:
    """Every `wiki/entities/*.md`, as identity claims, sorted by id.

    Non-recursive on purpose: the folder is flat by convention, and a nested page is a different
    kind of thing nobody decided should mint an identity.
    """
    directory = entities_dir(repo)
    if not os.path.isdir(directory):
        return []
    out = []
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".md") or filename.startswith("."):
            continue
        relpath = f"{ENTITIES_RELDIR}/{filename}"
        path = os.path.join(directory, filename)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as ex:
            # `str(OSError)` carries the ABSOLUTE path, and MCP echoes this `EntityError`
            # verbatim — so the message keeps only the repo-relative `relpath`; the errno and the
            # path go to the log.
            log.error("entity page %r could not be read from %r", relpath, directory, exc_info=True)
            raise EntityError(
                f"{relpath} could not be read ({ex.__class__.__name__}) — the entity registry is "
                f"derived from these pages, so it cannot be regenerated without it. The details "
                f"are in the server log") from ex
        front, _ = graph_pages.split_frontmatter(text)
        name = str(front.get("title") or front.get("name") or "").strip()
        if not name:
            raise EntityError(
                f"{relpath} declares no `title`, so it names no entity — every page in "
                f"{ENTITIES_RELDIR}/ is an identity, and one the registry cannot name would "
                f"silently stop resolving. Give it a title (the page contract requires one "
                f"anyway) or move it out of this folder")
        canonical_id = canonical_id_for(name)
        if not canonical_id:
            raise EntityError(
                f"{relpath} has the title {name!r}, which produces no usable registry id — it is "
                f"all punctuation or non-Latin characters. Titles become ids by slug, so this one "
                f"cannot be registered as written")
        out.append(PageEntity(
            canonical_id=canonical_id, name=name,
            entity_type=str(front.get("entity_type") or DEFAULT_ENTITY_TYPE).strip()
            or DEFAULT_ENTITY_TYPE,
            aliases=_aliases_of(front), relpath=relpath))

    duplicates = _duplicate_ids(out)
    if duplicates:
        raise EntityError(
            "two entity pages produce the same registry id, so one would overwrite the other: "
            + "; ".join(duplicates)
            + ". Registry ids are the slug of the page title, so two titles that differ only by "
              "punctuation or case collide — rename one, or make them aliases of a single entity")
    ambiguous = _duplicate_match_keys(out)
    if ambiguous:
        raise EntityError(
            "two entity pages are titled the same thing as far as entity resolution is concerned, "
            "so every mention of that name would silently anchor to whichever one the registry "
            "happened to index last: " + "; ".join(ambiguous)
            + f". Ids are distinct (they are slugs) but `by_alias` is keyed by the MATCHER, which "
              f"folds case, accents, punctuation and legal suffixes — 'Acme' and 'Acme Corp.' are "
              f"one key. Rename one, or make it an alias of the other and delete its page, then "
              f"run `{FIX_COMMAND}`")
    return sorted(out, key=lambda e: e.canonical_id)


def _duplicate_ids(entities: list[PageEntity]) -> list[str]:
    seen: dict[str, PageEntity] = {}
    clashes = []
    for entity in entities:
        first = seen.get(entity.canonical_id)
        if first is None:
            seen[entity.canonical_id] = entity
        else:
            clashes.append(f"{first.relpath} and {entity.relpath} both produce "
                           f"{entity.canonical_id!r}")
    return clashes


def _duplicate_match_keys(entities: list[PageEntity]) -> list[str]:
    """Two pages whose TITLES resolve to one `by_alias` key — a different collapse than
    `_duplicate_ids`, and invisible in the file.

    `normalize` folds strictly more than `slugify` (legal suffixes), so `Acme` and `Acme Corp.`
    keep distinct ids while claiming one matching key: the registry LOOKS unambiguous and resolves
    that name to whichever page sorted last. Titles only — an ALIAS colliding with a name is
    refused at mint time by `birth._refuse_collisions`; refusing it here too would make one bad
    pre-existing page unregenerable for the whole repo.
    """
    seen: dict[str, PageEntity] = {}
    clashes = []
    for entity in entities:
        key = normalize(entity.name)
        if not key:
            continue
        first = seen.get(key)
        if first is None:
            seen[key] = entity
        else:
            clashes.append(f"{first.relpath} ({first.name!r}) and {entity.relpath} "
                           f"({entity.name!r}) both resolve as {key!r}")
    return clashes


def registry_of(entities) -> Registry:
    """THE shared base: a `Registry` built from `PageEntity` claims, indexed the reader's own way.

    Separate from `derive_registry` because the mint's post-rebase re-check needs "what this repo
    implies MINUS the entity being minted" — including your own just-committed page would make
    every proposal collide with itself. One builder, two populations.
    """
    registry = Registry()
    for entity in entities:
        registry.entities[entity.canonical_id] = {
            "name": entity.name, "type": entity.entity_type, "aliases": list(entity.aliases)}
    _index(registry)
    return registry


def derive_registry(repo: str) -> Registry:
    """The registry `wiki/entities/` implies — indexed by exactly the code that indexes it on
    load, so the derived view cannot disagree with the file it derives."""
    return registry_of(read_entity_pages(repo))


def _index(registry: Registry) -> None:
    """Populate both lookup maps exactly as `load_registry` does — by CALLING the same indexer,
    rather than by re-typing the same key functions and promising they stay identical."""
    for canonical_id, entity in registry.entities.items():
        index_entity(registry, canonical_id, entity)


def committed_registry(repo: str) -> Registry:
    """`ops/entity-registry.json` as it stands on disk. Missing file = empty registry —
    `load_registry`'s own semantics, and the honest reading of a repo with no entities yet."""
    return load_registry(registry_path(repo))


# ── drift: what the pages say that the file does not (and the reverse) ────────────────────────
@dataclass(frozen=True)
class Divergence:
    """One semantic difference between the pages and the committed registry — "which entity, and
    what about it", never a text diff. `entity` is the id (divergences about one entity sort
    together); `message` is the whole sentence a human reads."""
    entity: str
    message: str


@dataclass
class RegenerateOutcome:
    """What `regenerate` did or would do. `changed` is the only thing a caller branches on: it is
    semantic from `check` (any divergence) and BYTE-level from `regenerate`."""
    changed: bool = False
    page_count: int = 0
    divergences: list[Divergence] = field(default_factory=list)


def _describe(entity: dict) -> str:
    aliases = ", ".join(entity.get("aliases") or ()) or "none"
    return f"name: {entity.get('name')}, type: {entity.get('type')}, aliases: {aliases}"


def compare(derived: Registry, committed: Registry, *,
            page_of: dict[str, str] | None = None) -> list[Divergence]:
    """Every way the two disagree, each named in the vocabulary a steward acts in. Ordered by
    entity id — a check whose output reorders between runs is one nobody can diff in CI."""
    page_of = page_of or {}
    out: list[Divergence] = []
    for canonical_id in sorted(set(derived.entities) | set(committed.entities)):
        page = page_of.get(canonical_id, f"{ENTITIES_RELDIR}/{canonical_id}.md")
        mine, theirs = derived.entities.get(canonical_id), committed.entities.get(canonical_id)
        if theirs is None:
            out.append(Divergence(canonical_id, (
                f"{mine['name']!r} ({page}) is an entity page that {REGISTRY_RELPATH} does not "
                f"register at all — run `{FIX_COMMAND}` to fix it")))
            continue
        if mine is None:
            out.append(Divergence(canonical_id, (
                f"{theirs['name']!r} is registered in {REGISTRY_RELPATH} ({_describe(theirs)}) but "
                f"no page in {ENTITIES_RELDIR}/ declares it — either the page was deleted or the "
                f"entry was hand-written; run `{FIX_COMMAND}` to fix it")))
            continue
        if mine["name"] != theirs["name"]:
            out.append(Divergence(canonical_id, (
                f"{page} is titled {mine['name']!r} but {REGISTRY_RELPATH} registers "
                f"{canonical_id!r} as {theirs['name']!r} — run `{FIX_COMMAND}` to fix it")))
        if mine["type"] != theirs["type"]:
            out.append(Divergence(canonical_id, (
                f"{mine['name']!r} ({page}) declares entity_type {mine['type']!r} but "
                f"{REGISTRY_RELPATH} has type {theirs['type']!r} — run `{FIX_COMMAND}` to fix it")))
        extra = sorted(set(mine["aliases"]) - set(theirs["aliases"]))
        missing = sorted(set(theirs["aliases"]) - set(mine["aliases"]))
        if extra:
            out.append(Divergence(canonical_id, (
                f"{mine['name']!r} ({page}) declares alias(es) {', '.join(repr(a) for a in extra)} "
                f"that {REGISTRY_RELPATH} does not have — run `{FIX_COMMAND}` to fix it")))
        if missing:
            out.append(Divergence(canonical_id, (
                f"{REGISTRY_RELPATH} carries alias(es) "
                f"{', '.join(repr(a) for a in missing)} for {mine['name']!r} that {page} no longer "
                f"declares — run `{FIX_COMMAND}` to fix it")))
    return out


def check(repo: str) -> RegenerateOutcome:
    """What `--check` reports. Reads only — nothing on disk moves, so it is safe in CI and on a
    dirty clone."""
    entities = read_entity_pages(repo)
    derived = registry_of(entities)
    divergences = compare(derived, committed_registry(repo),
                          page_of={e.canonical_id: e.relpath for e in entities})
    return RegenerateOutcome(changed=bool(divergences), page_count=len(entities),
                             divergences=divergences)


def regenerate(repo: str) -> RegenerateOutcome:
    """Rewrite `ops/entity-registry.json` from the pages. Returns whether the BYTES changed.

    Byte-level on purpose: it differs from the semantic answer exactly once — a semantically-right
    but differently-formatted hand-written registry — and reporting that as a change lets a
    steward make the one canonicalization commit instead of the tool lying about what it wrote.
    Writes through `save_registry` (tmp + `os.replace`), so an interruption leaves the previous
    registry intact — this file is what every anchoring decision resolves against.
    """
    outcome = check(repo)
    path = registry_path(repo)
    before = snapshot(repo)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    save_registry(path, derive_registry(repo))
    outcome.changed = snapshot(repo) != before
    return outcome


def snapshot(repo: str) -> str | None:
    """The registry file's exact bytes, or `None` when it does not exist.

    `regenerate` compares before/after; the mint keeps one so a failed approval restores the file
    by bytes it captured itself — never `git checkout` or `git clean` in a human's working copy.
    """
    path = registry_path(repo)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()
