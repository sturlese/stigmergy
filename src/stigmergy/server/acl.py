"""Fail-closed ACL visibility."""
import logging

log = logging.getLogger(__name__)

_OPEN = object()
_MALFORMED = object()


def _labels(acl) -> object:
    """Normalize a stored acl to a set of labels, `_OPEN`, or `_MALFORMED`."""
    if acl is None:
        return _OPEN
    if isinstance(acl, (list, tuple)):
        if not all(isinstance(a, str) for a in acl):
            return _MALFORMED
        return {s for s in (a.strip() for a in acl) if s}
    if isinstance(acl, str):
        return {s for s in (p.strip() for p in acl.split(",")) if s}
    return _MALFORMED


def visible(acl, audiences: set[str] | None) -> bool:
    """Whether a client with `audiences` (None = unrestricted) may see content labelled `acl`."""
    labels = _labels(acl)
    if labels is _MALFORMED:
        log.warning("acl fail-closed: malformed stored acl %r — hidden from every client", acl)
        return False
    if labels is _OPEN:
        return True
    if audiences is None:
        return True
    return bool(labels & set(audiences))
