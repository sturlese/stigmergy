# capture — the fast lane's front half

Narrative doc: [`docs/reference/capture.md`](../../../docs/reference/capture.md) (the how and why
for an operator and a submitter); the meeting drop CLI's own how and why is
[`docs/reference/meeting-distiller.md`](../../../docs/reference/meeting-distiller.md).
Design records: [ADR 014](../../../docs/decisions/014-capture-queue-and-attribution.md) (the queue,
attribution, the evidence plane), [ADR 016](../../../docs/decisions/016-human-loop-and-entity-governance.md)
(the disposition states), [ADR 018](../../../docs/decisions/018-pilot-readiness.md) (the echo-window
ruling), [ADR 020](../../../docs/decisions/020-meeting-distiller.md) (the drop CLI as the only door,
`kind` as the material's shape and the flow that reads it).

This package is largely untouched by the purge ([ADR 026](../../../docs/decisions/026-the-purge.md))
and the contraction ([ADR 027](../../../docs/decisions/027-the-contraction.md)) — the queue,
attribution and the evidence plane predate the organs both removed — but its vocabulary shrank with
them: `REJECTION_REASONS` lost `untraced-figure` with ingest-time figure verification, and the
learning loop's four promotion hints (`LOOP_HINT_KEYS`) and their forge refusal went with
`stigmergy.loop`. What replaced that refusal in shape, not in purpose, is
`reject_source_provenance_hints`.

This file is the code map — for whoever is about to edit this package, not run it.

## Purpose

Owns the durable queue a capture lands in, the exactly-once claim primitive the librarian drains it
with, the content-addressed evidence archive, the operational spine (`job_runs` / `ingest_errors`),
retention, the latency instrument, and the human loop's two write surfaces: a submitter's answer to
the librarian's one question (`record_reply`), and a steward's three ways to close a parked row
(`dispositions.requeue` / `resolve` / `reject`).

It stores every fact a capture's journey produces; it never decides what a capture's material
MEANS — that is `librarian`'s, and for identity questions `entities`'.

**The queue is durable; the index is disposable.** `stigmergy-index --rebuild` drops `pages_index` by
name; `schema.DURABLE_TABLES` names what must survive it.

## Key entry points

| Module | Owns |
|---|---|
| `schema.py` | The contract: the idempotent DDL and `startup_ddl_lock`; the 8-status enum and its named CHECK constraint; `TERMINAL_STATUSES` / `PARKED_STATUSES` / `FINISHED_STATUSES` / `GATE_NOT_YET_RUN_STATUSES`; the pure submission contract (`prepare_submission`, `Submission`, `material_digest`); the reply contract (`prepare_reply`, `MAX_REPLY_CHARS`); the hints allowlists and `normalize_hints`; the three refusals (`reject_server_owned_arguments`, `reject_source_provenance_hints`, `reject_drive_provenance_hints`); the rejection reason codes and the three withheld sentences behind `withheld_reason`; the situation vocabulary; the trace event vocabulary; `base_report` / `SEARCHABILITY_NOTE`; the meeting-date validator |
| `queue.py` | Insert · claim (`FOR UPDATE SKIP LOCKED`) · `release_expired` · `finish` (the `attempts` fence) · `holds_lease` · the two exits from a park (`record_reply`, `dispose`) · the one listing query and its two semantic entry points · `get_submission_trace` · `query_in_flight` · `counts_by_status` · the two latency sample readers |
| `dispositions.py` | The steward's three business intents over `queue.dispose` — `requeue` / `resolve` / `reject` — the two report sentences they put in front of a submitter (`resolved_report`, `rejected_report`), and `clean`, the seam every steward-typed string crosses |
| `evidence.py` | The content-addressed store: `split_stores_reason` / `is_loopback_host` (the drop doors' pre-flight: a remote queue with a loopback store is a capture the worker can never read);  `content_key` (pure), `S3EvidenceStore` (MinIO/R2, lazy boto3 client), `MemoryEvidenceStore` (the offline double), `store_from_env` |
| `ops.py` | The operational spine: `record_job_run`, `record_ingest_error`, and the `job_run` context manager. Also the one written-down spec for `job_runs.status` (`ok` / `error` / `partial`) |
| `retention.py` | `purge` — payload/hints/outcome deletion on terminal rows past the window, plus the age-independent secret/PII reconciliation clause; `purge_secret_capture_immediately` — its payload/hints half, right now, for the one reason whose clock is not 30 days |
| `latency.py` | `percentile`, `LatencySummary`, `summarize`, `render` — capture→filed p50/p95 from the trace alone, and the explicit refusal to answer below `MIN_SAMPLES` (10) |
| `cli.py` | `stigmergy-queue` — eight subcommands: `list` · `show` · `claim` · `reclaim` · `requeue` · `resolve` · `reject` · `purge`. Also the three shared renderings other CLIs import: `depth_line`, `format_ms`, `format_age`, plus `RECLAIM_NOW`; and the two drop CLIs' shared pre-flight — `refuse_split_stores` / `add_split_stores_flag` / `EXIT_SPLIT_STORES` (3) — a remote queue paired with a loopback evidence endpoint, refused before any fetch or upload |
| `meeting_cli.py` | `stigmergy-meeting drop <file> --title --date [--attendees] [--submitted-by]` — the only door onto the meeting flow, and the webhook's future target |
| `drive_cli.py` | `stigmergy-drive drop <file-id-or-url> [--submitted-by]` ([ADR 028](../../../docs/decisions/028-drive-door.md)) — the only door onto the drive flow. Fetches with the OPERATOR's own Google auth through `drive_client`, refuses the format policy's rejects BEFORE any byte moves, uploads the ORIGINAL BYTES as `blob_refs[1]` (`extra_blob_refs`) and a deterministic MANIFEST as the row's material (what dedup keys on — `drive_modified` rides in hints only), then submits exactly one `kind="drive"` row. No model, no conversion: the worker converts |
| `drive_client.py` | the Drive fetch seam: `GogDriveClient` (a `gog` subprocess — metadata + download, with a native Google file exported to PDF by Drive itself), `DriveFile`, `file_id_from` (every share-URL shape). Never touches Postgres; tests inject a fake and never run `gog` |
| `errors.py` | `CaptureError` and its four subclasses (`SubmissionRejected`, `ReplyRejected`, `EvidenceError`, `QueueStateError`), with the "which messages may cross the network" rule in the module docstring |

**Who depends on this package** (one-way, always): `server` (the service layer, the review lane, the
webhook, the audit DDL, the pilot report), `librarian` (the worker, processing, gates, report,
config, dedup, the CLI), `entities` (`situations`, and the CLI, which reuses `dispositions` and two
of `cli.py`'s renderings), `admin` (the console's whole drain — `dispositions`, `queue`,
`retention`, `latency` — plus `schema.startup_ddl_lock` for its own table),
`slack.store` (`capture.schema` only — pinned to that one module),
`gardener` (`ops`, `schema.startup_ddl_lock`, the CLI), `digest` (`ops`, `schema`), and
`views.regenerate` (`ops`). Nothing in this package knows any of them exists.

## Use these

- **`queue.claim_next` / `queue.finish(..., expected_attempts=...)`** — the lease and its fence.
  Never reimplement either. `attempts` is monotonic per delivery, so the value handed out at claim
  time names that delivery; without the fence a stalled worker silently overwrites the live one's
  row. This is what makes single-writer serialization a property of the QUEUE rather than of there
  happening to be one worker.
- **`queue.holds_lease`** — the same question the fence asks, asked EARLY, immediately before an
  irreversible step (a commit and push). The fence still stays; this narrows the window from "a
  whole agent run plus the gates" to "one push". It exists because a real capture was filed twice.
- **`queue.dispose` / `queue.record_reply`** — the ONLY two ways a row leaves a park other than the
  librarian's own worker. Both are state-guarded in SQL, not in the caller, and neither touches
  `attempts`. Do not call `dispose` from a new surface: name a business intent in `dispositions.py`
  first, the way `stigmergy-entities` does.
- **`schema.prepare_submission` / `schema.prepare_reply`** — the two pure, DB-free contracts. A new
  capture surface validates through these, never through its own copy of the rules. Note that
  `prepare_submission` is the seam EVERY caller of `queue.submit` passes through, which is why the
  meeting requirements live there and not only in the drop CLI.
- **`schema.ALLOWED_HINT_KEYS`** — the union the three small allowlists compose into. Extending it
  means adding a new small, string-valued allowlist beside the existing three, never widening the
  meaning of the original four.
- **`schema.reject_source_provenance_hints(hints, door=...)`** — the pattern for a hint a downstream
  reader TRUSTS rather than merely reads as a suggestion: refuse it at the one seam a CLIENT can
  reach (`BrainService._submit`), while leaving `normalize_hints` / `queue.submit` open for the one
  legitimate internal caller (here, the Slack transport, whose hints come from Slack's own API).
- **`queue.query_submissions`** — the ONE listing query (scope, status filter, ordering, paging, and
  the withheld-material rule evaluated in Postgres). New surfaces attach through
  `list_own_submissions` / `list_all_submissions`, or filter its output in Python the way
  `entities.situations.list_pending_situations` does — never a second `SELECT ... FROM capture_queue`.
- **`schema.withheld_reason`** — the ONE function that turns "is this row's material withheld, and
  why" into a sentence. Both `_shape_listed` and `get_submission_trace` call it, which is what
  closed a real drift where the same rule was decided in two places.
- **`dispositions.clean`** — the ONE seam a steward's `--note` / `--reason` crosses. It sits BELOW
  the CLIs on purpose; the defect that produced it was one CLI cleaning its `--reason` and the other
  not, so ANSI escapes reached a submitter's terminal.
- **`schema.startup_ddl_lock`** — the whole-database DDL critical section. Any new place that runs
  DDL against this database takes the same lock; `IF NOT EXISTS` is a check, not a lock, and does
  not make concurrent creation safe.
- **`evidence.content_key`** / **`evidence.MemoryEvidenceStore`** — the pure key scheme, and the
  offline double so a test needs no bucket.
- **`cli.depth_line` / `cli.format_ms` / `cli.format_age` / `cli.RECLAIM_NOW`** — imported by
  `stigmergy-librarian` and `stigmergy-entities` rather than retyped, so two tools in one operator's
  terminal print the same facts in the same words.

## Avoid / anti-patterns

- **Never take `submitted_by` — or any of `ATTRIBUTION_FIELDS` — from client input.** It comes from
  the resolved identity or the submission does not happen. The same holds for a steward's `--by`: it
  is attribution, not authorization (recorded, never checked), but it is still never taken from what
  a capture's own material says.
- **Never open a second write path.** Every write rides `queue.submit` / `queue.finish` /
  `queue.dispose` / `queue.record_reply`, which is what gives rate limiting, the audit row and the
  trace to every caller.
- **Never let this package import `stigmergy.server`, `stigmergy.answer`, `stigmergy.librarian` or
  `stigmergy.entities`.** Pinned by `tests/test_architecture.py`. Only the three operator CLIs —
  `cli.py`, `meeting_cli.py` and `drive_cli.py` — may reach `stigmergy.index` (for `store.connect` /
  `store.dsn`), and only they may open a connection
  or read the environment — every library function here takes `conn` as an argument. A fourth
  exemption is a design change, not a convenience; `drive_client.py` stays outside it deliberately,
  because it talks to `gog` and never to Postgres.
- **Never touch `os.environ` or call `.connect()` at MODULE scope**, in any file here including the
  CLIs. Pinned by `test_capture_library_modules_touch_no_global_state_at_module_scope`.
- **Never compose a submitter-facing sentence outside `dispositions.py` (a steward's action) or
  `librarian.report` (the fast lane's own account).** The shared SHAPE (`base_report`,
  `SEARCHABILITY_NOTE`) lives in `schema.py` because the column that stores it does; the wording
  belongs to whichever package authored the outcome.
- **Never widen `MCP_SUBMIT_KINDS` to match `KINDS` without deciding to.** `KINDS` gaining a
  flow-routing value does not make that value safe for a model-facing transport — that is exactly
  how `meeting` briefly became enqueueable through `brain_submit` beside the drop CLI's declared
  "only door".
- **Never add a non-additive DDL statement without reading `_CAPTURE_QUEUE_STATUS_CHECK`'s comment
  first.** Its `DO` block is one statement, therefore one transaction under autocommit. The previous
  DROP-then-ADD pair left the live queue unconstrained between two commits, took an ACCESS EXCLUSIVE
  lock on every CLI invocation, and raced two starters into `DuplicateObject`.
- **Never give `queue.release_expired` a default `visibility_timeout_s`.** It is a required keyword
  argument, unlike `claim_next`'s, and the asymmetry is the point: a claimer states the lease it is
  taking and knows the number, while a sweeper states how dead somebody else's worker must be before
  its work is seized — a fact this package cannot see. A default here is a guess wearing a policy's
  clothes, and it was one: callers that fell back to `DEFAULT_VISIBILITY_TIMEOUT_S` (300 s) swept
  against a worker whose lease is 900 s and requeued captures out from under running processes.
- **Never assume a `triage` row is generic.** Some carry `schema.SITUATION_KEY` and are identity
  questions `stigmergy-entities` can act on; some do not. Refusing to mint an entity from a row that
  is not an identity question is `entities.situations.require_situation`'s job, not this package's.
- **Never echo captured material — or a reply — unfenced.** A new read surface attaches to
  `query_submissions`'s shaping, which carries the withheld rule, not to its own `SELECT`.

## Data & contracts

- **`capture_queue`** — the durable row. Base columns: `id`, `kind`, `payload` (JSONB, nullable so
  retention can delete in place), `blob_refs`, `submitted_by`, `hints` (JSONB, same), `status`,
  `attempts`, `created_at` / `claimed_at` / `finished_at`, `result_ref`, `error`. Additive since:
  `report` (the librarian's structured account), `asked_at` (the one-ask budget — stamped on the
  FIRST `needs_input` and never cleared, so it survives a reply, a requeue and a redelivery),
  `parked_at` (when the CURRENT park began), `reply`, `trace` (append-only, bounded at 20 events,
  oldest dropped) and `outcome` (the agent's distillation, kept across a park so a re-file need not
  re-read the material — cleared on every terminal transition). Three indexes:
  `(status, created_at)`, `(submitted_by, created_at DESC)`, `(status, claimed_at)`.
- **`schema.STATUSES`** — `queued · claimed · filed · rejected · resolved · needs_input · triage ·
  failed`. `TERMINAL_STATUSES` = `{filed, rejected, resolved, failed}`; `PARKED_STATUSES` =
  `{needs_input, triage}`; `FINISHED_STATUSES` (what `finish()` accepts) = terminal-minus-`resolved`
  plus the parked pair. **`resolved` is deliberately absent from `FINISHED_STATUSES`**: it is a
  steward's disposition on a row nobody holds a lease on, so it must not be reachable through the
  lease-fenced transition at all — it has its own guarded statement, `queue.dispose`.
- **`schema.KINDS`** = `("raw", "page", "meeting", "drive")`; **`MCP_SUBMIT_KINDS`** stays `("raw", "page")`, which is what keeps both operator doors (`stigmergy-meeting`, `stigmergy-drive`) the only way into their flows. A
  `kind` names the SHAPE of the material and the flow that reads it, never a topic — read a new one
  as "which reader claims this row".
- **The hints allowlists** — `HINT_KEYS` (`type`/`path`/`entity`/`title`, the submitter's placement
  suggestions), `SOURCE_HINT_KEYS` (seven Slack provenance fields), `MEETING_HINT_KEYS`
  (`meeting_date`/`attendees`/`source_label`); `DRIVE_HINT_KEYS` (`drive_file_id`, `drive_name`, `drive_url`, `drive_mime`, `drive_modified`); `ALLOWED_HINT_KEYS` is their union and is what
  `normalize_hints` checks against. Every value is a plain string — a list is joined by the caller
  before it reaches this layer. `SOURCE_PROVENANCE_HINT_KEYS` (`source_client`, `source_permalink`)
  is the trusted subset: the first decides whether a `sources/slack/` page is attached, the second
  becomes that page's `url:`, and `reject_source_provenance_hints` refuses both from any door but
  `SLACK_DOOR`. `DRIVE_PROVENANCE_HINT_KEYS` (`drive_file_id`, `drive_url`) is the drive door's
  own trusted pair — `drive_url` lands on a reader-facing page — refused from every client door by
  `reject_drive_provenance_hints`, even though the drive flow is keyed on the row's `kind` rather
  than on a hint. `drive_name` sits outside the pair on purpose: any door may send it (the seam
  merely REQUIRES it on a drive row — it decides the conversion method at the worker), so nothing
  about it is door-gated.
- **`normalize_hints`'s three-key output** — `{"client", "declared_frontmatter", "flagged"}`. The
  two checks consult DIFFERENT constants on purpose: a `hints` key is the client addressing THIS
  QUEUE, so the whole `SERVER_OWNED_FIELDS` union is refused; frontmatter is a document describing
  ITSELF, where `id` and `status` are ordinary page-contract fields, so only `ATTRIBUTION_FIELDS` is
  flagged there.
- **`declared_frontmatter` is deliberately not YAML.** The input is attacker-controlled text over a
  public boundary and `yaml.safe_load` still expands anchors and aliases; a shallow, non-recursive
  line scan cannot be made to allocate. It is best-effort BY DESIGN — the flag is an annotation, and
  attribution never reads it, so a missed flag loses a note and never the security property.
- **`SERVER_OWNED_FIELDS`** = `ATTRIBUTION_FIELDS` (`submitted_by`, `verification`, `acl`,
  `content_hash`) ∪ `QUEUE_OWNED_COLUMNS` (nine). **Every member of `ATTRIBUTION_FIELDS` must have a
  declared parameter on the `brain_submit` MCP tool** — FastMCP builds the argument model with
  pydantic's `extra="ignore"`, so an undeclared field is stripped silently and can be ignored but
  never refused. That invariant has been broken once already.
- **`REJECTION_REASONS`** — `secret`, `pii`, `duplicate`, `steering`, `steward`,
  `malformed-frontmatter`. **`WITHHELD_REASONS`** = `{secret, pii}`.
- **The three withheld sentences** (`withheld_reason`, priority order): `queued`/`claimed` → the
  gate has not run at all (`WITHHELD_PENDING_NOTE`); `failed` → the accepted residual, and nothing
  automatic will look again (`WITHHELD_UNSCANNED_NOTE`); otherwise a genuine secret/PII match, or a
  legacy `rejected` row with no `reason_code` at all (`WITHHELD_MATERIAL_NOTE`, fail-closed).
  Everything else returns `""`. Three reasons get three sentences on purpose: telling the submitter
  of an ordinary queued capture that it "was refused as a secrets match" is false and alarming.
- **`schema.SITUATIONS`** = `unresolved-entity`, `unsupported-type`, written into a `triage` row's
  `report[SITUATION_KEY]` by the librarian and read by `entities.situations`. `SITUATION_NAME_KEY`
  keeps a single-string contract; `SITUATION_NAMES_KEY` is the additive list a multi-name park (a
  meeting) writes, authoritative when present and iterated independently per name.
  `UNNAMED_ENTITY_PLACEHOLDER` (`"something unnamed"`) is refused BY VALUE by the entities CLI — it
  is a syntactically ordinary name that would otherwise be suggested as a ready-to-run `--name`.
- **`job_runs.status`** — `ok` / `error` / `partial`, plain TEXT with no CHECK. The vocabulary is
  specified in `ops.py`'s module docstring, and that docstring is the shared spec: read it before
  adding a fourth value. `partial` means the run's PRIMARY work committed while an independent
  AUXILIARY sub-pass failed; `gardener` is its one live user today.
- **The evidence key** — `sha256/<2 hex>/<2 hex>/<full hex>`. Content addressing means identical
  material occupies one object while still producing two queue rows; the key is verifiable by
  re-hashing; the fan-out keeps prefix listings small.
- **`errors.ReplyRejected`** — two refusal shapes on one class. An IDENTITY failure gets one fixed
  generic sentence, byte-identical whether the row does not exist, belongs to someone else, or is
  not even parked (otherwise the response is an existence oracle). A STATE failure, raised only for
  a caller already authorized to see the row, may name the actual status.

## Tests

`tests/capture/` — 15 modules, ~4,600 lines.

**Pure (keyless, DB-less)**: `test_schema.py` (the largest — the submission contract, the hint
allowlists, the refusals, `withheld_reason`'s three-way branch, the meeting-date validator),
`test_dispositions.py` (the two report sentences and `clean`), `test_latency.py` (percentiles and
the below-threshold refusal), `test_evidence.py` (the key scheme and both stores),
`test_adversarial_cat7.py` (a frontmatter declaring its own server-owned fields).

**Postgres-backed** (`make db-up`, and `tests/testdb.py` refuses any database but `stigmergy_test`):
`test_queue_pg.py`, `test_queue_status_reads_pg.py`, `test_queue_withheld_reply_pg.py` (the full
status × `reason_code` matrix), `test_dispositions_pg.py`, `test_retention_pg.py`, `test_ops_pg.py`,
`test_schema_migration_pg.py` — **and the three CLI suites, whose names carry no `_pg` and which
need the database (and MinIO) all the same**: `test_cli.py` (`stigmergy-queue`, all eight
subcommands, dispositions included, and `reclaim`'s mandatory `--visibility-timeout`, where both
commands its refusal names are parsed out of the message and run), `test_meeting_cli.py`
(`stigmergy-meeting drop`'s four refusals and the validate → upload → insert ordering) and
`test_drive_cli.py` (`stigmergy-drive drop`'s format policy, the URL/id shapes, and the
manifest-as-material / original-bytes-as-blob split, against an injected Drive client).

Two properties cannot be proven without the real stack, and a double that fakes them proves the
double: exactly-once claiming (`FOR UPDATE SKIP LOCKED`) and the evidence store's dedup by object
count. `scripts/e2e_write.sh` (`make e2e-write`) is the compose-level proof end to end.

Layering is pinned in `tests/test_architecture.py`:
`test_capture_never_imports_server_answer_or_pipeline`, `test_only_capture_cli_may_import_the_index`,
`test_capture_library_modules_never_import_raw_psycopg`,
`test_capture_library_modules_touch_no_global_state_at_module_scope`,
`test_capture_never_imports_slack`, and the positive `test_server_imports_capture`.

## Common tasks

| Task | Touch |
|---|---|
| Add a new terminal or parked status | `schema.STATUSES` (the CHECK constraint rebuilds itself from it), then `TERMINAL_STATUSES` / `PARKED_STATUSES` / `FINISHED_STATUSES`, then whichever of `queue.finish` / `queue.dispose` produces it |
| Add a new steward disposition | a function in `dispositions.py` calling `queue.dispose` with its own status/event, its own report builder, and a `stigmergy-queue` subcommand — never a new SQL statement |
| Add a new server-owned field on `brain_submit` | `schema.ATTRIBUTION_FIELDS` **and** the tool parameter, in the same change |
| Add a new capture surface | validate through `schema.prepare_submission`, write through `queue.submit`, archive through `evidence.py`. The Slack transport and `meeting_cli.py` are the two worked examples |
| Add a new provenance-shaped hint | a new small string-valued allowlist added to `ALLOWED_HINT_KEYS`. If a downstream reader will TRUST it, also add a `reject_*` refusal at the client-reachable seam, mirroring `reject_source_provenance_hints` — never inside `normalize_hints` |
| Change a meeting requirement or format rule | `schema._require_meeting_hints` / `schema.validate_meeting_date` — the seam every caller of `queue.submit` crosses. The drop CLI's early copy exists only for message quality and is never the control |
| Change what retention keeps or purges | `retention._ELIGIBLE` (shared by the preview and the action, so a dry run cannot describe a different set) and `schema.TERMINAL_STATUSES` |
| Change how a steward's free text is cleaned | `dispositions.clean` — never at a call site |
| Add a statement to the startup DDL | append to `_ALL_DDL`; it already runs inside `startup_ddl_lock`. Adding a new PLACE that runs DDL needs the same lock |

## Notes

- **The write order is deliberate and asymmetric**: validate → evidence blob → queue row. An orphan
  blob is inert and content-addressed (the next identical submission reuses it); a row pointing at a
  blob that was never written is a submission nothing can verify. Validation runs before both, so a
  refusal writes neither.
- **`attempts` counts deliveries, not failures.** It is incremented on CLAIM and never on release, so
  a worker that dies before writing anything still burns one — which is what stops a poison item
  from being redelivered forever.
- **`purge_secret_capture_immediately` is NOT atomic with the rejection that triggers it.**
  `index.store.connect` opens every connection autocommit, so the status write and the purge are two
  separately-committed statements. `purge`'s age-independent `WITHHELD_REASONS` clause is the
  nightly reconciler for a crash between them — the gap is named and self-healing, not prevented.
- **Retention is honest about "physically"**: the UPDATE removes the value from the live row
  immediately, but the previous row version survives as a dead tuple until autovacuum. A hard
  guarantee for a data-subject request is a `VACUUM` away, documented rather than performed here.
- **`latency.py` was relocated from `stigmergy.librarian`** so `server.pilot_report` could reach it:
  `server` may not import `librarian`, and every caller — `librarian.cli`, `server.pilot_report`,
  `admin.service` — sits at or above `capture`.
- **`capture` never imports `librarian` — the traffic runs the other way, and `cli.py`'s pure
  renderings are what crosses.** `librarian.cli` imports `RECLAIM_NOW` / `depth_line` / `format_ms`
  and `entities.cli` imports `_clean` / `format_age`, so two tools in one operator's terminal print
  one dialect; `latency.render` uses `format_ms` from inside this package for the same reason. Every
  one of those is a downward edge and allowed.
