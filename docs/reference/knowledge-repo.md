# The knowledge repository

This platform stores no pages. It reads and writes a **separate git repository** — the knowledge
repo — which is where your knowledge actually lives. Point at it with `STIGMERGY_REPO` (or `--repo`);
with neither set, every command looks for `../knowledge-repo`, a sibling of this checkout.

The separation is the point: delete this platform and you still have your knowledge, as markdown
files, with history. This document is the contract that repository has to satisfy. For what goes
*inside* a page, see [the page contract](./page-contract.md).

## Starting from nothing

An almost-empty git repository is a valid starting point. **One file is genuinely required**:
`ops/identities.json`. Without it the stdio server refuses to start and the HTTP transport refuses
every request — never an open brain, on either transport.
Everything else in `ops/` is optional and falls back to a safe empty default, and no zone
directory has to exist before something writes into it — the corpus walk skips a zone that is not
there.

```
knowledge-repo/
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
    └── slack-channels.json
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

## `.claude/` — the agent's operating procedure

The librarian's instructions are **versioned in the knowledge repo**, not compiled into this
platform. That is deliberate: how your agent files pages is your policy, and it should change
without a redeploy.

| Path | What it is |
|---|---|
| `.claude/skills/librarian/SKILL.md` | how an ordinary capture becomes a page |
| `.claude/skills/meeting-distiller/SKILL.md` | how a transcript becomes a source page, a meeting page and one decision page per decision |
| `.claude/tools/stigmergy_lint.py` | the contract linter the gates run over every produced page |

Both skills and the linter are read **at the base commit the worker files against**, not from a
working tree, and startup refuses if the linter is not in that commit. A local edit nobody pushed
might as well not exist.

This repository keeps a **frozen copy** of the linter and the meeting brief as test fixtures, and a
test asserts they are byte-identical to the knowledge repo's own when both are on the same machine
(and skips cleanly when they are not). A stale frozen copy would mean CI enforcing a contract the
agent is no longer given.

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
