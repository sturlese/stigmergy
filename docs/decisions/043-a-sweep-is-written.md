# ADR 043 — a sweep is written, and the hand that typed the deletion has already approved it

- **Status**: accepted
- **Date**: 2026-08-21
- **Amends**: [ADR 039](./039-governed-repair-loop.md) — D1's third clause for the deletions a
  person initiates, and the `delete` amendment's B2 (creation CLI-only), B3 (the sweep unlinks),
  B4 (the plan is recomputed at apply time) and the "no partial sweep" bullet of B6. Everything
  else in 039 stands, B2's first paragraph above all: no model proposes a deletion, in any
  spelling.
- **Related**: [ADR 041](./041-file-first-govern-after.md) (a capture never waits on a person —
  the same asymmetry this ADR applies to a deletion), [ADR 042](./042-an-entity-is-born-written.md)
  (a page is written by a model with the material in front of it, never rendered by a script —
  the same argument, applied to the pages a deletion leaves behind).
  Narrative: [`docs/reference/repair.md`](../reference/repair.md).
- **Amended by** [ADR 044](./044-the-capture-is-the-approval.md): D1, D3 and D5 stand. D2's "in the act" becomes "in the worker's act" — `brain_delete` queues and the librarian performs the removal — and D4's model runs where every other model of the write path runs.

## Context

The first deletion of a `wiki/` page on the staging brain, 2026-08-21. Two notes about SpaceX had
been filed a minute apart with the same material; the second announced itself as a restatement
"with explicit source URLs" and carried none. A person typed `stigmergy-repair delete` for it with
a reason, read the plan back, and approved the proposal — the same person, twice, one minute
apart. Commit `b93e7ce` landed: the page gone, no dead link anywhere, every gate green.

And the surviving note now reads, at its foot:

```
This material has no direct links to existing pages in this brain — SpaceX is a new entity …

> [!NOTE] Overlaps with SpaceX IPO Performance — Sourced Review
> restatement of the same material with explicit source URLs
```

A callout announcing an overlap with a page that no longer exists, one line under a sentence
that says the note links to nothing. The view, `views/spacex.md`, names the removed page three
times — once as a markdown link the sweep never looked at, because it only knew `[[wikilinks]]`.
The view will be regenerated; the note will read like that until a person edits it by hand.

This is not a bug in the sweep. It is the sweep doing exactly what ADR 039's B3 designed:
`[[X]]` becomes `X`, "so the sentence that cited a page survives the page", and the scan asks the
link question exactly as the contract linter asks it so that it "edits prose nobody asked about"
never. The scrub was built to be provable without anybody reading the result, and the price of
editing prose without reading it is prose that is syntactically correct and says something that
stopped being true. `gardener/sweep.py` already states the general form of this, explaining why a
judgment is not a tenth deterministic check: *a suffix list in Python answers exactly the cases
whoever wrote it thought of.* A bracket scanner answers the brackets and misses the paragraph.

The second observation is about the two approvals. ADR 039's D1 says a HUMAN approves one at a
time, and for every proposal a MODEL writes overnight that clause is the whole point: nobody was
there. A deletion is the one repair a person initiates by hand — B2 made sure of it — so the
judgment D1 asks for has already been given, at the command, with a `--why`. What the second step
actually supplied was **authorization**: the CLI does not know who is typing, so the identity had
to be established somewhere, and the review lane's per-path steward guard was where. The second
click was doing an authentication's job, not a judgment's.

## Decision

### D1 — the pages a deletion leaves behind are WRITTEN by a model, and their structure by code

