"""Parse the canonical Markdown corpus into derived search rows."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from stigmergy.index.errors import StigmergyIndexError

ZONES = ("wiki", "sources")
WIKILINK_RE = re.compile(r"!?\[\[([^\[\]]+?)\]\]")
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_WIKI_PATH_RE = re.compile(r"^wiki/(?P<folder>notes|concepts)/[^/]+\.md$")
_SOURCE_PATH_RE = re.compile(
    r"^sources/\d{4}/(?:0[1-9]|1[0-2])/[0-9a-f]{8}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.md$"
)


class CorpusContractError(StigmergyIndexError):
    pass


@dataclass
class PageRow:
    path: str
    zone: str
    page_id: str
    title: str
    body: str
    type: str
    status: str = ""
    entity: list[str] = field(default_factory=list)
    updated: str = ""
    acl: list[str] | None = None
    inlinks: int = 0
    content_hash: str = ""
    links: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def embed_text(self) -> str:
        return f"{self.title}\n{self.body}"


_FRONTMATTER_RE = re.compile(r"^﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)$", re.S)
_OPENS_FRONTMATTER_RE = re.compile(r"^﻿?---[ \t]*\r?\n")
_DECLARES_ACL_RE = re.compile(r"""(?m)(?:^|[,{][ \t]*)['"]?acl['"]?[ \t]*:""")


def _asked_for_an_audience(after_opener: str) -> bool:
    block = after_opener.replace("\r\n", "\n").partition("\n\n")[0]
    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError:
        return bool(_DECLARES_ACL_RE.search(block))
    return isinstance(parsed, dict) and "acl" in parsed


def split_frontmatter_checked(text: str) -> tuple[dict, str, bool]:
    match = _FRONTMATTER_RE.match(text)
    if match:
        try:
            metadata = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            return {}, text, True
        if metadata is None:
            return {}, match.group(2), False
        if not isinstance(metadata, dict):
            return {}, match.group(2), True
        return metadata, match.group(2), False
    opener = _OPENS_FRONTMATTER_RE.match(text)
    malformed = bool(opener) and _asked_for_an_audience(text[opener.end() :])
    return {}, text, malformed


def split_frontmatter(text: str) -> tuple[dict, str]:
    metadata, body, _malformed = split_frontmatter_checked(text)
    return metadata, body


def entity_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip()))


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip()))


def _acl_labels(metadata: dict) -> list[str] | None:
    if "acl" not in metadata or metadata.get("acl") is None:
        return None
    value = metadata["acl"]
    if not isinstance(value, list):
        return []
    labels = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return list(dict.fromkeys(labels)) if len(labels) == len(value) else []


def _strip_code(text: str) -> str:
    return _INLINE_CODE_RE.sub("", _FENCE_RE.sub("", text))


def link_targets(text: str) -> list[str]:
    targets = []
    for match in WIKILINK_RE.finditer(_strip_code(text)):
        target = match.group(1).split("|")[0].split("#")[0].strip()
        if target.lower().endswith(".md"):
            target = target[:-3]
        if target:
            targets.append(target.lower())
    return targets


def content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_indexable_page(rel_path: str) -> bool:
    return bool(_WIKI_PATH_RE.fullmatch(rel_path) or _SOURCE_PATH_RE.fullmatch(rel_path))


def page_row(rel_path: str, zone: str, text: str) -> PageRow:
    if zone not in ZONES or not is_indexable_page(rel_path):
        raise CorpusContractError("path is outside the searchable corpus")
    metadata, body, malformed = split_frontmatter_checked(text)
    if malformed or not metadata:
        raise CorpusContractError("frontmatter is invalid")

    expected_type = "source"
    wiki_match = _WIKI_PATH_RE.fullmatch(rel_path)
    if wiki_match:
        expected_type = "note" if wiki_match.group("folder") == "notes" else "concept"
    if metadata.get("type") != expected_type:
        raise CorpusContractError(f"{rel_path} must have type {expected_type}")

    page_id = str(metadata.get("id") or "").strip()
    if not page_id or not body.strip():
        raise CorpusContractError("page id and body are required")
    title = str(metadata.get("title") or "").strip()
    if not title:
        title = "Captured source" if expected_type == "source" else Path(rel_path).stem
    acl = _acl_labels(metadata)
    if acl == []:
        raise CorpusContractError("acl must be null or a non-empty string list")

    status = str(metadata.get("status") or "")
    if expected_type in {"note", "concept"} and status not in {
        "seed",
        "developing",
        "mature",
        "evergreen",
    }:
        raise CorpusContractError("wiki status is invalid")
    if expected_type == "source" and status:
        raise CorpusContractError("source pages do not have editorial status")

    updated = metadata.get("updated") or metadata.get("captured_at") or ""
    return PageRow(
        path=rel_path,
        zone=zone,
        page_id=page_id,
        title=title,
        body=body,
        type=expected_type,
        status=status,
        entity=entity_list(metadata.get("entity")),
        updated=str(updated)[:10],
        acl=acl,
        content_hash=content_hash(f"{title}\n{body}"),
        links=link_targets(text),
        sources=_string_list(metadata.get("sources")),
    )


def by_stem_index(paths: list[str]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for path in paths:
        index.setdefault(Path(path).stem.lower(), []).append(path)
    return index


def resolve_links(own_path: str, stems: list[str], by_stem: dict[str, list[str]]) -> list[str]:
    resolved = {
        path
        for stem in dict.fromkeys(stems)
        for path in by_stem.get(stem, ())
        if path != own_path
    }
    return sorted(resolved)


def load_page_texts(pages: list[tuple[str, str]]) -> list[PageRow]:
    """Build canonical derived rows from repository-relative Markdown text."""
    rows = [
        page_row(path, path.split("/", 1)[0], text)
        for path, text in pages
        if is_indexable_page(path)
    ]
    rows.sort(key=lambda row: row.path)
    by_stem = by_stem_index([row.path for row in rows])
    inbound: dict[str, set[str]] = {}
    for row in rows:
        row.links = resolve_links(row.path, row.links, by_stem)
        for target in row.links:
            inbound.setdefault(target, set()).add(row.path)
    for row in rows:
        row.inlinks = len(inbound.get(row.path, ()))
    return rows


def load_pages(repo_dir: str) -> list[PageRow]:
    root = Path(repo_dir)
    pages = []
    for zone in ZONES:
        folder = root / zone
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*.md")):
            rel_path = path.relative_to(root).as_posix()
            if is_indexable_page(rel_path):
                pages.append((rel_path, path.read_text(encoding="utf-8")))
    return load_page_texts(pages)
