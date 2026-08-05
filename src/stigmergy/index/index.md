# index — the hybrid derived index

Narrative doc: [`docs/reference/index.md`](../../../docs/reference/index.md) — the what and the
where for an operator. Design record:
[ADR 012](../../../docs/decisions/012-hybrid-index.md), plus
[ADR 026](../../../docs/decisions/026-the-purge.md) (which removed the package this one used to be
policed against) and [ADR 022](../../../docs/decisions/022-entity-navigation.md) (the registry
coverage `check.py` warns about). Sibling contracts:
[`page-contract.md`](../../../docs/reference/page-contract.md) (who writes `entity:`),
[`navigation.md`](../../../docs/reference/navigation.md) (what `links` is served as).

This file is the code map — for whoever is about to edit this package, not run it.

## Purpose

A derived, disposable search layer over a checkout of the knowledge repo: Postgres + pgvector, a
lexical (FTS) arm and a semantic (vector) arm fused with RRF, then explainable contract ranking.
**Never a source of truth — git is.** `pages_index` is dropped and rebuilt from the repo whenever
convenient (`build.rebuild`, nightly via `.github/workflows/index-rebuild.yml` at 04:17 UTC), or
upserted/deleted one changed file at a time (`server.webhook`). Every hit carries the factors
that shaped its score, so "why did this page rank here" is always answerable.

**This package knows no identity.** It returns rows; `server.service` filters them through
`server.acl.visible`. That is a named, reasoned exception in the root suite, not an oversight —
`index/search.py`, `index/store.py`, `index/check.py` and `index/cli.py` are four of the entries in
`tests/test_architecture.py`'s `ACL_REACHABILITY_EXCEPTIONS`, each with its stated reason.

Who depends on it, verified against the real import graph:

