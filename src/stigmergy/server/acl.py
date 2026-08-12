"""The one visibility rule — the ACL enforcement primitive the whole service filters through.

The live stored shape is `pages_index.acl`, a Postgres `text[]` (list or None); a bare CSV string
also normalizes, kept so an unexpected stored shape fails closed rather than crashing.

Truth table (page acl × client audiences), fail-closed:
  - `None`       (no acl)            → open to everyone.
  - `[]` / `""`  (empty acl, nobody) → open to an UNRESTRICTED client, hidden from any scoped one.
  - `[labels…]`  (scoped)            → visible iff the client shares a label; unrestricted sees all.
  - malformed    (anything else)     → visible to NOBODY, not even unrestricted, and logged loudly.

Malformed is a hard hide even for unrestricted clients: a value we cannot trust must never
resolve to "open" at the one place access is decided.
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
        # any non-string element fails closed: str()-coercing it would forge a garbage label
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
    """The predicate for text composed from MORE than one page's identity: the scoping key's shape
    must match the composition's shape, so this is all-or-nothing, never per-path — a partial
    scrub is the kind of defense that looks complete and is not. An EMPTY `paths` is never "all
    visible". `visible_paths` is the caller's own exists-and-visible answer (any `in`-container)."""
    return bool(paths) and all(p in visible_paths for p in paths)
