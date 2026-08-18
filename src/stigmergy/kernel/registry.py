"""The entity registry — curated identity the automatic canonicalization defers to.

On-disk shape: `{"entities": {"<id>": {"name", "type", "aliases": []}}}`. Every anchoring
decision consults it FIRST — a string rule never mints identity. Plain, diffable JSON with
exactly ONE writer: `stigmergy-entities`, the governed birth door.
"""
import json
import os
from dataclasses import dataclass, field

from stigmergy.kernel.fsutil import write_text_atomic
from stigmergy.kernel.normalize import normalize

REGISTRY_FILE = "entity-registry.json"


@dataclass
class Registry:
    entities: dict = field(default_factory=dict)   # id -> {name, type, aliases: []}
    by_alias: dict = field(default_factory=dict)   # normalized alias/name/id -> id

    def canonical_id(self, name: str) -> str | None:
        return self.by_alias.get(normalize(name))

    def title(self, canonical: str) -> str | None:
        e = self.entities.get(canonical)
        return e.get("name") if e else None

    def type_of(self, canonical: str) -> str | None:
        e = self.entities.get(canonical)
        return e.get("type") if e else None


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
        for alias in (cid, e["name"], *e.get("aliases", [])):
            key = normalize(str(alias))
            if key:
                reg.by_alias[key] = cid
    return reg


def registry_text(reg: Registry) -> str:
    """The exact bytes `save_registry` writes, as a STRING.

    Split out from the write for one caller and one reason: the repair loop's `entity-alias` kind
    has to know what the regenerated registry WILL say before it writes anything, because a
    proposal stores the bytes a steward approves and the apply byte-compares against them. Building
    that string a second way would be a second writer of this file format, which is the one thing
    `ops/entity-registry.json` may not have — so the prediction and the write share this function.

    Sorting and the separators live HERE, which is what makes `generator.regenerate` idempotent.
    """
    data = {"entities": {cid: {"name": e["name"], "type": e["type"],
                               "aliases": sorted(set(e["aliases"]))}
                         for cid, e in sorted(reg.entities.items())}}
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def save_registry(path: str, reg: Registry) -> None:
    write_text_atomic(path, registry_text(reg))
