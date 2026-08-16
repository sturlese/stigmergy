# The capture queue — `stigmergy.capture`

The front half of the fast lane: the durable queue a capture lands in, the exactly-once claim
primitive that drains it, the content-addressed evidence archive, the operational spine and
retention. Design record: [ADR 014](../decisions/014-capture-queue-and-attribution.md); the three
MCP tools that reach it are served by [server.md](./server.md), which owns identity, rate limiting
and audit; operating it day to day is
[operator-runbook.md → Draining parked rows](./operator-runbook.md#draining-parked-rows).
Code map: [`src/stigmergy/capture/index.md`](../../src/stigmergy/capture/index.md).

**This package drains nothing.** Filing a capture into a page — the git commit, dedup, wikilink
resolution, entity anchoring, template validation and the eight gates — lives in
[librarian.md](./librarian.md). A submission here reaches `queued` and waits. **Nothing verifies a
figure at write time**: the only deterministic figure check runs at ANSWER time ([answer.md](./answer.md)).

## Module map

| Module | Does |
|---|---|
| `schema.py` | the DDL this package owns — `capture_queue`, `job_runs`, `ingest_errors`, all `CREATE TABLE IF NOT EXISTS` plus every additive column (`audit_log` is the fourth durable table and is created by `stigmergy.server.audit`; it is only NAMED here, because this is the one place the durable/disposable boundary is written down) — the status enum, `DURABLE_TABLES`, the hint allowlists, and the pure submission contract: size cap, kind/hint validation, server-owned-field refusal, frontmatter flagging |
| `queue.py` | insert · claim (`FOR UPDATE SKIP LOCKED`) · release-expired · terminal transitions · the guarded `dispose` transition · the one listing query and its two semantic entry points · the per-submission trace |
| `dispositions.py` | the steward's drain: `requeue`/`resolve`/`reject` — three semantic intents over `queue.dispose`'s ONE guarded transition, plus the sentence each puts in the submitter's report, plus `clean` (the bound-and-sanitize every `--note`/`--reason` crosses) |
| `decisions.py` | the append-only `review_decisions` ledger — its DDL, `record_decision`, the two reads (`latest_decisions` for every item at once; `latest_decision_for` for one, off the table's own index, which is what a refusal path asks), the verdict vocabulary and `DECISION_SOURCES`. It lives here because all three minting doors have to write it, and one of them (`stigmergy-entities`) may not import `stigmergy.server`. Every write names its door (`mcp`, `slack`, `admin`, `cli`) in a required `source` argument, refused with a `ValueError` if it is not one of the four — the table is append-only, so a door's own misspelling could never be corrected. It is stamped into `extra` LAST, so the validated value wins: a caller cannot override its door by putting a `source` key in `extra`. Both reads give back `""` for the rows written before the field existed |
| `evidence.py` | the content-addressed store: `S3EvidenceStore` (MinIO/R2), `MemoryEvidenceStore` (the offline double), `content_key` |
| `ops.py` | the operational spine: `job_runs` / `ingest_errors` writers and the `job_run` context manager |
| `retention.py` | `purge` — physical deletion of `payload`/`hints`/`outcome` on old terminal rows, plus the age-independent reconciliation for a secret/PII rejection; `purge_secret_capture_immediately` — `payload`/`hints`, right now, for exactly that rejection |
| `latency.py` | capture→filed and capture→searchable p50/p95 from the trace alone, here rather than in `stigmergy.librarian` so `stigmergy.server.pilot_report` can reach it too |
| `render.py` | the operator dialect every CLI prints in: `depth_line`, `format_ms`, `format_age`, `clean_for_terminal`, `RECLAIM_NOW`. Below the CLIs, because `latency.py` and `stigmergy-librarian`/`stigmergy-entities` read it too; it reaches nothing but `stigmergy.text` |
| `cli.py` | `stigmergy-queue` — the steward's view: list · show · claim · reclaim · requeue · resolve · reject · purge; `render.py`'s names are re-exported here for the CLIs that already take them from this module |
| `meeting_cli.py` | `stigmergy-meeting drop` — the ONE door onto the meeting flow: validate → upload the transcript as evidence → enqueue exactly one `kind="meeting"` row, and nothing else. See [meeting-distiller.md](./meeting-distiller.md) |
| `drive_cli.py` | `stigmergy-drive drop` — the ONE door onto the Drive flow ([ADR 028](../decisions/028-drive-door.md)): fetch one Drive file with the operator's own Google auth, upload the ORIGINAL bytes to evidence, enqueue exactly one `kind="drive"` row. It runs no model and performs no conversion — extraction is the worker's |
| `drive_client.py` | the Drive seam the drop CLI fetches through; it talks to `gog`, never to Postgres |
| `errors.py` | the domain exceptions (`SubmissionRejected`, `ReplyRejected`, `EvidenceError`, `QueueStateError`), all under `CaptureError` |

**Layering.** `capture` must never import `stigmergy.server` or `stigmergy.answer`; `stigmergy.server`
imports `capture`. The outward edge is to `stigmergy.index`, with one rule: **only the three
operator CLIs may import `stigmergy.index`** — `capture.cli`, `capture.meeting_cli` and
`capture.drive_cli`, asserted by `tests/test_architecture.py::test_only_capture_cli_may_import_the_index`.
Library code here never opens a connection and never reads the environment: every function takes
`conn`, and an entry point supplies it. `drive_client` talks to `gog`, not to a database.

## The three MCP tools

| Tool | What it does |
|---|---|
| `brain_submit(kind, material, hints?)` | queue a capture. Over MCP `kind` is `raw` (a conversation excerpt, a decision, a gotcha) or `page` (markdown you drafted) — `MCP_SUBMIT_KINDS`, deliberately narrower than the queue's own `KINDS`, which also carries `meeting` and `drive`: those two are the drop CLIs' to enqueue, and restricting them here rather than leaving it to `KINDS` is what keeps `stigmergy-meeting` and `stigmergy-drive` genuinely the only doors onto their flows. `hints` optionally suggests placement — `type`, `path`, `entity`, `title`, suggestions only. Three further allowlists exist for the surfaces that carry provenance rather than placement (`SOURCE_HINT_KEYS` for Slack, `MEETING_HINT_KEYS` for the meeting drop CLI, `DRIVE_HINT_KEYS` for the Drive one). Two of the three name the small subset a downstream reader actually TRUSTS, and only those are refused at the client seam: `SOURCE_PROVENANCE_HINT_KEYS` (`source_client`, `source_permalink`) and `DRIVE_PROVENANCE_HINT_KEYS` (`drive_file_id`, `drive_url`). `MEETING_HINT_KEYS` has no such subset — a meeting row can only be created by the drop CLI in the first place. `ALLOWED_HINT_KEYS` is the union of all four lists, and anything outside it is refused by name. Returns an ack with the submission id, the archived object key and a message that promises exactly what happened: **queued and attributed**, not "saved" |
| `brain_submissions(limit?, status?)` | what happened to what you captured: your own submissions, newest first, with state, timestamps, `result_ref`, any open `question` (plus `reply_hint`, the exact call that answers it), your `reply` once you have given one, `waiting_on`, the row's `events`, and a fenced excerpt. An unrestricted (steward) identity sees the whole queue with `mine` marking its own rows. A capture refused for a secret or PII echoes nothing — see [Withheld material](#withheld-material) |
| `brain_reply(submission_id, answer)` | answer the librarian's one question about a `needs_input` capture. Only the **original submitter or a steward** may reply; every other case — a stranger, a nonexistent id, somebody else's row — gets one identical, generic refusal that confirms nothing. A row that is not `needs_input` refuses *specifically*, but only for a caller already authorized to read it. The answer is bounded at 2000 characters, recorded on the row, traced with the actor, and the row returns to `queued` |

All three ride `BrainService._call`, so they inherit per-identity rate limiting, the audit row and
the error shaping the read tools have — one seam, not a second write path.

### The ask-back loop

When the librarian cannot resolve which entity a capture is about, worker **code** — not the agent
— routes it: a first `unresolved-entity` outcome finishes `needs_input` with a code-built question,
and everything else parks in `triage`. The question names what could not be resolved, lists the
registry's entities with their aliases (in full below 20 of them; above that it names the count and
asks for the exact name — never a silently truncated list), and states the call that answers it.

**One ask per capture, ever.** The budget is the `asked_at` column, so it survives a reply, a
steward's `requeue` and a lease redelivery alike; a replied capture that still cannot be resolved
parks in `triage`.

**The reply is data, not instructions.** It reaches the agent fenced and labelled as
`UNTRUSTED-DATA` on the next pass, and it bypasses nothing: the anchoring gate still resolves names
through the registry, and `page.stamp_server_fields` still deletes and rewrites every server-owned
key on the page — `status`, `as_of`, `submitted_by`, `entity`, `acl`. `verification` is in that set
too and is **stripped**, never written: nothing computes a verdict, so no page may claim one. The
audit row for a reply carries the answer's **size and hash**, never its text.

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

## The queue

```sql
capture_queue(id, kind, payload jsonb, blob_refs text[], submitted_by, hints jsonb,
              status, attempts, created_at, claimed_at, finished_at, result_ref, error,
              report jsonb,                                   -- the librarian's account
              asked_at, parked_at, reply, trace jsonb,        -- the human loop
              outcome jsonb)                                  -- the parked agent outcome
```

`outcome` is the agent's structured account of its last pass over THIS capture, kept across a park
so a re-file reuses it instead of re-reading the material — without it the park→resolve→re-file
loop loses distilled content, and loses more the longer the material is.

| Status | Means | Set by |
|---|---|---|
| `queued` | waiting for the librarian | submit, the expiry sweep, a reply or `requeue` |
| `claimed` | a worker holds a lease on it | `claim_next` |
| `filed` | a page exists; `result_ref` points at it | the librarian |
| `rejected` | a gate refused it, or a steward declined it; `error` says why | the librarian, or `stigmergy-queue reject` |
| `resolved` | a steward handled it **outside the fast lane** — not a rejection; the report names what happened and, where there is one, the page or commit | `stigmergy-queue resolve` |
| `needs_input` | a question is waiting on the submitter; `error` carries it, rendered as `question` | an `unresolved-entity` outcome on a capture that still has its one question |
| `triage` | a steward has to decide where it belongs | the librarian |
| `failed` | the librarian could not finish the item, or the attempts ran out; an `ingest_errors` row has the detail (stage `librarian` in the first case, `claim` in the second) | the librarian, or the expiry sweep |

Terminal = `filed`, `rejected`, `resolved`, `failed`. `needs_input` and `triage` are **parked
awaiting a human**, not terminal: retention never deletes material a person is about to be asked
about. `resolved` is purged on the ordinary 30-day window like any other terminal row. `error` is
the one "why is this row where it is" field, rendered by two names on the way out — `question` for
a `needs_input` row, `error` for `failed`/`rejected`/`resolved`
([ADR 014](../decisions/014-capture-queue-and-attribution.md)).

### The human loop's four columns

All four are additive (`ALTER TABLE … ADD COLUMN IF NOT EXISTS`) and NULL on rows written before
they existed; there is no backfill.

| Column | Means |
|---|---|
| `asked_at` | when this capture's **one** ask-back question was asked. Stamped on the first transition into `needs_input` and never cleared — not by the reply, not by a `requeue`, not by a lease redelivery. That is what makes "one ask per capture, ever" survive all three. |
| `parked_at` | when the row entered its **current** park, so `list`/`show` can say how long a human has been waited on. Distinct from `created_at` (when the material arrived) and from `finished_at` (which stays NULL on a parked row, because retention counts from it). |
| `reply` | the submitter's answer, bounded at 2000 characters (`schema.MAX_REPLY_CHARS`). Withheld from every read surface whenever the excerpt is — see [Withheld material](#withheld-material). It is the submitter's own free text, scanned by nothing, so a row whose material may not be read back must not hand back the sentence they wrote about it either. |
| `trace` | what **humans** did to the row: `asked`, `replied`, `requeued`, `resolved`, `rejected` — each with an actor, a note and a database timestamp. `audit_log` records the call; this records the row. Bounded at 20 events, oldest dropped. |

`status`'s CHECK constraint is named (`capture_queue_status_check`) and swapped when a new state has
to join it — the one migration here that is not a plain `ADD COLUMN`. It must stay a single
`DO $$` block (ONE statement, ONE transaction): a drop-then-add on an autocommit connection opens a
window with no constraint, pays an `ACCESS EXCLUSIVE` validation on every process start, and races
between concurrent starts. Its guard, built from `STATUSES`, makes it a no-op after the first run,
and the predicate only ever widens so the previous release keeps working across it.

### Claiming, leases and `attempts`

`claim_next` is a single `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`:
N parallel claimers against M queued rows produce exactly M claims, no row claimed twice.

A claim is a **lease**: `claimed_at` plus the visibility timeout the claimer states. `claim_next`
defaults to 300 s (a human-scale `stigmergy-queue claim`); the librarian's worker claims with its own
derived lease, 900 s by default, because one item can run two agent attempts plus the gates
(`librarian.config.minimum_visibility_timeout_s`). A worker that dies mid-item leaves a stale claim
the next claimer's sweep returns to `queued`. That sweep also runs in the librarian's worker loop
and standalone as `stigmergy-queue reclaim`; `queue.release_expired` has **no default** horizon,
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

`blob_refs[0]` is always the text material. A Drive row carries a second key: `queue.submit`'s
`extra_blob_refs` appends the ORIGINAL document bytes at `blob_refs[1]`, so the row's material (the
deterministic manifest dedup keys on) and the artefact the worker converts are separate objects.
Operator CLIs only — the MCP transport never passes it.