A deletion still stores a plan — the pages that go, and the full planned bytes of every page that
referred to one of them (B3's unit of approval stands). What changes is who writes the bytes.

- **Frontmatter is code's.** `related:` and `sources:` entries, `supersedes:`/`superseded_by:`
  pointers naming a going page are dropped exactly as today (`_scrubbed_front` and what it calls).
  That is structured data and the decision is a lookup; a model asked to do it would be asked to
  re-derive a fact the parser already states.
- **The machine zones stay code's, body and all.** A `views/` page is REGENERATED wholesale by
  the view sweep and a `sources/` page is a filed document's provenance: neither is prose anybody
  reconciles, so both are unlinked deterministically — ADR 039 B3's rule, kept exactly where it
  was right. Discovered on the first real deletion: handed two views and two entity pages, the
  writer returned nothing at all, because the brief it shares with the proposer forbids editing
  those zones. It was right twice over — a model arguing with a generated file produces bytes the
  next regeneration overwrites.
- **The body of an AUTHORED page is the model's.** It is handed the doomed pages (what is disappearing, and what it
  said), every page that names one of them, and the plan code already made, and it returns each
  referencing page's body with the references reconciled — a sentence rewritten, a callout that
  only existed because of the doomed page removed, a markdown link retired along with the
  wikilink. One call over the WHOLE referencing set, never one page at a time: a question about how
  a set of pages refers to something must see the set, the lesson the duplicate-identity pass
  learned about pairs.
- **Code proves the bounds**, and they are the bounds a steward would check by eye:
  1. the set of pages the model returned IS the set of AUTHORED pages that refer to a going page
     — none outside it, none missing, none twice;
  2. each returned page's frontmatter is byte-identical to code's own scrub of it;
  3. each body keeps its `# Title` line, opens no `---`, is never emptied, grows by at most a
     sentence or two, and loses at most a handful of lines that referred to nothing being removed.
     That last one is not decoration: the growth bound is one-sided, and on its own it admits a
     body handed back as its title line alone — not empty, title kept, no growth, no reference
     surviving, and a page's whole content gone;
  4. afterwards, nothing in the corpus refers to a going page — over the clone, plus the
     unfiltered whole-tree dead-link scan B5 introduced, both unchanged;
  5. the diff that lands is the diff that was written: `expected_bytes` to `gate_body_rewrite`,
     byte-equality, B5's proof unchanged.

  The reference question gains one shape the frozen linter does not count — a markdown link at a
  going page's path, which is how `views/spacex.md` named the removed note a third time. It is
  scoped to targets that name a page in THIS corpus (no URL scheme, ending in `.md`): a bound that
  REFUSES must not fire on something the writer has no business touching, or deleting
  `Roadmap.md` would demand the destruction of every `notion.so/…/Roadmap` anybody had written.
- **One retry, then a refusal.** A draft outside those bounds is retried once with the findings,
  as every other model road is; a second miss refuses the whole deletion, naming the page the
  model could not reconcile. No proposal is stored and nothing lands. **There is no deterministic
  fallback** — not because the old scrubber was wrong about links, but because two writers of the
  same page are two implementations that can disagree about it, and a floor that the model
  "usually" clears becomes the road the failures travel.

B6's "no partial sweep" keeps its meaning and loses most of its mechanism: a reference in a BODY
is never "one the sweep cannot rewrite" any more, because a writer reconciles anything.
`_scrubbed_body`, `_blanked_code` and `_unremovable_reference` go. Two refusals stay, both at plan
time and both naming the page so a person can act on it: a reference in a frontmatter field this
kind does not rewrite, and a referring page whose frontmatter block code cannot read at all (CRLF,
a BOM, an unterminated `---`). The second is new and it earns its place: such a page would
otherwise be handed to the writer whole, and every outcome is wrong — the frontmatter comes back
as prose, or is dropped, or the deletion fails twice blaming a model for a page shape.

### D2 — the hand that typed the deletion has already approved it

D1 of ADR 039 reads, for this kind: **a HUMAN decides — at the command when they gave it, in the
inbox when a model did.**

