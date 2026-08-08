"""Entity-alias resolution for entity-first retrieval.

`ops/entity-registry.json` is `stigmergy.kernel.registry`'s own on-disk contract — canonical
id -> `{name, type, aliases}` — and `stigmergy.entities` (the steward's CLI) is its only writer.
Neither `stigmergy.server` nor `stigmergy.answer` may import `stigmergy.entities`
(`tests/test_architecture.py`: packages talk through files, never imports), so this module reads
that JSON file directly — a file another package produces, read here without importing the code
that wrote it.

**Deliberately NOT byte-identical to `kernel.normalize.normalize`.** That function is the
registry's OWN, stricter normalization (accent/legal-suffix folding), used for resolve-before-mint
collision detection (`entities.birth`) — a false negative THERE would let a duplicate entity
through the gate. This module's normalization only has to be good enough to recognize a
registered name/alias occurring INSIDE a question; a false negative here just means the run falls
back to the semantic search this system already does today — never a security or correctness
defect.
"""
import json
import os
import re
import unicodedata

ENTITY_REGISTRY_RELATIVE = os.path.join("ops", "entity-registry.json")


def default_path(repo_dir: str | None) -> str:
    """Same `--repo` convention as `identity.default_path` — baked at server startup from
    whichever checkout this process was deployed with: base-commit semantics, never a live re-read
    of a working tree that could be mid-edit."""
    return os.path.join(repo_dir, ENTITY_REGISTRY_RELATIVE) if repo_dir else ""


def _norm(text: str) -> str:
    """Lowercase, accent-folded, punctuation-collapsed — good enough to match "Globex Corp"
    inside a natural-language question, not a claim about entity identity (see module docstring)."""
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.,()\"'/]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _load_entities(path: str | None) -> dict:
    """`id -> raw registry record dict` — the ONE JSON read + top-level validation both
    `load_aliases` and `load_registry` share, so a missing file and a malformed one behave
    IDENTICALLY for both readers rather than each parsing the file its own way.

    Missing/absent path -> `{}`: fail-open — entity-first resolution and registry enrichment
    both then find nothing, never a hard failure of `ask`/`list_entities`/`describe_entity`
    themselves.

    Malformed JSON (or a top-level shape that is not `{"entities": {...}}`) is NOT swallowed: a
    broken registry file is an operator-visible fault (raises), the same posture
    `identity.resolve_audiences` takes for a broken `identities.json` — silently degrading
    resolution would make retrieval/navigation quietly worse with no signal anywhere a golden run
    or an operator would see it.
    """
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    entities = data.get("entities")
    if not isinstance(entities, dict):
        raise ValueError(f"entity registry {path}: top-level 'entities' object is required")
    return entities


def _record_name(record: dict) -> str:
    """One record's display name as TEXT, `""` when it is absent or null.

    Shared by both readers, because `_load_entities` promises they behave identically on a
    malformed file and that has to hold one level down too. `str(None)` is `"none"`, and a
    `"name": null` in the registry used to become a real alias spelled `none` — so every question
    containing that ordinary English word resolved to that entity. It failed OPEN: no error, just
    a wrong `entity_hint` and a wrong `fts_expansion` handed to ranking.
    """
    return str(record.get("name", "") or "")


def _record_aliases(record: dict) -> list[str]:
    """One record's `aliases` as a list of TEXT, `[]` for any shape that is not a list of scalars.

    The list-ness matters as much as the elements: `aliases` is unpacked with `*`, and a bare
    STRING unpacks one character at a time — `"acme corp"` became eight single-letter aliases, so
    a lone "a" in ordinary prose resolved to that entity. `None` raised outright.
    """
    aliases = record.get("aliases")
    if not isinstance(aliases, list):
        return []
    return [str(a) for a in aliases if isinstance(a, str | int | float)]


def load_aliases(path: str | None) -> dict[str, str]:
    """Normalized alias/name/id text -> canonical entity id. See `_load_entities` for the
    missing-file / malformed-file postures this shares with `load_registry`, and `_record_name` /
    `_record_aliases` for the per-FIELD defences it shares with it — the two readers take their
    record fields through the same two helpers so neither can be hardened without the other."""
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
    """`id -> {id, name, type, aliases}` — the full registry records: `list_entities` enriches
    each anchored id with these, and `describe_entity`'s entity layer serves them
    directly. Same loader (`_load_entities`) `load_aliases` shares — a missing file and a
    malformed one behave identically for both readers — and a record whose own shape is not a
    mapping is skipped, same as `load_aliases`."""
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
    """The LONGEST registered alias/name/id that appears as a whole-word phrase in `question`, or
    `None`. Longest-first so "Acme Corp" wins over a shorter alias "Acme" that also matches;
    whole-word so a short alias like "gx" cannot match inside an unrelated word.

    Registry aliases ONLY: this function resolves a name to an id and never touches
    `search.search_arms` — expanding the query itself is a separate decision made elsewhere.
    """
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
    alias — `describe_entity`'s resolution: the input already NAMES one entity, unlike
    `resolve_entity`'s free-text substring search over a whole question. Same
    registry loader (`load_aliases`), same normalization (`_norm`) — ONE resolution mechanism for
    the search filter, the tools, and entity-first alike — a different question asked of it."""
    if not aliases or not text:
        return None
    return aliases.get(_norm(text))
