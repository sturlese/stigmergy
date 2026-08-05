# ADR 014 — The capture queue, server-side attribution, and the evidence plane

**Status:** accepted · 2026-07-25

## Context

The brain could be read from anywhere and written by nobody. Reading was finished: staging served
search, read and `ask` over HTTP with per-user tokens ([ADR 013](./013-http-transport-and-token-auth.md)).
Writing had exactly two paths and neither was a capture path — a batch pipeline over Drive, and
hand-editing markdown in a clone until the linter was happy. Everything that happens inside a
conversation (a decision, a gotcha, the reason something was rejected) died in that conversation.

Three specific things were missing, and each one is unsafe to build without the other two:

- **No queue.** Nowhere to put a capture, so nothing to serialize and nothing to retry. Fifty
  writers committing straight to git is the failure mode a single-writer design exists to
  prevent; the alternative to it was "don't write".
- **No attribution.** `submitted_by` was a frontmatter field in the page contract that no code
  produced. The whole fast-lane governance model rests on it: a `developing` page is trusted
  *because* a named person put it there. And a token is write power, not just read power.
- **No evidence archive.** The librarian's distillations have to be verifiable against the source
  material, and git must not hold the raw conversation — git cannot delete.

This ADR is the **front half**: the queue, the submit surface, attribution, the evidence plane, the
operational spine and retention. The librarian that drains it is
[ADR 015](./015-librarian.md). The seam between them is the queue contract, written here and
consumed there — the librarian cannot be built or tested without it, because the queue is its
input.

## Decision

### 1. A durable queue in the same Postgres, with a single writer draining it

A capture is inserted into `capture_queue` and acknowledged immediately; a librarian claims it
later and files it. Honest asynchrony: the submit ack promises **queued and attributed**, never
"saved to the brain", because nothing is in the brain until the librarian files it.