The blob is written **before** the queue row, and the order is load-bearing: an orphan blob is
inert and gets reused by the next identical submission, whereas a row pointing at a blob that was
never written is a capture whose evidence cannot be produced on demand.

**Both drop CLIs refuse to write across a split deployment.** `stigmergy-meeting drop` and
`stigmergy-drive drop` check, before any fetch or upload, that the queue's database host and the
evidence endpoint can plausibly belong to the same deployment (`capture.cli.refuse_split_stores`,
`evidence.split_stores_reason`, shared by both doors) — a remote queue paired with a loopback
evidence endpoint is refused with exit `3`, since a deployed worker can never reach a store on the
operator's laptop and the capture would fail with `NoSuchKey` seconds after being claimed. The
escape hatch is `--allow-split-stores`. `stigmergy-queue` carries no such guard: none of its
subcommands upload evidence.

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

## `stigmergy-queue` — the steward's view

```sh
.venv/bin/stigmergy-queue list                          # depth per status + the newest submissions
.venv/bin/stigmergy-queue list --status queued --limit 50
.venv/bin/stigmergy-queue show 7                        # one submission's trace and latencies
.venv/bin/stigmergy-queue claim --hold 60               # take one item; hold the lease, file nothing
.venv/bin/stigmergy-queue reclaim --visibility-timeout 0    # release EVERY claimed row, right now
.venv/bin/stigmergy-queue reclaim --visibility-timeout 900  # ...only ones past the worker's lease

# the drain: the three things a human can do with a PARKED row
.venv/bin/stigmergy-queue requeue 7 --by steward --note "registered the entity, try again"
.venv/bin/stigmergy-queue resolve 6 --by steward --note "folded into the person page by hand." \
    --page "wiki/entities/Ada Lovelace.md" --commit 9f8e7d2
.venv/bin/stigmergy-queue reject 9 --by steward --reason "duplicate of an existing customer page"

.venv/bin/stigmergy-queue purge --dry-run               # retention: what would go
.venv/bin/stigmergy-queue purge                         # retention: delete payload+hints
```

