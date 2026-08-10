# gardener — corpus health on demand: eight deterministic checks + a bounded model sweep

Narrative doc: [`docs/reference/gardener-digest.md`](../../../docs/reference/gardener-digest.md).
Design record: [ADR 024](../../../docs/decisions/024-gardener-digest.md) (the two-pass pattern, the
watermark, `job_runs.status`'s `partial` value, the ACL scoping and its residual, why
`views/staleness.py` exists), plus [ADR 026](../../../docs/decisions/026-the-purge.md) (which checks
the purge removed) and [ADR 027](../../../docs/decisions/027-the-contraction.md) (which one it
added). Siblings that read this one's findings store, and never recompute a check:
[`digest`](../digest/index.md) (the weekly Slack post) and
[`admin`](../admin/index.md) (the console's gardener tab,
[ADR 029](../../../docs/decisions/029-admin-console.md)).

This file is the code map — for whoever is about to edit this package, not run it.

## Purpose

The corpus has no other health surface: cross-document patterns (anchor concentration, dead
vocabulary, an aging seed nobody promoted) are invisible to any single filing. `stigmergy-gardener`
runs two passes — **eight deterministic checks** (exact, cheap, model-free) plus a **bounded model
editorial sweep** (judgment, for what a query cannot see) — and emits **findings only**, persisted
to `gardener_findings` with a `job_runs` row and printed as a severity-grouped report.

**It fixes nothing, writes nothing, vetoes nothing.** It never writes a page, opens a PR, edits the
registry or regenerates a view — it only NAMES the command for that. Ruled out structurally rather
than by discipline: this package imports no git plumbing and holds no literal path under `wiki/`,
both mechanically pinned.

The check count moved twice, and both moves are worth knowing because the SLA machinery below is
what they left behind. The purge removed three slugs with the canon lane (`stale-canon`, and both
arms of the contradiction SLA, which queried the now-gone `canon_proposals` table), leaving seven.
The contraction then stepped `date-bearing-body-link` DOWN from the meeting flow's own filing veto
to a finding here — a style convention belongs to the gardener, and the veto's justification had
gone with ingest-time figure verification — making eight.

**Read this before trusting the SLA machinery: nothing in this package can currently produce an
`sla` finding.** Every deterministic check emits `info` or `warn`; `MODEL_CHECK_SEVERITY` maps all
four model slugs to `warn`, with a stated reason (none carries a real time-bound clock, so a
manufactured urgency would be dishonest). The two checks that once emitted `sla` were the
contradiction-SLA arms. So `notice.post_sla_notice` short-circuits on every run, and everything
below it — `scope_findings_to_channel`, `_visible_page_paths`, `_redact` — is unreachable in
production today. It is tested and correct; it has no producer. The `_notice_page_paths` /
`_notice_detail` / `_notice_action` keys those functions read are, likewise, set by nothing.

## Key entry points

