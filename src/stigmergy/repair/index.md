# repair — the repair loop: a finding's path to zero, unattended

Sibling that produces the findings and fixes nothing: [`gardener`](../gardener/index.md).

**The covenant, in one sentence: a MODEL declares, CODE validates twice, code applies exactly what
it validated, and the diff is stored because nobody read it first**
([ADR 044](../../../docs/decisions/044-the-capture-is-the-approval.md)). Nothing here can write to
the knowledge repo without having passed all three, and there is no fourth step where somebody
says yes.

What stands where the approval stood: the ledger's permanent `content_key` memory (an applied or
refused repair is never derived again), the two ceilings in `settings.py` (how many repairs one
pass may land, and a tighter number for merges), and the nine gates, which judge a diff whatever
produced it.

FOUR repair kinds, and a finding rides exactly one road. `edits` is the librarian's own
declared-edit vocabulary and nothing else — `backlink`, `overlap`, `contradiction` — three strictly
additive shapes the nine gates already know how to judge. `entity-body` is the one kind that
REPLACES text: one drafted body for one entity page whose body does not say what the corpus knows
about the entity — still the template it was minted with, or written and empty of anything specific
— judged by `gate_body_rewrite`'s permitted-rewrite branch instead of its additive proof (ADR 039's
first amendment). `delete` is the one kind that REMOVES anything, and the one the covenant reads differently for
twice. **No model may declare it in any spelling**: a person acts at `brain_delete` (MCP, or the
console), or code derives it for exact-duplicate `sources/` pages, where the decision is a lookup
rather than a judgment (ADR 039's second amendment). And **a person's own deletion is decided by
the call that asked for it** — authorization runs in the act, the row is born `approved` and
applied in the same pass, and the diff goes back to them, because the reading of a written sweep
moves after the push (ADR 043). Its pages are WRITTEN: `deletion` owns the frontmatter, the bounds, and the machine zones whole — a view is regenerated and a source page is provenance, so both are unlinked deterministically rather than argued with; `sweep` owns the bodies of the pages a person wrote (`written_paths`). `entity-alias` is the one
kind that answers a finding about a PAIR: two registry entries that are the same entity, where the
model picks which name SURVIVES and says why, and code computes everything that follows — the
spellings that move, the pages that re-anchor, the supersession, the regenerated registry (ADR
039's third amendment). It is also the only kind that puts a file which is not a page into a
governed commit, and the only one that reaches `stigmergy.entities`.

The agent's judgment (which finding is worth repairing, which shape fits, what an entity page
should say, when a finding has gone stale and deserves nothing) lives in a skill in the KNOWLEDGE
repo, read at the base commit the pass runs against; a missing skill is a named refusal, never a
default.

## Modules

| Module | What it is |
|---|---|
| `run.py` | The pass itself, and the agent seam, ALL THREE model roads (`EDIT_PROPOSABLE_CHECKS`, `BODY_PROPOSABLE_CHECKS` and `ALIAS_PROPOSABLE_CHECKS` decide which one a finding rides; the deterministic duplicate-sources road asks no model at all): `ProposerContext` and its two READ tools; `ProposalBatch`/`ProposalSpec`/`EditOp` + `validate_batch` for the additive road; `EntityBodyDraft` + `anchored_pages`/`draft_entity_body`/`validate_draft` for the body road; `EntityMergeChoice` + `build_entity_alias_prompt`/`choose_survivor`/`validate_merge_choice` for the merge road; one retry each (every `agent.run` records its spend against its limits into `ProposeResult.model_calls`, persisted in `job_runs.stats` — the budgets' feedback loop, issue #81) — but never for a PARK, which both the body road (an empty `body_markdown`, `BODY_DECLINED_REASON`) and the merge road (an empty survivor, `MERGE_DECLINED_REASON`) treat as the answer their brief asked for rather than as an error to push the model off; `read_skill`; `run_repairs` and `RepairRunResult` — derive, then apply each repair in its own worktree at a base fetched for it, recording every outcome; and the three offline doubles. One of the two modules here that loads a model stack |
| `entity_body.py` | The `entity-body` writer and its validator — `validate`, `apply_declared`, `rewritten`, and the bounds a draft lives inside. Pure of the model stack: the apply runs it, and the apply must stay light enough for the MCP server process to enter on the deletion road |
| `entity_alias.py` | The `entity-alias` kind, whole: `plan` (the merge, a pure function of a worktree's bytes), the three page writers (`aliased`, `retired`, `reanchored`), `validate`/`apply_declared` (recompute, byte-compare, write the pages, regenerate, byte-compare the registry), the readers every other surface goes through (`survivor_path`, `absorbed_path`, `reanchored_paths`, `expected_bytes`, `derived_files`, `lane_for`), and `claimable_aliases` — the one place the contract linter's alias rule is enforced at PLAN time. The ONE module here that imports `stigmergy.entities`, and only its generator: the registry has exactly one writer |
| `sweep.py` | The `delete` kind's WRITER (ADR 043 D1): `SweepDraft`/`PageBody`, the fourth frame over the same skill (`build_sweep_system_prompt`), `build_sweep_writer`, the prompt (`build_sweep_prompt` — the same index/marker/fence shape every road here uses), `validate_draft` (the set bound, then the per-body bounds — title kept, no `---`, never emptied, `MAX_BODY_GROWTH_BYTES` above and `MAX_UNREFERENCING_LINES_DROPPED` below, the second because the first is one-sided and admits a body cut down to its title — then `deletion.validate` over the composed bytes), `split_head`/`compose`, `write`/`write_sync`, and `FakeSweepWriter`. The SECOND module here that loads a model stack, and the one the server enters — the applier is handed a finished plan |
| `brief.py` | The proposer's brief as a FILE: `SKILL_RELPATH`, `read_skill` (symlink-refused, size-capped before the bytes), `with_skill`, and the two prompt constants every road shares (`DETAILS_MARKER`, `PAGE_LINE`). Loads no model stack and no store, so the server's road to `sweep` does not drag the proposer's orchestration in |
| `deletion.py` | The `delete` kind's CODE half — `plan` (the sweep, a pure function of a worktree's bytes), `scrubbed` (one page's planned bytes), `validate`/`apply_declared` (recompute, byte-compare, perform), the readers every other surface goes through (`deleted_paths`, `scrubbed_paths`, `expected_bytes`, `lane_for`), and `duplicate_source_groups` (the one automatic road, which asks no model) — AND the package's shared bytes-level primitives, which the two other non-additive kinds reach through it rather than re-derive: `page_refusal` (the ONE confinement predicate), `read_text`, `sha256`, `page_stem`, `corpus_pages`, `plan_bytes`, `oversize_reason`, `PROVENANCE_ZONE_PREFIXES`. Its link scanner is hand-mirrored from the frozen contract linter and must stay that way |
| `apply.py` | `apply_in_tree` (perform → the per-kind cross-check → `run_gates` → gated commit → push; the two non-additive kinds push with `rebase=False`, so a lost race fails clean instead of replaying a diff onto a base the gates never judged) and `apply_and_record`, the door that writes the ledger row either way. `apply_via_clone`/`cloned` are the second road, for the deletion a person performs from a process holding no checkout. Owns `commit_message` (`Repair:` for a derived repair, `Approved-by:` for a person's deletion), `LEDGER_RESULT_KEYS`, the delete kind's whole-tree dead-link check, and `_lane_and_permission` — the caller-scoped facts the gates are told |
| `store.py` | `repairs` persistence: `record_applied`/`record_failed`/`record_skipped` (one write per outcome, never a transition), `recent`, `repair`, `counts_by_status` (the whole-table histogram a surface may draw a part-to-whole from), `known_content_keys` and `answered_findings` — the two halves of the memory. Pure — decides nothing, authorizes nothing |
| `schema.py` | The DDL behind `startup_ddl_lock` (including the rename from `repair_proposals` and the migration of its three retired statuses), `JOB_NAME`, the kind/status/op-name vocabularies, and the op record: `declared_edits`, `target_paths`, `content_key`, `page_set_key` |
| `settings.py` | `RepairSettings.from_env` — the model and the bounds, including the two ceilings on one pass. The ONE place this package reads the environment for configuration |
| `errors.py` | `RepairError`; `CorpusMovedError` for a refusal that is about the TREE rather than about the repair (recorded, never remembered — see the memory below); `ProposalStateError` for "there is nothing here to do" |

**Who enters this package, and from where.** `librarian.worker`'s idle pass calls `run.run_repairs`
— that is the whole loop. `server/review.py` reaches `apply`, `schema`, `deletion`, `sweep`,
`brief` and `settings` for the ONE repair a person performs (a deletion), and `stigmergy.admin`
reaches `store` and `schema` to READ the ledger. Neither may reach `run.py`, and that is why
`apply.py` must not load a model stack: it is entered from the MCP server process.
`tests/test_architecture.py` pins the separation from both sides — this package's
`test_only_the_proposer_loads_a_model_stack`, and the server's declared symbol list.

## Reuse

- `deletion.page_refusal` / `read_text` / `sha256` / `corpus_pages` / `plan_bytes` — the
  intra-package seam all three non-additive kinds share, one implementation each so the kinds
  cannot disagree about which files are pages, whether the corpus moved under a repair, or what a
  plan weighs. A fourth kind reaches these through `deletion`, never copies them.
- `librarian.edits.validate` / `apply_declared` — the SAME validator both ends run for the `edits`
  kind; `entity_body` and `deletion` each own their kind's twin. Derive time proves the repair is
  worth trying; apply time proves it still applies to the tree it is about to be committed from. Neither trusts the other: they are
  asking about two different trees. `deletion.validate` is the pair for a written sweep — the same
  two bounds at both ends — and `apply_declared` adds what a recomputation used to cover: a base
  hash per rewritten page, and a walk for a page the plan never named that now refers to a going
  one (ADR 043 D3).
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
- `librarian.gates.run_gates(ALL_GATES)` — a repair goes through the librarian's own nine gates,
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
  on the ledger row as `finding_subjects`, which is what lets the pre-model skip recognise the same
  question under a new finding id.
- `kernel.llm.build_processor` — the one fake/real dispatch, `CLEAN_LLM`-driven offline.
- `capture.ops.record_job_run` — one `job_runs` row per repair pass, and `latest_run` is the
  watermark the worker asks before starting another.

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
- Never let a MODEL decide WHICH pages a deletion removes. `validate_batch` refuses an op naming a
  deletion in any spelling, by name, and the duplicate road picks its copy by lookup. Judging that a
  page is stale is a person's decision. What a model DOES write is the pages that STAY (`sweep`) —
  the two are not the same question and the split is ADR 043's whole subject.
- Never give the sweep writer a fallback. A body it could not reconcile refuses the deletion; the
  old bracket scrubber is gone and must not come back as a floor, or the failures travel that road
  and nobody sees them.
- Never widen `GateContext.body_rewrite_allowed`, `deletions_allowed`, `expected_bytes`,
  `derived_files` or `provenance_pages` past what the repair's own ops name, and never set one from
  anywhere but `apply.py`
  (pinned in `tests/test_architecture.py`, both directions, plus a classification check over every
  keyword any module passes to a `GateContext`). A permission wide enough for a second page is a
  permission for a page nothing validated.
- Never let `deletion`'s link scanner drift from the frozen contract linter's. It is hand-mirrored
  on purpose (this package talks to that linter through FILES): one that sees FEWER links leaves a
  dead link and a veto at apply time. It deliberately sees ONE shape more — a markdown link at a
  going page's path — because a writer reconciles prose and a path in prose is a reference whether
  or not the linter counts it; that surplus is named here rather than discovered as drift. `index.corpus.link_targets` answers a deliberately DIFFERENT question — the index's edge
  graph — and is the wrong one to copy.
- Never apply without the cross-check: `run_gates` would happily pass a well-formed additive diff
  that is not the one this repair declared, and only its own `target_paths` can say so. Its SHAPE
  half is per kind — a sweep that quietly stopped deleting satisfies the path comparison exactly.
- Never let a failed apply go unrecorded, and never let a `failed` row be quietly retried. The row
  plus its `error` IS the record, and its key is remembered: a finding whose only expressible
  answer the gates refuse stops being answered, and the `error` column is the only place anybody
  finds out why.
- Never apply without storing the DIFF. Nobody read the change beforehand, so a row with no diff is
  a change nobody will ever read (ADR 044).
- Never compose a refusal from a caught exception's text. Every sentence raised from `apply.py` is
  stored and published verbatim; git names this host's worktree.
- Never read the environment outside `settings.py`, and never open a connection anywhere: every
  caller hands this package a `conn`.

## Contracts

- `repairs`: `id`, `created_at`, `run_id`, `finding_ids`, `finding_subjects`, `kind`,
  `target_paths`, `ops`, `rationale`, `content_key`, `status`, `applied_commit`, `diff`, `reason`,
  `error`, `model_id`. `kind ∈ ('edits', 'entity-body', 'delete', 'entity-alias')`;
  `status ∈ (applied, failed, skipped)`. Both CHECKs are swapped by guarded `DO` blocks, not
  carried by `CREATE TABLE IF NOT EXISTS` alone — a table that already exists never gains a value,
  and a value the code writes and the column refuses is an IntegrityError in production at night.
  The table is `repair_proposals` RENAMED, so a deployment keeps every repair it ever landed, and
  the three retired statuses are migrated to `skipped` with a reason saying so.
  `finding_subjects` is a list of LISTS — one sorted page set per finding answered, what each one
  NAMED as against what the answer edited.
- **The memory is permanent and it is the whole of what stops the loop repeating itself.**
  `content_key` identifies a repair by what it DOES (kind + sorted `op:path:link`, `note` excluded
  — and for `delete`, the removals ALONE, since which pages must also be scrubbed is a fact about
  the rest of the corpus rather than about the question), and the derivation skips any key the
  table holds. `applied` is obvious — it is also what makes a repair somebody REVERTED in git stay
  reverted. `failed` is the deliberate one: a repair the gates refused is not retried, so the loop
  does not spend a model call a night on a question it cannot answer. `skipped` rows carry no key
  and are remembered by nothing — as is a `failed` row whose refusal was a `CorpusMovedError`,
  because a race is not an answer. The UNIQUE index is partial for exactly that reason
  (`content_key <> ''`), and it is also the race: two workers deriving the same repair, one loses.
- `EDIT_PROPOSABLE_CHECKS` = `model-unlinked-mention`, `model-contradiction`, `orphan-page`;
  `BODY_PROPOSABLE_CHECKS` = `entity-placeholder-body`, `model-empty-entity-body`;
  `ALIAS_PROPOSABLE_CHECKS` = `model-duplicate-entity`; `PROPOSABLE_CHECKS` is their union and the
  three sets are disjoint. The two body checks are the deterministic and the judged
  halves of ONE question — the page's body does not say what the corpus knows about the entity —
  so they share a road rather than each getting one, and the gardener guarantees they never name
  the same page in one run. The other checks are absent by NAME, not by oversight: none of them is
  answered by a link, a callout or a body. `delete` answers no check at all — it has no finding behind it, which is why its rows
  carry an empty `finding_ids` and say what their question WAS in `finding_subjects` instead.
- **A `delete` row's `model_id` says whether anything was WRITTEN**: the model that wrote the
  bodies of the pages that stay, or empty when nothing referred to the removed ones. It never means
  a model chose the deletion — that kind is the only one for which no model ever does.
- **A `delete` row a person made carries their name in the commit, not in the ledger**: the row is
  `applied` like any other, and what says a human decided it is the `Approved-by:` trailer
  `commit_message` writes when `actor` is set (ADR 043 D2). A worker-derived repair carries
  `Repair: <check> #<finding>` instead — the commit log never claims a decision nobody made.
- `job_runs` job `repair`, `stats`: `findings_seen`, `applied`, `failed`, `skipped_known`,
  `skipped_invalid`, `skip_reasons`, `failures`, `model_calls`.
- Bounds: `settings.max_ops_per_proposal` (6) is how much ONE repair may be;
  `settings.max_repairs_per_run` (20) is how many one PASS may land, and
  `settings.max_merges_per_run` (3) is the tighter ceiling on the kind that retires an identity — a batch over
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
  ops carry whole pages; `sweep.MAX_BODY_GROWTH_BYTES` (512), how much a reconciled body may GROW
  before it is a body the writer wrote INTO, and `MAX_UNREFERENCING_LINES_DROPPED` (3), how many
  lines that referred to nothing may vanish before it is a page cut down rather than reconciled —
  a callout's second line and a list item's continuation are the seam a real reconciliation closes;
  and `review.MAX_DELETED_PAGES` (10), which is not a technical bound but a statement of what ONE
  `brain_delete` call means; the merge road SHARES
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
- The agent's skill: `.claude/skills/repair-proposer/SKILL.md` in the knowledge repo, read at the
  pass's base commit, refused if the leaf is a symlink, and size-capped before the bytes. THREE
  frames read it — `build_system_prompt`, `build_entity_body_system_prompt`, `build_entity_alias_system_prompt`
  — one procedure, three questions.

- Nothing decides a repair (ADR 044). The console's Repairs page READS the ledger — every outcome,
  each applied row with the diff that landed — and its one write is Remove pages, which is the
  deletion a person performs and is a repair like any other in everything but its trailer.

Tests live in `tests/repair/` (real git, real Postgres, real gates, the offline double for the
agent; `test_deletion.py` is the sweep's plan computation as pure functions, with no database and
no git at all), for the worker's pass in `tests/librarian/`, and for the console's read in
`tests/admin/`; the layering, the module-scope, the connection-seam, the closed apply-caller pins
and the granting-surface pins in `tests/test_architecture.py`. Narrative:
[`docs/reference/repair.md`](../../../docs/reference/repair.md), decisions:
[ADR 039](../../../docs/decisions/039-governed-repair-loop.md).
