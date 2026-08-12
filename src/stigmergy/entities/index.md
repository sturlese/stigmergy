# entities — governed entity birth, and the registry's derived view

The librarian never silently mints an entity: it proposes, a steward approves, the registry
regenerates. This package is the human half — the only writer of `wiki/entities/` in this
codebase, and the ONE path-scoped human-driven writer of `ops/`. Nothing here runs in a worker,
on a poll, or behind an agent. The proposal lives in Postgres: the `triage` row IS the proposal
(no proposals file — the librarian may not write `ops/`, so one would duplicate the row behind a
DB↔file sync surface).

Three properties: resolve before mint (a colliding id/name/alias is refused at the gate, naming
the entry); the registry is a pure function of `wiki/entities/*.md` (`regenerate --check` makes
that falsifiable); and "who approved this identity" is always answerable with a person — a
steward's own git identity on the commit, or, for a server-driven mint, the App's author line
plus an `Approved-by:` trailer naming the human.

## Modules

| Module | What it is |
|---|---|
| `cli.py` | `stigmergy-entities` — `list · show · approve · reject · create · regenerate [--check]`; a thin adapter over `mint.mint` (derives `author` from the steward's clone via `clone.preflight`); owns the printed-command safety (`_suggestable`) and is the only module here that opens a database connection |
| `situations.py` | which parked (`triage`) rows are an identity decision — `classify`, `subject_of` / `subjects_of`, the two semantic entry points (`list_pending_situations`, `get_situation`) and the write guard (`require_situation`) |
| `birth.py` | resolve-before-mint as a pure function of a proposal and a registry (`prepare`, `recheck`, `_refuse_collisions`) and the page renderer (`render_page`, `_yaml_str`, `commit_message`) |
| `generator.py` | the registry generator: `read_entity_pages`, `derive_registry`, `registry_of`, `compare` (semantic drift), `check` / `regenerate`, `canonical_id_for` |
| `clone.py` | the working copy's checks and push: `preflight` (branch/identity/clean/in-sync), `commit_and_push` with fetch-regenerate-retry, `write_page` / `discard_untracked`. Never repairs, never force-pushes |
| `mint.py` | the ONE mint orchestration `cli._mint` and `remote.mint_via_clone` both call: drift refusal, the gate against the registry the commit will PUBLISH, the template render, `generator.regenerate`, the gitleaks scan, ONE commit, bounded rebase-and-retry |
| `remote.py` | the server-driven mint door: `mint_via_clone` — a throwaway clone with the librarian App's credential, `mint.mint` with an `Approved-by:` trailer, cleanup in a `finally`. `credential` is needed only for `https://` remotes; raises only this package's own error types |
| `errors.py` | `EntityError` (base), `CollisionError` (the governance verdict), `CloneStateError`, `PushRaceError`, `CapabilityUnavailableError` (mapped by `stigmergy.server.review` to the server's equivalent — this package may not import `stigmergy.server`) |

## Use these

- `situations.classify` / `subject_of` / `subjects_of` — the ONE reading of "is this parked row an
  entity situation, and about what". `subjects_of` is the per-name list (a meeting park carries
  several, each independently approvable); `subject_of` the single display string it collapses to.
- `situations.require_situation` — the write guard before anything is validated or written; being
  able to read a row is not permission to mint from it.
- `birth.prepare` / `birth.recheck` — resolve-before-mint, pure (a `Registry` in, a `Proposal` or
  a raised `CollisionError` out). `recheck` is the SAME `_refuse_collisions` asked again after a
  rebase, never a second implementation of "collides".
- `generator.read_entity_pages` / `derive_registry` / `check` / `regenerate` — the only code that
  turns `wiki/entities/*.md` into a `Registry` or into `ops/entity-registry.json`; serialized
  through `kernel.registry.save_registry` / `load_registry`, never reimplemented.
- `generator.canonical_id_for(name)` — what a page titled `name` always regenerates as
  (`slugify`, not `normalize`; its docstring says why the two answer different questions).
- `mint.mint` — any new caller that mints an entity calls THIS, never re-derives the discipline.
  `author` is the caller's to resolve; `mint()` never reads git config to find one.
- `remote.mint_via_clone` — the ONE way a process with no steward's checkout mints; never open a
  second clone path or mint an installation token elsewhere.
- `clone.preflight` / `clone.commit_and_push` — the ONE way this subsystem writes to a git working
  copy. Git goes through `librarian.gitcmd` (the one dialect: error shaping, credential
  scrubbing); never wrap `subprocess.run(["git", ...])` again.
- `librarian.gates.scan_worktree_files` / `ensure_scanner` — the secrets gate every mint runs over
  exactly the files the commit carries; do not invoke gitleaks a second way.
- `librarian.githubapp` — the App-credential machinery `remote.py` alone reaches; never
  reimplement JWT signing or token minting.
- `capture.dispositions.reject` / `.requeue` — what `reject` and `approve --requeue` call; every
  caller rides the same state-guarded transition.

## Avoid

- **Never let `stigmergy.librarian` import `stigmergy.entities`.** The edge is one-way and
  load-bearing: the unattended worker must never depend on the steward's CLI. Where both need the
  same fact (which characters an entity name may carry), it is duplicated and declared at both
  ends — `librarian.report`'s identity filter and this package's `cli._suggestable`.
- **Never resolve a collision against the file on disk when a repo could be drifting.** `mint.mint`
  asks the gate about the registry the COMMIT will publish (`generator.registry_of` over freshly
  re-read pages); `mint._refuse_drift` runs first so that answer means something.
- **Never repair, reset, stash or discard anything in the steward's clone.** Every check REFUSES
  and says what to do; the one deletion (`discard_untracked`) is bounded to a file this process
  itself just wrote, by absolute path.
- **Never force-push, on any attempt, for any reason.** A genuine conflict fails the loop rather
  than being resolved — an identity conflict is exactly the decision a human must make.
- **Never build a suggested shell command from an untrusted value without `_suggestable`'s
  allow-list-then-quote discipline** — `shlex.quote` can quote anything and still produce a line
  whose shell parse disagrees with what a human reads.
- **Never interpolate a steward-authored value into a page with an f-string.** `render_page`
  writes every field through `_yaml_str`; an interpolated quote smuggles a second, silent alias
  past the collision gate (`birth.py`'s module docstring).
- **Never add a second implementation of "collides".** `prepare` and `recheck` both call
  `_refuse_collisions`; reuse it.

## Data & contracts

- `birth.Proposal` (frozen) — `canonical_id`, `name`, `entity_type`, `aliases` (tuple), `role`;
  constructed only by `prepare`. `entity_type` is one of `generator.ENTITY_TYPES`
  (`ops/templates/entity.md`'s own vocabulary, re-exported so page and registry can never
  enumerate it differently).
- `generator.PageEntity` (frozen) — one page's identity claim as read off disk.
- `generator.Divergence` / `RegenerateOutcome` — `Divergence` is semantic ("which entity, and
  what about it"), never a text diff; `RegenerateOutcome.changed` is BYTE-level (allows the one
  canonicalization commit when only formatting differs).
- `generator._duplicate_ids` / `_duplicate_match_keys` — two different collapses, both raised by
  `read_entity_pages`: same slug id, and distinct ids whose titles claim one `normalize()` key
  (legal-suffix folding). The knowledge repo's own linter mirrors the match-key rule — a declared
  duplication across two repos with no shared import.
- `capture.schema.SITUATION_NAMES_KEY` — the plural sibling of `SITUATION_NAME_KEY`, written only
  for multi-name parks; `situations.subjects_of` falls back to the singular key as a one-element
  list, keeping every single-name reader unchanged.
- The template, `ops/templates/entity.md`, is read from the repo and never reproduced here — an
  edit to it reaches minted pages with no platform release.

## Layering

Pinned in `tests/test_architecture.py`: this package imports `capture`, `stigmergy.kernel` and a
declared set of `librarian` modules (`gitcmd`/`errors`, `config`, `gates`, `githubapp`); `cli.py`
additionally reaches `stigmergy.index` for the one connection seam. It never imports `server` or
`answer`; `librarian`, `capture` and `views` never import it back. Two inbound edges exist, both
named, symbol-scoped exceptions with their own architecture tests: `stigmergy.server.review` (the
review inbox: `situations`, `generator.canonical_id_for`/`ENTITY_TYPES`, `remote`, the two error
names) and `stigmergy.admin` (the console: `situations`, `remote`, `generator`, `errors`) — both
walk the same governed mint door (`remote.mint_via_clone` -> `mint.mint`), and neither is a
license for the other to widen.

## Proofs that live elsewhere

`tests/entities/` is keyless and DB-less (real git via a per-test bare remote; gitleaks skipped
on a laptop without the binary, required in CI). The cross-package proofs live with their
fixtures: `tests/librarian/test_entity_full_circle_pg.py` (park → approve from a separate clone →
re-file anchored to the newborn entity), `tests/server/test_review.py` (the review-inbox door
mints for real), and `tests/admin/` (the console door).
