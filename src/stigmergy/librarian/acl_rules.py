"""ACL label resolution for a filed page — an adapter over `kernel.acl`.

The file on disk in the knowledge repo is written in another dialect than the reader's
(`{"path": "wiki/**", "acl": []}` with `"default": []`), and `kernel.acl.load_acl_config`
raises on it — a malformed ACL config is a loud, fail-closed error by design, so without this
adapter the worker could not start against the real repo. The file cannot simply be rewritten
without changing live visibility: every current rule resolves to no labels and the reader
cannot express that (a non-empty `default` is mandatory, and its implicit `["all"]` fallback is
not "open"). What this does NOT do is reimplement resolution: it normalizes into the reader's
shape and hands off to `acl.resolve_acl` — one matching algorithm, in the module that owns it.

**Empty means open.** A resolved empty list yields `None`, and `page.py` omits the `acl:` line
entirely — the file's own stated intent and `acl.py`'s semantics for pages without `acl`.
"""
import json
import os

from stigmergy.kernel import acl as acl_model
from stigmergy.librarian.errors import LibrarianConfigError

# The on-disk dialect's `path` carries a glob (`wiki/**`); the reader matches by prefix, so the
# glob tail is stripped. Faithful for the trailing-`**` form and nothing else — anything more
# exotic is refused rather than guessed at, because guessing wrong on an access-control file is
# the one failure mode it must not have.
_GLOB_SUFFIX = "/**"


# Reader matchers the librarian cannot supply an input for: `acl.resolve_acl` is called with
# `(config, page_path, None, None)`, and the `unit`/`entity_kind` branches require a truthy
# value, so such a rule can NEVER match — it would fall through to `default`, which in the real
# `ops/acl.json` is `[]`, i.e. OPEN. A config written to restrict something must not silently
# resolve to something weaker, so it stops the worker instead.
_UNSUPPORTED_MATCHERS = ("unit", "entity_kind")


def _guard_delegation(path_label: str, call):
    """Run a `kernel.acl` call, re-raising its `ValueError` as a config error — a bare
    `ValueError` escapes `cli.main`'s handlers as a raw traceback, and every other malformed ACL
    config here produces one clean fail-closed line."""
    try:
        return call()
    except LibrarianConfigError:
        raise
    except ValueError as ex:
        raise LibrarianConfigError(f"acl config {path_label}: {ex}") from ex


def _translate_rule(path_label: str, rule: dict) -> dict:
    """One on-disk rule -> one reader rule. Raises on anything not faithfully translatable."""
    pattern = str(rule.get("path", ""))
    if not pattern:
        raise LibrarianConfigError(
            f"acl config {path_label}: rule has neither a reader matcher "
            f"({', '.join(acl_model._MATCHERS)}) nor a 'path' pattern: {sorted(rule)}")
    if "*" in pattern.removesuffix(_GLOB_SUFFIX):
        raise LibrarianConfigError(
            f"acl config {path_label}: path pattern {pattern!r} is not a plain prefix or a "
            f"'<prefix>/**' glob — the resolver matches by prefix and will not guess at a "
            f"wildcard in the middle")
    return {"path_prefix": pattern.removesuffix(_GLOB_SUFFIX), "audiences": list(rule["acl"])}


def _adopt_reader_rule(path_label: str, rule: dict) -> dict:
    """A rule that already matches in the READER's dialect, checked for the half it may be
    missing.

    Dialect detection is per KEY, so a half-migrated rule exists: `path_prefix` with `acl`
    labels. Adopting it verbatim once dropped the label list entirely — a matcher with no
    labels, which either raised `KeyError` on every item or fell through to an OPEN default, the
    one failure mode this file may not have. So: `audiences` wins if present; `acl` is
    translated when it is the only one; both present and disagreeing is refused rather than
    guessed at; neither is refused too.
    """
    adopted = {k: rule[k] for k in acl_model._MATCHERS if k in rule}
    has_audiences = isinstance(rule.get("audiences"), list)
    has_acl = isinstance(rule.get("acl"), list)
    # Compared as SETS, and the message says so: an audience list is a set everywhere it is
    # used (`resolve_acl` hands it on, `server.acl.visible()` does membership), so
    # `["a", "b"]` and `["b", "a"]` are the same rule.
    if has_audiences and has_acl and set(map(str, rule["audiences"])) != set(map(str, rule["acl"])):
        raise LibrarianConfigError(
            f"acl config {path_label}: rule {sorted(rule)} carries BOTH 'audiences' and 'acl' and "
            f"they name different audiences ({sorted(map(str, rule['audiences']))} vs "
            f"{sorted(map(str, rule['acl']))}) — say the audience once; an access-control rule "
            f"with two answers is not one this will guess at")
    if not has_audiences and not has_acl:
        raise LibrarianConfigError(
            f"acl config {path_label}: rule {sorted(rule)} matches on "
            f"{', '.join(sorted(k for k in acl_model._MATCHERS if k in rule))} but names no "
            f"audience — it has neither an 'audiences' list nor an 'acl' one, so it would resolve "
            f"to nothing at all. Give it the audiences it is meant to grant, or remove it")
    adopted["audiences"] = list(rule["audiences"] if has_audiences else rule["acl"])
    return adopted


