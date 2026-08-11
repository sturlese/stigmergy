# The knowledge repository

This platform stores no pages. It reads and writes a **separate git repository** — the knowledge
repo — which is where your knowledge actually lives. Point at it with `--repo`, or with
`STIGMERGY_REPO` for the commands that read it: the librarian, the gardener, the digest, the views
and the entity tooling all default to `../stigmergy-brain`, a sibling of this checkout, when
neither is set. **`stigmergy-index`, `stigmergy-search` and `stigmergy-server` do not** — they read
no `STIGMERGY_REPO` at all and refuse without an explicit `--repo`, which is deliberate on a
process that serves reads to other people. Note also that nothing under `src/` loads a `.env` file:
copying `.env.example` sets nothing until you export it (`set -a && . ./.env && set +a`).

The separation is the point: delete this platform and you still have your knowledge, as markdown
files, with history. This document is the contract that repository has to satisfy. For what goes
*inside* a page, see [the page contract](./page-contract.md).

## Starting from nothing

No zone directory has to exist before something writes into it — the corpus walk skips a zone that
is not there — and four of the five `ops/` JSON files fall back to a safe empty default. But a repository
with *nothing* in it is not a working starting point, and each surface refuses for its own reason.
What each one needs before it will run at all:

| To run | The repo must carry | If absent |
|---|---|---|
| `stigmergy-index --rebuild` | **at least one page** under `wiki/` · `sources/` · `views/` | `EmptyCorpusError`. And since the server refuses to serve an empty index, this is what makes a truly empty repo unusable rather than merely quiet: you cannot index it, so you cannot serve it, so you cannot `brain_submit` into it |
| `stigmergy-server` | `ops/identities.json` | stdio refuses to start; HTTP refuses every request with the generic `401`. Never an open brain, on either transport |
| the librarian worker (either backend) | `.claude/tools/stigmergy_lint.py`, **in the commit it files against** | `LibrarianConfigError` at startup, before a single item is claimed |
| the librarian worker, `--backend pydantic` | `.claude/skills/librarian/SKILL.md`, same commit | the same refusal — the real agent has no operating procedure without it |
| `stigmergy-entities` / an approved entity proposal | `ops/templates/entity.md` | `EntityError` — a new entity page is that template with its identity fields filled in, and the command deliberately carries no copy of its own |
| anchoring to succeed rather than park | **at least one entity** in `ops/entity-registry.json` | nothing breaks, but every capture asks about the name it met and parks. That is the design working; it is still not a first run anybody enjoys |

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
│   └── entities/      # one page per registered entity; written only by `stigmergy-entities`
├── sources/           # captured raw material, verbatim — never edited
│   ├── meetings/      # the transcript behind a meeting page
│   ├── slack/         # the thread behind a 🧠 capture
│   └── drive/         # the document behind a `stigmergy-drive drop`
├── views/             # views/<entity-id>.md — written only by `stigmergy.views`
└── ops/               # configuration the platform reads
    ├── identities.json        # REQUIRED — the server fails closed without it
    ├── entity-registry.json
    ├── acl.json
    ├── stewards.json
    ├── slack-channels.json
    └── templates/
        └── entity.md         # REQUIRED to mint an entity — the shape a new entity page takes
