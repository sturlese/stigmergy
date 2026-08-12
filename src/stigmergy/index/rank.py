"""Contract-aware ranking over the fused arms. Pure code, explainable: every hit carries the
factors that shaped its score, so 'why did this page rank here' is always answerable.

Base relevance is Reciprocal Rank Fusion of the FTS and vector rankings. RRF is HIGHER-is-better,
so the factor constants — penalties > 1, boosts < 1 — DIVIDE the score. Staleness is judged
against an INJECTED `today`, never the wall clock, so ranking is testable and reproducible.
"""
import re
from datetime import date, timedelta

from stigmergy.text import sanitize

TOP_K = 5
RRF_K = 60                # the fusion constant
CANDIDATE_POOL = 40       # per-arm candidates considered before fusion

# multiplicative factors: penalties > 1 demote, boosts < 1 promote — applied as divisors of the
# higher-is-better fused score.
_PENALTY_SUPERSEDED = 4.0
_BOOST_ENTITY = 0.5
_BOOST_PERIOD = 0.6
_BOOST_FRESH = 0.7
_BOOST_EVERGREEN = 0.8
_PENALTY_STALE = 1.3
STALE_AFTER_DAYS = 365

# `inlinks` is DELIBERATELY not a factor — measured twice (0.9^min(n,3) and 0.7), and both took
# the golden's final arm down: link-degree rewards hubs, and hubs are exactly what a broad "what
# do we know about X" question must not bury the synthesis under. The column stays data; waking
# this needs a measured miss it would fix.

# 'current/latest'-style words prefer fresher as_of. English because the corpus and the questions
# are; an unrecognized word simply never fires the boost — silently — so check whether the set
# still matches the language people ask in before widening it.
_RECENCY_WORDS = {"current", "latest", "now", "today", "newest", "most recent"}

# Matched on WORD BOUNDARIES, never as substrings: `"now" in "what do we know about X"` is true,
# and that slack put the boost on the most common broad-question shape there is. A regex rather
# than `query_tokens`: `\b` spans the multi-word entry in one pass, and `query_tokens` keeps the
# apostrophe inside a token, so matching against it would drop "today's numbers". Longest-first
# alternation so a two-word entry cannot be shadowed by a one-word prefix of itself.
_RECENCY_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in sorted(_RECENCY_WORDS, key=len, reverse=True)) + r")\b")


# A split page's continuation parts carry a trailing part marker on their id, in TWO conventions
# both of which must be recognized: the declared `<id>#p<n>` (`librarian.processing.
# _build_source_parts` names the FILE `<stem>-p<n>.md` and stamps `id: "<stem>#p<n>"`) and the
# bare `<stem>-p<n>` stem fallback for parts filed without a declared id. Recognizing only one
# leaves the `superseded_by` propagation and the chain collapse silently INERT over the other.
# `superseded_by` is stamped on the PRIMARY page only, so `chain_base` recovers the shared
# document id; `corpus.load_pages` propagates the value at build time.
#
# Accepted residual of the bare-stem fallback: an INDEPENDENT page whose id merely ends in
# `-p<n>` merges into a base only when that base id also exists in the SAME directory — a pair
# indistinguishable by id shape alone. The substrate lint flags the orphan complement.
_PART_MARKER_RE = re.compile(r"(?:#p|-p)\d+$")


def chain_base(page_id: str) -> str:
    """The document id shared by a split page and its continuation parts: a trailing part marker
    stripped, both `#p<n>` and `-p<n>` spellings. Non-split ids are returned unchanged."""
    return _PART_MARKER_RE.sub("", page_id or "")


