# repair — the governed repair loop: a finding's path to zero

Sibling that produces the findings and fixes nothing: [`gardener`](../gardener/index.md).

**The covenant, in one sentence: a MODEL proposes, CODE validates twice, a HUMAN approves one at a
time, and code applies exactly what was approved.** Nothing here can write to the knowledge repo
without having passed through all four.

FOUR proposal kinds, and a finding rides exactly one road. `edits` is the librarian's own
declared-edit vocabulary and nothing else — `backlink`, `overlap`, `contradiction` — three strictly
additive shapes the eight gates already know how to judge. `entity-body` is the one kind that
REPLACES text: one drafted body for one entity page whose body does not say what the corpus knows
about the entity — still the template it was minted with, or written and empty of anything specific
— judged by `gate_body_rewrite`'s permitted-rewrite branch instead of its additive proof (ADR 039's
first amendment). `delete` is the one kind that REMOVES anything, and the one the covenant's first clause
reads differently for: **no model may propose it in any spelling.** A person types it at
`stigmergy-repair delete`, or code derives it for exact-duplicate `sources/` pages, where the
decision is a lookup rather than a judgment (ADR 039's second amendment). `entity-alias` is the one
kind that answers a finding about a PAIR: two registry entries that are the same entity, where the
model picks which name SURVIVES and says why, and code computes everything that follows — the
spellings that move, the pages that re-anchor, the supersession, the regenerated registry (ADR
039's third amendment). It is also the only kind that puts a file which is not a page into a
governed commit, and the only one that reaches `stigmergy.entities`.

The proposer's judgment (which finding is worth repairing, which shape fits, what an entity page
should say, when a finding has gone stale and deserves nothing) lives in a skill in the KNOWLEDGE
repo, read at run time from the checkout; a missing skill is a named refusal, never a default.

## Modules

| Module | What it is |
|---|---|
| `cli.py` | `stigmergy-repair propose \| list \| show <id> \| delete <path>... --why` — the only module that opens a connection or imports `stigmergy.index.store`. **No `apply`**: a terminal knows who is typing, not what they may approve. `delete` is the one verb that CREATES a proposal here, at `propose`'s authority level, because a deletion is the one repair no model may propose. Owns `preview`, the git-free rendering of what a proposal would change |
| `proposer.py` | The agent seam, ALL THREE model roads (`EDIT_PROPOSABLE_CHECKS`, `BODY_PROPOSABLE_CHECKS` and `ALIAS_PROPOSABLE_CHECKS` decide which one a finding rides; the deterministic duplicate-sources road asks no model at all): `ProposerContext` and its two READ tools; `ProposalBatch`/`ProposalSpec`/`EditOp` + `validate_batch` for the additive road; `EntityBodyDraft` + `anchored_pages`/`draft_entity_body`/`validate_draft` for the body road; `EntityMergeChoice` + `build_entity_alias_prompt`/`choose_survivor`/`validate_merge_choice` for the merge road; one retry each (every `agent.run` records its spend against its limits into `ProposeResult.model_calls`, persisted in `job_runs.stats` — the budgets' feedback loop, issue #81) — but never for a PARK, which both the body road (an empty `body_markdown`, `BODY_DECLINED_REASON`) and the merge road (an empty survivor, `MERGE_DECLINED_REASON`) treat as the answer their brief asked for rather than as an error to push the model off; `read_skill`, `propose_from_findings`, and the three offline doubles. The only module here that loads a model stack |
| `entity_body.py` | The `entity-body` writer and its validator — `validate`, `apply_declared`, `rewritten`, and the bounds a draft lives inside. Pure of the model stack, because the APPLY runs it inside the MCP server process |
| `entity_alias.py` | The `entity-alias` kind, whole: `plan` (the merge, a pure function of a worktree's bytes), the three page writers (`aliased`, `retired`, `reanchored`), `validate`/`apply_declared` (recompute, byte-compare, write the pages, regenerate, byte-compare the registry), the readers every other surface goes through (`survivor_path`, `absorbed_path`, `reanchored_paths`, `expected_bytes`, `derived_files`, `lane_for`), and `claimable_aliases` — the one place the contract linter's alias rule is enforced at PLAN time. The ONE module here that imports `stigmergy.entities`, and only its generator: the registry has exactly one writer |
| `deletion.py` | The `delete` kind, whole — `plan` (the sweep, a pure function of a worktree's bytes), `scrubbed` (one page's planned bytes), `validate`/`apply_declared` (recompute, byte-compare, perform), the readers every other surface goes through (`deleted_paths`, `scrubbed_paths`, `expected_bytes`, `lane_for`), and `duplicate_source_groups` (the one automatic road, which asks no model) — AND the package's shared bytes-level primitives, which the two other non-additive kinds reach through it rather than re-derive: `page_refusal` (the ONE confinement predicate), `read_text`, `sha256`, `page_stem`, `corpus_pages`, `plan_bytes`, `oversize_reason`, `PROVENANCE_ZONE_PREFIXES`. Its link scanner is hand-mirrored from the frozen contract linter and must stay that way |
| `remote.py` | `apply_via_clone` (clone → the kind's applier → the per-kind cross-check → `run_gates` → gated commit → push; the two non-additive kinds push with `rebase=False`, so a lost race fails clean instead of replaying the approved diff onto a base the gates never judged) and `apply_approved`, the door that also records the outcome. Owns `commit_message`, `LEDGER_RESULT_KEYS`, the delete kind's whole-tree dead-link check, and `_lane_and_permission` — the four caller-scoped facts the gates are told |
| `store.py` | `repair_proposals` persistence: `insert_proposal`, `pending_proposals`, `recent_decided`, `proposal`, `mark_decided`, `mark_applied`, `mark_failed`, `known_content_keys`. Pure — decides nothing, authorizes nothing |
| `schema.py` | The DDL behind `startup_ddl_lock`, `JOB_NAME`, the kind/status/op-name vocabularies, and the op record: `declared_edits`, `target_paths`, `content_key` |
| `settings.py` | `RepairSettings.from_env` — the model and the four bounds. The ONE place this package reads the environment for configuration |
| `errors.py` | `RepairError`, and `ProposalStateError` for "somebody got there first / there is nothing to do" |

**Two doors decide who may approve, and neither is here.** `server/review.py` reaches `store`,
`schema`, `errors` and `remote.apply_approved` (a declared, reasoned import edge); `stigmergy.admin`
reaches `store`, `schema` and `errors` and enters the apply through
`server.review.apply_repair_and_record`, the ONE ordering both doors run. Neither may reach
`proposer.py`, and that is why `remote.py` must not load a model stack: it runs inside the MCP
server process, and `tests/test_architecture.py` pins the separation from both sides — this
package's `test_only_the_proposer_loads_a_model_stack`, and the server's declared symbol list.

## Reuse

- `deletion.page_refusal` / `read_text` / `sha256` / `corpus_pages` / `plan_bytes` — the
  intra-package seam all three non-additive kinds share, one implementation each so the kinds
  cannot disagree about which files are pages, whether the corpus moved under a proposal, or what
  a plan weighs. A fourth kind reaches these through `deletion`, never copies them.
- `librarian.edits.validate` / `apply_declared` — the SAME validator both ends run for the `edits`
  kind; `entity_body` and `deletion` each own their kind's twin. Propose time proves a proposal is
  storable; apply time proves it still applies to the clone. Neither trusts the other: they are
  asking about two different trees. `deletion.apply_declared` goes one step further and RECOMPUTES
  its plan, because a sweep's content depends on every other page in the corpus.
- `librarian.page` — the frontmatter LINE machinery (`top_level_key_line`, `top_level_key_span`,
  `frontmatter_lines`, `strip_key_lines`, `yaml_scalar`, `yaml_list`, `parse_list_value`,
  `with_related_link`) that `entity_body` rewrites `updated:`/`role:` through, `deletion` removes
  list entries and pointer lines with, `entity_alias` rewrites `aliases:`/`superseded_by:`/`entity:`
  through, and `gate_body_rewrite` compares the before and after with. ONE owner for "what lines
  does a top-level key occupy", or two writers and a gate could disagree about the same block.
- `entities.generator.read_entity_pages` / `registry_of` / `regenerate` — the ONE derivation of
  `ops/entity-registry.json` from `wiki/entities/*.md`, and the only edge from this package into
  `stigmergy.entities`. `entity_alias.plan` PREDICTS the regenerated bytes through that reader plus
  `kernel.registry.registry_text`; the apply runs the real `regenerate` and refuses unless the file
  it produced is byte-identical. One writer of the registry, still — never a hand-built JSON here.
- `librarian.gates.run_gates(ALL_GATES)` — a repair goes through the librarian's own eight gates,
  not a subset. `GateContext(material="", outcome=None)` is honest: nothing was captured and no
  agent wrote here, and every gate that reads either is scoped to CREATED pages, of which a repair
  diff has none.
- `librarian.gitcmd.commit(gated_entries=…)` — the diff the gates approved is the diff that lands.
- `librarian.githubapp.authenticated_clone_url` — the SHARED credential resolver, the same one
  `entities.remote` mints through, so the two server-driven doors cannot disagree about when a
  credential is needed.
- `librarian.gather.load_corpus` / `search_candidates` / `confined_page` — one lexical ranking and
  one containment rule behind the proposer's tools and the filing agent's.
- `gardener.store.latest_completed_run` / `findings_for_run` — findings are READ, never
  recomputed; `subjects` is the LIST, never the display string re-split. The same list is stored
  on the proposal as `finding_subjects`, which is what lets the pre-model skip recognise the same
  question under a new finding id.
- `kernel.llm.build_processor` — the one fake/real dispatch, `CLEAN_LLM`-driven offline.
- `capture.ops.record_job_run` — one `job_runs` row per propose pass.

## Avoid

- Never give the proposer a write tool, a third tool, or a path into `gitcmd`. Its two tools read,
  and its inability to write is structural, not promised.
- Never extend the ADDITIVE op vocabulary past `page.EDIT_KINDS` — a new shape there is a new gate
  question. A new KIND is a bigger decision, not a smaller one: it needs its own validator, its own
  writer, its own branch in the gates and its own ADR record, which is what `entity-body` and
  `delete` each have.
- Never let a MODEL compute a file list. `entity-alias` hands it exactly one decision — which of
  two entity pages survives — and `entity_alias.plan` derives every byte that follows from the
  corpus. A road where the model named the pages to re-anchor would be #72's deletion lesson
  re-learned on a kind whose error is a page's whole history moved onto the wrong company.
- Never let a MODEL reach the `delete` kind. `validate_batch` refuses an op naming a deletion in any
  spelling, by name, and the deterministic duplicate road is CODE — not a model call whose answer
  happens to be checked. Judging that a page is stale is a person's decision.
- Never widen `GateContext.body_rewrite_allowed`, `deletions_allowed`, `expected_bytes`,
  `derived_files` or `provenance_pages` past what the proposal names, and never set one from
  anywhere but `remote.py`
  (pinned in `tests/test_architecture.py`, both directions, plus a classification check over every
  keyword any module passes to a `GateContext`). A permission wide enough for a second page is a
  permission for a page nobody approved.
- Never let `deletion`'s link scanner drift from the frozen contract linter's. It is hand-mirrored
  on purpose (this package talks to that linter through FILES): a scanner that sees MORE links
  edits prose nobody asked about, and one that sees FEWER leaves a dead link and a veto at apply
  time. `index.corpus.link_targets` answers a deliberately DIFFERENT question — the index's edge
  graph — and is the wrong one to copy.
- Never apply without the cross-check: `run_gates` would happily pass a well-formed additive diff
  that is not the one a steward approved, and only the stored `target_paths` can say so. Its SHAPE
  half is per kind — a sweep that quietly stopped deleting satisfies the path comparison exactly.
- Never restore `approved` after a failed apply. `failed` + the `error` column is the record; a
  silent revert hides that a gate refused.
- Never compose a refusal from a caught exception's text. Every sentence raised from `remote.py`
  reaches a steward verbatim through the review lane; git names this host's throwaway clone.
- Never read the environment at module scope, and never open a connection outside `cli.py`.

## Contracts

- `repair_proposals`: `id`, `created_at`, `run_id`, `finding_ids`, `finding_subjects`, `kind`,
  `target_paths`, `ops`, `rationale`, `content_key`, `status`, `decided_by`, `decided_at`, `notes`,
  `applied_commit`, `error`, `model_id`. `kind ∈ ('edits', 'entity-body', 'delete', 'entity-alias')`; `status ∈ (pending,
  approved, rejected, applied, failed)`. The kind CHECK is swapped by a guarded `DO` block, not
  carried by `CREATE TABLE IF NOT EXISTS` alone — a table that already exists never gains a value,
  and a kind the code writes and the column refuses is an IntegrityError in production at night. `finding_subjects` is a list of LISTS — one sorted page set per
  finding answered, what each one NAMED as against what the answer would edit.
- **A REJECTED row is the dismissal memory.** `content_key` identifies a proposal by what it would
  DO (kind + sorted `op:path:link`, `note` excluded — and for `delete`, the removals ALONE, since
  which pages must also be scrubbed is a fact about the rest of the corpus rather than about the
  question), and the proposer skips a key held by a
  pending, approved, rejected or applied row. "Reviewed and declined" is a durable fact, and a
  steward is not asked the same question every night. `failed` is deliberately NOT remembered — a
  failed apply is a steward's YES that hit a fault, and the next run must be able to derive it
  again; both halves of the memory (`store.known_content_keys` and the pre-model
  `proposer.already_proposed`) exclude it, or the optimisation would suppress what the
  authoritative check forgives. The UNIQUE index is narrower on purpose — pending only — so
  re-proposing after a rejection stays a human decision rather than a constraint violation.
- `EDIT_PROPOSABLE_CHECKS` = `model-unlinked-mention`, `model-contradiction`, `orphan-page`;
  `BODY_PROPOSABLE_CHECKS` = `entity-placeholder-body`, `model-empty-entity-body`;
  `ALIAS_PROPOSABLE_CHECKS` = `model-duplicate-entity`; `PROPOSABLE_CHECKS` is their union and the
  three sets are disjoint. The two body checks are the deterministic and the judged
  halves of ONE question — the page's body does not say what the corpus knows about the entity —
  so they share a road rather than each getting one, and the gardener guarantees they never name
  the same page in one run. The other checks are absent by NAME, not by oversight: none of them is
  answered by a link, a callout or a body. `delete` answers no check at all — it has no finding behind it, which is why its rows
  carry an empty `finding_ids` and say what their question WAS in `finding_subjects` instead.
- **An empty `model_id` on a `delete` row is a statement, not a gap**: no model proposed it, and
  that kind is the only one for which that can be true.
- `job_runs` job `repair-propose`, `stats`: `findings_seen`, `proposed`, `skipped_known`,
  `skipped_invalid`, `skip_reasons`.
- Bounds: `settings.max_ops_per_proposal` (6) is how much ONE approval may be;
  `settings.max_proposals_per_run` (20) is how many approvals one NIGHT may ask for — a batch over
  it is refused whole with a named reason the retry carries, and the run stops batching once it is
  full, recording what it left for the next pass. The SAME number bounds what a per-finding road
  may ASK the model about (`ASK_CEILING_REASON`, issue #103): a declined draft or pair stores
  nothing and is remembered nowhere — deliberately, the answer changes as the corpus grows — so
  without it a corpus of thin entities could spend unbounded calls a night while storing nothing; `MAX_RATIONALE_CHARS` 400, `MAX_NOTE_CHARS` 300,
  `MAX_PAGE_BODY_CHARS` 12000, `MAX_SKILL_BYTES` 256 KiB. The model budget is DERIVED from the
  batch, not fixed: `batch_limits(n)` pays `MIN_TOOL_CALLS_PER_FINDING` (6) per finding plus one
  finding's worth of orientation for the call itself, and `REQUEST_HEADROOM_OVER_TOOLS` (2) above
  that — the tool budget is the work ceiling, the request budget only the runaway bound above it,
  pinned by test to stay dominated at EVERY batch size. A fixed budget against a growing batch is
  what starved the first real corpus, so `settings.batch_size` and the allowance move together;
  the body road holds `BODY_DRAFT_LIMITS` instead — one entity per call, no batch to derive from,
  and the number it always had, because #75 was the edits road's problem and the body road was the
  half that worked. A lapsed budget skips that one batch or draft, recorded, never the run.
  The body road adds `MIN_ANCHORED_PAGES` (2 — below it no model is asked at all, whichever of the
  two checks named the page, and the run records that it was not) and
  `MAX_ANCHORED_PAGES` (10 per prompt), and `entity_body`'s own `MAX_BODY_BYTES` (6000),
  `MAX_BODY_LINES` (110) and `MAX_ROLE_CHARS` (200). Those three are CONSTANTS rather than env
  settings on purpose: the real ceiling is the knowledge repo's contract linter, so an operator
  raising them could only produce proposals the gates then refuse. The delete road adds
  `settings.max_plan_bytes` (100000) — a SIZE rather than an op count, because that kind's
  ops carry whole pages so the apply can recompute and byte-compare them; the merge road SHARES
  that setting, since both store whole pages for the identical reason and one ceiling governs how
  much stored content one approval may carry. The merge road holds `MERGE_CHOICE_LIMITS` (the body
  road's figure, and a constant for the same reason: one pair per call, no batch to derive from).
- **A merge moves the absorbed entity's ALIASES and never its own name, and that is a constraint
  rather than a choice.** The knowledge repo's contract linter refuses an alias that names an
  existing page (`alias 'X' collides with page wiki/entities/X.md`) because the wikilink namespace
  is keyed on page stems, and the absorbed page stays by governance. `entity_alias.claimable_aliases`
  refuses such a claim at PLAN time, with a sentence, rather than letting `gate_contract` veto it at
  apply time. What follows is stated in ADR 039's third amendment and is not a defect to be fixed
  here: the absorbed name keeps resolving to the identity the merge retired, whose page now says
  what absorbed it.
- The proposer's skill: `.claude/skills/repair-proposer/SKILL.md` in the knowledge repo, read at
  run time, refused if the leaf is a symlink, and size-capped before the bytes. THREE frames read
  it — `build_system_prompt`, `build_entity_body_system_prompt`, `build_entity_alias_system_prompt`
  — one procedure, three questions.

- The review lane's own kind is `repair-proposal` (`stigmergy.review_kinds`), decided with
  `approve`/`reject` only, authorized by a steward for EVERY page in `target_paths`, and listed in
  the inbox's MANAGEMENT read only — a proposal has no submitter, and it names page paths. The
  Slack doorbell deliberately does not ring for it: a kind with no card is skipped rather than
  rendered as another kind's.

Tests live in `tests/repair/` (real git, real Postgres, real gates, the offline double for the
agent; `test_deletion.py` is the sweep's plan computation as pure functions, with no database and
no git at all) and, for the two doors, in `tests/server/test_review.py` and `tests/admin/`; the
layering, the module-scope, the connection-seam, the closed apply-caller pins and the three
granting-surface pins in `tests/test_architecture.py`. Narrative:
[`docs/reference/repair.md`](../../../docs/reference/repair.md), decisions:
[ADR 039](../../../docs/decisions/039-governed-repair-loop.md).
