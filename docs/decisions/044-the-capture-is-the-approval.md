# ADR 044 — the capture is the approval

- **Status**: accepted
- **Date**: 2026-08-21
- **Supersedes**: [ADR 016](./016-human-loop-and-entity-governance.md) in its governance half
  (a steward confirms what the librarian proposed — the three repo-sourced inputs read at the base
  commit stand); [ADR 030](./030-server-side-entity-minting.md) whole (the server-side decision
  doors); [ADR 039](./039-governed-repair-loop.md) in D1's "a HUMAN decides" clause and every door
  built for it (the kinds, the validators, the appliers and the nine-gate apply stand).
- **Amends**: [ADR 028](./028-drive-door.md) (the Drive door — original bytes fetched at the door
  with the operator's own auth and converted at the worker: replaced by D4);
  [ADR 041](./041-file-first-govern-after.md) ("file first" stands word for word; "govern after" is
  withdrawn, because nothing is left to govern); [ADR 042](./042-an-entity-is-born-written.md)
  (stands; the steward who registers becomes any submitter); [ADR 043](./043-a-sweep-is-written.md)
  (D1, D3 and D5 stand; D2's "in the act" becomes "in the worker's act", and D4's model runs where
  every other model of the write path runs).
- **Related**: [ADR 014](./014-capture-queue-and-attribution.md) (the queue),
  [ADR 015](./015-librarian.md) (the one writer), [ADR 024](./024-gardener-digest.md) (findings
  and the digest), [ADR 021](./021-views.md) (convergence). Tracked as #134.

## Context

On 2026-08-21 an inventory of the write path counted its doors. A proposed identity could be
decided from four places — `review_decide` over MCP, the doorbell card in Slack, the console's
Entities desk and `stigmergy-entities` from a clone — under two authorization models: the first two
asked `ops/stewards.json`, the other two asked nothing (a token, a `--by`). A repair proposal waited
silently, since the doorbell never rang for one (ADR 039's no-ring decision), so the nightly
proposer's work sat in a queue nobody was told about. A meeting transcript and a Drive document
could each enter through exactly one door, an operator CLI, reachable from neither the MCP server
nor the console — the two kinds this system files best were the two nobody remembered how to file.
Operating the write path had become a sequence of steps, each with a door of its own.

The second observation is ADR 043's, generalized. That record found that the second click on a
person's own deletion supplied an authentication, not a judgment. The same is true of everything
the librarian proposes. A capture is a human act: somebody read a thread, a transcript, a document
and decided the brain should hold it. What the librarian does next — name the entity the material
is about, spell it the way the material spells it, file the page, link it — is bookkeeping over
that decision, and the bookkeeping is judged by nine deterministic gates before it lands. The
steward in the inbox was reviewing the model's bookkeeping after code had already judged it, and
every click either confirmed what the gates had passed or undid one commit. A queue whose proposal
count is permanently non-zero is not governance; it is a backlog with a person's name on it.

The third observation is about the README's central claim — two narrow seams, one writer into git
and one API out of it. The review lane made the API process a second writer: a throwaway clone per
decision, pushed with the librarian App's credential, from the process that serves `ask`. ADR 030
accepted that for three doors that needed a commit on a click. With no click to serve, the second
writer has no reason to exist.

## Decision

### D1 — the capture is the approval

The identity a capture introduces is born CONFIRMED, by the person who captured: the librarian
writes the page with `approved_by` = the row's `submitted_by` — the token's email over HTTP, the
`--identity` over stdio, the reacting user's resolved email from Slack, the console's actor for a
registration. A spelling the material uses for a registered entity lands in that entity's
`aliases:`. A `register` hint (name, type, aliases) is accepted from every door and pins what the
librarian would otherwise infer; it carries no authority, because there is no authority left to
carry. The `proposed` state has no spelling anywhere: not in the registry
(`{name, type, aliases, approved_by}`), not in the page contract (`approved_by` stays and is never
empty; `proposed_aliases` goes), not in a ledger.

The ninth gate keeps its shape and flips its clause: a created entity page must be one this run
birthed AND carry a non-empty `approved_by`; a modified one still needs the byte-proof code
produced before the diff existed. What it refuses is unchanged — an entity page written by anything
but the birth writer, `repairable=False`.

### D2 — nothing is proposed to a person; a repair is applied

`stigmergy.repair` keeps its kinds, its validators and its appliers and loses its inbox. For the
latest completed gardener run past a watermark, the worker derives the repairs — the three model
roads and the code-derived duplicate-`sources/` deletion, exactly as the proposer did — validates
each against the real checkout, applies it through its kind's applier, the cross-check and the
nine gates, and commits: ONE commit per repair, App-authored, with a `Repair:` trailer naming the
finding. Then it records a `repairs` row — kind, ops, target paths, the finding, `content_key`,
`applied | failed | skipped`, the diff, the commit or the refusing sentence. `content_key` stays
the permanent memory: a key applied once is never derived again, and a key that failed is not
retried (a gate's refusal is deterministic for the same bytes) — the row says why. Two ceilings
bound a pass, `STIGMERGY_REPAIR_CEILING` and `STIGMERGY_REPAIR_MERGE_CEILING`, and what a ceiling
defers is counted and logged, never silent.

The review lane goes: `review_queue` and `review_decide`, the `review_decisions` and
`steward_notifications` tables, the Slack doorbell and its cards, the console's Inbox and every
verdict route, `stigmergy-entities` and `stigmergy-repair` whole. `ops/stewards.json` leaves the
knowledge-repo contract. The gardener's `sla` band and its Slack notice go with them — live code
with no producer, and the gardener's only reason to hold a Slack token.

### D3 — one writer: the worker

Every commit to the knowledge repo is the worker's. The API process queues and reads.
`brain_submit` queues a capture. `brain_delete` queues a `delete` row — the paths and the reason —
and the worker runs the plan, the written sweep, the gates and the commit (ADR 043 D1, D3 and D5
unchanged), with the per-page diff landing in that row's `report`, read through
`brain_submissions` and the console exactly as a filing's report is. `brain_delete` authorizes on
the one fact the server can check at submit time without a clone: the caller is an UNRESTRICTED
identity — no audience restriction in `ops/identities.json` — the only kind that can see every
page a removal touches, including the pages the sweep will rewrite, so no per-path guard is needed
and no refusal can reveal a referrer. The console's Remove pages queues the same row under the
console's actor.

`entities.remote`, `repair.remote`, `entities.clone` and the App credential on the `app` process
group go. A deployed server that cannot push is the point, not a degradation.

### D4 — every kind enters at `brain_submit`

`KINDS` is `raw`, `page`, `meeting`, `document`, and it is the one vocabulary: `MCP_SUBMIT_KINDS`
goes, because there is no second door left to keep narrower. A `meeting` carries the transcript as
material and `title`, `meeting_date`, `attendees` as hints, validated at the seam every caller
crosses. A `document` carries the document's TEXT as material and `title`, `source_url` as hints,
and the worker files it as the Drive flow filed an extracted document — a synthesis page plus the
verbatim `sources/` part(s), `source_kind: upload`, `url:` = `source_url`. The client extracts: an
agent with a Drive connector, a person with a file open, a script. Provenance asserted by a client
has the standing the material itself has — the submitter's claim, attributed to the submitter —
which is why `source_url` is accepted where `drive_url` was refused: `drive_url` promised a fetch
the server had verified, `source_url` promises nothing the submitter has not. The Slack pair
(`source_client`, `source_permalink`) stays door-gated: the transport asserts it, not a person.
Material caps are per kind — 256 KB for `raw` and `page`, 1 MB for `meeting` and `document` — and
the HTTP body cap follows. The evidence store keeps the text as received, content-addressed.

`stigmergy-meeting`, `stigmergy-drive`, `capture.drive_client`, `kernel.converters` (text
extraction and the vision OCR), `poppler-utils` in the image and the conversion term in the
worker's lease go.

### D5 — visibility after, not approval before

What replaces the inbox is a report. The digest's corpus deltas name the repairs applied — by
kind, with titles — beside the pages filed and the entities born; the console's Repairs page is
the `repairs` ledger with each diff; the Slack thread reply names the entities a capture bore;
`brain_submissions` carries a deletion's diff. The undo is `git revert` in the knowledge repo, and
a reverted repair is never re-derived (D2). There is no override file and no exception list: a
human's "no" is a commit.

### D6 — maintenance is the worker's night shift

The four scheduled jobs become worker passes: the view sweep (which also fires the moment the
queue goes idle after a filing or a repair, the interval as backstop), `garden` (the ten checks and
the three model passes, then the repairs of D2, then the views), the index rebuild and the
retention purge — each with a due time, a ceiling, a `job_runs` row and a cooperative stop, and
all of them only when the queue is idle. The Actions templates, the admin GitHub token,
`STIGMERGY_CRONS_ENABLED` and the console's dispatch/enable/disable levers go; the Jobs page reads
the passes off `job_runs`. `stigmergy-gardener`, `stigmergy-index --rebuild` and
`stigmergy-queue purge` stay as local commands.

**Implementation note (added when D6 landed).** This decision listed the index rebuild as a worker
pass AND as a local command, and only one of those could be built: `librarian.bootstrap` strips
`OPENAI_API_KEY`/`EMBED_API_KEY` before exec'ing the worker — deliberately, so the write path
cannot reach the read path's credential — so the worker cannot embed anything at all. The rebuild
therefore stays an operator command, the admin console names that command instead of offering a
button that could only ever fail, and the night shift is three passes plus the view sweep. Nothing
rebuilds the index on a schedule any more; the push webhook keeps it current between rebuilds, and
the console's Index page lints the live index on demand so drift is visible to whoever looks.

## What this deliberately does NOT do

- **It does not make a model the last word on anything.** Every diff still passes the nine gates;
  a contradiction repair still flags and never resolves; the ceilings are code deciding how much a
  night may change; a failed apply stays failed and visible.
- **It does not remove attribution.** A filing commit names its submitter; a repair commit names
  its finding; a deletion commit names the person who asked in an `Approved-by:` trailer.
- **It does not add an override.** No `distinct_from`, no exclusion file: a revert is the "no",
  and `content_key` makes it permanent.
- **It does not touch the read path.** `acl.visible()` stays the one place read access is decided;
  `brain_delete`'s unrestricted rule reuses `ops/identities.json`, it does not extend it.
- **It does not fetch anything for anybody.** No Google credential exists server-side, before or
  after. A document reaches the brain as text a client already holds.
- **It does not keep a steward for the one scary verb.** A merge of two registered entities is the
  repair with the largest blast radius, and it is bounded by its own ceiling, its own commit, its
  diff in the ledger and its line in the digest — not by a role file kept alive for one button.

## Consequences

- Eight MCP tools; ten entry points; no Actions workflow; `review_decisions` and
  `steward_notifications` dropped; `repair_proposals` becomes `repairs`. On the order of 5,400
  lines of source deleted outright, and the README's pinned counts move with them
  (`tests/test_readme_claims.py`, `tests/test_docs_claims.py`).
- The knowledge repo: `ops/stewards.json` deleted, the linter's `proposed` / `proposed_aliases` /
  stewards checks removed, the librarian's skill says "declare the identity" where it said
  "propose". The platform suite cannot see that half; the change is landed when both repositories
  are green.
- The worker's lease loses the conversion term; the console's derivation sentence and
  `WORKER_DEFAULT_LEASE_S` follow.
- The risks are named rather than softened. A wrong merge moves a page's history onto the wrong
  entity until it is reverted; a wrong alias resolves to the wrong entity until the gardener's
  identity pass or a revert; model prose lands on entity pages nobody read first — ADR 043
  accepted the same for the deletion sweep.
- The README's write-path diagram is redrawn: amber leaves the write path except at the caller
  of `brain_submit` and `brain_delete`.
- Phased, one commit per phase, the suite green at each; #134 tracks the phases.

## Alternatives rejected

- **Automate identities, keep the inbox for repairs.** The inbox's cost is the machinery — four
  doors, two ledgers, a doorbell, a console page — and keeping it for one kind keeps all of it for
  the kind that was never even rung for.
- **A confidence threshold before a model finding is applied.** A threshold is a second model
  opinion dressed as a gate. Gates judge bytes; a ceiling bounds a night; a revert undoes a commit.
- **A bytes path for documents over MCP.** A five-megabyte base64 blob is millions of tokens —
  unusable from an LLM client, which is the client — and it keeps three converters, a vision model
  and poppler alive for nobody.
- **A synchronous `brain_delete` in the API process, as ADR 043 built it.** Keeps a second writer,
  a clone per call and the App credential on a public process, for one tool.
- **Keep the crons in Actions.** Keeps the PAT, four templates, `STIGMERGY_CRONS_ENABLED` and a
  split between what the worker does unattended and what Actions does unattended — a split the
  periodic view sweep had already crossed.
- **"May remove what they can see."** Needs the referring set before the clone exists, and refuses
  in a way that reveals a referrer the caller may not see.
