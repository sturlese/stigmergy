# Changelog

All notable changes to this project are documented here, in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) style, following
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version stays below `1.0.0` the contracts described in
[`docs/reference/`](./docs/reference) may still move between minor releases. What will not move
without a decision record in [`docs/decisions/`](./docs/decisions) is *behaviour*: this project
treats its test suite as the contract.

## [0.8.0] - 2026-08-21

**An entity is born written, and keeps being written** ([ADR 042](./docs/decisions/042-an-entity-is-born-written.md), #131).
Twelve of the first brain's nineteen entity pages said nothing about the entity: the two hand
doors rendered the template with the name filled in. There is no deterministic birth any more — a
steward's registration is a capture, the librarian writes the page from what the steward said and
what the brain already holds, and a filing that establishes something about a known entity grows
its page.

### ⚠ BREAKING CHANGES
- `stigmergy-entities create` commissions a capture instead of committing a page: it needs
  `--about` (what the entity is, in your own words), the database (`--dsn`) and the evidence
  environment the drop CLIs use, and prints the capture to follow; the page appears when the
  capture files, born confirmed by `--by` (or your git email)
- `POST /admin/api/entities/create` takes `about` (required) and no `role`, and answers the queued
  row (`id`, `status: queued`, `entity_id`, `name`, `message`) instead of a commit; the console's
  Register form asks "What is it?" and opens the capture
- `server.review.create_and_record` is gone (`commission_registration` replaces it);
  `entities.mint` and `entities.remote.mint_via_clone` are gone (`entities.guard` keeps the two
  shared refusals; `decide_via_clone` is the one server-driven door)
- `brain_submit` refuses the four `register_*` hints from every client door
- the knowledge repo's briefs change with it (`e118c8a`): look before you write, the registration
  paragraph, `entity_updates`

### Added
- `entity_updates` in the librarian's account (both flows): what the material establishes about a
  registered entity is APPENDED under that page's own `## Facts` / `## Connections`, `updated:`
  moved, lines the page already carries skipped, the file proved byte for byte; the report says
  "It adds N facts and M connections to the page of `id`"; refusals `update-unknown-entity` and
  `update-of-new-entity`
- a steward's registration through the librarian: `capture.schema.registration_hints` /
  `registration_from_hints`, the brief's REGISTRATION paragraph, `identity/registration-missing`
  for an account that ignores it, the identity gate's `not-confirmed-by-its-steward`, the ledger
  row written by the worker after the push
- `birth.render_page` refuses an entity page with no What / Who, drops a section with nothing to
  say, and strips the template's HTML comments — nineteen pages had carried them, indexed as text

### Removed
- the deterministic mint: `entities/mint.py`, `remote.mint_via_clone`, `review.create_and_record`,
  the console's synchronous Register and the CLI's `create` commit

## [0.7.0] - 2026-08-21

**File first, govern after** ([ADR 041](./docs/decisions/041-file-first-govern-after.md), #125).
A capture never waits on a person any more. A name the registry does not know used to park the
capture on a question to its submitter and then on a steward; two of five real notes were cancelled
by their own authors that way. Now the librarian files at once and PROPOSES the entity — a complete
page with `approved_by: ""` and a `proposed` registry entry — and a steward approves, merges or
declines it later, from whichever door is nearest, in one governed commit.

### ⚠ BREAKING CHANGES
- the capture statuses are `queued · claimed · filed · rejected · failed` (`resolved` survives
  read-only on old rows). `needs_input` and `triage` are retired words the queue refuses by name;
  rows found in them are returned to `queued` once at startup, so nothing already captured is lost
- the `brain_reply` MCP tool is gone — nine tools, pinned by a test. `brain_submit`'s
  acknowledgement now names the entities the capture will be filed against and says unknown ones
  will be proposed
- `review_queue` / `review_decide` speak two new item kinds, `identity-proposal` (item id = the
  entity id; verdicts `approve`, `merge` with `into`, `decline`) and `alias-proposal` (item id =
  `<entity id>:<alias>`; `approve`, `decline`); `entity-proposal` and `parked-capture` are read-only
  legacy kinds on the rows that carry them
- `stigmergy-queue` keeps `list · show · claim · reclaim · purge` and loses `requeue · resolve ·
  reject`; `stigmergy-entities` is now `pending · approve · decline · merge · create · regenerate`
  (`propose` is the librarian's job)
- the admin console's `queue/{id}/requeue|resolve|reject` routes are gone; `entities/{id}` takes a
  registry id; `entities/decide` and `entities/create` are new. The Captures page is read-only
- entity pages carry `approved_by` (absent = confirmed before the field existed, `""` = proposed,
  a name = who confirmed it) and `proposed_aliases`; the registry carries `proposed`,
  `approved_by` and `proposed_aliases`. The knowledge repo's librarian and meeting-distiller briefs,
  entity template and linter move with it — both repos ship together

### Added
- `librarian.identity` — the proposal writer: folds every new name against the registry with the
  birth gate's own fold (a known name becomes a proposed spelling, never a twin), writes the
  entity page from every field the reasoning filled, reads the ledger so a declined name is never
  proposed twice, and tells the gates exactly what it wrote
- the ninth gate, `identity`: every write under `wiki/entities/` is a declared proposal arriving
  unconfirmed, or a proposed spelling proved byte for byte — nothing else
- `entities.decide` — approve / merge / decline for an identity, approve / decline for a spelling,
  one `apply` with preflight, drift refusal, secrets scan, one commit and rollback on a failed push;
  `remote.decide_via_clone` for the deployed doors, `Decided-by:` trailer
- the inbox is derived from the registry, `pages_index` and the ledger — no new table; proposed
  entities are visible in search and `list_entities`, marked
- the console's Entities desk: each proposal with the registry verdict on its name, merge
  candidates, Approve / Merge into… / Decline, proposed spellings, the registry browser, and
  **Register an entity** born confirmed with the live name check
- the Slack card for a proposal, with the same three verbs
- the filing report's proposals clause; the digest counts proposals decided

### Removed
- the ask-back: `brain_reply`, the `needs_input`/`triage` states, the three dispositions, the
  parked-capture mint door, `entities.situations`, the console's queue drain, the meeting flow's
  "reuse the parked distillation" rule (nothing is re-filed, so nothing is re-read)

## [0.6.0] - 2026-08-20

The admin console grows up from a quick ops skin into a control room a steward or an admin can
read without the runbook open: grouped navigation, a unified inbox, plain-language labels for every
system word (the raw word one hover away), a "how to read this page" explainer per page, and
charts — every one with a table twin — in the README's own "colour is who decides" key, validated
for colour-vision deficiency in both modes — which a steward can now choose between, or leave to
the device.

### Added
- `/admin/api/inbox` — everything parking on a human as ONE list, the same read the Slack
  doorbell rings from (`server.review.items_for_doorbell`), with per-kind counts; the sidebar badge
  is its count
- `/admin/api/entities/registry` and `/admin/api/entities/resolve` — the registry this server
  serves (the index's snapshot, else the `--entity-registry` file) and the pre-mint check over it
  with the mint gate's own folds: `registered` (requeue, nothing to mint), `collides` (the gate
  will refuse it — alias it in the knowledge repo instead of minting a twin), `similar` (advisory),
  `clear`. Both entity routes attach a verdict per unresolved name; the Approve form checks the
  Name and every Alias live as the steward types; the Entities page carries a searchable registry
  browser
- `/admin/api/metrics?days=` — captures by arrival day and outcome (`queue.outcomes_by_day`, new
  beside `counts_by_status`), capture→filed samples, `ask` outcomes per day shaped with the pilot
  report's own predicates, calls per day/tool/identity, each job's run history, the latest
  decisions, repair counts
- `/admin/api/meta` ships every closed vocabulary the console renders (statuses with their parked
  and terminal subsets, situations, repair kinds, severities, item kinds, decision doors), so the
  frontend never hardcodes a list that could drift
- the Dashboard's live write path — the window's captures flowing through the model's draft and
  code's gates into landed / parked / refused / could-not-finish, with real counts
- an appearance picker — Auto, Light or Dark — in the sidebar and on the login screen, remembered
  in the browser and stamped before the first paint (no flash on the way in). Every colour token
  is one `light-dark(light, dark)` declaration, so the two themes cannot drift apart

### Changed
- the console's pages: Dashboard, Inbox, Captures, Entities, Repairs, Gardener, Index, Worker,
  Jobs, Digest, Activity (the old tab names `overview`/`queue`/`crons` still route); every page
  opens with a collapsible explainer, pages with a time axis share one 7/30/90-day window
- the frontend is one module per page under `static/assets/views/`, with `copy.js` (the
  vocabulary), `charts.js` (SVG charts built with `createElementNS`) and `state.js`; the static
  discipline tests follow the split
- the Jobs page renders no levers at all without the GitHub token, instead of disabled ones

### Fixed
- the shell and its assets carry `cache-control: no-cache` — a deploy that renames a module no
  longer leaves a browser running the old `app.js` against new imports for hours (a blank page)
- inline `style` attributes, which `style-src 'self'` silently refused, are gone: every style goes
  through the CSSOM
- the librarian's placeholder for a park that named nothing (`something unnamed`) is refused by
  value at the terminal name gate (`entities.birth`) — every mint door, not only the prefill rule;
  `entities.situations.is_mintable_name` is the one comparison, and the console's per-name checks
  carry it as `mintable` so no surface offers a button for it
- `metrics` runs off the event loop and every read of a table that only grows is bounded in SQL:
  `decisions.recent_decisions` (the ledger feed), `repair.store.counts_by_status` (the whole-table
  histogram), a ceiling on the pending proposals the console reads, `pilot_report.answer_shape_by_day`
  (the report's own classifier, grouped in SQL and pinned against the Python original)
- the pre-mint similarity listing folds the registry once per request, not once per name
- `answer_shape_by_day`'s SQL mirror of `shape_of` no longer casts `result ->> 'citations'` to
  `int`. `audit_log.result` is JSONB with nothing under it, and an `ask` row older than
  `audit_summary` carries `citations` as the LIST of page paths — so the cast raised on real data
  while every test that fed it today's integer stayed green, and since eight of the console's
  eleven pages fetch `metrics`, one legacy row rendered almost the whole console as failed. The
  mirror now asks the question `shape_of` asks — truthiness — as a JSONB comparison against the
  falsy set, which cannot raise whatever a past writer left in the column
- the deterministic-check count is gone from the prose that nothing pins: six sites said "nine"
  and the live workflow said "eight" for a tuple of ten. The package docstrings, the console's two
  copies of the cron description and the cron template now say "the deterministic checks", which
  survives the next one being added; the sites that state the number derive it. Beside it, a guard
  that every `*.py` a code map names in backticks exists

## [0.5.0] - 2026-08-20

v0.4.0's follow-up tracker, emptied: the five issues the open-models port filed against itself
(#110–#113, #115), each landed behind the instrument that owns it. The reference deployment's
embeddings move to `qwen3-embedding-8b` at 2560 MRL dimensions through the same OpenRouter key —
gated by the retrieval golden holding recall@5 at 0.969, identical to the `text-embedding-3-large`
baseline it replaces.

### Added
- `EMBED_DIMENSIONS` — MRL truncation for over-ceiling embedding models: request-level
  `dimensions`, sent only when set, refused by name when malformed; what fits a 4096-native model
  under the schema's 4000-dimension HNSW ceiling (#116, closes #115)
- `index_meta` records the embedding HOST beside model and dim, and `search_arms` refuses a
  host mismatch by name BEFORE the first embedding — the same model name on two hosts is not
  provably the same vector space, and a mismatched query returns noise without failing. Legacy
  indexes without a recorded host skip the check until their next rebuild (#117, closes #112)

### Fixed
- the vision OCR pass reaches `cost_usd`: both forms return token usage as data, the pass is
  priced through the librarian's one table (an unpriced vision model degrades to a loud `$0.00`
  line, never a refused capture), every exit bills it exactly once, and the report names the
  share on `conversion_cost_usd` (#118, closes #110)
- the visibility-lease derivation carries the drive conversion budget, imported from the
  kernel's own vision clocks so the term cannot drift: 1290s at the class default, 1890s on the
  deployed worker, with every prose statement of the numbers moved in the same commit and the
  reclaim refusal's example lease pinned to the derivation by a parity test (#119, closes #113)
- the docs-claims environment guards see the model-seam env family (`EMBED_*`, `CLEAN_*`,
  `ANSWER_*`, `VISION_MODEL`) through one name-enumerated pattern — never a wildcard, because the
  literal scan cannot tell an env var from a module constant (#120, closes #111)

## [0.4.0] - 2026-08-19

The open-models port. Every model seam now runs on any pydantic-ai provider — OpenRouter
first — and the reference deployment moved to open-weight models behind this repo's own
instruments: the filing golden passes every bar on `deepseek-v4-flash` at roughly $0.008 per
capture, the QA golden scores 1.00 on every axis on `glm-5.2`, and the retrieval golden holds
recall@5 at 0.969 with the same embedding model served through the new host. No gate, ACL,
fence or eval moved: the seams did.

### Added
- `ask` takes the two-form model convention — a bare `ANSWER_MODEL` stays the OpenAI Responses
  API; a provider-prefixed pydantic-ai id authenticates with that provider's own key — and
  `openrouter` joins the librarian's key preflight and pricing table (#107)
- the embedder speaks to any OpenAI-compatible `/embeddings` host (`EMBED_BASE_URL`,
  `EMBED_API_KEY`, `EMBED_MODEL`), with the recorded-model rule keeping every query in its
  index's vector space; the worker boot strips the embed credential like the OpenAI one (#108)
- two-form vision OCR: a provider-prefixed `VISION_MODEL` transcribes poppler-rasterized page
  images through pydantic-ai — bounded pages, a spoken cut — while the bare Gemini form stays
  byte-for-byte what it was (#109)

### Fixed
- an empty model-chosen search query is a repairable refusal handed back to the asking agent,
  instead of a provider 400 that crashed the whole ask (#114)
- the audit sweep over the port (#114): the OpenAI key never travels to a non-default embedding
  host; a prefixed `VISION_MODEL` missing its provider's key refuses naming that key instead of
  advising a requeue that could never work; the rasterizer carries timeouts and a pixel bound
  against raster-bomb pages; the provider→key table lives once, in the kernel; the worker boot
  says out loud when one credential wears two names. Deferred halves filed as #110–#113
- account schemas decode nested structures a provider's tool-calling stringifies — the defect
  that filed meetings with `decisions` empty on routes returning nested lists as JSON strings
  (#114)

## [0.3.1] - 2026-08-19

The three decisions v0.3.0's adversarial review filed (#101, #102, #103), each taken on the side
of simplicity: no new pass machinery, one new knob where two siblings already had theirs, and two
of the three fixes ride seams that already existed.

### Fixed

- **The editorial sweep is bounded on every axis — and deliberately not by batching** (#101). Its
  checks are about PAIRS, and a batch boundary would silently decide which contradictions are ever
  visible. Instead: the changed half keeps the newest `STIGMERGY_GARDENER_SWEEP_CHANGED_CEILING`
  (default 30) filings, every fenced body is clamped, and the prompt becomes settings-shaped —
  never corpus-shaped, even on a first run or after a cron outage. The overflow is counted, named,
  and never lost: it joins the unchanged pool the rotating sample already covers. A failed night
  re-presents a bounded population, so the frozen-watermark loop degrades to a bounded retry.
- **The view sweep yields to arriving work, and a hung synthesis has a clock** (#102).
  `should_stop` becomes a reason-string contract: the worker's own callable says "the process is
  shutting down" or "a capture is waiting in the queue", and the recorded deferral repeats those
  words — a capture submitted mid-pass now costs one entity's regeneration, not a whole ceiling's.
  `SYNTHESIS_TIMEOUT_S` turns a provider that stops answering into the existing withheld shape
  instead of a hung worker loop with no lease, no row and no log line.
- **The night's one number bounds the asks, not only the inbox** (#103). The per-finding repair
  roads (body, merge) stop at `max_proposals_per_run` asks with a recorded `ask-ceiling-reached`
  reason — a night of declined drafts, which store nothing by design, is now a bounded bill. The
  recurrence stays deliberate; what is bounded is its nightly cost, and the spend records from
  #81 show how close a night comes.

## [0.3.0] - 2026-08-19

The governed repair loop grows from a design into a working subsystem with four proposal kinds,
the view layer converges itself, and the `ops/` control files a deployed process trusts stop being
deploy-time copies. Two adversarial audits ran over the batch — one on the original five issues,
one on the whole window — and their surviving findings are either fixed here or filed with a
decision recorded (#101, #102, #103).

### Added

- **The governed repair loop** (ADR 039, #69/#71/#72/#89): a gardener finding gets a path to zero
  through four proposal kinds — additive edits, one drafted `entity-body` per page, governed
  `delete` (the one kind no model may propose), and `entity-alias` (two registry entries that are
  one entity: the model picks the survivor, code computes the sweep, one steward decision per pair
  permanently). A MODEL proposes, CODE validates twice, a HUMAN approves one at a time.
- **The view sweep** (#86): a view is never stale, whatever wrote the corpus — a state-based
  convergence pass on the librarian worker's idle branch, one commit per entity, cooperative
  shutdown between entities, mutual exclusion by advisory lock.
- **Two more model checks** (#84, #89): an entity body that is written and says nothing, and two
  registry entries that denote one company; plus a tenth deterministic check
  (`anchored-to-superseded-entity`, #88) counting the residual an applied merge cannot sweep up.
- **The ops-file cache** (ADR 040, #74/#79): the entity registry, the identity roster and the
  channel scope map ride the derived index as verbatim-TEXT snapshots, refreshed by the push
  webhook (fetched at the BRANCH ref — a replayed delivery can only install what the branch says
  now) and reconciled per file by the nightly rebuild (the access files are never cleared over an
  absent checkout copy). A revocation lands within seconds of its push instead of at the next
  deploy. The page road gets delivery-id replay protection; an EMPTY snapshot resolves nobody.
- **The budgets' feedback loop** (#75/#81): every proposer model call records requests and tool
  calls against its limits — and token counts — into `job_runs.stats`; the edits budget derives
  from the batch it carries; `kernel.llm.model_override` is the public seam for proving tool-loop
  properties against the real agent, keyless.
- **Agentic entity resolution** (#77): a filing near miss is the agent's judgment, with four new
  filing-eval fixtures pinning it.
- The meeting distiller sees the corpus it files into (ADR 038); `project` joins the entity types
  (ADR 037); every review decision records its door, and stale Slack cards close themselves.

### Fixed

- An empty entity-body draft is the park, not a validation error — one model call, not two,
  forever (#83).
- Every unfenced prompt-header scalar is hygiened, not only the path — and the seventh prompt
  builder (`views/synthesis.py`) that the consolidation sweep never reached (#92 follow-up).
- A backlink that stopped qualifying stops being cited (#85); the view agent names its model
  instead of inheriting the read path's (#90).
- The non-additive repair kinds never rebase: a lost push race fails clean instead of landing a
  diff the gates never judged against that base (#88).
- The weekly digest reads every model pass's error, so a `partial` run can never render as a
  clean one; a blank entity body no longer falls between the deterministic and the model halves
  of the empty-body pair.
- `STIGMERGY_REPAIR_BATCH` — the one count knob that multiplies a model budget — gains a maximum;
  entity names refuse control characters; three `entities` refusals stop echoing a foreign
  exception; non-ASCII stems are written verbatim in frontmatter lists.
- The registry served by MCP follows `main`, not the deploy (#74); `stigmergy-index --check`
  lints the copy the server actually serves.

### Changed

- The whole-branch refactor sweep (#92): one definition per fact across fourteen modules —
  shared seams single-sourced, dead surface dropped, the ordinary/meeting filing copy-paste
  collapsed, prompt-scalar hygiene folded down into `stigmergy.text`.
- `_expansion_terms` is bounded by count and term length; the two registry parsers' authority is
  stated (strict for writing, tolerant for serving); the admin console's Index panel answers
  freshness for all three cached ops files.

## [0.2.2] - 2026-08-12

Prose only. No executable code changed in this release, and that is a mechanically verified
claim rather than an assurance: for all 156 modified modules the AST with docstrings blanked is
byte-identical to `v0.2.1`, so no statement, import, name or string literal moved.

### Changed

- **Narrative prose purged from code comments and reference documentation.** This repository's
  own doctrine — *a comment states a constraint the code cannot show, never the story of how the
  code got here* — was not being honoured by the code itself: essay-length docstrings, obituaries
  for deleted subsystems and process narration had accumulated to roughly 46% of `src/`. That text
  cost twice: every change paid upkeep on prose no test protects, and a reader (agent or human)
  spent most of a module's context on history instead of on the system. `src/` goes from 41,180 to
  29,946 lines, with 59% fewer comment/docstring lines; `docs/reference/` from 6,687 to 5,798;
  `evals/` and `scripts/` prose down 44%, `evals/README.md` from 495 to 229 lines. Every package's
  `index.md` code map is rewritten as a present-tense map. Nothing is lost that was not already
  recorded: history lives in `git log` and in `docs/decisions/`, which is exactly what makes it
  safe to remove from the code.
- **Every trace of subsystems that no longer exist is gone from code prose** — the learning loop,
  the extraction pipeline, the retired filing backend, the read site and the canon lane. A reader
  grepping `src/` for a name now finds only names the system actually has.

### Notes for contributors

Four bodies of prose were deliberately left alone, each for a reason worth knowing before the next
cleanup: `tests/` (a test's prose is this project's specification, and the reproduce-first rule
requires a test to say what the old behaviour was), `docs/decisions/` (an ADR records a decision,
so history belongs there by design), MCP tool and pydantic schema docstrings (they are contracts
sent to models and clients, not commentary), and the frozen fixtures asserted byte-identical to
the knowledge repo's own copies.

Two mechanical guards caught genuine over-deletion during the work — the meeting brief's
`date-bearing-body-link` contract marker and the operator runbook's sweep line — and both were
satisfied by restoring the fact, never by relaxing the check.

## [0.2.1] - 2026-08-12

### Added

- **Anthropic prompt caching on the ordinary filing run** (ADR 036) —
  `STIGMERGY_LIBRARIAN_PROMPT_CACHE` (`off` | `5m` | `1h`, default `5m`, refused by name for
  anything else) caches the system prompt, the five tool schemas and the growing message list on
  every turn of an iterating capture, where a cache read prices at a fraction of ordinary input.
  The meeting flow is untouched, byte for byte: one call per capture means a cache write with no
  read ever to offset its own premium. Measured on the filing golden against the third-re-freeze
  baseline — same fixture, same brief, same model, caching the only variable — $0.577 against
  $1.590 over 11 agent passes: **-63.7%**, with the facet table byte-identical and every gated
  bar PASS. The row is in `evals/history.ndjson`.

### Changed

- **`pricing.PRICES` and `$STIGMERGY_LIBRARIAN_PRICING` gain a fourth figure** — `[input, cached
  input, cache write, output]` dollars per million tokens, closing the follow-up ADR 032 named. A
  legacy 3-figure row (`[input, cached input, output]`) is still accepted, normalized with the
  cache write rate equal to the input rate — today's semantics before this column existed — so an
  operator's existing override keeps working unedited.

## [0.2.0] - 2026-08-12

The filing engine moves onto this project's own agentic harness (pydantic-ai), and the
Claude-Code-harness path retires. Two changes an operator upgrading from `0.1.0` must act on:
`STIGMERGY_LIBRARIAN_BACKEND=sdk` is refused at startup, by name (see **Removed**), and
`STIGMERGY_LIBRARIAN_MODEL` now takes a provider-prefixed id — `anthropic:claude-sonnet-5`, not
`claude-sonnet-5` (see **Changed**).

### Added

- **The filing golden** — a third eval instrument, on the one surface that writes: `make
  filing-golden` drives ten frozen captures through the real filing path (agent, eight gates, real
  git, real Postgres) against a frozen mini knowledge repo and scores functional facets
  deterministically, each with its own denominator and its own bar, fixed from the first Sonnet-5
  baseline pair. The fixture pins the brief version every score was measured under.
- **The `FilingAgent` port and the pricing seam** (ADR 032) — the filing backend contract is an
  explicit, typed Protocol with three conforming implementations, and token usage becomes
  `report.cost_usd` through one pricing module (declared-inclusive token convention, a rate table
  with an env override that refuses non-finite, negative and zero-output rates — an unpriced model
  is a loud startup refusal, never a silent `$0.00`).
- **A pydantic-ai meeting backend** (`STIGMERGY_LIBRARIAN_BACKEND=pydantic`) — the meeting flow
  runs as one structured, tool-less call on any provider-prefixed pydantic-ai model string.
- **The structured ordinary flow** (ADR 033) — a deterministic gatherer (`librarian/gather.py`)
  reads the checkout at the base commit and hands the model candidates, an entity view and the
  link neighbourhood instead of live `Read`/`Glob`/`Grep` exploration; the agent returns the page's
  own text in its account and code writes it, confined by construction rather than by a permission
  hook. The pydantic-ai backend now serves both flows, so the meeting-only restriction above (and
  its eval-rig escape) is gone.
- **The agentic pydantic harness — the ordinary filing agent explores again** (ADR 034), on this
  project's own harness rather than a vendor's. `PydanticFilingAgent.run()` is an ITERATING
  pydantic-ai run with five tools over the item's checkout — `search_pages`, `read_page`,
  `list_page_names`, `resolve_entities`, `write_page` — whose bodies are the gatherer's own pure
  functions, with confinement asked INSIDE each tool instead of in a permission hook. Reads reach
  the content zones and the per-type page templates (`ops/templates/<type>.md`, the structural
  source of truth for the container a self-writing run must produce) and nothing else; writes reach
  one new page in the fast-lane folders and the outcome file, through the unchanged
  `agent.confined_write`. ADR 033's
  gathered block survives as the SEED those tools go further from, so the port grows a second
  declaration (`wants_gathered`) beside `structured_ordinary`; the agent writes its own page and
  returns its account as `.librarian-outcome.json` again. The rule behind it, which the next
  milestone should not have to re-derive: **deterministic code may seed context and implement
  tools, and must not replace the model's ability to decide the context is not enough.** The
  meeting flow is untouched — one structured call, no tools.

### Changed

- **`STIGMERGY_LIBRARIAN_MODEL` now takes a PROVIDER-PREFIXED id, and the default moved with it**
  (`claude-sonnet-5` -> `anthropic:claude-sonnet-5`). Same model, same provider: the surviving
  backend resolves ids through pydantic-ai, where a bare name means an OpenAI model, so a bare
  default would have been the one value a worker could not boot on. A worker configured with a bare
  id is refused at startup, and the refusal names the prefixed spelling of the id it was given.
- **`STIGMERGY_LIBRARIAN_MAX_TURNS` is LIVE again** (ADR 034), at the same default and with the
  same meaning it always had: how many model requests one ordinary capture may spend going round
  with its tools. It reaches pydantic-ai as `UsageLimits(request_limit=…)`, and exceeding it is a
  refusal that names the variable rather than a silent stop. It had been deprecated for one
  milestone, while the ordinary flow made a single call; no operator has to re-derive a number,
  because the number did not move.
- **`STIGMERGY_LIBRARIAN_MAX_TOOL_CALLS` stays DEPRECATED** and read by no shipped backend: it was
  a tool-call ceiling the worker counted itself for a harness that had none, and pydantic-ai both
  accumulates tool calls and bounds the loop that makes them by REQUESTS — so a second
  hand-maintained ceiling would need a defect behind it rather than a symmetry. Still PARSED, so a
  value an operator set is not silently dropped — but a malformed one fails the boot with a bare
  `ValueError` rather than a named refusal, which is pre-existing behaviour. Removing it is the
  recorded follow-up. `..._TIMEOUT_S` is unaffected and IS refused by name: the wall clock is still
  enforced around the WHOLE run, and the visibility lease is still derived from it.
- **`AgentRun.turns` and `AgentRun.tool_calls` carry real numbers again** for the ordinary flow,
  read from the framework's own accumulator rather than counted a second time by hand. Zero stays a
  legitimate answer and now means one specific thing — this shape has no loop (every meeting run,
  any structured backend) — never "nobody counted".

- The HTTP tier's tests skip without Docker instead of failing, and the librarian code map names
  both halves of the refusal-routing suite.

### Removed

- **The `sdk` filing backend — the Claude Code harness path — is retired** (ADR 033 D6's gate,
  spent: the full M0 golden all-bars-PASS on the structured flow, a 20-capture staging shakedown
  with zero flow failures, the container e2e green on CI per push, and then an explicit decision).
  Gone with it: `SdkAgent` and its two run methods, the options builders, the three tool-permission
  hooks, the tool allow/deny lists, the subprocess environment allow-list and the Claude-credential
  startup pre-flight; the `claude-agent-sdk` dependency; and, from the image, the Node runtime and
  the ~500MB agent CLI with their entries in `scripts/docker/tool-checksums.txt`. **The image is
  roughly 55% smaller.** `fly.toml` moves to `backend=pydantic` with the prefixed model id.

  **A deployment still configured for `sdk` is refused at startup, by name.** The value lives in a
  `fly.toml` or a gitignored `.env` that a `git pull` does not touch, so this is configuration
  outliving its code rather than a typo: the message says the backend was retired, names the two
  edits the replacement takes, and gives the image rollback (`fly releases` -> `fly deploy
  --image`) for getting a worker running meanwhile. The queue is durable; nothing claimed is lost
  while it is down.

  What is genuinely lost, rather than replaced: the harness lockdown that hardened a subprocess
  which no longer exists. The write-confinement RULE (`agent.confined_write`) is untouched, and it
  is now asked by BOTH shipped backends — the offline double on every keyless filing, and the real
  backend's `write_page` tool on every live one (ADR 034). The hand-counted tool-call ceiling stays
  gone: the framework counts tool calls itself.

- **`worker._check_brief_matches_backend`** (ADR 034) — the startup refusal for a structured worker
  whose knowledge-repo brief still described a tool-holding run. Keyed on `structured_ordinary`, it
  went inert the moment the shipped ordinary backend declared `False`, and an inert check that
  still reads as coverage is worse than no check. The landing-order rule it enforced survives in the
  mechanism that made it enforceable: the brief is environment-neutral, and each backend states its
  own mechanics in the preamble it composes. `pydantic_backend.ORDINARY_ADR` retired with its only
  reader.

### Fixed

- **Filing reliability: a symmetric brief, a corrective facts line, and faults that name
  themselves** (ADR 035) — measured on the agentic harness, 8 of 13 first-pass drafts omitted the
  page's frontmatter block entirely under the old brief emphasis; after the knowledge-repo brief
  rewrite, 0 of 12. Two shape-neutral defenses land with it: the contract gate's `frontmatter`
  finding now appends a facts line stating the field split (what the worker stamps after the draft
  versus what the draft must already carry) to every corrective retry, and a pydantic-ai
  `UnexpectedModelBehavior` fault now persists its real message — bounded, fence-neutralized where
  it reaches a prompt — instead of surviving only as a class name. The two hand-rolled one-line
  composers collapse into one seam, `stigmergy.text.one_line`. The filing eval fixture is re-frozen
  a third time (the librarian brief alone moved; the linter and the meeting brief are
  byte-identical).

Sixteen further bug-sweep fixes and one documentation correction, none of them behaviour changes in
the ADR sense — each closes a gap between what the code promised and what it did. Grouped by what
they protect:

- **Pages that indexed open.** A page whose frontmatter could not be parsed, and two further routes
  to the same end, no longer land in the index with no `acl` label — the failure mode where an
  unreadable page becomes a readable one.
- **Access and identity.** A scoped queue read that failed open; an entity proposal accepted as a
  parked capture on a caller's say-so; an entity registry that could leak its own path or mint a
  phantom alias; a raw byte in a signature header answered `500` instead of `401`; a signed
  non-object webhook body ignored rather than acted on.
- **Refusals that blamed the wrong party.** Two librarian refusals named the wrong cause, a secret
  split across a line break reported a line number that was not one, and `Ctrl-C` stopped claiming
  work on its way out.
- **The surfaces people actually see.** Four Slack defects; a view that never went stale and one
  that cited itself; a hand-written page under `views/` that killed the gardener run; a recency word
  that was matched as a substring; a snippet that was not reproducible.
- **Prose that the code did not keep.** Six documented promises reconciled with the code, and four
  faults that nothing told anybody about.

## [0.1.0] - 2026-08-07

First public release. The system it describes has been running against a real corpus before being
published, so this entry describes what the release **contains** rather than what changed since a
previous one — there isn't one.

### Added

- **The write path.** Captures arrive on a durable Postgres queue and are drained one at a time by
  the librarian: an agent (Claude Agent SDK) drafts a page in an ephemeral git worktree, and then
  **code judges the resulting diff** before anything can commit. Eight deterministic gates —
  zone, binary-page, body-rewrite, secrets, PII, frontmatter, contract linter, anchoring — each of
  which can veto. The agent proposes; code decides.
- **The human loop.** When a capture cannot be placed, the librarian asks its submitter exactly one
  question rather than guessing, and the budget for that is a database column, so it survives a
  retry, a redelivery and a steward requeue. Creating a new entity is a governed act: a steward
  approves it, and `stigmergy-entities` is the only writer of the registry.
- **Read access decided in one place.** Ordered path rules stamp audience labels at write time;
  `server.acl.visible()` is the single function the read path goes through, and an architecture
  test fails the build if any reader of the index bypasses it.
- **One MCP server, ten tools**, over stdio and streamable HTTP, with per-user hashed-token auth,
  an audit log and rate limiting. `ask` answers with citations or refuses — figures and quotes are
  verified against the sources at answer time, by code.
- **Three doors onto the same queue**: Slack (a 🧠 reaction captures a thread verbatim, `@`-mention
  or DM to ask, Block Kit review surfaces for stewards), the meeting distiller (a transcript
  becomes an atomic page *set* — source, meeting, and one page per decision, each anchored
  separately), and a Drive door that fetches through the operator's own Google auth.
- **A hybrid derived index** — Postgres + pgvector, a lexical and a semantic arm fused with
  Reciprocal Rank Fusion, then explainable contract ranking (superseded pages demoted, entity and
  period matches boosted, staleness penalised against an injected `today`). Disposable by design:
  wipe it and rebuild from git.
- **Views**: a per-entity rollup with a deterministic skeleton plus an agent-written synthesis,
  whose audience is the intersection of its members'.
- **The gardener**, eight deterministic corpus-health checks plus a bounded model editorial sweep,
  and a two-section Slack digest scoped to its destination channel.
- **An admin console** at `/admin` on the existing app process group — steward drain, remote
  control of the crons, and an activity view. Inert until its token hash is configured.
- **Seventeen CLIs**, a `docker-compose` stack for the whole thing, and cron templates in
  [`deploy/workflows/`](./deploy/workflows) to copy into your own knowledge repo.

### Notes

- Python 3.12+, Apache-2.0. Knowledge lives in a **separate git repository you own** — this one
  stores no pages.
- The suite is keyless by construction: 3,598 tests at 92.77% coverage run against real Postgres,
  real MinIO and real git, with an offline double standing in for the model. If something needs an
  API key to pass, it is in the wrong place.
- Not yet load-tested beyond a single team's corpus, and the SLA notice path has no producer today
  (nothing emits an `sla` finding) — both are stated in the reference docs rather than left to be
  discovered.

[0.2.2]: https://github.com/sturlese/stigmergy/releases/tag/v0.2.2
[0.2.1]: https://github.com/sturlese/stigmergy/releases/tag/v0.2.1
[0.2.0]: https://github.com/sturlese/stigmergy/releases/tag/v0.2.0
[0.1.0]: https://github.com/sturlese/stigmergy/releases/tag/v0.1.0
