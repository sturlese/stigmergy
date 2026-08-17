# index — the hybrid derived index

A derived, disposable search layer over a knowledge-repo checkout: Postgres + pgvector, an FTS
arm and a vector arm fused with RRF, then explainable contract ranking — every hit carries the
factors that shaped its score. Never a source of truth (git is): `pages_index` is dropped and
rebuilt at will; `embedding_cache`, `index_meta` and `entity_registry_snapshot` survive. That last
one caches repo-derived data the SERVER reads rather than anything retrieval queries — the
knowledge repo's `ops/entity-registry.json`, put here because the deployed process groups hold no
checkout and were otherwise served a copy baked at deploy time (issue #74). The package knows no
identity — it
returns rows, and `server.acl.visible()` decides access above it — and imports no writer: it
carries its own frontmatter parser, so a writer refactor cannot change what gets indexed.
Narrative doc: [`docs/reference/hybrid-index.md`](../../../docs/reference/hybrid-index.md).

## Modules

- `corpus.py` — checkout → `PageRow`s, pure and DB-less. `ZONES` is the ONLY governing list;
  `page_row` is THE single-file parser (both `load_pages` and the webhook call it);
  `split_frontmatter_checked` / `_acl_labels` fail CLOSED (an unreadable or malformed `acl`
  indexes `[]`, visible to nobody — a loud gap beats a silent leak); `entity_list` fails closed
  too (malformed elements dropped, never stringified); `resolve_links`/`by_stem_index` are the
  one wikilink resolution; `is_chain_primary`/`chain_part_pattern` decide split-chain siblings;
  build-time `superseded_by` propagation is marker-gated, directional and directory-keyed.
- `rank.py` — pure ranking, no DB: `rrf_fuse`, `contract_factors` (divisors of the RRF score —
  superseded 4.0, evergreen 0.8, told entity hint 0.5, period 0.6, fresh 0.7, stale 1.3),
  `chain_base`, `rank()` (RRF → factors → sort → chain collapse → top-k), `_snippet`. `today` is
  injected, never the wall clock; the entity boost fires only on a TOLD `entity_hint`; `inlinks`
  is deliberately not a factor (measured, rejected — link-degree rewards hubs).
- `search.py` — the shared base query every caller rides: both arms under the same
  `_filter_clause` and pool, `FILTER_COLUMNS` (the one list of filterable columns; `entity` is
  membership over `text[]`), `search()`/`search_arms()`, the told parameters `entity_hint` and
  `fts_expansion` (lexical arm only). Never add a raw `acl` filter — enforcement is above.
- `store.py` — all SQL DDL and writes: `init_schema` drops `pages_index` BY NAME (the durable
  tables share the database), `create_search_indexes` (after the bulk load), the writers over the
  one column list `PAGE_COLUMNS` (`_UPSERT_SET` excludes `inlinks` so an update never clobbers
  the rebuild's count), the embedding cache, `read_meta`, autocommit `connect`, `dsn`,
  `host_of_dsn`. Never a second column list, never a non-autocommit reader — `search.fetch_pages`
  SELECTs through this same `PAGE_COLUMNS`. Also the singleton `entity_registry_snapshot`:
  `read_entity_registry` (`None` when the database has none — the table is PROBED, so an index
  built before it existed reads as "no snapshot" rather than erroring), `read_entity_registry_meta`
  (`source`/`refreshed_at` — the console's "is my registry fresh, and from which sha?"),
  `write_entity_registry` (creates the table on the way in, so an incremental refresh never waits
  for a rebuild), `clear_entity_registry` and `ensure_entity_registry_table` (the startup seam that
  keeps that create from ever racing inside the webhook's transaction). `MAX_ENTITY_REGISTRY_BYTES`
  bounds what either writer may install — a per-request parse cost, not an ingest cost. The TEXT
  verbatim — `server.entity_aliases` owns what the bytes mean, and a second interpretation here is
  the drift a cache must not add.
- `build.py` — `rebuild()`: the one full-rebuild entry point, cache-aware, one transaction (a
  mid-rebuild failure leaves the previous index standing). It reconciles the registry snapshot
  inside that same transaction — written from `registry_path(repo)`, CLEARED when the checkout has
  none or carries an oversized one — the nightly counterpart to the push webhook's incremental
  refresh. The clear is never silent: a warning, and `entity_registry` in the returned stats.
- `check.py` — `run_checks()`: the substrate lint over the live index; errors exit 1 via the CLI.
  `served_registry()` picks WHICH registry copy it lints — the index's snapshot, else the
  `--entity-registry` file — so the console and the server never answer differently about which
  entities are registered.
- `golden.py` — golden-set loading and per-arm recall/hit scoring, chain-equivalent, pure; the
  set lives in `evals/retrieval_golden.json` and every observed miss grows it.
- `cli.py` — `stigmergy-index` (`--rebuild` xor `--check`) and `stigmergy-search`; thin parsing,
  domain errors translated to exit codes.
- `errors.py` — `StigmergyIndexError` / `EmptyIndexError` / `EmptyCorpusError`; the library never
  raises `SystemExit`.
- `backends/embedder.py` — `OpenAIEmbedder` plus `build_embedder`/`embedder_for_model`, the one
  fake/real dispatch (fake imported deferred; never suggest it as a keyless substitute — a
  mismatched vector space returns noise, not an error). `backends/fake_embedder.py` — the
  deterministic keyless double for tests/CI.

## Reuse / avoid

Reuse the seams — `page_row`, `resolve_links`, `entity_list`, `chain_base` (its callers span
collapse, propagation, the lint and golden scoring: change it and all move together), `search`,
`rank`/`contract_factors`, the store writers, `rebuild` — rather than re-deriving parsers, column
lists or WHERE clauses. A ranking signal the service must resolve is a new TOLD parameter, never
inferred here. Operator entry points: `make index-rebuild`, `make index-check`,
`make retrieval-golden`.

Tests: `tests/index/` (pure suites keyless and DB-less; Postgres suites use `make db-up`'s
`stigmergy_test` and skip without it), plus the layering rules in `tests/test_architecture.py`.
