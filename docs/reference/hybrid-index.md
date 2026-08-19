# The hybrid derived index — `stigmergy.index`

What this package is and how to drive it. The **why** lives in
[ADR 012](../decisions/012-hybrid-index.md); this is the what and the where.
Code map: [`src/stigmergy/index/index.md`](../../src/stigmergy/index/index.md).

A derived, disposable search layer over a checkout of the knowledge repo: Postgres +
pgvector, a lexical arm and a semantic arm fused with RRF, then the explainable contract
ranking. Never a source of truth — wipe it and rebuild it from git whenever convenient.

## Module map

| Module | Does |
|---|---|
| `corpus.py` | repo checkout → `PageRow`s: zone walk over `ZONES = ("wiki", "sources", "views")` — an **include list and nothing else**, which is why `ops/` (the registry, identities, templates) never reaches retrieval. (There is deliberately no `EXCLUDED_ZONES` constant beside it: an include-list needs no exclude-list.) Also: tolerant frontmatter parsing, `entity_list`'s fail-CLOSED normalization of both `entity:` dialects, the wikilink graph → `inlinks` AND resolved outbound `links` (`resolve_links`/`by_stem_index` — the one algorithm the webhook shares), the build-time `superseded_by` propagation onto split-chain siblings, `content_hash` of the embedded text; `page_row` is the public single-file parser both `load_pages` and the incremental webhook call |
| `backends/embedder.py` | the OpenAI-dialect embedder — `text-embedding-3-large` on OpenAI by default; any OpenAI-compatible `/embeddings` host via `EMBED_BASE_URL` + `EMBED_API_KEY`, build-time default model via `EMBED_MODEL` — plus `build_embedder`, the one fake/real dispatch (deferred fake import) |
| `backends/fake_embedder.py` | deterministic hashed bag-of-words double (tests/CI; keyless) |
| `store.py` | all SQL DDL and writes: `pages_index` (dropped/recreated per rebuild; carries `links` + its GIN index and `generated_at`), `embedding_cache` (survives; keyed by model + content_hash), `index_meta`, `ops_file_snapshot` (survives; the relpath-keyed cache of the knowledge repo's `ops/` control files, read/written/cleared through `read_ops_file`/`write_ops_file`/`clear_ops_file` — see "The ops files ride along" below), `webhook_deliveries` (survives; the applied-delivery ids behind the webhook's replay protection); `upsert_pages`/`delete_pages`/`current_content_hashes` are the webhook's incremental primitives, beside `insert_pages`, never a second row shape; `existing_paths` is the webhook's one-query snapshot for outbound-link resolution; `pages_with_page_id_prefix`/`set_superseded_by` are the webhook's split-chain propagation primitives. `create_search_indexes` runs **after** the bulk load, never before |
| `build.py` | `rebuild(conn, repo_dir, embedder, fts_config="english")` — the full rebuild, cache-aware: init schema → insert rows → build the search indexes → reconcile the entity-registry snapshot from the checkout |
| `rank.py` | pure ranking: RRF fusion (`RRF_K` 60, `CANDIDATE_POOL` 40 per arm, `TOP_K` 5) + the six contract factors (ADR 012), snippets; `today` injected for staleness so a ranking is reproducible instead of wall-clock dependent. RRF is higher-is-better, so the factor constants DIVIDE the score — penalties > 1, boosts < 1. **Two things live here that a reader might expect elsewhere**: the entity boost fires on an `entity_hint` the service resolved and passed DOWN (never re-inferred from query tokens), and `rank()` collapses a split document's parts to ONE top-k slot before the `[:k]` cut |
| `search.py` | the shared base query: both SQL arms under the same frontmatter filters, fusion, `search()` / `search_arms()`; `FILTER_COLUMNS` is the allowlist. It threads `entity_hint` (to the ranker) and `fts_expansion` (registry aliases OR-ed into the LEXICAL arm only — the vec arm embeds the raw query untouched) |
| `check.py` | the retrieval-substrate lint behind `stigmergy-index --check` — deterministic SQL over `pages_index` asking the questions an operator would not think to ask until something already looked wrong. See "Linting the index" below |
| `golden.py` | golden-set loading + per-arm Recall@k scoring (pure); it carries each question's `filters` through to the runner but never interprets them (no `search` import, no DB) |
| `cli.py` | `stigmergy-index` (`--rebuild` \| `--check`, mutually exclusive), `stigmergy-search` |
| `errors.py` | the domain exceptions (`StigmergyIndexError`, `EmptyIndexError`, `EmptyCorpusError`) — the library never raises `SystemExit`; the CLIs translate them to exit codes, and the server maps the same seams to responses |

