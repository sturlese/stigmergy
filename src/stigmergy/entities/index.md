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
| `situations.py` | which parked (`triage`) rows are an identity decision — `classify`, `subject_of` / `subjects_of` / `mint_name_prefill`, the two semantic entry points (`list_pending_situations`, `get_situation`) and the write guard (`require_situation`) |
| `birth.py` | resolve-before-mint as a pure function of a proposal and a registry (`prepare`, `recheck`, `_refuse_collisions`) and the page renderer (`render_page`, `_yaml_str`, `commit_message`) |
| `generator.py` | the registry generator: `read_entity_pages`, `derive_registry`, `registry_of`, `compare` (semantic drift), `check` / `regenerate`, `canonical_id_for` |
| `clone.py` | the working copy's checks and push: `preflight` (branch/identity/clean/in-sync), `commit_and_push` with fetch-regenerate-retry, `write_page` / `discard_untracked`. Never repairs, never force-pushes |
| `mint.py` | the ONE mint orchestration `cli._mint` and `remote.mint_via_clone` both call: drift refusal, the gate against the registry the commit will PUBLISH, the template render, `generator.regenerate`, the gitleaks scan, ONE commit, bounded rebase-and-retry |
| `remote.py` | the server-driven mint door: `mint_via_clone` — a throwaway clone with the librarian App's credential, `mint.mint` with an `Approved-by:` trailer, cleanup in a `finally`. `credential` is needed only for `https://` remotes; raises only this package's own error types. Also the ONE place a refusal changes audience: the post-clone ladder re-words the four types whose sentences name that clone, and passes the door-neutral ones through |
| `errors.py` | `EntityError` (base), `CollisionError` (the governance verdict) and its `CollisionRaceError` subclass (the post-rebase re-ask), `CloneStateError`, `PushRaceError`, `TemplateMissingError`, `CapabilityUnavailableError`. Neither inbound consumer lets one of these out: `stigmergy.server.review` translates at every raise site (the mint and the pre-mint `require_situation` guard alike) into `ReviewError`/its own `CapabilityUnavailableError`, `stigmergy.admin` into `AdminRefused` at its own boundary — a surface barred from importing this package could catch one only as an unanticipated fault, whose text it must not show |

## Use these

- `situations.classify` / `subject_of` / `subjects_of` / `mint_name_prefill` — the ONE reading of
  "is this parked row an entity situation, about what, and what may a form offer for it". Three
  functions over the same row with three different jobs: `subject_of` DISPLAYS (one string; several
  names joined with `", "`), `subjects_of` ACTS (the per-name list — an ordinary or meeting park
  can carry several names, each independently approvable), `mint_name_prefill` DECIDES (the single
  unresolved name, or `""` when several or none mean no default can be right — and `""` too for
  `capture.schema.UNNAMED_ENTITY_PLACEHOLDER`, refused BY VALUE for the reason `cli._suggestable`
  refuses it: it is the librarian's word for a park that named nothing, and a mint door is one
  unchanged click from signing it into the registry, where it then resolves forever).
