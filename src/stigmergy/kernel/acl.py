"""ACL resolution — audience labels derived deterministically from the source path.

Who may see a document is a property of WHERE it lives. The mapping is config, never an LLM
decision: `{"default": [...], "rules": [{<matcher>: ..., "audiences": [...]}]}`, first matching
rule wins. The resolved list lands in page frontmatter (`acl: [...]`); pages without `acl` are
visible to every client. `stigmergy.server.acl.visible()` enforces at query time.
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

    Not every caller has a file: the librarian reads `ops/acl.json` out of a commit, so its
    locator is e.g. `origin/main@abc123:ops/acl.json` — every message names `label` so an
    operator is never sent to edit a temp file that does not exist. One validator for both
    entry points, so they cannot drift about what a malformed config is.
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
    """Labels can be CSV-serialized downstream: a comma inside one would silently split into two
    audiences at enforcement time, and an empty label would vanish in the same round-trip."""
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
    """A view's audience is the INTERSECTION of its members' — a rollup must never widen access.
    Members without ACLs don't restrict; all-None -> None (open). An empty intersection is
    restrictive by construction, never silently open."""
    sets = [set(a) for a in member_acls if a is not None]
    if not sets:
        return None
    out = set.intersection(*sets)
    return sorted(out)


def visible_to_view(row_acl: list[str] | None, view_acl: list[str] | None) -> bool:
    """Whether a row from a governed NON-MEMBER source (a backlink page, say) may render on a
    view. Kept separate from `view_acl`: a non-member source must never NARROW the view, only be
    excluded from rendering on it. Truth table: an open row (None) renders anywhere; an open view
    admits only open rows (a restricted row there would reach an audience its author restricted
    it from); a narrowed view admits a restricted row only when `set(view_acl) <= set(row_acl)`
    — every client that can read the view can also read the row."""
    if row_acl is None:
        return True
    if view_acl is None:
        return False
    return set(view_acl) <= set(row_acl)