`stigmergy.kernel` is a library every package — including this one — may depend on freely.
`tests/index/test_architecture.py` pins the rule that matters — **the index reaches for no
writer** (`stigmergy.librarian`, `stigmergy.entities`, `stigmergy.views`, `stigmergy.capture`), checked per
module and at ANY nesting depth, so the derived cache can never depend on the thing it is derived
from — plus the deferred-fake-embedder import rule.

## Schema (`pages_index`)

One row per page: `path` (PK), `page_id` (frontmatter `id`, or file stem), `zone`, `title`,
`body`, the frontmatter filter columns (`type`, `status`, `entity`, `owner`, `tier`, `as_of`,
`updated`, `superseded_by`, `supersedes`), `acl` (`text[]`; NULL = open, `{}` = nobody; malformed
shapes fail CLOSED — **stored here, enforced by the server** via `stigmergy.server.acl.visible`),
`inlinks`, `links`, `generated_at`, `content_hash`, `tsv` (FTS vector), `embedding`.
Never migrated: rebuild is the upgrade path.

**Two names a reader may still meet in old material are NOT columns** — say so plainly, because a
stale filter name is a `ValueError` at the tool boundary, not a silent miss:

- `period` was an exact duplicate of `as_of` (every page carrying one carried both, with the same
  value) and had **no producer** — no template offered it and no code stamped it. One dated field,
  not two. The `period-match` ranking factor survives and reads `as_of` alone.
- `extraction_quality` went with the ingest pipeline that computed it.

