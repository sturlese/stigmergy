"""The registry generator: `ops/entity-registry.json` derived from `wiki/entities/*.md`.

This is what makes "derived view of entity pages" a fact rather than a claim: a hand-maintained
registry makes the word "derived" something nothing checks, and leaves the "the registry
regenerates" half of governed entity birth with no code behind it at all.

**The shape is the reader's, not this module's.** `kernel.registry`'s shape is canonical (it is
what ADR 008 describes), so the output is serialized through `save_registry` and read back through
`load_registry` — tested code that is imported and never reimplemented. Nothing here writes JSON.
That is also what makes `regenerate` idempotent for free: the writer sorts entities, sorts aliases
and pins the separators, so "run it twice, get the same bytes" is a property of the one writer
rather than of this module remembering to be careful.

**Scanned: `wiki/entities/` and nothing else.** Not `wiki/` recursively, not the whole repo. This
module governs entity BIRTH; an entity MERGE is a different decision with its own approval path
and is untouched here.

**The canonical id is `slugify(title)` and that is a contract, not a default.**

The registry maps `id -> {name, type, aliases}`, and the pages carry no id field — so if the id
were free-form the steward would be authoring a fact no regeneration could ever recover, and the
first `--check` after an approval would report drift against the file that approval had just
written. Deriving it makes the registry a genuine pure function of the pages; `birth.prepare`
therefore *verifies* the steward's `--id` against `canonical_id_for(name)` and refuses a mismatch,
rather than storing something unrecoverable. It is also what lets a hand-written registry be
reproduced byte for byte, as long as every id in it really is the slug of its page title.

**A page this cannot read is an ERROR, never a skip.** A silently dropped entity page is a
registry that quietly stops resolving a name the graph used to anchor on, which is the failure
`load_registry`'s own docstring refuses ("a broken identity file must never silently degrade to
wrong entities"). The same rule, one layer up.
"""
import os
from dataclasses import dataclass, field

from stigmergy.entities.errors import EntityError
from stigmergy.kernel import frontmatter as graph_pages
from stigmergy.kernel.normalize import normalize, slugify
from stigmergy.kernel.registry import Registry, load_registry, save_registry

# Repo-relative, slash-separated — the same spelling discipline `librarian.config`'s three
# RELPATHs use, and for the same reason: these are git paths first and filesystem paths second.
ENTITIES_RELDIR = "wiki/entities"
REGISTRY_RELPATH = "ops/entity-registry.json"

# What every drift message ends with, and the only command this subsystem tells anyone to run.
# One spelling, because a message containing a command is an executable promise — and this exact
# string is also written by hand in the knowledge repo's own `stigmergy_lint.py`, which is
# stdlib-only and cannot import it. That duplication is deliberate and is called out at both ends
# rather than left for someone to discover.
FIX_COMMAND = "stigmergy-entities regenerate"

# `entity_type` on the page -> `type` in the registry. The vocabulary is `ops/templates/entity.md`'s
# own comment (`person|organization|product|tool|repository|place`), which is the human-facing
# source of truth for the page contract; the linter does not enum-check this field, so this is the
# only place it is enforced at all. `birth.prepare` refuses anything outside it; the generator is
# LENIENT about it on read, because a page that already exists with an unexpected value must not
# make the whole registry unregenerable — that would turn one bad page into a repo-wide outage.
ENTITY_TYPES = ("person", "organization", "product", "tool", "repository", "place")

# `load_registry`'s own default for an entity with no declared type. Mirrored rather than
# re-decided, so a page with no `entity_type` and a registry entry with no `type` describe the
# same entity after a round trip instead of differing by a default nobody chose twice.
DEFAULT_ENTITY_TYPE = "organization"


def canonical_id_for(name: str) -> str:
    """The registry id a page titled `name` will always regenerate as.

    `slugify` rather than `normalize`: `normalize` strips legal suffixes and is the ALIAS-matching
    key (it is what makes "Globex Corp" find "Globex"), while an id is a stable, readable file-safe
    handle. They answer different questions and the registry already uses both — ids are keys,
    `by_alias` is normalized. Using the matcher for the key would give "Acme Corp" the id "acme"
    and make two genuinely different entities fight over one slot.
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


def entity_page_path(repo: str, name: str) -> str:
    """Where a page for `name` lives. The title IS the filename (and therefore the wikilink
    target): the knowledge repo resolves `[[Jordan Reyes]]` by basename, and the linter refuses
    duplicate basenames case-insensitively, so a page whose title and stem disagree would be
    linkable under one spelling and registered under another."""
    return os.path.join(entities_dir(repo), f"{name}.md")


def _aliases_of(front: dict) -> tuple[str, ...]:
    """`aliases` as a tuple of clean strings. A scalar is accepted as a one-element list — the
    linter reports `aliases must be a list` as its own finding, and refusing to regenerate over a
    page it has already flagged would make one lint error block every other entity's registration.
    """
    declared = front.get("aliases")
    if declared is None or declared == "":
        return ()
    values = declared if isinstance(declared, list) else [declared]
    return tuple(str(v).strip() for v in values if str(v).strip())


def read_entity_pages(repo: str) -> list[PageEntity]:
    """Every `wiki/entities/*.md`, as identity claims, sorted by id.

    Non-recursive on purpose: the folder is flat by convention and a nested page would be a
    different kind of thing (a view, a draft) that nobody decided should mint an identity.
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
            raise EntityError(
                f"{relpath} could not be read ({ex}) — the entity registry is derived from these "
                f"pages, so it cannot be regenerated without it") from ex
        front, _ = graph_pages.split_frontmatter(text)
        if not isinstance(front, dict):
            front = {}
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
    """Two pages whose TITLES resolve to one `by_alias` key. Beside `_duplicate_ids`, not folded
    into it, because they catch two different collapses and only one of them is visible in the
    file.

    An id is `slugify(title)` and a `by_alias` key is `normalize(title)`, and `normalize` folds
    strictly more: it strips legal suffixes, so `Acme` and `Acme Corp.` keep distinct ids while
    claiming one matching key. The registry then LOOKS unambiguous — two entries, two ids, the
    linter's page↔registry rule satisfied — and resolves that name to whichever page sorted last.
    That is the "Acme / ACME Corp / acme-client" failure governed birth exists to prevent, arriving
    through the derived view rather than through the gate, and it is exactly what a repo can end up
    holding when an approval races another one or lands beside an unregistered page.

    Titles only. An ALIAS colliding with another entity's name is the same hazard, but it is
    refused at mint time by `birth._refuse_collisions` against the registry the commit will
    publish, and refusing it HERE too would make one bad pre-existing page unregenerable for the
    whole repo — the outage this module's docstring argues against.
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

    Separate from `derive_registry` because the callers need two different populations of the same
    thing. `derive_registry(repo)` answers "what does this repo imply" and is what `regenerate`
    publishes; `entities.cli` also needs "what does this repo imply MINUS the entity I am currently
    minting" — the post-rebase collision re-check, where including your own just-committed page
    would make every proposal collide with itself. One builder, two questions, rather than a second
    filtered copy of the indexing rule somewhere it cannot be seen.
    """
    registry = Registry()
    for entity in entities:
        registry.entities[entity.canonical_id] = {
            "name": entity.name, "type": entity.entity_type, "aliases": list(entity.aliases)}
    _index(registry)
    return registry


