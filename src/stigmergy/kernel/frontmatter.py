"""Frontmatter parsing — the read half of the page contract.

The write half lives in `kernel.page` (`_yaml`). Nothing else belongs here: entity pages are
minted by `stigmergy.entities` through the governed door, and links are computed once at
index-build time.
"""
import re

import yaml

# `\r?\n`, the same as `index.corpus`'s own block matcher and for the same reason: a CRLF checkout
# is not a malformed page. Anchored on bare `\n`, this matched NOTHING on a page written on Windows
# or normalized by a `.gitattributes` rule, so a well-formed page read as frontmatter-less — and
# `entities.generator` reads entity pages through here, so it refused them with "declares no
# `title`, so it names no entity", blocking `stigmergy-entities regenerate` and the governed mint
# door behind it. The two parsers are twins; when one learns something the other has to.
_FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", re.S)


def split_frontmatter(text: str):
    """(frontmatter dict, body). If it doesn't parse, frontmatter = {} and body = full text."""
    if text.startswith("---"):
        m = _FRONTMATTER_RE.match(text)
        if m:
            try:
                return (yaml.safe_load(m.group(1)) or {}), m.group(2)
            except yaml.YAMLError:
                return {}, text
    return {}, text