- `situations.mint_name_prefill` — the one-vs-several prefill rule for the entity-mint Name field,
  decided HERE and nowhere else, and DELIVERED on the row: `_situation_view` carries it beside
  `subject`/`subjects`, so every consumer of `list_pending_situations` / `get_situation` (the CLI's
  `--json`, both admin entity routes) reads one already-decided value instead of computing it over
  whatever shape of the row it happens to hold. Both mint doors — the Slack modal (`slack.render`,
  handed the same value on the review item `server.review` builds) and the admin console's Approve
  form (`admin/static/assets/views.js`) — OBEY it: they render it, and list `subjects` when it is
  `""`. Neither counts the names again, so no door can disagree about WHEN a default is safe.
  The offered STRING can still differ between them: sanitization is per transport — the console
  strips control characters out of what it renders, Slack and MCP do not. What that can no longer
  cause is a garbled entity: `birth._refuse_control_characters` REFUSES C0/C1 in a name, an alias
  and a role at the terminal gate every door passes through, so the worst a ragged prefill produces
  is a refusal naming the code point. The console's stripped default mints; Slack's raw one is
  refused. Both are honest — on each door the steward mints exactly the name they READ — which is
  why the remaining difference is in the outcome and not in what anyone was shown. Submitting
  either door mints one entity as one signed commit, so a
  surface that re-derives the rule is a second policy that can drift into minting a name nobody
  chose.
  `cli._print_next_commands` looks similar and is not this: its one-vs-several test decides whether
  to LABEL each printed command with its name, over a list whose fallback is taken only when
  `subjects_of` is empty — which is exactly when no join ran, so what it reaches is the row's raw
  singular `SITUATION_NAME_KEY`, verbatim, never the joined display string. Nothing there prefills
  a form.
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
  second clone path or mint an installation token elsewhere. It is also where a refusal is re-worded
  for a steward who holds no clone (ADR 030's "two-door refusal wording" amendment): the module's
  own constants carry the argument, and the mapping keys on the exception TYPE, never on its text.
  A raise site anywhere in this package whose sentence names a path or a `git -C` command therefore
  needs a class no door-neutral raise site shares — which is why `CollisionRaceError` exists beside
  `CollisionError`, and `TemplateMissingError` beside a plain `EntityError`.
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
- **Never read a parked row's name key yourself.** `situations.subjects_of` / `subject_of` are the
  DECLARED ONE READER of `capture.schema.SITUATION_NAMES_KEY` and its legacy singular, and that is
  enforced rather than agreed: `tests/test_architecture.py` requires every module naming either
  constant AS CODE to be the definition (`capture/schema.py`), the one writer
  (`librarian/report.py`) or this module — anything else is a listed exception carrying its reason,
  with the pruning test every allowlist in that file has (naming a key in a comment stays free, as
  `cli.py` does). The keys are a wire format in a JSONB column with no schema behind them, so
  nothing type-checks a second opinion about what a park named; two lanes each deciding that for
  themselves is issue #32, where a capture naming two entities lost one all the way to a human.
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
- **Never give `remote.mint_via_clone` a bare `except EntityError` arm.** Its ladder maps four
  named types and everything else passes through ON PURPOSE — birth-field validation, the collision
  verdict, the secrets refusal and the drift refusal are the refusals a steward can actually act on,
  and all four are `EntityError`s. A catch-all arm would replace every one of them with "a
  server-side fault, approve again".

## Data & contracts

- `birth.Proposal` (frozen) — `canonical_id`, `name`, `entity_type`, `aliases` (tuple), `role`;
  constructed only by `prepare`. `entity_type` is one of `generator.ENTITY_TYPES`
  (`ops/templates/entity.md`'s own vocabulary, re-exported so page and registry can never
  enumerate it differently).
- `generator.PageEntity` (frozen) — one page's identity claim as read off disk.
- `generator.Divergence` / `RegenerateOutcome` — `Divergence` is semantic ("which entity, and
  what about it"), never a text diff. `changed` is semantic from `check` (any divergence) and
  BYTE-level from `regenerate` — the byte answer is what allows the one canonicalization commit
  when only formatting differs.
- `generator._duplicate_ids` / `_duplicate_match_keys` — two different collapses, both raised by
  `read_entity_pages`: same slug id, and distinct ids whose titles claim one `normalize()` key
  (legal-suffix folding). The knowledge repo's own linter mirrors the match-key rule — a declared
  duplication across two repos with no shared import.
- `capture.schema.SITUATION_NAMES_KEY` — the ONLY name key a park writes, a list whatever the count.
  `situations.subjects_of` still falls back to the singular `SITUATION_NAME_KEY` as a one-element
  list, and that fallback is PERMANENT: rows parked before the plural collapse keep the old key and
  are never migrated, so a reader that dropped it would blank a live steward's queue.
- The template, `ops/templates/entity.md`, is read from the repo and never reproduced here — an
  edit to it reaches minted pages with no platform release.

## Layering

Pinned in `tests/test_architecture.py`: this package imports `capture`, `stigmergy.kernel` and a
declared set of `librarian` modules (`gitcmd`/`errors`, `config`, `gates`, `githubapp`); `cli.py`
additionally reaches `stigmergy.index` for the one connection seam. The `librarian.gates` edge is
the secrets scanner and only that: `approve`/`create` commit `--no-verify`, so this scan is the
one that runs — the governed door into `main` (where a secret can never be deleted from) must not
be the unscanned one. It never imports `server` or `answer`; `librarian`, `capture` and `views`
never import it back. Two inbound edges exist, both named, symbol-scoped exceptions with their own
architecture tests: `stigmergy.server.review` (the review inbox: `situations`,
`generator.canonical_id_for`/`ENTITY_TYPES`, `remote`, the two error names) and `stigmergy.admin`
(the console: `situations`, `generator`, `errors`) — both walk the same governed mint door
(`remote.mint_via_clone` -> `mint.mint`), the console reaching it through
`server.review.mint_and_record_approval` rather than through `remote` itself, and neither edge is
a license for the other to widen.

## Proofs that live elsewhere

`tests/entities/` is keyless and DB-less (real git via a per-test bare remote; gitleaks skipped
on a laptop without the binary, required in CI). The cross-package proofs live with their
fixtures: `tests/librarian/test_entity_full_circle_pg.py` (park → approve from a separate clone →
re-file anchored to the newborn entity), `tests/server/test_review.py` (the review-inbox door
mints for real), and `tests/admin/` (the console door).
