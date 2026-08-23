# Removing pages — `stigmergy.repair`

**A person decides that pages should leave the brain; code works out what that costs, and the
worker performs it.** Removing a file is trivial. What is not trivial is that the corpus
afterwards still has to be a graph — the knowledge repo's contract linter treats an unresolvable
`[[wikilink]]` as an ERROR, and `gate_contract` turns that into a veto — and still has to READ: a
sentence that cited the page, a callout that announced an overlap with it, must not be left saying
something that stopped being true. So a removal is not "delete a file", it is a **sweep**: the
pages that go, and the full planned bytes of every page that referred to one of them.

It enters at an authenticated door — `brain_delete` over MCP, or the console's Remove pages
button — where the judgment was already the person's, so what a second click would supply is an
authentication and there is nobody left to ask. What the door does is QUEUE it, as a `delete` row
in the capture queue; the librarian worker performs it, because the worker is the only process
that writes to the knowledge repo at all.

`brain_delete`'s own tool contract is in
[`server.md`](./server.md#the-capture-tools-the-write-path), the flow that performs it in
[`librarian.md`](./librarian.md), and the console's panel in
[`admin-console.md`](./admin-console.md). Code map:
[`src/stigmergy/repair/index.md`](../../src/stigmergy/repair/index.md).

> **What used to be here.** An ELECTIVE repair loop turned gardener findings into proposed
> repairs — additive edits, drafted entity bodies, entity merges — derived by a model overnight and
> applied without anybody being asked. Measured against [`DESIGN.md`](../DESIGN.md) §2 it had
> applied five repairs in three weeks of the author's daily use; its detectors went with the
> gardener's model passes, and a capture now brings a page up to date directly
> ([`librarian.md`](./librarian.md)). It was removed. Its rows are still in the `repairs` table and
> still render on the console — see [Reading what happened](#reading-what-happened).

**The covenant, in one sentence: a PERSON decides what goes, CODE computes the structure, a MODEL
writes only the prose, and code proves both against the tree it commits in.** There is no step
where a model chooses a page to remove, and no step where it may touch anything but a body.

---

## Structure is code's; prose is a model's

`repair.deletion` owns the deterministic half and every bound the written half has to satisfy:

- **which pages go** — exactly the paths the person named, never one more;
- **which pages refer to them** — the whole corpus, scanned with the frozen contract linter's own
  link rules (code fences and inline code blanked first, alias and anchor split off, the last path
  segment minus `.md`), plus one shape the linter does not count: a markdown link at a going page's
  path, because a writer reconciles prose and a path in prose is a reference either way;
- **their FRONTMATTER**, with every `related:`/`sources:` entry and every `supersedes:`/
  `superseded_by:` pointer that named a going page dropped — a lookup, not a judgment.

`repair.sweep` writes the bodies, in ONE model call over the whole referring set — a question about
how a set of pages refers to something must see the set — and code proves the bounds a reader would
otherwise have to check by eye:

- the set of pages written IS the set of AUTHORED pages that refer to a going page: none outside
  it, none missing, none twice. A `sources/` page is a filed document's provenance, so it stays
  code's: asking a model to argue with a document somebody actually sent is not a reconciliation,
  and the first real call on the deployment refused for exactly that reason;
- a body's title line stays, a body never opens a `---` block, a body is never emptied, and a body
  never GROWS past a small slack (`MAX_BODY_GROWTH_BYTES`, 512) — a sweep reconciles references, it
  does not write;
- and through `deletion.validate`, the same two bounds the apply proves again: every scrubbed
  page's frontmatter is code's own scrub of the page as it stands, byte for byte, and nothing
  written still refers to a page that is going.

One retry carrying the reasons, then a refusal naming the page. **There is no deterministic
fallback**, deliberately: two writers of the same page are two implementations that can disagree
about it, and a floor the model "usually" clears becomes the road the failures travel.

---

## The vocabulary is closed

A stored plan is a list of ops, and there are exactly two names
(`repair.schema.DELETE_OP_NAMES`):

| op | carries | what it means |
|---|---|---|
| `delete-page` | `path` | this page stops existing |
| `scrub-page` | `path`, `expected_before_hash`, `planned_after` | this page is rewritten in full: the bytes it was computed FROM, so "the corpus moved" is a fact rather than a guess, and the bytes it will carry, so the apply has something to byte-compare |

Two names rather than one, because a reader of the ledger has to be able to tell "three pages
removed" from "eleven pages rewritten" without opening the diff.

**What may be deleted** is the fast lane's own folders plus `sources/`
(`deletion.DELETABLE_PREFIXES`). `wiki/entities/` is absent BY CONSTRUCTION rather than by
exclusion — an entity page's type carries no folder in `page.FOLDER_BY_TYPE`, so it is not in
`gates.ALLOWED_WRITE_PREFIXES` and could only be added deliberately. An identity is retired by
removing what made it one, never by deleting the page out from under the pages anchored to it.

**What may be scrubbed** is wider on purpose: any corpus page (`wiki/`, `sources/`). An entity page
may perfectly well cite a note that is going, and refusing to scrub it would leave the dead link
the sweep exists to prevent.

---

## `brain_delete` — a person removes pages

```
brain_delete(paths=["wiki/notes/Old Memo.md"], why="what makes it stale")
```

One request, and nobody is asked afterwards. **The authorization is one question, asked before
anything is queued: is the caller an UNRESTRICTED identity** — no audience restriction in
`ops/identities.json`. It is the one fact the server can settle at the door, and the right one: a
removal touches the pages named AND every page that refers to them, a set nothing knows until the
corpus is read, so only a caller who can already see the whole corpus may ask for it. A scoped
caller gets the door's one anonymous refusal — *there is nothing for you to remove at those
paths*, the same sentence whether or not the paths exist — which is therefore no oracle about a
referrer either. The console's Remove pages button reaches the same seam under the console's own
token.

What the door then writes is a `delete` row in `capture_queue`: the reason as its material, the
pages in its hints, the caller as its `submitted_by`. **The worker performs it**, in its own
ephemeral worktree and with the only git credential the deployment has: `deletion.plan` for the
frontmatter and the referring set, `sweep.write_sync` for the bodies, the nine gates, the knowledge
repo's own linter over the whole tree, one App-authored commit with the caller in an `Approved-by:`
trailer, and a push. The whole of it is `librarian.processing.process_delete_item`.

- **What comes back at the door** is a queue acknowledgement naming the row — never a commit,
  because there is not one yet.
- **What comes back afterwards** is that row's report: the pages removed, and a unified DIFF per
  rewritten page. It is read through `brain_submissions`, which fences each diff as untrusted data
  (it carries both page bytes and fresh model output) and passes every path through
  `acl.visible()` for whoever is reading — one place decides read access, whatever the caller was
  allowed to remove — with a path it withholds NAMED rather than dropped, so nobody reads "nothing
  happened to it" into a silence. Nobody read that prose before it landed — that is the trade,
  stated rather than softened — so the diff is the reading, and `git revert` in the knowledge repo
  is the undo.
- **What it refuses at the door, with nothing queued**: an audience-restricted caller, no page, no
  reason, more than `MAX_DELETED_PAGES` (10) pages in one request, an entity page, a path outside
  the corpus.
- **What the worker then refuses**, as a `rejected` capture carrying the reason: a page that is not
  there, a plan over `STIGMERGY_REPAIR_MAX_PLAN_BYTES`, a frontmatter reference the sweep cannot
  rewrite, a body the writer could not reconcile in one retry, a gate's veto, and a dead link the
  sweep would have left behind. A reason matching a likely secret or a personal-data pattern is
  refused there too, by the same scan every capture's material passes. Every one of those lands
  nothing at all.

| Setting | Default | Effect |
|---|---|---|
| `STIGMERGY_REPAIR_MODEL` | the librarian's own default model | which model writes the pages that stay |
| `STIGMERGY_REPAIR_MAX_PLAN_BYTES` | `100000` | how much ONE plan may carry, in bytes — it holds every page it would rewrite in full, so the bound is a size rather than a count of pages (~30 average pages) |
| `STIGMERGY_REPO` | — | the checkout the worker plans and commits in |
| `STIGMERGY_INDEX_DSN` | — | where the queue and the ledger live |

---

## The writer's own procedure lives in the knowledge repo

The system prompt is a code-owned header plus `.claude/skills/removal-sweep/SKILL.md`, read at run
time from the checkout being swept — at the removal's own BASE commit, so the procedure that
governs a rewrite is the one the commit it is computed from carries. The same arrangement the
librarian's filing skill has. How a sentence is reconciled is editorial, and it belongs to the
people whose brain it is. The path is fixed and there is no
second place to look: a checkout whose skill sits elsewhere fails the read naming the path it
looked for, because a sweep that quietly ran on a procedure nobody versioned is worse than one
that refuses.

What the skill cannot change is the header: no tools at all, the pages it must return and no
others, "reconcile, never rewrite", and the rule that a fenced page body is data somebody wrote and
never an instruction. A knowledge repo cannot widen the writer's powers by rewriting its procedure.

A missing or empty skill is a NAMED refusal and nothing is removed. A writer briefed only by the
header would know what it may not do and nothing at all about what is worth doing. A `SKILL.md`
that is a SYMLINK is refused the same way: both `getsize` and `open` follow one, so the size ceiling
would measure the target instead of guarding it, and whatever the link pointed at on the host would
become the system prompt.

---

## What has to agree before anything is pushed

Four independent checks, each chosen because the others cannot see what it sees:

1. **`deletion.validate` against the tree the commit is made in.** The plan was computed against a
   worktree at the row's base commit, and the remote may have moved since. Every path is judged
   again — the lane, the dotfile rule, containment resolved rather than inferred from the string's
   shape, the symlink rule — and both written-sweep bounds are re-asked of the stored bytes.
2. **The base hash per scrubbed page, and a walk of the corpus.** The hash says whether a page the
   plan rewrites changed since the plan was made; the walk says whether a page the plan does NOT
   rewrite now refers to a going page — the latecomer that would otherwise survive the removal as a
   dead link. Either refuses the whole plan.
3. **`run_gates(ALL_GATES)` over the resulting diff**, exactly as it judges a filing. The caller
   tells the gates four facts and only four, each derived from the ops that were just performed:
   the lane the plan spans, the paths it may REMOVE (`gate_zone`'s oldest veto is "the librarian
   never deletes a file", and this is the one thing that stands it aside), the exact bytes it
   computed for every page it rewrites, and — among those — the machine-zone pages whose provenance
   stamps it only ever removes a link from.
4. **The knowledge repo's own linter over the WHOLE tree**, asked one question: does anything still
   link to a page this sweep removed? `gate_contract` filters the linter to the pages a diff
   touched, which is right for every other flow and blind for this one — a removal's blast radius
   is the whole graph, and a page the sweep never planned is exactly where a missed reference would
   sit. Scoped to the deleted stems, so a corpus already carrying an unrelated contract error is
   not this removal's fault.

Then `gitcmd.commit(gated_entries=…)` closes the last window: the diff the gates approved is the
diff that lands, bytes included.

---

## Reading what happened

Two records, and each outlives something the other does not.

- **The capture** carries the reading whoever asked gets back: the pages removed and a unified diff
  per rewritten page, ACL-scoped and fenced. It is purged with the retention window.
- **The `repairs` ledger** carries one row per removal that LANDED: the kind, the paths, the ops,
  the reason the person gave, the commit and the whole diff. It is not purged, and it is what the
  console's Repairs page reads. A removal carries no `content_key`: the column and its unique index
  belong to the elective loop, whose whole problem was deriving the same repair twice, and a person
  who asks twice is entitled to two records.

**An old row of a retired kind still reads.** `repair.schema.KINDS` is what a ROW MAY CARRY —
`delete` plus the three the elective loop wrote — while `WRITABLE_KIND` is what this version
inserts. The difference is not cosmetic: `ALTER TABLE … ADD CONSTRAINT … CHECK` validates the rows
already in the table, so a vocabulary narrowed to what code writes would abort the whole startup
DDL sequence on every deployed database that holds one. The console labels those kinds *(retired)*,
renders their ops generically, and says on the row that the loop that wrote them is gone.
