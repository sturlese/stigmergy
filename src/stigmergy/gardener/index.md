# gardener — corpus health on demand

Narrative doc: [`docs/reference/gardener-digest.md`](../../../docs/reference/gardener-digest.md).
Siblings that read the findings store and never recompute a check: [`digest`](../digest/index.md)
and [`admin`](../admin/index.md).

`stigmergy-gardener` runs two passes — eight deterministic checks plus a bounded model editorial
sweep — and emits **findings only**, persisted to `gardener_findings` with a `job_runs` row and
printed as a severity-grouped report. It fixes nothing, writes nothing, vetoes nothing: no git
plumbing, no literal path under `wiki/`, both pinned by `tests/test_architecture.py`, which also
pins every import edge and the threshold-literal ban.

## Modules

| Module | What it is |
|---|---|
| `cli.py` | `stigmergy-gardener [--repo] [--dsn] [--channels] [--json]` — one command. The only module here that imports `stigmergy.index.store`, `stigmergy.librarian.config` or `stigmergy.slack.bolt_gateway` |
| `run.py` | `run_gardener` — the one function the CLI calls: run everything, persist findings + a `job_runs` row in ONE transaction, re-fetch, post the SLA notice. Owns `RunResult` |
| `checks.py` | The eight deterministic checks, `ALL_CHECK_SLUGS`, `build_finding` (the one finding-dict assembler, shared with the sweep), `count_indexed_pages`, `_recent_filed_pages` |
| `sweep.py` | The model sweep: schema, prompt, validation + one retry, `run_sweep`, `build_judge`, `to_finding`, `FakeGardenerSweep`, page selection (`previous_run_watermark`, `select_pages`) |
| `store.py` | `gardener_findings` persistence: `insert_findings`, `findings_for_run`, `latest_completed_run` |
| `report.py` | The terminal report: `render_report`, `render_json`, `sweep_summary_text`. Pure text from plain data |
| `notice.py` | The SLA Slack notice: `sla_findings`, `scope_findings_to_channel`, `compose_notice`, `require_channel`, `post_sla_notice` |
| `schema.py` | The DDL behind `startup_ddl_lock`, `JOB_NAME`, the severity/source vocabularies, `MAX_DETAIL_CHARS`, `MAX_MODEL_DETAIL_CHARS` |
| `settings.py` | `GardenerSettings.from_args` — the thresholds, the digest channel, the sweep's model and sample size. Owns `DIGEST_CHANNEL_ID_ENV` and `SLACK_BOT_TOKEN_ENV`, which `digest.settings` re-exports, and `int_setting`, the shared count-shaped env validator |
| `errors.py` | `GardenerError` (a precondition on running the tool) and `SweepGarbage` (a run-level outcome — a sibling, not a subclass) |

Downstream: `digest` imports `schema`/`store`/`settings` (`int_setting` included — a declared
consumer, importing it rather than hand-mirroring it); `admin` imports `store`/`schema`.
Nothing else imports this package.

`cli.py`'s `_connect`/`_gateway`/`_repo`/`main` are a deliberate twin of `digest/cli.py` — change
both or neither.

## Reuse

- `checks.build_finding` — the one finding assembler; `**extra` carries keys the table lacks
  (`model_id`, the `_notice_*` family).
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
- Never let the sweep's failure abort the run (`_run_sweep_pass` catches everything), and never
  commit `'ok'` on a failed sweep — `'partial'` is what keeps the sweep watermark from advancing
  past pages nothing judged.
- Never put a threshold literal outside `settings.py` (grep-asserted).
- Never compose the notice's wording in `stigmergy.slack.copy`; `report.py` writes terminal
  markdown, `notice.py` writes Slack mrkdwn — two dialects, never mixed. The notice emoji is `⚠️`
  + the word "SLA", never the bell (the bell means "a decision waits in `review_queue`").

## Contracts

- `gardener_findings`: `id`, `run_id`, `check_slug` (Python key `"check"`), `severity`, `source`,
  `subject`, `detail`, `suggested_action`, `created_at`, `model_id`.
- Check populations, severities and settings defaults live in `checks.py`/`sweep.py`/
  `settings.py` beside their code. No check currently emits `sla`, so the notice machinery has no
  producer — a check that adds one must also set `_notice_page_paths` (the ACL scoping key, a
  LIST) and `_notice_detail`/`_notice_action`.
- `job_runs.status`: `'ok'`, `'partial'` (sweep failed, deterministic findings intact), `'error'`.
  `stats['sweep']['selected_at']` is the sweep watermark; every population exclusion is counted
  into `stats`, never silently dropped.

Tests live in `tests/gardener/`; the layering, git-plumbing, `wiki/`-path and threshold proofs in
`tests/test_architecture.py`.
