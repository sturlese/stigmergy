"""Golden-set scoring: Recall@5 per retrieval arm. Pure logic — `evals/run_retrieval.py` wires it
to a live index.

Golden format: a `questions` list of `q` + `expect.pages` (page ids — frontmatter `id`, or the
file stem) plus an OPTIONAL `filters` object handed straight to `search.search_arms`. `filters`
is carried, never interpreted: this module stays pure (no `search` import, no DB); an illegal
column is `_filter_clause`'s `ValueError`, and `tests/index/test_golden.py` checks the shipped
set keylessly.
"""
import json
from pathlib import Path

from stigmergy.index.rank import chain_base

# fts / vec / rrf are the individual arms, for comparable numbers; `final` is the product ranking
# (RRF + contract factors) and the arm the R@5 >= 0.80 bar is read on.
ARMS = ("fts", "vec", "rrf", "final")


def load_golden(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    questions = data["questions"] if isinstance(data, dict) else data
    out = []
    for item in questions:
        expect = item["expect"]["pages"] if isinstance(item.get("expect"), dict) else item["expect"]
        if not expect:
            raise ValueError(f"golden question without expected pages: {item.get('q')!r}")
        out.append({"id": item.get("id", ""), "q": item["q"], "expect": list(expect),
                    "filters": dict(item.get("filters") or {})})
    return out


def recall_at_k(expected: list[str], ranking: list[str], k: int) -> float:
    """CHAIN-equivalent: surfacing a split document's part (`X-p3`) IS surfacing document `X` —
    rank's top-k collapses a chain to its best-scoring member, and which member that is must not
    decide a hit (the golden always expects the base id)."""
    top = {chain_base(r) for r in ranking[:k]}
    return sum(1 for e in expected if chain_base(e) in top) / len(expected)


def hit_at_k(expected: list[str], ranking: list[str], k: int) -> bool:
    top = {chain_base(r) for r in ranking[:k]}
    return any(chain_base(e) in top for e in expected)


def evaluate(questions: list[dict], arm_rankings_fn, k: int = 5) -> dict:
    """Score every question. `arm_rankings_fn(q, filters)` returns {arm: [page ids, best first]}
    for each of ARMS; reports per-arm mean recall@k and hit@k plus per-question detail. The
    callback takes TWO arguments, REQUIRED: an optional-second-argument signature would let a
    caller silently drop a question's declared filters — measuring the wrong thing while
    reporting a number."""
    detail = []
    sums = {arm: {"recall": 0.0, "hits": 0} for arm in ARMS}
    for item in questions:
        filters = item.get("filters") or {}
        rankings = arm_rankings_fn(item["q"], filters)
        row = {"id": item["id"], "q": item["q"], "expect": item["expect"], "filters": filters}
        for arm in ARMS:
            ranking = rankings[arm]
            r = recall_at_k(item["expect"], ranking, k)
            h = hit_at_k(item["expect"], ranking, k)
            sums[arm]["recall"] += r
            sums[arm]["hits"] += h
            row[arm] = {"recall": round(r, 3), "hit": h, "top": ranking[:k]}
        detail.append(row)
    n = len(questions)
    summary = {arm: {"recall_at_k": round(s["recall"] / n, 3), "hit_at_k": round(s["hits"] / n, 3)}
               for arm, s in sums.items()}
    return {"k": k, "questions": n, "summary": summary, "detail": detail}


def render_report(report: dict) -> str:
    k = report["k"]
    lines = [f"questions={report['questions']}  k={k}",
             f"{'arm':<5} {'R@' + str(k):<7} {'hit@' + str(k):<7}"]
    for arm in ARMS:
        s = report["summary"][arm]
        lines.append(f"{arm:<5} {s['recall_at_k']:<7.3f} {s['hit_at_k']:<7.3f}")
    for row in report["detail"]:
        misses = [arm for arm in ARMS if not row[arm]["hit"]]
        if misses:
            lines.append(f"  MISS [{','.join(misses)}] {row['q']}")
    return "\n".join(lines)