def derive_registry(repo: str) -> Registry:
    """The registry `wiki/entities/` implies. Built through the reader's own dataclass so the
    `by_alias` index is populated by exactly the code that populates it on load — a second
    implementation of "which spellings resolve to this entity" is how a derived view starts
    disagreeing with the file it was derived from."""
    return registry_of(read_entity_pages(repo))


def _index(registry: Registry) -> None:
    """Populate `by_alias` exactly as `load_registry` does — same key function, same precedence."""
    for canonical_id, entity in registry.entities.items():
        for alias in (canonical_id, entity["name"], *entity.get("aliases", ())):
            key = normalize(str(alias))
            if key:
                registry.by_alias[key] = canonical_id


def committed_registry(repo: str) -> Registry:
    """`ops/entity-registry.json` as it stands on disk. A missing file is an EMPTY registry, which
    is `load_registry`'s own semantics for one — and the honest reading of a repo that has never
    had an entity."""
    return load_registry(registry_path(repo))


# ── drift: what the pages say that the file does not (and the reverse) ────────────────────────
@dataclass(frozen=True)
class Divergence:
    """One semantic difference between the pages and the committed registry.

    Semantic and not a text diff — a JSON diff answers "these bytes differ", and the question a
    steward has is "which entity, and what about it". `entity` is the id so two divergences about
    one entity sort together; `message` is the whole sentence a human reads.
    """
    entity: str
    message: str


@dataclass
class RegenerateOutcome:
    """What `regenerate` did or would do. `changed` is the only thing a caller branches on."""
    changed: bool = False
    page_count: int = 0
    divergences: list[Divergence] = field(default_factory=list)


def _describe(entity: dict) -> str:
    aliases = ", ".join(entity.get("aliases") or ()) or "none"
    return f"name: {entity.get('name')}, type: {entity.get('type')}, aliases: {aliases}"


def compare(derived: Registry, committed: Registry, *,
            page_of: dict[str, str] | None = None) -> list[Divergence]:
    """Every way the two disagree, each named in the vocabulary a steward acts in.

    Ordered by entity id so a repo with several drifts reports them the same way twice — a check
    whose output reorders between runs is one nobody can diff in CI.
    """
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
    """What `--check` reports. Reads only — nothing on disk moves, which is what makes it safe to
    run in CI and safe to run on a dirty clone."""
    entities = read_entity_pages(repo)
    derived = derive_registry(repo)
    divergences = compare(derived, committed_registry(repo),
                          page_of={e.canonical_id: e.relpath for e in entities})
    return RegenerateOutcome(changed=bool(divergences), page_count=len(entities),
                             divergences=divergences)


def regenerate(repo: str) -> RegenerateOutcome:
    """Rewrite `ops/entity-registry.json` from the pages. Returns whether the BYTES changed.

    `changed` is byte-level rather than semantic on purpose, and the two answers differ exactly
    once — on a repo whose registry is semantically right but was hand-written with different
    formatting. That is the one canonicalization commit a repo is allowed to need, and reporting it
    as a change is what lets a steward see it and make that commit; reporting it as "nothing to do"
    while rewriting the file would be the tool lying about what it just did.

    Writes through `save_registry`, which writes a temp file and `os.replace`s it — an interrupted
    regeneration leaves the previous registry intact rather than a half-written one, which matters
    because this file is what every anchoring decision resolves against.
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

    Two callers, one meaning. `regenerate` compares before against after to answer "did this
    actually change anything"; `entities.cli._mint` keeps one so a failed approval can put the file
    back EXACTLY as it found it — by bytes it captured itself, never with `git checkout` or `git
    clean`, because the file being restored lives in a human's working copy.
    """
    path = registry_path(repo)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()
