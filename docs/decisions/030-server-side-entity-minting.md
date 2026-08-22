# ADR 030 — entity birth completes from all three interfaces

**Status:** accepted · 2026-08-04 · **amended 2026-08-16** twice (see the two amendments at the
end — "the ledger writer moves below the doors", after which the CLI door, described here as
ledger-less, records the same row; and "two-door refusal wording", which splits what a refusal SAYS
per door while D4's reuse of the checks themselves stands) · supersedes ADR 029's "entity writes
stay CLI" consequence,
which deferred exactly this decision to its own record ("web-native entity birth needs an
authorship decision (operator identity vs App bot) that deserves its own ADR"). This is that ADR.
Requested and argued before it was written; unblocked by steward resolution reaching the
deployed groups.
**Superseded by** [ADR 044](./044-the-capture-is-the-approval.md): the server-side decision doors are gone whole. An entity is born from a capture, confirmed by whoever captured, in the commit that files the page.

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


## Amendment: the ledger writer moves below the doors (2026-08-16)

**Status:** accepted · amends D2's attribution half. Nothing above is withdrawn; one gap it left
open is closed.

### What this record got wrong by omission

D2 settles who may act at each door and how they are attributed. It says nothing about who
*records* the act, and the answer turned out to be "two doors out of three". `review_decisions` is
written by `server.review.record_decision`, and `stigmergy.entities` may not import
`stigmergy.server` — an edge `tests/test_architecture.py` enforces and this record relies on. So
`stigmergy-entities approve` minted, pushed, and wrote nothing to Postgres.

That is not a tidiness problem, because two surfaces read the table as if it were complete: the
admin console's Activity view and the weekly digest's governance section. Both under-reported
identity approvals, silently, by exactly the CLI's share — and nobody reading either could tell
"no CLI approvals happened" from "CLI approvals are not counted". An incompleteness a reader cannot
see is worse than a documented absence.

### The decision

**The ledger moves to `stigmergy.capture.decisions`, below both `entities` and `server`.** That
module now owns the table: its DDL, its one write, its one read, and the verdict vocabulary. All
three doors record the same row. `server.review` re-exports `ensure_review_schema` and
`record_decision` under the names eight entry points already used, because a table changing owner
is not a reason for all of them to learn a new import.

**No architecture edge is crossed to do it.** `entities` and `server` both already import
`capture`, which is the bottom of the durable-state stack — the `entities` -> `server` edge this
record depends on is untouched, and the architecture tests still enforce it in both directions.

The CLI's `reject` records too. Refusing an identity is as much a governance decision as granting
one, the console already recorded its own Reject for that reason, and recording only `approve`
would leave "who decided this identity" answering from different tables depending on the verdict.

**D2 is unchanged in substance**: `record_decision` carries no authorization and never has.
Authorization stays per surface — a resolved MCP identity, an admin token, a steward's shell — and
a permission check buried in the writer would be a fourth, invisible one none of the doors could
state. Self-approval remains *enforced* on MCP and Slack and *attributed* on the console and the
CLI, exactly as D2 says.

### Alternatives, and why not

- **A second writer inside the CLI**, duplicating the schema behind its own `--dsn`. Cheaper, and
  it puts a second writer on an append-only governance table — the way two definitions of what a
  decision is eventually appear.
- **Accept the gap and document it**, making both reading surfaces say "server-side approvals".
  Legitimate: the CLI is a host-local door for an operator who already holds the DSN and the
  credentials, and its mints *are* attributable from git. Rejected because every future reader of
  those two surfaces would have to carry the caveat, and a count that is complete needs no caveat
  at all.

### Consequences

- `review_decisions` answers "who approved this identity" for every door. A CLI approval is now
  attributable twice — its commit's author and its ledger row, carrying the same steward identity.
- `extra` records which door decided (`{"door": "cli"}`), and for an approve the entity id and the
  commit that landed. Additive, in the column ADR 030 already reserved for per-kind detail. *(Later:
  `record_decision` gained a required `source` argument that names the door for all four of them,
  validated against a closed set, so this door stopped writing its own `door` key — nothing ever
  read it. The ledger is never migrated, so rows written before that keep it.)*
- **The digest and the gardener stopped importing the server.** Both cron CLIs reached into
  `server.review` for one DDL call, and `digest.sections` for two literals — which pulled the whole
  git write stack (`librarian.gitcmd`/`gates`/`base_inputs`/`githubapp`) into every digest process
  to count rows in one table. The declared transitive-reach exception for that is now empty, and
  its test still runs, so a re-widening is visible on the first module.
- A database no server has ever started against now gets the ledger table from the CLI's own
  `_connect`, the same startup pattern every other entry point follows. Without it, `approve`
  would mint, push, and then fail on the INSERT — after the irreversible half.


## Amendment: two-door refusal wording (2026-08)

**Status:** accepted · amends D4's reuse half. Nothing above is withdrawn; one consequence of the
reuse it mandates is closed.

### What this record got wrong by omission

D4 settles that the discipline survives by reuse: both doors call the same `stigmergy.entities`
seams. It says nothing about the refusals those seams produce, and a refusal is not door-neutral
just because the check that raised it is.

`stigmergy.entities` was written for one door. Its messages address an operator standing in a
clone: they interpolate that clone's absolute path and hand out `git -C <path> …` to run in it,
which is exactly right for `stigmergy-entities` and exactly what makes the subsystem debuggable.
The server door then reused the same seams — and `server.review` echoes an `EntityError` to a
steward over MCP **verbatim**. What a steward got back for a missing entity template was:

> `ops/templates/entity.md` is missing from
> `/var/folders/j1/7vqsgmw139b2c5xbw30s8xr40000gn/T/stigmergy-entity-mint-saptenvs/repo` — … this
> command does not carry its own copy …

Three things wrong with it at that door: it publishes the server host's temp directory, it names a
directory that no longer exists (the `TemporaryDirectory` is removed before the message is
serialized), and it tells somebody who ran no command that "this command" carries no copy. The one
fix a steward *could* act on — commit the template to the knowledge repo — was the only thing the
sentence never said. The same shape held for a lost push race, an unmintable clone and a
post-rebase collision.

### The decision

**A refusal that names a filesystem path or a runnable command is composed for the door whose
operator can act on it, and the OTHER door re-words it at the boundary that exists for that.**

For the server door that boundary is `entities.remote.mint_via_clone` — already the module where
every foreign exception is renamed into this package's vocabulary. Its post-clone `try` grows an
ordered ladder over four refusal TYPES (`CloneStateError`, `TemplateMissingError`,
`CollisionRaceError`, `PushRaceError`), each arm logging the library's own diagnosis with
`exc_info=True` and raising a written sentence in its place. MOVED, not lost: the operator reading
the server log still gets the path and the traceback, which is the trade `MINT_FAULT_MESSAGE`
already made for `LibrarianError`. **The CLI door is unchanged, byte for byte.**

**The mapping keys on the TYPE, never on the text**, so a raise site whose sentence is written for
a terminal needs a class no door-neutral raise site shares. Two classes were added for that and for
nothing else: `TemplateMissingError`, and `CollisionRaceError` for the post-rebase re-ask in
`mint._recheck_and_regenerate`.

`CollisionRaceError` is the subtle half and the reason this is a decision rather than a rewording.
`CollisionError` was raised at two sites doing two different jobs: `birth._refuse_collisions` is
the resolve-before-mint **governance verdict** — the identity already exists, point the capture at
it — and is door-neutral; `mint._recheck_and_regenerate` re-asks that same gate after a rebase and
splices the local clone's sha and two `git -C` commands onto the answer. Mapping the base class
would have caught both and told a steward "something else changed the registry while this mint was
in flight, approve again" about an entity registered months ago: a governance verdict turned into a
retry loop that cannot succeed. The race subclasses the verdict, so every existing `except
CollisionError` still catches it and the terminal still reads the full sentence.

**The pass-through set is part of the decision, not what is left over.** Birth-field validation,
the collision verdict, the secrets refusal (`mint._relocate` has already rewritten gitleaks'
scratch path to the repo-relative page, and the rule id is what a steward would allowlist) and the
drift refusal (which names the portable `stigmergy-entities regenerate`) all reach the wire
untouched. They are the refusals a steward can act on, and every one of them is an `EntityError` —
which is why `mint_via_clone` has no bare `except EntityError` arm and must never grow one.

### Alternatives, and why not

- **Map at `server.review` instead.** It is where the wire is, but it is also the place that must
  not know which `entities` refusals are dirty — and the admin console reaches the same mint
  without passing through it, so the leak would stay open on one of the two server doors.
- **Sanitize the text — strip anything path-shaped on the way out.** Cheap and general, and it
  produces a sentence nobody wrote: the remaining words still address an operator holding a clone.
  A refusal is copy, and copy is composed, not filtered.
- **Give `entities` one neutral wording for every door.** It would cost the CLI its whole local
  diagnosis — *which* checkout is dirty, *which* sha the commit is sitting on — to spare a steward
  a path. The terminal door is the one where those answers cannot be looked up anywhere else.

### Consequences

- Four sentences and not one, because "approve again", "commit the template first" and "ask whoever
  runs this deployment" are three different instructions. Every mapped sentence leads with what
  state was left behind ("Nothing was pushed"), which is the fact a steward needs before any other,
  and ends with the action they can take.
- `admin_actions.error_class` records `EntityError` for a mapped fault instead of the library's own
  class, the posture that column already had for every `LibrarianError`. The library's class and
  its full sentence are in the server log.
- The properties are enforced, not written down: `tests/entities/test_remote.py` sweeps every
  refusal constant this module publishes for an absolute path or a `git -C`, deriving the list from
  the module so a new sentence joins by existing; `tests/server/test_review.py` proves what reaches
  the wire, mapped and passed-through alike, with the template case driven end to end against a
  real remote; `tests/entities/test_cli.py` pins the CLI's own sentence byte for byte, since the two
  doors are now free to diverge and nothing else would notice this one drifting.