| Module | Owns |
|---|---|
| `cli.py` | `stigmergy-gardener [--repo] [--dsn] [--channels] [--json]` — one command, no subcommands. The only module here that imports `stigmergy.index.store`, `stigmergy.librarian.config` or `stigmergy.slack.bolt_gateway` |
| `run.py` | `run_gardener` — the one function the CLI calls: load the registry, run every check, run the sweep, persist findings + a `job_runs` row in ONE transaction, re-fetch, post the notice. Owns `RunResult`, `_run_all_checks`, `_run_sweep_pass` |
| `checks.py` | The eight deterministic checks, `ALL_CHECK_SLUGS`, `build_finding` (the one finding-dict assembler, shared with the sweep), `count_indexed_pages`, and the shared `_recent_filed_pages` population |
| `sweep.py` | The model sweep: `SweepFindingSpec` / `SweepBatchOutput`, `SWEEP_SYS`, `build_prompt`, `tag_selected_pages`, `_validate`, `run_sweep`, `build_judge`, `to_finding`, `FakeGardenerSweep`, plus page selection (`previous_run_watermark`, `select_pages`) |
| `store.py` | `gardener_findings` persistence: `insert_findings`, `findings_for_run`, `latest_completed_run`. Never composes text, never decides what a check found |
| `report.py` | The terminal report: `render_report`, `render_json`, `sweep_summary_text`. Pure text from plain data |
| `notice.py` | The SLA Slack notice: `sla_findings`, `scope_findings_to_channel`, `compose_notice`, `require_channel`, `post_sla_notice` |
| `schema.py` | The DDL behind `startup_ddl_lock`, `JOB_NAME`, the severity/source vocabularies, `MAX_DETAIL_CHARS` (500), `MAX_MODEL_DETAIL_CHARS` (200) |
| `settings.py` | `GardenerSettings.from_args` — five thresholds, the digest channel, the sweep's model and sample size. Also the OWNER of `DIGEST_CHANNEL_ID_ENV` and `SLACK_BOT_TOKEN_ENV`, which `digest.settings` re-exports |
| `errors.py` | `GardenerError` (a precondition on running the tool) and `SweepGarbage` (a run-level outcome — a deliberate sibling, not a subclass) |

**Who depends on this package**, and on exactly what: `digest`, on `schema`, `store` and
`settings`; `admin`, on `store` (`latest_completed_run` / `findings_for_run`, read-back only) and
`schema` (`JOB_NAME`, `ensure_gardener_schema`). Nothing imports `checks`, `sweep`, `run`,
`report` or `notice` from outside — a second caller of a CHECK would be a new decision, not a
reuse.

## Use these

- **`checks.build_finding`** — the ONE place a finding dict is assembled, shared by every
  deterministic check AND by `sweep.to_finding`. `**extra` is the escape hatch for keys the table
  does not have (`model_id`, and the `_notice_*` family).
- **`store.findings_for_run`** — and note that `run_gardener` deliberately RE-FETCHES through it
  after committing rather than reusing the in-memory list, so `--json` and the report render what is
  durably true. The one exception is the notice, which needs the pre-insert list because the
  `_notice_*` keys never survive a round-trip.
- **`store.latest_completed_run`** — `status IN ('ok','partial')`, because a `partial` run's
  FINDINGS are exactly as trustworthy as an `ok` run's: they were computed and persisted before the
  sweep ever ran.
- **`sweep.previous_run_watermark`** — `status = 'ok'` ONLY, deliberately narrower than the above. A
  `partial` run is one where the sweep itself failed, so its `stats.sweep` never advanced the
  rotation and must never be the next sweep's baseline. The two readers of one column disagree on
  purpose; `capture.ops`'s module docstring is the shared spec for that vocabulary.
- **`views.staleness.list_stale_entities` / `list_all_anchored_entities`** — reused verbatim by two
  checks, never re-derived. Import `views.staleness`, never `views.regenerate`: the latter
  module-level-imports `views.writer`, which would load the entire git write stack into every
  gardener process.
- **`librarian.page.is_provenance_type`** — imported as pure policy. A provenance page's
  `entity: []` means "the extractor found no evidence", never a checked company-wide declaration;
  three checks would lie about their own population without it.
- **`stigmergy.text.parse_result_ref`** — the shared `'<path>@<sha>'` parser, used by both
  `checks._recent_filed_pages` and `sweep.select_pages`.
- **`stigmergy.text.fence` / `sanitize` / `clamp`** — a page body reaches the model only fenced; a
  model's rationale and excerpt are sanitized before composition, and the composed `detail` is
  hard-clamped regardless of how the two sized individually. The clamp is what GUARANTEES the
  column bound, independent of validation.
- **`capture.ops.record_job_run`** — called DIRECTLY, not through the `job_run` context manager,
  because every finding needs `run_id` at insert time and that manager only writes its row on exit.
  `run.py` replicates the same try/except/re-raise shape by hand.

## Avoid / anti-patterns

