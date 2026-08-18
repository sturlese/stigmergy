"""The entity registry — curated identity the automatic canonicalization defers to.

On-disk shape: `{"entities": {"<id>": {"name", "type", "aliases": []}}}`. Every anchoring
decision consults it FIRST — a string rule never mints identity. Plain, diffable JSON with
exactly ONE writer: `stigmergy-entities`, the governed birth door.

**TWO lookups, because the registry is asked two different questions** (`kernel.normalize`'s
docstring carries the argument in full):

- `canonical_id` — *"which entity does this text MEAN?"*, asked at filing time. Keyed on
  `resolution_key`, which folds no judgment. A spelling the registry has never seen does not
  resolve here and is not supposed to: the filing agent judges the near miss and declares the id,
  and this lookup is the FENCE that refuses an id nothing registers.
- `collision_id` — *"would this NEW name be confused with one we have?"*, asked at mint time by
  `entities.birth._refuse_collisions`. Keyed on `normalize`, the coarser legal-suffix fold, whose
  failure direction is a refusal to a human rather than a silent wrong anchor.

Both maps are filled by `index_entity`, the ONE place either key is computed for a stored entity.
"""
import json
import os
from dataclasses import dataclass, field

from stigmergy.kernel.fsutil import write_text_atomic
from stigmergy.kernel.normalize import normalize, resolution_key

REGISTRY_FILE = "entity-registry.json"


@dataclass
class Registry:
    entities: dict = field(default_factory=dict)      # id -> {name, type, aliases: []}
    by_alias: dict = field(default_factory=dict)      # COLLISION key -> id (mint gate)
    by_resolution: dict = field(default_factory=dict)  # RESOLUTION key -> id (filing)

    def canonical_id(self, name: str) -> str | None:
        """The id this TEXT names, or `None`. See the module docstring for why this fold is the
        narrow one — a near miss is the agent's judgment to declare, not this map's to guess."""
        return self.by_resolution.get(resolution_key(name))

    def collision_id(self, name: str) -> str | None:
        """The id this name would COLLIDE with at mint time, or `None`. The coarse fold, and the
        only lookup that keeps it: a false negative here mints a duplicate identity."""
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

    The id, the display name and every alias are all keyed, and later entries win — the precedence
    the file has always had.
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
    the copy the derived index caches for the deployed server (`index.store`'s
    `entity_registry_snapshot`, issue #74). Splitting the read from the parse is what keeps ONE
    answer to "does this registry load": a lint that read the served copy through a second,
    laxer parse would bless a substrate the server refuses. `origin` only names the source in the
    error, since there is no path to give when the bytes came from the index."""
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
        reg.entities[cid] = {"name": e["name"], "type": e.get("type", "organization"),
                             "aliases": list(e.get("aliases", []))}
        index_entity(reg, cid, reg.entities[cid])
    return reg


def save_registry(path: str, reg: Registry) -> None:
    data = {"entities": {cid: {"name": e["name"], "type": e["type"],
                               "aliases": sorted(set(e["aliases"]))}
                         for cid, e in sorted(reg.entities.items())}}
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
