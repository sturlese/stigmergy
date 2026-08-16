# capture — the fast lane's front half

Narrative doc: [`docs/reference/capture.md`](../../../docs/reference/capture.md); the meeting
drop CLI's own is [`docs/reference/meeting-distiller.md`](../../../docs/reference/meeting-distiller.md).
This file is the code map — for whoever is about to edit this package, not run it.

## Purpose

Owns the durable queue a capture lands in, the exactly-once claim primitive the librarian drains
it with, the content-addressed evidence archive, the operational spine (`job_runs` /
`ingest_errors`), retention, the latency instrument, and the human loop's two write surfaces: a
submitter's reply (`record_reply`) and a steward's three dispositions. It stores every fact a
capture's journey produces; it never decides what the material MEANS — that is `librarian`'s.

**The queue is durable; the index is disposable.** `stigmergy-index --rebuild` drops
`pages_index` by name; `schema.DURABLE_TABLES` names what must survive it.

## Modules

| Module | Owns |
|---|---|
| `schema.py` | The contract: idempotent DDL and `startup_ddl_lock`; the status enum and its named CHECK; the status sets (`TERMINAL_STATUSES` / `PARKED_STATUSES` / `FINISHED_STATUSES` / `GATE_NOT_YET_RUN_STATUSES`); the pure submission and reply contracts (`prepare_submission`, `prepare_reply`); the hint allowlists and `normalize_hints`; the three `reject_*` refusals; the rejection reason codes and `withheld_reason`; the situation and trace-event vocabularies; `base_report` / `SEARCHABILITY_NOTE`; `validate_meeting_date` |
| `queue.py` | Insert · claim (`FOR UPDATE SKIP LOCKED`) · `release_expired` · `finish` (the `attempts` fence) · `holds_lease` · the two exits from a park (`record_reply`, `dispose`) · the one listing query and its two entry points · `get_submission_trace` · `query_in_flight` · `counts_by_status` · the two latency sample readers |
| `dispositions.py` | The steward's three intents over `queue.dispose` (`requeue` / `resolve` / `reject`), their two report sentences, and `clean` — the seam every steward-typed string crosses |
| `decisions.py` | The append-only `review_decisions` ledger — its DDL, its one write (`record_decision`), its one read (`latest_decisions`) and the two closed vocabularies: the verdicts, and `DECISION_SOURCES` (which DOOR recorded — `mcp`/`slack`/`admin`/`cli`, required on every write, stamped into `extra` and raised on if unknown, because an append-only row cannot be respelled later). **Not `dispositions.py`**: that moves a capture between queue STATES and is about the material; this records a governance DECISION about an identity and changes no row's status. It lives here because of who has to WRITE it — all three minting doors, one of which (`stigmergy-entities`) may not import `stigmergy.server`, where it used to live (ADR 030's amendment) |
| `evidence.py` | The content-addressed store: `content_key` (pure), `S3EvidenceStore` (MinIO/R2, lazy boto3 client), `MemoryEvidenceStore` (the offline double), `store_from_env`, and the drop doors' pre-flight (`split_stores_reason` / `is_loopback_host`) |
| `ops.py` | `record_job_run`, `record_ingest_error`, the `job_run` context manager, and the written-down `job_runs.status` spec (`ok` / `error` / `partial`) |
| `retention.py` | `purge` — payload/hints/outcome deletion on terminal rows past the window, plus the age-independent secret/PII reconciliation; `purge_secret_capture_immediately` |
| `latency.py` | `percentile`, `LatencySummary`, `summarize`, `render` — capture→filed p50/p95, refusing to answer below `MIN_SAMPLES` |
| `render.py` | The operator dialect every CLI prints in, and the home of these renderings: `depth_line`, `format_ms`, `format_age`, `clean_for_terminal`, `RECLAIM_NOW`. Below the CLIs because `latency.py` needs `format_ms` and `server.pilot_report` imports that; reaches nothing but `stigmergy.text` |
| `cli.py` | `stigmergy-queue` (list · show · claim · reclaim · requeue · resolve · reject · purge); `render.py`'s names re-exported for the CLIs that already import them from here; the drop CLIs' shared pre-flight and runner (`refuse_split_stores` / `add_split_stores_flag` / `EXIT_SPLIT_STORES`, `connect`, `resolve_submitted_by` / `add_submitted_by_flag` / `OPERATOR_EMAIL_ENV`, `drop_main` / `drop_interrupted`) |
| `meeting_cli.py` | `stigmergy-meeting drop` — the only door onto the meeting flow |
| `drive_cli.py` | `stigmergy-drive drop` — the only door onto the drive flow: fetches with the operator's own Google auth, uploads the original bytes as `blob_refs[1]`, submits a deterministic manifest as the row's material; no model, no conversion |
| `drive_client.py` | The Drive fetch seam: `GogDriveClient` (a `gog` subprocess), `DriveFile`, `file_id_from`. Never touches Postgres; tests inject a fake |
| `errors.py` | `CaptureError` and its four subclasses, with the which-messages-may-cross-the-network rule |

## Use these

- **`queue.claim_next` / `queue.finish(..., expected_attempts=...)`** — the lease and its fence.
  Never reimplement either.
- **`queue.holds_lease`** — the fence's question asked early, immediately before an irreversible
  step (a commit and push).
