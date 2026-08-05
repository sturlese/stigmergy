# ADR 012 — The hybrid derived index: Postgres + pgvector, RRF, contract-aware ranking

**Status:** accepted · 2026-07-19

## Context

The brain's corpus (authored `wiki/`, machine `sources/` + `views/`) was only
reachable by grepping or reading files — which fails exactly where the product must win:
questions arrive as whole sentences, and in a **different language from the pages**, and
loses recall systematically. A spike measured it instead of assuming it, over a 41-page
assuming it, over a 41-page mixed corpus (7 real + benchmark/demo synthetic) and 20 real
real embedder:

| Arm | hit@5 |
|---|---|
| FTS only (Postgres `tsvector`/`ts_rank_cd`, OR-of-lexemes) | **0.60** |
| Vector only (pgvector cosine, OpenAI `text-embedding-3-large`) | **1.00** |
| RRF hybrid (k=60) | **0.95** |

Retrieval also has contract obligations the write path already pays for: superseded pages,
extraction quality, ACL labels, maturity status. A retrieval layer that ignores them serves
stale or untrustworthy truth with confidence.

## Decision

A **derived, disposable hybrid index** — Postgres + pgvector (`pages_index`), rebuilt at will
from a checkout of the knowledge repo, never a source of truth. `stigmergy.index` shares no code
with the packages that produce what it indexes: it reads the repo and nothing else, so a change
to how pages are written can never silently change what gets indexed. `tests/index/test_architecture.py`
holds that door shut — the index reaches for no writer.

1. **Both arms, RRF-fused.** Lexical: native FTS, **OR-of-lexemes** (AND semantics measure
   0 on whole natural-language questions by construction). Semantic: pgvector cosine over
   `text-embedding-3-large` — the spike measured ES→EN hit@5 = 1.00 with it. Fusion:
   Reciprocal Rank Fusion, k=60.

   RRF is kept **despite** vec-only winning the spike (1.00 vs 0.95): at 41 pages the
   semantic arm can afford to win alone; at scale FTS is what rescues exact identifiers and
   proper nouns. The golden set arbitrates — if rrf stays below vec-only on the golden set too,
   that becomes a documented gap decision, not a silent tweak.

2. **Contract-aware ranking, explainable.** After fusion, deterministic factors (`stigmergy.index.rank`):
   superseded pages heavily demoted (current truth first; history stays reachable), exact
   entity/period matches boosted, "current/latest"-style questions
   prefer fresher `as_of`, `status: evergreen` outranks `seed` on equal relevance, and stale
   pages are penalized past a 365-day horizon — against an **injected** `today`, never the wall
   clock, so ranking is testable and reproducible.
   **Every hit carries the list of factors applied to it** — "why did this page rank here"
   is always answerable. The original rationale (BM25-era) chose deterministic factors over
   an opaque learned ranker precisely for that answerability; the hybrid keeps the factors
   and swaps only the base relevance under them.

   Two factors this ADR also specified — `verification: failed` demoted hardest, then `partial`,
   and `manual_review` demoted — are gone, along with the field that fed them
   ([ADR 026](./026-the-purge.md)). Nothing computes a verification verdict any more, and a
   ranking input with no producer is a score nobody can reason about.

3. **The index is a cache.** `pages_index` is **never migrated**: every rebuild drops and
   recreates it — wipe and rebuild is the upgrade path. The e2e proof keeps this honest:
   fixture repo → build → golden run → wipe volumes → rebuild → **identical hit lists**. Two
   tables survive a rebuild: `embedding_cache`, keyed by `(model, content_hash)`, so rebuilds
   re-embed only pages whose content changed — which is why native `content_hash` emission had
   to land before the index could be built at all — and `index_meta`, one row naming the model,
   the dimension, the FTS config and when the index was last built, so staleness is
   self-diagnosing. The index's `content_hash` column is the hash of the **embedded text**
   (title + body): authored pages carry no provenance hash, and split parts share one, so the
   cache key must derive from what is actually embedded, not from the raw source bytes.

   On the feeding side, the source-page splitter splits **any** body over the 150-line cap, not
   only `representation: full` sources — deliberately broader than the contract requires: the
   split preserves all content, so it can only ever *prevent* a contract violation, never
   cause one.

4. **Zones and ACL.** Indexed: `wiki/`, `sources/`, `views/`. Excluded: `ops/`,
   `meta/`, `datasets/`. The `acl` column is **stored here and enforced by the server**
   ([ADR 010](./010-acl.md)), as a Postgres `text[]` (labels survive verbatim — no delimiter can
   be lossy), preserving the NULL-vs-empty distinction (`NULL` = no acl, open; `{}` = empty acl,
   nobody). Parsing **fails closed**: a page whose `acl` is present but malformed (mapping,
   boolean, blank) indexes as visible to nobody — a loud retrieval gap beats a silent leak;
   the one forgiven shape is a bare scalar (`acl: sales`), read as the one-label list it
   obviously meant.

5. **Offline-first.** A deterministic fake embedder (hashed bag-of-words, ported from the
   spike) stands in for the real one in tests and CI — CI needs no API keys, ever. It is
   reached only through a deferred import inside `build_embedder` (the offline-double rule), so
   production never loads the double.

## Consequences

- `search_brain` and `ask` consume the index **as a library** — the schema carries their filter
  and ACL columns, and enforcement happens once, in the service.
- The retrieval golden (`evals/retrieval_golden.json`, questions over the frozen corpus)
  scores Recall@5 **per arm**, so a ranking change shows which arm moved. It needs a real
  embedder, so it runs on demand and appends to `evals/history.ndjson`; CI stays keyless and
  checks the fixture's own integrity instead.
- Embedding cost is negligible at this scale (~pennies per rebuild with the cache); any
  model swap re-runs the goldens against the recorded baseline before promotion.

## Alternatives rejected

- **FTS only** — measured at 0.60 hit@5 on ES→EN; below the product bar, not an opinion.
- **`pg_search`/ParadeDB/BM25 upgrade** — only reconsidered if golden recall shows native
  FTS lacking; the spike's data closed the question for now.
- **Cross-encoder reranker** — deferred; the factor model must stay explainable while trust in
  the system is being built.
- **Voyage embeddings**, the original assumption — replaced by OpenAI after the spike validated
  its multilingual quality; provider re-evaluation is out of scope until the model-change
  procedure triggers it.

A full rebuild stayed the only write path for a while, on the grounds that it is cheap at this
scale and keeps the cache property provable. It is no longer the only one: a GitHub webhook
upserts the pages of a merged commit incrementally, through the *same* row builder and store
primitives the full walk uses, with the nightly rebuild as reconciliation. Two code paths writing
`pages_index` is exactly the drift that reuse exists to prevent.
