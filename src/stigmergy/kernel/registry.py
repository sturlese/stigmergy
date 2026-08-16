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
    reg = Registry()
    if not path or not os.path.exists(path):
        return reg
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"registry {path}: top level must be an object")
    entities = data.get("entities")
    if not isinstance(entities, dict):
        raise ValueError(f"registry {path}: top-level 'entities' object is required")
    for cid, e in entities.items():
        if not isinstance(e, dict) or not e.get("name"):
            raise ValueError(f"registry {path}: entity {cid!r} needs at least a 'name'")
        reg.entities[cid] = {"name": e["name"], "type": e.get("type", "organization"),
                             "aliases": list(e.get("aliases", []))}
        for alias in (cid, e["name"], *e.get("aliases", [])):
            key = normalize(str(alias))
            if key:
                reg.by_alias[key] = cid
    return reg


def save_registry(path: str, reg: Registry) -> None:
    data = {"entities": {cid: {"name": e["name"], "type": e["type"],
                               "aliases": sorted(set(e["aliases"]))}
                         for cid, e in sorted(reg.entities.items())}}
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
