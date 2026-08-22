# ADR 041 — file first, govern after: the librarian proposes entities instead of parking captures

- **Status**: accepted
- **Date**: 2026-08-21
- **Supersedes**: the parked half of [ADR 014](./014-capture-queue-and-attribution.md) (the
  `needs_input` and `triage` statuses and their "not terminal, awaiting a human" rule), the
  ask-back of [ADR 015](./015-librarian.md) (`brain_reply`, the one question a capture gets), the
  ask-once loop and the meeting park of [ADR 020](./020-meeting-distiller.md), and ADR 030's
  mint-from-a-parked-capture door ([ADR 030](./030-server-side-entity-minting.md) — the App-authored
  governed commit with a human named in a trailer STANDS; what it is driven by changes).
- **Amends**: [ADR 016](./016-human-loop-and-entity-governance.md) — an entity is still born
  through a human, but the human now confirms a page that already exists instead of authorising one
  that does not yet.
- **Related**: [ADR 039](./039-governed-repair-loop.md) (the review lane and its ledger, which this
  decision gives two more item kinds), [ADR 040](./040-ops-file-freshness.md) (the registry snapshot
  the inbox is derived from).
  Narrative: [`docs/reference/librarian.md`](../reference/librarian.md),
  [`docs/reference/capture.md`](../reference/capture.md),
  [`docs/reference/operator-runbook.md`](../reference/operator-runbook.md).
- **Amended by** [ADR 044](./044-the-capture-is-the-approval.md): "file first" stands word for word. "Govern after" is withdrawn — there is nothing left to govern, because what a capture establishes is written in the commit that files it.

## Context

The write path parked. A capture that named something the registry did not know stopped in
`needs_input` with one question to its submitter; an unanswered question, or an answer that did not
resolve, moved it to `triage`, where a steward could mint the entity from the parked capture,
requeue it, resolve it by hand or reject it. Seven statuses, two parked ones, three dispositions,
an ask-back tool, a Slack thread per question, a "reuse the parked distillation" rule for meetings,
and a console page whose whole job was draining what the other pages had parked.

Two failures drove the redesign, both observed on the staging deployment within a week:

- **Captures were lost to the ceremony.** Of five notes dropped through the MCP door, two reached
  "Resolve by hand" and were cancelled by the person who had written them. Nothing was wrong with
  the notes. Each named an unknown entity; the question went to a submitter who had already moved
  on; the park became a chore; the chore was dismissed. A company brain earns its keep by being
  used casually and often, and a write path that hands work back to the writer is exactly what
  stops that.
- **The same input behaved differently per door.** A name typed into a capture parked; the same
  name minted through the console was born at once; the same name in a meeting transcript parked
  the whole page SET. Three behaviours for one event is not governance, it is state.

The redesign's brief was one sentence: *the beauty is in the simplicity — file first, govern
after.*

## Decision

**D1 — a capture never waits on a person.** The librarian's outcome has one decision, `file`. A
name the registry does not know is not a reason to stop: the librarian **proposes** the entity — a
complete page under `wiki/entities/` (name, type, role, aliases, summary, facts, connections: every
field the reasoning could fill) carrying `approved_by: ""`, plus a registry entry marked
`proposed` — and anchors the capture's page to it in the same commit. The statuses are
`queued → claimed → filed | rejected | failed`; `needs_input` and `triage` are retired words the
queue refuses by name, and rows found in them at startup return to `queued` once. `resolved`
survives read-only on the rows that already carry it.