- **Never write anything but a finding and a `job_runs` row.** A `suggested_action` may NAME a
  command; this package never runs one.
- **Never import git plumbing.** Pinned by `test_gardener_never_touches_git_plumbing` and — because
  an AST-level check cannot see a transitive load — by a subprocess-based transitive-reach pin. Both
  exist because an earlier `checks.py` imported `views.regenerate` and pulled the whole write stack
  in while the AST check reported clean.
- **Never hold a literal path fragment under `wiki/`.** Pinned by its own test.
- **Never import `stigmergy.server` beyond `errors.StartupError` / `errors.IdentityError`,
  `acl.visible` / `all_visible` and `review.ensure_review_schema`; never `stigmergy.answer`; never
  `stigmergy.entities`; never `stigmergy.librarian` beyond `page` and (CLI only) `config`.**
- **Never let a threshold literal appear outside `settings.py`.** Grep-asserted; every env name
  lives beside its `DEFAULT_*`.
- **Never generate a `suggested_action` from model output.** `MODEL_SUGGESTED_ACTIONS` is a static
  dict keyed by slug and `to_finding` looks it up — no formatting, no joining, no interpolation of
  even the trusted subject path. The bright line is "zero interpolation for any model-sourced
  action", because "only trust the untrusted parts" is exactly the judgment that fails under
  injection pressure.
- **Never let the sweep's failure abort the run.** `_run_sweep_pass` catches everything and reports
  through its returned stats. Every OTHER failure inside `run_gardener`'s try block aborts the run
  entirely, by design — nothing computed so far can be trusted either. Note that page SELECTION sits
  outside that try on purpose: a bug in pure SQL is a real defect, not a sweep outage.
- **Never commit `'ok'` on a failed sweep.** The failure `'partial'` prevents was real: a week of
  model outage produced seven `'ok'` rows, a daily-advancing watermark, and every page filed during
  the outage permanently excluded from the "changed" set — while `job_runs WHERE status='error'`
  reported zero failures the whole time.
- **Never compose Slack copy in `stigmergy.slack.copy`.** The notice's wording lives in `notice.py`;
  `slack.copy` is scoped to that package's own surfaces.
- **Never use `#`/`##` in a Slack surface or `*bold*`/`•` in the terminal report.** `report.py`
  writes markdown for a terminal; `notice.py` writes Slack mrkdwn. Two readers, two dialects.
- **Never reuse the 🔔 emoji for an SLA notice.** The bell means "a decision is waiting in
  `review_queue`" everywhere else, and no `review_decide` verdict ever closes an `sla` finding. It
  is `⚠️`, always paired with the plain word "SLA".

## Data & contracts

- **`gardener_findings`** — `id`, `run_id`, `check_slug`, `severity`, `source`, `subject`, `detail`,
  `suggested_action`, `created_at`, `model_id`, with a plain index on `run_id`. The column is
  `check_slug`, never bare `check` (a reserved SQL keyword); the Python key stays `"check"`, renamed
  once in `findings_for_run` rather than by a quoted SQL alias. `severity` and `source` both carry
  CHECK constraints.
- **The eight deterministic checks**:

  | Slug | Severity | Population |
  |---|---|---|
  | `orphan-page` | info | `zone='wiki'`, non-exempt type, zero inbound wikilinks — a live `links @> ARRAY[path]` containment check via the GIN index, never the `inlinks` column, which is only as fresh as the last full rebuild |
  | `aging-seed` | warn | `status IN ('seed','developing')` whose `updated` is older than `aging_seed_days` |
  | `stale-view` | warn | every entity `views.staleness.list_stale_entities` names — the ONE check whose `suggested_action` is a pasteable command |
  | `anchor-concentration` | warn | the last `concentration_window` filings; fires when the top entity's share exceeds `concentration_share`. Ties broken alphabetically, so two runs over one window agree |
  | `dead-vocabulary` | info | registered entities with zero anchored pages |
  | `company-wide-fraction` | warn | the last `company_window` filings; fires when the `entity: []` share exceeds `company_share`. No subject — a corpus-wide fact |
  | `company-page-names-entity` | warn | every company-wide, non-provenance `wiki/` page whose body names a registered entity's name, id or alias verbatim. The whole corpus, not a window |
  | `date-bearing-body-link` | warn | any page in `wiki/`, `sources/` or `views/` whose BODY prose wikilinks a `YYYY-MM-DD-…` stem |

