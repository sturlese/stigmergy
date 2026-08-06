"""Contract-aware ranking over the fused arms. Pure code, explainable: every hit carries the
factors that shaped its score, so 'why did this page rank here' is always answerable — no
opaque similarity.

The design is recorded in ADR 012. Two properties are load-bearing:

- Base relevance is Reciprocal Rank Fusion of the FTS and vector rankings, not BM25 alone. RRF is
  HIGHER-is-better, so the factor constants — penalties > 1, boosts < 1 — DIVIDE the score rather
  than multiplying it.
- `status: evergreen` outranks `seed` on equal relevance (maturity boost), and stale pages are
  penalized by age — deterministically, against an INJECTED `today` and never the wall clock, so
  ranking is testable and reproducible.
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

# `inlinks` is DELIBERATELY not a factor, and this was measured rather than assumed: a candidate
# boost (0.9^min(n,3), and a stronger 0.7) took the retrieval golden's final arm from 1.000 to
# 0.923 both times — the highly-inlinked entity page outranked the VIEW on a broad "what do we
# know about X" question. Link-degree rewards hubs, and hubs are exactly what broad questions must
# NOT bury the synthesis under. The column stays data (gardener, webhook reconciliation); waking
# this needs a measured miss it would fix.

# 'current/latest'-style words prefer fresher as_of. The set is English because the corpus and
# the questions are: a deployment answering in another language extends it, and the boost is
# simply not applied to a word nothing here recognizes — silently, which is why a set that has
# stopped matching the language people ask in is worth checking before it is worth widening.
_RECENCY_WORDS = {"current", "latest", "now", "today", "newest", "most recent"}


# A split page's continuation parts carry a trailing part marker on their id. TWO conventions
# exist: the historical `<id>#p2`, and the live meeting/document flow's `<stem>-p2`
# (`librarian.processing._build_source_parts` names the FILE `<stem>-p<n>.md` and writes no
# frontmatter id, so the page_id IS the suffixed stem). Recognizing only `#p` leaves both the
# build-time `superseded_by` propagation and the rank-time chain collapse below INERT over every
# part the live system actually produces — silently.
#
# When a document is superseded, versions.py stamps `superseded_by` on the PRIMARY page only —
# never on the continuation parts. So a bare per-page check demotes part 1 while parts 2..n rank
# on as if current: the split-chain demotion bug. `chain_base` recovers the shared document id.
#
# This function's caller used to be `rank()` itself, reconstructing chain membership at QUERY
# time from whichever candidates happened to be in a given search's pool — a reconstruction that
# silently failed whenever the chain's primary page fell outside that pool. `corpus.load_pages`
# calls it at BUILD time instead, grouping every row by its chain base and propagating the
# primary's `superseded_by` onto every sibling BEFORE the row ever reaches storage — so by the
# time `rank()` sees a candidate, its own `superseded_by` column already tells the whole truth,
# and no per-query reconstruction is needed at all.
#
# False-positive note: an INDEPENDENT page whose id merely
# ends in `-p<n>` merges into a base it never belonged to only when that base id also exists in
# the SAME directory (the collapse key below carries the directory). The substrate lint flags
# the ORPHAN complement (a part-shaped id with no base beside it) — the same-directory pair with
# a real base present is INDISTINGUISHABLE by id shape alone and is an accepted residual: chain
# identity is inferred from filenames because the splitter writes no frontmatter `id:` on parts.
# The real fix is explicit chain identity at the PRODUCER (`_build_source_parts` stamping ids).
_PART_MARKER_RE = re.compile(r"(?:#p|-p)\d+$")


def chain_base(page_id: str) -> str:
    """The document id shared by a split page and its continuation parts: a trailing part
    marker stripped. BOTH spellings are handled — `#p<n>` and `-p<n>` — because the live producer
    writes the second and matching only the first left this inert over every real split.
    Non-split ids are returned unchanged."""
    return _PART_MARKER_RE.sub("", page_id or "")


def rrf_fuse(rankings: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion (ported from the spike's rank_rrf): each arm contributes
    1/(k + rank); ids missing from an arm simply don't score there."""
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
    v = value.strip().strip('"')
    if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", v):
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    if m := re.fullmatch(r"(\d{4})-Q([1-4])", v, re.I):
        return date(int(m.group(1)), int(m.group(2)) * 3, 28)
    if m := re.fullmatch(r"(\d{4})-(\d{2})", v):
        month = int(m.group(2))
        return date(int(m.group(1)), month, 28) if 1 <= month <= 12 else None
    if m := re.fullmatch(r"\d{4}", v):
        return date(int(v), 12, 31)
    return None


