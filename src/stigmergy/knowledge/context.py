from __future__ import annotations

import json
import re
from pathlib import Path

from stigmergy.entities.model import load_entities
from stigmergy.kernel.acl import flows_into
from stigmergy.knowledge.pages import parse_page
from stigmergy.knowledge.sources import (
    SourceContractError,
    read_source,
    source_file_size,
)
from stigmergy.server.acl import visible
from stigmergy.server.identity import principal_from_text

_WORD_RE = re.compile(r"[^\W_]{2,}", re.UNICODE)
MAX_PLANNER_CONTEXT_BYTES = 768 * 1024
MAX_CONTEXT_ENTITIES = 50
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


def actor_scope(root: str, subject: str) -> tuple[frozenset[str] | None, bool]:
    path = Path(root, "ops", "identities.json")
    principal = principal_from_text(path.read_text(encoding="utf-8"), subject, origin=str(path))
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
) -> dict:
    folded_source = source_text.casefold()
    terms = set(_WORD_RE.findall(folded_source))
    candidates = []
    for folder in ("wiki/notes", "wiki/concepts"):
        base = Path(root, *folder.split("/"))
        if not base.is_dir():
            continue
        for path in base.glob("*.md"):
            relative = path.relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8")
            page = parse_page(relative, text)
            if not visible(page.acl, None if actor_groups is None else set(actor_groups)):
                continue
            if not flows_into(_list(page.acl), _list(capture_acl)):
                continue
            overlap = len(terms & set(_WORD_RE.findall(f"{page.title}\n{page.body}".casefold())))
            if overlap:
                candidates.append(
                    (
                        overlap,
                        {
                            "path": page.path,
                            "title": page.title,
                            "type": page.role,
                            "status": page.status,
                            "entities": list(page.entities),
                            "sources": list(page.sources),
                            "body": page.body,
                            "capture_may_update": flows_into(_list(capture_acl), _list(page.acl)),
                        },
                    )
                )
    candidates.sort(key=lambda item: (-item[0], item[1]["path"]))
    candidate_values = [value for _score, value in candidates[: max(1, limit)]]
    source_evidence, source_evidence_truncated = _source_evidence(
        root,
        candidates=candidate_values,
        terms=terms,
        capture_acl=capture_acl,
        actor_groups=actor_groups,
    )

    entity_claims = []
    for entity_id, record in sorted(load_entities(root).items()):
        claims = [
            claim
            for claim in record.claims
            if visible(claim.acl, None if actor_groups is None else set(actor_groups))
            and flows_into(_list(claim.acl), _list(capture_acl))
        ]
        relevance = max(
            (len(terms & set(_WORD_RE.findall(claim.value.casefold()))) for claim in claims),
            default=0,
        )
        if entity_id.casefold() in folded_source:
            relevance += 1
        if claims and relevance:
            entity_claims.append(
                (
                    relevance,
                    {
                    "id": entity_id,
                    "entity_type": record.entity_type,
                    "names": [
                        {"value": claim.value, "kind": claim.kind}
                        for claim in sorted(claims, key=lambda item: item.introduced_at, reverse=True)
                    ],
                    },
                )
            )
    entity_claims.sort(key=lambda item: (-item[0], item[1]["id"]))
    context = {
        "candidates": candidate_values,
        "source_evidence": source_evidence,
        "entities": [
            value for _score, value in entity_claims[:MAX_CONTEXT_ENTITIES]
        ],
        "truncated": source_evidence_truncated or len(entity_claims) > MAX_CONTEXT_ENTITIES,
    }
    while _rendered_bytes(context) > MAX_PLANNER_CONTEXT_BYTES:
        context["truncated"] = True
        if context["entities"]:
            context["entities"].pop()
        elif len(context["candidates"]) > 1:
            context["candidates"].pop()
        elif len(context["source_evidence"]) > 1:
            context["source_evidence"].pop()
        elif context["candidates"]:
            context["candidates"].pop()
        elif context["source_evidence"]:
            context["source_evidence"].pop()
        else:
            raise ValueError("filing context exceeds its byte limit")
    return context


def render_context(context: dict) -> str:
    rendered = json.dumps(context, ensure_ascii=False, sort_keys=True, indent=2)
    if len(rendered.encode("utf-8")) > MAX_PLANNER_CONTEXT_BYTES:
        raise ValueError("filing context exceeds its byte limit")
    return rendered


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
        acl = source.acl
        if not visible(acl, None if actor_groups is None else set(actor_groups)):
            continue
        if not flows_into(_list(acl), _list(capture_acl)):
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


def _list(value: tuple[str, ...] | None) -> list[str] | None:
    return None if value is None else list(value)