def rrf_fuse(rankings: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion: each arm contributes 1/(k + rank); ids missing from an arm simply
    don't score there."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, page_id in enumerate(ranking, start=1):
            scores[page_id] = scores.get(page_id, 0.0) + 1.0 / (k + rank)
    return scores


def query_tokens(query: str) -> set[str]:
    return set(re.findall(r"[\w][\w'-]*", query.lower()))


def query_periods(query: str) -> set[str]:
    out = set()
    for m in re.finditer(r"\b(20\d\d)(?:[-/ ]?(Q[1-4])|[-/](0?[1-9]|1[0-2]))?\b", query, re.I):
        year = m.group(1)
        if m.group(2):
            out.add(f"{year}-{m.group(2).upper()}")
        elif m.group(3):
            out.add(f"{year}-{int(m.group(3)):02d}")
        out.add(year)
    return out


def _period_end(value: str) -> date | None:
    """Latest plausible day of a (possibly coarse) as_of/updated value — a page dated '2026'
    is treated as fresh through 2026, so coarse-but-recent pages are never punished."""
    # EVERY branch is guarded, not only the full-date one: `\d{4}` matches `0000` and `date()`
    # rejects year 0, so an unguarded branch lets ONE oddly-dated page break every search whose
    # candidate pool touches it. Any invalid value yields None, never a raise.
    v = value.strip().strip('"')
    try:
        if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", v):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if m := re.fullmatch(r"(\d{4})-Q([1-4])", v, re.I):
            return date(int(m.group(1)), int(m.group(2)) * 3, 28)
        if m := re.fullmatch(r"(\d{4})-(\d{2})", v):
            month = int(m.group(2))
            return date(int(m.group(1)), month, 28) if 1 <= month <= 12 else None
        if m := re.fullmatch(r"\d{4}", v):
            return date(int(v), 12, 31)
    except ValueError:
        return None
    return None


def contract_factors(page: dict, query: str, today: date | None = None,
                     entity_hint: str | None = None) -> list[tuple[float, str]]:
    """The deterministic contract factors for one candidate page.
    Returns (factor, label) pairs; factors > 1 demote, < 1 boost."""
    q_low = query.lower()
    periods = query_periods(query)
    wants_fresh = _RECENCY_RE.search(q_low) is not None

    factors: list[tuple[float, str]] = []
    if page.get("superseded_by"):
        factors.append((_PENALTY_SUPERSEDED, "superseded"))
    if page.get("status") == "evergreen":
        factors.append((_BOOST_EVERGREEN, "status-evergreen"))
    # The boost fires on the entity id the SERVICE resolved and passed down (`entity_hint`),
    # matched by MEMBERSHIP of the page's `entity` list — never re-inferred from query tokens,
    # which is structurally dead for every multi-word id (`acme-capital` never equals one token of
    # "Acme Capital"). No hint means no entity factor: ranking applies what it is TOLD.
    # A SCALAR string must not be iterated — it would be walked as characters.
    raw_entity = page.get("entity") or ()
    entities = [raw_entity] if isinstance(raw_entity, str) else raw_entity
    if entity_hint and any(e and str(e).lower() == str(entity_hint).lower() for e in entities):
        factors.append((_BOOST_ENTITY, f"entity:{entity_hint}"))
    # `as_of` is the ONE dated field a query period matches against — no parallel `period` column.
    page_periods = {x for x in (page.get("as_of"),) if x}
    if periods and any(pp == qp or pp.startswith(qp + "-") or qp.startswith(pp + "-")
                       for pp in page_periods for qp in periods):
        factors.append((_BOOST_PERIOD, "period-match"))
    if wants_fresh and page.get("as_of") and not page.get("superseded_by"):
        factors.append((_BOOST_FRESH, f"fresh:{page['as_of']}"))
    if today is not None:
        freshest = page.get("as_of") or page.get("updated") or ""
        end = _period_end(freshest)
        if end is not None and today - end > timedelta(days=STALE_AFTER_DAYS):
            factors.append((_PENALTY_STALE, f"stale:{freshest}"))
    return factors


def rank(candidates: dict[str, dict], fts_ranking: list[str], vec_ranking: list[str],
         query: str, k: int = TOP_K, today: date | None = None,
         include_superseded: bool = True, entity_hint: str | None = None) -> list[dict]:
    """Top-k hits: RRF over the two arms, then the contract factors. `candidates` maps path ->
    page row (the union of both arms). Hits carry `score`, `factors` and a `snippet`;
    `include_superseded=False` drops stale versions entirely (they stay readable by path —
    exclusion is an operational filter, not deletion). Ties break on path: deterministic.
    `entity_hint` is the service-resolved entity id, told rather than inferred (see
    `contract_factors`); None means no entity factor fires."""
    fused = rrf_fuse([fts_ranking, vec_ranking])
    q_tokens = query_tokens(query)
    hits = []
    for path, base in fused.items():
        page = candidates.get(path)
        if page is None:
            continue
        # A continuation part carries its OWN `superseded_by`, propagated at BUILD time onto every
        # chain sibling (`corpus.load_pages`) — so a bare per-row check is correct whether or not
        # the chain's primary is in THIS query's candidate set. No per-chain reconstruction
        # belongs here: rebuilding membership from the candidate pool misses a part whose primary
        # fell outside it.
        in_superseded_chain = bool(page.get("superseded_by"))
        if not include_superseded and in_superseded_chain:
            continue
        applied = contract_factors(page, query, today, entity_hint)
        score = base
        for factor, _label in applied:
            score /= factor
        page = dict(page)
        body = page.pop("body", "") or ""
        arms = [arm for arm, ranking in (("fts", fts_ranking), ("vec", vec_ranking)) if path in ranking]
        hits.append({**page, "score": score, "arms": arms,
                     "factors": [label for _f, label in applied],
                     "snippet": _snippet(body, q_tokens)})
    hits.sort(key=lambda h: (-h["score"], h["path"]))
    # ONE document, ONE top-k slot: without the collapse a single split transcript floods four of
    # five slots and buries the page that actually answered. The best-scoring member represents
    # the chain (the sort above decides it); the others stay reachable by path — collapse is a
    # top-k presentation rule, not deletion. The key carries the DIRECTORY beside the chain base:
    # two ID-less pages sharing a file stem in different folders fall back to the same page_id,
    # and a bare base key would merge unrelated documents; a real chain's parts sit BESIDE their
    # primary, so the directory dimension never splits one.
    collapsed, seen_chains = [], set()
    for h in hits:
        base = chain_base(h.get("page_id") or "") or h["path"]
        key = (h["path"].rsplit("/", 1)[0] if "/" in h["path"] else "", base)
        if key in seen_chains:
            continue
        seen_chains.add(key)
        collapsed.append(h)
    return collapsed[:k]


def _snippet(body: str, q_tokens: set[str], width: int = 240) -> str:
    """The region around the LONGEST query token present in the body (fallback: the head).
    Ties break alphabetically, never by set-iteration order — CPython randomizes that per process,
    and the snippet is user-visible on every hit, so determinism has to include it."""
    low = body.lower()
    best = 0
    for t in sorted(q_tokens, key=lambda tok: (-len(tok), tok)):
        if len(t) < 3:
            continue
        i = low.find(t)
        if i >= 0:
            best = max(0, i - width // 3)
            break
    return sanitize(re.sub(r"\s+", " ", body[best:best + width]).strip())