def contract_factors(page: dict, query: str, today: date | None = None,
                     entity_hint: str | None = None) -> list[tuple[float, str]]:
    """The deterministic contract factors for one candidate page.
    Returns (factor, label) pairs; factors > 1 demote, < 1 boost."""
    q_low = query.lower()
    periods = query_periods(query)
    wants_fresh = any(w in q_low for w in _RECENCY_WORDS)

    factors: list[tuple[float, str]] = []
    if page.get("superseded_by"):
        factors.append((_PENALTY_SUPERSEDED, "superseded"))
    if page.get("status") == "evergreen":
        factors.append((_BOOST_EVERGREEN, "status-evergreen"))
    # The boost fires on the entity id the SERVICE resolved from the registry and passed down
    # (`entity_hint`), matched by MEMBERSHIP of the page's `entity` list — never re-inferred from
    # query tokens here. Token inference is structurally dead for every multi-word entity (an id
    # like `acme-capital` can never equal one token of "Acme Capital"), so it silently narrows the
    # factor to single-word ids — a defect invisible except by eyeballing a search result, which
    # is the class of latency `stigmergy-index check` exists to end. No hint means no entity factor:
    # resolution is the service's job (it owns the registry and identity), and ranking only
    # applies what it is TOLD.
    #
    # A SCALAR string must not be iterated — a caller handing a bare string instead of a list
    # would otherwise be walked as characters.
    raw_entity = page.get("entity") or ()
    entities = [raw_entity] if isinstance(raw_entity, str) else raw_entity
    if entity_hint and any(e and str(e).lower() == str(entity_hint).lower() for e in entities):
        factors.append((_BOOST_ENTITY, f"entity:{entity_hint}"))
    # `as_of` alone: a separate `period` field was an exact duplicate of it (every page carrying
    # one carried both, with the same value) and had no producer — no template offered it and no
    # code stamped it. One dated field, not two.
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
        # A split document's continuation parts ("<id>#p2", "#p3", …) carry their OWN
        # `superseded_by`, propagated at BUILD time onto every sibling in the chain
        # (`corpus.load_pages`, grouped by `chain_base`) — so a bare per-row check is correct on
        # its own, whether or not the chain's primary happens to be in THIS query's candidate set.
        # When the field lived on the primary row only, this function reconstructed chain
        # membership from whatever candidates happened to be here, which MISSED a continuation
        # part whose primary fell outside the candidate set — the exact case
        # `tests/index/test_rank.py` pins. `contract_factors` already turns a truthy
        # `superseded_by` into the "superseded" penalty+label, so no per-chain compensation
        # belongs here.
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
    # ONE document, ONE top-k slot. A split document's parts are one page set — without this, a
    # broad query lets a single transcript flood four of five slots and bury the page that
    # actually answered. That was measured, not imagined: a final top-5 of the meeting page plus
    # ALL FOUR transcript parts, with the expected decision page sitting at vec rank 2.
    # Best-scoring member represents the chain (the sort order above already decides it); the
    # others stay reachable by path — collapse is a top-k presentation rule, not deletion.
    #
    # The key carries the page's DIRECTORY beside the chain base: two ID-LESS pages sharing a file
    # stem in different folders both fall back to the same stem-derived page_id (the same class
    # `corpus.load_pages` marker-gates for propagation — measured here on two unrelated
    # `quarterly-update.md` twins), and a bare chain_base key would merge two unrelated documents.
    # A real chain's parts sit BESIDE their primary (`_build_source_parts` writes `<stem>-p<n>.md`
    # into the primary's own folder; the historical `#p` ids shared one file's zone path), so the
    # directory dimension never splits a genuine chain.
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
    """First region of the body containing a query token (fallback: the head)."""
    low = body.lower()
    best = 0
    for t in sorted(q_tokens, key=len, reverse=True):
        if len(t) < 3:
            continue
        i = low.find(t)
        if i >= 0:
            best = max(0, i - width // 3)
            break
    return sanitize(re.sub(r"\s+", " ", body[best:best + width]).strip())