### The drain

The three dispositions share one guarded transition (`queue.dispose`) and one set of rules,
enforced in SQL rather than in the CLI:

- they move a row **only** out of `triage` or `needs_input` — a `claimed` row is refused by name
  ("a worker may be mid-item") and a terminal row is refused too;
- they **never touch `attempts`**, so the lease fence stays monotonic;
- they record the actor and the note on the row's own `trace`, which `show` prints.

`--by` is **attribution, not authorization**: the CLI records who you say you are and does not
check it — an operator with the DSN already has the database.

**A `triage` row that is an identity question** — an unresolvable entity
(`SITUATION_UNRESOLVED_ENTITY`) or a page type the fast lane does not file
(`SITUATION_UNSUPPORTED_TYPE`) — can still be drained with the three commands above, but minting a
new entity from one goes through `stigmergy-entities` (`approve`/`reject`, the only writer of
`ops/entity-registry.json` and `wiki/entities/` in this codebase): see
[operator-runbook.md → Governed entity birth](./operator-runbook.md#draining-parked-rows)
and [ADR 016](../decisions/016-human-loop-and-entity-governance.md). `stigmergy-entities reject`
and its `--requeue` flag ride this same `dispositions.reject`/`.requeue` seam.

> **`--note` and `--reason` reach the submitter's report verbatim.** They never touch the material
> path, so gitleaks and the PII gate never see them. `dispositions.clean` sanitizes (control
> characters) and bounds (500 characters) them on every disposition — not in the CLI, so
> `stigmergy-entities reject --reason` gets the same cleaning. What they SAY is the steward's
> responsibility, and both `--help` strings say so.

`resolve` with neither `--page` nor `--commit` still works but warns: it leaves the submitter's
report silent about where their material went.

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
`payload_purged: true` and returns no excerpt). `outcome` is on the list because it holds the full
drafted body of every page a distillation produced; `finish` and `dispose` clear it on every
terminal transition, so retention is the belt-and-braces layer. It is deliberately **not** part of
the eligibility guard, which stays `payload IS NOT NULL OR hints IS NOT NULL` so the purge is
idempotent.

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

