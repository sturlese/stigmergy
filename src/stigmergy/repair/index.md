# repair — the governed repair loop: a finding's path to zero

Sibling that produces the findings and fixes nothing: [`gardener`](../gardener/index.md).

**The covenant, in one sentence: a MODEL proposes, CODE validates twice, a HUMAN approves one at a
time, and code applies exactly what was approved.** Nothing here can write to the knowledge repo
without having passed through all four.

TWO proposal kinds, and a finding rides exactly one road. `edits` is the librarian's own
declared-edit vocabulary and nothing else — `backlink`, `overlap`, `contradiction` — three strictly
additive shapes the eight gates already know how to judge. `entity-body` is the one kind that
REPLACES text: one drafted body for one entity page still carrying its template, judged by
`gate_body_rewrite`'s permitted-rewrite branch instead of its additive proof (ADR 039's amendment).
The proposer's judgment (which finding is worth repairing, which shape fits, what an entity page
should say, when a finding has gone stale and deserves nothing) lives in a skill in the KNOWLEDGE
repo, read at run time from the checkout; a missing skill is a named refusal, never a default.

## Modules

| Module | What it is |
|---|---|
| `cli.py` | `stigmergy-repair propose \| list \| show <id>` — the only module that opens a connection or imports `stigmergy.index.store`. **No `apply`**: a terminal knows who is typing, not what they may approve. Owns `preview`, the git-free rendering of what a proposal would change |
| `proposer.py` | The agent seam, BOTH roads: `ProposerContext` and its two READ tools; `ProposalBatch`/`ProposalSpec`/`EditOp` + `validate_batch` for the additive road; `EntityBodyDraft` + `anchored_pages`/`draft_entity_body`/`validate_draft` for the body road; one retry each, `read_skill`, `propose_from_findings`, and the two offline doubles. The only module here that loads a model stack |
| `entity_body.py` | The `entity-body` writer and its validator — `validate`, `apply_declared`, `rewritten`, and the bounds a draft lives inside. Pure of the model stack, because the APPLY runs it inside the MCP server process |
| `remote.py` | `apply_via_clone` (clone → the kind's applier → the cross-check → `run_gates` → gated commit → push) and `apply_approved`, the door that also records the outcome. Owns `commit_message`, and `_lane_and_permission` — the two caller-scoped facts the gates are told |
| `store.py` | `repair_proposals` persistence: `insert_proposal`, `pending_proposals`, `recent_decided`, `proposal`, `mark_decided`, `mark_applied`, `mark_failed`, `known_content_keys`. Pure — decides nothing, authorizes nothing |
| `schema.py` | The DDL behind `startup_ddl_lock`, `JOB_NAME`, the kind/status vocabularies, and the op record: `declared_edits`, `target_paths`, `content_key` |
| `settings.py` | `RepairSettings.from_env` — the model and the three bounds. The ONE place this package reads the environment for configuration |
| `errors.py` | `RepairError`, and `ProposalStateError` for "somebody got there first / there is nothing to do" |

**Two doors decide who may approve, and neither is here.** `server/review.py` reaches `store`,
`schema`, `errors` and `remote.apply_approved` (a declared, reasoned import edge); `stigmergy.admin`
reaches `store`, `schema` and `errors` and enters the apply through
`server.review.apply_repair_and_record`, the ONE ordering both doors run. Neither may reach
`proposer.py`, and that is why `remote.py` must not load a model stack: it runs inside the MCP
server process, and `tests/test_architecture.py` pins the separation from both sides — this
package's `test_only_the_proposer_loads_a_model_stack`, and the server's declared symbol list.

## Reuse

- `librarian.edits.validate` / `apply_declared` — the SAME validator both ends run for the `edits`
  kind, and `entity_body.validate` / `apply_declared` is its twin for the other. Propose time
  proves a proposal is storable; apply time proves it still applies to the clone. Neither trusts
  the other: they are asking about two different trees.
- `librarian.page` — the frontmatter LINE machinery (`top_level_key_line`, `frontmatter_lines`,
  `strip_key_lines`, `yaml_scalar`) that `entity_body` rewrites `updated:`/`role:` through, and
  `gate_body_rewrite` compares the before and after with. ONE owner for "what lines does a
  top-level key occupy", or the writer and the gate could disagree about the same two lines.
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
  writer, its own branch in `gate_body_rewrite` and its own ADR record, which is what `entity-body`
  has.
- Never widen `GateContext.body_rewrite_allowed` past the ONE page a proposal names, and never set
  it from anywhere but `remote.py` (pinned in `tests/test_architecture.py`, both directions). A
  permission wide enough for a second page is a permission for a page nobody approved.
- Never apply without the cross-check: `run_gates` would happily pass a well-formed additive diff
  that is not the one a steward approved, and only the stored `target_paths` can say so.
- Never restore `approved` after a failed apply. `failed` + the `error` column is the record; a
  silent revert hides that a gate refused.
- Never compose a refusal from a caught exception's text. Every sentence raised from `remote.py`
  reaches a steward verbatim through the review lane; git names this host's throwaway clone.
- Never read the environment at module scope, and never open a connection outside `cli.py`.

## Contracts

- `repair_proposals`: `id`, `created_at`, `run_id`, `finding_ids`, `finding_subjects`, `kind`,
  `target_paths`, `ops`, `rationale`, `content_key`, `status`, `decided_by`, `decided_at`, `notes`,
  `applied_commit`, `error`, `model_id`. `kind ∈ ('edits', 'entity-body')`; `status ∈ (pending,
  approved, rejected, applied, failed)`. The kind CHECK is swapped by a guarded `DO` block, not
  carried by `CREATE TABLE IF NOT EXISTS` alone — a table that already exists never gains a value,
  and a kind the code writes and the column refuses is an IntegrityError in production at night. `finding_subjects` is a list of LISTS — one sorted page set per
  finding answered, what each one NAMED as against what the answer would edit.
- **A REJECTED row is the dismissal memory.** `content_key` identifies a proposal by what it would
  DO (kind + sorted `op:path:link`, `note` excluded), and the proposer skips a key held by a
  pending, approved, rejected or applied row. "Reviewed and declined" is a durable fact, and a
  steward is not asked the same question every night. `failed` is deliberately NOT remembered — a
  failed apply is a steward's YES that hit a fault, and the next run must be able to derive it
  again; both halves of the memory (`store.known_content_keys` and the pre-model
  `proposer.already_proposed`) exclude it, or the optimisation would suppress what the
  authoritative check forgives. The UNIQUE index is narrower on purpose — pending only — so
  re-proposing after a rejection stays a human decision rather than a constraint violation.
- `EDIT_PROPOSABLE_CHECKS` = `model-unlinked-mention`, `model-contradiction`, `orphan-page`;
  `BODY_PROPOSABLE_CHECKS` = `entity-placeholder-body`; `PROPOSABLE_CHECKS` is their union. The
  other checks are absent by NAME, not by oversight: none of them is answered by a link, a callout
  or a body.
- `job_runs` job `repair-propose`, `stats`: `findings_seen`, `proposed`, `skipped_known`,
  `skipped_invalid`, `skip_reasons`.
- Bounds: `settings.max_ops_per_proposal` (6) is how much ONE approval may be;
  `settings.max_proposals_per_run` (20) is how many approvals one NIGHT may ask for — a batch over
  it is refused whole with a named reason the retry carries, and the run stops batching once it is
  full, recording what it left for the next pass; `MAX_RATIONALE_CHARS` 400, `MAX_NOTE_CHARS` 300,
  `MAX_PAGE_BODY_CHARS` 12000, `MAX_SKILL_BYTES` 256 KiB; `PROPOSER_LIMITS` 6 requests / 24 tool
  calls. The body road adds `MIN_ANCHORED_PAGES` (2 — below it no model is asked at all) and
  `MAX_ANCHORED_PAGES` (10 per prompt), and `entity_body`'s own `MAX_BODY_BYTES` (6000),
  `MAX_BODY_LINES` (110) and `MAX_ROLE_CHARS` (200). Those three are CONSTANTS rather than env
  settings on purpose: the real ceiling is the knowledge repo's contract linter, so an operator
  raising them could only produce proposals the gates then refuse.
- The proposer's skill: `.claude/skills/repair-proposer/SKILL.md` in the knowledge repo, read at
  run time, refused if the leaf is a symlink, and size-capped before the bytes.

- The review lane's own kind is `repair-proposal` (`stigmergy.review_kinds`), decided with
  `approve`/`reject` only, authorized by a steward for EVERY page in `target_paths`, and listed in
  the inbox's MANAGEMENT read only — a proposal has no submitter, and it names page paths. The
  Slack doorbell deliberately does not ring for it: a kind with no card is skipped rather than
  rendered as another kind's.

Tests live in `tests/repair/` (real git, real Postgres, real gates, the offline double for the
agent) and, for the two doors, in `tests/server/test_review.py` and `tests/admin/`; the layering,
the module-scope, the connection-seam and the closed apply-caller pins in
`tests/test_architecture.py`. Narrative:
[`docs/reference/repair.md`](../../../docs/reference/repair.md), decisions:
[ADR 039](../../../docs/decisions/039-governed-repair-loop.md).