**D2 — a proposal is a page, not a row.** The pending set is whatever the registry marks
`proposed`; the inbox is DERIVED — registry (the index's snapshot, else the service file) joined to
`pages_index` for the summary and the anchored pages, and to the review ledger for who decided
what. No new table, no second source of truth, nothing to drain: approve it and it is gone from
the inbox because the registry no longer says `proposed`. Proposed entities are visible in search
and `list_entities` from the moment they are filed, marked as proposed, because hiding them would
hide the pages anchored to them.

**D3 — three verbs, one governed writer.** A steward **approves** (the page gets `approved_by:
<who>`, the registry drops the mark), **merges into** a registered entity (the proposal's aliases
join the survivor, every page anchored to it is re-anchored, the proposal page goes), or
**declines** (page and entry go). A proposed *spelling* — a name the librarian recognised as a
registered entity's alias — is its own item kind with approve and decline only. All four doors
(CLI `stigmergy-entities pending/approve/decline/merge`, the console's Entities desk, the Slack
card, MCP `review_decide` with `into`) run `entities.decide.apply`: preflight, drift refusal,
secrets scan, one commit, rollback via `clone.restore_tracked` if the push fails. The App authors it
with a `Decided-by:` trailer; the CLI signs with the steward's own identity. `gitcmd.commit`'s
gated-entries rule and the path-scoped writer are unchanged.

**D4 — the ledger is the librarian's memory of no.** A declined identity is recorded in
`review_decisions` (kind `identity-proposal`, verdict `reject`) and the librarian reads it before
proposing: a name a steward has declined is never proposed twice, and the corrective brief says so
when the model tries. The two legacy kinds (`entity-proposal`, `parked-capture`) stay readable for
the rows that carry them and are written by nothing.

**D5 — a ninth gate guards the identity zone.** The agent's own write tool refuses
`wiki/entities/`; `gate_identity` makes that a proof instead of a tool's refusal: every page
created there must be a proposal the worker itself wrote from the declared outcome and must arrive
with `approved_by` empty, and every page modified there must be a proposed spelling the worker
appended and proved byte for byte. Before writing a proposal, `identity.write_proposals` folds the
name against the registry with the birth gate's own fold — a name the registry knows under any
spelling becomes a proposed alias of that entity, never a twin — so the twin-entity defence no
longer depends on which door the name arrived through.

**D6 — hand registration is born confirmed.** The console's *Register an entity*
(`server.review.create_and_record`, App-authored, the steward named in the trailer and in
`approved_by`) and `stigmergy-entities create` (the steward's own git identity) mint straight to an
approved page — the steward IS the approver — with the registry check on name and aliases run
before anything is written, live as they type in the console. There is no form that mints "from" a
capture, because no capture is waiting.

**D7 — `brain_reply` is removed.** Nine MCP tools. A submitter is told in `brain_submit`'s
acknowledgement which entities the capture will be filed against and that unknown ones will be
proposed; the echo is the whole of the submitter's involvement.

## Consequences

- The filing report carries a *proposals clause* so a reader of the commit sees what the
  librarian invented and what it only recognised; the weekly digest counts proposals decided.
- The meeting flow proposes like the ordinary flow. The "reuse the parked distillation" rule is
  moot: nothing is re-filed, so nothing is re-read.
- The console's Captures page is read-only; the Inbox lists proposed entities, proposed spellings
  and repair proposals; the Entities desk decides them with the registry verdict beside each name.
- A proposal older than a week is a steward's debt, not a submitter's. Nothing expires on its own.
- Both repositories change: the librarian's and the meeting distiller's skills, the entity
  template, the linter's lifecycle rules and the registry's `proposed` key live in the knowledge
  repo; the frozen twins under `tests/librarian/fixtures/` pin them by sha.

## Alternatives rejected

- **Keep the park but answer it from the inbox.** Removes the submitter from the loop and keeps
  everything else — the seven statuses, the three dispositions, the re-file. The complexity was the
  defect, not its distribution.
- **Auto-approve proposals after N days.** A silent approval is a mint nobody decided; ADR 016's
  rule that an entity is born through a human holds.
- **Hide proposed entities until approved.** Leaves the pages anchored to them orphaned in search
  and answers, which is the user-visible failure the proposal exists to avoid.
- **A `proposals` table.** A second source of truth next to the registry, with a sync problem
  the snapshot road already solved for ops files. The registry's own key is enough.
