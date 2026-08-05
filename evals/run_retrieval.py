#!/usr/bin/env python3
"""Recall@5 runner over the retrieval golden set (ADR 012) — per-arm numbers.

Assumes a running Postgres (docker compose up) and, unless --rebuild is given, an already
built index. Offline with --embedder fake (plumbing + idempotency only — the numbers mean
nothing semantically); the exit-evidence measurement uses the real embedder:

The corpus is `evals/corpus/` — committed and frozen, so the series it feeds stays comparable.
See `evals/corpus/PROVENANCE.json`.

  # keyless self-check / e2e arm:
  python evals/run_retrieval.py --embedder fake --rebuild --repo evals/corpus

  # the real measurement (needs OPENAI_API_KEY):
  python evals/run_retrieval.py --embedder openai --rebuild --repo evals/corpus \
      --report evals/out/retrieval-report.json

Arms: fts / vec / rrf are the raw retrieval arms; `final` adds the contract factors — the arm the
R@5 >= 0.80 bar is read on.

**Substrate posture:** this runner measures the ranking SUBSTRATE — arms, fusion, contract
factors, chain collapse — and deliberately does NOT resolve entities: `search_arms` is called with
no `entity_hint`/`fts_expansion`, so the entity boost and the alias expansion (both fed by the
SERVICE's registry resolution) never fire here. The served path's fidelity is the QA golden's job
(`run_qa.py`, which builds the very `Settings` the deployed server runs with). Teaching this
runner to resolve would make it measure a hybrid nobody serves — the blended search with a TOLD
`entity_hint` lives in `BrainService._search` and is pinned by
`tests/server/test_entity_first_search_pg.py`.

**Why the questions carry `filters`.** Called as `search.search_arms(conn, q, embedder=embedder,
k=args.k)` with no `filters` argument, the golden run is a real, moveable witness for the
FTS/vector folding and the `entity` BOOST, but structurally BLIND to the `entity` FILTER (the
`%(entity)s = ANY(entity)` membership clause): a broken filter could not change the run's output
no matter how broken, because the run never asked it to filter anything. `make_arm_rankings` below
forwards each question's declared `filters` to `search_arms`, and 10 of the 16 golden questions
declare `filters.entity` (the ones whose subject is a named entity — see
`retrieval_golden.json`'s `_filters_comment` for why the other 6 are a deliberate unfiltered
control half).

Sabotage-verified, which is the only proof that matters here: inverting the membership clause in
`index/search.py` (`= ANY(entity)` -> `<> ALL(entity)`) moves this run's numbers. It could not,
before. `tests/index/test_pg_integration.py`'s Postgres tests remain the FILTER's unit-level
evidence; this run is now its end-to-end one.
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
    ap.add_argument("--embedder", choices=["openai", "fake"], default=None,
                    help="default: whatever the built index was embedded with (index_meta)")
    ap.add_argument("--model", default=None)
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
    """The per-question ranking callback `golden.evaluate` drives, as a factory so the one thing
    that matters most — that a question's `filters` actually REACH `search_arms` — is provable
    without a Postgres (`tests/evals/test_run_retrieval_filters.py`). A closure built inside
    `_run` is only ever testable by a keyed, DB-bound end-to-end run, which is how a gap like that
    survives unnoticed."""
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
            embedder = build_embedder(args.embedder or "openai", args.model)
            stats = build.rebuild(conn, args.repo, embedder)
            print(f"rebuilt: {stats['pages']} pages · {stats['embedded']} new embeddings, "
                  f"{stats['cached']} cached · model={stats['model']}")
        meta = store.read_meta(conn)
        if meta is None:
            sys.exit("the index is empty — pass --rebuild --repo <dir> or build it first")
        embedder = (build_embedder(args.embedder, args.model) if args.embedder
                    else embedder_for_model(meta["model"]))

        report = golden.evaluate(questions, make_arm_rankings(conn, embedder, args.k), k=args.k)

    report["embedder"] = embedder.model
    print(golden.render_report(report))
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                     encoding="utf-8")
        print(f"report -> {args.report}")
    # A real-instrument run — the embedder actually resolved is not the fake one
    # (`embedder_for_model`'s own `model.startswith("fake")` convention) — appends R@5 (the arm the
    # exit bar reads on, per this module's own docstring) to the durable, git-resident series.
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
