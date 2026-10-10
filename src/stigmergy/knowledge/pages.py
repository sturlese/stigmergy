"""Note, concept, entity, and project page contract."""

from __future__ import annotations

import datetime as dt
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass

import yaml

from stigmergy.index.corpus import ENTITY_TYPES, split_frontmatter_checked
from stigmergy.kernel.normalize import resolution_key

ROLES = ("note", "concept", "entity", "project")
FOLDERS = {"note": "wiki/notes", "concept": "wiki/concepts", "entity": "wiki/entities",
           "project": "wiki/projects"}
KNOWLEDGE_FOLDERS = tuple(FOLDERS.values())
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
    sources: tuple[str, ...]
    body: str
    entity_type: str | None = None
    aliases: tuple[str, ...] = ()

    def name_keys(self) -> tuple[str, ...]:
        """Normalized title and aliases: the keys two entity pages may never share."""
        return tuple(dict.fromkeys(resolution_key(value) for value in (self.title, *self.aliases)))


def title_stem(title: str) -> str:
    """The filename stem a knowledge title is stored under, and so the target a wikilink uses."""
    normalized = unicodedata.normalize("NFC", " ".join(title.split()))
    return _UNSAFE_TITLE.sub("-", normalized).strip(" .-")


def page_path(role: str, title: str) -> str:
    if role not in ROLES:
        raise PageContractError("knowledge role must be note, concept, entity, or project")
    filename = title_stem(title)
    if not filename:
        raise PageContractError("page title cannot form a filename")
    if len(f"{filename}.md".encode()) > _MAX_FILENAME_BYTES:
        raise PageContractError("page title forms an oversized filename")
    return f"{FOLDERS[role]}/{filename}.md"


def path_role(path: str) -> str | None:
    """The role a knowledge path belongs to, or None outside the knowledge folders."""
    for role, folder in FOLDERS.items():
        if path.startswith(f"{folder}/") and path.endswith(".md") and "/" not in path[len(folder) + 1 :]:
            return role
    return None


def render_page(
    *,
    path: str,
    role: str,
    title: str,
    body: str,
    acl: tuple[str, ...] | None,
    sources: tuple[str, ...] = (),
    status: str = "developing",
    page_id: str | None = None,
    created: dt.date | None = None,
    updated: dt.date | None = None,
    entity_type: str | None = None,
    aliases: tuple[str, ...] = (),
) -> str:
    expected = page_path(role, title)
    if path != expected:
        raise PageContractError("page path does not match its role and title")
    if status not in STATUSES:
        raise PageContractError("invalid editorial status")
    today = dt.datetime.now(dt.UTC).date()
    clean_title = " ".join(title.split())
    metadata = {
        "id": page_id or f"page_{uuid.uuid4()}",
        "type": role,
        "title": clean_title,
        "status": status,
        "created": (created or today).isoformat(),
        "updated": (updated or today).isoformat(),
        "acl": list(acl) if acl is not None else None,
        "sources": list(dict.fromkeys(sources)),
    }
    if role == "entity":
        metadata["entity_type"] = _entity_type(entity_type)
        metadata["aliases"] = list(_aliases(aliases, title=clean_title))
    elif entity_type is not None or aliases:
        raise PageContractError("only entity pages carry an entity type or aliases")
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
    sources = _strings(metadata.get("sources"), field="sources")
    entity_type = None
    aliases: tuple[str, ...] = ()
    if role == "entity":
        entity_type = _entity_type(metadata.get("entity_type"))
        aliases = _aliases(_strings(metadata.get("aliases"), field="aliases"), title=title)
    elif "entity_type" in metadata or "aliases" in metadata:
        raise PageContractError("only entity pages carry an entity type or aliases")
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
        sources=sources,
        body=body.strip(),
        entity_type=entity_type,
        aliases=aliases,
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


def _entity_type(value) -> str:
    if value not in ENTITY_TYPES:
        raise PageContractError("entity type is invalid")
    return value


def _aliases(values, *, title: str) -> tuple[str, ...]:
    """Aliases are distinct alternate names; the title is never repeated as an alias."""
    cleaned = tuple(" ".join(value.split()) for value in values)
    keys = [resolution_key(value) for value in cleaned]
    if any(not any(character.isalnum() for character in key) for key in keys):
        raise PageContractError("aliases must contain searchable text")
    if len(set(keys)) != len(keys) or resolution_key(title) in keys:
        raise PageContractError("aliases must be unique and differ from the title")
    return cleaned


def _strings_or_none(value, *, field: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    result = _strings(value, field=field)
    if not result:
        raise PageContractError(f"{field} cannot be empty")
    return result


def render_like(page: KnowledgePage, **overrides) -> str:
    """Render a page from its parsed record, replacing only the given fields."""
    fields = {
        "path": page.path,
        "role": page.role,
        "title": page.title,
        "body": page.body,
        "acl": page.acl,
        "sources": page.sources,
        "status": page.status,
        "page_id": page.page_id,
        "created": page.created,
        "updated": page.updated,
        "entity_type": page.entity_type,
        "aliases": page.aliases,
    }
    fields.update(overrides)
    return render_page(**fields)

