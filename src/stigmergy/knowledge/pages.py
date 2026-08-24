"""Target note and concept page contract."""

from __future__ import annotations

import datetime as dt
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass

import yaml

from stigmergy.index.corpus import split_frontmatter_checked

ROLES = ("note", "concept")
STATUSES = ("seed", "developing", "mature", "evergreen")
_UNSAFE_TITLE = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|\[\]`#]+")
_MAX_FILENAME_BYTES = 255


class PageContractError(ValueError):
    pass


@dataclass(frozen=True)
class KnowledgePage:
    path: str
    page_id: str
    role: str
    title: str
    status: str
    created: dt.date
    updated: dt.date
    acl: tuple[str, ...] | None
    entities: tuple[str, ...]
    sources: tuple[str, ...]
    body: str


def page_path(role: str, title: str) -> str:
    if role not in ROLES:
        raise PageContractError("knowledge role must be note or concept")
    normalized = unicodedata.normalize("NFC", " ".join(title.split()))
    filename = _UNSAFE_TITLE.sub("-", normalized).strip(" .-")
    if not filename:
        raise PageContractError("page title cannot form a filename")
    if len(f"{filename}.md".encode()) > _MAX_FILENAME_BYTES:
        raise PageContractError("page title forms an oversized filename")
    folder = "notes" if role == "note" else "concepts"
    return f"wiki/{folder}/{filename}.md"


def render_page(
    *,
    path: str,
    role: str,
    title: str,
    body: str,
    acl: tuple[str, ...] | None,
    entities: tuple[str, ...] = (),
    sources: tuple[str, ...] = (),
    status: str = "developing",
    page_id: str | None = None,
    created: dt.date | None = None,
    updated: dt.date | None = None,
) -> str:
    expected = page_path(role, title)
    if path != expected:
        raise PageContractError("page path does not match its role and title")
    if status not in STATUSES:
        raise PageContractError("invalid editorial status")
    today = dt.datetime.now(dt.UTC).date()
    metadata = {
        "id": page_id or f"page_{uuid.uuid4()}",
        "type": role,
        "title": " ".join(title.split()),
        "status": status,
        "created": (created or today).isoformat(),
        "updated": (updated or today).isoformat(),
        "acl": list(acl) if acl is not None else None,
        "entity": list(dict.fromkeys(entities)),
        "sources": list(dict.fromkeys(sources)),
    }
    yaml_text = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False, width=1000).rstrip()
    normalized_body = body.strip()
    if not normalized_body:
        raise PageContractError("page body is empty")
    if not normalized_body.startswith("# "):
        normalized_body = f"# {metadata['title']}\n\n{normalized_body}"
    return f"---\n{yaml_text}\n---\n\n{normalized_body}\n"


def parse_page(path: str, text: str) -> KnowledgePage:
    metadata, body, malformed = split_frontmatter_checked(text)
    if malformed or not metadata:
        raise PageContractError("page frontmatter is invalid")
    role = metadata.get("type")
    title = metadata.get("title")
    status = metadata.get("status")
    if role not in ROLES or not isinstance(title, str) or status not in STATUSES:
        raise PageContractError("page role, title, or status is invalid")
    if path != page_path(role, title):
        raise PageContractError("page path does not match its role and title")
    acl = _strings_or_none(metadata.get("acl"), field="acl")
    entities = _strings(metadata.get("entity"), field="entity")
    sources = _strings(metadata.get("sources"), field="sources")
    try:
        created = dt.date.fromisoformat(str(metadata["created"]))
        updated = dt.date.fromisoformat(str(metadata["updated"]))
    except (KeyError, ValueError) as error:
        raise PageContractError("page dates are invalid") from error
    page_id = str(metadata.get("id") or "").strip()
    if not page_id or not body.strip():
        raise PageContractError("page id or body is missing")
    return KnowledgePage(
        path=path,
        page_id=page_id,
        role=role,
        title=" ".join(title.split()),
        status=status,
        created=created,
        updated=updated,
        acl=acl,
        entities=entities,
        sources=sources,
        body=body.strip(),
    )


def read_page(root: str, path: str) -> KnowledgePage:
    full = os.path.join(root, *path.split("/"))
    return parse_page(path, open(full, encoding="utf-8").read())


def _strings(value, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise PageContractError(f"{field} must be a string list")
    cleaned = tuple(item.strip() for item in value)
    if len(set(cleaned)) != len(cleaned):
        raise PageContractError(f"{field} contains duplicates")
    return cleaned


def _strings_or_none(value, *, field: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    result = _strings(value, field=field)
    if not result:
        raise PageContractError(f"{field} cannot be empty")
    return result
