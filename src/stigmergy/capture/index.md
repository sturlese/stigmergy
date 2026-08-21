# capture — the fast lane's front half

Narrative doc: [`docs/reference/capture.md`](../../../docs/reference/capture.md); the meeting
kind's own flow is [`docs/reference/meeting-distiller.md`](../../../docs/reference/meeting-distiller.md).
This file is the code map — for whoever is about to edit this package, not run it.

## Purpose

Owns the durable queue a capture lands in, the exactly-once claim primitive the librarian drains
it with, the content-addressed evidence archive, the operational spine (`job_runs` /
`ingest_errors`), retention, the latency instrument, and the append-only governance ledger every
door that decides an identity writes to. It stores every fact a capture's journey produces; it
never decides what the material MEANS — that is `librarian`'s.

**Nothing here waits on a person.** A row is `queued`, `claimed`, then terminal; the two states a
capture used to park in (`schema.RETIRED_STATUSES`) are gone, along with the dispositions that
drained them. What a human decides now is an IDENTITY, after the filing, through
`stigmergy.entities` — and the only trace of it in this package is a `review_decisions` row.

**The queue is durable; the index is disposable.** `stigmergy-index --rebuild` drops
`pages_index` by name; `schema.DURABLE_TABLES` names what must survive it.

## Modules

