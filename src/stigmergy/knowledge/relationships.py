"""Local source attribution parsing shared by the writer and its gates."""

from __future__ import annotations

import re

_SOURCE_ATTRIBUTION = re.compile(
    r"\(Source: `(?P<path>sources/\d{4}/\d{2}/[^`\n]+\.md)`\)"
)


def source_attributions(body: str) -> frozenset[str]:
    """Return the canonical local sources cited by a page body."""
    return frozenset(match["path"] for match in _SOURCE_ATTRIBUTION.finditer(body))
