"""The entity registry — curated identity the automatic canonicalization defers to.

On-disk shape: `{"entities": {"<id>": {"name", "type", "aliases": [], "approved_by"}}}`. Every
anchoring decision consults it FIRST — a string rule never mints identity. Plain, diffable JSON,
DERIVED from `wiki/entities/*.md` by `entities.generator` and written by nothing else.

**One lifecycle fact rides beside the identity: who introduced it.** The librarian files a capture
about a name nothing resolves to by creating the entity page itself, born CONFIRMED by the person
whose capture it was ([ADR 044](../../../docs/decisions/044-the-capture-is-the-approval.md)), so the
note lands anchored and later captures naming the same thing anchor to the same id. `approved_by`
is that person; empty on a page written before the field existed, and never a waiting state — there
is none. A spelling the material uses for a registered entity is one of its `aliases`, added in the
same commit.

**TWO lookups, because the registry is asked two different questions** (`kernel.normalize`'s
docstring carries the argument in full):

- `canonical_id` — *"which entity does this text MEAN?"*, asked at filing time. Keyed on
  `resolution_key`, which folds no judgment. A spelling the registry has never seen does not
  resolve here and is not supposed to: the filing agent judges the near miss and declares the id,
  and this lookup is the FENCE that refuses an id nothing registers.
- `collision_id` — *"would this NEW name be confused with one we have?"*, asked before an entity
  is created by `entities.birth._refuse_collisions`. Keyed on `normalize`, the coarser legal-suffix
  fold, whose failure direction is a refusal rather than a silent duplicate identity.

Both maps are filled by `index_entity`, the ONE place either key is computed for a stored entity.
"""
import json
import os
from dataclasses import dataclass, field

from stigmergy.kernel.fsutil import write_text_atomic
from stigmergy.kernel.normalize import normalize, resolution_key

REGISTRY_FILE = "entity-registry.json"

# The registry's own spelling of an entry's keys. `entities.generator` reads the PAGE-side
# vocabulary (`approved_by:` on the frontmatter) and builds entries through `entry()` below, so the
# two vocabularies meet in exactly one function.
APPROVED_BY_KEY = "approved_by"


def entry(name: str, entity_type: str, aliases=(), *, approved_by: str = "") -> dict:
    """THE one constructor of a registry entry.

    Every writer of `Registry.entities` — the file reader here, the page-derived builder in
    `entities.generator`, a test seeding a registry — goes through this, so no entry can lack a
    field a reader branches on. A reader that `.get()`s with a default is a reader that silently
    treats a half-built entry as approved.
    """
    return {"name": str(name), "type": str(entity_type), "aliases": list(aliases or ()),
            APPROVED_BY_KEY: str(approved_by or "")}


@dataclass
class Registry:
    entities: dict = field(default_factory=dict)      # id -> entry() (see above)
    by_alias: dict = field(default_factory=dict)      # COLLISION key -> id (the birth gate)
    by_resolution: dict = field(default_factory=dict)  # RESOLUTION key -> id (filing)

    def canonical_id(self, name: str) -> str | None:
        """The id this TEXT names, or `None`. See the module docstring for why this fold is the
        narrow one — a near miss is the agent's judgment to declare, not this map's to guess."""
        return self.by_resolution.get(resolution_key(name))

    def collision_id(self, name: str) -> str | None:
        """The id this name would COLLIDE with at birth, or `None`. The coarse fold, and the only
        lookup that keeps it: a false negative here creates a duplicate identity."""
        return self.by_alias.get(normalize(name))

    def title(self, canonical: str) -> str | None:
        e = self.entities.get(canonical)
        return e.get("name") if e else None

    def type_of(self, canonical: str) -> str | None:
        e = self.entities.get(canonical)
        return e.get("type") if e else None


def index_entity(reg: Registry, canonical_id: str, entry: dict) -> None:
    """Key one already-stored entity into BOTH lookup maps.

    THE one place either key is computed for a registry, shared by the file/text reader here and by
    `entities.generator._index` (which builds a `Registry` from `wiki/entities/` instead). Two
    indexers would be two answers to "does this name resolve", and the generator's own docstring has
    promised for as long as it existed that it indexes "exactly as `load_registry` does".

    The id, the display name and every alias are keyed, and later entries win — the precedence the
    file has always had.
    """
    for alias in (canonical_id, entry.get("name", ""), *(entry.get("aliases") or ())):
        text = str(alias)
        collision = normalize(text)
        if collision:
            reg.by_alias[collision] = canonical_id
        resolution = resolution_key(text)
        if resolution:
            reg.by_resolution[resolution] = canonical_id


def load_registry(path: str | None) -> Registry:
    """Missing path/file -> empty registry (the graph works unregistered); malformed -> error,
    loudly — a broken identity file must never silently degrade to wrong entities."""
    if not path or not os.path.exists(path):
        return Registry()
    with open(path, encoding="utf-8") as f:
        return registry_from_text(f.read(), path)


def registry_from_text(text: str | None, origin: str) -> Registry:
    """`load_registry` over TEXT — the same reader, for bytes that never were a local file.

    The registry reaches a reader from two places now: the file this module has always read, and
    the copy the derived index caches for the deployed server (`index.store`'s ops-file snapshot,
    issue #74). Splitting the read from the parse is what keeps ONE answer to "does this registry
    load": a lint that read the served copy through a second, laxer parse would bless a substrate
    the server refuses. `origin` only names the source in the error, since there is no path to
    give when the bytes came from the index.

    A file written before `approved_by` existed carries no approver, and reads as confirmed by
    nobody in particular — there is no waiting state for it to fall into.
    """
    reg = Registry()
    if text is None:
        return reg
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"registry {origin}: top level must be an object")
    entities = data.get("entities")
    if not isinstance(entities, dict):
        raise ValueError(f"registry {origin}: top-level 'entities' object is required")
    for cid, e in entities.items():
        if not isinstance(e, dict) or not e.get("name"):
            raise ValueError(f"registry {origin}: entity {cid!r} needs at least a 'name'")
        reg.entities[cid] = entry(
            e["name"], e.get("type", "organization"), e.get("aliases", []),
            approved_by=str(e.get(APPROVED_BY_KEY) or ""))
        index_entity(reg, cid, reg.entities[cid])
    return reg


def registry_text(reg: Registry) -> str:
    """The exact bytes `save_registry` writes, as a STRING.

    Split out from the write for one caller and one reason: the repair loop's `entity-alias` kind
    has to know what the regenerated registry WILL say before it writes anything, because the
    declared repair stores those bytes and the apply byte-compares against them — the corpus can
    move between deriving a repair and committing it, and a mismatch must be a refusal rather than
    a silently different registry. Building
    that string a second way would be a second writer of this file format, which is the one thing
    `ops/entity-registry.json` may not have — so the prediction and the write share this function.

    Sorting and the separators live HERE, which is what makes `generator.regenerate` idempotent.
    Every key is written on every entry, `approved_by` included: a reader that has to `.get()` a
    default is a reader that can be wrong about which default.
    """
    data = {"entities": {cid: {"name": e["name"], "type": e["type"],
                               "aliases": sorted(set(e["aliases"])),
                               APPROVED_BY_KEY: str(e.get(APPROVED_BY_KEY) or "")}
                         for cid, e in sorted(reg.entities.items())}}
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def save_registry(path: str, reg: Registry) -> None:
    write_text_atomic(path, registry_text(reg))