| Module | Owns |
|---|---|
| `schema.py` | The contract: idempotent DDL and `startup_ddl_lock`; the status enum, its named CHECK and the two-halved guard that swaps it; `RETIRED_STATUSES` and the startup migration that returns a still-parked row to `queued` with one `trace` event saying why; the status sets (`TERMINAL_STATUSES` / `FINISHED_STATUSES` / `GATE_NOT_YET_RUN_STATUSES`); the pure submission contract (`prepare_submission`); the kind vocabulary every door speaks (`KINDS`) and the per-kind material caps under it (`MATERIAL_CAP_BYTES`, read through `max_material_bytes`; `MAX_MATERIAL_BYTES` is the largest, which is what a transport's body limit derives from); the hint allowlists — placement, the three provenance ones, and `REGISTER_HINT_KEYS`, the four `register_*` keys a steward door submits an entity registration as (`Registration`, `registration_hints`, `registration_from_hints`, `REGISTRATION_SOURCES` = the two doors, `admin` and `cli`) — and `normalize_hints`; the three `reject_*` refusals; the rejection reason codes and `withheld_reason`; `clean_note`; `base_report` / `SEARCHABILITY_NOTE`; `validate_meeting_date` |
| `queue.py` | Insert · claim (`FOR UPDATE SKIP LOCKED`) · `release_expired` · `finish` (the `attempts` fence, and the ONLY transition out of a claim) · `holds_lease` · the one listing query and its two entry points · `get_submission_trace` · `query_in_flight` · `work_waiting` (is anything QUEUED right now — one bit, for the worker's view sweep deciding whether to yield the loop back) · `counts_by_status` · `outcomes_by_day` · the two latency sample readers |
| `decisions.py` | The append-only `review_decisions` ledger — its DDL, its one write (`record_decision`), its three reads (`latest_decisions` for every item at once, `latest_decision_for` for one, `recent_decisions` for the newest rows as a bounded feed — every decision, not the latest per item) and the two closed vocabularies: the verdicts (`approve` / `reject` / `merge` / `request_changes`, with `extra["into"]` naming the survivor on a merge), and `DECISION_SOURCES` (which DOOR recorded — `mcp`/`slack`/`admin`/`cli`, required on every write and raised on if unknown, because an append-only row cannot be respelled later). It is stamped into `extra` LAST, so `source` is authoritative: a caller cannot override the validated value through `extra`. It records a governance DECISION about an identity and changes no row's status — the two tables in this package answer different questions and share no state. It lives here because of who has to WRITE it — all four deciding doors, one of which (`stigmergy-entities`) may not import `stigmergy.server`, where it used to live (ADR 030's amendment). **It is also the librarian's decline memory**: `librarian.processing` reads it so an identity a steward declined is never proposed again |
| `evidence.py` | The content-addressed store: `content_key` (pure), `S3EvidenceStore` (MinIO/R2, lazy boto3 client), `MemoryEvidenceStore` (the offline double), `store_from_env`, and the enqueueing CLI's pre-flight (`split_stores_reason` / `is_loopback_host`) |
| `ops.py` | `record_job_run`, `record_ingest_error`, the `job_run` context manager, the written-down `job_runs.status` spec (`ok` / `error` / `partial`), and `try_advisory_lock` — the NON-blocking mutual exclusion a maintenance pass takes so a loser can skip and say so (`views.regenerate.sweep`); `schema.startup_ddl_lock` is the blocking sibling, and each caller owns its own key |
| `retention.py` | `purge` — payload/hints/outcome deletion on terminal rows past the window, plus the age-independent secret/PII reconciliation; `purge_secret_capture_immediately`. `outcome` is a legacy column nothing writes any more, so this is now its only eraser |
| `latency.py` | `percentile`, `LatencySummary`, `summarize`, `render` — capture→filed p50/p95, refusing to answer below `MIN_SAMPLES` |
| `render.py` | The operator dialect every CLI prints in, and the home of these renderings: `depth_line`, `format_ms`, `format_age`, `clean_for_terminal`, `RECLAIM_NOW`. Below the CLIs because `latency.py` needs `format_ms` and `server.pilot_report` imports that; reaches nothing but `stigmergy.text` |
| `cli.py` | `stigmergy-queue` (list · show · claim · reclaim · purge — five subcommands, none of which moves a row on a person's behalf); `render.py`'s names re-exported for the CLIs that already import them from here; the split-stores guard the one CLI that still enqueues runs first (`refuse_split_stores` / `add_split_stores_flag` / `EXIT_SPLIT_STORES`), `connect`, and `WORKER_DEFAULT_LEASE_S` — the worker's derived lease, duplicated here (the reverse import would be a cycle) and pinned to `librarian.config.DEFAULT_VISIBILITY_TIMEOUT_S` by `tests/capture/test_cli.py` |
| `errors.py` | `CaptureError` and its three subclasses (`SubmissionRejected`, `EvidenceError`, `QueueStateError`), with the which-messages-may-cross-the-network rule |

## Use these

- **`queue.claim_next` / `queue.finish(..., expected_attempts=...)`** — the lease and its fence.
  Never reimplement either.
- **`queue.holds_lease`** — the fence's question asked early, immediately before an irreversible
  step (a commit and push).
- **`schema.prepare_submission`** — the pure, DB-free contract every
  capture surface validates through; the seam every caller of `queue.submit` crosses, which is
  why a `meeting`'s and a `document`'s required hints live there: below every door, so none can
  skip them.
- **`schema.ALLOWED_HINT_KEYS`** — extend by adding a new small, string-valued allowlist, never
  by widening the meaning of an existing key. A hint a downstream reader will TRUST also gets a
  `reject_*` refusal at the client-reachable seam (`BrainService._submit`), mirroring
  `reject_source_provenance_hints` — never inside `normalize_hints`.
- **`schema.registration_hints` / `registration_from_hints`** — the ONE way an entity registration
  is written into a capture's hints and the ONE way it is read back. Both steward doors (the admin
  console's Register an entity, `stigmergy-entities create`) build them here so they cannot describe
  a registration differently, and `reject_registration_hints` refuses all four from every
  client-reachable door: a registration is an act of authority, and `brain_submit` attributes
  material, never authority.
- **`queue.query_submissions`** — the ONE listing query (scope, filter, paging, the withheld rule
  in Postgres). New surfaces attach through `list_own_submissions` / `list_all_submissions`,
  never a second `SELECT ... FROM capture_queue`.
- **`schema.withheld_reason`** — the ONE function turning "is this material withheld, and why"
  into a sentence; both read paths call it.
- **`schema.clean_note`** — the ONE seam an operator-typed string crosses on its way into a ledger
  row or a report: control characters stripped, newlines flattened, clipped word-safe. It is BELOW
  every surface deliberately — one CLI once cleaned its note while its sibling passed it raw.
- **`decisions.record_decision` / `latest_decisions` / `latest_decision_for` /
  `recent_decisions`** — the governance ledger. A new door that decides an identity writes through
  `record_decision` with its own `source`, and never a second INSERT.
- **`schema.startup_ddl_lock`** — any new place that runs DDL against this database takes the
  same lock; `IF NOT EXISTS` is a check, not a lock.
- **`evidence.content_key` / `evidence.MemoryEvidenceStore`** — the pure key scheme and the
  offline double.
- **`render.depth_line` / `format_ms` / `format_age` / `clean_for_terminal` / `RECLAIM_NOW`** —
  imported by other CLIs so two tools in one terminal print one dialect. `cli.py` re-exports them
  under the same names; new callers take them from `render`, which no connection seam hangs off.
- **`cli.refuse_split_stores` / `add_split_stores_flag` / `connect`** — a surface that enqueues
  from an operator's terminal rides these: the same refusal, the same flag, the same exit code as
  `stigmergy-entities create`.

## Avoid

- **Never take `submitted_by` — or any `ATTRIBUTION_FIELDS` member — from client input.** It
  comes from the resolved identity or the submission does not happen. A steward's `--by` is
  attribution, not authorization.
- **Never open a second write path.** Every capture write rides `queue.submit` / `claim_next` /
  `release_expired` / `finish`, and there is no transition a person performs on a row.
- **Never reintroduce a state that waits on somebody.** `RETIRED_STATUSES` and the startup
  migration exist so the two old words cannot come back; a decision that needs a human is a
  decision about an identity, and it belongs in `stigmergy.entities` with a `review_decisions` row.
- **Never import `stigmergy.server`, `stigmergy.answer`, `stigmergy.librarian` or
  `stigmergy.entities`** (pinned by `tests/test_architecture.py`). Only `cli.py` may reach
  `stigmergy.index` (for `store.connect` / `store.dsn`), open a connection, or read the
  environment — library code takes `conn` as an argument, and nothing touches global state at
  module scope.
- **Never give a door a kind of its own.** `KINDS` is the one vocabulary every door speaks
  (`raw`, `page`, `meeting`, `document`), and what a kind requires is enforced at
  `prepare_submission`, never at one door
  ([ADR 044](../../../docs/decisions/044-the-capture-is-the-approval.md) D4).
- **Never compose a submitter-facing sentence outside `librarian.report`.**
  The shared shape lives in `schema.py`; the wording belongs to the package that authored the
  outcome.
- **Never add a non-additive DDL statement without reading `_CAPTURE_QUEUE_STATUS_CHECK`'s
  comment** — the reasoning is atomicity and locks, not idempotence.
- **Never give `queue.release_expired` a default `visibility_timeout_s`.** A sweeper states how
  dead someone else's worker must be, a fact this package cannot see.
- **Never write a legacy column.** `asked_at`, `parked_at`, `reply` and `outcome` are still CREATED
  so a reader of an old row finds what it expects, and `trace` takes exactly one further event per
  row the startup migration moves. Writing any of them again would mean the parked states came
  back in everything but name.
- **Never echo captured material unfenced.** A new read surface attaches to
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
- The status CHECK's guard asks BOTH halves — every current status present AND no retired one — so
  a constraint still admitting `needs_input` can never read as up to date. The parked-row migration
  runs BEFORE that swap, or the swap would refuse to constrain a table holding a word it no longer
  lists.
