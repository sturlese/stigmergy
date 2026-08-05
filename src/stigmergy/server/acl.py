"""The one visibility rule — the ACL enforcement primitive the whole service filters through.

The stored shape every live caller passes is `pages_index.acl`, a Postgres `text[]` column (a
Python list, or None). A bare CSV string (`"sales,leadership"`) is still normalized correctly
below; no caller in this codebase produces that shape, and the branch stays because accepting it
costs nothing and a future stored `acl` shape should fail closed rather than crash. Both normalize
to a set of audience labels here, so enforcement is written once and reused by search, read_page
and the discovery hints — no per-surface ad-hoc filtering.

Truth table (page acl × client audiences), fail-closed:
  - `None`       (no acl)            → open to everyone.
  - `[]` / `""`  (empty acl, nobody) → open to an UNRESTRICTED client, hidden from any scoped one.
  - `[labels…]`  (scoped)            → visible iff the client shares a label; unrestricted sees all.
  - malformed    (anything else)     → visible to NOBODY, not even unrestricted, and logged
                                       loudly (ADR 012's fail-closed parse).

A *malformed* stored value is a hard hide even for unrestricted clients — stricter than treating
it as absent — because a value we cannot trust must never resolve to "open" at the one place
access is decided.
"""
import logging

log = logging.getLogger(__name__)

_OPEN = object()        # the page carries no acl at all → visible to everyone
_MALFORMED = object()   # a stored value we cannot parse → visible to nobody (fail closed)


def _labels(acl) -> object:
    """Normalize a stored acl to a set of labels, `_OPEN`, or `_MALFORMED`."""
    if acl is None:
        return _OPEN
    if isinstance(acl, (list, tuple)):
        # a list/tuple is the trusted shape (postgres `text[]`), but only if EVERY element is a
        # string: a non-string element (`[{"a": 1}]`, `[None]`) is a value we cannot trust, and
        # str()-coercing it would forge a garbage label visible to unrestricted clients. Fail closed.
        if not all(isinstance(a, str) for a in acl):
            return _MALFORMED
        return {s for s in (a.strip() for a in acl) if s}
    if isinstance(acl, str):                       # CSV: "sales,leadership" (or "" = nobody)
        return {s for s in (p.strip() for p in acl.split(",")) if s}
    return _MALFORMED                              # dict / bool / int / … → cannot be trusted


def visible(acl, audiences: set[str] | None) -> bool:
    """Whether a client with `audiences` (None = unrestricted) may see content labelled `acl`."""
    labels = _labels(acl)
    if labels is _MALFORMED:
        log.warning("acl fail-closed: malformed stored acl %r — hidden from every client", acl)
        return False
    if labels is _OPEN:
        return True
    if audiences is None:                          # unrestricted client sees any non-malformed page
        return True
    return bool(labels & set(audiences))           # empty acl → empty intersection → hidden


def all_visible(paths, visible_paths) -> bool:
    """The "every named page visible, or the whole composed unit is unsafe to show" predicate for
    text built from MORE than one page's identity (a multi-page filing, a finding that names a
    second or third page) — reused rather than each caller inventing its own version.
    `gardener.notice.scope_findings_to_channel` once keyed its redaction on a SINGLE page while
    the text it protected was composed from a LIST, which is exactly the shape
    `digest.sections._corrections_filed` already had to get right for a multi-page filing: the
    scoping key's shape must match the composition's shape, and this function is the one place
    that rule is written.

    `paths` may repeat or include a path this call has not resolved to a title/acl at all
    (`visible_paths` is the caller's own "and it actually exists, and it's visible" answer — a
    plain `set`/`dict` `in` supports either shape a caller already has on hand). A partial scrub —
    some of several named pages visible, others not — is exactly the kind of defense that looks
    complete and is not, so this is all-or-nothing, never per-path. An EMPTY `paths` list is never
    "all visible" — nothing to name is not itself a visible fact worth composing text about."""
    return bool(paths) and all(p in visible_paths for p in paths)
