from __future__ import annotations

import json
import re
from pathlib import Path

from stigmergy.kernel.acl import flows_into
from stigmergy.kernel.normalize import resolution_key
from stigmergy.knowledge import contradictions
from stigmergy.knowledge.pages import KNOWLEDGE_FOLDERS, parse_page
from stigmergy.knowledge.sources import (
    SourceContractError,
    read_source,
    source_file_size,
)
from stigmergy.knowledge.write_guard import WriteContext, WriteRefused, allow_existing
from stigmergy.server import roster
from stigmergy.server.acl import visible

_WORD_RE = re.compile(r"[^\W_]{2,}", re.UNICODE)
MAX_PLANNER_CONTEXT_BYTES = 768 * 1024
MAX_CATALOG_SUMMARY_CHARS = 160
MAX_CONTEXT_SOURCE_EVIDENCE = 12
MAX_CONTEXT_SOURCE_BYTES = 100_000
MAX_CONTEXT_SOURCE_REFERENCES = 64
MAX_CONTEXT_SOURCE_FILE_BYTES = 128 * 1024
MAX_CONTEXT_SOURCE_READ_BYTES = 2 * 1024 * 1024
_SOURCE_STOPWORDS = {
    "and",
    "are",
    "for",
    "from",
    "has",
    "have",
    "into",
    "not",
    "source",
    "that",
    "the",
    "this",
    "was",
    "were",
    "with",
}


def actor_scope(conn, subject: str) -> tuple[frozenset[str] | None, bool]:
    """The groups a writer job's actor holds right now, from the roster."""
    principal = roster.principal(conn, subject)
    return (
        None if principal.unrestricted else frozenset(principal.groups),
        principal.unrestricted,
    )


def filing_context(
    root: str,
    *,
    source_text: str,
    capture_acl: tuple[str, ...] | None,
    actor_groups: frozenset[str] | None,
    limit: int = 12,
    measure: dict | None = None,
) -> dict:
    """Safe existing graph context: a complete catalog plus the closest pages in full. `measure`,
    when given, receives what the context holds and how many entries the byte budget dropped."""
    folded_source = source_text.casefold()
    named_source = f" {resolution_key(source_text)} "
    terms = set(_WORD_RE.findall(folded_source))
    candidates = []
    named = []
    dated_catalog = []
    for folder in KNOWLEDGE_FOLDERS:
        base = Path(root, *folder.split("/"))
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.md")):
            relative = path.relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8")
            page = parse_page(relative, text)
            if not _admits(page.acl, actor_groups=actor_groups, capture_acl=capture_acl):
                continue
            entry = {"path": page.path, "title": page.title, "type": page.role, "summary": _summary(page.body)}
            if page.role == "entity":
                entry["entity_type"] = page.entity_type
                entry["aliases"] = list(page.aliases)
            dated_catalog.append((page.created, entry))
            overlap = len(terms & set(_WORD_RE.findall(f"{page.title}\n{page.body}".casefold())))
            candidate = {
                **{key: value for key, value in entry.items() if key != "summary"},
                "status": page.status,
                "sources": list(page.sources),
                "body": page.body,
                "filed_contradictions": _filed_contradictions(page.body),
                "capture_may_update": flows_into(_list(capture_acl), _list(page.acl)),
            }
            if page.role in ("entity", "project") and any(f" {key} " in named_source for key in page.name_keys()):
                named.append(candidate)
            elif overlap:
                candidates.append((overlap, candidate))
    # Oldest page first, ties in folder and path order: a capture filed today adds its pages at the
    # end, so consecutive captures share most of the catalog as a cached prompt prefix.
    catalog = [entry for _created, entry in sorted(dated_catalog, key=lambda item: item[0])]
    candidates.sort(key=lambda item: (-item[0], item[1]["path"]))
    # An entity or project the source names is always shown in full: ranking by shared words
    # favours long pages, and a page the model cannot see is one it can never extend.
    candidate_values = [*named, *(value for _score, value in candidates[: max(1, limit)])]
    source_evidence, source_evidence_truncated = _source_evidence(
        root,
        candidates=candidate_values,
        terms=terms,
        capture_acl=capture_acl,
        actor_groups=actor_groups,
    )
    context = {
        "catalog": catalog,
        "candidates": candidate_values,
        "source_evidence": source_evidence,
        "truncated": source_evidence_truncated,
    }
    trimmed = 0
    while _rendered_bytes(context) > MAX_PLANNER_CONTEXT_BYTES:
        trimmed += 1
        context["truncated"] = True
        if len(context["candidates"]) > 1:
            context["candidates"].pop()
        elif len(context["source_evidence"]) > 1:
            context["source_evidence"].pop()
        elif context["candidates"]:
            context["candidates"].pop()
        elif context["source_evidence"]:
            context["source_evidence"].pop()
        elif context["catalog"]:
            context["catalog"].pop()
        else:
            raise ValueError("filing context exceeds its byte limit")
    if measure is not None:
        measure.update(catalog=len(context["catalog"]), candidates=len(context["candidates"]),
                       evidence=len(context["source_evidence"]), trimmed=trimmed,
                       budget_bytes=MAX_PLANNER_CONTEXT_BYTES)
    return context


