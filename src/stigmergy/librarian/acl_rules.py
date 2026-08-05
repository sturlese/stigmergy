"""ACL label resolution for a filed page — an adapter over `kernel.acl`.

**Why an adapter and not a direct call.** `kernel.acl.load_acl_config` reads one dialect —
"`path_prefix` / `unit` / `entity_kind` → audiences; first match wins; explicit default". The
file actually on disk in the knowledge repo is written in another — `{"path": "wiki/**",
"acl": []}` with `"default": []` — and loading it raises. A malformed ACL config is a loud,
fail-closed error by design, so without this adapter the worker would refuse to start against the
real repo and nothing could ever be filed.

The file cannot simply be rewritten without changing live visibility: every current rule
resolves to no labels and the reader cannot express that (a non-empty `default` is mandatory,
and its implicit `["all"]` fallback is not "open" — it would hide pages from the one
audience-scoped identity that exists).

**What it does NOT do**: reimplement resolution. It normalizes the on-disk dialect into the
reader's shape and hands off to `acl.resolve_acl` — one matching algorithm, in the module that
already owns it and already has the tests. The adapter lives here rather than in the kernel so
the blast radius is one subsystem: the kernel's own contract is untouched.

**Empty means open.** A resolved empty list yields `None`, and `page.py` omits the `acl:` line
entirely. That is the file's own stated intent ("Absent acl = open") and matches `acl.py`'s
docstring: "pages without `acl` are visible to every client."
"""
import json
import os

from stigmergy.kernel import acl as acl_model
from stigmergy.librarian.errors import LibrarianConfigError

# The on-disk dialect's keys, and what each maps to in the reader's dialect. `path` carries a
# glob (`wiki/**`); the reader matches by prefix, so the glob tail is stripped. That is a
# faithful translation for the trailing-`**` form and nothing else — anything more exotic is
# refused rather than guessed at, because guessing wrong on an access-control file is the one
# failure mode it must not have.
_GLOB_SUFFIX = "/**"


# Reader matchers the librarian cannot supply an input for. `acl.resolve_acl` is called with
# `(config, page_path, None, None)` — there is no unit and no entity kind at filing time — and its
# `unit`/`entity_kind` branches require a truthy value, so such a rule can NEVER match. It would
# fall through to `default`, which in the real `ops/acl.json` is `[]`, i.e. OPEN. A config written
# to restrict something must not silently resolve to something weaker than intended, so it stops
# the worker instead.
_UNSUPPORTED_MATCHERS = ("unit", "entity_kind")


def _guard_delegation(path_label: str, call):
    """Run a `kernel.acl` call, re-raising its `ValueError` as a config error.

    The delegated label validation raises a bare `ValueError`, which is neither
    `LibrarianConfigError` nor even a `LibrarianError` — so `cli.main()`, which catches those,
    let it through as a raw traceback. Every other malformed ACL config in this module produces one
    clean fail-closed line; a comma in an audience label produced a stack trace. Same defect class
    as an interrupt answered with a traceback, at a different seam.
    """
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


def load(path: str | None):
    """Load the ACL config from a FILE, whichever dialect it is written in.

    Returns the reader's normalized config, or `None` when there is no file at all (open
    corpus — `acl.py`'s own semantics for a missing config). Raises `LibrarianConfigError` on
    anything malformed: called ONCE at worker startup, never per item.

    The fast lane does not use this entry point — it reads `ops/acl.json` at the commit it files
    against (`base_inputs.load_acl`). This one stays because "the config as it sits in a checkout"
    is a real question for the steward tooling that edits that file, and because reading a path is
    the only shape a caller with a file has.
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

    This is the seam the base-pinned read needs. The worker reads `ops/acl.json` out of `base.sha`
    with `gitcmd.show`, so it holds bytes and a locator (`origin/main@abc123def456:ops/acl.json`)
    rather than a path — and an ACL config resolved from the working tree while the page is
    committed against another tree can stamp audience labels that do not match the commit they
    land in.

    `None` content means the commit carries no such file, which is the same thing a missing file
    means: an open corpus. Everything else — unreadable, wrong shape, an unsupported matcher, an
    invalid label — is the same loud fail-closed refusal it has always been, because a
    silently-open access-control file is the one failure mode this may not have.
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
            translated.append(dict(rule))
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
        # Pure reader dialect and nothing empty: let the module that owns the format validate it,
        # so its label rules (no commas, no blanks) apply unchanged. Through its TEXT entry point,
        # so this adapter never needs a file the caller may not have.
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

    `None` is returned both when ACLs are off entirely and when the matching rule resolves to no
    labels — from the page's point of view those are the same thing, and the page contract has
    one way to say it: omit the field.
    """
    labels = acl_model.resolve_acl(config, page_path, None, None)
    return list(labels) if labels else None
