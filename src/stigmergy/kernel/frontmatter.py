"""Frontmatter parsing — the read half of the page contract.

The write half lives in `kernel.page` (`_yaml`). Nothing else belongs here: entity pages are
minted by `stigmergy.entities` through the governed door, and links are computed once at
index-build time.
"""
import re

import yaml


def split_frontmatter(text: str):
    """(frontmatter dict, body). If it doesn't parse, frontmatter = {} and body = full text."""
    if text.startswith("---"):
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
        if m:
            try:
                return (yaml.safe_load(m.group(1)) or {}), m.group(2)
            except yaml.YAMLError:
                return {}, text
    return {}, text