- **The four model checks** — `model-contradiction`, `model-anchor-fit`, `model-unlinked-mention`,
  `model-superseded-canon`; all `warn`, all `source='model'` with `model_id` recorded.
- **`GardenerSettings`** — `aging_seed_days` (30), `concentration_window` (30),
  `concentration_share` (0.6), `company_window` (20), `company_share` (0.3), `digest_channel_id`
  (empty is honest — most runs never touch Slack), `model` (`STIGMERGY_GARDENER_MODEL`, default
  `gpt-5.6-luna` — a concrete cheap-class default rather than deferring to the shared `CLEAN_MODEL`,
  so the sweep never silently rides whatever that happens to be), `sweep_sample` (10).
- **`job_runs.status`** — `'ok'`, `'partial'` (the sweep failed, the deterministic findings did not)
  or `'error'` (the run aborted). `stats` carries `pages_checked`, `entities_checked`,
  `filing_population_exclusions`, `age_population_exclusions`, `findings_total`,
  `findings_by_check`, `findings_by_severity`, and a `sweep` sub-object.
- **`stats['sweep']['selected_at']`** is the sweep's watermark, captured immediately before
  `select_pages` runs — never `job_runs.started_at`, written after the model call and the
  deterministic commit. A page filed between the two would otherwise fall in NO sweep window ever.
  On a sweep failure the rotation offset is reset to the value the run STARTED from, so a
  `job_runs` row never claims a rotation advanced past pages nothing swept.
- **Every exclusion is counted, never silently dropped.** `_recent_filed_pages` counts
  `unparsed_result_ref`, `page_not_indexed` and `provenance_excluded`; `_age_query` counts
  `malformed_updated`; `select_pages` counts `unparsed_result_ref` and `changed_page_not_indexed`.
  All surface in `job_runs.stats`.
- **The malformed-date guard** is `CASE WHEN pg_input_is_valid(updated, 'date') THEN … ELSE false
  END`, not a regex before a cast. Two reasons, both learned the hard way: a regex guards the SHAPE,
  so `updated: "2026-02-30"` passes it and still raises on the cast, aborting the whole run daily;
  and PostgreSQL does not guarantee left-to-right evaluation of ANDed quals, so "the regex runs
  first" was never a rule, only the shape one plan happened to pick. `CASE` is the documented
  mechanism for conditional evaluation.
- **`_first_verbatim_match` uses lookarounds, never `\b`.** A trailing `\b` after a spelling ending
  in punctuation (`"Beta Robotics, Inc."`) requires the NEXT character to be a word character — in
  prose that is almost never true, so the control appeared to work per alias and silently never
  fired for any such alias.
- **`SWEEP_LIMITS`** — `request_limit=3, tool_calls_limit=0`. Zero tools is a STRUCTURAL property of
  the agent's usage limits, never a request made in a prompt: the model has no way to call anything.
  One retry carrying the validation error as its brief, then `SweepGarbage`.
- **`_validate` does not re-check the slug enum at the pydantic level** on purpose:
  `SweepFindingSpec.check` is a bare `str`, so an out-of-vocabulary slug becomes a NAMED rejection
  reason a reader can see in `skip_reasons`, rather than a schema error the model may not recover
  from cleanly on retry.

## Tests

`tests/gardener/` — 13 modules plus `conftest.py` and `support.py`, ~3,000 lines.

