# ADR 042 — an entity is born written, and keeps being written

- **Status**: accepted
- **Date**: 2026-08-21
- **Supersedes**: the hand-mint door of [ADR 030](./030-server-side-entity-minting.md) (a
  server-driven `mint_via_clone` and the CLI's `create` rendering the template — the App-authored
  governed commit with a human named in a trailer STANDS, for decisions), and ADR 016's "a
  steward at `create` may leave the template's stubs" ([ADR 016](./016-human-loop-and-entity-governance.md)).
- **Amends**: [ADR 041](./041-file-first-govern-after.md) — the proposal road is the ONLY birth
  road now, and a steward's registration is one of its captures.
- **Related**: [ADR 039](./039-governed-repair-loop.md) (the `entity-body` repair, which stays the
  road for a page whose summary has become wrong), [ADR 021](./021-views.md) (the synthesized
  view, which stays the entity's rollup; the page is its spine).
  Narrative: [`docs/reference/librarian.md`](../reference/librarian.md),
  [`docs/reference/operator-runbook.md`](../reference/operator-runbook.md),
  [`docs/reference/admin-console.md`](../reference/admin-console.md).

## Context

Twelve of the first brain's nineteen entity pages said nothing about the entity. They carried the
template's own stubs for a body — `<One clear paragraph: what this entity is and why it's in the
brain.>` — which GitHub hid as HTML, the index ranked as knowledge, the views summarised as "not
yet populated", and `ask` could do nothing with. Every one of them had been born through a hand
door: `stigmergy-entities create` or the console's *Register an entity*, both of which rendered
`ops/templates/entity.md` with the identity fields filled in and committed. No model, no corpus,
no context — a script writing a page. The librarian's own proposals (ADR 041) were fine: the
model writes the What / Who, the facts and the connections from the capture in front of it, and
code refuses a proposal without its summary.

Two smaller defects rode along. Every entity page carried the template's HTML maintenance
comment, copied verbatim and indexed as text. And an entity page was written exactly once — at
birth — while everything the brain learned afterwards went to notes and to the view; the spine
never grew.

The brief for the fix, in the operator's words: the brain has models that reason; an entity page
that enters it has to be rich, and no deterministic script with a name and a type can make it so.

## Decision

**D1 — there is no deterministic birth.** `entities.mint` and `entities.remote.mint_via_clone`
are gone; `entities.guard` keeps the two refusals every governed write shares. An entity is born
through the librarian, from material, with its page written by a model that has the material and
the brain in front of it — the road ADR 041 opened for proposals is the only road.

**D2 — a steward's registration is a capture.** *Register an entity* and `stigmergy-entities
create` keep their place, but what they do is commission: the steward says what the entity is, in
their own words (a required field — a form with nothing in it is refused, and so is a name the
served registry already resolves), and that text is queued as a capture carrying the registration
(`register_name`, `register_type`, `register_aliases`, `register_source` among its hints). The
librarian's brief tells it a steward is introducing the entity; it proposes it under that name,
writes the page from the material and from what the brain already holds, anchors the note to it,
and the entity is born **confirmed** by the steward instead of proposed — `approved_by` names
them, the registry entry is not marked proposed, and the ledger carries their approval, written
after the push like every door's. A registration the account ignores is a repairable refusal
(`registration-missing`) the corrective retry answers; a name the registry already resolves asks
for nothing but the spelling. `brain_submit` refuses the registration hints from every client: a
registration is an act of authority, and that tool attributes material, never authority.

**D3 — the render refuses emptiness.** `birth.render_page` will not write an entity page whose
What / Who is missing; a section with nothing to say is not written (no `- <fact…>` a reader
takes for a fact); the template's comments stay in the template. The structural backstop: a door
added tomorrow cannot bear an empty page either.

**D4 — the spine accretes.** The account gains `entity_updates`: what the material establishes
about an entity the registry already knows — facts, and pages it should connect to. Code APPENDS
those lines under the page's own `## Facts` and `## Connections` (creating the section when the
page was born with nothing to say there), moves `updated:` to today, skips a line the page
already carries, and proves the file byte for byte through the same `expected_bytes` road a
proposed spelling takes, so the ninth gate admits exactly the planned edit. An update naming an
entity the registry does not resolve, or one the same account proposes, is refused with the brief
that says where those facts belong. The What / Who paragraph is not a filing's to change: a page
whose summary has become wrong is the repair loop's `entity-body` business (ADR 039).

**D5 — the librarian looks before it writes.** The brief asks for a search of the brain before
proposing, and for what the existing pages establish to land in the proposal's facts and
connections; a proposal is the first version of a page somebody will read, not a name on a list.

## Consequences

- Registering an entity is no longer instantaneous: the page appears when the capture files, two
  or three minutes later, and it can fail like any capture — visibly, with its reason, in
  Captures — instead of succeeding as an empty commit.
- The console's Register form asks "What is it?"; the CLI asks `--about`. A steward who knows
  nothing about the entity is told to capture about it first: better a page that does not exist
  than one that says nothing.
- `server.review.create_and_record` is replaced by `commission_registration`; the closed caller
  set of the shared sequences (`tests/test_architecture.py`) names it.
- Entity pages stop being static: a filing that establishes something about a known entity grows
  its page, and the report says so ("It adds 2 facts and 1 connection to the page of `acme-corp`").
- The twelve existing empty pages are a steward's debt the gardener already sees
  (`entity-placeholder-body`) and the repair loop already drafts for; nothing here rewrites them.
- Both repositories change: the briefs (look before you write, the registration paragraph,
  `entity_updates`) live in the knowledge repo; the frozen twins under `tests/librarian/fixtures/`
  and `evals/filing/repo/` pin them by sha.

## Alternatives rejected

- **Make `--summary` a required field of the hand mint.** Replaces an empty page with a page
  holding one sentence typed into a form — still a script writing the page, still blind to what
  the brain already holds about the name. The operator's objection was to the shape, not the
  field.
- **Draft the page at the door with a model, synchronously.** Puts a model call, a corpus read
  and a budget on a request handler, duplicating the worker's filing run with a second prompt and
  a second set of gates. The worker already does all of it; the door only had to become a capture.
- **Fill the empty pages automatically on the next capture that names them.** Makes a filing
  rewrite a summary it did not author; `entity_updates` appends facts and leaves the What / Who to
  the governed repair, which is the one road a rewrite has.
- **Keep a synchronous mint for operators who "know what they are doing".** A second birth road
  is the second contract ADR 030 warned about, and the one that produced the twelve.
