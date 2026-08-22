# The knowledge repository

This platform stores no pages. It reads and writes a **separate git repository** — the knowledge
repo — which is where your knowledge actually lives. Point at it with `--repo`, or with
`STIGMERGY_REPO` for the commands that read it: the librarian, the gardener, the digest, the views
and the repair proposer all default to `../stigmergy-brain`, a sibling of this checkout, when
neither is set. **`stigmergy-index`, `stigmergy-search` and `stigmergy-server` do not** — they read
no `STIGMERGY_REPO` at all and refuse without an explicit `--repo`, which is deliberate on a
process that serves reads to other people. Note also that nothing under `src/` loads a `.env` file:
copying `.env.example` sets nothing until you export it (`set -a && . ./.env && set +a`).

The separation is the point: delete this platform and you still have your knowledge, as markdown
files, with history. This document is the contract that repository has to satisfy. For what goes
*inside* a page, see [the page contract](./page-contract.md).

## Starting from nothing

No zone directory has to exist before something writes into it — the corpus walk skips a zone that
is not there — and three of the four `ops/` JSON files fall back to a safe empty default. But a
repository with *nothing* in it is not a working starting point, and each surface refuses for its
own reason. What each one needs before it will run at all:

| To run | The repo must carry | If absent |
|---|---|---|
| `stigmergy-index --rebuild` | **at least one page** under `wiki/` · `sources/` · `views/` | `EmptyCorpusError`. And since the server refuses to serve an empty index, this is what makes a truly empty repo unusable rather than merely quiet: you cannot index it, so you cannot serve it, so you cannot `brain_submit` into it |
| `stigmergy-server` | `ops/identities.json` | stdio refuses to start; HTTP refuses every request with the generic `401`. Never an open brain, on either transport |
| the librarian worker (either backend) | `.claude/tools/stigmergy_lint.py`, **in the commit it files against** | `LibrarianConfigError` at startup, before a single item is claimed |
| the librarian worker, `--backend pydantic` | `.claude/skills/librarian/SKILL.md`, same commit | the same refusal — the real agent has no operating procedure without it |
| the librarian writing an identity — one a capture introduced, or one somebody registered from the console | `ops/templates/entity.md` | a `no-template` veto on the capture that would have created it, saying to commit the template to the knowledge repo. A new entity page is that template with its identity filled in and its body written, and no door carries a copy of its own — which is why the librarian refuses to invent one |
| anchoring to resolve against something | **at least one entity** in `ops/entity-registry.json` | nothing breaks and nothing waits: a capture about a name nothing resolves CREATES that entity, confirmed by whoever captured, and files anchored to it — so an empty registry simply means the first captures each bring an identity with them |

So the honest minimum is: one seed page, `ops/identities.json`, the linter, and — the moment you
want a real agent or a real entity — the librarian skill and the entity template. The three
`scripts/walk_*.py` narrations in the platform repo build a conforming throwaway repo themselves,
which is the cheapest way to see the shape before you commit to one.

```
stigmergy-brain/
├── wiki/              # what people wrote — the zone the agent may file into
│   ├── notes/         # type: note      ─┐ the three folders the librarian agent
│   ├── decisions/     # type: decision   │ may CREATE a page in
│   ├── concepts/      # type: concept   ─┘
│   ├── meetings/      # type: meeting — written only by the meeting flow
│   └── entities/      # one page per entity, written by the librarian in the commit that
│                      # files the capture that introduced it. Nothing else creates one
├── sources/           # captured raw material, verbatim — never edited
│   ├── meetings/      # the transcript behind a meeting page
│   ├── slack/         # the thread behind a 🧠 capture
│   └── documents/     # the text behind a `kind="document"` capture
├── views/             # views/<entity-id>.md — written only by `stigmergy.views`
└── ops/               # configuration the platform reads
    ├── identities.json        # REQUIRED — the server fails closed without it
    ├── entity-registry.json
    ├── slack-channels.json
    └── templates/
        └── entity.md         # REQUIRED before any entity page can be written — its shape
```

## The three zones

