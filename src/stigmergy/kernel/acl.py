"""ACL resolution — audience labels derived deterministically from the source path.

Who may see a document is a property of WHERE it lives (the same insight as entity resolution):
"Finance/" is for finance+leadership, "Clients/" for sales, board minutes for leadership. The
mapping is config, not code, and never an LLM decision:

    {"default": ["all"],
     "rules": [
       {"path_prefix": "/Drive/Finance", "audiences": ["finance", "leadership"]},
       {"unit": "Clients", "audiences": ["sales", "leadership"]},
       {"path_contains": "board", "audiences": ["leadership"]}
     ]}

First matching rule wins (ordered, like the corpus taxonomy). The resolved list lands in the
page frontmatter (`acl: [...]`), and on views as the INTERSECTION of their members' audiences —
a rollup must never widen access to what it summarizes. `stigmergy.server.acl.visible()` enforces
it at query time; pages without `acl` are visible to every client.

Deliberate limitation, documented: this maps *conventions* to audiences. Mirroring live Drive
per-file permissions is a connector concern (the fetch sidecar already persists whatever
metadata Drive returns) and would feed the same field.
"""
import json

_MATCHERS = ("path_prefix", "path_contains", "unit", "entity_kind")


def load_acl_config(path: str | None) -> dict | None:
    """None/missing -> no ACLs (open corpus). Malformed -> loud error: silently open is the one
    failure mode an access-control file must not have."""
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return load_acl_config_text(f.read(), label=path)


def load_acl_config_text(text: str, *, label: str) -> dict | None:
    """The same validation over CONTENT rather than a path, with `label` naming the source.

    The seam exists because not every caller has a file to open. The librarian reads
    `ops/acl.json` out of the commit its worktrees branch from rather than out of a working tree
    (ST3, `stigmergy.librarian.base_inputs`), so what it holds is bytes and a locator like
    `origin/main@abc123def456:ops/acl.json`. Every message below names `label` for that reason —
    the dialect adapter in `librarian.acl_rules` re-raises them, and a message naming a temp file
    or nothing at all would tell an operator to go and edit something that does not exist.

    `load_acl_config` is now this function plus a `read()`, so there is one validator and the two
    entry points cannot drift about what a malformed config is.
    """
    cfg = json.loads(text)
    rules = cfg.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError(f"acl config {label}: 'rules' must be a list")
    for rule in rules:
        if not isinstance(rule.get("audiences"), list) or not rule["audiences"]:
            raise ValueError(f"acl config {label}: every rule needs a non-empty 'audiences' list")
        if not any(k in rule for k in _MATCHERS):
            raise ValueError(f"acl config {label}: rule needs one of {_MATCHERS}: {rule}")
        _check_labels(label, rule["audiences"])
    default = cfg.get("default", ["all"])
    if not isinstance(default, list) or not default:
        raise ValueError(f"acl config {label}: 'default' must be a non-empty list")
    _check_labels(label, default)
    return {"default": [str(a) for a in default],
            "rules": [{k: rule[k] for k in (*_MATCHERS, "audiences") if k in rule} for rule in rules]}


def _check_labels(path: str, audiences: list) -> None:
    """Audience labels can be CSV-serialized downstream: a comma inside a label would silently
    split into two audiences at enforcement time — the exact silent-corruption failure mode an
    access-control config must not have. Empty labels would vanish in the same round-trip."""
    for a in audiences:
        s = str(a)
        if "," in s or not s.strip():
            raise ValueError(f"acl config {path}: invalid audience label {s!r} "
                             "(labels must be non-empty and must not contain ',')")


def resolve_acl(config: dict | None, source_path: str, unit: str | None,
                entity_kind: str | None) -> list[str] | None:
    """Audience list for one document, or None when ACLs are off. First matching rule wins."""
    if config is None:
        return None
    low = (source_path or "").lower()
    for rule in config["rules"]:
        if "path_prefix" in rule and low.startswith(str(rule["path_prefix"]).lower()):
            return list(rule["audiences"])
        if "path_contains" in rule and str(rule["path_contains"]).lower() in low:
            return list(rule["audiences"])
        if "unit" in rule and unit and str(rule["unit"]).lower() == str(unit).lower():
            return list(rule["audiences"])
        if "entity_kind" in rule and entity_kind and rule["entity_kind"] == entity_kind:
            return list(rule["audiences"])
    return list(config["default"])


def view_acl(member_acls: list) -> list[str] | None:
    """A view summarizes all its members: its audience is the INTERSECTION of theirs. Members
    without ACLs don't restrict; all-None -> None (open). An empty intersection means nobody
    below unrestricted clients sees it — restrictive by construction, never silently open."""
    sets = [set(a) for a in member_acls if a is not None]
    if not sets:
        return None
    out = set.intersection(*sets)
    return sorted(out)


def visible_to_view(row_acl: list[str] | None, view_acl: list[str] | None) -> bool:
    """Whether a row from a GOVERNED BUT NON-MEMBER source (a backlink page, for instance) may
    render on a view. The rule: *no string derived from a governed source may render on a view
    whose audience is not a subset of that source's audience.* `view_acl` (the intersection,
    above) already computes
    the view's OWN audience from its MEMBERS only; this is a separate read gate applied to
    every OTHER governed source a view's skeleton feeds from — never folded into the
    intersection itself, because a non-member source must never NARROW the view, only be
    excluded from rendering on it.

    `row_acl is None` (open) is visible on any view, open or narrowed alike — an unrestricted
    source cannot leak anything a broader audience could not already read elsewhere. Otherwise: an
    OPEN view (`view_acl is None`, visible to unrestricted clients too) may include ONLY
    equally open rows, because a restricted row rendered there would reach an audience its own
    author restricted it from; a NARROWED view may include a restricted row only when the row's
    own audience covers the view's WHOLE audience (`set(view_acl) <= set(row_acl)`) — every
    client that can read the view can also read the row."""
    if row_acl is None:
        return True
    if view_acl is None:
        return False
    return set(view_acl) <= set(row_acl)
