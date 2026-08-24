from __future__ import annotations

import os
from pathlib import Path

import yaml

from stigmergy.entities.model import REGISTRY_PATH, load_entities, registry_bytes
from stigmergy.index.corpus import split_frontmatter_checked


def repair_deterministic(root: str) -> tuple[str, ...]:
    records = load_entities(root)
    changed = []
    redirects = {absorbed: canonical for canonical, record in records.items() for absorbed in record.absorbed_ids}
    for folder in ("wiki/notes", "wiki/concepts"):
        base = Path(root, *folder.split("/"))
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            metadata, body, malformed = split_frontmatter_checked(text)
            entities = metadata.get("entity")
            if malformed or not isinstance(entities, list):
                continue
            replacement = list(dict.fromkeys(redirects.get(value, value) for value in entities))
            if replacement == entities:
                continue
            metadata["entity"] = replacement
            frontmatter = yaml.safe_dump(
                metadata,
                allow_unicode=True,
                sort_keys=False,
                width=1000,
            ).rstrip()
            path.write_text(f"---\n{frontmatter}\n---\n\n{body.strip()}\n", encoding="utf-8")
            changed.append(path.relative_to(root).as_posix())
    registry = Path(root, *REGISTRY_PATH.split("/"))
    expected = registry_bytes(records)
    if not registry.is_file() or registry.read_bytes() != expected:
        os.makedirs(registry.parent, exist_ok=True)
        registry.write_bytes(expected)
        changed.append(REGISTRY_PATH)
    return tuple(sorted(changed))