| Suite | Covers |
|---|---|
| `test_checks_corpus_pg.py` | the corpus-shaped checks against real Postgres |
| `test_checks_filings_pg.py` | the two window/share checks and the shared filings population, including its exclusion counters, plus `company-page-names-entity` (which shares their provenance exclusion but no window) |
| `test_checks_dossiers.py` | the three file-based checks that read the repo checkout — `stale-view`, `dead-vocabulary`, `date-bearing-body-link` — no Postgres, no `conn` fixture |
| `test_sweep.py` | prompt construction and fencing, validation and its rejection reasons, the retry, `SweepGarbage`, `to_finding`, and the offline double |
| `test_sweep_pg.py` | page selection: the watermark, the changed set, the rotating sample and its offset |
| `test_store_pg.py` | insert/read-back and `latest_completed_run`'s `('ok','partial')` widening |
| `test_run_pg.py` | the largest — orchestration, the one transaction, `'partial'` on sweep failure, the notice path, the honest error row |
| `test_notice.py` / `test_notice_pg.py` | composition, and the ACL scoping/redaction |
| `test_report.py` | the terminal report and `--json`, including `_source_tag` |
| `test_settings.py` | every threshold's validation and its refusal message |
| `test_cli.py` | exit codes, refusals, `--json` |
| `test_stale_view_command_pg.py` | that the one runnable `suggested_action` names a command that actually exists |

`tests/test_architecture.py` carries the layering edges, the git-plumbing and `wiki/`-path proofs,
the threshold-literal scan, the `ACL_REACHABILITY_EXCEPTIONS` entry for this package's two direct
`pages_index` readers (`checks.py` and `sweep.py` — an operator tool with terminal output and no
caller identity to scope to), and the transitive-reach pin for `views.staleness`.

## Common tasks

| Task | Touch |
|---|---|
| Add a deterministic check | a slug constant + an entry in `ALL_CHECK_SLUGS`, a `check_*` function returning `build_finding` dicts, a call in `run._run_all_checks`, and a threshold in `settings.py` if it needs one |
| Add a model check | a slug in `sweep.ALL_MODEL_CHECK_SLUGS`, a severity in `MODEL_CHECK_SEVERITY`, a STATIC entry in `MODEL_SUGGESTED_ACTIONS`, and a bullet in `SWEEP_SYS` |
| Change a threshold's default | `settings.py`'s `DEFAULT_*` beside its `*_ENV` — never a literal at the call site |
| Change the report's wording | `report.py` only |
| Change the notice's wording | `notice.py` only — never `slack.copy` |
| Make a check produce an `sla` finding | note it would be the FIRST one. It must also set `_notice_page_paths` for the scoping path to protect it, and `_notice_detail` / `_notice_action` for the notice to say anything useful |
| Add an exclusion to a population | count it into the check's `population_stats` sink — never drop it silently |

## Notes

- **`report.NUM_DETERMINISTIC_CHECKS` computes the count from `ALL_CHECK_SLUGS`**, never a literal,
  so the printed report cannot disagree with the checks that actually ran. A new check needs no
  edit to the report's wording at all — which is the property to preserve when adding one.
- **`cli._connect` ensures three schemas** (capture, review, gardener) in the same order
  `digest/cli.py` does. A narrower earlier version ensured two, which was invisible over a mature
  database — every test fixture ensures all of them — and fatal on a fresh one, on the very first
  real run.
- **The gardener has no caller identity**, which is why its terminal report needs no ACL scoping and
  why it is an `ACL_REACHABILITY_EXCEPTIONS` entry. The SLA notice is the exception that proves the
  rule: the moment this package acquired a BROADCAST surface it also acquired the scoping
  requirement, and the mechanical guard could not see that on its own, because the notice reads
  findings rows and never `pages_index`.
- **A notice failure never withholds the report.** The findings are committed before the notice is
  attempted, so a missing token, an unset channel, a `SlackApiError` or an unresolvable channels
  file is captured into `RunResult.notice_error` and printed to stderr — the run's exit code goes
  nonzero, the report still prints in full.
