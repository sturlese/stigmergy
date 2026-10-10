"""Deterministic ranking for fused lexical and vector candidates."""

from __future__ import annotations

import re
from datetime import date, timedelta

from stigmergy.kernel import layers
from stigmergy.text import clamp, sanitize

TOP_K = 5
RRF_K = 60
CANDIDATE_POOL = 40

_BOOST_ENTITY = 0.5
_BOOST_EVERGREEN = 0.8
_PENALTY_STALE = 1.3
STALE_AFTER_DAYS = 365


# Neighbors of the best hits only break ties and extend the tail, so they must not outvote the
# retrieval arms that found the page answering the question.
ARM_WEIGHTS = {"graph": 0.25}

# Fused pages mark the units a restricted layer contributed; ranking text never carries the mark.


def rrf_fuse(rankings: list[list[str]], k: int = RRF_K,
             weights: list[float] | None = None) -> dict[str, float]:
    scores: dict[str, float] = {}
    for index, ranking in enumerate(rankings):
        weight = 1.0 if weights is None else weights[index]
        for position, page_id in enumerate(ranking, start=1):
            scores[page_id] = scores.get(page_id, 0.0) + weight / (k + position)
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
    entity_hints: tuple[str, ...] = (),
) -> list[tuple[float, str]]:
    del query
    factors: list[tuple[float, str]] = []
    if page.get("status") == "evergreen":
        factors.append((_BOOST_EVERGREEN, "status-evergreen"))
    links = page.get("links") or ()
    own = page.get("vpath") or page.get("path")
    named = [hint for hint in entity_hints if own == hint or hint in links]
    for position, hint in enumerate(named):
        factors.append((_BOOST_ENTITY if position == 0 else 1.0, f"entity:{hint}"))
    if len(entity_hints) > 1 and len(named) == len(entity_hints):
        factors.append((_BOOST_ENTITY, "entities:all"))
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
    entity_hints: tuple[str, ...] = (),
    extra_rankings: dict[str, list[str]] | None = None,
) -> list[dict]:
    rankings = {"fts": fts_ranking, "vec": vec_ranking, **(extra_rankings or {})}
    fused = rrf_fuse(list(rankings.values()),
                     weights=[ARM_WEIGHTS.get(arm, 1.0) for arm in rankings])
    query_words = query_tokens(query)
    hits = []
    bodies: dict[str, str] = {}
    for path, base_score in fused.items():
        candidate = candidates.get(path)
        if candidate is None:
            continue
        factors = contract_factors(candidate, query, today, entity_hints)
        score = base_score
        for factor, _label in factors:
            score /= factor
        page = dict(candidate)
        body = page.pop("body", "") or ""
        bodies[path] = body
        arms = [arm for arm, ranking in rankings.items() if path in ranking]
        hits.append(
            {
                **page,
                "score": score,
                "arms": arms,
                "factors": [label for _factor, label in factors],
                "snippet": snippet(body, query_words),
            }
        )
    hits.sort(key=lambda hit: (-hit["score"], hit["path"]))
    for hit in hits[:k]:
        hit["summary"] = summary_of(bodies[hit["path"]])
    return hits[:k]


def snippet(body: str, query_words: set[str], width: int = 240) -> str:
    body = layers.strip_markers(body)
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


SUMMARY_WIDTH = 240
SUMMARY_SCAN_CHARS = 8192
_FENCE = re.compile(r"^\s*(```|~~~)")
_NOT_PROSE = re.compile(r"^\s*(#|[-*+]\s|\d+[.)]\s|\||>)")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_CITATION = re.compile(r"\s*\((?:Sources?|Fuentes?):[^()]{0,600}\)")
_WIKILINK = re.compile(r"\[\[([^\[\]|\n]{1,200})(?:\|([^\[\]\n]{1,200}))?\]\]")
_MDLINK = re.compile(r"\[([^\[\]\n]{1,200})\]\([^()\s]{0,500}\)")
_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3}|`{1,3})(\S(?:.{0,200}?\S)?)\1")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"\u201c(\u00c0-\u00de\u00bf\u00a1])")
_ABBREVIATION = re.compile(
    r"(?:^|\s)(?:Dr|Dra|Mr|Mrs|Ms|Sr|Sra|Prof|St|Inc|Ltd|Co|vs|e\.g|i\.e|approx)\.$")


def _first_paragraph(body: str) -> str:
    """The first prose paragraph, or else the first list item."""
    paragraph: list[str] = []
    first_item = ""
    in_fence = False
    for line in (body or "").splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            if paragraph:
                break
            continue
        if in_fence:
            continue
        stripped = line.strip()
        if not stripped or _NOT_PROSE.match(line):
            if not first_item and _LIST_ITEM.match(line):
                first_item = _LIST_ITEM.sub("", line).strip()
            if paragraph:
                break
            continue
        paragraph.append(stripped)
        if sum(len(part) for part in paragraph) > 4 * SUMMARY_WIDTH:
            break
    return " ".join(paragraph) if paragraph else first_item


def _clean(text: str) -> str:
    text = layers.strip_markers(text)
    text = _CITATION.sub("", text)
    text = _WIKILINK.sub(lambda m: m.group(2) or m.group(1), text)
    text = _MDLINK.sub(lambda m: m.group(1), text)
    text = _EMPHASIS.sub(lambda m: m.group(2), text)
    return re.sub(r"\s+", " ", sanitize(text)).strip()


def _first_sentence(text: str) -> str:
    for match in _SENTENCE_END.finditer(text):
        if _ABBREVIATION.search(text[max(0, match.start() - 8):match.start()]):
            continue
        return text[:match.start()]
    return text


def summary_of(body: str) -> str:
    """One catalog line for a page: the first sentence of its first prose paragraph after the
    title, with links shown as their text and citations removed. Pages without prose fall back to
    their first list item. Only the head of the body is parsed and every pattern is bounded, so the
    cost per page is constant however large or hostile the body is."""
    return clamp(_first_sentence(_clean(_first_paragraph((body or "")[:SUMMARY_SCAN_CHARS]))),
                 SUMMARY_WIDTH)
