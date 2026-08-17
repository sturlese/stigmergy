# librarian — code map

The fast lane's back half: drains `capture_queue` one row at a time and turns each into either a
committed page in the knowledge repo or an honest, actionable refusal. **The agent judges**
(placement, wikilinks, anchoring, duplication — meaning problems); **code vetoes** (the eight
gates, over the diff, after the fact). Gates check; they never interpret — judgment belongs in
the skill (`.claude/skills/librarian/SKILL.md`, versioned in the knowledge repo, not here).

Narrative: [`docs/reference/librarian.md`](../../../docs/reference/librarian.md); the meeting
flow: [`docs/reference/meeting-distiller.md`](../../../docs/reference/meeting-distiller.md);
view regeneration: [`docs/reference/views.md`](../../../docs/reference/views.md). Design
records live in [`docs/decisions/`](../../../docs/decisions). This file is for whoever is about
to edit this package, not run it.

Layering (`tests/test_architecture.py` enforces it): `librarian` imports `capture` and
`stigmergy.kernel`, and never `server` or `answer` — a worker beside the API; the two talk only
through the durable queue row. `stigmergy.index.corpus` is a declared LIBRARY reach (a pure
repo parser — nothing here touches `pages_index`); `stigmergy.index.store` is reached by
`cli.py` alone; `processing.py` may import exactly one `stigmergy.views` symbol
(`views.regenerate`).

## Modules

| Module | What it is |
|---|---|
| `processing.py` | `process_item` — one capture end to end (`Result`, `Deps`, the refused-diff digest); `process_meeting_item` — the sibling for `kind="meeting"` rows, filing a page SET; `process_drive_item` — the thin drive sibling (kernel-hands conversion, then `process_item` with the source attachment on). The flows share one spine, and a new one joins it rather than copying it: `_resolve_filing_base` (this item's base commit, plus the `Deps` re-read at it), `_commit_and_push` (gated commit → lease re-check → push) and `_route_refusal` (which terminal state a surviving veto earns, with the anchoring park the one branch each flow passes in). Read it first when tracing a capture's path; everything else is reached from it |
| `worker.py` | the loop: `startup_checks` (every fail-closed startup refusal), `sweep`, `Worker` (signal handling), `process_next` — the only caller of the whole processing path |
| `gates.py` | the one veto surface: `Finding`, `GateContext`, `run_gates`, `ALL_GATES` (eight gates: zone, binary, body-rewrite, secrets, pii, frontmatter, contract, anchoring). A new check is a `(ctx) -> list[Finding]` added to `ALL_GATES`, never a special case in `processing.py` |
| `agent.py` | the agent seam's shared half: `build_agent`, `BACKENDS`, `parse_outcome` / `parse_meeting_outcome` (the trust boundary both backends' accounts cross), `confined_write` (the write allow-list), the prompt builders and the fence |
| `pydantic_backend.py` | `PydanticFilingAgent` — the real backend, serving both flows: an iterating ordinary run (five confined tools, an outcome file, it writes its own page) and one structured meeting call. `FilingToolbox` holds the tool bodies so every confinement rule is testable with no model; `_register_tools` writes the model-facing docstrings, which ARE the tool schema |
| `double.py` | `DoubleAgent` — the offline, directive-driven backend the whole keyless suite runs against; misbehaves on demand, behaves perfectly on ordinary material, and writes through `agent.confined_write` like the real one |
| `filing_port.py` | `FilingAgent` — the port `processing.py` is written against: the two calls, the `AgentRun` envelope, `priced()`, the fault contract, the per-flow side-effect rules, and the two declared capabilities (`structured_ordinary`, `wants_gathered`). Imports `errors` and nothing else |
| `gather.py` | the deterministic gatherer — a pure function of `(worktree, registry, material)` producing the seeded context BOTH flows get (the ordinary agent's seed, and the tool-less meeting agent's whole view of the corpus), and the bodies the ordinary search/read tools share (`load_corpus`, `search_candidates`, `confined_page`). Reads the checkout, never `pages_index` |
| `page.py` | the placement table (`PAGE_TYPES`: SEVEN types known, THREE creatable — `note`, `decision`, `concept`; every derived list and regex computes from it), path identity (`path_key`, `is_inside`), server-owned frontmatter (`SERVER_OWNED_KEYS`, `stamp_server_fields`, `stamp_source_fields`), page-name policy (`unnameable_reason`), the additive-edit primitives |
| `edits.py` | declared edits to existing pages, on BOTH flows: the agent declares, `validate`/`apply` perform — all-or-nothing, judged by the gates like any other write. Its editable set is `page.FOLDER_BY_TYPE`'s three folders; a caller whose own lane is narrower (the meeting flow) sees the difference refused by `gate_zone` |
| `report.py` | the ONLY place a sentence a human reads about a fast-lane outcome is composed; the CLI (`render_prose`) and `brain_submissions` render the same fact set. Its shape (`base_report`) and `SEARCHABILITY_NOTE` live in `capture.schema`, re-exported. A steward-authored sentence belongs in `capture.dispositions` instead |
| `dedup.py` | the two DB-backed pre-agent levels: `find_retry` (retry collapse) and `find_already_filed` (exact re-file), keyed on the queue's own content hash |
| `base_inputs.py` | the ONLY way the fast lane reads the ACL config, the entity registry, the contract linter and `ops/stewards.json` — at `base.sha`, never off the working tree (a working-tree read is a read around the governed steward flow) |
| `acl_rules.py` | the ACL config ADAPTER: the knowledge repo's on-disk dialect normalised into `kernel.acl`'s and handed to `acl.resolve_acl` — one matching algorithm, never a second. `resolve(config, path)` answers which audience labels a filed page carries, `None` meaning `page.py` writes no `acl:` line; anything it cannot translate faithfully is a fail-closed `LibrarianConfigError` rather than a guess |
| `config.py` | `Settings` — every tunable, resolved once (`from_args`, the only place the SETTINGS are read from `os.environ`); the visibility lease derived from the per-item bounds. Also the shared `--repo` resolution every operator CLI outside this package funnels through: `repo_path` (explicit → `$STIGMERGY_REPO` → default, absolute) and `is_repo_checkout` (worktree-tolerant: `.git` is a FILE in a `git worktree add` checkout, and that file must actually be a `gitdir:` pointer — a stray `.git` file is not a checkout) |
| `pricing.py` | model id → $/MTok, four figures per row (`PRICES` + `$STIGMERGY_LIBRARIAN_PRICING`, `AS_OF`); `compute_cost_usd`, `require_priced` — for backends that report tokens rather than dollars; an unpriced model is refused at startup |
| `gitcmd.py` | everything git: ephemeral worktrees and their reap, the diff (`diff_entries` / `added_lines` — the structured forms every gate reads), the gated `commit(gated_entries=…)`, the rebase-and-retry `push`. `--text` and `core.quotePath=false` are load-bearing on every diff invocation |
| `githubapp.py` | the GitHub App identity: JWT → installation token → `push_config` (the token travels in the environment, never argv), the commit identity, and `repo_slug` — the ONE `owner/name` parser, beside its only consumers (`push_url`/`push_config`) and the reason this module touches `gitcmd` |
| `gitcredential.py` | `stigmergy-librarian-credential` — the App-backed git credential helper the container's per-item fetch authenticates through |
| `bootstrap.py` | `stigmergy-librarian-boot` — the deployed worker's entry: clone-or-fast-forward, verify checkout == base ref, strip the read path's secrets, exec the loop |
| `cli.py` | `stigmergy-librarian` — `once` / `run` / `status`, the operator's front door; conventions shared with `stigmergy-queue` |
| `errors.py` | the `LibrarianError` hierarchy; `LibrarianConfigError` (startup, fail closed) vs per-item errors, `StaleBaseError` (stops the loop), `OutcomeShapeError` (carries findings into the corrective retry) |

