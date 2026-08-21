# The capture queue — `stigmergy.capture`

The front half of the fast lane: the durable queue a capture lands in, the exactly-once claim
primitive that drains it, the content-addressed evidence archive, the operational spine and
retention. Design record: [ADR 014](../decisions/014-capture-queue-and-attribution.md); the two
MCP tools that reach it are served by [server.md](./server.md), which owns identity, rate limiting
and audit; what a steward does after a filing is
[operator-runbook.md → Governing what the librarian proposed](./operator-runbook.md#governing-what-the-librarian-proposed).
Code map: [`src/stigmergy/capture/index.md`](../../src/stigmergy/capture/index.md).

**This package drains nothing.** Filing a capture into a page — the git commit, dedup, wikilink
resolution, entity anchoring, template validation and the nine gates — lives in
[librarian.md](./librarian.md). A submission here reaches `queued` and waits. **Nothing verifies a
figure at write time**: the only deterministic figure check runs at ANSWER time ([answer.md](./answer.md)).

**Nothing here ever waits on a person.** A capture is queued, claimed and finished; a name the
registry does not know does not stop it — the librarian files the page and proposes the identity,
and a steward confirms it afterwards from the review inbox. The two states a capture used to park
in are retired (see [The queue](#the-queue)).

## Module map

| Module | Does |
|---|---|
| `schema.py` | the DDL this package owns — `capture_queue`, `job_runs`, `ingest_errors`, all `CREATE TABLE IF NOT EXISTS` plus every additive column (`audit_log` is the fourth durable table and is created by `stigmergy.server.audit`; it is only NAMED here, because this is the one place the durable/disposable boundary is written down) — the status enum with its retired pair and the startup migration that empties it, `DURABLE_TABLES`, the hint allowlists, `clean_note` (the bound-and-sanitize every operator-typed string crosses), and the pure submission contract: size cap, kind/hint validation, server-owned-field refusal, frontmatter flagging |
| `queue.py` | insert · claim (`FOR UPDATE SKIP LOCKED`) · release-expired · the one terminal transition (`finish`, fenced by `attempts`) · the one listing query and its two semantic entry points · the per-submission trace |
| `decisions.py` | the append-only `review_decisions` ledger — its DDL, `record_decision`, the three reads (`latest_decisions` for every item at once; `latest_decision_for` for one, off the table's own index, which is what a refusal path asks; `recent_decisions` as a bounded feed), the verdict vocabulary (`approve`, `reject`, `merge`, `request_changes`) and `DECISION_SOURCES`. It lives here because all four deciding doors have to write it, and one of them (`stigmergy-entities`) may not import `stigmergy.server`. Every write names its door (`mcp`, `slack`, `admin`, `cli`) in a required `source` argument, refused with a `ValueError` if it is not one of the four — the table is append-only, so a door's own misspelling could never be corrected. It is stamped into `extra` LAST, so the validated value wins: a caller cannot override its door by putting a `source` key in `extra`. Both reads give back `""` for the rows written before the field existed. **This ledger is also the librarian's decline memory**: `librarian.processing._declined_identity_ids` reads it, so an identity a steward declined is never proposed again |
| `evidence.py` | the content-addressed store: `S3EvidenceStore` (MinIO/R2), `MemoryEvidenceStore` (the offline double), `content_key` |
| `ops.py` | the operational spine: `job_runs` / `ingest_errors` writers and the `job_run` context manager |
| `retention.py` | `purge` — physical deletion of `payload`/`hints`/`outcome` on old terminal rows, plus the age-independent reconciliation for a secret/PII rejection; `purge_secret_capture_immediately` — `payload`/`hints`, right now, for exactly that rejection |
| `latency.py` | capture→filed and capture→searchable p50/p95 from the trace alone, here rather than in `stigmergy.librarian` so `stigmergy.server.pilot_report` can reach it too |
| `render.py` | the operator dialect every CLI prints in: `depth_line`, `format_ms`, `format_age`, `clean_for_terminal`, `RECLAIM_NOW`. Below the CLIs, because `latency.py` and `stigmergy-librarian`/`stigmergy-entities` read it too; it reaches nothing but `stigmergy.text` |
| `cli.py` | `stigmergy-queue` — the operator's view: list · show · claim · reclaim · purge; `render.py`'s names are re-exported here for the CLIs that already take them from this module; and the split-stores guard (`add_split_stores_flag` / `refuse_split_stores` / `EXIT_SPLIT_STORES`) the one CLI that still enqueues runs before it does |
| `errors.py` | the domain exceptions (`SubmissionRejected`, `EvidenceError`, `QueueStateError`), all under `CaptureError` |

**Layering.** `capture` must never import `stigmergy.server` or `stigmergy.answer`; `stigmergy.server`
imports `capture`. The outward edge is to `stigmergy.index`, with one rule: **only `capture.cli` may
import `stigmergy.index`**, asserted by
`tests/test_architecture.py::test_only_capture_cli_may_import_the_index`. Library code here never
opens a connection and never reads the environment: every function takes `conn`, and an entry point
supplies it.

## The two MCP tools

| Tool | What it does |
|---|---|
| `brain_submit(kind, material, hints?)` | queue a capture. `kind` names the SHAPE of the material, and it is the queue's own `KINDS` with nothing held back: `raw` (a conversation excerpt, a decision, a gotcha), `page` (markdown you drafted), `meeting` (a transcript) or `document` (the text of a document you already hold). ONE vocabulary for every door — no door has a kind of its own, so nothing here is narrower than anywhere else ([ADR 044](../decisions/044-the-capture-is-the-approval.md) D4). The material cap is per kind, in UTF-8 bytes: 256 KB for `raw` and `page`, 1 MB for `meeting` and `document` (`MATERIAL_CAP_BYTES`, read through `max_material_bytes(kind)`; `MAX_MATERIAL_BYTES` is the largest of them, which is what a transport's request-body limit has to fit). `hints` optionally suggests placement — `type`, `path`, `entity`, `title`, suggestions only. Three further allowlists carry provenance rather than placement (`SOURCE_HINT_KEYS` for the Slack transport, `MEETING_HINT_KEYS` — `meeting_date`, `attendees`, `source_label` — and `DOCUMENT_HINT_KEYS` — `source_url`), and a fifth carries AUTHORITY rather than either: `REGISTER_HINT_KEYS` (`register_name`, `register_type`, `register_aliases`, `register_source`), refused outright here — see [A registration is a capture](#a-registration-is-a-capture-and-only-two-doors-may-assert-one). Exactly one provenance subset is door-gated: `SOURCE_PROVENANCE_HINT_KEYS` (`source_client`, `source_permalink`), refused at the client seam because the Slack TRANSPORT asserts that pair, not a person. Neither a meeting's hints nor a document's are refused from anybody: what a submitter asserts about their own material has the standing the material itself has, and is attributed to them the same way. `ALLOWED_HINT_KEYS` is the union of all five lists, and anything outside it is refused by name. Returns an ack with the submission id, the archived object key and a message that promises exactly what happened: **queued and attributed**, not "saved" — plus `entities`, the registered entities this material already names (`{id, name, proposed}` each, ACL-scoped like `list_entities`), so a submitter sees on the spot which identities the brain recognises |
| `brain_submissions(limit?, status?)` | what happened to what you captured: your own submissions, newest first, with state, timestamps, `result_ref`, the librarian's `report` (which names the page, the anchor, and any entity or spelling it PROPOSED), the row's `events` and a fenced excerpt. An unrestricted (steward) identity sees the whole queue with `mine` marking its own rows. A capture refused for a secret or PII echoes nothing — see [Withheld material](#withheld-material) |

Both ride `BrainService._call`, so they inherit per-identity rate limiting, the audit row and
the error shaping the read tools have — one seam, not a second write path.

### How a meeting and a document enter

Through `brain_submit`, like everything else. Both kinds carry TEXT the CLIENT already holds, and
the client is what extracted it: an agent session with a Drive connector, a person with the file
open in front of them, a script. Nothing is fetched or converted server-side — no Google credential
exists there ([ADR 044](../decisions/044-the-capture-is-the-approval.md) D4).

| Kind | Material | Required hints | Optional hints | Cap |
|---|---|---|---|---|
| `meeting` | the transcript | `title`, `meeting_date` (`YYYY-MM-DD`) | `attendees`, `source_label` | 1 MB |
| `document` | the document's text | `title` | `source_url`, one line and `http(s)` | 1 MB |

Both requirement sets are checked in `schema.prepare_submission` — the seam every caller of
`queue.submit` crosses, so no door can skip them, and the refusal happens before any blob or row is
written. Each requirement earns its place: a missing `meeting_date` silently degrades every filed
decision page's `as_of` to today, and a document with no title has no identity for its source page
to carry.

What the worker does with them differs. A `meeting` runs the second flow and files a page SET
(source + meeting + one decision page per decision — [meeting-distiller.md](./meeting-distiller.md));
a `document` runs the ordinary flow with the source attachment on, so a synthesis page lands beside
the verbatim `sources/documents/` part(s), and `source_url` lands as `url:` on them
([librarian.md → The source attachment](./librarian.md#the-source-attachment-a-parameter-never-a-third-flow)).

### Nothing is ever asked of a submitter

There is no reply tool, and no state a capture sits in waiting for one. A name nothing in the
registry resolves is PROPOSED: the librarian creates the entity page in the same commit as the note
(`approved_by: ""`, the proposal mark), anchors the page to it, and files. The report says so, and a
steward confirms, merges or declines the identity afterwards from the review inbox
([server.md → The review tools](./server.md#the-review-tools)). The mechanism is
[librarian.md → Writing an identity](./librarian.md#writing-an-identity-what-a-filing-does-to-the-registry);
the governance doors are
[operator-runbook.md](./operator-runbook.md#governing-what-the-librarian-proposed).

### Attribution is the server's

`submitted_by` is the resolved caller identity: the `--identity` name over stdio, the token's
email over HTTP. It is never an argument. `brain_submit` **declares** four server-owned
parameters — `submitted_by`, `verification`, `acl`, `content_hash` — solely so that passing any of
them is an explicit error rather than something the MCP SDK drops silently. A refused submit
creates **no row and no blob**:

```json
{"error": "submitted_by is set by the server, not by the caller — remove it and resubmit
  (attribution comes from your resolved identity; submitting as someone else requires their
  token, not their name)"}
```

The same refusal covers any server-owned field inside `hints` (those four, plus the queue's own
columns).

### The residual: declared fields are refused, undeclared ones are dropped

FastMCP builds a tool's argument model with pydantic's default `extra="ignore"`, so an undeclared
argument is **stripped by the SDK before the server sees it**, silently; the trap can only refuse
what it declares. Everything else rests on the structural guarantee that no code path reads client
input into a server-computed column. The consequence: **a future server-owned field added to the
queue contract without a matching declared, refused parameter would be silently swallowed here.**

#### Adding a server-owned field safely

`capture/schema.py` splits the server-owned names into two lists with opposite obligations:

| Constant | Members | Obligation |
|---|---|---|
| `ATTRIBUTION_FIELDS` | `submitted_by`, `verification`, `acl`, `content_hash` | **every member MUST have a declared parameter on `brain_submit`** — declaring is the only way to refuse rather than silently ignore, and a test walks the set to enforce it |
| `QUEUE_OWNED_COLUMNS` | `id`, `status`, `attempts`, `blob_refs`, `result_ref`, `created_at`, `claimed_at`, `finished_at`, `error` | deliberately **no** tool parameter — a client has no vocabulary for a queue column, so there is nothing to declare; they are listed so that naming one in an argument or a hint is refused loudly rather than confusingly |
| `SERVER_OWNED_FIELDS` | the union | what `reject_server_owned_arguments` consults |

A new field a client or a document could plausibly assert goes in `ATTRIBUTION_FIELDS` **and** onto
the tool signature, in the same change; a new queue column goes in `QUEUE_OWNED_COLUMNS` only. A key
inside `hints` addresses the **queue**, so the union is refused there; frontmatter is a document
describing **itself**, where `id` and `status` are ordinary page-contract fields, so only
`ATTRIBUTION_FIELDS` is flagged. Such frontmatter is **recorded, never trusted**: the material is
stored verbatim, the declared fields land in `hints.declared_frontmatter`, and the server-owned
subset is listed in `hints.flagged` and echoed in the ack:

> Note: the material declares acl, content_hash, submitted_by, verification in its frontmatter;
> recorded as a hint and ignored — those fields are the server's.

### A registration is a capture, and only two doors may assert one

A steward introducing an entity nobody has captured about does not write a page: what they know
about it becomes the MATERIAL of an ordinary `raw` capture, and four hints say which entity that
capture registers.

| Hint | What it carries |
|---|---|
| `register_name` | the entity's name, as the steward spelled it — its page title, its filename, its wikilink target |
| `register_type` | the entity's type, one of the registry's closed list of entity types |
| `register_aliases` | the other spellings that mean it, comma-separated (empty when there are none) |
| `register_source` | which door asked — `admin` or `cli`, `schema.REGISTRATION_SOURCES`, the same two spellings the review ledger records |

`schema.registration_hints(...)` builds all four (plus the ordinary `entity` hint, so the steward's
own spelling is in the haystack the librarian checks a proposed name against), and
`schema.registration_from_hints(hints)` reads them back off a stored row as a `Registration`. The
capture's `submitted_by` is the steward, which is what the entity page's `approved_by` is born
carrying.

**Exactly two doors may set them, and neither is a client.** The console's *Register an entity* and
`stigmergy-entities create` call `queue.submit` directly. Every client-reachable door goes through
`BrainService._submit`, which calls `schema.reject_registration_hints` and REFUSES any `register_*`
key by name: a registration is an act of authority, and `brain_submit` attributes material, never
authority. The refusal says so and names what to do instead — capture what you know about the
thing, and the librarian proposes the entity for a steward to confirm.

## The queue

```sql
capture_queue(id, kind, payload jsonb, blob_refs text[], submitted_by, hints jsonb,
              status, attempts, created_at, claimed_at, finished_at, result_ref, error,
              report jsonb,                                   -- the librarian's account
              asked_at, parked_at, reply, trace jsonb,        -- the retired human loop
              outcome jsonb)                                  -- the retired stored outcome
```

**The last five columns are LEGACY and are still created on a fresh database.** They were the human
loop, back when a capture could wait on a person; nothing writes `asked_at`, `parked_at`, `reply` or
`outcome` any more, and a reader of an old row still has to find the columns it expects. `trace`
gains exactly one further event per row the startup migration below moves, and nothing after that.
Retention still nulls `outcome` along with `payload`/`hints`.

| Status | Means | Set by |
|---|---|---|
| `queued` | waiting for the librarian | submit, the expiry sweep, the startup migration below |
| `claimed` | a worker holds a lease on it | `claim_next` |
| `filed` | a page exists; `result_ref` points at it | the librarian |
| `rejected` | a gate refused it; `error` says why and `report.reason_code` says which class | the librarian |
| `resolved` | **LEGACY, read-only**: a steward closed the row by hand, back when a capture could park on a person. Nothing writes it; rows carrying it stay readable and purgeable | nothing |
| `failed` | the librarian could not finish the item, or the attempts ran out; an `ingest_errors` row has the detail (stage `librarian` in the first case, `claim` in the second) | the librarian, or the expiry sweep |

Every status but `queued` and `claimed` is terminal (`TERMINAL_STATUSES`), and `finish` may move a
claim into `filed`, `rejected` or `failed` only (`FINISHED_STATUSES` — `resolved` is absent because
nothing reaches it). `error` is the one "why is this row where it is" field
([ADR 014](../decisions/014-capture-queue-and-attribution.md)).

### The two retired states, and the migration that empties them

`needs_input` and `triage` are `schema.RETIRED_STATUSES`. They were the two states a capture waited
on a person in; a name the registry does not know is now proposed and filed instead. They are NAMED
rather than deleted because two statements spell them:

- `_CAPTURE_QUEUE_PARKED_MIGRATION` runs at startup, BEFORE the CHECK swap, and returns any row
  still in either state to `queued` — clearing `error`, `claimed_at` and `parked_at` and appending
  one `trace` event (`event: "requeued"`, `actor: "migration"`) saying the parked states retired and
  the row is being re-filed under the rule that replaced them. Nothing a person was waiting on is
  lost; it is simply filed. Every later start is a no-op, because no row can enter the states again.
- the status CHECK is then swapped so the two words cannot come back.

`status`'s CHECK constraint is named (`capture_queue_status_check`) — the one migration here that is
not a plain `ADD COLUMN`. It must stay a single `DO $$` block (ONE statement, ONE transaction): a
drop-then-add on an autocommit connection opens a window with no constraint, pays an
`ACCESS EXCLUSIVE` validation on every process start, and races between concurrent starts. Its guard
asks BOTH halves — the constraint names every status in `STATUSES` **and** none in
`RETIRED_STATUSES` — so it is a no-op after the first run and a constraint still admitting a retired
word can never read as current.

### Claiming, leases and `attempts`

`claim_next` is a single `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`:
N parallel claimers against M queued rows produce exactly M claims, no row claimed twice.

A claim is a **lease**: `claimed_at` plus the visibility timeout the claimer states. `claim_next`
defaults to 300 s (a human-scale `stigmergy-queue claim`); the librarian's worker claims with its own
derived lease, 900 s by default, because one item can run two agent attempts plus the gates plus
the headroom on top of both (`librarian.config.minimum_visibility_timeout_s`). A worker that dies
mid-item leaves a stale claim the next claimer's sweep returns to `queued`. That sweep also runs in
the librarian's worker loop and standalone as `stigmergy-queue reclaim`; `queue.release_expired` has **no default** horizon,
because a sweeper cannot see the lease of the worker whose work it is taking away.

`attempts` counts **deliveries**: incremented when a row is claimed, never on release. A worker
that dies before writing anything still burns an attempt, which stops a poison item from being
redelivered forever. At `--max-attempts` (default 3) the row goes `failed` and an `ingest_errors`
row records the stage and count.

**`attempts` is also the fencing token.** A worker that *stalls* must not come back and finish an
item since handed to someone else. `finish()` requires `expected_attempts`, the `attempts` value
`claim_next` handed *this* delivery, and the UPDATE matches only
`status = 'claimed' AND attempts = %s` — a `status = 'claimed'` guard alone would let a stale write
land. A stale finish updates nothing and raises `QueueStateError`:

```
submission 5 was redelivered (this worker held delivery 1, it is now on 2) — its lease is gone
and another worker owns it; do not retry, discard this run's work
```

## The evidence plane

Every submission archives its raw material at:

```
sha256/<first two hex>/<next two hex>/<full sha256 hex>
```

Identical material submitted twice yields **two queue rows and exactly one object** — the bytes
deduplicate by content addressing, while dedup against the graph stays the librarian's judgment.
The key is verifiable: re-hash the bytes and compare (`content_key` is pure). The same sha256
appears in the submission's `audit_log` row, so "who submitted what, when" joins to "which object
holds it" without the audit row ever carrying the material.

`blob_refs[0]` is the text material, and today it is the only entry: every kind reaches the queue
as text, so there is no second artefact to archive beside it. `queue.submit` still takes
`extra_blob_refs`, appended AFTER the material's own blob so `blob_refs[0]` stays what every reader
assumes — a door that one day archives an original alongside its text has the seam, and nothing
passes it now.

The blob is written **before** the queue row, and the order is load-bearing: an orphan blob is
inert and gets reused by the next identical submission, whereas a row pointing at a blob that was
never written is a capture whose evidence cannot be produced on demand.

**The one CLI that enqueues refuses to write across a split deployment.** `stigmergy-entities
create` checks, before any upload, that the queue's database host and the evidence endpoint can
plausibly belong to the same deployment (`capture.cli.refuse_split_stores`,
`evidence.split_stores_reason`) — a remote queue paired with a loopback evidence endpoint is refused
with exit `3`, since a deployed worker can never reach a store on the operator's laptop and the
capture would fail with `NoSuchKey` seconds after being claimed. The escape hatch is
`--allow-split-stores`. `stigmergy-queue` carries no such guard: none of its subcommands upload
evidence.

### Configuration

| Var | Default (local = the compose service) | Staging |
|---|---|---|
| `STIGMERGY_EVIDENCE_ENDPOINT` | `http://127.0.0.1:9000` | the R2 S3 endpoint |
| `STIGMERGY_EVIDENCE_BUCKET` | `stigmergy-evidence` | the R2 bucket |
| `STIGMERGY_EVIDENCE_ACCESS_KEY_ID` | `minioadmin` | the scoped R2 key id |
| `STIGMERGY_EVIDENCE_SECRET_ACCESS_KEY` | `minioadmin` | the scoped R2 secret |

The defaults match the `minio` service in `docker-compose.yml`, so `make db-up` plus a submit works
with no configuration. Building the store does **no I/O**: a server whose bucket is unreachable
still starts and serves every read tool, and the failure surfaces on the submit that needs it, as a
clean error naming no bucket, endpoint or credential.

**Setting the staging column** (the four Fly secrets, against the R2 bucket) is
[operator-runbook.md → One-time setup](./operator-runbook.md#one-time-setup-before-the-first-deploy),
which also carries the bucket's retention policy — the one step that must happen **before** other
people's material enters it. The separate `R2_*` variables belong to
[`scripts/r2_smoke.py`](../../scripts/r2_smoke.py) (`make r2-smoke`), a standalone credential
check against the raw bucket.

## `stigmergy-queue` — the operator's view

```sh
.venv/bin/stigmergy-queue list                          # depth per status + the newest submissions
.venv/bin/stigmergy-queue list --status queued --limit 50
.venv/bin/stigmergy-queue show 7                        # one submission's trace and latencies
.venv/bin/stigmergy-queue claim --hold 60               # take one item; hold the lease, file nothing
.venv/bin/stigmergy-queue reclaim --visibility-timeout 0    # release EVERY claimed row, right now
.venv/bin/stigmergy-queue reclaim --visibility-timeout 900   # ...only ones past the worker's lease
.venv/bin/stigmergy-queue purge --dry-run               # retention: what would go
.venv/bin/stigmergy-queue purge                         # retention: delete payload+hints
```

### Nothing is drained by hand

**Five subcommands, and none of them moves a row on a person's behalf.** A capture reaches a
terminal state through the librarian or through the expiry sweep, and nowhere else: the three
dispositions this CLI used to carry (`requeue`, `resolve`, `reject`) retired with the parked states
they operated on. What replaced them is a decision about an IDENTITY rather than about a queue row,
taken after the page is already filed, through `stigmergy-entities` and its three sibling doors —
[operator-runbook.md](./operator-runbook.md#governing-what-the-librarian-proposed).

`show` still prints a row's `trace`, because an old row's history is still its history: what a
steward did to it while captures could park, and the one `requeued` event the startup migration
appended on the way out of a retired state.

`--dsn` (or `$STIGMERGY_INDEX_DSN`) picks the database; `--json` makes any command machine-readable.
Errors here are **local and specific**, unlike the posture over HTTP.

`claim` deliberately processes nothing: it takes an item, prints it, optionally holds the lease
for `--hold` seconds and exits **without** finishing it. Killing it mid-hold simulates a dead
worker. Ctrl-C during the hold prints a report (under `--json`, a JSON object) rather than a
traceback: which submission now holds an orphaned lease, and the two ways it comes back — the
visibility timeout, or `stigmergy-queue reclaim --visibility-timeout 0`.

> **`--visibility-timeout` means two different things** under the same flag name. On `claim` it is
> how long **this** lease lasts. On `reclaim` it is how **old** a claim must be to be released — so
> `reclaim --visibility-timeout 300` at second zero releases nothing, and `--visibility-timeout 0`
> releases every claimed row right now.
>
> **On `reclaim` the flag is REQUIRED** and the command refuses without it: this CLI cannot see the
> lease the other worker holds, and a shorter horizon requeues captures out from under processes
> still filing them. The refusal names the two values that are almost always right: `0` after you
> killed the worker yourself, and the worker's own lease (900 s by default) otherwise.

Every command exits **130** on Ctrl-C, never `0` — a real lease may still be outstanding.

## The operational spine

```sql
job_runs(id, job, status, started_at, finished_at, stats jsonb, error)
ingest_errors(id, source, source_doc_id, stage, error, attempts, last_at, resolved)
```

`job_runs` records each processing run: `capture-reclaim` when a sweep actually released or failed
something (a no-op sweep stays silent), `capture-purge` / `capture-purge-dry-run` on every retention
run, and `capture-purge-immediate` on each secret/PII rejection's own purge. `ingest_errors` records
each failed item with its stage and attempt count, joined back by `source_doc_id`.

The per-submission trace is `created_at → claimed_at → finished_at` plus `attempts`, queryable by
id (`stigmergy-queue show <id>`, or `queue.get_submission_trace`). It carries the two latencies the
fast lane's capture→page target (p50 < 5 min) is measured with:

```sql
-- capture -> terminal latency, from the trace alone
SELECT status,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY finished_at - created_at) AS p50,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY finished_at - created_at) AS p95
FROM capture_queue WHERE finished_at IS NOT NULL GROUP BY status;

-- what is stuck, and for how long
SELECT id, submitted_by, status, attempts, now() - created_at AS age
FROM capture_queue WHERE status NOT IN ('filed', 'rejected') ORDER BY created_at;
```

## Retention

`stigmergy-queue purge` nulls `payload`, `hints` and `outcome` on rows **terminal for more than 30
days** (`retention.DEFAULT_RETENTION_DAYS`). What survives: `id`, `submitted_by`, `status`, all
three timestamps, `attempts` and `result_ref` — so the trace and the latency measurement stay intact
and a purged submission is still readable as history (`brain_submissions` marks it
`payload_purged: true` and returns no excerpt). `outcome` is on the list because a row written
before the parked states retired can still hold the full drafted body of every page a distillation
produced; nothing writes that column any more, so this is now the only thing that clears it. It is
deliberately **not** part of the eligibility guard, which stays
`payload IS NOT NULL OR hints IS NOT NULL` so the purge is idempotent.

**A second eligibility clause ignores the window entirely.** A `rejected` row whose `reason_code` is
a secret or PII match is purged by every run **regardless of age** — `purge_secret_capture_immediately`
runs as a separate statement from the rejection write (connections are autocommit), so a crash
between the two would otherwise leave such a row holding its payload at rest forever.

The **evidence blob is not touched**: it has its own lifecycle, the bucket's retention policy. Once
the queue row is stripped the archive is the only place the material as submitted still exists, so
it is what a data-subject request has to be executed against. Set that policy on the R2 bucket
**before** other people's material enters it.

The UPDATE removes the value from the live row immediately, but the previous row version survives
as a dead tuple until autovacuum reclaims it. For a hard guarantee run `VACUUM capture_queue` after
the purge; the purge does not do it on every run.

## Withheld material

Some rows are listed with **no excerpt and no client hints**. `withheld_reason` carries
the sentence saying why, on `brain_submissions` and on `stigmergy-queue list` alike. What survives:
id, submitter, status, all three timestamps, `attempts`, `blob_refs`, the `trace` (code-built or
steward-authored notes, never captured material) and the refusal sentence itself.

**Three non-overlapping reasons, each with its OWN sentence** (`schema.withheld_reason`, in
priority order — the query decides the boolean, this function picks which sentence explains it):

| Reason | When | Sentence |
|---|---|---|
| pending | `status` is `queued` or `claimed` (`GATE_NOT_YET_RUN_STATUSES`) | `WITHHELD_PENDING_NOTE` — nothing has scanned this material yet; it appears here as soon as the librarian has looked at it |
| unscanned | `status` is `failed` | `WITHHELD_UNSCANNED_NOTE` — the run failed before the scan reached it, and unlike a queued capture nothing retries it; ask an operator |
| matched | `report.reason_code` is `secret` or `pii` — or the row is `rejected` with **no** `reason_code` at all | `WITHHELD_MATERIAL_NOTE` — refused as a secrets or personal-data match; the refusal itself names what matched and where |

Every other state — `filed`, the legacy `resolved`, and a `rejected` row for any other reason
(`duplicate`, `steering`, `malformed-frontmatter`, or the legacy `steward`) — shows its excerpt
normally: the gate has run and said nothing about this material. The window is keyed on "has the
gate run", never on `TERMINAL_STATUSES`.

Four properties, each load-bearing:

- **Suppressed, never redacted, and never truncated around the match** — gitleaks reports a rule
  and a line, not a guaranteed span, so a redaction would be a guess presented as a guarantee.
- **Decided in the query** (`capture.queue._MATERIAL_WITHHELD`), not at each surface, so the value
  never leaves Postgres for a withheld row. Both read paths — `query_submissions` and
  `get_submission_trace` — go through that one expression. The hints are narrowed rather than
  emptied: the SQL drops the `client` and `declared_frontmatter` sub-objects.
- **Keyed on `report.reason_code`**, the structured half of a refusal (`capture.schema`), never on
  the refusal's prose. A `rejected` row carrying no `reason_code` is withheld too, fail-closed —
  which is also why `steward` exists as a code at all.
- **The SQL and its Python mirror agree by construction.** `_REASON_FLAGGED_SQL` is wrapped in
  `COALESCE(..., false)` so it is a genuine two-valued boolean matching `schema._reason_flagged`;
  without it a NULL `report` evaluates to SQL NULL, a landmine for any consumer that negates it.

The material stays **archived exactly as submitted** — a live credential in it has to be rotated
whatever the report says. Suppression governs read-backs, never the archive.

**A `secret`/`pii` rejection is also purged on the spot**, so on such a row `content_sha256` and
`bytes` (both read out of `payload`) are gone as well — the blob key in `blob_refs` is what still
answers "which object holds what was actually submitted". A withheld `queued`/`claimed`/`failed`
row keeps its payload, and therefore keeps both.

## Reuse these seams

- `capture.queue.claim_next(conn, ...)` — the claim primitive. Never write a second claim query,
  and never `SELECT ... WHERE status = 'queued'` without `FOR UPDATE SKIP LOCKED`.
- `capture.queue.finish(conn, id, status=..., expected_attempts=..., result_ref=...)` — the one
  terminal transition, guarded by state **and** fencing token. Pass the `attempts` value the
  claim returned; `expected_attempts` is required, not optional.
- `capture.schema.clean_note(text)` — the ONE seam an operator-typed string crosses on its way into
  a ledger row or a report: control characters stripped, newlines flattened, clipped word-safe.
- `capture.decisions.record_decision` / `latest_decisions` / `latest_decision_for` /
  `recent_decisions` — the governance ledger, below every door that decides an identity.
- `capture.queue.query_submissions` — the ONE listing query (scope, status filter, ordering,
  paging). New surfaces attach through `list_own_submissions` / `list_all_submissions`.
- `capture.evidence.content_key(bytes)` — the key scheme, as a pure function.
- `capture.schema.prepare_submission` — the submission contract, pure and DB-free: size cap,
  kind/hint validation, server-owned refusal, frontmatter flagging.
- `capture.evidence.MemoryEvidenceStore` — the offline double, so a test needs no bucket.

## Avoid / anti-patterns

- **Never take `submitted_by` from client input** — not from an argument, `hints` or frontmatter.
  It comes from the resolved identity or the submission does not happen.
- **Never open a second write path.** Both MCP tools go through `BrainService._call`; a
  surface that writes to `capture_queue` directly skips rate limiting, the audit row and
  attribution at once. The operator CLIs holding the DSN are the declared exception —
  `stigmergy-queue` for reading and moving rows, `stigmergy-entities create` for the one enqueue
  left outside the server — so `--submitted-by` there is attribution, not authorization.
- **Never add a transition that moves a row on a person's behalf.** A capture is finished by the
  worker holding its claim or by the expiry sweep; the parked states and the three dispositions over
  them are retired, and a governance decision is about an IDENTITY, taken after the filing, through
  `stigmergy.entities`.
- **Never generalize the index rebuild's `DROP`.** `store.init_schema` drops `pages_index` by
  name; the four tables in `schema.DURABLE_TABLES` and `review_decisions` share that database and
  cannot be rebuilt from git — [hybrid-index.md → Sharing the database with the durable
  half](./hybrid-index.md#sharing-the-database-with-the-durable-half).
- **Never echo captured material unfenced.** It is untrusted data like any page body;
  `brain_submissions` fences excerpts and neutralizes every other free-text field it returns.
- **Never echo captured material a refusal already declared unsafe.** Fencing answers "could this
  text act as instructions", not "does it belong in the response at all". A new read surface
  attaches to `queue.query_submissions` and gets both answers — see
  [Withheld material](#withheld-material) — rather than writing its own `SELECT payload ->> 'text'`.
- **Never let observability fail the work.** `job_runs`/`ingest_errors` writes are best-effort and
  swallowed-with-a-loud-log, the same posture as `AuditWriter`.

## Run it

```sh
make db-up                                   # postgres + minio (the evidence plane)
make e2e-write                               # the write-path e2e, from empty volumes

.venv/bin/stigmergy-server --identity steward@example.com --repo ../stigmergy-brain   # submit over stdio
.venv/bin/stigmergy-queue list                 # what is waiting
```

To drive the whole path by hand, follow
[operator-runbook.md → Release gates & drills](./operator-runbook.md#release-gates--drills).
Connecting a client to the server in the first place is
[server.md](./server.md#connect-claude-code--desktop-stdio) (stdio) or
[server.md](./server.md#http-transport) (HTTP, per-user token).

## Tests

`tests/capture/` and the write-path additions under `tests/server/`
(`test_service_capture.py`). The keyless, DB-less suites are `test_schema.py`,
`test_evidence.py` (on `MemoryEvidenceStore`), `test_latency.py` and
`test_adversarial_cat7.py` — forged frontmatter, one of the three armed adversarial categories,
named `test_adversarial_cat7_*` so a `-k` collection finds every case. The `_pg` and CLI suites
need the real database: **exactly-once claiming** needs real Postgres (`FOR UPDATE SKIP LOCKED` is
the mechanism) and **the evidence store's dedup** needs a real bucket for the object-count
assertion.

`scripts/e2e_write.sh` (`make e2e-write`) is the compose-level proof: empty volumes → submit over
the real MCP stdio protocol → archive → index rebuild → parallel claimers → a killed worker →
retention.
