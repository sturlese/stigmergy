# entities — governed entity birth, and the registry's derived view

Narrative doc: the runbook's
[draining parked rows](../../../docs/reference/operator-runbook.md#draining-parked-rows) section,
which is where governed entity birth is operated from, and the ask-back/triage half of
[`docs/reference/capture.md`](../../../docs/reference/capture.md); the
meeting flow's own multi-name ask-back is covered in
[`docs/reference/meeting-distiller.md`](../../../docs/reference/meeting-distiller.md) instead.
Design record: [ADR 016](../../../docs/decisions/016-human-loop-and-entity-governance.md),
[ADR 020](../../../docs/decisions/020-meeting-distiller.md) (the multi-name park this package's
`subjects_of` exists for) and [ADR 026](../../../docs/decisions/026-the-purge.md) (D3 is why
`ops/` has one path-scoped writer, and D4 is why this package reads
`stigmergy.kernel` rather than the removed `stigmergy.pipeline`). This file
is the code map — for whoever is about to edit this package, not run it.

## Purpose

The governance rule, made real: *the librarian never silently mints an entity; it proposes, a
steward approves, the registry regenerates.* This package is the human half of that sentence and
**the only writer of `wiki/entities/` anywhere in this codebase** — the fast lane's confinement is
untouched by it, and nothing here runs in a worker, on a poll, or behind an agent. `ops/` has
exactly ONE path-scoped writer, this package (`ops/entity-registry.json` plus `wiki/entities/`);
the librarian's own write confinement refuses all of `ops/`. A fleet supervisor briefly held a
second, human-driven `ops/` write (`ops/playbook.md`) and went whole at
[ADR 026](../../../docs/decisions/026-the-purge.md) D3 — worth knowing only because
`clone.py`'s wording parameterization still carries its shape (see Notes). This package holds three
properties, none of which is about convenience:

1. **Resolve before mint** — an id, name or alias that collides with a registered entity is refused at
   the gate, naming the entry it collided with (the `northwind-slides.md`-next-to-`Northwind
   Capital.md` class of defect).
2. **The registry is a derived view, and derived means derived** — `ops/entity-registry.json` is a
   pure function of `wiki/entities/*.md`, and `regenerate --check` makes that claim falsifiable.
3. **Who approved this identity is always answerable with a person** — `stigmergy-entities approve`/
   `create` commit the page and the regenerated registry together with the STEWARD's own git
   identity, so `git blame` names a person directly. A server-driven mint
   ([ADR 030](../../../docs/decisions/030-server-side-entity-minting.md), the ONE other door,
   `entities.remote.mint_via_clone`) commits as the librarian App instead — but carries the
   resolved human in an `Approved-by:` trailer, so the answer is still a name, and the author line
   (App vs. human) additionally answers "through which door".

## Key entry points

| Module | Owns |
|---|---|
| `cli.py` | `stigmergy-entities` — `list · show · approve · reject · create · regenerate [--check]`; the operator's front door, now a THIN adapter over `mint.mint` (it derives `author` from the steward's own clone via `clone.preflight` and hands it in) |
| `situations.py` | which parked (`triage`) rows are an identity decision at all — `classify`, `subject_of` / its plural sibling `subjects_of`, the two semantic entry points (`list_pending_situations`, `get_situation`) and the write guard (`require_situation`) |
| `birth.py` | resolve-before-mint as a pure function of a proposal and a registry (`prepare`, `recheck`, `_refuse_collisions`) and the page renderer (`render_page`, `commit_message` — the `trailer` parameter an App-authored mint appends, ADR 030) |
| `generator.py` | the registry generator: `read_entity_pages`, `derive_registry`, `compare` (semantic drift), `check` / `regenerate` |
| `clone.py` | the working copy's own checks and push: preflight (branch/identity/clean/in-sync), `commit_and_push` with fetch-regenerate-retry, never a force-push. `write_page`/`discard_untracked` and the granular `ensure_*` checks are what `mint.py` calls directly (it takes `author` as a parameter rather than deriving one via `preflight`, which stays `cli.py`'s own call) |
| `mint.py` | **the shared mint orchestration** (ADR 030 D4) — `mint(repo, *, ..., author, submission_id=None, trailer="", ...)`: drift refusal, `birth.prepare` against the registry the commit will PUBLISH, the template render, `generator.regenerate`, the gitleaks scan, ONE commit via `clone.commit_and_push`, bounded rebase-and-retry, never a force-push. The ONE function `entities.cli._mint` and `entities.remote.mint_via_clone` both call, so a divergence between the CLI's door and a server-driven one is a defect this module's existence prevents |
| `remote.py` | **a server-driven mint** (ADR 030 D3) — `mint_via_clone(repo_url, branch, credential, *, ..., approved_by, ...)`: clones into a `tempfile.TemporaryDirectory` with the librarian App's own credential (`librarian.githubapp`), configures the App's git identity, calls `mint.mint` with an `Approved-by:` trailer, pushes, cleans up in a `finally`. `credential` is required only when `repo_url` is `https://` — a local path (every test, and the composition's bare remote) needs none. Refuses by naming the missing capability (`errors.CapabilityUnavailableError`) rather than degrading, and never lets a `librarian.errors` exception escape its own boundary unmapped |
| `errors.py` | `EntityError` (base), `CollisionError` (a governance verdict), `CloneStateError`, `PushRaceError`, `CapabilityUnavailableError` (ADR 030 — named after, and mapped by `stigmergy.server.review` to, `server.errors.CapabilityUnavailableError`'s own posture; entities may not import `stigmergy.server` to raise that type directly) |

`cli.py` is still where to start tracing one `approve` end to end (module docstring walks the
order of operations, which is a correctness property here, not a style choice) — it now hands off
to `mint.py` for the discipline itself, shared with `remote.py`'s server-driven door.

## Use these

- `situations.classify(row)` / `subject_of(row)` / `subjects_of(row)` — the ONE reading of "is this
  parked row an entity situation, and about what". `stigmergy-queue list` and `stigmergy-entities list`
  answer different questions over the same table; do not re-derive this predicate at a second call
  site. **`subjects_of` is the per-name list, `subject_of` the single display string it
  collapses to.** A meeting park can name several unresolved entities at once — one call naming two
  customers and an unregistered project code — each independently approvable; `subjects_of` returns
  every name so a caller that must act on each one on its own (`entities.cli._print_next_commands`,
  which prints or refuses one `stigmergy-entities approve` command PER name, so a sibling name that
  happens to be unsafe to paste into a shell never blocks the others) can. `subject_of` stays the
  single string every EXISTING reader (the `list` column, `show`'s headline) consumes — unchanged
  for a single-name park, and a comma-joined `", "` string of all names for a multi-name one, so a
  caller that only ever renders one string still gets something true rather than going blank.
- `situations.require_situation` — the write guard `approve`/`reject` call BEFORE anything is
  validated or written. Being able to `stigmergy-queue show` any row is not permission to mint an entity
  from it; this is what enforces that distinction.
- `birth.prepare` / `birth.recheck` — resolve-before-mint, pure (a `Registry` value in, a `Proposal`
  or a raised `CollisionError` out — no git, no network). `recheck` is the SAME function
  (`_refuse_collisions`) asked again after a rebase, never a second implementation of "collides".
- `generator.read_entity_pages` / `derive_registry` / `check` / `regenerate` — the ONLY code that
  turns `wiki/entities/*.md` into a `Registry` or into `ops/entity-registry.json`. Serialized
  through `stigmergy.kernel.registry.save_registry` / read through `load_registry` — imported and
  never reimplemented.
- `generator.canonical_id_for(name)` — the one function that says what a page titled `name` will
  always regenerate as (`slugify`, not `normalize` — see its docstring for why the two answer
  different questions and both matter here).
- `mint.mint(repo, *, ..., author, submission_id=None, trailer="", ...)` — the ONE mint
  orchestration (ADR 030 D4). A new caller that mints an entity calls THIS, never re-derives the
  discipline: drift refusal, resolve-before-mint against the PUBLISHED registry, the template
  render, the secrets scan, one commit, bounded rebase-and-retry. `author` is the caller's to
  resolve (a steward's own git identity for `cli.py`, the librarian App's for `remote.py`) —
  `mint()` itself never reads git config to find one.
- `remote.mint_via_clone(repo_url, branch, credential, *, ..., approved_by, ...)` — the ONE way a
  process with no steward's checkout mints (ADR 030 D3): a throwaway clone, the App's credential,
  `mint()`, a push, cleanup. A new server-side surface that mints calls this, never opens its own
  clone or mints its own installation token.
- `clone.preflight` / `clone.commit_and_push` — the ONE way this subsystem writes to a git working
  copy (a steward's own for `cli.py`, a throwaway one for `remote.py`). Never wrap
  `subprocess.run(["git", ...])` again here: `librarian.gitcmd` already owns this repo's one git
  dialect (invocation, error shaping, credential scrubbing), and a second wrapper is a second place
  to forget that a push URL can carry a token. `preflight` itself is CLI-only now (it derives an
  identity from git config, which a throwaway clone has none to give); `mint()` calls its three
  non-identity checks (`ensure_on_branch`/`ensure_clean`/`ensure_in_sync`) directly.
- `librarian.gates.scan_worktree_files` / `ensure_scanner` — the secrets gate every mint runs
  (`mint.py`, moved out of `cli.py`-only territory by ADR 030) over exactly the files a commit is
  about to carry, before it is made. Same gitleaks binary, same `--redact`, same `Finding` shape the
  fast lane uses; do not invoke gitleaks a second way here.
- `librarian.githubapp` — the App-credential machinery (`configured`, `installation_token`,
  `identity`) `remote.py` alone reaches into, for a server-driven mint's clone/push identity. Never
  reimplement JWT signing or installation-token minting a second time; `bootstrap.py`/
  `gitcredential.py` are the librarian worker's own two callers of the same machinery, and this is
  the third shape (a tokenized clone URL, `remote._authenticated_url`'s own docstring says why).
- `capture.dispositions.reject` / `.requeue` — what `stigmergy-entities reject` and `approve --requeue`
  call, and what a server-driven approve's `requeue=True` calls too (`server.review`). Nobody's
  judgment about an identity is a different KIND of rejection from a steward's judgment about
  anything else; every caller rides the same state-guarded transition.

## Avoid / anti-patterns

- **Never let `stigmergy.librarian` import `stigmergy.entities`.** The edge is one-way and load-bearing:
  the unattended worker must never depend on the steward's CLI. Where the two packages need the same
  fact and the import would have to run the wrong way (which characters an entity NAME may carry), it
  is stated at both ends with the duplication declared, not resolved by an import in the wrong
  direction — see `librarian.report`'s `_UNSAFE_IN_IDENTITY`-equivalent filter and this package's
  `cli._suggestable`.
- **Never resolve a collision against the file on disk when a repo could be drifting.** `mint.mint`
  asks the collision gate about the registry the COMMIT will publish (`generator.registry_of` over
  freshly re-read pages), never the possibly-stale `ops/entity-registry.json`. `mint._refuse_drift`
  runs first and exists precisely so the gate's answer means something — true for `cli.py`'s own
  clone and for `remote.py`'s throwaway one alike, since both mint through the same function.
- **Never repair, reset, stash or discard anything in the steward's clone.** This is a human's own
  working copy (`clone.py`'s module docstring). Every check REFUSES and says what to do; the one
  deletion (`discard_untracked`) is bounded to a file this process itself wrote moments ago, by
  absolute path, never a glob and never `git clean`.
- **Never force-push, on any attempt, for any reason.** `commit_and_push`'s retry loop fetches,
  rebases, regenerates and amends — a genuine conflict fails the loop (`PushRaceError` /
  `_rebase_onto_remote`'s abort) rather than being resolved, because an identity conflict is exactly
  the decision this subsystem exists to put in front of a human.
- **Never build a suggested shell command from an untrusted value without checking it is safe to
  paste, not merely safe to quote.** `cli._suggestable` exists because `shlex.quote` can quote
  anything and still produce a line whose shell parse disagrees with what a human reading it sees
  (`Acme" --aliases "<a registered name>`). A new printed command built from captured material must go
  through the same allow-list-then-quote discipline, not through `shlex.quote` alone.
- **Never interpolate a steward-authored value into a page with an f-string.** `birth.render_page`
  writes every field through `_yaml_str`, which owns the escaping contract; an f-string produced the
  exact chain (`birth.py`'s module docstring) that let a quote in `--aliases` smuggle a second, silent
  alias past the collision gate and into the registry.
- **Never add a second implementation of "collides".** `birth.prepare` and `birth.recheck` both call
  `_refuse_collisions`; a caller that needs the same question answered again (a rebase, a retry)
  reuses it rather than re-deriving the id/name/alias comparison.

## Data & contracts

- **`birth.Proposal`** (frozen) — `canonical_id`, `name`, `entity_type`, `aliases` (tuple), `role`.
  Constructed only by `prepare`; `relpath` derives the page path from `name`. `entity_type` is one of
  `generator.ENTITY_TYPES` (`person`, `organization`, `product`, `tool`, `repository`, `place` —
  `ops/templates/entity.md`'s own vocabulary, re-exported so the page and the registry can never
  enumerate it differently).
- **`generator.PageEntity`** (frozen) — one entity page's identity claim as read off disk:
  `canonical_id`, `name`, `entity_type`, `aliases`, `relpath`.
- **`generator.Divergence`** / **`RegenerateOutcome`** — `check`/`regenerate`'s result. `Divergence`
  is semantic ("which entity, and what about it"), never a text diff. `RegenerateOutcome.changed`
  is BYTE-level — it is what allows one canonicalization commit when only the bytes differ — while
  `.divergences` is semantic. The two answers differ in exactly one case: a semantically-right but
  differently-formatted hand-written registry.
- **`generator._duplicate_ids`** / **`_duplicate_match_keys`** — two DIFFERENT collapses, both
  raised by `read_entity_pages`, neither folded into the other. `_duplicate_ids` catches two
  pages whose titles slugify to the same registry id; `_duplicate_match_keys` catches the
  narrower case where the ids stay distinct but `normalize()` (which additionally strips legal
  suffixes — `Acme` and `Acme Corp.` are one matching key) would make `by_alias` resolve a name
  to whichever page sorted last, invisible to the id-only check and to the page↔registry linter
  rule. The knowledge repo's own `.claude/tools/stigmergy_lint.py` mirrors this exact rule
  (`seen_match_keys`) so a linter pass over `wiki/entities/*.md` catches the same
  collision the generator would refuse at `regenerate` time — a declared duplication (two repos,
  no shared import), not a redundant check.
- **`capture.schema.SITUATION_NAMES_KEY`** (`"entity_names"`) — the plural sibling of
  `SITUATION_NAME_KEY` (`"entity_name"`), a JSON-encodable list written ONLY when a parked capture
  has more than one unresolved name (`librarian.report.triage_entity_multi`). A SINGLE-name park
  carries no such key, and neither does any row predating the plural form;
  `situations.subjects_of` falls back to the singular key as a one-element list, which is what keeps
  every existing reader and every non-meeting flow working unchanged. Two keys rather than widening
  one key to "always a list", specifically to avoid a migration — see the constant's own docstring
  in `capture/schema.py` for the full argument.
- **`errors.CollisionError`** — the one refusal here that is a GOVERNANCE verdict rather than an
  operational fault (a dirty clone clears itself; a collision means the identity already exists).
  It names which of `--name`/`--id`/an alias collided, and with which registered entry — a refusal
  that does not name what it collided with leaves the human nothing to act on.
- **`errors.PushRaceError`** — carries what the bounded retry loop left behind: the commit is in the
  local clone, nothing was force-pushed. A message that says "could not push" without saying what
  state that leaves is exactly the failure this shape exists to prevent.
- **`errors.CapabilityUnavailableError`** (ADR 030) — a server-driven mint's own refusal when the
  librarian App credential or the knowledge-repo URL is simply absent, as opposed to every OTHER
  `EntityError` here, which is a governance verdict or an operational fault. `stigmergy.server.review`
  maps it to `server.errors.CapabilityUnavailableError` of the identical posture (echoed verbatim
  over MCP, never collapsed to a class name) — the one place both names are in scope, since this
  package may not import `stigmergy.server` to raise that type directly.
- **The template, `ops/templates/entity.md`** — read from the repo, never reproduced in this package.
  An edit to it (a new section, a reworded prompt) reaches minted pages with no platform release.

## Tests

`tests/entities/` — keyless and **DB-less throughout**: no suite here opens Postgres. The two
readers that would (`situations.get_situation` / `list_pending_situations`) are stubbed, because
what is under test is the classification and the rendering, not psycopg. Where a suite needs git it
uses REAL git (`conftest.build_repo`: a bare remote plus a steward's clone, fresh per test), and
`require_gitleaks` skips on a laptop without the binary but FAILS in CI rather than letting a
secrets gate pass by never running:

| Suite | Covers |
|---|---|
| `test_birth.py` | resolve-before-mint (`prepare`/`recheck`), the page renderer, `_yaml_str`'s escaping contract |
| `test_generator.py` | `read_entity_pages`, drift detection (`compare`), `regenerate`'s idempotence |
| `test_clone.py` | preflight (branch/identity/clean/in-sync), the fetch-regenerate-retry push loop, never-force-push |
| `test_situations.py` | `classify`/`subject_of`/`subjects_of` including the legacy `open_question`-prefix fallback and the multi-name (`SITUATION_NAMES_KEY`) vs single-name (`SITUATION_NAME_KEY`) fallback, `require_situation`'s three refusals |
| `test_cli.py` | `create` and `regenerate` end to end through `cli.main` against a real bare remote (collisions, the gitleaks refusal AND its benign twin, the post-rebase recheck, the two-steward race) — exercises `mint.mint` transitively through `cli._mint`'s thin adapter, so every discipline in `mint.py` is proven here whether or not `mint.py` has a test file of its own; `show`/`list` through their `_cmd_*` with `situations` stubbed — every printed-command safety case lives here. `--json`, exit codes. `approve`'s own end-to-end path is `tests/librarian/test_entity_full_circle_pg.py` below |
| `test_remote.py` | `remote.mint_via_clone`/`_authenticated_url` (ADR 030 D3), unit-level and keyless: the tokenized-URL shape (proven against `gitcmd`'s own scrub regex, not by inspection), the absent-vs-half-configured-vs-token-exchange-failed three-way refusal split, and that a throwaway clone is removed even when `mint.mint` raises. `githubapp.installation_token` is monkeypatched rather than driven through its own `opener=` seam — this module has exactly one caller and threads no `opener` of its own through |
| `test_errors.py` | the error hierarchy's messages, `CapabilityUnavailableError` included |
| `test_stigmergy_lint.py` | the frozen fixture copy of the knowledge repo's linter, incl. the registry-consistency rule and the quoted-key fix |

`tests/librarian/test_entity_full_circle_pg.py` is the one integration test that proves this package
composes with `capture` + `server.service` + `librarian`: an unregistered entity is asked about, a
steward approves it through `stigmergy-entities` from a SEPARATE clone, and the originating capture
re-files anchored to the newborn entity — against a local bare remote, with no worker restart
(exercises fetch-before-claim). It lives there rather than under `tests/entities/` because the
fixtures it needs (a real bare remote, the double agent, `capture_queue`) are `tests/librarian/
conftest.py`'s.

`tests/server/test_review.py` is the equivalent proof for the review-inbox door (ADR 030): an
entity-proposal approve, driven through `review.review_decide`, mints for real against a real bare
remote — commit, `Approved-by:` trailer, page, registry, the ledger's `extra` — plus every refusal
that must leave git untouched (old-shape calls, authorization, drift, a missing credential). It
lives there, not here, for the same reason the librarian's own integration test does: the fixtures
(`env`, `conn`, a steward identity) are `tests/server/conftest.py`'s.

`tests/admin/test_service_pg.py`/`test_routes_pg.py` are the THIRD door's own proof: `entity_
approve` mints for real against `tests/admin/conftest.py`'s own bare remote
(`build_bare_knowledge_repo` — a LOCAL, minimal builder, not a cross-import of either fixture
above; each package that needs real git for this builds its own), plus the actor-attribution
bookkeeping, `requeue=False`, the entity-id slug default, and the missing-capability /
no-longer-parked refusals — no steward check or self-approval refusal to prove here, because
this door deliberately has neither (D2).

## Common tasks

| Task | Touch |
|---|---|
| Add a new identity field a steward may author | `birth.Proposal`, `birth.prepare`'s validation, `birth.render_page`'s `fields` dict (through `_yaml_str`), the CLI flag in `cli.build_parser` — and, since ADR 030, `mint.mint`'s own signature and all THREE mint doors that carry identity fields through it: `server.review._decide_entity_proposal`/`review_decide`'s new kwarg plus `mcp_server.py`'s `review_decide` closure, `slack.render`'s mint modal, and `admin.service.entity_approve`'s own signature plus the console form field in `views.js` — and `generator`'s reader if the registry should carry it |
| Change what counts as a collision | `birth._refuse_collisions` — the one function both `prepare` and `recheck` call; do not add a second collision check elsewhere |
| Change the registry's derivation | `generator.read_entity_pages` (what a page contributes) and `generator._index` (how `by_alias` is populated) — keep both in lockstep with `stigmergy.kernel.registry.load_registry`'s own semantics |
| Add a new `stigmergy-entities` subcommand | `cli.build_parser` + a `_cmd_*` function; reuse `situations`/`birth`/`generator`/`clone`/`mint` rather than inlining logic in the CLI |
| Change the push safety checks | `clone.py`'s `ensure_*` functions, in the order `preflight` calls them (cheapest and most consequential first) — `mint.mint` calls the three non-identity ones directly, so a reordering there needs mirroring |
| Change the mint discipline itself (drift, the template, the secrets scan, the commit/retry) | `mint.py` — the ONE place, shared by `cli.py` and `remote.py`; never re-derive any of it at a second call site |
| Change how a server-driven mint authenticates or where it clones from | `remote.py`'s `_authenticated_url` (the credential) / `mint_via_clone`'s `repo_url` argument (the source — `server.settings.librarian_repo_url` is where the caller reads it from) |

## Notes

- **Layering, mechanically pinned** (`tests/test_architecture.py`): `entities` imports `capture`
  (the queue's read path, `dispositions`, `schema`),
  `stigmergy.kernel` (the registry reader/writer, `normalize`, the frontmatter parser) and, of
  `stigmergy.librarian`'s modules, `gitcmd` / `errors` (see `clone.py`'s docstring for why reaching
  into `gitcmd` is a deliberate reuse and not a rewrite; `errors` carries the exception types
  `gitcmd`/`gates` raise), `config` (`mint.py`'s `gitleaks_bin` default) and `gates` (`mint.py`'s
  secrets scan, ADR 030 — moved out of `cli.py`-only territory into the shared mint seam) and
  `githubapp` (`remote.py`'s App credential, ADR 030 D3 — the SAME precedent `gitcmd` already set:
  a door open to the whole library-module bucket even though only ONE module walks through it
  today). `cli.py`, the front door, additionally reaches `stigmergy.index` (the one connection seam,
  exactly as in `capture.cli`). The package must never import `server` or `answer`, and neither
  `librarian` nor `capture` may import it back — both edges are asserted
  (`test_librarian_never_imports_entities`, `test_capture_never_imports_entities`), not merely
  documented. `views` is a third package forbidden to import it
  (`test_views_never_imports_server_answer_capture_or_entities`).
- **There are TWO inbound edges, both named exceptions, and NEITHER is read-only any more, since
  ADR 030: `stigmergy.server.review` and `stigmergy.admin`.** Both walk the SAME governed mint door
  (`remote.mint_via_clone` -> `mint.mint`) directly, and neither is a license for the other to
  widen — they are reviewed, symbol- or submodule-scoped, independently.
  - **The review inbox's edge** (MCP, Slack) may import exactly six symbols — `situations` (the
    whole module, because the inbox and `stigmergy-entities` must agree byte for byte about what an
    entity situation IS), `generator.canonical_id_for` / `generator.ENTITY_TYPES` (an approve's
    `entity_id` default and `entity_type` validation), `remote` (the whole module — a
    server-driven mint's ONE door, walked only from the one governed verdict allowed to) and
    `errors.EntityError` / `errors.CapabilityUnavailableError` (the two names a mint refusal is
    mapped through). `cli.suggestable_entity_name` left this allowlist the same change
    `mint_command` left `review_decide`'s response (D5): nothing on that lane prints a shell
    command any more. Pinned by
    `test_server_never_imports_entities_beyond_the_one_declared_review_lane_exception` and its own
    positive (`declared ⊆ used`) twin.
  - **The admin console's edge** may import `situations` (the pending-identity read, unchanged),
    `remote` (the same mint door), `generator` (`ENTITY_TYPES` + `canonical_id_for`, the same two
    facts the review inbox reaches for) and `errors` (`EntityError`, mapped to `AdminRefused`).
    `birth` and `cli` left this allowlist: both existed only for the bracket-placeholder
    `stigmergy-entities approve` command `_entity_commands` built (ADR 029), deleted whole the same
    change that built the console's real Approve form (D5) — nothing here prints a shell command
    any more either. **This edge does NOT go through `review_decide`** — `admin.service.
    entity_approve` drives `entities.remote` directly, the same way this package already drives
    `capture.dispositions` directly for the Queue tab, never through `BrainService`/`review_decide`
    (both banned imports for `stigmergy.admin`, `test_admin_never_imports_the_read_path_or_the_mcp_
    adapter`). This is not a shortcut: `review_decide`'s steward check and self-approval refusal
    are for a RESOLVED identity (a bearer token, a Slack profile), and the console's `actor` is a
    free-text field on a shared admin credential — routing through `review_decide` would either
    wrongly enforce that check against it or require bypassing the one governance gate the review
    lane exists to hold, neither of which is what D2's attribution-not-authorization ruling asks
    for. The console's own `review_decisions` write reuses `server.review.record_decision`
    (public since this change) rather than re-implementing the ledger insert a second time.
    Pinned by `test_admin_imports_only_its_declared_set`, whose own positive twin
    (`test_admin_actually_uses_its_declared_librarian_exception`) exists today only for the
    `librarian.config` exception — the entities/`review_kinds` grants added here have no
    analogous pruning test yet (a follow-up, not a gap this change introduces silently: the
    grep-based `test_admin_imports_only_its_declared_set` still refuses anything undeclared).
- **`clone.py`'s refusal messages are wording-parameterized, not hardcoded.** `Wording`
  (a frozen dataclass) carries every entity-specific noun `identity`/`preflight`/`commit_and_push`/
  `_rebase_onto_remote` compose their messages from; `ENTITY_WORDING` is the default every call site
  in THIS package relies on, unconstructed. The parameterization was earned by a SECOND caller — a
  fleet supervisor with its own `PLAYBOOK_WORDING`, chosen over a near-duplicate copy of this
  module — and that caller is gone. Nothing exercises the parameter today; a future second caller
  supplies its own `Wording` and never edits `ENTITY_WORDING`'s defaults.
- **Where the proposal lives: in Postgres, not in a file.** The `triage` row IS the proposal. An
  `ops/entity-proposals.json` was considered and deliberately not built: the librarian may not
  write `ops/`, so that file could only ever be written by this CLI — duplicating the row and
  creating a DB↔file sync surface for no benefit.
- **This file's structure matches [`librarian/index.md`](../librarian/index.md)**, the first `src/`
  package to set the convention every code map in this repo follows.
