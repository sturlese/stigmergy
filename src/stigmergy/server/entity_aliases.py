"""Entity-alias resolution for entity-first retrieval over `ops/entity-registry.json`:
`stigmergy.server` may not import `stigmergy.entities` (packages talk through files).

READING and PARSING are separate here, and that is the point: the registry reaches the service
from two places — the index's snapshot (refreshed by the push webhook, so a freshly minted entity
is served immediately) and the file a process was started with (the fallback) — and both must mean
exactly the same thing. So the TEXT is the unit: `aliases_from_text`/`registry_from_text` are the
one parser, and the path-taking `load_aliases`/`load_registry` are `read_file` plus that parser.

`_norm` is deliberately NOT `kernel.normalize.normalize`: that one is the registry's stricter
folding for resolve-before-mint collision detection, where a false negative lets a duplicate
through a gate. A false negative HERE only costs a fallback to ordinary semantic search.

It IS `kernel.normalize.resolution_key`, the narrow fold #77 split out of that stricter one — the
key that folds only how a keyboard and a locale render a name. `resolve_exact` asks the question
`Registry.canonical_id` asks, "which entity does this text name?", and two implementations of one
fold is how the MCP server and the librarian come to disagree about which entity a name means: the
worker anchors a page to an id this service would never resolve back, and nothing anywhere reports
a mismatch. Imported rather than re-derived; `stigmergy.kernel` is the bottom of the stack and
depends on nothing, so the edge costs this package nothing it did not already have.
"""
import json
import os
import re

from stigmergy.kernel.normalize import resolution_key

# POSIX, because a webhook's changed-path list is POSIX — `server.webhook` matches this string
# against it verbatim. `default_path` re-splits it for the local filesystem.
ENTITY_REGISTRY_RELPATH = "ops/entity-registry.json"


def default_path(repo_dir: str | None) -> str:
    """Same `--repo` convention as `identity.default_path`. The PATH is resolved once at startup,
    and it is the FALLBACK source: wherever the index carries a registry snapshot, that snapshot is
    what the service answers from (see `service.BrainService._registry_source`)."""
    return os.path.join(repo_dir, *ENTITY_REGISTRY_RELPATH.split("/")) if repo_dir else ""


# Lowercase, accent-folded, punctuation-collapsed — matching inside a question, not a claim about
# entity identity (see module docstring). One name inside this module for the ONE fold, so every
# reader here and `Registry.canonical_id` cannot answer differently.
_norm = resolution_key


def read_file(path: str | None) -> str | None:
    """The registry file's TEXT, or `None` when there is no file to read (an unset path, or a path
    nothing exists at). `None` is the "no registry here" answer every reader fails open on, and it
    is what lets a caller choosing between sources — snapshot or file — do so without knowing how
    either one is parsed."""
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def _entities_from_text(text: str | None, origin: str) -> dict:
    """`id -> raw registry record dict` — the ONE parse every reader shares, so a snapshot and a
    file behave identically for both readers. No registry at all (`None`) -> `{}` (fail-open:
    resolution finds nothing). Malformed JSON or a top level that is not `{"entities": {...}}`
    RAISES: silently degrading retrieval has no signal anywhere an operator or a golden run would
    see it. `origin` only names the source in that message, for the operator who has to fix it —
    it is why the message must not reach a tool caller (`errors.RegistryError`)."""
    if text is None:
        return {}
    data = json.loads(text)
    # Wording mirrored from `kernel.registry.load_registry`, deliberately: two parsers over one
    # file format must not disagree about what a registry IS. Without this, valid JSON whose top
    # level is a list/string/number/null reached `.get` and raised `AttributeError`, which the
    # service converts to nothing — so the ONE malformed shape that skipped `RegistryError` was
    # also the one a truncated snapshot is likeliest to produce.
    if not isinstance(data, dict):
        raise ValueError(f"entity registry {origin}: top level must be an object")
    entities = data.get("entities")
    if not isinstance(entities, dict):
        raise ValueError(f"entity registry {origin}: top-level 'entities' object is required")
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


def aliases_from_text(text: str | None, origin: str) -> dict[str, str]:
    """Normalized alias/name/id text -> canonical entity id. Shares `_entities_from_text` and the
    per-field helpers with `registry_from_text`, so neither reader can be hardened without the
    other."""
    aliases: dict[str, str] = {}
    for cid, e in _entities_from_text(text, origin).items():
        if not isinstance(e, dict):
            continue
        for alias in (cid, _record_name(e), *_record_aliases(e)):
            key = _norm(str(alias))
            if key:
                aliases[key] = cid
    return aliases


def registry_from_text(text: str | None, origin: str) -> dict[str, dict]:
    """`id -> {id, name, type, aliases, approved_by}` — the full records
    `list_entities`/`describe_entity` serve. Same parse and per-field helpers as
    `aliases_from_text`; a non-mapping record is skipped."""
    out: dict[str, dict] = {}
    for cid, e in _entities_from_text(text, origin).items():
        if not isinstance(e, dict):
            continue
        out[cid] = {
            "id": cid,
            "name": _record_name(e),
            "type": str(e.get("type", "") or ""),
            "aliases": _record_aliases(e),
            # The one lifecycle fact the generator writes: who introduced the identity.
            # Absent on a registry from before the key existed.
            "approved_by": str(e.get("approved_by", "") or ""),
        }
    return out


def load_aliases(path: str | None) -> dict[str, str]:
    """`aliases_from_text` over a FILE — for a caller that holds only a path (`evals/run_qa.py`,
    and any process with no index connection to ask for a snapshot)."""
    return aliases_from_text(read_file(path), path or "")


def load_registry(path: str | None) -> dict[str, dict]:
    """`registry_from_text` over a FILE, the path-only half of `load_aliases` above."""
    return registry_from_text(read_file(path), path or "")


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
