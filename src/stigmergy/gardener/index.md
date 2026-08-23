# gardener — corpus health on demand

Narrative doc: [`docs/reference/gardener.md`](../../../docs/reference/gardener.md).
Sibling that reads the findings store and never recomputes a check:
[`admin`](../admin/index.md).

`stigmergy-gardener` runs every deterministic check `checks.ALL_CHECK_SLUGS` names and emits
**findings only**, persisted to `gardener_findings` with a `job_runs` row and printed as a
severity-grouped report. It fixes nothing, writes nothing, vetoes nothing: no git plumbing, no
literal path under `wiki/`, both pinned by `tests/test_architecture.py`, which also pins every
import edge and the threshold-literal ban.

**It asks no model.** The three model passes this package used to run — an editorial sweep over
changed-plus-sampled pages, an empty-body pass over the entity zone, an identity pass over the
registry behind it — were retired: over three weeks of daily use they produced nine findings
between them, and three of the four slugs behind a repair road produced none, ever. So there is no
model name here, no budget, no key, and no third run outcome between "completed" and "raised".

## Modules

| Module | What it is |
|---|---|
| `cli.py` | `stigmergy-gardener [--repo] [--dsn] [--json]` — one command. The only module here that imports `stigmergy.index.store` or `stigmergy.librarian.config` |
| `run.py` | `run_gardener` — the one function the CLI calls: run every check, persist findings + a `job_runs` row in ONE transaction, re-fetch. Owns `RunResult`. Synchronous: nothing in a run awaits anything |
| `checks.py` | Every deterministic check and `ALL_CHECK_SLUGS`, the one list of their slugs — how many there are is pinned against that tuple by `tests/test_readme_claims.py` and `tests/test_docs_claims.py`, so a new check moves the documents that COUNT them and needs no number here. Also `build_finding` (the one finding-dict assembler), `count_indexed_pages`, `_recent_filed_pages`, and `entity_zone_pages`/`placeholder_lines` — the confinement-checked walk of the entity zone (run ONCE per run by `run.py`) and the one spelling of "still its template" |
| `store.py` | `gardener_findings` persistence: `insert_findings`, `findings_for_run`, `latest_completed_run` |
| `report.py` | The terminal report: `render_report`, `render_json`. Pure text from plain data |
| `schema.py` | The DDL behind `startup_ddl_lock`, `JOB_NAME`, the severity/source vocabularies, `MAX_DETAIL_CHARS` |
| `settings.py` | `GardenerSettings.from_args` — the five thresholds the checks measure against. Also declares `int_setting`, the count-shaped validator every count here funnels through |
| `errors.py` | `GardenerError` — a precondition on running the tool at all |

Downstream: `admin` imports `store`/`schema`. Nothing else imports this package.

## Reuse

- `checks.build_finding` — the one finding assembler; `store.py` persists the named columns only.
  `subjects` is derived from `subject` unless a caller passes the list it already has.
- `store.latest_completed_run` — `status IN ('ok','partial')`. `'partial'` is HISTORICAL: it meant
  a model pass had failed while the deterministic findings committed anyway, and no run written now
  can be one. The predicate still accepts it so a deployment whose last completed run predates the
  model passes' retirement is not blank until the next nightly pass.
- `index.corpus.load_pages` — the ONE parser for "what does this checkout contain", shared with
  the index build. `check_dead_vocabulary` reads its anchored-entity population straight off it.
- `librarian.page.is_provenance_type` — a provenance page's `entity: []` means "no evidence
  found", never a checked company-wide declaration.
- `stigmergy.text.parse_result_ref` — the one parser for a filed capture's `result_ref`, so this
  package and the librarian cannot disagree about what one means.
- `capture.ops.record_job_run` — called directly, not via the `job_run` context manager: every
  finding needs `run_id` at insert time.

## Avoid

- Never write anything but a finding and a `job_runs` row; a `suggested_action` may NAME a
  command, this package never runs one.
- **Never add a model back without the use to justify it.** The passes that were here are the
  measured case against one: they read pages every night for three weeks and produced nine
  findings, and the three slugs whose findings fed a repair road produced zero. A model pass here
  also costs a provider key on the worker, which is what `bootstrap.READ_PATH_ONLY_ENV` strips —
  the trap that used to need its own preflight refusal.
- Never walk the entity zone inside a check, and never load the registry a second time.
  `run.run_gardener` does each once and hands the results to every consumer; two walks of one zone
  in one run can disagree about what the corpus contained.
- Never follow a symlink out of that walk, and never read a file above `MAX_ENTITY_PAGE_BYTES`.
  What the walk reads does not stay in the process — an excerpt is persisted, printed and rendered
  in the admin console — so a symlinked leaf AND every resolved path component are refused
  (`page.is_inside` + the leaf `islink`, the pair `gather._confined` uses). Every refusal is
  counted into `stats['entity_zone_walk_exclusions']`.
- Never put a threshold literal outside `settings.py` (grep-asserted).
- Never post anything from here. This package holds no Slack edge and no caller identity:
  findings reach a person through the admin console. `report.py` writes terminal markdown, and
  that is the only dialect this package speaks.

## Contracts

- `gardener_findings`: `id`, `run_id`, `check_slug` (Python key `"check"`), `severity`, `source`,
  `subject`, `subjects`, `detail`, `suggested_action`, `created_at`, `model_id`. `subject` is the
  display string and `subjects` the same fact as a LIST — a consumer that acts on the pages reads
  the list, never re-splits the prose. Empty when a finding names none
  (`check_company_wide_fraction` reports a corpus-wide fraction). `source` is `'deterministic'` on
  every row written now; `'model'` and a non-empty `model_id` appear only on rows the retired
  passes wrote, and both stay in the vocabulary so such a row reads back as what it is rather than
  being relabelled.
- Check populations, severities and settings defaults live in `checks.py`/`settings.py` beside
  their code. This package FIXES NOTHING and never has: every check reports, and nothing in the
  system reads a finding and writes a page from it. `entity-placeholder-body` — an entity page
  whose body still carries the template's angle markers, or is blank below its title — is the
  check that used to have a repair kind of its own; today its suggested action names the two
  things a PERSON can do, because an entity page grows from what captures establish about it. The
  severity vocabulary is
  `schema.SEVERITIES` — `info` and `warn`, in `schema.SEVERITY_ORDER` for the report — and every
  reader of it (`report.py`, the console's chips) spells it off those names.
- `job_runs.status`: `'ok'` or `'error'` for a run this package writes — every check is
  deterministic, so a run completes or it raises. `'partial'` is a historical value only (see
  `store.latest_completed_run` above). `stats` carries `pages_checked`/`entities_checked`,
  `findings_total`/`findings_by_check`/`findings_by_severity`, and the exclusion counters
  `filing_population_exclusions`/`age_population_exclusions`/`entity_zone_walk_exclusions` — every
  population exclusion is counted, never silently dropped.

Tests live in `tests/gardener/`; the layering, git-plumbing, `wiki/`-path and threshold proofs in
`tests/test_architecture.py`.