Some rows are listed with **no excerpt, no `reply` and no client hints**. `withheld_reason` carries
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

Every other state — `needs_input`, `triage`, `filed`, `resolved`, and a `rejected` row for any other
reason (`duplicate`, `steering`, `steward`) — shows its excerpt normally: `needs_input` and `triage`
are the two states where a submitter must re-read what they sent to answer the question, or a
steward must to triage it. The window is keyed on "has the gate run", never on `TERMINAL_STATUSES`.

Four properties, each load-bearing:

- **Suppressed, never redacted, and never truncated around the match** — gitleaks reports a rule
  and a line, not a guaranteed span, so a redaction would be a guess presented as a guarantee.
- **Decided in the query** (`capture.queue._MATERIAL_WITHHELD`), not at each surface, so the value
  never leaves Postgres for a withheld row. Both reads of the `reply` column — `query_submissions`
  and `get_submission_trace` — go through that one expression. The hints are narrowed rather than
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
  terminal/parked transition, guarded by state **and** fencing token. Pass the `attempts` value the
  claim returned; `expected_attempts` is required, not optional.
- `capture.queue.query_submissions` — the ONE listing query (scope, status filter, ordering,
  paging). New surfaces attach through `list_own_submissions` / `list_all_submissions`.
- `capture.evidence.content_key(bytes)` — the key scheme, as a pure function.
- `capture.schema.prepare_submission` — the submission contract, pure and DB-free: size cap,
  kind/hint validation, server-owned refusal, frontmatter flagging.
