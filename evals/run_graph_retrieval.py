#!/usr/bin/env python3
"""Graph-search retrieval runner over the frozen graph corpus.

Runs every question in `evals/graph_golden.json` through `BrainService`'s search, the same
resolution, ranking, and ACL path clients reach, and reports per family:

  - recall at 8 and at each width in `--widths`: share of expected pages returned;
  - MRR: reciprocal rank of the first expected page;
  - entity share: entity pages among the first 8 results;
  - leaks: forbidden pages returned to a scoped identity, which must be zero.

Latency is measured over `--passes`. The first pass embeds each query; later passes reuse the query
vector, so they time the database and ranking work alone. The instrument calls the service's
search body directly, below the per-identity rate limit and audit wrapper.

  python evals/run_graph_retrieval.py --embedder fake --rebuild          # plumbing only
  python evals/run_graph_retrieval.py --embedder openrouter --rebuild --live --max-usd 0.05

`--ablate` switches off graph features inside this process only, to measure each one's share:
`graph` (neighbor ranking), `intersection` (pages linking every named entity), and `multi-entity`
(resolve only the first named entity). Production code has no such switch.
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import eval_history  # noqa: E402
import spend  # noqa: E402
import workspace  # noqa: E402

from stigmergy.index import build, measures, search, store  # noqa: E402
from stigmergy.index.backends.embedder import build_embedder  # noqa: E402
from stigmergy.server.identity import resolve_principal  # noqa: E402
from stigmergy.server.service import BrainService  # noqa: E402
from stigmergy.server.settings import Settings  # noqa: E402

TOP = 8


class _QueryVectorCache:
    """Reuse one vector per query after the first pass, so later passes time search alone."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self._vectors: dict[str, list[float]] = {}

    def embed(self, texts, *args, **kwargs):
        texts = list(texts)
        if len(texts) == 1 and texts[0] in self._vectors:
            return [self._vectors[texts[0]]]
        vectors = self._inner.embed(texts, *args, **kwargs)
        if len(texts) == 1:
            self._vectors[texts[0]] = vectors[0]
        return vectors

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _ablate(features: set[str]) -> None:
    unknown = features - {"graph", "intersection", "multi-entity"}
    if unknown:
        raise SystemExit(f"unknown --ablate feature(s): {sorted(unknown)}")
    if "graph" in features and hasattr(search, "neighbor_ranking"):
        search.neighbor_ranking = lambda *args, **kwargs: []
    if "intersection" in features and hasattr(search, "intersection_ranking"):
        search.intersection_ranking = lambda *args, **kwargs: []
    if "multi-entity" in features and hasattr(BrainService, "_entities_named_in"):
        resolve = BrainService._entities_named_in
        BrainService._entities_named_in = lambda self, question: resolve(self, question)[:1]


def _percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, int(round(share * len(ordered))) - 1)]


def score_question(case: dict, hits: list[dict], widths: list[int], entity_paths: set[str],
                   forbidden_titles: set[str] = frozenset(), cited: dict[str, set[str]] | None = None) -> dict:
    """`recall@k` counts the expected pages in the first k hits; `reach@k` also counts an expected
    source one of those hits cites, which the answering agent reads one hop away."""
    ranking = [h["path"] for h in hits]
    expected = list(case.get("expect", {}).get("pages", []))
    forbidden = set(case.get("forbid", []))
    row = {"id": case["id"], "family": case.get("shape", case["family"]), "top": ranking[:TOP]}
    if expected:
        for width in sorted({TOP, *widths}):
            found = sum(1 for page in expected if page in ranking[:width])
            row[f"recall@{width}"] = round(found / len(expected), 4)
            near = set(ranking[:width]) | {source for page in ranking[:width] for source in (cited or {}).get(page, ())}
            row[f"reach@{width}"] = round(sum(1 for page in expected if page in near) / len(expected), 4)
        first = next((i for i, page in enumerate(ranking, 1) if page in expected), None)
        row["mrr"] = round(1 / first, 4) if first else 0.0
    row["entity_share"] = round(sum(1 for p in ranking[:TOP] if p in entity_paths) / max(1, len(ranking[:TOP])), 4)
    linked = {link["path"] for h in hits for link in h.get("links") or ()}
    titles = {link["title"] for h in hits for link in h.get("links") or ()}
    row["leaks"] = sorted((forbidden & (set(ranking) | linked)) | (forbidden_titles & titles))
    return row