`corpus.PageRow` also carries `tags`, `mentions` and `entity_meta` (a `type: entity`
page's own `role`/`aliases`). None of the three is a column — they feed the `tsv` vector only, so
they are searchable lexically and not filterable.

**`embedding` is `halfvec`, not `vector` — a hard ceiling, not a preference.** pgvector
refuses to build an HNSW index above 2000 dimensions and the production embedder
(`text-embedding-3-large`) is 3072; `halfvec` raises the ceiling to 4000 by storing 16-bit floats.
The vectors are cosine-normalized and HNSW is approximate anyway, so the precision lost sits well
below the noise the approximation already introduces. Changing the COLUMN rather than casting at
the two call sites is deliberate: a cast in the query that disagrees with the cast in the index
fails **silently** (the planner just seq-scans). A wrong type here fails loudly.

**`links` is `text[] NOT NULL DEFAULT '{}'` — the entity-navigation graph.** Resolved
OUTBOUND wikilink targets — repo-relative paths, never stems. `corpus.resolve_links`/
`by_stem_index` are the ONE resolution algorithm both the full rebuild (`corpus.load_pages`, in
memory over the whole corpus) and the incremental webhook (`server.webhook`, one query
against `pages_index`'s own existing paths via `store.existing_paths`) share — a stem resolving
to several pages stores every match (the same semantics `inlinks` already counts), a stem
resolving to nothing stores nothing. A GIN index (`pages_index_links_gin`) turns the INBOUND view
into a containment lookup (`links @> ARRAY[path]`), never a scan — `read_page`'s `backlinks` and
`describe_entity`'s timeline both ride it. See [navigation.md](./navigation.md) for the served
shape.

**`generated_at` is `text NOT NULL DEFAULT ''`.** A view's own `generated_at`
frontmatter (ISO-8601), the one view-only field `describe_entity`'s view layer needs that
no other column carries (views set neither `updated` nor `as_of`) — empty for every other
page.

**`entity` is `text[] NOT NULL DEFAULT '{}'` — a page's aboutness, plural (see
[page-contract.md](./page-contract.md) for the field's own contract).** `corpus.entity_list`
normalizes both dialects a page may carry — a bare string (`entity: initech`, an older writer's
dialect; nothing mints one now, but pages already carrying it stay valid) and a list — into this
column, stripping elements, rejecting bools, and folding
`""` to `[]` (never `[""]`). `{}` means either "no `entity` key at all" (a pre-contract page)
or "checked, explicit company-wide scope" in `wiki/**` — the index does not need to tell
them apart, only the page contract does.

## Queryable filters vs stored columns

`--filter` (and the library's `filters=`) accepts exactly the **seven** names in
`search.FILTER_COLUMNS`:

```
zone · type · status · entity · owner · tier · as_of
```

`period` is **not** among them — the column does not exist; anything outside the seven is
rejected with a clean error naming the caller's own key and the allowed set. `entity` is matched
by **membership** (`%s = ANY(entity)`) rather than equality — a page anchored to several
entities is found by any one of them; every other filter column stays a scalar equality, and the
public contract is otherwise unchanged (a caller still passes one value per column). `acl` is
stored but NOT filterable: the enforcement point is the server's `acl.visible`, not the index
query layer — offering a raw acl filter here would let callers fake access control with none of
its guarantees. `updated` is a coarse freshness string consumed by the staleness factor, not a
meaningful equality filter. `zone` is in the list because it is a filter retrieval actually needs.

> **Two tool docstrings restate that same list and must move with it** — the MCP `search_brain`
> tool (`server/mcp_server.py`) and `ask`'s own agent-facing `search` tool
> (`answer/synthesize.py`). Both advertised `period` for a while after the column stopped
> existing, and a filter name copied out of a tool description is an unknown-filter `ValueError`
> at the boundary rather than a silent no-op — the right failure, but not one that reads as a
> stale docstring to the person (or the model) that copied it. Neither may name anything outside
> `search.FILTER_COLUMNS`.

## Schema (`index_meta`)

A singleton row recording the last successful rebuild: `model`, `dim`, `fts_config`, and
`built_at` (timestamptz; added additively — `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`,
so a database written before the column existed upgrades in place rather than needing a wipe). The
server reads it via `store.read_meta()` and returns `built_at` on every `search_brain` response, so
a stale index is self-diagnosing; a row missing the column is treated as "no index built" rather
than raising, so startup surfaces the `--rebuild` hint instead of a raw `UndefinedColumn` error.

## Sharing the database with the durable half

This Postgres holds more than a cache. `capture_queue`, `audit_log`, `job_runs` and
`ingest_errors` (`stigmergy.capture.schema.DURABLE_TABLES`) are **durable**: a queued capture exists
nowhere else until the librarian files it, so it cannot be rebuilt from git the way `pages_index`
can. `review_decisions` (the append-only verdict record, created by
`stigmergy.server.review.ensure_review_schema`) shares the database on the same terms. That is why
`store.init_schema` drops `pages_index` **by name** and always must — a "drop the schema and
rebuild" shortcut would take the queue with the cache. `stigmergy-index --rebuild` leaves all four
durable tables standing, asserted by
`tests/capture/test_queue_pg.py::test_capture_queue_survives_stigmergy_index_rebuild`.
See [capture.md](./capture.md).

## Run it

```sh
make db-up                                   # postgres+pgvector + minio (docker-compose.yml; loopback-only)
.venv/bin/stigmergy-index --rebuild --repo ../stigmergy-brain            # real embedder: needs OPENAI_API_KEY (or EMBED_BASE_URL + EMBED_API_KEY)
.venv/bin/stigmergy-index --rebuild --repo ../stigmergy-brain --embedder fake   # keyless (tests/CI double)

.venv/bin/stigmergy-search "How much was the deposit on the Kestrel Lodge booking?"
.venv/bin/stigmergy-search "globex quarterly revenue" --filter entity=globex -k 10
.venv/bin/stigmergy-search "revenue" --current-only --json      # drop superseded instead of demoting
```

The real embedder speaks OpenAI's `/embeddings` dialect to whichever host `EMBED_BASE_URL` names
(OpenAI by default; `EMBED_API_KEY` is that host's credential). `OPENAI_API_KEY` remains the
fallback for the DEFAULT host only — it is never sent to an `EMBED_BASE_URL` host, whose missing
key refuses loudly instead of borrowing a credential the host did not issue. `EMBED_MODEL` is
the BUILD-time default model only: the model is recorded in `index_meta` and queries always
embed with the recorded one, so changing it takes effect at the next `--rebuild`, never
mid-index — and pointing `EMBED_BASE_URL` at a host that serves a DIFFERENT model under the same
name deserves a `--rebuild` for the same vector-space reason. `EMBED_DIMENSIONS` is MRL
truncation, sent as the dialect's `dimensions` field only when set: it is how a 4096-native
model (Qwen3-Embedding-8B) fits under the schema's 4000-dimension HNSW ceiling, it must agree
between build and query for a standing index, and a mismatch fails loudly as a pgvector
dimension error rather than silently in ranking.

Every hit renders its score, arms (`fts`/`vec`), the ranking factors applied and a snippet. There
are **six** factor labels, and that is the whole set `rank.contract_factors` can emit:

| Label | Effect | Fires when |
|---|---|---|
| `superseded` | demote (×4.0) | the row's own `superseded_by` is set — including a split document's continuation parts, which the build propagates |
| `status-evergreen` | boost | `status: evergreen` outranks a seed on equal relevance |
| `entity:<id>` | boost | the resolved `entity_hint` is a **member** of the page's `entity` list |
| `period-match` | boost | a period in the query matches the page's `as_of` (prefix-compatible either way) |
| `fresh:<as_of>` | boost | the query used a recency word and the page is dated and not superseded |
| `stale:<date>` | demote | the page's freshest date is more than `STALE_AFTER_DAYS` (365) behind the injected `today` |

`manual-review` and `status-canonical` were factors once and are **gone**. A hit will never carry
either.

**The entity boost is TOLD, never inferred.** It fires on the id `BrainService._search`
resolved from the registry and handed down as `entity_hint` — matched by list membership, never
re-derived from query tokens here. The old token-inference form was **structurally dead for every
multi-word entity**: an id like `northwind-group` can never equal one token of "Northwind
Capital", so the factor had silently narrowed itself to single-word ids. No hint means no
entity factor: resolution belongs to the service, which owns the registry and the identity;
ranking only applies what it is told. It was found by eyeballing a search result, which is the
class of latency `stigmergy-index --check` now exists to end.

**One document, one top-k slot.** `rank()` collapses a split document's parts before it cuts
to `k`, keyed on `(directory, chain_base)` — the directory is in the key because two id-less pages
sharing a file stem in different folders fall back to the same stem-derived `page_id`, and a bare
chain key would merge two unrelated documents. The measured miss: a broad query let a single
transcript flood four of five slots and bury the decision page that answered it. The other parts
stay reachable by path — collapse is a presentation rule, not deletion.

**`inlinks` is deliberately NOT a factor.** Measured once under the standing "expect nothing, else
DELETE" rule: a candidate boost took the retrieval golden's final arm from 1.000 to
0.923, twice — the highly-inlinked entity page outranked the VIEW on a broad question. Link-degree
rewards hubs, and hubs are exactly what a broad question must not bury the synthesis under. The
column stays data (the gardener, webhook reconciliation); waking it needs a measured miss it fixes.

The DSN comes from `STIGMERGY_INDEX_DSN` (default
`postgresql://stigmergy:stigmergy@localhost:54321/stigmergy`); queries embed with whatever model
the index was built with (`index_meta`).

## Linting the index (`stigmergy-index --check`)

The pages are linted by the knowledge repo's own linter. **The index never was** — which is why
the multi-word entity-boost defect above sat latent until someone happened to eyeball a
result. `--check` (`make index-check`) asks the index the questions an operator would not think to
ask until something already looked wrong. Each one is deterministic SQL over `pages_index`, plus
one optional file read of the registry.

**ERROR — the index is lying to an arm or to an identity layer; exit 1:**

- **duplicate `page_id`** — two layers key on it (golden expectations, chain grouping) and a
  duplicate makes both ambiguous. The fix is a frontmatter `id:` on one page; the stem-fallback
  twins this catches are exactly the `quarterly-update.md`-in-two-folders class.
- **orphan continuation part** — a `-p<n>`/`#p<n>` page_id whose primary (the bare base id, same
  directory) is not in the index: the chain machinery treats it as its own document, silently.
- **missing embedding / empty tsv** — a page invisible to one arm is a retrieval hole no golden
  question will find until it happens to expect that page.

**WARN — worth an operator's eyes, never an exit code:** a dangling `superseded_by` (the named
successor is not in the index — may be legitimately historical), and an anchored-but-unregistered
entity (it resolves for navigation under ADR 022 D5, but gets no aliases, no entity-first search
and no TOLD boost).

`--check` reads its id set through `kernel.registry`, never through `stigmergy.server`: the index
sits below the server in the import graph. WHICH copy it reads is `check.served_registry` — the
index's snapshot where the database has one, the `--entity-registry` file where it does not, the
server's own order. A lint reading the other copy reports on a world nobody is living in: on a
deployed console the file is the one baked at deploy time, so every entity minted since the rollout
would be warned about as unregistered while the server serves full records for it.

## The ops files ride along

The index also carries `ops_file_snapshot` — one row per cached `ops/` control file, the knowledge
repo's `ops/entity-registry.json`, `ops/identities.json` and `ops/slack-channels.json` as TEXT
(`store.OPS_FILE_RELPATHS` is the one spelling). Nothing in retrieval reads them: they are here
because the SERVER reads these files on the hot path exactly as it reads pages, and the deployed
`app` and `slack` process groups hold no checkout at all — they were served copies baked into the
image at deploy time, so an entity minted after a rollout had no name until the next deploy (issue
#74), and — the sharper half — an identity revoked after it kept resolving, and a channel scoped
after it stayed unscoped, until the next deploy (issue #79).

They have the same two writers pages have, and no others:

- **`stigmergy.server.webhook`**, incrementally — a push whose changed paths include one of the
  three fetches that file **at the branch ref, never at the pushed sha**, and writes it in the
  SAME transaction as the pages. The ref choice is the replay defense: a replayed or delayed
  delivery re-fetches what the branch says NOW, so no historical roster is installable through
  the endpoint. The lookup is over the RAW pushed paths: `ops/` is in no `ZONES` entry, so every
  page filter is blind to it.
- **`rebuild()`**, nightly — one decision per file. Present in the checkout: written. Absent:
  CLEARED for the registry (a repo before its first mint genuinely has none, and readers fall
  back to their own file), KEPT for the two access files — clearing those would hand every
  deployed reader back to the roster baked at the last deploy, a revocation silently undone by a
  cron; a deployment that genuinely wants "nobody" pushes an explicit `{}`, a committed statement.
  Nothing is silent: the stats carry `ops_files`, a clear warns, a keep logs an error naming the
  file.

Both writers refuse a file above `store.MAX_OPS_FILE_BYTES` rather than installing it — this text
is parsed on every tool call (the registry) or every request/event (the access files), so its size
is a per-request cost, not the one-off cost of the push that wrote it. A refusal leaves the
previous snapshot standing on BOTH roads, logs the size and the cap, and a push's PAGES still land.

The bytes are stored verbatim and parsed only by each file's own reader (`server/entity_aliases`,
`server/identity`, `slack/channels` — `server/ops_files` chooses which copy, once); a snapshot-less
database (an older index, or a local run) falls back to the process's own file, which is the
behaviour that predates the table. An EMPTY snapshot is a real value, and the access readers fail
CLOSED on it: `""` resolves nobody, never everybody.

Beside the cache sits `webhook_deliveries` — the page road's own replay protection, one row per
APPLIED `X-GitHub-Delivery` id, recorded inside the same transaction as the writes so a failed
delivery never records itself and GitHub's manual redelivery still works. Pages are fetched at the
delivery's own sha (their consistency story is the delivery's path list), so without this a
captured delivery could re-install old page bytes and re-perform old deletions until the nightly
rebuild; with it, a repeat is acknowledged with `duplicate delivery` and applied nowhere.

## Golden set + e2e

```sh
make retrieval-golden                        # Recall@5 per arm, fake embedder (plumbing check)
make retrieval-golden EMBEDDER=openai RETRIEVAL_ARGS="--rebuild --repo evals/corpus --report evals/out/retrieval-report.json"
make e2e                                     # build -> golden -> WIPE volumes -> rebuild -> byte-identical report -> substrate lint
```

`make e2e` is the idempotency proof that this layer is a CACHE: the whole retrieval report —
per-arm rankings included, not just the top hits — must come back byte-identical across a volume
wipe. It runs the fake embedder precisely because it is deterministic, so any diff is a real
nondeterminism bug rather than embedding drift.

The golden set is `evals/retrieval_golden.json` — **16** questions over the frozen
reference corpus at `evals/corpus/`, and the set `make gates` reads the R@5 ≥ 0.80 bar on. Arms
reported: `fts`, `vec`, `rrf` and `final` (RRF + contract factors — the arm the bar is read on).
Every observed miss becomes a golden candidate the same day.

**What the golden run does and does not exercise.** It DOES pass `filters=`:
`run_retrieval.make_arm_rankings` forwards each question's declared filters into
`search_arms`, and 10 of the 16 questions declare `filters.entity` — the other 6 are a deliberate
unfiltered control half. That is sabotage-verified rather than asserted: inverting the membership
clause in `search.py` (`= ANY(entity)` → `<> ALL(entity)`) moves this run's numbers, which it could
not do while the run never asked anything to filter.

The real blindness is one layer up, and it is deliberate: the runner passes **no `entity_hint` and
no `fts_expansion`**, so the served entity-first path — registry resolution, the TOLD entity boost,
the lexical-arm alias expansion — is **not what the golden set measures**. The golden measures the
arms under explicit filters. Coverage of the resolution layer lives in
`tests/server/test_entity_first_search_pg.py` and `tests/index/test_pg_search_edges.py`; do not
read a green golden run as proof that entity-first retrieval still works.

## Tests

`tests/index/` — the pure suites (`test_corpus`, `test_rank`, `test_rank_edges`, `test_search_unit`,
`test_golden`, `test_embedder`, `test_cli`) run keyless and DB-less; the postgres-backed suites
(`test_pg_integration`, `test_pg_search_edges`, `test_incremental_pg`, `test_r1_split_chain_pg`,
`test_entity_meta_tsv_pg`, `test_check_pg`) run against the `stigmergy_test` database `make db-up`
creates, and skip cleanly without it — except when `$STIGMERGY_TEST_DSN` is set (CI mode), where an
unreachable database FAILS instead of skipping. They reach Postgres only through `tests/testdb.py`,
which refuses any database but `stigmergy_test`
([operator-runbook.md](./operator-runbook.md#the-two-databases)). `test_architecture.py` pins that
the index imports no writer package, plus the deferred-fake-embedder import rule. Fixture corpus:
`tests/index/fixtures/repo` — **11 pages** across the three zones (4 `wiki/`, 6 `sources/`, 1
`views/`) plus three excluded-zone markers under `ops/`, `meta/` and `datasets/` that must never
appear in a build. `make e2e` runs against that SAME fixture repo — there is no second one — with
its own question set at `tests/index/fixtures/e2e-questions.json`.

## Not built here

- a cross-encoder reranker;
- a BM25/ParadeDB upgrade — worth doing only if golden recall shows native FTS lacking, and it
  has not.
