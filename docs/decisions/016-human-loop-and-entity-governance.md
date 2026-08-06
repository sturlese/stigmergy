# ADR 016 — Reading the fast lane's inputs at the base commit, and governed entity birth

**Status:** accepted · 2026-07-27

## Context

The librarian files. It arrived carrying two governance debts, and they turn out to be one:

- **The base-commit read.** The local librarian read `ops/acl.json`,
  `ops/entity-registry.json` and the contract
  linter (`.claude/tools/stigmergy_lint.py`) off the **working tree** of `settings.repo`, while the diff
  it judges is built from a worktree branched at `base` (`origin/<branch>`, normally). An uncommitted
  local edit to any of the three therefore changed a run's behavior without changing the commit the
  page was filed against — for `acl.json`, that meant the audience labels stamped onto a page could
  disagree with what the commit that produced it actually said. `worker._check_skill_at` had already
  taken the symmetric correction for the skill file, for the same underlying reason: a check
  that can pass while the thing it checks is absent is worse than no check.
- **Entity birth is ungoverned because it is impossible.** The registry holds three hand-seeded
  entities and nothing grows it; every capture about anything else parks in `triage` forever.
  `ops/entity-registry.json` is hand-maintained, so "the registry is a derived view of the entity
  pages" is a claim nothing checks, and no proposal→approval flow for entity birth exists at all.

These two are recorded together because the central move makes them one argument rather than two:
**the moment the registry becomes the output of a governed flow (the second decision below), a
working-tree read of it becomes a read *around* that governance** (the first decision below) — an
uncommitted local edit could anchor captures to an entity nobody approved. Closing one without the
other leaves a door open that the other one was built to shut.

Not recorded here: **ask-back** (`needs_input`, `brain_reply`) and **the steward's triage drain**
(`stigmergy-queue requeue/resolve/reject`, the `resolved` terminal state). Both are extensions of
the already-recorded division of labor (ADR 015: the agent judges, code routes and vetoes) —
routing an existing agent declaration to a new destination and giving a human three ways to close a
row are operational completions of that doctrine, not new architectural decisions. They are described
in [`docs/reference/capture.md`](../reference/capture.md) and
[`docs/reference/librarian.md`](../reference/librarian.md). The deployed worker (a second Fly process
group running the same image) is likewise an operational decision, recorded in the runbook
rather than here, because it does not change what the librarian is allowed to do — only where it runs.

## Decision

### 1. The three repo-sourced inputs are read at the base commit, in every mode

`librarian.base_inputs` is now the only way the fast lane reads `ops/acl.json`,
`ops/entity-registry.json` and the contract linter. All three resolve against `base.sha` — the same
commit the worktree branches from and the diff is judged against — never against
`settings.repo`'s working tree, on a laptop or in the deployed container alike.

**Why this is a governance property and not tidiness.** Once entity pages and the registry are the
output of the steward's `approve` flow (decision 2), a working-tree read is a read *around* that
gate: an uncommitted edit to `ops/entity-registry.json` could anchor captures to an entity nobody
approved, and an uncommitted edit to `ops/acl.json` could stamp a page with audience labels the
committed configuration never declared. Two further consequences fall out of the same ruling: a
deployed worker has no working tree anybody edits, so tree semantics would exist only in the one
environment production never runs in — the class of local/deployed divergence this repo keeps
paying down; and a filed page's stamps (its audience labels above all) become reproducible from
history, which the ACL design always assumed and this makes true.

**Three shapes, deliberately not one patch**, because the three inputs are three different kinds of
thing: the ACL config is data with a data-level parser, so it gets a `load_text` seam
(`acl_rules.load_text` → `kernel.acl.load_acl_config_text`) and never becomes a file at
all; the registry's parser (`kernel.registry.load_registry`) is tested code that takes
a path (import and adapt, never rewrite), so the blob is materialized to a temp file rather
than inventing a data-level entry point for one caller; the linter is a script — it is *executed*, not
parsed — so it is materialized per item, because the base commit is resolved per item and the script
that judges a diff must always be the one in the commit the diff was built from. Missing keeps its
existing meaning in every case: no ACL config is an open corpus, no registry is an empty one (the
graph works unregistered), a missing linter is the same loud fail-closed startup refusal it always
was.

**Rejected: leave the working-tree read for the ACL config and the registry, close it only for the
linter.** The linter was the most visible case (it is executed), but the argument is identical for
all three: any of them read off a working tree lets an uncommitted edit change what a run does or what
it stamps. A partial close would have left exactly the asymmetry this decision exists to remove.

**Cost, accepted:** trying a linter change by editing it and running the librarian against it no
longer works. Commit and push it, or exercise it through the linter's own suite in the knowledge
repo. This is the same trade the skill-at-base correction already made, now applied uniformly.

### 2. Governed entity birth: propose in the queue, mint by steward-signed commit, registry as a genuinely derived view

A new subsystem, `stigmergy.entities`, and its CLI, `stigmergy-entities` (`list · show · approve · reject
· create · regenerate [--check]`), become **the only writer of `ops/` and `wiki/entities/`
anywhere in this codebase.** The librarian's confinement from ADR 015 is untouched: nothing in the
fast lane gains the ability to write either location.

- **The proposal lives in Postgres, not in a file.** A `triage` row parked with
  `situation: unresolved-entity` (or `unsupported-type`) already carries the material, the agent's
  rationale and the unresolved name — it **is** the proposal. A file-based `ops/entity-proposals.json`
  was the obvious shape and was rejected: the librarian may not write `ops/` (a confinement this repo
  defends everywhere else), so that file could only ever be written by the steward CLI itself, at
  which point it would duplicate the row and create a DB↔file sync surface with no benefit. The
  durable, diffable artifacts that matter are the entity page and the registry, both born in the
  approve commit.