def aggregate(rows: list[dict], widths: list[int]) -> dict:
    families: dict[str, dict] = {}
    keys = [f"{measure}@{w}" for measure in ("recall", "reach") for w in sorted({TOP, *widths})]
    keys += ["mrr", "entity_share"]
    for family in sorted({r["family"] for r in rows}):
        members = [r for r in rows if r["family"] == family]
        summary = {"questions": len(members)}
        for key in keys:
            values = [r[key] for r in members if key in r]
            if values:
                summary[key] = round(statistics.mean(values), 4)
        families[family] = summary
    return {"families": families, "leaks": sum(len(r["leaks"]) for r in rows)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", default=str(ROOT / "evals" / "graph_golden.json"))
    ap.add_argument("--identities", default=str(ROOT / "evals" / "graph_identities.json"))
    ap.add_argument("--repo", default=str(ROOT / "evals" / "graph_corpus"))
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--embedder", choices=["openrouter", "fake"], default="openrouter")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--widths", default="8,16,32")
    ap.add_argument("--passes", type=int, default=5)
    ap.add_argument("--label", default="", help="free-text label recorded with the history row")
    ap.add_argument("--ablate", default="", help="comma list: graph, intersection, multi-entity")
    ap.add_argument("--report", default=None)
    spend.add_budget_arguments(ap)
    args = ap.parse_args()
    widths = [int(w) for w in args.widths.split(",") if w.strip()]
    _ablate({a.strip() for a in args.ablate.split(",") if a.strip()})
    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    paid = args.embedder == "openrouter"
    budget = spend.require_budget(ap, args, "graph-retrieval", paid=paid,
                                  expected="under 0.01 USD with a warm embedding cache",
                                  models=("openai/text-embedding-3-large",))
    embedder = build_embedder(args.embedder)
    if budget is not None:
        embedder = spend.MeteredEmbedder(embedder, budget)
    with store.connect(args.dsn) as conn:
        workspace.bind(conn)
        if args.rebuild:
            stats = build.rebuild(conn, args.repo, embedder)
            print(f"rebuilt: {stats['pages']} pages", file=sys.stderr)
        cached = _QueryVectorCache(embedder)
        services: dict[str, BrainService] = {}

        def service(identity: str) -> BrainService:
            if identity not in services:
                principal = resolve_principal(args.identities, identity)
                audiences = principal.audiences
                settings = Settings(identity=identity, llm="fake")
                services[identity] = BrainService(settings, conn, cached,
                                                  set(audiences) if audiences is not None else None,
                                                  identity=identity, principal=principal)
            return services[identity]

        with conn.cursor() as cur:
            cur.execute("SELECT path FROM pages_index WHERE type = 'entity'")
            entity_paths = {row[0] for row in cur.fetchall()}
            cur.execute("SELECT path, title FROM pages_index")
            titles = dict(cur.fetchall())
            cur.execute("SELECT vpath, acl, sources FROM pages_index WHERE type <> 'source'")
            cited_rows = cur.fetchall()
        graph = measures.compute(measures.index_rows(conn), measures.BRAIN)["measures"]
        width = max(widths)
        rows, first_ms, warm_ms = [], [], []
        for n in range(args.passes):
            for case in golden["questions"]:
                if budget is not None and n == 0:
                    budget.check()
                svc = service(case.get("identity", golden.get("default_identity", "steward")))
                started = time.perf_counter()
                result = svc._search(case["q"], case.get("filters"), width)
                elapsed = (time.perf_counter() - started) * 1000
                (first_ms if n == 0 else warm_ms).append(elapsed)
                if n == 0:
                    forbidden_titles = {titles[p] for p in case.get("forbid", []) if p in titles}
                    audiences = svc.audiences
                    cited: dict[str, set[str]] = {}
                    for vpath, acl, sources in cited_rows:
                        if acl is None or audiences is None or set(acl) & set(audiences):
                            cited.setdefault(vpath, set()).update(sources or ())
                    rows.append(score_question(case, result["hits"], widths, entity_paths,
                                               forbidden_titles, cited))
    report = {**aggregate(rows, widths), "questions": len(rows), "graph": graph,
              "latency_ms": {"first_pass_p50": round(statistics.median(first_ms), 1),
                             "first_pass_p90": round(_percentile(first_ms, 0.9), 1),
                             "warm_p50": round(statistics.median(warm_ms), 1) if warm_ms else None,
                             "warm_p90": round(_percentile(warm_ms, 0.9), 1) if warm_ms else None},
              "rows": rows}
    if budget is not None:
        report["spend"] = budget.summary()
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if paid:
        eval_history.append_run(
            suite="graph-retrieval", git_sha=eval_history.resolve_git_sha(ROOT),
            metrics={"label": args.label, "ablate": args.ablate, "families": report["families"],
                     "leaks": report["leaks"],
                     "latency_ms": report["latency_ms"], "questions": len(rows), "graph": graph,
                     **eval_history.corpus_provenance(args.repo)})
    return 1 if report["leaks"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