| Consumer | Reaches |
|---|---|
| `server.service` | `search`, `rank`, `store`, `backends.embedder.embedder_for_model`, `errors` — the retrieval consumer, and the one that supplies `entity_hint`/`fts_expansion` |
| `server.webhook` | `corpus` + `store` — the incremental writer; the ONLY other producer of `pages_index` rows |
| `server.settings`, `server.pilot_report`, `answer.service` | `store` alone — connection and `read_meta` (`answer.service` for the `built_at` stamp it puts on every response) |
| `server.mcp_server` | `errors` alone — it maps `StigmergyIndexError` onto a tool response and never touches the store |
| `admin.service` | `store` (connection/meta) + `check` + `errors` — `index_state` reads `read_meta`, `index_substrate_check` runs `check.run_checks` IN PROCESS rather than shelling out to `stigmergy-index --check` |
| `views.skeleton`, `views.staleness` | `corpus` only — the pure repo parser, never the index itself (a disposable cache must not be a generator's input) |
| `capture.cli`, `capture.meeting_cli`, `capture.drive_cli`, `entities.cli`, `gardener.cli`, `digest.cli`, `librarian.cli`, `views.cli` | `store.connect` and nothing else — the shared connection seam, pinned per package (`test_only_capture_cli_may_import_the_index` covers all three capture CLIs; `test_only_entities_cli_imports_the_index` and the `gardener`/`digest`/`views` `..._stays_within_the_documented_edge_plus_its_own_db_connection` tests are its twins) |
| `evals/run_retrieval.py` | `build`, `golden`, `rank`, `search`, `store`, `backends.embedder`, `errors` — the retrieval measurement harness |
| `evals/run_qa.py` | `build`, `store`, `backends.embedder`, `errors` — the QA harness builds the frozen corpus and then asks through the server, never through `search` directly |

## Key entry points

| Module | Owns |
|---|---|
| `corpus.py` | repo checkout → `PageRow`s. The zone walk (`ZONES = wiki, sources, views` — the ONLY list that governs; there is no exclude-list), the tolerant `split_frontmatter`, `entity_list`'s fail-closed `entity:` dialect, `_acl_labels`' fail-closed ACL parse, `link_targets` → `by_stem_index` → `resolve_links` (outbound `links` + inbound `inlinks` from ONE resolution), the build-time `superseded_by` propagation onto split-chain siblings (`is_chain_primary` / `chain_part_pattern`, directory-keyed), `content_hash`. **`page_row` is THE single-file parser** both `load_pages` and the webhook call |
| `rank.py` | pure ranking, no DB: `rrf_fuse`, `contract_factors` (six factors), `chain_base`/`_PART_MARKER_RE`, `rank()` — RRF → factors → sort → **chain collapse** (one document, one top-k slot) → `[:k]`, and `_snippet`. `today` is injected for staleness, never the wall clock |
| `search.py` | the shared base query: both SQL arms under the SAME `_filter_clause` and candidate pool, `fetch_pages`, `search()` / `search_arms()`. Owns `FILTER_COLUMNS` and the two TOLD parameters, `entity_hint` and `fts_expansion` |
| `store.py` | all SQL DDL and writes: `init_schema` (drops `pages_index` **by name**), `create_search_indexes` (the two retrieval indexes, built AFTER the bulk load), `insert_pages`/`upsert_pages`/`delete_pages`, `current_content_hashes`/`existing_paths`, `pages_with_page_id_prefix`/`set_superseded_by` (the webhook's split-chain window), `read_meta`, the embedding cache, `connect` (autocommit) and `dsn` |
| `build.py` | `rebuild(conn, repo_dir, embedder, fts_config="english")` — the full rebuild, cache-aware, all of it in ONE transaction |
| `check.py` | **the substrate lint.** `run_checks(conn, registry_path)` → findings over the LIVE index (three ERROR classes, two WARN), `render`, `registry_ids`. Pure SQL + one optional file read |
| `golden.py` | golden-set loading (`load_golden`, carrying each question's `filters`) + per-arm `recall_at_k`/`hit_at_k`/`evaluate`/`render_report`. Chain-equivalent scoring. Pure — no `search` import, no DB |
| `cli.py` | `stigmergy-index` (`--rebuild` xor `--check`) and `stigmergy-search` — the two console scripts `pyproject.toml` declares (`index_main`, `search_main`). Thin argument parsing over the library |
| `errors.py` | `StigmergyIndexError`, `EmptyIndexError`, `EmptyCorpusError` — the library never raises `SystemExit`; the CLIs translate, the server maps to responses |
| `backends/embedder.py` | `OpenAIEmbedder` (`text-embedding-3-large`, 3072-dim, batches of 128, injectable `transport`) + `build_embedder` / `embedder_for_model` — the one fake/real dispatch, fake imported DEFERRED |
| `backends/fake_embedder.py` | `FakeEmbedder` — deterministic hashed bag-of-words, 256-dim, `model = "fake-hashed-bow-256"`. Keyless; tests/CI only |

Operator entry points: `make index-rebuild` (real embedder, local), `make index-check`,
`make rebuild-staging`, `make retrieval-golden`, `make e2e`.

**`stigmergy.index` never imports a writer** (`librarian`, `entities`, `views`, `capture`) —
parametrized over every module here by `tests/index/test_architecture.py`. The other half of the
rule was once "the index never imports the pipeline"; `stigmergy.pipeline` is gone whole
([ADR 026](../../../docs/decisions/026-the-purge.md) D4) and what survived it, `stigmergy.kernel`, is
a dependency-free library every package may depend on — so there is no second package left for this
one to be a sibling *of*. The suite says that explicitly
(`test_the_pipeline_it_used_to_be_a_sibling_of_is_gone`) rather than silently dropping the half
that no longer applies.

## Use these

- **`corpus.page_row(rel_path, zone, text)`** — THE single-file parser. `load_pages` (full walk)
  and `server.webhook.process_push` (one changed file) both call this and only this, so there is
  provably one parser rather than two that can drift.
- **`corpus.resolve_links` / `by_stem_index`** — THE outbound-wikilink resolution. `load_pages`
  builds `by_stem` from its own in-memory walk; the webhook builds it from `store.existing_paths`'s
  one-query snapshot. One algorithm, two snapshots; `tests/server/test_webhook.py` pins parity.
  `by_stem_index` excludes `views/` as a link TARGET — a view's filename is the entity id and
  collides case-insensitively with the Title-Case entity page's stem.
- **`corpus.entity_list`** — the ONE normalizer for `entity:`'s dialect (bare string, list, or
  absent) into the `text[]` column. Fails CLOSED: drops bools, drops nested list/dict elements
  rather than stringifying their `repr`, strips whitespace, folds `""` to `[]` (never `[""]`).
- **`corpus.chain_part_pattern` / `is_chain_primary`** — the ONE definition of "what counts as a
  continuation sibling", shared by `load_pages`'s build-time propagation and `server.webhook`'s
  incremental one. Two regexes here would drift; one cannot.
- **`rank.chain_base`** — the shared document id behind a split page. Five callers now, not one:
  `rank()`'s collapse key, `corpus.load_pages`'s build-time propagation, `corpus.is_chain_primary`
  (which `server.webhook`'s incremental propagation rides on), `check.run_checks`'s orphan check,
  and `golden.recall_at_k`/`hit_at_k`'s chain equivalence. Change it and all five move together.
- **`search.search` / `search_arms`** — the shared retrieval seam every caller rides (both CLIs,
  `evals/run_retrieval.py`, `server.service._run_search`). `FILTER_COLUMNS` is the one list of
  filterable columns; a new one is added there, never accepted ad hoc by a caller's own WHERE.
- **`rank.rank` / `contract_factors`** — the one ranking function. A new factor is a new
  `(constant, label)` entry in `contract_factors`, documented beside the existing constants, never
  a second scoring pass over `rank`'s output.
- **`store.upsert_pages` / `delete_pages` / `current_content_hashes`** — the ONLY incremental
  writers. A new incremental caller reuses these rather than re-deriving the row shape or the
  `ON CONFLICT` column list; `_PAGE_COLUMNS` / `_page_params` / `_INSERT_SQL` are the one column
  list, one params builder and one INSERT template behind both insert and upsert.
- **`build.rebuild`** — the only full-rebuild entry point; cache-aware, keyed on
  `(model, content_hash)`, and the whole drop+create+cache+insert+index sequence sits in ONE
  transaction so a mid-rebuild failure leaves the PREVIOUS index rather than an empty-but-valid one
  a concurrent reader would answer from with silent zero hits.
- **`check.run_checks`** — the substrate lint. Reach for it (or `make index-check`) before
  debugging a retrieval miss by hand: the multi-word entity-boost defect it was built for sat
  latent for months because the only way to see a substrate defect was eyeballing a search result.

## Avoid / anti-patterns

- **Never write a second `pages_index` row-shape.** `corpus.page_row` is the one parser;
  `store._PAGE_COLUMNS` / `_page_params` / `_INSERT_SQL` are the one column list, params builder and
  statement behind both writers. This module's own docstring records that the column list, the
  VALUES clause and the params dict were each written out twice while a docstring claimed
  otherwise — a column added to one copy and not the other would have diverged silently.
- **Never let an UPDATE clobber `inlinks`.** `_UPSERT_SET` excludes it (and `path`, the conflict
  key) on purpose: a single changed file cannot resolve the whole-corpus INBOUND graph, and writing
  its honest-but-uninformed `0` over an existing row would demote every incrementally-edited page
  until the next nightly rebuild. `links` is the OPPOSITE case and stays IN the SET list — a file
  CAN compute its own outbound targets, so its incoming value is the freshest fact available.
- **Never re-infer the entity boost from the query.** It fires on `entity_hint` — the id the
  SERVICE resolved from the registry and passed down — matched by membership. Token inference was
  structurally dead for every multi-word entity (`northwind-group` can never equal one token of
  "Northwind Group") and had silently narrowed the factor to single-word ids for as long as it
  existed. Resolution belongs to the caller that owns the registry; ranking applies what it is TOLD.
- **Never treat a bare `entity` string as a list by iterating it.** A Python `str` iterates over its
  CHARACTERS. `contract_factors` wraps a scalar in a one-element list before the loop, so a legacy
  caller or upstream bug handing it a bare string cannot fire per-character matches.
- **Never add a raw ACL filter to `FILTER_COLUMNS`.** `acl` is stored and deliberately not
  filterable here — the enforcement point is `server.acl.visible` above this layer. A raw filter
  would let a caller fake access control with none of its guarantees.
- **Never read a green retrieval-golden run as proof the SERVED path works.** `evals/run_retrieval.py`
  measures the ranking SUBSTRATE and deliberately passes no `entity_hint`/`fts_expansion`, so
  neither the entity boost nor the alias expansion ever fires there. The served flow
  (scoped-first, then unscoped fallback) lives in `BrainService._search`; its evidence is
  `tests/server/test_entity_first_search_pg.py` and the QA golden (`evals/run_qa.py`), not this run.
  *(An older blindness — that the golden never passed `filters=` at all — is repaired:
  `make_arm_rankings` forwards each question's declared filters, and 10 of the 16 questions in
  `retrieval_golden.json` declare `filters.entity`. Sabotage-verified: inverting the membership
  clause moves the numbers, which it could not before.)*
- **Never re-derive the fake/real embedder dispatch,** and never import the fake at module level.
  `build_embedder`/`embedder_for_model` are the one dispatch and the fake import stays deferred —
  pinned by `test_production_never_imports_the_fake_embedder_at_module_level`, and mirroring
  `kernel.llm.build_processor`'s identical pattern.
- **Never suggest `--embedder fake` as a workaround for a missing key.** `OpenAIEmbedder`'s refusal
  message says so explicitly and a test pins the wording: a query embedded by the fake against an
  index built with the real one lands in a DIFFERENT vector space, so search returns noise and does
  not fail — the one failure mode worse than an error.
- **Never reintroduce rank-time chain reconstruction.** `rank()` trusts each candidate's own
  `superseded_by` column because `corpus.load_pages` propagates it at build time. The old
  `superseded_bases` cross-reference silently failed whenever a chain's primary fell outside the
  candidate pool; `test_rank_does_not_reconstruct_supersession_from_a_sibling_in_the_candidate_set`
  is the red side of that pair, and exists so the compensation cannot ossify. Extend the propagation
  in `corpus.py` instead.
- **Never key chain identity on the base id alone.** Both the build-time propagation and the
  rank-time collapse carry the page's DIRECTORY beside the base. Two
  ID-LESS pages sharing a file stem in different folders fall back to the same `page_id`, and a bare
  base key would exchange supersession between unrelated documents or merge them into one top-k
  slot. A real chain's parts sit BESIDE their primary, so the directory dimension never splits one.
- **Never add an `IF NOT EXISTS` to the `pages_index` indexes.** Plain `CREATE INDEX` is correct
  here and nowhere else in this codebase's DDL: the table was just dropped and recreated, so the
  index provably cannot exist. Every other `CREATE INDEX` in the repo guards a SURVIVING table.
- **Never open a non-autocommit connection for reading.** `store.connect` is autocommit on purpose:
  a reader sitting idle-in-transaction holds an `AccessShareLock` that a concurrent rebuild's
  `DROP TABLE` would block behind forever. Writers get atomicity from explicit
  `conn.transaction()` blocks.
- **Never drop the schema instead of the table.** `init_schema` targets `pages_index` BY NAME
  because this database also holds the DURABLE half of the system (`capture_queue`, `audit_log`,
  `job_runs`, `ingest_errors`) — material that exists nowhere else until the librarian files it.

## Data & contracts

- **`corpus.PageRow`** — one page as the index stores it; field names mirror `pages_index` columns.
  Two fields are DUAL-STAGE: `page_row` alone can only set `inlinks = 0` and `links` to raw STEMS;
  `load_pages` (whole-corpus) and the webhook (one query) overwrite both with real values before
  storage. `tags` / `mentions` / `entity_meta` are **tsv-only** — they feed `_TSV_SQL` and are never
  stored columns. `embed_text` is `title\nbody` — contextual retrieval, where the page IS the chunk.
- **`pages_index`** — `path` is the primary key. `entity text[] NOT NULL DEFAULT '{}'`;
  `acl text[]` nullable (NULL = open, `{}` = nobody); `links text[]` GIN-indexed; `generated_at`
  (views only); `tsv tsvector`; `embedding halfvec(dim)`.
  **`halfvec`, not `vector`, and it is a hard ceiling not a preference**: pgvector refuses
  an HNSW index above 2000 dimensions and production runs 3072. Changing the COLUMN rather than
  casting at the two call sites is deliberate — a cast that disagrees with the index fails SILENTLY
  (the planner seq-scans); a wrong column type fails loudly.
- **The two retrieval indexes** (`store.create_search_indexes`) — `pages_index_tsv_gin` serves
  `tsv @@ tsq`, `pages_index_embedding_hnsw` serves `embedding <=> …` with `halfvec_cosine_ops`,
  which must match BOTH the column type and the operator `_VEC_SQL` uses or the index is decoration.
  Built AFTER the bulk load, not in `init_schema`: an HNSW index maintains a navigable graph per row
  inserted, so building it first makes 50–100k rows each pay graph maintenance, and building it last
  is one bulk construction. `pages_index_links_gin` is the exception — it goes in `init_schema`
  beside the DDL because backlinks need containment, not because of load order.
- **`search.FILTER_COLUMNS`** = `zone`, `type`, `status`, `entity`, `owner`, `tier`, `as_of` — seven.
  `period` and `extraction_quality` were DELETED for want of a producer: `period` duplicated `as_of`
  value-for-value on every page that carried it, and `extraction_quality` was an inherited OCR flag
  no template offers and no code stamps. Every column is a scalar equality EXCEPT `entity`, which is
  `%s = ANY(entity)` membership. `filters={"entity": ""}` matches NOTHING — a genuine contract
  change from the older nullable scalar, documented in `_filter_clause` rather than patched
  around, because the direction is safe (an old accidental capability → matches nothing).
- **`rank.contract_factors`** — returns `(factor, label)` pairs applied as DIVISORS of the
  higher-is-better RRF score, so `> 1` demotes and `< 1` boosts:

  | Label | Constant | Fires when |
  |---|---|---|
  | `superseded` | 4.0 | `superseded_by` is truthy — on the row's OWN column, chain and all |
  | `status-evergreen` | 0.8 | `status == "evergreen"`. RE-BOUND from `canonical`, which died with the canon lane; rebinding rather than deleting kept the maturity axis attached to a real producer |
  | `entity:<id>` | 0.5 | `entity_hint` matches an element of the page's `entity` list (case-insensitive). Once per page, never once per element; no hint ⇒ no factor |
  | `period-match` | 0.6 | a period parsed from the query prefix-matches the page's `as_of` (`as_of` alone — there is no `period` column) |
  | `fresh:<as_of>` | 0.7 | the query carries a recency word (ES + EN sets) AND the page has `as_of` AND is not superseded |
  | `stale:<value>` | 1.3 | `today - _period_end(as_of or updated) > 365 days`. Only when `today` is injected — never the wall clock |

  `_period_end` treats a coarse value as its LATEST plausible day (a page dated `2026` is fresh
  through 2026), so coarse-but-recent pages are never punished, and an invalid date that matches the
  shape regex but raises on construction yields `None` rather than crashing the run.
- **`inlinks` is DELIBERATELY not a ranking factor.** It was measured twice (`0.9^min(n,3)` and a
  stronger `0.7`) and both weightings moved the golden set's `final` arm DOWN — the highly-inlinked
  entity page outranked the VIEW on a broad "what do we know about X" question. Link-degree
  rewards hubs, and hubs are exactly what a broad question must not bury the synthesis under. The
  column stays DATA (the gardener's orphan check, webhook reconciliation); waking it needs a
  measured miss it would fix.
- **Chain collapse** — after sorting and BEFORE the `[:k]` cut, `rank()` keeps only the
  best-scoring member of each `(directory, chain_base(page_id))` group, so `k` counts DOCUMENTS, not
  rows. The defect it fixes was measured: on `claude: formato formacion` the final top-5 was a
  meeting page plus ALL FOUR of its transcript parts, and the decision page that answered sat at vec
  rank 2. The other members stay reachable by path — collapse is a top-k presentation rule, not
  deletion, exactly as `include_superseded=False` is an operational filter and not deletion.
- **`search.search_arms` returns** `{"fts": [...], "vec": [...], "hits": [...], "page_ids": {...}}`.
  `fts_expansion` is appended to the LEXICAL arm's query only — the tsquery is an OR of lexemes, so
  extra registry names can only ever ADD candidates; the vector arm embeds the raw query untouched,
  because expansion is a lexical repair, not a semantic one.
- **`_FTS_SQL` single-quotes every lexeme** (inner quotes doubled) before `to_tsquery`. Normalized
  lexemes can still contain tsquery syntax (`:` in URLs, `/`), which would otherwise let a hostile —
  or merely URL-bearing — query crash the arm. And it ORs rather than ANDs, because
  `websearch_to_tsquery` ANDs every term and a whole natural-language question would then match nothing by
  construction.
- **`index_meta`** — singleton row: `model`, `dim`, `fts_config`, `built_at`. `read_meta` returns
  `None` (treated as "no index built", which surfaces the actionable `--rebuild` hint) both when the
  table is absent and when a legacy row predates the `built_at` column, rather than raising.
- **`check.run_checks` findings** — errors first, then by check name, then detail:

  | Severity | Check | What it catches |
  |---|---|---|
  | error | `duplicate-page-id` | two pages carrying one `page_id` — golden expectations and chain grouping both key on it. Two ID-less pages whose file stems collide are the common case |
  | error | `orphan-continuation-part` | a `-p<n>`/`#p<n>` id with no bare primary in the same directory — the chain machinery silently treats it as its own document |
  | error | `missing-embedding` / `empty-tsv` | a page invisible to one arm: a silent retrieval hole no golden question finds until it happens to expect that page |
  | warn | `dangling-superseded-by` | the named successor id is not in the index (may be legitimately historical) |
  | warn | `anchored-but-unregistered` | an `entity` value with no registry record — it resolves for navigation (ADR 022 D5) but gets no aliases, no entity-first search, no TOLD boost |

  Any ERROR exits 1 (`cli.index_main`). A missing registry SKIPS the coverage warning rather than
  inventing findings; a MALFORMED one raises.
- **`golden.ARMS`** = `("fts", "vec", "rrf", "final")`; `final` is the arm the R@5 ≥ 0.80 bar reads.
  Scoring is CHAIN-EQUIVALENT (`chain_base` on both sides): surfacing `X-p3` IS surfacing
  document `X`, and which member the collapse happens to keep must not decide a hit. There is ONE
  retrieval set, `evals/retrieval_golden.json` (16 questions, 10 of them entity-filtered), and both
  readers use it: `evals/run_gates.py` / `make gates` for the bar, `make retrieval-golden` for the
  per-arm report.
- **Embedding cache** — `embedding_cache(model, content_hash)` SURVIVES a rebuild; `build.rebuild`
  consults it only if the table already exists, embeds one vector per DISTINCT `content_hash`, and
  `store_embeddings` is `ON CONFLICT DO NOTHING`. `content_hash` is `sha256:` over
  `title\nbody` — so a frontmatter-only edit that touches neither does not re-embed.

## Tests

`tests/index/` — 14 modules, ~2,200 lines, plus an 11-page fixture repo
(`tests/index/fixtures/repo`: 4 `wiki/`, 6 `sources/`, 1 `views/`, plus three excluded-zone markers
that must never surface) which `scripts/e2e.sh` also drives.

The pure suites (`test_corpus`, `test_rank`, `test_rank_edges`, `test_golden`, `test_embedder`,
`test_search_unit`, `test_cli`, `test_architecture`) run keyless and DB-less. The Postgres-backed
ones run against the `stigmergy_test` database `make db-up` creates and skip cleanly without it —
except when `$STIGMERGY_TEST_DSN` is set (CI mode), where an unreachable database FAILS instead of
skipping. They reach Postgres only through `tests/testdb.py`, which refuses any database but
`stigmergy_test`, with no override flag.

| Suite | Covers |
|---|---|
| `test_corpus.py` | `entity_list`'s eleven fail-closed cases, the three-zone walk and its counts, ACL parsing (absent vs empty vs scalar vs malformed), `inlinks`, code-fenced non-links, `resolve_links`/`by_stem_index` (including the `views/` exclusion and ambiguous stems), split-chain propagation — the live `-p<n>` convention, the cross-directory refusal, the same-stem twins, and the conflicting-donor warning |
| `test_rank.py` | every contract factor and its label, the TOLD entity boost (hint required, multi-word ids, multi-valued lists, once-per-page, the bare-string guard), the chain collapse (flooding, live vs historical marker, an independent part-shaped id, directory twins, best-member-wins), and the pinned proof that `rank()` no longer reconstructs supersession from a sibling |
| `test_rank_edges.py` | `_period_end` across day/month/quarter/year and its invalid values, staleness at the horizon, a forged `superseded_by` in an authored page, and pool-edge tie-breaking that is insertion-order free |
| `test_search_unit.py` | `_filter_clause`, pure: `entity` membership vs scalar equality, filters combined, unknown column raises |
| `test_check_pg.py` | the substrate lint — build a CLEAN corpus, assert zero findings, then inject each corruption class **by SQL**, the way real corruption arrives (partial writes, hand surgery, a half-applied webhook) rather than through the builder, which is correct. Each class asserts its finding, its severity, and the errors-first order |
| `test_pg_integration.py` | full rebuild + search over real Postgres: the zone population, the filter/ACL columns and the `links` GIN landing in the schema, **both retrieval indexes existing and matching the operators the arms actually use**, the schema building at the PRODUCTION 3072 dimension, plural-entity cases, a URL-bearing query not crashing the lexical arm, cache reuse + idempotency, and the CLI end-to-end |
| `test_pg_search_edges.py` | FTS-emptied-by-filter still served by the vector arm, `--current-only` combined with filters, and the `entity` filter's real coverage |
| `test_incremental_pg.py` | the webhook's own primitives — `upsert_pages`/`delete_pages`/`current_content_hashes`, the `inlinks` preservation on UPDATE and its honest `0` on a fresh INSERT, `pages_with_page_id_prefix`'s LIKE-escaping, `set_superseded_by` stamping AND clearing. Isolated from `test_pg_integration`'s module-scoped fixture because it mutates rows in place |
| `test_r1_split_chain_pg.py` | a continuation part demoted via its own propagated `superseded_by` even when its primary is OUTSIDE the candidate set — the case the deleted rank-time reconstruction could not see |
| `test_entity_meta_tsv_pg.py` | an entity page's `role`/`aliases` folding into `tsv` and becoming lexically findable, a non-entity page's identical frontmatter NOT folding, and that no new ranking factor was introduced |
| `test_golden.py` | the pure scorer, the shipped set's shape (≥15 questions, ≥10 entity-filtered, only legal filter columns), that `evaluate` passes each question's filters through, and chain equivalence in both directions (a chain member credits its base; a different document never does) |
| `test_embedder.py` | fake determinism/dim/normalization, the keyless refusal, `embedder_for_model`'s mapping, the HTTP path over a stub transport, and that the missing-key message never suggests the fake as a substitute |
| `test_cli.py` | `_parse_filters`, the mutually-exclusive mode group, and hit rendering with and without factors |
| `test_architecture.py` | the index-never-imports-a-writer rule (parametrized per module), the pipeline-is-gone assertion, and the deferred-fake-embedder import rule |

Beyond this directory: `tests/test_architecture.py` pins the layering edges that name this package —
`test_server_imports_the_index_as_a_library` (the positive assertion), the per-package connection-seam
rules for `capture`/`entities`/`gardener`/`digest`, and the four `ACL_REACHABILITY_EXCEPTIONS`
entries. `tests/capture/test_queue_pg.py::test_capture_queue_survives_stigmergy_index_rebuild` pins
that a rebuild leaves the durable tables standing. `tests/server/test_webhook.py` pins that the
webhook's link resolution and `load_pages`' agree on one corpus.

## Common tasks

| Task | Touch |
|---|---|
| Add a new frontmatter filter column | `search.FILTER_COLUMNS`, `corpus.PageRow` + `page_row`, `store._PAGE_COLUMNS`/`_page_params`/`_PAGES_DDL`, and `search.fetch_pages`'s `cols` — keep all four in lockstep |
| Add a new contract ranking factor | `rank.contract_factors` (a new `(constant, label)` entry beside the documented ones) — and measure it against the golden set before keeping it, the way `inlinks` was measured and rejected |
| Change what a rebuild embeds or caches | `build.rebuild`; note that `PageRow.embed_text` changes what gets EMBEDDED and `content_hash` changes what gets CACHED — they are computed from the same two fields but are not the same decision |
| Change the webhook's incremental row shape | `store._PAGE_COLUMNS`/`_page_params`/`_UPSERT_SET` — never a second column list in `server.webhook` |
| Change outbound-link resolution | `corpus.resolve_links`/`by_stem_index` — the ONE algorithm; both callers share it |
| Change `entity:`'s normalization | `corpus.entity_list` — mirror it in `docs/reference/page-contract.md` and check `contract_factors`' scalar-string guard still holds |
| Change what counts as a split-chain sibling | `corpus.chain_part_pattern` + `rank._PART_MARKER_RE`/`chain_base`, and re-check all five `chain_base` callers (rank collapse, build-time propagation, `is_chain_primary`/the webhook's incremental propagation, `check`'s orphan rule, golden scoring) |
| Add a substrate check | a `_finding(...)` block in `check.run_checks` + a severity in `FINDING_SEVERITIES`' order, and an injected-corruption case in `test_check_pg.py` |
| Grow the golden set | `evals/retrieval_golden.json` — every observed retrieval miss becomes a candidate the same day |
| Add a ranking signal the SERVICE must resolve | a new TOLD parameter on `search_arms` → `rank`, resolved in `server.service`, never inferred here — the `entity_hint` precedent |

## Notes

- **`stigmergy.index` is a derived, disposable layer.** `pages_index` is never migrated — wipe and
  rebuild is the upgrade path. `embedding_cache` and `index_meta` are the two SURVIVING
  tables, and the durable half of this same database (`capture_queue`, `audit_log`, `job_runs`,
  `ingest_errors` — `capture.schema`) is untouched by `init_schema`.
- **`check.py` is one of THREE independent readers of `ops/entity-registry.json`.** The other two
  are `kernel.registry.load_registry` (the shared reader `entities`, `gardener`, `views` and
  `librarian.base_inputs` all go through) and `server.entity_aliases` (which parses the file itself
  because `stigmergy.server` may not import `stigmergy.entities`, and whose normalization is
  deliberately looser than the kernel's — see its module docstring). The divergence is real and has
  a reason on each side: this package's doctrine is that it carries its own parsers so no writer's
  refactor changes what gets indexed, and the readers disagree behaviourally — `kernel.registry`
  treats a missing file as an EMPTY registry (which here would flag every anchored entity as
  unregistered), while `check.registry_ids` returns `None` and skips the coverage check entirely.
  A FOURTH reader is worth a deliberate decision rather than a fourth parser.
- **`superseded_by` has no automated producer.** Nothing in `src/` writes it: a human puts it on
  the PRIMARY page of a chain, prompted by the gardener's sweep. That is the whole reason
  continuation parts carry an EMPTY field in their own frontmatter, and therefore the reason both
  propagations exist — `corpus.load_pages` at build time and `server.webhook` incrementally — so
  that every row reaching storage carries the truth and `rank()` can trust each candidate's own
  column. Both are marker-gated and directional (donor = `is_chain_primary`, receiver =
  `chain_part_pattern`); a part with a value nobody donated is left exactly as the repo wrote it.
  **Three comments still credit a `versions.py` for the stamp** — `corpus.py`, `rank.py` and
  `server/webhook.py`. No such module exists anywhere in `src/`. The mechanism those comments
  describe is right and the producer they name is dead; read the sentence above instead, and do not
  go looking for the file.