def _summary(body: str) -> str:
    """The first prose line of a page, without its heading, citations, or emphasis."""
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ">", "<!--", "|", "```")):
            continue
        stripped = re.sub(r"\(Source: `[^`]*`\)", "", stripped)
        stripped = re.sub(r"^[-*]\s+", "", stripped)
        stripped = re.sub(r"[*_`]", "", stripped)
        stripped = " ".join(stripped.split())
        if not stripped:
            continue
        if len(stripped) > MAX_CATALOG_SUMMARY_CHARS:
            stripped = stripped[:MAX_CATALOG_SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"
        return stripped
    return ""


def authorized_derived_page_paths(
    root: str,
    *,
    write_context: WriteContext,
) -> frozenset[str]:
    """Return derived paths admitted by the writer without extending model context."""
    paths = set()
    for folder in KNOWLEDGE_FOLDERS:
        base = Path(root, *folder.split("/"))
        if not base.is_dir():
            continue
        for path in base.glob("*.md"):
            relative = path.relative_to(root).as_posix()
            page = parse_page(relative, path.read_text(encoding="utf-8"))
            try:
                allow_existing(write_context, page.acl)
            except WriteRefused:
                continue
            paths.add(page.path)
    return frozenset(paths)


def render_context(context: dict) -> str:
    # The catalog leads because it changes least between captures; a provider reuses a cached
    # prompt only up to its first differing byte.
    order = sorted(context, key=lambda key: (key != "catalog", key))
    rendered = json.dumps({key: _sorted_keys(context[key]) for key in order}, ensure_ascii=False, indent=2)
    if len(rendered.encode("utf-8")) > MAX_PLANNER_CONTEXT_BYTES:
        raise ValueError("filing context exceeds its byte limit")
    return rendered


def _sorted_keys(value):
    if isinstance(value, dict):
        return {key: _sorted_keys(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sorted_keys(item) for item in value]
    return value


def _rendered_bytes(context: dict) -> int:
    return len(json.dumps(context, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _source_evidence(
    root: str,
    *,
    candidates: list[dict],
    terms: set[str],
    capture_acl: tuple[str, ...] | None,
    actor_groups: frozenset[str] | None,
) -> tuple[list[dict], bool]:
    evidence_terms = terms - _SOURCE_STOPWORDS
    ranked = {}
    references = []
    seen = set()
    for candidate in candidates:
        for relative in candidate["sources"]:
            if relative in seen:
                continue
            seen.add(relative)
            references.append(relative)
    truncated = len(references) > MAX_CONTEXT_SOURCE_REFERENCES
    total_read = 0
    for relative in references[:MAX_CONTEXT_SOURCE_REFERENCES]:
        try:
            size = source_file_size(root, relative)
        except (SourceContractError, OSError):
            continue
        if size > MAX_CONTEXT_SOURCE_FILE_BYTES:
            truncated = True
            continue
        if total_read + size > MAX_CONTEXT_SOURCE_READ_BYTES:
            truncated = True
            break
        total_read += size
        try:
            source = read_source(
                root,
                relative,
                max_bytes=MAX_CONTEXT_SOURCE_FILE_BYTES,
            )
        except (SourceContractError, OSError, UnicodeError):
            continue
        if len(source.body.encode("utf-8")) > MAX_CONTEXT_SOURCE_BYTES:
            truncated = True
            continue
        if not _admits(source.acl, actor_groups=actor_groups, capture_acl=capture_acl):
            continue
        source_terms = set(
            _WORD_RE.findall(f"{source.title}\n{source.body}".casefold())
        ) - _SOURCE_STOPWORDS
        overlap = len(evidence_terms & source_terms)
        if overlap:
            ranked[relative] = (
                overlap,
                {
                    "path": relative,
                    "title": source.title,
                    "date": source.captured_at.date().isoformat(),
                    "body": source.body,
                },
            )
    ordered = sorted(ranked.values(), key=lambda item: (-item[0], item[1]["path"]))
    truncated = truncated or len(ordered) > MAX_CONTEXT_SOURCE_EVIDENCE
    result = [item for _score, item in ordered[:MAX_CONTEXT_SOURCE_EVIDENCE]]
    return result, truncated


def _filed_contradictions(body: str) -> list[dict]:
    try:
        located = contradictions.parse_all(body)
    except contradictions.ContradictionContractError:
        return []
    return [
        {
            "id": item.record.contradiction_id,
            "explanation": item.record.explanation,
            "claims": [claim.model_dump(mode="json") for claim in item.record.claims],
        }
        for item in located
    ]


def _admits(
    acl: tuple[str, ...] | None,
    *,
    actor_groups: frozenset[str] | None,
    capture_acl: tuple[str, ...] | None,
) -> bool:
    """The one admission rule: the actor may read it and it flows into the capture audience."""
    return visible(acl, None if actor_groups is None else set(actor_groups)) and flows_into(
        _list(acl), _list(capture_acl)
    )


def _list(value: tuple[str, ...] | None) -> list[str] | None:
    return None if value is None else list(value)
