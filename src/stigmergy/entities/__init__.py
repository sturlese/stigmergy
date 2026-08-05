"""`stigmergy.entities` — governed entity birth and the registry's derived view.

The rule this package makes real: *the librarian never silently mints an entity; it proposes, a
steward approves, the registry regenerates.* This package is the second half of that sentence — the
instrument a human drives, and **the only writer of `wiki/entities/` anywhere in this
codebase**. It is also the **ONE** path-scoped human-driven writer of `ops/`
(`ops/entity-registry.json` plus `wiki/entities/`). The fast lane's confinement sits beside it: the
librarian writes one page per capture inside a throwaway worktree and cannot reach any of `ops/`.
Nothing here runs in a worker, on a poll, or behind an agent.

**Where the proposal lives: in Postgres, not in a file.** The `triage` row IS the proposal — it
already carries the material, the agent's rationale and the unresolved name. A separate
`ops/entity-proposals.json` is deliberately not built: the librarian may not write `ops/`, so that
file could only ever be written by this CLI, duplicating the row and creating a DB<->file surface
to keep in sync. The durable, diffable artifacts are the ones that matter — the entity page and the
registry, both born in one commit.

**Three properties this package exists to hold, none of which is about convenience:**

1. **Resolve before mint.** An id, a name or an alias that collides with a registered entity is
   refused at the gate, naming the entry it collided with. This is the
   `northwind-slides.md`-next-to-`Northwind Group.md` class of defect that ungoverned minting
   produces within a week at fifty writers, and within one repo at one.
2. **The registry is a derived view, and derived means derived.** `ops/entity-registry.json` is a
   pure function of `wiki/entities/*.md` — the "derived view" ADR 008 describes, turned from prose
   into fact. `regenerate --check` is that claim made falsifiable, and `generator.canonical_id_for`
   is why an id nobody could regenerate is refused rather than stored.
3. **The commit is signed by the human.** `approve` commits the page AND the regenerated registry
   together, with the STEWARD's own git identity — not the App's. `git blame` answering "who
   approved this identity" is the whole of the governance; a PR flow with one steward and no branch
   protection would be ceremony.

Layering: `entities` imports `capture` (the queue's read path, the disposition seam and the
terminal's `_clean`), `kernel` (the canonical registry reader/writer, the normalizer and the
frontmatter parser) and three modules of `librarian` — `gitcmd`, `config` and `gates` — see
`clone.py` for why the first is a reach and not a rewrite. It must never import `server` or
`answer`, and only `cli.py` opens a database connection (through `index.store`), exactly as in
`capture`.

**The edge to `librarian.gates` is the secrets scanner and only that** (`cli._refuse_secrets`).
`approve`/`create` commit `--no-verify` — so the knowledge repo's own hooks do not run — and what
they commit is `--role` and `--aliases` text a steward typed with untrusted material on screen.
The librarian's `--no-verify` is defensible because the librarian runs gitleaks itself; this path
running none would make the *governed* door the unscanned one, into `main`, which is the place a
secret can never be deleted from. Same binary, same `--redact`, same `Finding` — a second scanner
invocation here would be a second answer to "is this a secret".

**The direction of that edge is one-way and load-bearing.** `librarian` must never import
`entities`: the unattended worker cannot depend on the steward's CLI. Where the two need the same
fact and the import would have to run the wrong way — the character set an entity NAME may carry —
it is stated at both ends with the duplication declared (`librarian.report._UNSAFE_IN_IDENTITY`),
the same posture `generator.FIX_COMMAND` takes about its copy in the knowledge repo's linter.
"""