- **`queue.dispose` / `queue.record_reply`** — the only two ways a row leaves a park outside the
  worker. Do not call `dispose` from a new surface: name a business intent in `dispositions.py`.
- **`schema.prepare_submission` / `schema.prepare_reply`** — the pure, DB-free contracts every
  capture surface validates through; the seam every caller of `queue.submit` crosses, which is
  why the meeting/drive requirements live there and not only in the drop CLIs.
- **`schema.ALLOWED_HINT_KEYS`** — extend by adding a new small, string-valued allowlist, never
  by widening the meaning of an existing key. A hint a downstream reader will TRUST also gets a
  `reject_*` refusal at the client-reachable seam (`BrainService._submit`), mirroring
  `reject_source_provenance_hints` — never inside `normalize_hints`.
- **`queue.query_submissions`** — the ONE listing query (scope, filter, paging, the withheld rule
  in Postgres). New surfaces attach through `list_own_submissions` / `list_all_submissions`,
  never a second `SELECT ... FROM capture_queue`.
- **`schema.withheld_reason`** — the ONE function turning "is this material withheld, and why"
  into a sentence; both read paths call it.
- **`dispositions.clean`** — the ONE seam a steward's `--note` / `--reason` crosses.
- **`schema.startup_ddl_lock`** — any new place that runs DDL against this database takes the
  same lock; `IF NOT EXISTS` is a check, not a lock.
- **`evidence.content_key` / `evidence.MemoryEvidenceStore`** — the pure key scheme and the
  offline double.
- **`render.depth_line` / `format_ms` / `format_age` / `clean_for_terminal` / `RECLAIM_NOW`** —
  imported by other CLIs so two tools in one terminal print one dialect. `cli.py` re-exports them
  under the same names; new callers take them from `render`, which no connection seam hangs off.
- **`cli.drop_main` / `drop_interrupted` / `resolve_submitted_by` / `add_submitted_by_flag` /
  `connect`** — a new drop door rides these; the two that exist share every sentence and every
  exit code through them.

## Avoid

- **Never take `submitted_by` — or any `ATTRIBUTION_FIELDS` member — from client input.** It
  comes from the resolved identity or the submission does not happen. A steward's `--by` is
  attribution, not authorization.
- **Never open a second write path.** Every write rides `queue.submit` / `finish` / `dispose` /
  `record_reply`.
- **Never import `stigmergy.server`, `stigmergy.answer`, `stigmergy.librarian` or
  `stigmergy.entities`** (pinned by `tests/test_architecture.py`). Only the three operator CLIs
  (`cli.py`, `meeting_cli.py`, `drive_cli.py`) may reach `stigmergy.index` (for `store.connect` /
  `store.dsn`), open a connection, or read the environment — library code takes `conn` as an
  argument, and nothing touches global state at module scope.
- **Never widen `MCP_SUBMIT_KINDS` to match `KINDS` without deciding to** — that is exactly how
  a drop-CLI-only flow becomes enqueueable through `brain_submit`.
- **Never compose a submitter-facing sentence outside `dispositions.py` or `librarian.report`.**
  The shared shape lives in `schema.py`; the wording belongs to whichever package authored the
  outcome.
- **Never add a non-additive DDL statement without reading `_CAPTURE_QUEUE_STATUS_CHECK`'s
  comment** — the reasoning is atomicity and locks, not idempotence.
- **Never give `queue.release_expired` a default `visibility_timeout_s`.** A sweeper states how
  dead someone else's worker must be, a fact this package cannot see.
- **Never assume a `triage` row is generic** — only rows carrying `schema.SITUATION_KEY` are
  identity questions `stigmergy-entities` can act on.
- **Never name the parked-name keys outside their three declared homes.**
  `schema.SITUATION_NAMES_KEY` is what an `unresolved-entity` park writes — a list, whatever the
  count — and `schema.SITUATION_NAME_KEY` is READ-ONLY legacy: nothing writes it, and rows already
  in the queue that carry it are never migrated, which is why `entities.situations.subjects_of`
  keeps its fallback permanently rather than as a transition to finish. Defined here, written by
  `librarian.report.triage_entity`, read by `entities.situations`, and nowhere else —
  `tests/test_architecture.py` enforces that with a named exception list and a pruning test.
  They are a wire format in a JSONB column with no schema behind them, so a fourth module deciding
  what they mean is caught by nothing else.
- **Never echo captured material — or a reply — unfenced.** A new read surface attaches to
  `query_submissions`'s shaping, which carries the withheld rule, not to its own `SELECT`.

## Notes

- `capture` is a store everyone who interprets its rows imports (`server`, `librarian`,
  `entities`, `admin`, `slack.store`, `gardener`, `digest`, `views.regenerate`), never the
  reverse. The only crossings are downward: other packages' CLIs import `render.py`'s dialect —
  `stigmergy-entities` straight from `render`, `stigmergy-librarian` through `cli.py`'s re-export.
- Write order is deliberate and asymmetric: validate → evidence blob → queue row. An orphan blob
  is inert and content-addressed; a row pointing at an unwritten blob is a submission nothing
  can read. A refusal writes neither.
- `attempts` counts deliveries, not failures: incremented on claim, never on release, so a
  poison item cannot be redelivered forever.
- `purge_secret_capture_immediately` is NOT atomic with the rejection that triggers it (two
  autocommit statements); `purge`'s age-independent secret/PII clause is the nightly reconciler.
