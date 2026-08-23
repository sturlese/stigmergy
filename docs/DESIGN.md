# The definition

**What this system is, and the closed list of things that earn their place in it.**

This is the one document that says what Stigmergy *is*. It is not a history: it does not record
what was tried, what was rejected, or in which order. That belongs in git. And it is not a
description of a mechanism — [`reference/`](./reference) is *what* each subsystem does, and the
`index.md` beside each package is *where* its modules live.

What this document holds is the part that decides whether a subsystem gets to exist at all: the
shape, the forced list, the page vocabulary, and the rules that are not negotiable. **Read it
before adding anything, and before defending anything from removal.**

One thing it deliberately does not hold: the reason a particular guard is written the way it is —
the attack it exists for, the defect that motivated it, the "if you change this you must also
change that". Those live **in the comment or the test that owns them**, next to the code they
constrain, because a reason kept in a separate document is a reason that goes stale without
anything failing.

---

## 1. What it is

A **Karpathy wiki, in git, in the cloud, for a team.**

The starting shape is one person's: instead of re-deriving an answer from raw sources on every
question, a model keeps a markdown wiki current — reading each new source, folding it into the
pages it touches, maintaining the cross-references. Knowledge compounds in one place instead of
being recomputed per query, and the bookkeeping that makes people abandon wikis is done by
something that does not get bored.

Stigmergy is that, pointed at an organization:

- **Knowledge is markdown in a git repository you own** — a *separate* repository, which this
  platform reads and writes and never contains. Delete the platform and the knowledge survives,
  in files, with history.
- **Material comes in through a door that needs no install** — Slack, or `brain_submit` from any
  MCP client. Anything that requires a person to run this stack is not a door.
- **Answers come out through one API** — the MCP server, with hybrid retrieval (full-text +
  vector) and an answer that is verified against what the tools returned or refused.
- **Code decides what lands**, never the model.

Two narrow seams do all the work: **one writer into git, one API out of it.** Everything else is
either forced by §2 or must justify itself against it.

---

## 2. What multi-user forces

A personal vault needs none of this. A shared one needs all of it, and the argument for each is
the same shape: *in a personal vault the cost of getting this wrong is borne by the person who
got it wrong; in a shared one it is not.*

This list is **closed**. Nine entries:

| Forced | Why a team forces it | Where it lives |
|---|---|---|
| **Identity** | An action has to be attributable to a person, and the person cannot be whoever the client claims to be | the server resolves it per request; the transports map their own users onto it |
| **Visibility** | A team wiki holds salaries, board material, a customer's confidential figures. Not everyone may read everything | `server/acl.py` — `visible()` is the ONE enforcement point, on every read surface |
| **A durable queue** | Two people capture at once, a worker dies mid-file, a model provider is down. "It was submitted" must survive all three | `stigmergy.capture` — leases, attempts, redelivery |
| **One writer** | Concurrent writes to the same git repository, from a model, is a corruption story. One process, one item at a time | the librarian worker |
| **Attribution** | Every page has to answer "who put this here" for the next person, not for an audit | stamped by the server on the capture row; carried onto the page |
| **Gates over the diff** | A model rewriting somebody else's page is a failure with no owner. So the last word is code | `librarian/gates.py` — nine gates over the diff, and the diff they approved is provably the diff that lands |
| **Retrieval at scale** | One person greps their vault. A team's corpus needs ranking, and ranking needs an index | `stigmergy.index` — Postgres + pgvector, rebuildable from the repo |
| **A door that needs no install** | A colleague who has to set up a stack does not capture anything. This is a product constraint, not a technical one | `stigmergy.slack` and the MCP server |
| **The evidence plane** | `sources/` is verbatim — and a `content_hash` proves nothing unless the original bytes still exist somewhere. Raw material never enters git; git holds the knowledge and the pointer, the store holds the proof | `capture/evidence.py`, content-addressed on `sha256`; MinIO locally, R2 in staging, one code path |

**The subtraction criterion, for everything else:** anything not on this list must demonstrate
*use* — not correctness, not test coverage, not elegance. **Passing tests is not evidence of
use.** A subsystem that works perfectly and that nobody's work flows through is a subsystem to
remove, and the number that says so is in the database, not in an argument.

---

## 3. The page vocabulary

**Four types.** A page's `type:` says what kind of claim it makes; the folder is where that kind
lives, and nothing else:

| `type:` | Folder | Written by | Holds |
|---|---|---|---|
| `note` | `wiki/notes/` | the filing agent | what someone concluded — a synthesis, a decision, a distilled session |
| `concept` | `wiki/concepts/` | the filing agent | a thing the organization thinks with, independent of any one event |
| `entity` | `wiki/entities/` | code, from the capture that introduced the identity | who or what pages are *about* — the identity, its aliases, and what is known about it |
| `source` | `sources/<door>/` | the worker, byte for byte from the captured material | what someone actually said or sent |

Everything about the shape of this list is a decision, so each one is stated:

- **`decision` is a `note`.** A decision is a conclusion someone reached; splitting conclusions
  into two folders by their grammatical mood buys nothing and forces a placement question at
  every filing.
- **There is no `meeting` type.** A transcript is a `source`; what was distilled out of it is a
  `note`. A meeting is an *event*, and an event is provenance, not a knowledge destination.
- **There is no `view` type.** A per-entity rollup is a read-time answer, not a stored page —
  `describe_entity` already assembles it, per reader, ACL-scoped. Karpathy's concept stays; the
  materialised copy and the sweep that keeps it fresh do not.
- **`project` is an entity type, not a page type.** A project is an identity that pages anchor
  to. The entity page plus everything anchored to it *is* the project page; a page type would put
  the same thing in two places and let them disagree.
