from __future__ import annotations

import json
import re
from pathlib import Path

from stigmergy.entities.model import load_entities
from stigmergy.kernel.acl import flows_into
from stigmergy.knowledge.pages import parse_page
from stigmergy.server.acl import visible
from stigmergy.server.identity import principal_from_text

_WORD_RE = re.compile(r"[^\W_]{2,}", re.UNICODE)
MAX_PLANNER_CONTEXT_BYTES = 768 * 1024
MAX_CONTEXT_ENTITIES = 50


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
            if not (
                flows_into(_list(page.acl), _list(capture_acl))
                or flows_into(_list(capture_acl), _list(page.acl))
            ):
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
                            "context_may_flow_to_capture": flows_into(
                                _list(page.acl), _list(capture_acl)
                            ),
                            "capture_may_update": flows_into(_list(capture_acl), _list(page.acl)),
                        },
                    )
                )
    candidates.sort(key=lambda item: (-item[0], item[1]["path"]))

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
        "candidates": [value for _score, value in candidates[: max(1, limit)]],
        "entities": [
            value for _score, value in entity_claims[:MAX_CONTEXT_ENTITIES]
        ],
        "truncated": len(entity_claims) > MAX_CONTEXT_ENTITIES,
    }
    while _rendered_bytes(context) > MAX_PLANNER_CONTEXT_BYTES:
        context["truncated"] = True
        if context["entities"]:
            context["entities"].pop()
        elif context["candidates"]:
            context["candidates"].pop()
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


def _list(value: tuple[str, ...] | None) -> list[str] | None:
    return None if value is None else list(value)
