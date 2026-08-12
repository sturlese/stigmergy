"""`stigmergy.entities` — governed entity birth and the registry's derived view.

The librarian never silently mints an entity: it proposes, a steward approves, the registry
regenerates. This package is the human half — the only writer of `wiki/entities/` in this
codebase, and the ONE path-scoped human-driven writer of `ops/` (`ops/entity-registry.json` plus
`wiki/entities/`). Nothing here runs in a worker, on a poll, or behind an agent. The proposal
lives in Postgres — the `triage` row IS the proposal; a separate `ops/entity-proposals.json` is
deliberately not built (the librarian may not write `ops/`, so it would duplicate the row behind
a DB<->file sync surface).

Three properties, none about convenience: resolve before mint (an id, name or alias colliding
with a registered entity is refused at the gate — one thing under several identities is the
failure this governance prevents); the registry is a pure function of `wiki/entities/*.md`
(`regenerate --check` makes that falsifiable); and the commit is signed by the human whose
judgment it records, so `git blame` answers "who approved this identity".

Layering: imports `capture`, `kernel` and a declared set of `librarian` modules (the git dialect,
config, the secrets gate, the App credential); never `server` or `answer`; only `cli.py` opens a
database connection. All pinned in `tests/test_architecture.py`. The `librarian.gates` edge is
the secrets scanner and only that: `approve`/`create` commit `--no-verify`, so this scan is the
one that runs — the governed door into `main` (where a secret can never be deleted from) must not
be the unscanned one. The edge is one-way and load-bearing: `librarian` never imports `entities`
(the unattended worker cannot depend on the steward's CLI); where both need the same fact, it is
duplicated and declared at both ends.
"""
