"""Entity-alias resolution for entity-first retrieval, reading `ops/entity-registry.json`
directly: `stigmergy.server` may not import `stigmergy.entities` (packages talk through files).

`_norm` is deliberately NOT `kernel.normalize.normalize`: that one is the registry's stricter
folding for resolve-before-mint collision detection, where a false negative lets a duplicate
through a gate. A false negative HERE only costs a fallback to ordinary semantic search.
"""
import json
import os
import re
import unicodedata

ENTITY_REGISTRY_RELATIVE = os.path.join("ops", "entity-registry.json")


def default_path(repo_dir: str | None) -> str:
    """Same `--repo` convention as `identity.default_path` — baked at server startup, never a
    live re-read of a working tree that could be mid-edit."""
    return os.path.join(repo_dir, ENTITY_REGISTRY_RELATIVE) if repo_dir else ""


def _norm(text: str) -> str:
    """Lowercase, accent-folded, punctuation-collapsed — matching inside a question, not a claim
    about entity identity (see module docstring)."""
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.,()\"'/]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _load_entities(path: str | None) -> dict:
    """`id -> raw registry record dict` — the ONE JSON read both `load_aliases` and
    `load_registry` share, so missing and malformed files behave identically for both readers.
    Missing path -> `{}` (fail-open: resolution finds nothing). Malformed JSON or a top level
    that is not `{"entities": {...}}` RAISES: silently degrading retrieval has no signal anywhere
    an operator or a golden run would see it."""
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    entities = data.get("entities")
    if not isinstance(entities, dict):
        raise ValueError(f"entity registry {path}: top-level 'entities' object is required")
    return entities


def _record_name(record: dict) -> str:
    """One record's display name as TEXT, `""` when absent or null — shared by both readers.
    Guards `"name": null`: `str(None)` would mint a real alias spelled `none`, resolving every
    question containing that ordinary word."""
    return str(record.get("name", "") or "")


def _record_aliases(record: dict) -> list[str]:
    """One record's `aliases` as a list of TEXT, `[]` for any non-list shape. The list-ness
    matters: `aliases` is unpacked with `*`, and a bare STRING would unpack one character at a
    time into single-letter aliases."""
    aliases = record.get("aliases")
    if not isinstance(aliases, list):
        return []
    return [str(a) for a in aliases if isinstance(a, str | int | float)]


def load_aliases(path: str | None) -> dict[str, str]:
    """Normalized alias/name/id text -> canonical entity id. Shares `_load_entities` and the
    per-field helpers with `load_registry`, so neither reader can be hardened without the other."""
    aliases: dict[str, str] = {}
    for cid, e in _load_entities(path).items():
        if not isinstance(e, dict):
            continue
        for alias in (cid, _record_name(e), *_record_aliases(e)):
            key = _norm(str(alias))
            if key:
                aliases[key] = cid
    return aliases


def load_registry(path: str | None) -> dict[str, dict]:
    """`id -> {id, name, type, aliases}` — the full records `list_entities`/`describe_entity`
    serve. Same loader and per-field helpers as `load_aliases`; a non-mapping record is skipped."""
    out: dict[str, dict] = {}
    for cid, e in _load_entities(path).items():
        if not isinstance(e, dict):
            continue
        out[cid] = {
            "id": cid,
            "name": _record_name(e),
            "type": str(e.get("type", "") or ""),
            "aliases": _record_aliases(e),
        }
    return out


def resolve_entity(aliases: dict[str, str], question: str) -> str | None:
    """The LONGEST registered alias/name/id appearing as a whole-word phrase in `question`, or
    None. Longest-first so "Acme Corp" beats "Acme"; whole-word so a short alias cannot match
    inside an unrelated word. Resolves a name to an id only — query expansion happens elsewhere."""
    if not aliases or not question:
        return None
    q_norm = _norm(question)
    if not q_norm:
        return None
    best_key = ""
    best_cid = None
    for key, cid in aliases.items():
        if len(key) <= len(best_key):
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", q_norm):
            best_key, best_cid = key, cid
    return best_cid


def resolve_exact(aliases: dict[str, str], text: str) -> str | None:
    """The canonical id for `text` when it IS (after normalization) a registered id, name or
    alias — the input already NAMES one entity, unlike `resolve_entity`'s substring search. Same
    loader, same `_norm`: one resolution mechanism, a different question asked of it."""
    if not aliases or not text:
        return None
    return aliases.get(_norm(text))