def load(path: str | None):
    """Load the ACL config from a FILE, whichever dialect it is written in.

    Returns the reader's normalized config, or `None` when there is no file at all (open
    corpus). Raises `LibrarianConfigError` on anything malformed: called ONCE at worker startup,
    never per item. The fast lane does not use this entry point — it reads `ops/acl.json` at the
    commit it files against (`base_inputs.load_acl`); this stays for the operator tooling, whose
    only shape is a path to a checkout.
    """
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as ex:
        raise LibrarianConfigError(f"acl config {path}: unreadable ({ex})") from ex
    return load_text(text, label=path)


def load_text(text: str | None, *, label: str):
    """The same loader over CONTENT, with `label` naming where the content came from.

    The seam the base-pinned read needs: the worker reads `ops/acl.json` out of `base.sha` with
    `gitcmd.show`, so it holds bytes and a locator rather than a path — and an ACL config
    resolved from the working tree while the page is committed against another tree can stamp
    audience labels that do not match the commit they land in. `None` content means the commit
    carries no such file: an open corpus. Everything else fails closed loudly — a silently-open
    access-control file is the one failure mode this may not have.
    """
    if text is None:
        return None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as ex:
        raise LibrarianConfigError(f"acl config {label}: unreadable ({ex})") from ex
    if not isinstance(raw, dict):
        raise LibrarianConfigError(f"acl config {label}: top level must be an object")

    rules = raw.get("rules", [])
    if not isinstance(rules, list):
        raise LibrarianConfigError(f"acl config {label}: 'rules' must be a list")

    # Dialect detection is per rule, not per file, so a half-migrated file is still readable and
    # a rule that is neither dialect is named individually.
    translated, on_disk_dialect = [], False
    for rule in rules:
        if not isinstance(rule, dict):
            raise LibrarianConfigError(f"acl config {label}: every rule must be an object")
        unsupported = sorted(k for k in _UNSUPPORTED_MATCHERS if k in rule)
        if unsupported:
            raise LibrarianConfigError(
                f"acl config {label}: rule {sorted(rule)} matches on {', '.join(unsupported)}, "
                f"which the librarian cannot supply when it files a page — such a rule can never "
                f"match, so it would silently fall through to the default. Rewrite it as a "
                f"'path_prefix' rule, or remove it")
        if any(k in rule for k in acl_model._MATCHERS):
            # A rule whose LABELS still come from `acl` is half-migrated, not pure reader
            # dialect: the delegation below re-validates the original TEXT with the reader's own
            # loader, which requires `audiences` on every rule and would refuse this very rule.
            if not isinstance(rule.get("audiences"), list):
                on_disk_dialect = True
            translated.append(_adopt_reader_rule(label, rule))
            continue
        if not isinstance(rule.get("acl"), list):
            raise LibrarianConfigError(
                f"acl config {label}: rule {sorted(rule)} has no 'audiences' and no 'acl' list")
        on_disk_dialect = True
        translated.append(_translate_rule(label, rule))

    default = raw.get("default", ["all"])
    if not isinstance(default, list):
        raise LibrarianConfigError(f"acl config {label}: 'default' must be a list")

    if not on_disk_dialect and all(r.get("audiences") for r in translated) and default:
        # Pure reader dialect and nothing empty: let the module that owns the format validate
        # it, so its label rules apply unchanged — through its TEXT entry point, so this adapter
        # never needs a file the caller may not have.
        return _guard_delegation(
            label, lambda: acl_model.load_acl_config_text(text, label=label))

    # Mixed or empty-label config: validate the labels ourselves with the module's own checker,
    # then hand back the normalized shape `resolve_acl` consumes. Empty audience lists are
    # allowed HERE and nowhere else — they are how the on-disk file spells "open".
    def _check_every_label():
        for rule in translated:
            acl_model._check_labels(label, rule.get("audiences", []))
        acl_model._check_labels(label, default)

    _guard_delegation(label, _check_every_label)
    return {"default": [str(a) for a in default],
            "rules": [{k: r[k] for k in (*acl_model._MATCHERS, "audiences") if k in r}
                      for r in translated]}


def resolve(config, page_path: str) -> list[str] | None:
    """Audience labels for a page at `page_path`, or `None` when it carries no `acl:` line.

    `None` both when ACLs are off entirely and when the matching rule resolves to no labels —
    from the page's point of view those are the same thing, and the page contract has one way to
    say it: omit the field.
    """
    labels = acl_model.resolve_acl(config, page_path, None, None)
    return list(labels) if labels else None