- `capture.evidence.MemoryEvidenceStore` — the offline double, so a test needs no bucket.

## Avoid / anti-patterns

- **Never take `submitted_by` from client input** — not from an argument, `hints` or frontmatter.
  It comes from the resolved identity or the submission does not happen.
- **Never open a second write path.** All three MCP tools go through `BrainService._call`; a
  surface that writes to `capture_queue` directly skips rate limiting, the audit row and
  attribution at once. The three operator CLIs (`stigmergy-queue`, `stigmergy-meeting`,
  `stigmergy-drive`) are the declared exception: they hold the DSN, so `--by` there is
  attribution, not authorization.
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
`test_dispositions.py`, `test_evidence.py` (on `MemoryEvidenceStore`), `test_latency.py` and
`test_adversarial_cat7.py` — forged frontmatter, one of the three armed adversarial categories,
named `test_adversarial_cat7_*` so a `-k` collection finds every case. The `_pg` and CLI suites
need the real database: **exactly-once claiming** needs real Postgres (`FOR UPDATE SKIP LOCKED` is
the mechanism) and **the evidence store's dedup** needs a real bucket for the object-count
assertion.

`scripts/e2e_write.sh` (`make e2e-write`) is the compose-level proof: empty volumes → submit over
the real MCP stdio protocol → archive → index rebuild → parallel claimers → a killed worker →
retention.
