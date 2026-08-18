# gardener — corpus health on demand

Narrative doc: [`docs/reference/gardener-digest.md`](../../../docs/reference/gardener-digest.md).
Siblings that read the findings store and never recompute a check: [`digest`](../digest/index.md)
and [`admin`](../admin/index.md).

`stigmergy-gardener` runs three passes — nine deterministic checks, a bounded model editorial
sweep over changed-plus-sampled pages, and a model empty-body pass over every entity page in the
checkout — and emits **findings only**, persisted to `gardener_findings` with a `job_runs` row and
printed as a severity-grouped report. It fixes nothing, writes nothing, vetoes nothing: no git
plumbing, no literal path under `wiki/`, both pinned by `tests/test_architecture.py`, which also
pins every import edge and the threshold-literal ban.

## Modules

| Module | What it is |
|---|---|
| `cli.py` | `stigmergy-gardener [--repo] [--dsn] [--channels] [--json]` — one command. The only module here that imports `stigmergy.index.store`, `stigmergy.librarian.config` or `stigmergy.slack.bolt_gateway` |
| `run.py` | `run_gardener` — the one function the CLI calls: run everything, persist findings + a `job_runs` row in ONE transaction, re-fetch, post the SLA notice. Owns `RunResult` |
| `checks.py` | The nine deterministic checks, `ALL_CHECK_SLUGS`, `build_finding` (the one finding-dict assembler, shared with both model passes), `count_indexed_pages`, `_recent_filed_pages`, and `entity_zone_pages`/`placeholder_lines` — the confinement-checked walk of the entity zone (run ONCE per run by `run.py`) and the one spelling of "still its template". `check_entity_placeholder_bodies` and `sweep.select_empty_body_pages` are pure functions of that walk's list |
| `sweep.py` | BOTH model passes: the shared schema, `_validate` (its `allowed_slugs` is what keeps the two vocabularies apart), `_run_batch`, `to_finding`. The editorial four — `SWEEP_SYS`, `build_prompt`, `run_sweep`, `build_judge`, `FakeGardenerSweep`, page selection (`previous_run_watermark`, `select_pages`). The empty-body fifth — `EMPTY_BODY_SYS`, `build_empty_body_prompt`, `run_empty_body_sweep`, `build_empty_body_judge`, `FakeEmptyBodySweep`, `select_empty_body_pages`/`in_batches` |
| `store.py` | `gardener_findings` persistence: `insert_findings`, `findings_for_run`, `latest_completed_run` |
| `report.py` | The terminal report: `render_report`, `render_json`, `sweep_summary_text`. Pure text from plain data. Both model passes name their own failure there, and the empty-body ceiling names what it deferred |
| `notice.py` | The SLA Slack notice: `sla_findings`, `scope_findings_to_channel`, `compose_notice`, `require_channel`, `post_sla_notice` |
| `schema.py` | The DDL behind `startup_ddl_lock`, `JOB_NAME`, the severity/source vocabularies, `MAX_DETAIL_CHARS`, `MAX_MODEL_DETAIL_CHARS` |
| `settings.py` | `GardenerSettings.from_args` — the thresholds, the digest channel, the model, the sweep's sample size and the empty-body pass's batch (bounded above by `MAX_EMPTY_BODY_BATCH`, the only thing between the population and a single call) and run ceiling. Owns `DIGEST_CHANNEL_ID_ENV` and `SLACK_BOT_TOKEN_ENV`, which `digest.settings` re-exports, and `int_setting`, the shared count-shaped env validator |
| `errors.py` | `GardenerError` (a precondition on running the tool) and `SweepGarbage` (a run-level outcome — a sibling, not a subclass) |

Downstream: `digest` imports `schema`/`store`/`settings` (`int_setting` included — a declared
consumer, importing it rather than hand-mirroring it); `admin` imports `store`/`schema`.
Nothing else imports this package.

`cli.py`'s `_connect`/`_gateway`/`_repo`/`main` are a deliberate twin of `digest/cli.py` — change
both or neither.

## Reuse

- `checks.build_finding` — the one finding assembler; `**extra` carries keys the table lacks
  (the `_notice_*` family). `subjects` is derived from `subject` unless a caller passes the list
  it already has (`sweep.to_finding` does).
- `store.latest_completed_run` — `status IN ('ok','partial')`; `sweep.previous_run_watermark` —
  `'ok'` only. The two readers disagree on purpose: a partial run's findings are trustworthy, its
  sweep baseline is not.
- `views.staleness.list_stale_entities` / `list_all_anchored_entities` — reused verbatim by two
  checks. Import `views.staleness`, never `views.regenerate` (which loads the git write stack).
- `librarian.page.is_provenance_type` — a provenance page's `entity: []` means "no evidence
  found", never a checked company-wide declaration.
- `stigmergy.text.fence`/`sanitize`/`clamp`/`parse_result_ref` — bodies reach the model only
  fenced; model text is sanitized then hard-clamped (the clamp guarantees the column bound).