The queue lives in the same Postgres as the index — one database, no second dependency — but it
is **the durable half of a database whose other half is a cache**. `pages_index` is dropped and
rebuilt on every `stigmergy-index --rebuild`; `capture_queue` holds material that exists nowhere
else. `store.init_schema` therefore drops `pages_index` **by name**, `schema.DURABLE_TABLES`
names the four tables a rebuild must leave standing, and a test asserts they survive
one. This is where "the database is disposable" stops being true, and it is written down in three
places (this ADR, `index/store.py`'s DROP, `capture/schema.py`) precisely because a future
shortcut would be silent.

**Rejected: a dedicated queue technology** (SQS, Redis, a broker). `FOR UPDATE SKIP LOCKED` is
exactly the primitive needed, the database is already there and already backed up, and a queue in
the same transaction domain as the audit trail is easier to reason about than one that is not.
Revisit if the write path ever outgrows one machine — nothing here assumes it will not.

**Rejected: a migration framework.** Four tables, `CREATE TABLE IF NOT EXISTS` at startup, the
same ownership pattern `index/store.py` and `server/audit.py` already use. Introducing Alembic
for four tables would add a second, competing notion of who owns the schema.

### 2. Claiming is exactly-once, and a dead worker loses nothing

`claim_next` is one statement: `UPDATE ... WHERE id = (SELECT id ... FOR UPDATE SKIP LOCKED LIMIT
1)`. Each claimer locks a different row and skips the ones its peers hold, so N parallel claimers
against M queued rows produce exactly M claims with no row claimed twice. There is no window
between "chosen" and "claimed" for a second claimer to see the row free. This property lives in
Postgres, not in our code, which is why the test for it must run against real Postgres — a double
that fakes `SKIP LOCKED` proves the double.

A claim is a **lease**, not a transfer: `claimed_at` plus a visibility timeout. A worker that dies
mid-item leaves its row `claimed` with a stale `claimed_at`; the next claimer's sweep returns it
to `queued`. The sweep is on the claim hot path deliberately — a recovery mechanism that only
runs from a cron is a recovery mechanism that rots — and is also callable directly
(`stigmergy-queue reclaim`, and the librarian's worker loop).

**A lease needs a fencing token, and `attempts` is it.** Exactly-once *claiming* does not by
itself give exactly-once *finishing*, and the first draft of this ADR claimed a property it did
not have — caught in review and fixed before ship. `finish()` originally guarded only
`status = 'claimed'`, which is a state check, not a lease check. The hole: A claims row 5
(delivery 1) → A stalls past the visibility timeout → the sweep requeues it → B claims it
(delivery 2) → A calls `finish()`. The row *is* `claimed`, so A's stale write lands, silently
stealing B's item; B then fails loudly having possibly already filed the same capture.

`attempts` is monotonic per delivery and is incremented only by the claim statement, so the value
handed to a worker at claim time names *that* delivery and no other. `finish()` therefore requires
`expected_attempts` and matches `status = 'claimed' AND attempts = %s`; a stale finish updates
nothing and raises, with a message that distinguishes "redelivered, your lease is gone, discard
this run's work" from "this row was never in flight". Required rather than optional on purpose: an
optional fence is a fence nobody passes, and this failure is silent duplicated work rather than a
loud error. With one worker it is improbable; with N it is structural — and the fence is what makes
N>1 safe if it is ever needed. `release_expired` passes it for free (its rows are locked
`FOR UPDATE` in the same transaction, so the value cannot have moved).

**`attempts` counts deliveries, not failures**, and is incremented at CLAIM time. A worker that
dies before writing anything still burns an attempt, which is what stops a poison item from being
redelivered forever; incrementing on release instead would lose the count whenever the sweep
never ran. When the attempts are exhausted the row goes `failed` and an `ingest_errors` row
records the stage and the count.

### 3. The status enum is written in full up front, though the queue itself sets only three values

`queued · claimed · filed · rejected · needs_input · triage · failed`. The queue performs
`queued`, `queued → claimed`, `claimed → queued` (expiry) and `claimed → failed` (attempts
exhausted). The rest is the contract the librarian fills, including the two states the anchoring
contract needs: `needs_input` (the librarian asks the submitter a question — a zero-anchor capture
is never filed ownerless) and `triage` (a steward decides). Writing the vocabulary up front means
the consumer is built against a fixed contract instead of inventing one, and it means the `CHECK`
constraint is written once rather than migrated later.

`needs_input` and `triage` are deliberately **not terminal**. They are parked awaiting a human,
and retention must never delete the material a person is about to be asked about.

An eighth value, `resolved` — a steward handled the capture outside the fast lane, which is not the
same thing as refusing it — was added later by
[ADR 016](./016-human-loop-and-entity-governance.md), through the same guarded single-statement
`CHECK` replacement this decision anticipated.

### 4. Attribution is the server's, and forging it is an error rather than a no-op

`submitted_by` is the resolved caller identity — the `--identity` name over stdio, the token's
email over HTTP — read from `BrainService.identity`, the same value that already keys every audit
row and every rate-limit bucket ([ADR 013](./013-http-transport-and-token-auth.md)). There is no
code path from client-controlled bytes to that column: `capture.queue.submit` takes `submitted_by`
as an argument the service supplies and has no way to learn an identity from the input. **That is
the security property**, and it is structural.

On top of it, three refusals, because "quietly ignored" is not good enough for the one field
authorship rests on:

- A server-owned field arriving as a **tool argument** is an explicit error. This required
  declaring the field on the tool signature so that it *can* be refused: FastMCP builds its
  argument model with pydantic's default `extra="ignore"`, so an unexpected argument is dropped
  silently by the SDK before our code ever sees it. Four fields are declared — `submitted_by`,
  `verification`, `acl` and `content_hash` — each documented in the tool description as
  server-owned; passing any fails with no row and no blob created. (The first cut declared only
  `submitted_by`, which left the other three quietly accepted; caught and fixed before ship.)
  `verification` outlived the thing it named: nothing computes a verdict any more
  ([ADR 026](./026-the-purge.md)), and the parameter stays declared precisely so that passing one
  is a loud error rather than something silently ignored.

  **The residual, stated rather than papered over**: a field that is *not* declared is dropped,
  not refused. What covers those is the structural guarantee — no code path reads client input
  into a server-computed column — not the trap. The operational consequence: a future server-owned
  field added to the queue contract without a matching declared parameter would be silently
  swallowed, so the parameter must be added with the field.
- A server-owned field arriving inside **`hints`** is the same error. `hints` is an allowlist
  (`type`, `path`, `entity`, `title`) rather than a free-form bag, so an unknown key also gets a
  clear message — the same posture `search_brain` already takes for an unknown filter name. Later
  doors joined that allowlist with their own small, string-valued groups rather than opening a
  second metadata channel ([ADR 017](./017-slack-transport.md) D5,
  [ADR 020](./020-meeting-distiller.md), [ADR 028](./028-drive-door.md) D7), so the four names
  above are the *base* group and `ALLOWED_HINT_KEYS` is their union.
- **Frontmatter inside a pre-drafted page is recorded, never trusted.** The material is stored
  verbatim; the declared fields land in `hints.declared_frontmatter` and the server-owned subset
  in `hints.flagged`, which the ack echoes back so the submitter learns their document did not get
  to set them.

The frontmatter scan is deliberately **not YAML**. `index/corpus.split_frontmatter` uses
`yaml.safe_load`, which is right for a repo checkout this system produced; this input is
attacker-controlled text arriving over a public boundary, and `safe_load` still expands anchors
and aliases (a 256 KB billion-laughs payload fits inside our size cap). A shallow, non-recursive
`key: value` scan cannot be made to allocate. It is best-effort by design: an exotically quoted
`submitted_by:` may go unflagged, and that costs a note, never the security property — because
attribution never reads this dict.

The audit row records the **names attempted** (`server_owned_args_present: ["submitted_by", …]`)
without recording their values: the attempt is the security signal, and the value is somebody's
identity, ACL label or trust claim.

### 5. The evidence plane archives the capture's own raw material, not client binaries

Every submission writes its material to an S3-compatible store at
`sha256/<ab>/<cd>/<hash>` and records the key in `blob_refs`. Content addressing means identical
material submitted twice yields **two rows and exactly one object** — the bytes deduplicate for
free, while dedup against the *graph* stays the librarian's judgment (two identical submissions
are two rows on purpose).

Client-sent binary attachments are excluded. No client can send one: the flows that handle
attachments compose or fetch their material server-side rather than accepting bytes over the tool
boundary, so client-side binary transport would have been built for a synthetic test. `blob_refs`
is already an array, so adding a second ref later changes no contract — and that is exactly what
happened: the Drive door ([ADR 028](./028-drive-door.md)) keeps the original document bytes as a
second ref beside the text manifest the row's material is built from.

**Write order: blob before row.** The failure modes are not symmetric. An orphan blob is inert and
content-addressed (the next identical submission reuses it); a row pointing at a blob that was
never written is a submission nothing can check against its source. Validation runs before both,
which is what makes "no row and no blob" true for every refusal — a rejected or rate-limited
submit leaves nothing behind.

MinIO locally, R2 in staging, one code path. Errors crossing the network are reduced to a class
name inside `EvidenceError` itself: the exceptions boto3 raises embed the endpoint, the bucket and
the access key id, and putting any of those on the wire is exactly what the generic-refusal rule
forbids.

### 6. Both tools ride the existing service seam

`brain_submit` and `brain_submissions` go through `BrainService._call`, exactly like the read
tools, so they inherit per-identity rate limiting, the audit row and the error shaping without a
second enforcement path. The 30 req/min overall bucket covers writes with no new configuration; a
refused submit creates no row and no blob and returns the same generic refusal shape as a refused
read.

The audit row records the act and never the content: `kind`, the material's byte size, its
sha256 — the same hash the evidence key is built from, so the trail joins up to the archived
object — the hint **keys**, and the names of any server-owned arguments attempted. No captured
text reaches `audit_log`.

`brain_submissions` is scoped by SUBMITTER, not by ACL audiences: a submitter sees their own
submissions, and an unrestricted (steward) identity sees the whole queue with `mine` marking its
own rows. The scope decision is made once, from the resolved audience scope, and the tool has no
`submitted_by` filter at all — so a scoped identity has no way to ask for another identity's
rows. Echoed capture text is fenced with the same `UNTRUSTED-DATA` helper page bodies use: an
echoed capture is untrusted data like any other.

### 7. Retention deletes the material and keeps the trace

`stigmergy-queue purge` nulls `payload` and `hints` on rows terminal for more than 30 days. `id`,
`submitted_by`, `status`, the three timestamps, `attempts` and `result_ref` survive, so the
per-submission trace and the capture→page latency measurement stay intact and a purged submission
is still readable as history. Keeping raw writing forever accumulates other people's material with
no policy; deleting the row would destroy the record that it ever happened.

The evidence blob is **not** touched: it has its own lifecycle (the bucket's retention policy),
because checking a filed page against its source has to remain possible after the queue row is
stripped.

Honest caveat: the UPDATE removes the value from the live row immediately, but the previous row
version survives as a dead tuple until autovacuum reclaims it, as with any Postgres delete. For a
hard guarantee (a data-subject request) that is a `VACUUM` away, documented in
[capture.md](../reference/capture.md) rather than performed on every purge run.

### 8. Audit keys are truncated too, not only audit values

`_truncate_for_audit` bounds dict **keys** as well as values: `filters` is a dict whose keys are as
client-controlled as its values, `search_brain` rejects an unknown filter NAME, and that rejection
is itself audited — so a multi-MB key would have landed in the JSONB row through the very path the
value cap was added to close.

### 9. Every client-controlled container is bounded by SIZE, COUNT and DEPTH

Bounding the *length of each string* an argument carries is not enough. This is the decision that
legitimizes large bodies, and a bound on string length alone leaves three ways to be expensive
without any single string being long. All three are closed here:

- **Count.** The audit row records hint key *names*, read off the raw client dict before
  validation and written whatever the outcome — so a `hints` dict with 100k keys produced a
  multi-MB JSONB row on a call that was about to be rejected anyway. Now capped at 32 names plus a
  `...[N more keys]` marker (the legal hint names are a short, closed allowlist an order of
  magnitude below the cap, so a marker here always means the caller was doing something odd, which
  is itself worth recording).
- **Depth.** `_truncate_for_audit` recursed without a limit, so a few KB of `[[[[…` raised
  `RecursionError` — inside `_call`'s `finally`, where it would have replaced the caller's real
  result with an audit-shaping crash. Now bounded at 20 levels with a marker, which also keeps
  `Jsonb`'s own recursive serialization inside the same bound. Belt and braces: the shaping call
  is additionally wrapped so *any* failure logs loudly and still writes a row, rather than
  surfacing through the served call.
- **Body size.** Nothing below the auth middleware bounded an HTTP request body: the MCP SDK does
  `await request.body()` with no limit, uvicorn imposes none, and the 256 KB material cap fires
  only *after* the whole body is buffered, parsed and hashed — an OOM lever on one small machine.
  A declared `content-length` over 1 MiB (4× the material cap plus envelope) is now refused with a
  generic `413` **before** any of the body is read, and a chunked body with no declared length is
  cut off at the same bound as it streams.

The through-line: **put the contract ahead of the buffering, not behind it.** A limit that is
enforced only after the expensive thing has already happened is documentation, not a limit.

## Consequences

- **The database stops being disposable.** Until now every byte in Postgres could be rebuilt from
  the repo. The hosted Postgres becomes the only copy of material not yet filed. Mitigations: short
  retention, the explicit "the repo is still the canonical store" boundary, and a backup drill —
  but the exposure starts here.
- **The bucket holds unfiltered material.** The PII/secrets gate is at commit time, so between
  submit and filing the raw capture lives in Postgres and in the bucket. This is deliberate: git is
  what must stay clean, because git cannot delete, and bouncing at submit as well would put the
  same rule in two places. The consequence the operator owns: the bucket's retention policy must be
  set **before** other people's material enters it.
- **A token is now write authority.** The revocation path (drop the hash from the store) was
  designed for read access; the blast radius of a leaked token grows here. No new mechanism is
  proposed — but the risk changes character.
- **`stigmergy.capture` is a new subsystem** and a new layering edge: it must never import
  `stigmergy.server` or `stigmergy.answer`; `stigmergy.server` imports it. The one outward edge is the
  operator CLI → `stigmergy.index.store`, the connection seam (the later drop CLIs share that one
  exemption for the identical reason), and library code in the package never opens a connection.
  Text hygiene is not on that edge: `sanitize`/`clamp` live in `stigmergy.text`, which imports
  nothing from this project, so cleaning a string never means reaching into the search index.
  `tests/test_architecture.py` is what makes all of that real.
- **The MCP tool surface grows by two, additively** — no existing tool changes its name, arguments
  or response shape. The only consumers that notice are the three tests that assert the tool set
  exhaustively.
- **Captures pile up with nothing draining them** until the librarian exists. Honest states and
  honest copy are the mitigation: `queued` means queued, and while nothing drains it that is a
  truthful and complete answer.

## Alternatives rejected

- **Deduplicating at submit.** Two identical submissions create two rows on purpose. Dedup is
  judgment against the graph and belongs to the librarian; content addressing already
  deduplicates the bytes, which is the part that actually costs storage.
- **A PII/secrets gate at submit time**, for faster feedback. One gate, at the boundary that
  matters (git), rather than the same rule implemented twice in two places that can drift.
- **`search_brain` peeking at the pending queue** to bridge the freshness gap. Deferred: measure
  the gap before designing around it.
- **A `question` column beside `error`.** The column list is the contract the librarian builds
  against, and a second free-text column would fork "why is this row where it is" into two fields
  that can disagree. One column, rendered by two names on the way out (`question` for
  `needs_input`, `error` for `failed`/`rejected`).
- **A UUID primary key.** `BIGSERIAL`, matching `audit_log`. Sequential ids are enumerable, but at
  this point no tool takes an id as an argument — `brain_submissions` lists your own and nothing
  else — so there is nothing to enumerate. The rule stated for the future was that a tool which
  DOES take an id must scope it by submitter, the way `queue.get_submission_trace` already takes an
  optional `submitter` and returns the same "not found" shape for another identity's row as for a
  nonexistent one. `brain_reply` is that tool, and it holds the rule: only the original submitter
  or a steward may answer, and every other case — a stranger, a nonexistent id, somebody else's
  row — gets one byte-identical refusal that confirms nothing.