## Cross-module rules

- **Path questions go through `page.path_key` / `path_keys` / `is_inside`, never `==`** — the one
  answer that is correct on a case- and normalization-insensitive filesystem, shared by
  `agent.confined_write`, `edits.validate`, `gather._confined` and `processing._write_new`.
- **`processing._write_new` is THE write for every page-building flow** — containment plus
  `OSError`-to-`WorktreeError` in one place; never call `page.open_for_new` from a flow directly.
- **Gates read the diff through `gitcmd.diff_entries` / `added_lines`, never a hand-parsed
  rendered diff** — page content can be spelled to look like diff metadata.
- **`gates.registry_candidates` is the ONE reading of "which entities exist"** — the agent brief
  and the human question both list it, and a second implementation would let the two disagree.
- **The three repo-sourced inputs are read at `base.sha` through `base_inputs`** —
  `Settings.acl_path` / `registry_path` / `linter_path` answer only "where does this live in a
  checkout", for steward tooling and messages.
- **The agent never touches an existing page** — `agent.confined_write` admits one NEW `.md` page
  in a creatable folder; an edit is declared in the outcome and performed by `edits.py`.
- **A gate is TOLD a fact, it never infers one** — the flow-scoped `GateContext` fields default
  to the ordinary lane, and the two callers that widen them (the meeting flow, the source
  attachment) widen the instance they build, never a module constant: a new flow is out of
  bounds by default. `edits_allowed` is the one field no caller declares any more: the meeting
  flow set it `False` until it gained the same declared-edit mechanism (ADR 038).
  FOUR of them SUSPEND a proof rather than narrowing a lane, and each has its granting surface
  pinned both directions in `tests/test_architecture.py`. Three are `repair/remote.py`'s alone (ADR
  039's two amendments): `body_rewrite_allowed` names the single page whose body a governed repair
  may replace, `deletions_allowed` the paths a governed sweep may remove, and `expected_bytes` the
  exact file a caller computed for a page it rewrites. Empty, each changes nothing — every capture
  still meets `gate_body_rewrite`'s additive proof unchanged, and `gate_zone`'s "the librarian never
  deletes a file" stays literally true. The fourth, `provenance_pages`, has two granters making the
  same claim: `processing.py` for the source pages one capture just wrote, and `repair/remote.py`
  for the machine-zone pages a sweep rewrites — those stamps are the librarian's own, and neither
  flow is the thing that asserted one.
