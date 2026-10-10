"""Audience containment for knowledge writes."""


def flows_into(content_acl: list[str] | None, page_acl: list[str] | None) -> bool:
    """Return whether every reader of ``page_acl`` may read ``content_acl``."""
    if content_acl is None:
        return True
    if page_acl is None:
        return False
    return set(page_acl) <= set(content_acl)