- `capture.ops.record_job_run` — called directly, not via the `job_run` context manager: every
  finding needs `run_id` at insert time.

## Avoid

- Never write anything but a finding and a `job_runs` row; a `suggested_action` may NAME a
  command, this package never runs one.
- Never generate a `suggested_action` from model output — `MODEL_SUGGESTED_ACTIONS` is a static
  dict looked up by slug, zero interpolation even of the trusted subject path.
- Never let a model pass read the module's full slug vocabulary. `_validate` takes `allowed_slugs`
  as a PARAMETER, and it is load-bearing in both directions: it is what lets the empty-body pass
  accept only its own slug, and what stops the editorial sweep from emitting that slug on a
  sampled page.
- Never let either model pass's failure abort the run (`_run_sweep_pass`/`_run_empty_body_pass`
  each catch everything), and never commit `'ok'` when either failed — a run whose model pass never
  happened must not read as a clean bill of health for the pages nothing looked at.
- Never derive a watermark from `job_runs.status`. That status is an AGGREGATE over two independent
  passes; `previous_run_watermark` asks the sweep's own `stats.sweep.error` instead, the same read
  `digest.sections` performs on the same blob. Reading `'ok'` only would freeze the sweep's `since`
  and its sample rotation every time the OTHER pass failed.
- Never walk the entity zone inside a check or a pass. `run.run_gardener` walks it once and hands
  the list to both consumers; two walks straddling the sweep's model call would report a page
  twice or not at all, and the exclusion between the two checks is only exact over one page set.
- Never follow a symlink out of that walk, and never read a file above `MAX_ENTITY_PAGE_BYTES`.
  What the walk reads leaves the machine — a body is fenced into a model prompt and an excerpt is
  persisted, printed and rendered in the admin console — so a symlinked leaf AND every resolved
  path component are refused (`page.is_inside` + the leaf `islink`, the pair `gather._confined`
  uses). Every refusal is counted into `stats['empty_body']['walk_exclusions']`.
- Never sample the empty-body population. It COVERS the entity zone; the ceiling is a spend bound
  for a corpus that grew hundreds of entity pages, and when it binds the run records what it
  deferred in `stats`, as a skip reason and as a log warning. A ceiling that truncated in silence
  is the failure that check exists to end.
- Never put a threshold literal outside `settings.py` (grep-asserted).
- Never compose the notice's wording in `stigmergy.slack.copy`; `report.py` writes terminal
  markdown, `notice.py` writes Slack mrkdwn — two dialects, never mixed. The notice emoji is `⚠️`
  + the word "SLA", never the bell (the bell means "a decision waits in `review_queue`").

## Contracts

- `gardener_findings`: `id`, `run_id`, `check_slug` (Python key `"check"`), `severity`, `source`,
  `subject`, `subjects`, `detail`, `suggested_action`, `created_at`, `model_id`. `subject` is the
  display string (comma-joined when a sweep finding names two pages) and `subjects` the same fact
  as a LIST — a consumer that acts on the pages reads the list, never re-splits the prose. Empty
  when a finding names none (`check_company_wide_fraction` reports a corpus-wide fraction).
- Check populations, severities and settings defaults live in `checks.py`/`sweep.py`/
  `settings.py` beside their code. TWO checks have a REPAIR of their own
  (`repair.schema.KIND_ENTITY_BODY`) and they are the deterministic and judged halves of one
  question: `entity-placeholder-body` (a body still carrying the template's angle markers) and
  `model-empty-entity-body` (a body somebody wrote that says nothing about the entity). Each names
  ONE entity page and the repair proposer drafts that page's body. The two populations are
  disjoint BY CONSTRUCTION — one walk per run, and `select_empty_body_pages` excludes from it a
  page the deterministic check reports, before the model is asked — so one page is one finding and
  one draft, never two. This package still fixes nothing — it does not import the repair loop,
  and the repair loop reads findings from `store` and nothing else. No check currently emits `sla`, so the notice machinery has no
  producer — a check that adds one must also set `_notice_page_paths` (the ACL scoping key, a
  LIST) and `_notice_detail`/`_notice_action`.
- `job_runs.status`: `'ok'`, `'partial'` (either model pass failed, deterministic findings
  intact), `'error'`. `stats['sweep']['selected_at']` is the editorial sweep's watermark; the
  empty-body pass keeps none, because it covers its population every run — and neither watermark
  is read off that status, only off its own pass's recorded `error`. `stats['empty_body']` carries
  `population`/`excluded_placeholder`/`considered`/`judged`/`deferred`/`unjudged`/
  `walk_exclusions`. `skipped` means validation rejections in BOTH passes, so a dashboard may
  compare them; the ceiling's own count is `deferred`. Every population exclusion is counted into
  `stats`, never silently dropped.

Tests live in `tests/gardener/`; the layering, git-plumbing, `wiki/`-path and threshold proofs in
`tests/test_architecture.py`.