- **Wording for humans lives in `report.py` only**; a read path branches on `reason_code`
  (`capture.schema.REJECTION_REASONS`), never on prose and never on `stage`, which is
  `failed_system`'s alone. An unresolved-entity park gets ONE pair of builders (`needs_input` /
  `triage_entity`) for both flows and any number of names; what an ordinary capture and a meeting
  differ in is their `meeting` flag — the noun the submitter's own material is called and what is at
  stake — never a second builder telling one of them about the other's flow.
- **A park writes ONE name shape: the plural, a list even for one name** — `unresolved_names` on
  the ask side, `capture.schema.SITUATION_NAMES_KEY` on the parked row. The count chooses the
  SENTENCE (one name reads as one name, never "1 things"), never the keys. The singular keys are
  read-only LEGACY, not a deprecation waiting to be finished: nothing writes them, rows already
  carrying them are never migrated, and `entities.situations.subjects_of` reads them permanently.
  `report.py` is the DECLARED ONE WRITER, and that is enforced, not just described:
  `tests/test_architecture.py` requires every module naming either constant AS CODE to be the
  definition (`capture/schema.py`), the writer (here) or the reader (`entities/situations.py`) —
  anything else is a listed exception carrying its reason, with the pruning test every allowlist
  in that file has. Prose about the keys is free; a second writer is a reviewed decision there.
- **An unresolved-entity park's names are normalised ONCE, in `processing._unresolved_names`** —
  the account may spell them `triage.name` or `triage.names` (`agent.parse_outcome` folds the
  singular into a one-element list at the boundary; accepting both inbound is not writing both
  outbound), and both flows reach `_ask_or_park`, the ONE park router. A second reader that strips
  differently makes one name mint two registry entities that never match each other. The
  completeness check reads the RAW values of both spellings, so a name that failed its own bound
  earns that finding alone — never a second, contradicting "never declared" one in the same brief.
- **Do not put a filesystem path — or any `str(exception)` — on the wire for a mid-run fault**:
  `worker.process_next`'s config branch logs the real exception and returns a fixed sentence
  naming only the stage.
- **Do not log a HANDLED failure with `exc_info=True`** — a traceback above a careful sentence
  makes a handled validation read as a crash; wrap any new external loader so no
  non-`LibrarianError` escapes `cli.main` as a stack trace.
- **Do not read the environment at import time** — `config.Settings.from_args` is the only place.
- **Do not import an agent framework at module scope** — `pydantic_ai` is imported inside the
  backend's own methods only, so a keyless run loads no framework (`pydantic` itself is
  module-scope in `pydantic_backend.py`: the output schema is plain data). Do not route a
  librarian backend through `kernel.llm.build_processor` either — the librarian's offline path
  is `double.DoubleAgent`, answering to `$STIGMERGY_LIBRARIAN_BACKEND`.
- **Do not import `stigmergy.entities`** — the edge runs one way only (the steward's CLI imports
  this package); where both need the same fact, it is stated at both ends with the duplication
  declared. Do not import `stigmergy.server` or `stigmergy.answer` from anywhere in this package.
- **Do not run two long-running workers on the same repo AND the same worktree root** — give each
  its own `STIGMERGY_LIBRARIAN_WORKTREE_ROOT`.
- **The skill is the agent's ONE briefing**, read at the base commit and injected into the system
  prompt; adding a second injected text is a design decision, not a patch. The ordinary and
  meeting briefs live in the knowledge repo and each has a two-sided rule-table contract test
  (`test_librarian_brief_contract.py`, `test_meeting_brief_contract.py`) — resync both sides,
  and both frozen copies, in one landing.
- **A backend that reports tokens must be priced** (`pricing.require_priced`, at startup) — a
  silent `$0.00` reads as free, in the one direction nobody audits.
- **`result_ref`/`sha` name a meeting's OWN commit, not the branch tip** — the post-filing view
  regeneration can push a second commit on top; code that reads "the current tip" to learn what
  a capture filed is wrong (see [`views/index.md`](../views/index.md)).

## Tests

Everything under `tests/librarian/` runs keyless against `double.DoubleAgent` — real git, real
Postgres, no API key. Every adversarial directive has a benign twin, and the frozen copies of
the knowledge repo's linter and briefs carry their own drift tests (`test_frozen_linter.py`,
the two brief-contract suites). `scripts/e2e_librarian.py` drives a host worker against a real
bare remote; `scripts/e2e_librarian_container.py` drives the deployed image's worker.
