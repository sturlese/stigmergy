# entities — the identity lifecycle: decide what was proposed, create what nobody did

The librarian never CONFIRMS an entity: it proposes one while filing (`librarian.identity` writes
the page with `approved_by: ""`), and a steward decides it here. This package is the human half —
the only place an identity is approved, merged, declined or born already confirmed, and the ONE
path-scoped human-driven writer of `ops/`. Nothing here runs in a worker, on a poll, or behind an
agent.

**The proposal is the PAGE, not a database row.** `approved_by: ""` in an entity page's
frontmatter is the whole of it; `ops/entity-registry.json` is a derived view, and the review inbox
a derived view of that. There is no proposals table and no DB↔file sync surface — which is why
`pending` can read a clone with no database at all.

Four properties:

- **resolve before writing** — a colliding id/name/alias is refused at the gate, naming the entry;
- **the registry is a pure function of `wiki/entities/*.md`**, regenerated in the same commit as
  any page that changes one (`regenerate --check` makes that falsifiable);
- **"who decided this identity" is always answerable with a person** — a steward's own git identity
  on the commit, or, for a server-driven decision, the App's author line plus a `Decided-by:`
  trailer (an `Approved-by:` one for a `create`);
- **a decision is one commit, or nothing** — the checkout is restored on any failure, and the
  ledger row is written only after the push.

## Modules

