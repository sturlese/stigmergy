"""Deterministic ranking for fused lexical and vector candidates."""

from __future__ import annotations

import re
from datetime import date, timedelta

from stigmergy.text import sanitize

TOP_K = 5
RRF_K = 60
CANDIDATE_POOL = 40

_BOOST_ENTITY = 0.5
_BOOST_EVERGREEN = 0.8
_PENALTY_STALE = 1.3
STALE_AFTER_DAYS = 365


def rrf_fuse(rankings: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, page_id in enumerate(ranking, start=1):
            scores[page_id] = scores.get(page_id, 0.0) + 1.0 / (k + position)
    return scores


def query_tokens(query: str) -> set[str]:
    return set(re.findall(r"[\w][\w'-]*", query.lower()))


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


def contract_factors(
    page: dict,
    query: str,
    today: date | None = None,
    entity_hint: str | None = None,
) -> list[tuple[float, str]]:
    del query
    factors: list[tuple[float, str]] = []
    if page.get("status") == "evergreen":
        factors.append((_BOOST_EVERGREEN, "status-evergreen"))
    raw_entities = page.get("entity") or ()
    entities = [raw_entities] if isinstance(raw_entities, str) else raw_entities
    if entity_hint and any(
        str(entity).casefold() == str(entity_hint).casefold() for entity in entities
    ):
        factors.append((_BOOST_ENTITY, f"entity:{entity_hint}"))
    updated = _date(page.get("updated") or "")
    if today is not None and updated is not None and today - updated > timedelta(days=STALE_AFTER_DAYS):
        factors.append((_PENALTY_STALE, f"stale:{updated.isoformat()}"))
    return factors


def rank(
    candidates: dict[str, dict],
    fts_ranking: list[str],
    vec_ranking: list[str],
    query: str,
    k: int = TOP_K,
    today: date | None = None,
    entity_hint: str | None = None,
) -> list[dict]:
    fused = rrf_fuse([fts_ranking, vec_ranking])
    query_words = query_tokens(query)
    hits = []
    for path, base_score in fused.items():
        candidate = candidates.get(path)
        if candidate is None:
            continue
        factors = contract_factors(candidate, query, today, entity_hint)
        score = base_score
        for factor, _label in factors:
            score /= factor
        page = dict(candidate)
        body = page.pop("body", "") or ""
        arms = [
            arm
            for arm, ranking in (("fts", fts_ranking), ("vec", vec_ranking))
            if path in ranking
        ]
        hits.append(
            {
                **page,
                "score": score,
                "arms": arms,
                "factors": [label for _factor, label in factors],
                "snippet": _snippet(body, query_words),
            }
        )
    hits.sort(key=lambda hit: (-hit["score"], hit["path"]))
    return hits[:k]


def _snippet(body: str, query_words: set[str], width: int = 240) -> str:
    lowered = body.lower()
    start = 0
    for token in sorted(query_words, key=lambda value: (-len(value), value)):
        if len(token) < 3:
            continue
        position = lowered.find(token)
        if position >= 0:
            start = max(0, position - width // 3)
            break
    return sanitize(re.sub(r"\s+", " ", body[start : start + width]).strip())
