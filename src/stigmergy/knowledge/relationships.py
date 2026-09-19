"""Pure evidence checks for deliberate entity relationships."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection

_DASH_VARIANTS = str.maketrans({character: "-" for character in "‐‑‒–—―−"})
_SOURCE_ATTRIBUTION = re.compile(
    r"\(Source: `(?P<path>sources/\d{4}/\d{2}/[^`\n]+\.md)`\)"
)


def source_attributions(body: str) -> frozenset[str]:
    """Return the canonical local sources cited by a page body."""
    return frozenset(match["path"] for match in _SOURCE_ATTRIBUTION.finditer(body))


def has_entity_relationship_evidence(
    body: str,
    visible_name: str,
    source_paths: Collection[str],
) -> bool:
    """Require one visible name and a declared canonical source attribution nearby."""
    normalized_name = _normalized(visible_name)
    if not normalized_name:
        return False
    allowed_sources = frozenset(source_paths)
    if not allowed_sources:
        return False
    paragraphs = re.split(r"\n[ \t]*\n", body)
    name_pattern = re.compile(rf"(?<!\w){re.escape(normalized_name)}(?!\w)")
    for paragraph in paragraphs:
        if not name_pattern.search(_normalized(paragraph)):
            continue
        if _has_declared_attribution(paragraph, allowed_sources):
            return True
    return False


def _normalized(value: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", value).translate(_DASH_VARIANTS).casefold(),
    ).strip()


def _has_declared_attribution(paragraph: str, source_paths: frozenset[str]) -> bool:
    return bool(source_attributions(paragraph) & source_paths)