| Module | What it is |
|---|---|
| `cli.py` | `stigmergy-entities` — `pending · approve · decline · merge · create · regenerate [--check]`; a thin adapter over `decide.apply` (and `mint.mint` for `create`), deriving `author` from the steward's clone via `clone.preflight`. The only module here that opens a database connection, and it does so because every decision writes the `review_decisions` row through `capture.decisions` — below the `entities` -> `server` edge — naming this door with `decisions.SOURCE_CLI`. `pending` needs no database: it reads the clone's own entity pages |
| `decide.py` | the five decisions as pure worktree operations — `approve_entity`, `merge_entity`, `decline_entity`, `approve_alias`, `decline_alias` — each returning an `Outcome` (kind, entity id, `into`/`alias`, `changed_paths`, `reanchored`, a commit `subject` and a human `summary`). No database, no auth, no commit. `apply()` is the commit discipline both doors share, and `commit_message` the one dialect. `_reanchor` is the part a caller must not reinvent: a decline or a merge takes a page away, so every `wiki/**` page whose `entity:` names it is rewritten in the SAME commit — to the survivor, or to nothing |
| `birth.py` | resolve-before-write as a pure function of a proposal and a registry (`prepare`, `recheck`, `_refuse_collisions`), the terminal name gate every door passes (`_clean_name`, `clean_aliases`, `_refuse_control_characters`), the body builder (`prepare_body`) and the page renderer (`render_page`, `_yaml_str`, `commit_message`). `render_page` takes `approved_by` and `related` as arguments — the librarian passes `""` and the page it proposed from, `create` passes the steward |
| `generator.py` | the registry generator: `read_entity_pages` (each page's `PageEntity`, carrying `proposed` / `approved_by` / `proposed_aliases`), `derive_registry`, `registry_of`, `compare` (semantic drift, lifecycle included), `check` / `regenerate`, `canonical_id_for`, and the layout constants every other package spells through it (`ENTITIES_RELDIR`, `REGISTRY_RELPATH`, `TEMPLATE_RELPATH`, `APPROVED_BY_KEY`, `PROPOSED_ALIASES_KEY`, `ENTITY_TYPES`, `FIX_COMMAND`) |
| `clone.py` | the working copy's checks and push: `preflight` (branch/identity/clean/in-sync), `commit_and_push` with fetch-regenerate-retry, `write_page` / `discard_untracked` / `restore_tracked`. Never repairs, never force-pushes |
| `mint.py` | the ONE birth orchestration `cli._cmd_create` and `remote.mint_via_clone` both call: drift refusal, the gate against the registry the commit will PUBLISH, the template render, `generator.regenerate`, the gitleaks scan, ONE commit, bounded rebase-and-retry. `refuse_drift` and `refuse_secrets` are reused by `decide.apply`, so a birth and a decision are held to the same two checks |
| `remote.py` | the server-driven door: `decide_via_clone` (a throwaway clone with the librarian App's credential, `decide.apply` with a `Decided-by:` trailer) and `mint_via_clone` (its `mint.mint` sibling with an `Approved-by:` trailer), cleanup in a `finally`. `credential` is needed only for `https://` remotes; raises only this package's own error types. Also the ONE place a refusal changes audience: the post-clone ladder re-words the four types whose sentences name that clone, and passes the door-neutral ones through |
| `errors.py` | `EntityError` (base), `CollisionError` (the governance verdict) and its `CollisionRaceError` subclass (the post-rebase re-ask), `CloneStateError`, `PushRaceError`, `TemplateMissingError`, `CapabilityUnavailableError`. Neither inbound consumer lets one of these out: `stigmergy.server.review` translates at the raise site into `ReviewError`/its own `CapabilityUnavailableError`, `stigmergy.admin` into `AdminRefused` at its own boundary — a surface barred from importing this package could catch one only as an unanticipated fault, whose text it must not show |

## Use these

- `decide.approve_entity` / `merge_entity` / `decline_entity` / `approve_alias` / `decline_alias` —
  the five decisions, and the ONE definition of what each one DOES to a checkout. They are pure
  worktree operations: no database, no authorization, no commit. Every door calls these, so
  "declining removes the page AND re-anchors what pointed at it" is one implementation rather than
  four. Each refuses with an `EntityError` carrying a one-line human message — an unknown id, an id
  that is not a proposal, an `into` that is unknown/proposed/the same entity, a spelling that is not
  proposed, a merge whose spellings would collide with a THIRD entity.
- `decide.apply(repo, *, action, branch, author, trailer=…)` — the commit discipline both doors
  share, in the order a birth uses: branch/clean/in-sync preflight, `mint.refuse_drift`, the
  decision, `mint.refuse_secrets` over exactly the files the commit will carry, then one commit
  rebased and re-derived on a race. `action` is `lambda repo: decide.approve_entity(repo, …)` or any
  sibling. On ANY exception the tracked files are restored to the head it started from.
- `decide.Outcome` — what a commit message, a ledger row and a human's confirmation line are all
  built from. `reanchored` is the one field a surface must not recompute: it is the list of pages
  the decision rewrote, and a caller that counted them itself could disagree with the commit.
- `birth.prepare` / `birth.recheck` — resolve-before-write, pure (a `Registry` in, a `Proposal` or
  a raised `CollisionError` out). `recheck` is the SAME `_refuse_collisions` asked again after a
  rebase, never a second implementation of "collides".
- `generator.read_entity_pages` / `derive_registry` / `check` / `regenerate` — the only code that
  turns `wiki/entities/*.md` into a `Registry` or into `ops/entity-registry.json`; serialized
  through `kernel.registry.save_registry` / `load_registry`, never reimplemented.
- `generator.canonical_id_for(name)` — what a page titled `name` always regenerates as
  (`slugify`, not `normalize`; its docstring says why the two answer different questions).
- `generator.APPROVED_BY_KEY` / `PROPOSED_ALIASES_KEY` / `TEMPLATE_RELPATH` / `REGISTRY_RELPATH` /
  `ENTITIES_RELDIR` — the knowledge repo's identity layout, spelled ONCE. `librarian.identity`
  imports these rather than retyping them, which is the whole reason that import edge exists.
- `mint.mint` — any new caller that creates a CONFIRMED entity calls THIS, never re-derives the
  discipline. `author` is the caller's to resolve; `mint()` never reads git config to find one.
- `remote.decide_via_clone` / `remote.mint_via_clone` — the ONE way a process with no steward's
  checkout writes; never open a second clone path or mint an installation token elsewhere. Both go
  through `_in_fresh_clone`, which is also where a refusal is re-worded
  for a steward who holds no clone (ADR 030's "two-door refusal wording" amendment): the module's
  own constants carry the argument, and the mapping keys on the exception TYPE, never on its text.
  A raise site anywhere in this package whose sentence names a path or a `git -C` command therefore
  needs a class no door-neutral raise site shares — which is why `CollisionRaceError` exists beside
  `CollisionError`, and `TemplateMissingError` beside a plain `EntityError`.
- `clone.preflight` / `clone.commit_and_push` / `clone.restore_tracked` — the ONE way this
  subsystem writes to a git working copy. Git goes through `librarian.gitcmd` (the one dialect:
  error shaping, credential scrubbing); never wrap `subprocess.run(["git", ...])` again.
- `librarian.gates.scan_worktree_files` / `ensure_scanner` — the secrets gate every write runs over
  exactly the files the commit carries; do not invoke gitleaks a second way.
- `librarian.githubapp` — the App-credential machinery `remote.py` alone reaches; never
  reimplement JWT signing or token minting.
- `capture.decisions.record_decision` — the ledger row every decision writes, AFTER the push, with
  this door's own `SOURCE_CLI`. The librarian reads that ledger to refuse re-proposing a declined
  identity, which is why a decline that never reached it is worse than no decline at all.

## Avoid

- **Never let `stigmergy.librarian` import this package beyond `identity.py`.** That ONE module may
  reach `birth`, `generator` and `errors` — a proposed page must be the page `create` renders, and
  the registry must be regenerated by the same function. Everything else here is out of the
  worker's reach: the unattended worker proposes, it never decides.
- **Never decide an identity without recording it.** The page change and the ledger row are two
  halves of one decision, in that order (`decide.apply` then `record_decision`, after the push). A
  decline the ledger never learned about is one the librarian re-proposes on the next capture that
  mentions the name — which is why `decline` refuses to run without a database.
- **Never delete or re-anchor a page outside `decide.py`.** A decline and a merge are the only two
  operations in this system that remove a page, and each must rewrite every `entity:` pointing at
  it in the same commit: a page whose anchor names an id the registry no longer has is a page the
  gates would refuse to file today.
- **Never resolve a collision against the file on disk when a repo could be drifting.** `mint.mint`
  asks the gate about the registry the COMMIT will publish (`generator.registry_of` over freshly
  re-read pages); `mint.refuse_drift` runs first so that answer means something, and `decide.apply`
  runs it for the same reason — regenerating somebody else's drift inside a commit that says
  "confirm X" is `ensure_clean`'s argument applied to the derived file.
- **Never repair, reset, stash or discard anything in the steward's clone.** Every check REFUSES
  and says what to do; the one deletion (`discard_untracked`) is bounded to a file this process
  itself just wrote, by absolute path.
- **Never force-push, on any attempt, for any reason.** A genuine conflict fails the loop rather
  than being resolved — an identity conflict is exactly the decision a human must make.
- **Never interpolate a steward-authored value into a page with an f-string.** `render_page`
  writes every field through `_yaml_str`; an interpolated quote smuggles a second, silent alias
  past the collision gate (`birth.py`'s module docstring). Frontmatter EDITS go through
  `librarian.page`'s primitives (`with_list_field` / `with_scalar_field` / `rebuild`) for the same
  reason — `decide.py` never rewrites a line itself.
- **Never add a second implementation of "collides".** `prepare` and `recheck` both call
  `_refuse_collisions`; reuse it. `merge_entity` asks the same question a third way and still
  through the registry (`collision_id` over the survivor's world WITHOUT the proposal), because a
  spelling that would resolve to a THIRD entity is a collision the steward did not decide.
- **Never give `remote._in_fresh_clone` a bare `except EntityError` arm.** Its ladder maps four
  named types and everything else passes through ON PURPOSE — birth-field validation, the collision
  verdict, the secrets refusal and the drift refusal are the refusals a steward can actually act on,
  and all four are `EntityError`s. A catch-all arm would replace every one of them with "a
  server-side fault, try again".

## Data & contracts

- `birth.Proposal` (frozen) — `canonical_id`, `name`, `entity_type`, `aliases` (tuple), `role`;
  constructed only by `prepare`. `entity_type` is one of `generator.ENTITY_TYPES`
  (`ops/templates/entity.md`'s own vocabulary, re-exported so page and registry can never
  enumerate it differently).
- `decide.Outcome` (frozen) — `kind` (one of `decide.KINDS`), `entity_id`, `into`, `alias`,
  `changed_paths` (every file written or deleted, the registry included), `reanchored` (the pages
  whose `entity:` was rewritten), `subject` (one-line commit subject), `summary` (one sentence for a
  human) and `details` (per-kind extra, e.g. a merge's resulting alias list).
- `generator.PageEntity` (frozen) — one page's identity claim as read off disk, LIFECYCLE included:
  `proposed` (`approved_by` present and empty), `approved_by` and `proposed_aliases`. The three
  travel into the registry, which is how the review inbox exists without a table.
- `generator.Divergence` / `RegenerateOutcome` — `Divergence` is semantic ("which entity, and
  what about it"), never a text diff. `changed` is semantic from `check` (any divergence) and
  BYTE-level from `regenerate` — the byte answer is what allows the one canonicalization commit
  when only formatting differs.
- `generator._duplicate_ids` / `_duplicate_match_keys` — two different collapses, both raised by
  `read_entity_pages`: same slug id, and distinct ids whose titles claim one `normalize()` key
  (legal-suffix folding). The knowledge repo's own linter mirrors the match-key rule — a declared
  duplication across two repos with no shared import.
- **The legal-suffix fold lives on THIS side of the system only.** `birth._refuse_collisions` asks
  `Registry.collision_id` and `_duplicate_match_keys` asks `normalize` directly: both answer "would
  these two ever be confused?", where a false negative mints a duplicate identity and the refusal
  falls closed onto a steward. The FILING side stopped folding it (issue #77) — which entity a
  capture means is the agent's judgment now — so `canonical_id` and `collision_id` legitimately
  disagree about the same string, and a caller here that reached for `canonical_id` would be
  weakening the gate.
- `generator._index` calls `kernel.registry.index_entity` rather than keying the maps itself; its
  promise to index "exactly as `load_registry` does" is now kept by calling the same function.
- **The lifecycle is on the PAGE.** `approved_by: ""` is proposed, a name is who confirmed it, an
  ABSENT key is a page from before the field existed and reads as confirmed;
  `proposed_aliases: [...]` are spellings waiting on the same steward. `_require_proposed` and
  `_require_proposed_alias` are the ONE reading of "is there anything here to decide", and they
  refuse a CONFIRMED entity by name: an approved identity retires through `superseded_by` on its
  page, never through this door.
- `review_kinds.KIND_IDENTITY_PROPOSAL` / `KIND_ALIAS_PROPOSAL` and `alias_item_id` — the item kinds
  and ids a ledger row is written under, spelled in the dependency-free root module so this package
  and `stigmergy.server` agree without importing each other. An identity's `item_id` is the entity's
  registry id; an alias's is `<entity id>:<alias>`.
- The template, `ops/templates/entity.md`, is read from the repo and never reproduced here — an
  edit to it reaches every new entity page with no platform release, whether the librarian proposed
  it or a steward created it.

## Layering

Pinned in `tests/test_architecture.py`: this package imports `capture`, `stigmergy.kernel` and a
declared set of `librarian` modules (`gitcmd`/`errors`, `config`, `gates`, `githubapp`, and
`page` for the frontmatter primitives `decide.py` edits through); `cli.py`
additionally reaches `stigmergy.index` for the one connection seam and `stigmergy.review_kinds` for
the kinds its ledger rows name — the root module exists so two packages that may not import each
other still spell those strings once. The `librarian.gates` edge is
the secrets scanner and only that: every commit here is `--no-verify`, so this scan is the
one that runs — the governed door into `main` (where a secret can never be deleted from) must not
be the unscanned one. It never imports `server` or `answer`; `capture` and `views` never import it
back.

**Four inbound edges**, each a named, symbol-scoped exception with its own architecture test:

- `stigmergy.librarian.identity` — `birth`, `generator`, `errors`. The PROPOSER, and the only
  librarian module allowed in; it never reaches `decide`, `clone`, `mint`, `remote` or `cli`.
- `stigmergy.server.review` — `decide` (the five decisions, handed to the door as `action`),
  `remote` (the door itself, reached as a module ATTRIBUTE so a test can monkeypatch it) and the
  two error names it maps into its own vocabulary.
- `stigmergy.admin` — `decide`, `generator` (`ENTITY_TYPES` and `canonical_id_for`) and `errors`.
  `remote` is deliberately ABSENT: the console reaches the governed door through
  `server.review.decide_and_record` / `create_and_record`, the ONE ordering every server-side door
  runs.
- `stigmergy.repair.entity_alias` — `generator` and `errors`, to REGENERATE
  `ops/entity-registry.json` inside a governed merge commit, because the registry has exactly one
  writer and hand-building that file would be a second.

No edge is a license for another to widen.

## Proofs that live elsewhere

`tests/entities/` is keyless and DB-less (real git via a per-test bare remote; gitleaks skipped
on a laptop without the binary, required in CI). The cross-package proofs live with their
fixtures: `tests/librarian/test_identity_unit.py` (what the librarian's proposal actually writes,
against the same page shape this package renders), `tests/server/test_review.py` (the review-inbox
door decides for real) and `tests/admin/` (the console door).
