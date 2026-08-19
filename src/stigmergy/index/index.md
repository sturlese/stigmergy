# index — the hybrid derived index

A derived, disposable search layer over a knowledge-repo checkout: Postgres + pgvector, an FTS
arm and a vector arm fused with RRF, then explainable contract ranking — every hit carries the
factors that shaped its score. Never a source of truth (git is): `pages_index` is dropped and
rebuilt at will; `embedding_cache`, `index_meta`, `ops_file_snapshot` and `webhook_deliveries`
survive. `ops_file_snapshot` caches repo-derived files the SERVER reads rather than anything
retrieval queries — the knowledge repo's `ops/entity-registry.json`, `ops/identities.json` and
`ops/slack-channels.json`, put here because the deployed process groups hold no checkout and were
otherwise served copies baked at deploy time: a mint had no name until the next rollout (issue
#74) and a revoked identity kept resolving until it (issue #79). `webhook_deliveries` is the page
road's replay protection, one row per applied delivery id. The package knows no
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
  SELECTs through this same `PAGE_COLUMNS`. Also the relpath-keyed `ops_file_snapshot`
  (`OPS_FILE_RELPATHS` is the one spelling of the three cached files, and
  `CLEARED_WHEN_CHECKOUT_LACKS` the one per-file reconcile posture): `read_ops_file` (`None` when
  the database has none — the table is PROBED, so an index built before it existed reads as "no
  snapshot" rather than erroring; `""` is a real empty snapshot and every access reader fails
  CLOSED on it), `read_ops_file_meta` (`source`/`refreshed_at` — the console's "is what I am
  serving fresh, and from which sha?"), `write_ops_file` (creates the table on the way in, so an
  incremental refresh never waits for a rebuild), `clear_ops_file` (returns whether a snapshot was
  actually destroyed, which keeps the rebuild's warnings honest) and `ensure_ops_file_table` (the
  create-only startup seam that keeps the webhook's own create from racing inside its
  transaction; the rebuild road's `init_schema` is where issue #74's `entity_registry_snapshot`
  is retired). `MAX_OPS_FILE_BYTES` bounds what either writer may install — a per-request parse
  cost, not an ingest cost. The TEXT verbatim — each file's own reader owns what the bytes mean,
  and a second interpretation here is the drift a cache must not add. Beside it,
  `webhook_deliveries`: `ensure_webhook_dedupe_table`, `delivery_already_applied` and
  `record_delivery` (called on the webhook's own cursor inside phase 2, so a failed apply never
  records and manual redelivery still works).
- `build.py` — `rebuild()`: the one full-rebuild entry point, cache-aware, one transaction (a
  mid-rebuild failure leaves the previous index standing). It reconciles every ops-file snapshot
  inside that same transaction — the nightly counterpart to the push webhook's incremental
  refresh — with one decision per file (`_reconcile_ops_files`): written when the checkout
  carries it, cleared when absent only for the registry, KEPT for the access files (a cron must
  never restore a deploy-baked roster), kept on oversize for all three (the webhook's own
  posture). Nothing is silent: `ops_files` in the returned stats, a warning per clear, an error
  per keep.
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
  mismatched vector space returns noise, not an error). 'openai' names the DIALECT: the host is
  `EMBED_BASE_URL` (OpenAI by default) with `EMBED_API_KEY` as its credential and `EMBED_MODEL`
  as the build-time default model; an explicit model (the index's recorded one) always wins, and
  `OPENAI_API_KEY` falls back for the DEFAULT host only — it is never sent to another host.
  `EMBED_DIMENSIONS` (MRL truncation, request-level `dimensions`) is what fits a 4096-native
  model under the schema's 4000-dim HNSW ceiling; build and query must agree with the index.
  The rebuild records the embedding HOST in `index_meta` beside model/dim, and `search_arms`
  refuses a mismatch by name before the first embedding (a legacy index without one skips the
  check until its next rebuild).
  `backends/fake_embedder.py` — the deterministic keyless double for tests/CI.

## Reuse / avoid

Reuse the seams — `page_row`, `resolve_links`, `entity_list`, `chain_base` (its callers span
collapse, propagation, the lint and golden scoring: change it and all move together), `search`,
`rank`/`contract_factors`, the store writers, `rebuild` — rather than re-deriving parsers, column
lists or WHERE clauses. A ranking signal the service must resolve is a new TOLD parameter, never
inferred here. Operator entry points: `make index-rebuild`, `make index-check`,
`make retrieval-golden`.

Tests: `tests/index/` (pure suites keyless and DB-less; Postgres suites use `make db-up`'s
`stigmergy_test` and skip without it), plus the layering rules in `tests/test_architecture.py`.