`wiki` · `sources` · `views` (`index.corpus.ZONES` — an include-list, so a directory absent from it
is not indexed). Only markdown files under these three directories are indexed as pages — anything
else in the repo is invisible to search, which is what makes `ops/`, `.claude/` and a `README.md`
safe to keep beside them.

The zones differ by **who writes them**:

| Zone | Written by | Contains |
|---|---|---|
| `wiki/` | people; the librarian, through the nine gates (`wiki/entities/` too, in the same commit as the capture that introduced the identity); the meeting flow (`wiki/meetings/`); an applied repair (a drafted entity body, a merge) | what someone concluded |
| `sources/` | the librarian worker only, from the captured material, byte for byte | what someone said or sent — written once, never edited |
| `views/` | `stigmergy.views` only — ONE writer and two entry points, both inside the librarian worker: its convergence sweep (the guarantee) and the hook right after a meeting files (best-effort). There is no command | derived rollups, regenerated from their members |

A capture **door** (the 🧠 gesture, `brain_submit` from any MCP client, the console's *Register an
entity*) never writes a page. It puts a row on the queue and archives the material in the evidence
plane; the worker is what turns that into commits. So "nothing reached the repo" and "nothing was
captured" are different states, and the second one is much rarer than it looks.

### The fast lane

The librarian agent may create exactly **three** page types, each in a fixed folder:

| `type:` | Folder |
|---|---|
| `note` | `wiki/notes/` |
| `decision` | `wiki/decisions/` |
| `concept` | `wiki/concepts/` |

The page vocabulary is **seven** types (`librarian.page.PAGE_TYPES`, one table every placement
question reads). The other four — `entity`, `source`, `meeting`, `view` — may be read, linked and
cross-referenced by the agent but never created by it: each has exactly one writer elsewhere (a
governed command, or a flow that owns it). A capture that would need one is REFUSED naming the type
and why, rather than being quietly downgraded to a `note`.