A person's deletion enters through the authenticated doors — an MCP tool and the console's
Repairs page — and not through the CLI, which loses the verb. The tool takes the paths and the
reason, and in ONE pass against ONE fresh clone: computes the referencing set (deterministic,
before any model is asked), runs the review lane's own steward guard over the FULL touched set —
doomed and referencing alike, `all(...)` not `any(...)`, `_guard_repair_decision`'s rule
verbatim — writes the sweep (D1), proves the bounds, runs the nine gates, commits App-authored
with the deciding human in the trailer, and pushes without rebase (the lost-race amendment
stands). It returns the commit and the per-page diff, or a refusal that names its reason. One call names
at most ten pages — not a technical bound (the plan's byte ceiling is that) but a statement of what
one call MEANS: pages a person judged, never a corpus sweep typed in one line.

The proposal row is born `approved` in the caller's name and is `applied` or `failed` when the
call returns, never `pending`: the ledger, the console's history
and the metrics keep their source of truth without a second table, and `review_decide` stops
seeing a person's deletion at all. B2's reason for keeping creation CLI-only — "a button is a
surface with its own authorization question" — is answered rather than overruled: the question was
the per-path steward guard, it already existed, and it now runs in the act.

The asymmetry is ADR 041's, applied one kind over. A capture lands the moment a person submits it
because a person was there; a proposal a model wrote overnight waits because nobody was. A deletion
a person typed belongs with the first, and only an accident of which door it entered through had
put it with the second.

### D3 — what B4 protected, and what covers it now

B4 recomputed the plan at apply time for two reasons, and both need answering because a written
sweep cannot be recomputed.

*A page that gained a link to the doomed page between propose and apply.* For a person's deletion
there is no "between": the plan is computed against the clone it lands on, in the same pass. For
the one road that still rests in a table — B2's code-derived duplicate `sources/` deletions, which
nobody initiated and which therefore still wait in the inbox — the plan is written at propose time
and proved at apply time by the per-page `expected_before_hash` every scrub op already carries,
plus D1's third bound over the clone. A page that gained a reference since is either a changed
page (hash mismatch, refused) or a new one the third bound catches. The recompute was belt and
braces over those two, and the belt is gone because it cannot hold prose.

*A stored row edited between Approve and apply, writing a sentence nobody proposed.* For a
person's deletion the row never rests, so the window does not exist. For the duplicate road it
inherits exactly `entity-body`'s posture — stored bytes, a base hash, the gates — and exactly its
exposure, which ADR 039's first amendment accepted for a drafted body and this one accepts for a
written sweep.

### D4 — where the model runs, and what the architecture keeps pinned

The MCP server process already runs a model: `ask` builds a `pydantic_ai` agent inside the tool
(`stigmergy.answer.service`). What `test_only_the_proposer_loads_a_model_stack` pins is narrower
and stays true: the repair APPLY path — `remote.py`, `store`, `schema` — imports no model stack.
The sweep writer lives on the propose side, beside the other three model roads, and the server's
tool reaches it through ONE declared symbol: a named exception in `tests/test_architecture.py`
with its own pruning test, the same shape as the review lane's list. `remote.apply_approved` is
handed a finished plan, as it is today.

The writer's brief lives in the knowledge repo beside the proposer's
(`.claude/skills/`), read at run time from the checkout; a missing brief is a named refusal,
never a default prompt in code (039's D4, unchanged).

### D5 — the diff is the reading, on the road where nobody could read it earlier

The one road that still rests in the inbox — B2's code-derived duplicate `sources/` deletions —
shows the steward every planned body IN FULL, exactly as `entity-body` shows its draft: there a
person decides before the push, and hiding the only thing worth reading would be that kind's own
mistake made twice.

On the act road, nothing human reads the rewritten prose before it is pushed. That is the trade, and it is stated
here rather than softened: the fidelity of a rewritten paragraph has no proof code can run, and
the only reading a pending row ever bought was the steward's. What replaces it: the tool returns
the per-page diff, so the agent in the conversation reads it back to the person who asked; the
gates prove structure; the gardener's editorial sweep reads every changed page on its next run;
and the page's last good bytes are one commit back. Those diffs are page CONTENT, so they obey
what every other surface that echoes a page obeys — `acl.visible()` decides who may read one
(being a steward of a folder is not being in a page's audience, and a withheld diff is NAMED
rather than dropped) and each is fenced as UNTRUSTED-DATA, since a diff carries both the page's
own bytes and fresh model output. The undo is git, by an operator with a
checkout, with the knowledge repo's authorship-baseline cost when the sweep touched `views/` or
`sources/` — a governed revert is not in this ADR.

## What this deliberately does NOT do

- **No model proposes a deletion.** B2's first paragraph stands word for word: `validate_batch`
  refuses it by name, in every spelling. The model writes what a deletion does to the pages that
  remain; it never writes which pages go.
- **No autonomous apply for anything a MODEL initiated.** The nightly proposer's `edits`,
  `entity-body` and `entity-alias` proposals, and the code-derived duplicate deletions, wait in
  the inbox exactly as before. D2 moves one kind's one road, the one a person already decided.
- **No preview-then-confirm.** A preview that applies on a second call needs the written plan
  stored between the two, because a model does not recompute — and a stored plan awaiting a
  second call is a pending row under another name. The choice is binary: the inbox, or the act.
- **No fifth kind.** A consolidation — a human naming which of two pages survives and a model
  folding what the other knew into it — is the written sweep with one more instruction in the
  brief and one more bound (the survivor may gain), and it would be the next amendment, not a
  clause smuggled into this one. `model-superseded-canon` becoming proposable rides the same shape
  and waits for the same decision.
- **No change to what may be deleted.** `wiki/entities/` stays undeletable, the three content
  zones stay a whitelist, provenance pages stay declared as such to the gates.

## Consequences

- `deletion.py` loses its prose scrubber, its "unremovable reference" refusal and its
  recompute; `cli.py` loses `delete`; `remote.py` loses the recompute's drift finding. The module
  gains nothing but a caller.
- The server gains one tool whose contract is paths and a reason in, a commit and a diff out; the
  README's pinned tool count moves, `tests/test_readme_claims.py` says so; the console's Repairs
  page gains the same action and its `delete` renderer stops saying "nothing else changes here",
  which would now be false — it shows the diff.
- `docs/reference/repair.md`, `repair/index.md`, and every sentence that names
  `stigmergy-repair delete` are rewritten in the same change (`tests/test_docs_claims.py`).
- The knowledge repo gains the writer's brief; the platform suite cannot see it, so the change
  is not landed until both repositories are green.
- The change touches steward resolution and the facts the gates are told, so the auditor is
  mandatory; and because the defect was found on the deployment, the verification is a real
  deletion on staging of a page a survivor cites in prose, read back as a diff — one sitting.
- The SpaceX note keeps its orphaned callout until somebody edits it: this ADR changes what the
  next deletion leaves behind, not what the last one left.

## Alternatives rejected

- **Remove the whole callout block deterministically when its only subject is a going page.**
  Code wrote that block (`page.with_callout`), so its shape is known — and it is exactly the
  suffix list `gardener/sweep.py` warns against: it answers the callout and misses the sentence
  above it, the markdown link in the view, and every shape nobody wrote down.
- **Keep the scrubber and let a model polish its output.** Two writers of the same page, and a
  second road kept alive as a floor. The repo's own rule for a frontmatter block — *one owner, or
  two writers and a gate could disagree* — applies to a paragraph.
- **Keep the proposal pending and let the steward read the written sweep before it lands.** It is
  what today does, and it asked the same person the same question twice while supplying only an
  authentication. Where a second human genuinely should read — a model-initiated change — the
  inbox stays.
- **Keep `delete` in the CLI and add the tool beside it.** The CLI cannot authorize, so it would
  keep needing the inbox: two roads for one intent, the exact shape this ADR removes.
