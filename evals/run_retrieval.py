#!/usr/bin/env python3
"""Recall@k runner over the retrieval golden set — per-arm numbers.

Needs a running Postgres and, unless `--rebuild` is given, a built index. `--embedder fake` checks
plumbing and idempotency only; its numbers mean nothing semantically. The corpus is `evals/corpus/`,
committed and frozen so the series stays comparable.

  # keyless self-check / e2e arm:
  python evals/run_retrieval.py --embedder fake --rebuild --repo evals/corpus

  # the real measurement (needs OPENROUTER_API_KEY):
  python evals/run_retrieval.py --embedder openrouter --rebuild --repo evals/corpus \
      --report evals/out/retrieval-report.json

Arms: fts / vec / rrf are the raw retrieval arms; `final` adds the contract factors and is the arm
`bars.BAR_RECALL` is read on.

This measures the ranking SUBSTRATE — arms, fusion, contract factors, chain collapse — and
deliberately does NOT resolve entities: `search_arms` gets no `entity_hint`/`fts_expansion`, so the
entity boost and the alias expansion never fire. The served path is `run_qa.py`'s job; teaching this
runner to resolve would make it measure a hybrid nobody serves.

Each question's declared `filters` ARE forwarded (`make_arm_rankings`). Without that the run is
structurally blind to the `entity` FILTER — a broken membership clause could not move the numbers,
because the run never asked it to filter. Part of the golden set is a deliberate unfiltered control.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:                                    # run as a script: evals/ is sys.path[0], sibling import
    import eval_history  # noqa: E402
except ModuleNotFoundError:             # imported as `evals.run_retrieval` (the unit tests)
    from evals import eval_history  # noqa: E402

from stigmergy.index import build, golden, rank, search, store  # noqa: E402
from stigmergy.index.backends.embedder import build_embedder, embedder_for_model  # noqa: E402
from stigmergy.index.errors import StigmergyIndexError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", default=str(ROOT / "evals" / "retrieval_golden.json"))
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--embedder", choices=["openrouter", "fake"], default=None,
                    help="default: whatever the built index was embedded with (index_meta)")
    ap.add_argument("--rebuild", metavar="", nargs="?", const=True, default=False,
                    help="rebuild the index first (requires --repo)")
    ap.add_argument("--repo", default=None, help="knowledge-repo checkout (with --rebuild)")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--report", default=None, help="write the full JSON report here")
    args = ap.parse_args()
    if args.rebuild and not args.repo:
        ap.error("--rebuild requires --repo")

    questions = golden.load_golden(args.golden)
    try:
        return _run(args, questions)
    except StigmergyIndexError as ex:
        sys.exit(str(ex))               # domain error -> exit code; the library never exits


def make_arm_rankings(conn, embedder, k: int):
    """The per-question ranking callback `golden.evaluate` drives. A factory rather than a closure
    inside `_run` so that "a question's `filters` reach `search_arms`" is provable without a
    Postgres."""
    def arm_rankings(q: str, filters: dict | None = None) -> dict:
        arms = search.search_arms(conn, q, embedder=embedder, k=k, filters=filters or None)
        ids = arms["page_ids"]
        fused = sorted(rank.rrf_fuse([arms["fts"], arms["vec"]]).items(),
                       key=lambda kv: (-kv[1], kv[0]))
        return {"fts": [ids[p] for p in arms["fts"]],
                "vec": [ids[p] for p in arms["vec"]],
                "rrf": [ids[p] for p, _ in fused],
                "final": [h["page_id"] for h in arms["hits"]]}
    return arm_rankings


def _run(args, questions) -> int:
    with store.connect(args.dsn) as conn:
        if args.rebuild:
            embedder = build_embedder(args.embedder or "openrouter")
            stats = build.rebuild(conn, args.repo, embedder)
            print(f"rebuilt: {stats['pages']} pages · {stats['embedded']} new embeddings, "
                  f"{stats['cached']} cached · model={stats['model']}")
        meta = store.read_meta(conn)
        if meta is None:
            sys.exit("the index is empty — pass --rebuild --repo <dir> or build it first")
        embedder = (build_embedder(args.embedder) if args.embedder
                    else embedder_for_model(meta["model"]))

        report = golden.evaluate(questions, make_arm_rankings(conn, embedder, args.k), k=args.k)

    report["embedder"] = embedder.model
    print(golden.render_report(report))
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                     encoding="utf-8")
        print(f"report -> {args.report}")
    # Only a run whose RESOLVED embedder is real appends to the durable series.
    if not report["embedder"].startswith("fake"):
        eval_history.append_run(
            suite="retrieval", git_sha=eval_history.resolve_git_sha(ROOT),
            metrics={"recall_at_5": report["summary"]["final"]["recall_at_k"], "k": args.k,
                    "embedder": report["embedder"], "questions": report["questions"],
                    "filtered_questions": sum(1 for q in questions if q["filters"]),
                    **eval_history.corpus_provenance(args.repo)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
