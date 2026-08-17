# repair — the governed repair loop: a finding's path to zero

Sibling that produces the findings and fixes nothing: [`gardener`](../gardener/index.md).

**The covenant, in one sentence: a MODEL proposes, CODE validates twice, a HUMAN approves one at a
time, and code applies exactly what was approved.** Nothing here can write to the knowledge repo
without having passed through all four.

The op vocabulary is the librarian's declared-edit kinds and nothing else — `backlink`,
`overlap`, `contradiction` — three strictly additive shapes the eight gates already know how to
judge. The proposer's judgment (which finding is worth repairing, which shape fits, when a finding
has gone stale and deserves nothing) lives in a skill in the KNOWLEDGE repo, read at run time from
the checkout; a missing skill is a named refusal, never a default.

## Modules

| Module | What it is |
|---|---|
| `cli.py` | `stigmergy-repair propose \| list \| show <id>` — the only module that opens a connection or imports `stigmergy.index.store`. **No `apply`**: a terminal knows who is typing, not what they may approve. Owns `preview`, the git-free rendering of what a proposal would change |
| `proposer.py` | The agent seam: `ProposerContext` and its two READ tools, `ProposalBatch`/`ProposalSpec`/`EditOp`, `validate_batch` + one retry, `build_system_prompt`, `read_skill`, `propose_from_findings`, `FakeRepairProposer`. The only module here that loads a model stack |
| `remote.py` | `apply_via_clone` (clone → `edits.apply_declared` → the cross-check → `run_gates` → gated commit → push) and `apply_approved`, the door that also records the outcome. Owns `commit_message` |
| `store.py` | `repair_proposals` persistence: `insert_proposal`, `pending_proposals`, `recent_decided`, `proposal`, `mark_decided`, `mark_applied`, `mark_failed`, `known_content_keys`. Pure — decides nothing, authorizes nothing |
| `schema.py` | The DDL behind `startup_ddl_lock`, `JOB_NAME`, the kind/status vocabularies, and the op record: `declared_edits`, `target_paths`, `content_key` |
| `settings.py` | `RepairSettings.from_env` — the model and the two bounds. The ONE place this package reads the environment for configuration |
| `errors.py` | `RepairError`, and `ProposalStateError` for "somebody got there first / there is nothing to do" |

**Two doors decide who may approve, and neither is here.** `server/review.py` reaches `store`,
`schema`, `errors` and `remote.apply_approved` (a declared, reasoned import edge); `stigmergy.admin`
reaches `store`, `schema` and `errors` and enters the apply through
`server.review.apply_repair_and_record`, the ONE ordering both doors run. Neither may reach
`proposer.py`, and that is why `remote.py` must not load a model stack: it runs inside the MCP
server process, and `tests/test_architecture.py` pins the separation from both sides — this
package's `test_only_the_proposer_loads_a_model_stack`, and the server's declared symbol list.

## Reuse

- `librarian.edits.validate` / `apply_declared` — the SAME validator both ends run. Propose time
  proves a proposal is storable; apply time proves it still applies to the clone. Neither trusts
  the other: they are asking about two different trees.
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
  recomputed; `subjects` is the LIST, never the display string re-split.
- `kernel.llm.build_processor` — the one fake/real dispatch, `CLEAN_LLM`-driven offline.
- `capture.ops.record_job_run` — one `job_runs` row per propose pass.

## Avoid

- Never give the proposer a write tool, a third tool, or a path into `gitcmd`. Its two tools read,
  and its inability to write is structural, not promised.
- Never extend the op vocabulary past `page.EDIT_KINDS` — a new shape is a new gate question.
- Never apply without the cross-check: `run_gates` would happily pass a well-formed additive diff
  that is not the one a steward approved, and only the stored `target_paths` can say so.
- Never restore `approved` after a failed apply. `failed` + the `error` column is the record; a
  silent revert hides that a gate refused.
- Never compose a refusal from a caught exception's text. Every sentence raised from `remote.py`
  reaches a steward verbatim through the review lane; git names this host's throwaway clone.
- Never read the environment at module scope, and never open a connection outside `cli.py`.

## Contracts

- `repair_proposals`: `id`, `created_at`, `run_id`, `finding_ids`, `kind`, `target_paths`, `ops`,
  `rationale`, `content_key`, `status`, `decided_by`, `decided_at`, `notes`, `applied_commit`,
  `error`, `model_id`. `kind ∈ ('edits',)` in v1; `status ∈ (pending, approved, rejected, applied,
  failed)`.
- **A REJECTED row is the dismissal memory.** `content_key` identifies a proposal by what it would
  DO (kind + sorted `op:path:link`, `note` excluded), and the proposer skips a key with ANY prior
  row. "Reviewed and declined" is a durable fact, and a steward is not asked the same question
  every night. The UNIQUE index is narrower on purpose — pending only — so re-proposing after a
  rejection stays a human decision rather than a constraint violation.
- `PROPOSABLE_CHECKS` = `model-unlinked-mention`, `model-contradiction`, `orphan-page`. The other
  checks are absent by NAME, not by oversight: none of them is answered by a link or a callout.
- `job_runs` job `repair-propose`, `stats`: `findings_seen`, `proposed`, `skipped_known`,
  `skipped_invalid`, `skip_reasons`.
- Bounds: `settings.max_ops_per_proposal` (6) is how much ONE approval may be;
  `MAX_RATIONALE_CHARS` 400, `MAX_NOTE_CHARS` 300, `MAX_PAGE_BODY_CHARS` 12000,
  `MAX_SKILL_BYTES` 256 KiB; `PROPOSER_LIMITS` 6 requests / 24 tool calls.
- The proposer's skill: `.claude/skills/repair-proposer/SKILL.md` in the knowledge repo, read at
  run time and size-capped before the bytes.

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