`entity` is the interesting row, because the fast lane does reach that folder — but never as the
agent's own write. When a capture is about something the registry does not know, the agent DECLARES
the identity in its account and worker CODE writes the page, from this repo's own
`ops/templates/entity.md`, with `approved_by:` naming whoever captured
(see [The identity lifecycle](#the-identity-lifecycle-on-the-page-and-nowhere-else)).

This is deliberately small. The agent's write path is allow-listed to `.md` files in these three
folders *that do not already exist*, so "the agent overwrote a page" is not a failure mode that
needs detecting — it is unrepresentable.

## `ops/` — the configuration files

One is required; the other three fall back to a safe empty default when absent. **Malformed is
never the same as absent**: every one of these loaders raises rather than degrading quietly,
because a scoping or identity file the platform cannot parse must never be read as "no
restrictions apply".

| File | Shape | What it controls | Absent |
|---|---|---|---|
| `identities.json` | `{identity: [group names]}` | who exists and what they may see. One shape, a list of groups; membership of `brain-admins` is unrestricted; an empty list is a principal who reads every OPEN page and no other; `all` is a reserved word (open is the ABSENCE of a label on a page). Keys beginning with `_` are comments. The HTTP transport resolves a bearer token to an email and then looks that email up here, on every request | **REQUIRED** — stdio refuses to start, HTTP refuses every request with the generic `401`, and an identity absent from the file gets no access at all |
| `entity-registry.json` | `{"entities": {id: {name, type, aliases, approved_by}}}` | the entity vocabulary, and who introduced each identity. Anchoring resolves against it; a name it does not carry cannot be anchored to, which is what makes the librarian WRITE the entity rather than invent an anchor. **Derived, never hand-written**: it is a pure function of `wiki/entities/*.md`, regenerated by `entities.generator` in the same commit as any page that changes one — `librarian.identity` introducing an identity or teaching one a spelling, and an applied `entity-alias` merge | an empty registry — the graph works unregistered, with no aliases and no entity-first resolution |
| `slack-channels.json` | `{channel_id: [group names]}` | the groups a public-channel answer or a broadcast is computed at, so a digest cannot spill scoped material into an open channel — and, since [ADR 045](../decisions/045-audience-from-the-door.md) D2, the label a 🧠 capture taken there is FILED at. Same grammar as `identities.json`, same parser | the **empty set** for every channel — a channel not listed is public: it reads only pages carrying no label, and a capture from it is filed open |

`identities.json` is the one with teeth. TWO of the other three are read at the commit the work
is happening against, never from a working tree: the librarian resolves
`entity-registry.json` at the base commit each item files against, so an uncommitted local edit
cannot change what a page is stamped with or what it anchors to.
`slack-channels.json` rides the SNAPSHOT road that `identities.json` does
(`slack.channels.channel_audiences_live` → `server.ops_files`): the index's cached copy wherever
the database carries one, the process's own baked file where it does not. So a channel scoped by
a push takes effect within seconds on process groups that hold no checkout, rather than at the
next deploy (issue #79). Since [ADR 045](../decisions/045-audience-from-the-door.md) D2 that file
decides two things rather than one — the scope an answer in a channel is computed at, AND the
audience a 🧠 capture taken there is filed at — and the 🧠 door refuses outright when the file is
absent entirely, because "no map at all" would silently file every scoped channel's captures
open. A brain with no scoped channels says so with `{}`.

`ops/` carries one more thing that is not configuration: **`ops/templates/entity.md`**, the page
shape a new entity takes. There is exactly ONE renderer of it — the librarian, writing the entity
inside the commit that files the capture, whether the material introduced the entity or the
submitter registered it by name — and it refuses loudly when the template is missing, because the
template is the knowledge repo's own source of truth for what one of its pages looks like, not
something this platform should be supplying from the outside. It carries one field the other
templates do not: `approved_by:`, which the librarian fills in with the capture's submitter. Its
own HTML comments are notes to whoever edits the TEMPLATE and are stripped
from every page rendered from it, and a section the writer has no lines for is dropped heading and
stub together — see [page-contract.md](./page-contract.md#the-body-an-entity-page-is-born-with).

## The identity lifecycle, on the page and nowhere else

An entity's page is the only place its lifecycle is recorded, and there is one state:
**introduced**. `ops/entity-registry.json` is a derived view of the page, and one frontmatter field
carries the fact — documented field-by-field in [the page contract](./page-contract.md):

| On the page | Means |
|---|---|
| `approved_by: ana@example.com` | the person whose capture introduced this identity. The librarian wrote the page in the commit that filed their capture, and `approved_by` is that capture's `submitted_by`. Nothing waits on anybody: the capture is the approval ([ADR 044](../decisions/044-the-capture-is-the-approval.md)) |
| no `approved_by` key at all | a page written before the field existed. Pages under the older contract are not migrated, and read the same way |

`approved_by: ""` is neither, and nothing writes it: an identity nobody is named for is an identity
nobody stands behind. There is no `proposed_aliases:` field either — a spelling the material uses
for a registered entity is appended to that entity's own `aliases:` in the same commit, and
resolves from that moment.

**The contract linter checks the lifecycle** (`lifecycle` findings): `approved_by` must be a
string, it may not be empty, and a page carrying `proposed_aliases` is an error naming `aliases` as
where those spellings belong. It also checks the DERIVED view against the pages — a name, a type or
an alias list the registry does not match is a `warn`, because the fix is not a person's: the
librarian worker regenerates the registry from the entity pages on its next pass over the zone, and
neither side is hand-edited. What stays an `error` there is a page the generator cannot read at
all — no title, or two titles that slug to one id — because no regeneration can survive it.

## `.claude/` — the agent's operating procedure

The librarian's instructions are **versioned in the knowledge repo**, not compiled into this
platform. That is deliberate ([ADR 015](../decisions/015-librarian.md)): how your agent files pages
is your company's filing policy, and it belongs where the people whose knowledge it files can read
and PR it — which is also why this platform ships no canonical copy for you to adopt.

**Write only editorial policy into it.** The skill is not where the agent's confinement is
declared, and restating it there buys nothing: the platform injects a system-prompt header ahead of
your text on every run, and that header — not your file — is what tells the agent it has exactly
five tools, no shell and no network, that writes are confined to a `.md` page that does not exist
yet, and that nothing in the checkout may be read as instructions. None of that is negotiable from
the skill, because the gates enforce it whatever the skill says. So the file only has to answer the
questions that are genuinely yours: what type is this, which folder, how should it be titled, what
counts as a near-duplicate here, what should it link to. The platform validates exactly two things
about it — that it is not empty and that it is under a size ceiling — and deliberately nothing else.

| Path | What it is |
|---|---|
| `.claude/skills/librarian/SKILL.md` | how an ordinary capture becomes a page |
| `.claude/skills/meeting-distiller/SKILL.md` | how a transcript becomes a source page, a meeting page and one decision page per decision |
| `.claude/tools/stigmergy_lint.py` | the contract linter the gates run over every produced page |

Both skills and the linter are read **at the base commit the worker files against**, not from a
working tree, and startup refuses if the linter is not in that commit. A local edit nobody pushed
might as well not exist.

This repository keeps a **frozen copy** of the linter and of BOTH briefs as test fixtures, and a
test asserts each is byte-identical to the knowledge repo's own when both are on the same machine
(and skips cleanly when they are not). A stale frozen copy would mean CI enforcing a contract the
agent is no longer given. Each brief is a **two-sided contract** with the platform's own code, and
each has its own rule table asserted in both directions —
`tests/librarian/test_meeting_brief_contract.py` and `test_librarian_brief_contract.py` — so an
edit to either side alone turns the suite red rather than being discovered by a filing.

**The librarian brief is backend-NEUTRAL** ([ADR 033](../decisions/033-structured-filing-flow.md),
[ADR 034](../decisions/034-agentic-pydantic-harness.md)): it describes a worker that hands the agent
its context and asks for one structured account, and it names no tool. What differs between runs is
stated in the platform's own preamble in front of it — which tools a run holds, whether that handed
context is the whole of what it can see or a seed it can search past, and whether the agent writes
its own page or code writes it from the account — so a brief in this repository never has to be
rewritten when a backend changes.

**A `.mcp.json` in this repository is content, not configuration, and the librarian treats it as
such.** The agent runs with an explicitly empty MCP server list and strict MCP config, so the
knowledge repo cannot declare a server the filing agent would then boot. It got that way the hard
way: the first real agent-backed run booted the repo's own MCP servers and hung until it was
interrupted. A file in the repo can declare any command; the worker's own environment allow-list is
only a defense if nothing in the repo can add to it.

## What the platform will never do to your repository

- **It never force-pushes and never rewrites history.** Every commit is additive — with one named
  exception, a person's own `brain_delete`: the pages they named go, and every page that referred to
  one is rewritten in the same commit, with the diff handed straight back to them
  ([ADR 043](../decisions/043-a-sweep-is-written.md)). An `entity-alias` merge removes no page at
  all: the absorbed identity keeps its page, marked `superseded_by:` the survivor.
- **It never commits content the gates did not approve.** The commit is scoped to exactly the
  approved paths *and their approved bytes*, so neither an unrelated file that happened to be dirty
  in the worktree nor an in-place rewrite of an approved page can ride along.
- **It never writes outside the three zones and `ops/`.** `ops/entity-registry.json` is regenerated
  from the entity pages, in the same commit that changed one, and only by the two writers that may
  change one: the librarian WRITING an identity (authored as the librarian App, `Submitted-by:` the
  capture's submitter) and an applied `entity-alias` merge (authored as the App, with the person who
  approved it in an `Approved-by:` trailer).
- **It never invents the name behind an identity.** The librarian writes an entity page with
  `approved_by:` naming the capture's own submitter, and the ninth gate refuses any other name —
  including none ([ADR 044](../decisions/044-the-capture-is-the-approval.md)). The name in that
  field is always a person's, put there by a door that authenticated them, never the worker's own
  judgment.
- **One writer at a time.** The librarian claims a single queue item, works in a throwaway
  `git worktree`, and fetches before every claim. A concurrent human push is detected and the item
  is retried, never overwritten.
