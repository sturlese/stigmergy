"""Master operations over entity pages: merge, rename, and the shared name index.

An entity is an ordinary librarian-owned page; the wikilink is its only relation. These operations
are the only writers that move or fold entity pages, and each rewrites every link in the same
worktree so the corpus never carries a dangling entity reference.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path

from stigmergy.capture.schema import EntityMergeEvidence
from stigmergy.index.corpus import split_frontmatter_checked
from stigmergy.kernel.normalize import resolution_key
from stigmergy.knowledge.pages import (
    KNOWLEDGE_FOLDERS,
    KnowledgePage,
    PageContractError,
    page_path,
    parse_page,
    render_page,
)


class EntityOperationError(RuntimeError):
    pass


def entity_pages(root: str) -> dict[str, KnowledgePage]:
    folder = Path(root, "wiki", "entities")
    pages: dict[str, KnowledgePage] = {}
    if not folder.is_dir():
        return pages
    for path in sorted(folder.glob("*.md")):
        relative = path.relative_to(root).as_posix()
        pages[relative] = parse_page(relative, path.read_text(encoding="utf-8"))
    return pages


def name_index(pages: dict[str, KnowledgePage]) -> dict[str, tuple[str, ...]]:
    """Every normalized title or alias mapped to the entity pages that claim it."""
    index: dict[str, list[str]] = {}
    for path, page in pages.items():
        for key in page.name_keys():
            index.setdefault(key, []).append(path)
    return {key: tuple(paths) for key, paths in index.items()}


def find_entity(pages: dict[str, KnowledgePage], reference: str) -> KnowledgePage | None:
    """Resolve a path, title, or alias to exactly one entity page."""
    if reference in pages:
        return pages[reference]
    matches = name_index(pages).get(resolution_key(reference), ())
    return pages[matches[0]] if len(matches) == 1 else None


def merge_entities(
    root: str,
    paths: tuple[str, ...],
    *,
    at: dt.datetime,
    evidence: EntityMergeEvidence,
    source_roots: tuple[str, ...] = (),
) -> str:
    """Fold entity pages into the first one listed and return its path."""
    pages = entity_pages(root)
    selected = list(dict.fromkeys(paths))
    if len(selected) < 2 or any(path not in pages for path in selected):
        raise EntityOperationError("merge requires distinct existing entity pages")
    chosen = tuple(pages[path] for path in selected)
    if len({page.entity_type for page in chosen}) != 1:
        raise EntityOperationError("entities with different types cannot be merged")
    if len({page.acl for page in chosen}) != 1:
        raise EntityOperationError("entities with different visibility cannot be merged")
    _verify_merge_evidence(source_roots or (root,), chosen, evidence)
    canonical = chosen[0]
    body = canonical.body
    aliases = list(canonical.aliases)
    sources = list(canonical.sources)
    links: dict[str, str] = {}
    for page in chosen:
        if page.path == canonical.path:
            continue
        body = f"{body.rstrip()}\n\n## Formerly {page.title}\n\n{_without_heading(page.body)}"
        for name in (page.title, *page.aliases):
            if resolution_key(name) not in {resolution_key(value) for value in (canonical.title, *aliases)}:
                aliases.append(name)
        sources.extend(source for source in page.sources if source not in sources)
        links[Path(page.path).stem] = Path(canonical.path).stem
        os.unlink(os.path.join(root, *page.path.split("/")))
    _write(root, canonical, body=body, aliases=tuple(aliases), sources=tuple(sources), updated=at.date())
    rewrite_links(root, links)
    return canonical.path


def rename_entity(root: str, path: str, new_title: str, *, at: dt.datetime, rewrite: bool = True) -> str:
    """Move an entity page to its new title, keeping the old title as an alias."""
    pages = entity_pages(root)
    page = pages.get(path)
    if page is None:
        raise EntityOperationError("entity page does not exist")
    try:
        new_path = page_path("entity", new_title)
    except PageContractError as error:
        raise EntityOperationError(str(error)) from error
    if new_path != path and Path(root, *new_path.split("/")).exists():
        raise EntityOperationError("an entity page already uses that title")
    new_key = resolution_key(new_title)
    if any(other != path for other in name_index(pages).get(new_key, ())):
        raise EntityOperationError("another entity already claims that name")
    aliases = tuple(
        value for value in (*page.aliases, page.title) if resolution_key(value) != new_key
    )
    aliases = tuple(dict.fromkeys(aliases))
    if new_path != path:
        os.unlink(os.path.join(root, *path.split("/")))
    _write(root, page, path=new_path, title=" ".join(new_title.split()), aliases=aliases, updated=at.date())
    if new_path != path and rewrite:
        rewrite_links(root, {Path(path).stem: Path(new_path).stem})
    return new_path


_WIKILINK_RE = re.compile(r"!?\[\[([^\[\]]+?)\]\]")


def rewrite_links(root: str, stems: dict[str, str]) -> tuple[str, ...]:
    """Point every wikilink at a moved stem to its new stem, keeping the shown text."""
    if not stems:
        return ()
    folded = {old.casefold(): new for old, new in stems.items()}

    def rewrite(match):
        target, separator, label = match.group(1).partition("|")
        name, anchor_mark, anchor = target.partition("#")
        cleaned = name.strip().removesuffix(".md")
        replacement = folded.get(cleaned.casefold())
        if replacement is None or replacement == cleaned:
            return match.group(0)
        embed = "!" if match.group(0).startswith("!") else ""
        shown = label if separator else cleaned
        return f"{embed}[[{replacement}{anchor_mark}{anchor}|{shown}]]"

    changed = []
    for folder in KNOWLEDGE_FOLDERS:
        base = Path(root, *folder.split("/"))
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            metadata, body, malformed = split_frontmatter_checked(text)
            if malformed or not metadata:
                continue
            rewritten = _WIKILINK_RE.sub(rewrite, body)
            if rewritten == body:
                continue
            path.write_text(text[: len(text) - len(body)] + rewritten, encoding="utf-8")
            changed.append(path.relative_to(root).as_posix())
    return tuple(changed)


def _write(
    root: str,
    page: KnowledgePage,
    *,
    path: str | None = None,
    title: str | None = None,
    body: str | None = None,
    aliases: tuple[str, ...] | None = None,
    sources: tuple[str, ...] | None = None,
    updated: dt.date | None = None,
) -> None:
    target = path or page.path
    Path(root, *target.split("/")).write_text(
        render_page(
            path=target,
            role="entity",
            title=title or page.title,
            body=body if body is not None else page.body,
            acl=page.acl,
            sources=sources if sources is not None else page.sources,
            status=page.status,
            page_id=page.page_id,
            created=page.created,
            updated=updated or page.updated,
            entity_type=page.entity_type,
            aliases=aliases if aliases is not None else page.aliases,
        ),
        encoding="utf-8",
    )


def _without_heading(body: str) -> str:
    lines = body.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


_IDENTITY_KIND = r"(?:entity|identity|person|organization|organisation|company|account)"
_IDENTITY_KIND_PLURAL = r"(?:entities|identities|people|persons|organizations|organisations|companies|accounts)"
_IDENTITY_LIST_RE = re.compile(
    rf"^(?P<subject>.+?) (?:"
    rf"are (?:the same {_IDENTITY_KIND}"
    rf"|aliases? for the same {_IDENTITY_KIND}"
    rf"|duplicate(?:d)? (?:[a-z0-9&+_-]+ )?{_IDENTITY_KIND_PLURAL})"
    rf"|refer to the same {_IDENTITY_KIND})$"
)
_IDENTITY_PAIR_RE = re.compile(
    rf"^(?P<left>.+?) (?:"
    rf"is (?:an? )?alias of"
    rf"|is another name for"
    rf"|is the same {_IDENTITY_KIND} as"
    rf"|refers to the same {_IDENTITY_KIND} as) (?P<right>.+)$"
)
_WORD_RE = re.compile(r"[a-z0-9]+(?:[&+_-][a-z0-9]+)*")


def _verify_merge_evidence(
    roots: tuple[str, ...],
    pages: tuple[KnowledgePage, ...],
    evidence: EntityMergeEvidence,
) -> None:
    for item in evidence.source_assertions:
        body = _read_first_source(roots, item.path)
        assertion = " ".join(item.assertion.split())
        readable = " ".join(body.split())
        if assertion not in readable:
            raise EntityOperationError("merge assertion is not present in the cited source")
        if not _assertion_binds_pages(assertion, pages):
            raise EntityOperationError("merge assertion does not unambiguously equate every selected entity")


def _read_first_source(roots: tuple[str, ...], relative: str) -> str:
    """A merge may cite a source of its own layer or of `public`."""
    for root in roots[:-1]:
        if Path(root, *relative.split("/")).is_file():
            return _read_immutable_source(root, relative)
    return _read_immutable_source(roots[-1], relative)


def _read_immutable_source(root: str, relative: str) -> str:
    root_path = Path(root).resolve()
    path = root_path.joinpath(*relative.split("/"))
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise EntityOperationError("source does not exist") from error
    if not resolved.is_relative_to(root_path) or not resolved.is_file() or path.is_symlink():
        raise EntityOperationError("source must be a regular repository file")
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise EntityOperationError("source could not be read") from error
    metadata, body, malformed = split_frontmatter_checked(text)
    if (
        malformed
        or metadata.get("type") != "source"
        or str(metadata.get("id") or "") != resolved.stem
    ):
        raise EntityOperationError("path is not an immutable source")
    return body


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(_WORD_RE.findall(resolution_key(value)))


def _name_sequences(page: KnowledgePage) -> tuple[tuple[str, ...], ...]:
    values = tuple(dict.fromkeys(_tokens(name) for name in (page.title, *page.aliases)))
    return tuple(value for value in values if value)


def _sequence_inside(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if len(left) > len(right):
        left, right = right, left
    return any(right[index : index + len(left)] == left for index in range(len(right) - len(left) + 1))


def _list_subject_binds(subject: str, pages: tuple[KnowledgePage, ...]) -> bool:
    words = _tokens(subject)
    candidates = []
    for page in pages:
        matches = []
        for name in _name_sequences(page):
            for start in range(len(words) - len(name) + 1):
                if words[start : start + len(name)] == name:
                    matches.append((start, start + len(name), name))
        if not matches:
            return False
        candidates.append(tuple(dict.fromkeys(matches)))
    order = sorted(range(len(pages)), key=lambda index: len(candidates[index]))
    states = 0

    def assign(offset: int, used: frozenset[int], names: tuple[tuple[str, ...], ...]) -> bool:
        nonlocal states
        states += 1
        if states > 10_000:
            return False
        if offset == len(order):
            remaining = [word for index, word in enumerate(words) if index not in used]
            return bool(remaining) and all(word == "and" for word in remaining)
        for start, end, name in candidates[order[offset]]:
            positions = frozenset(range(start, end))
            if positions & used or any(_sequence_inside(name, other) for other in names):
                continue
            if assign(offset + 1, used | positions, (*names, name)):
                return True
        return False

    return assign(0, frozenset(), ())


def _pair_subject_binds(left: str, right: str, pages: tuple[KnowledgePage, ...]) -> bool:
    if len(pages) != 2:
        return False
    sides = (_tokens(left), _tokens(right))
    if not all(sides):
        return False
    for first, second in ((pages[0], pages[1]), (pages[1], pages[0])):
        if (
            sides[0] in _name_sequences(first)
            and sides[1] in _name_sequences(second)
            and not _sequence_inside(sides[0], sides[1])
        ):
            return True
    return False


def _assertion_binds_pages(assertion: str, pages: tuple[KnowledgePage, ...]) -> bool:
    normalized = " ".join(_tokens(assertion))
    listed = _IDENTITY_LIST_RE.fullmatch(normalized)
    if listed is not None:
        return _list_subject_binds(listed.group("subject"), pages)
    paired = _IDENTITY_PAIR_RE.fullmatch(normalized)
    return bool(paired and _pair_subject_binds(paired.group("left"), paired.group("right"), pages))
