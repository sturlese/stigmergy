"""Frontmatter parsing — the read half of the page contract. The write half is `kernel.page`
(`_yaml`)."""
import re

import yaml

# `\r?\n` and the optional BOM: a CRLF checkout or a BOM-writing editor is not a malformed page.
# Twin of `index.corpus`'s own block matcher — when one parser learns a tolerance, the other must.
_FRONTMATTER_RE = re.compile("^﻿?---\r?\n(.*?)\r?\n---\r?\n?(.*)$", re.S)
_OPENS_FRONTMATTER_RE = re.compile("^﻿?---")


def split_frontmatter(text: str):
    """(frontmatter dict, body). If it doesn't parse, frontmatter = {} and body = full text.

    The dict half is a promise about the TYPE, not just about parsing succeeding: valid YAML that
    is not a mapping (a list, a bare scalar) declares no fields, so it degrades to `{}` the same
    way an unparseable block does. Callers `.get()` the result immediately — handing back a list
    only moves the type error one frame away from the malformed page that caused it.
    """
    if _OPENS_FRONTMATTER_RE.match(text):
        m = _FRONTMATTER_RE.match(text)
        if m:
            try:
                loaded = yaml.safe_load(m.group(1))
            except yaml.YAMLError:
                return {}, text
            return (loaded if isinstance(loaded, dict) else {}), m.group(2)
    return {}, text
