# ADR 030 — entity birth completes from all three interfaces

**Status:** accepted · 2026-08-04 · supersedes ADR 029's "entity writes stay CLI" consequence,
which deferred exactly this decision to its own record ("web-native entity birth needs an
authorship decision (operator identity vs App bot) that deserves its own ADR"). This is that ADR.
Requested and argued before it was written; unblocked by steward resolution reaching the
deployed groups.

## Context

Entity birth must be completable from the three interfaces. Before this record, all three
dead-ended in a command a human had to run somewhere with a checkout: the console's Entities tab
rendered a half-filled `stigmergy-entities approve` template with bracket placeholders; the Slack
card's Approve returned the same command; MCP's `review_decide` recorded the decision and returned
`mint_command`, its docstring promising "This tool NEVER writes to git".

The operator's argument, which is the crux and which this record accepts: **when the command is
pasted to an agent, the agent already approves on the operator's behalf** — so the authority was
de facto "whoever authenticated", not "whoever holds a terminal". The split bought governance
nothing and cost a three-hop flow with bracket placeholders. The terminal path it protected was
never itself an authorization check: `stigmergy-entities approve` verifies nobody's authority —
whoever holds a checkout and the DSN mints, and `--by` is attribution.

What the split DID buy, and what must survive its removal, is the discipline around the write:
resolve-before-mint collision checks, drift refusal, gitleaks over exactly the files the commit
carries, registry regenerated from the pages, ONE commit, no force-push, bounded retries against
a concurrent librarian push. Those live in `stigmergy.entities` and are kept by reuse, not
reimplementation.

## Decisions

### D1 — Commit authorship: the App writes, the human is named in a trailer

A server-side mint commits as the **librarian GitHub App** — the only push credential the `app`
group holds — carrying an `Approved-by: <resolved identity>` trailer. The governance record is the
append-only `review_decisions` row plus that trailer: `git log` answers "who approved this
identity" with a named human either way, and the App-vs-human author line answers "through which
door".

The CLI path is unchanged: a steward at a terminal still commits under their own git identity.
ADR 016's "two doors into main, two signers" consequence is therefore refined rather than
repealed: the **trailer and the zone** distinguish a governed mint (App + `Approved-by` +
`wiki/entities/` + registry) from a fast-lane filing (App, no trailer, the three creatable
folders) and from a hand mint (human identity). The knowledge repo's authorship check must be
re-read against an App-authored `wiki/entities/` commit — that half lives in the knowledge repo
and this change is not done until both repos are green.

### D2 — Authorization per surface: enforce where identity is real, attribute where it is not

- **MCP and Slack**: the caller's identity is resolved (token / Slack profile), the steward check
  runs against `ops/stewards.json` through the steward-resolution mechanism (repo read where a checkout
  exists, baked snapshot where none does), and `SELF_APPROVAL_REFUSED` is enforced — the proposer
  of an entity may never be its approver. These lanes come out **stronger** than the terminal they
  replace.
- **Console**: mints under the admin token with `actor` as **attribution**, exactly like every
  other console mutation and exactly like the CLI it replaces. The admin-token holder is the
  deployment operator, who already holds the secrets and the DSN — this is not an escalation, and
  pretending one shared credential could enforce a second-human rule would be theatre. The
  asymmetry is stated out loud rather than papered over: self-approval is *enforced* on MCP and
  Slack, *attributed* on the console and the CLI.

### D3 — Where the write happens: a throwaway clone per request, in the `app` process

The mint clones the knowledge repo into a temp directory using the App credential
(`STIGMERGY_LIBRARIAN_REPO_URL` + the `librarian.githubapp` token machinery, both already in the
`app` group's environment as an accepted residual), runs the same preparation the CLI runs,
commits once, pushes, and removes the clone. Rare operation, small repo, seconds of bounded git
work — not an agent run, so it does not violate the "no slow agent run inside an HTTP request"
rule any more than the webhook's own git-adjacent work does.

**Rejected: clone at boot.** It couples the public read path's startup to GitHub availability and
parks a standing checkout in the public-facing container for an operation that happens rarely.

**Rejected: hand the mint to the librarian worker via a queue row.** ADR 015/016 confine the fast
lane OUT of `ops/` and `wiki/entities/`; routing governance writes through the worker would
dissolve the single most load-bearing ownership rule in the birth design. `stigmergy.entities`
remains the only writer of both locations — the package invariant is untouched; what changes is
that a server process may now *drive* it, exactly as a human's shell did.

**Refusal, not degradation, when the capability is absent.** A server without the App credential
or the repo URL (a local stdio server, say) refuses a mint by naming the missing capability, the
`CapabilityUnavailableError` posture — the CLI remains the local path. gitleaks must be present
and working in the deployed image (it already is: one image carries the worker's toolchain); a
mint refuses cleanly without it, exactly as the CLI does.

### D4 — The discipline survives by reuse

Server-side minting calls the same `stigmergy.entities` seams the CLI does — `birth.prepare`
(collision gate against the registry the commit will *publish*), the drift refusal, the template
render, `generator.regenerate`, the gitleaks scan over exactly the files the commit carries, one
commit, bounded rebase-and-retry against a concurrent push, never a force-push. The CLI's
orchestration is extracted into a shared library function both callers use; a divergence between
the two doors is the defect class this decision exists to prevent.

### D5 — What each surface becomes

- **MCP**: `review_decide(item_kind="entity-proposal", verdict="approve")` takes the identity
  metadata (`name` required; `entity_id` defaulting to its slug; `entity_type` from the closed
  list; optional `aliases`, `role`; optional `requeue`) and **mints**. The response names the
  entity, the commit and whether the capture was requeued; `mint_command` is gone. The docstring's
  "This tool NEVER writes to git" is rewritten to the truth: reject and every parked-capture
  disposition still never touch git; an entity-proposal approve makes exactly one commit through
  the governed door. The human still authors the metadata — a default slug is a prefill, not an
  agent's judgment (ADR 016's rule holds).
- **Slack**: the card's Approve opens a modal (name prefilled from the proposal, type select,
  aliases, role, requeue checkbox) and mints on submit. Reject is unchanged.
- **Console**: the Entities tab grows a real Approve form with the same fields and a requeue
  checkbox; the bracket-placeholder command template is deleted, not polished.
- **CLI**: unchanged, still first-class — it is the road that needs no server at all.

## The breaking-change matrix (the `review_decide` contract)

- **Consumers, enumerated**: the Slack review card (`slack/review.py`), updated in this change;
  the admin console (which never called the tool — it rendered the CLI template, deleted in this
  change); MCP clients — the operator's own sessions, enumerable because `STIGMERGY_TOKEN_STORE`
  lists every identity that can reach the tool on this single-operator deployment. No third-party
  consumer exists or can exist unenumerated.
- **Compatibility decision**: a **coordinated breaking change**, all consumers first-party and
  shipped in the same image. Rejected safer options: a compatible extension cannot express
  "approve now mints" (the semantics, not the shape, change); expand–contract (a transitional
  second tool) breaks the pinned ten-tool list and the README's counted claim to serve a
  transition window with no beneficiary. The old call shape — approve with no metadata — fails
  **loud and actionable**, naming the now-required fields; it never silently records-without-
  minting. Sign-off: the operator's.
- **Data**: none migrated. `review_decisions` is append-only; new rows carry the mint outcome in
  the existing `extra` column (additive). Entity pages and the registry produced by the new door
  are byte-compatible with the CLI's own output.
- **Rollback**: redeploy the previous image — the old behavior returns; entities minted meanwhile
  are ordinary, valid knowledge-repo content requiring no reversal.
- **Transition tests**: the old-shape call (no metadata) against the new surface asserts the
  actionable refusal; reject and parked-capture paths keep their never-touches-git tests; the
  categorical "this seam never writes to git" test is *narrowed by name* to the paths where it
  remains true, in the same change that records why.

## Consequences

- The three-hop placeholder flow disappears; approving an entity is one action from any surface.
- The App key's blast radius grows by one operation class (governed mints) on a credential the
  `app` group already carried — accepted as the residual it already was, with the same revocation
  drill.
- `server` gains a declared, symbol-scoped architecture edge into `stigmergy.entities`' mint seam
  (and transitively `librarian.gitcmd`/`githubapp`, the webhook precedent one edge over) — named
  in `tests/test_architecture.py`, never a general license.
- A revoked steward's approve authority on the deployed groups still moves at re-bake speed
  (steward resolution's own recorded trade); the fast lever remains the identity's token.