```

## The three zones

`wiki` · `sources` · `views` (`index.corpus.ZONES` — an include-list, so a directory absent from it
is not indexed). Only markdown files under these three directories are indexed as pages — anything
else in the repo is invisible to search, which is what makes `ops/`, `.claude/` and a `README.md`
safe to keep beside them.

The zones differ by **who writes them**:

| Zone | Written by | Contains |
|---|---|---|
| `wiki/` | people; the librarian, through the eight gates; the meeting flow (`wiki/meetings/`); `stigmergy-entities` (`wiki/entities/`) | what someone concluded |
| `sources/` | the librarian worker only, from the captured material, byte for byte | what someone said or sent — written once, never edited |
| `views/` | `stigmergy.views` only — two triggers, one writer: `stigmergy-views regenerate` by hand, and the librarian right after a meeting files (best-effort) | derived rollups, regenerated from their members |

A capture **door** (the 🧠 gesture, `stigmergy-meeting drop`, `stigmergy-drive drop`, `brain_submit`)
never writes a page. It puts a row on the queue and uploads the original bytes to the evidence
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
governed command, or a flow that owns it). A capture that would need one parks in `triage` with a
message naming the type and why, rather than being quietly downgraded to a `note`.

This is deliberately small. The agent's write path is allow-listed to `.md` files in these three
folders *that do not already exist*, so "the agent overwrote a page" is not a failure mode that
needs detecting — it is unrepresentable.

## `ops/` — the configuration files

One is required; the other four fall back to a safe empty default when absent. **Malformed is
never the same as absent**: every one of these loaders raises rather than degrading quietly,
because a scoping or identity file the platform cannot parse must never be read as "no
restrictions apply".

| File | Shape | What it controls | Absent |
|---|---|---|---|
| `identities.json` | `{identity: "*" \| [audience labels]}` | who exists and what they may see. `"*"` means unrestricted. The HTTP transport resolves a bearer token to an email and then looks that email up here, on every request | **REQUIRED** — stdio refuses to start, HTTP refuses every request with the generic `401`, and an identity absent from the file gets no access at all |
| `entity-registry.json` | `{"entities": {id: {name, type, aliases}}}` | the entity vocabulary. Anchoring resolves against it; a name it does not carry cannot be anchored to, which is what makes the librarian *ask* instead of inventing. Written only by `stigmergy-entities` | an empty registry — the graph works unregistered, with no aliases and no entity-first resolution |
| `acl.json` | `{"version": 1, "rules": [{"path": "wiki/**", "acl": [labels]}], "default": [labels]}` | which audience labels get **stamped** on a page as it is filed, by path prefix; first match wins, else `default`. A resolved empty list means no `acl:` line at all, i.e. open | no file → an open corpus (nothing is labelled) |
| `stewards.json` | `{"<zone path prefix>" \| "*": email \| [emails]}` | who may decide a review item, and who the doorbell rings when one parks. Longest matching prefix wins; `"*"` is the universal fallback | an empty map — nobody resolves for any scope, every review decision fails closed, and the doorbell records the undeliverable notification instead of swallowing it |
| `slack-channels.json` | `{channel_id: [audience labels]}` | the audience scope a public-channel answer or a broadcast is computed at, so a digest cannot spill scoped material into an open channel | the **empty set** for every channel — which, per the ACL truth table, sees only pages carrying no `acl` label |

`identities.json` is the one with teeth. THREE of the other four are read at the commit the work
is happening against, never from a working tree: the librarian resolves `acl.json`,
`entity-registry.json` and `stewards.json` at the base commit each item files against, so an
uncommitted local edit cannot change what a page is stamped with or who may sign it off.
`slack-channels.json` is the exception — it is read as a plain FILE path, which on the deployed app
is the copy the deploy baked into the image, so a channel's scope changes only with a redeploy.

`ops/` carries one more thing that is not configuration: **`ops/templates/entity.md`**, the page
shape a newly minted entity takes. `stigmergy-entities` and a server-side approve both render a new
entity page from it and both refuse loudly when it is missing, because the template is the knowledge
repo's own source of truth for what one of its pages looks like — not something this platform should
be supplying from the outside.

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

**The librarian brief is backend-NEUTRAL** ([ADR 033](../decisions/033-structured-filing-flow.md)):
it describes a worker that hands the agent its context in one message and writes the page from one
structured account, and it names no tool. Which tools a particular run actually holds is stated in
the platform's own preamble in front of it, so a brief in this repository never has to be rewritten
when a backend changes.

**A `.mcp.json` in this repository is content, not configuration, and the librarian treats it as
such.** The agent runs with an explicitly empty MCP server list and strict MCP config, so the
knowledge repo cannot declare a server the filing agent would then boot. It got that way the hard
way: the first real agent-backed run booted the repo's own MCP servers and hung until it was
interrupted. A file in the repo can declare any command; the worker's own environment allow-list is
only a defense if nothing in the repo can add to it.

## What the platform will never do to your repository

- **It never force-pushes and never rewrites history.** Every commit is additive.
- **It never commits content the gates did not approve.** The commit is scoped to exactly the
  approved paths *and their approved bytes*, so neither an unrelated file that happened to be dirty
  in the worktree nor an in-place rewrite of an approved page can ride along.
- **It never writes outside the three zones and `ops/`** — and `ops/` only through
  `stigmergy.entities`: a steward's own commit from the CLI (authored as the steward), or a
  server-driven mint from MCP, Slack or the console (authored as the librarian App, with the
  approver named in an `Approved-by:` trailer instead) —
  [ADR 030](../decisions/030-server-side-entity-minting.md).
- **One writer at a time.** The librarian claims a single queue item, works in a throwaway
  `git worktree`, and fetches before every claim. A concurrent human push is detected and the item
  is retried, never overwritten.