- **`approve` is one commit, signed by the steward's own git identity, pushed.** It materializes the
  entity page from `ops/templates/entity.md`, regenerates `ops/entity-registry.json` from every page
  in `wiki/entities/`, and commits both together — never as two commits, never authored by the
  librarian's GitHub App. `git blame` answering "who approved this identity" with a named human is the
  whole of the governance: a PR flow with one steward and no branch protection would be ceremony
  rather than review.
  *Amended 2026-08-04 ([ADR 030](./030-server-side-entity-minting.md)):* this describes
  the CLI's own commit, unchanged. A server-driven mint from MCP, Slack or the console now exists
  beside it and DOES commit as the librarian's GitHub App — carrying an `Approved-by:` trailer in
  place of the steward's own git identity, so the governance answer moves from `git blame` alone to
  `git log` plus that trailer.
- **Resolve-before-mint.** `approve`/`create` refuse an id, name or alias that collides with an
  already-registered entity, naming the entry it collided with — the
  `northwind-slides.md`-next-to-`Northwind Group.md` class of defect, refused at the gate rather than
  produced and cleaned up later. The check is asked of the registry the commit is about to *publish*
  (freshly derived from the pages), not of the possibly-drifted file on disk — which is why `approve`
  separately refuses to run at all into a clone whose registry and pages already disagree (see below).
- **The registry is a derived view, and derived means derived.** `stigmergy-entities regenerate` is a
  pure function of `wiki/entities/*.md`, serialized through the graph layer's own
  `save_registry`/`load_registry` (one serializer, never reimplemented). `--check` exits non-zero on
  drift and names the divergence semantically ("which entity, and what about it"), not as a text
  diff — "the registry is derived from the pages" stops being prose and becomes something CI in the
  knowledge repo enforces (`stigmergy_lint.py`'s registry-consistency rule).
- **The steward authors the identity metadata, not the agent.** The
  agent's outcome schema is untouched — it still only ever says "I cannot place
  this, and the name is X." `entity_type`, `aliases` and `role` arrive as flags a human types with the
  material on screen. Agent-drafted aliases would invite rubber-stamping, which is the failure mode a
  governance gate exists to prevent in the first place.
- **The steward's own clone is never repaired, only refused.** `approve`/`create` refuse a dirty
  working tree, a local branch diverged from its remote, and a clone not on the branch being pushed —
  and never force-push, on any attempt. A push race with the librarian (or with another steward) is
  answered by fetch, regenerate, and retry, bounded at three attempts; a genuine conflict (two
  approvals touching the same entity) is left for a human to resolve by hand, because that overlap is
  exactly the identity decision this subsystem exists to put in front of one.

## Consequences

- **Two doors into `main` now exist, with two different signers.** The librarian's GitHub App commits
  fast-lane pages; the steward's own git identity commits entity births. Keeping them distinct is the
  point — `git blame` must answer "was this reviewed by a human or filed by the fast lane" without
  ambiguity, and a shared identity for both would erase that distinction.
  *Amended 2026-08-04 ([ADR 030](./030-server-side-entity-minting.md)):* a third shape
  joins these two — a server-driven mint, App-authored like the fast lane but carrying an
  `Approved-by:` trailer a fast-lane commit never has. The trailer plus the zone (`wiki/entities/`
  and the registry, never a fast-lane page) is what now tells a governed mint from a fast-lane
  commit; the distinction this bullet protects is refined, not erased.
- **Triage will start draining instead of only filling up.** Until this shipped, the registry's three
  hand-seeded entities were the ceiling on what could ever anchor, and the lever to revisit if the
  ceiling felt too low was never "let the agent mint" — it was "widen how liberally a page may declare
  company-wide scope" (ADR 015's own consequence). That lever is unchanged; this ADR adds the other
  one: a human grows the registry, on purpose, one entity at a time.
- **An uncommitted edit to `ops/acl.json`, the registry, or the linter now does nothing until it is
  pushed.** This closes a real bypass but removes a real convenience (trying a linter change locally
  before committing it). Documented in the runbook so it surprises an operator once, in words, rather
  than in the middle of a run.
- **The steward's own machine now needs gitleaks**, because `approve`/`create` scan the exact files a
  commit is about to carry before making it — the one human-driven path to `main` in this system, and
  therefore the one place a secret typed into `--role`/`--aliases` must never reach unscanned. Absent,
  the commands refuse rather than silently skipping the scan.
- **Cost per approval is a fetch, a rebase and a regeneration on contention**, and up to three retries
  before giving up and telling the steward to try again. Acceptable in front of a human who is
  watching; the same budget would be wrong for an unattended worker, which is why it is smaller than
  `gitcmd.PUSH_ATTEMPTS`.

## Alternatives rejected

- **A file-based `ops/entity-proposals.json`.** See decision 2's first bullet: the
  librarian cannot write it, so it would have to be written by the steward CLI from the same row the
  CLI already reads — a second copy of the same fact with nothing gained.
- **An agent-drafted proposal for `entity_type`/`aliases`/`role`.** Identity metadata authored by the
  party being governed is the failure mode governed birth exists to close, not a convenience worth
  keeping.
- **A PR-based approval flow instead of a direct signed commit.** One steward and no branch protection
  makes a PR ceremony rather than governance; it would mean something only where multiple reviewers
  and protection rules exist.
- **Tolerating the working-tree read for the ACL config and the registry a while longer**, closing only
  the linter (the most visible case, since it executes). Rejected because the argument for closing it
  is identical for all three, and a partial close leaves exactly the asymmetry this ADR exists to
  remove — see decision 1's own rejected alternative.