- **There is no `meta` type.** Operational state — what the last sweep found, what the queue is
  doing — lives in Postgres, the admin console and the digest: surfaces retrieval cannot reach. A
  `meta` page would be indexed, and then `ask` could answer a business question with the state of
  a maintenance pass.
- **`sources/` is a top-level zone, not `wiki/sources/`.** The zone boundary is what `gate_zone`
  enforces, and it carries the one absolute: `sources/**` is never rewritten.
- **No `index.md`, no `log.md`, no `hot.md`.** A single-user vault needs hand-maintained entry
  points; here `pages_index`, the capture queue and git history already answer those questions,
  and they answer them per reader. A global index of every title is also an existence leak: with
  visibility enforced, the *list of things that exist* is itself restricted.

---

## 4. The rules

**Structure goes in code, judgment goes in prose.** If a property can be decided by looking at
bytes, a gate decides it and the model is never asked. If it cannot — is this the same customer,
is this claim contradicted, is this worth writing down — it is the model's, stated in prose in
the skill, and bounded by the gates around it.

**The gates decide, never the model.** Nine of them run over the diff, and
`gitcmd.commit(gated_entries=…)` proves the diff they approved is the diff that lands. A model
can be argued with. A gate cannot.

**One pipe for material, one lane for removal.** Every capture archives its material verbatim to
`sources/<door>/`, always, and then writes pages into `wiki/`, all treated identically. `kind`
chooses the *prose, not the code path*: what the brief asks for, the byte cap, the `sources/`
subfolder. Nothing else. The one honest exception is removal — `brain_delete` is not material, it
is an instruction to remove, and it archives nothing.

**`sources/` is verbatim, and is never rewritten.** This is the one absolute. Everything under
`wiki/**` is the agent's to revise; a page that gets better is the whole point of the pattern.

**A rewrite is loud, or it does not happen.** Approval-before is visibility-after: nobody clicks
to confirm a write, so what stands in its place is that the change is attributed, diffed, and
revertible in a repository the team owns — *and that the page's own submitter is told when a
capture rewrites their page*. Two structural bounds are cheap and stay: the H1 survives a rewrite
(a rewrite must not turn a page into a different page), and `sources/` is untouchable. When the
model is unsure whether new material supersedes what a page says, it adds the contradiction
callout rather than picking a winner: a visible flag beats a silent wrong choice.

**An honest refusal beats a confident guess.** Any figure that cannot be traced back to what the
tools returned *this run* is withheld and the answer says so. A system that never refuses is the
failure, not the success.

**Untrusted content is never read as instructions.** Page bodies and captured material reach a
model inside one hardened fence, built in exactly one place, with in-band neutralization.

**Anything not forced by §2 must demonstrate use.**

---

## 5. What was decided, and why

The short list — the choices that would otherwise be re-litigated every few months:

- **The capture is the approval.** Somebody read the material and decided the brain should hold
  it. That is the human act; everything after it is bookkeeping. There is no review queue over
  what the librarian wrote, no second person confirming an entity, no proposal waiting. The undo
  is `git revert` in a repository the team owns.
- **An entity is born written.** A capture that meets a name the registry does not know
  introduces it, in the same commit as the page that mentioned it, with `approved_by:` naming the
  submitter. A spelling the registry already knows becomes an alias, not a twin. Nothing waits.
- **The audience comes from the door, not from the folder.** Who may read a page is decided by
  the person capturing, where they captured — the channel they posted in, or the groups they
  named. The folder is the page's *type*; an audience is metadata. That decision travels on the
  capture and is stamped on every page it writes, the verbatim source included.
- **A model never reads what the page it is writing could not cite.** Scoping the model's reads
  to the audience of the page being written is what makes it impossible to carry a restricted
  title into an open page by a link.
- **One repository, one API, one enforcement point.** Enclaves multiply the places a permission
  can be wrong; a second read surface multiplies the places an ACL can be wrong.
- **No scheduler outside the deployment.** Every unattended pass runs on the worker's idle
  branch, so maintenance never starts while a capture is waiting, "did it run" is one table, and
  a pass that silently stops looking green is not a failure mode. A maintenance pass fails the
  pass, never the boot.

**The trade this design makes, stated plainly rather than defended.** A capture that rewrites a
page is protected by the gates, by the audience check, and by attribution + diff + `git revert` —
**by nothing structural.** Byte-equality against a declared plan (`expected_bytes`) is meaningful
for a *repair*, where a model proposes, code validates, and code recomputes the bytes at apply
time and compares them against what was approved. For a **capture there is no separate approval
step**: the bytes are the ones the agent just wrote, so comparing them to what the agent wrote
proves nothing. It is vacuous, and it is not the gate. What makes the trade acceptable is that
the rewrite is not silent — which is why telling the rewritten page's submitter is part of the
design and not a nicety.

---

## 6. What is not yet true

This document is the definition; the code converges on it. What is still open, so that nobody
reads a false sentence above as a description of today:

- **The page vocabulary is still seven types in code** (`librarian.page.PAGE_TYPES`): `decision`,
  `meeting` and `view` have not yet been merged away.
- **There are still two filing flows** — the ordinary one and a separate meeting flow that files
  a page set. §4's one pipe replaces both.
- **A capture still cannot rewrite a page body**; it can only append through a closed vocabulary
  of declared edits. §4's rewrite rule is the change that opens it.
- **Views are still materialised pages** under `views/`, kept fresh by a convergence sweep.
- **Some subsystems have not yet been measured against §2's criterion** — the gardener's model
  passes, the elective half of the repair loop, the weekly digest.

Each line here is deleted by the change that makes it false, in the same commit.
