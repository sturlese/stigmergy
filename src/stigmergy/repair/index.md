# repair — a person's page removal: what goes, what has to be rewritten, and the record of it

Sibling that reads the corpus and fixes nothing: [`gardener`](../gardener/index.md). The flow that
performs a removal: [`librarian`](../librarian/index.md).

**A human decides; code performs.** Somebody names pages at `brain_delete` or on the console's
Remove pages button, and this package answers the questions code owns: which of those paths are
pages this lane may delete at all, which pages in the corpus refer to them, and exactly what bytes
every one of those pages must end up carrying so the reference does not survive as a dead link. The
one thing code cannot do is write the prose — a sentence that cited a removed page still has to
read — so `sweep.py` asks a model for the bodies, and `deletion.py` refuses anything it wrote that
touched more than the body, byte for byte.

Nothing here opens a connection or commits. `librarian.processing.process_delete_item` owns the
worktree, the gates, the commit and the push; this package is handed a checkout and hands back a
plan.

> **What used to be here.** An ELECTIVE loop turned gardener findings into proposed repairs —
> `edits` (three additive shapes), `entity-body` (a drafted entity page body), `entity-alias` (a
> merge of two registry entries) — derived by a model overnight and applied without anybody being
> asked. Measured against `docs/DESIGN.md` §2 it had applied five repairs in three weeks of daily
> use, its detectors went with the gardener's model passes, and a capture brings a page up to date
> directly now. It was removed, along with the four modules that were only its: the pass and its
> three proposer roads, the entity-body writer, the merge, and the governed apply door.
> `schema.RETIRED_KINDS` is the half of it a deployed database still holds.

## Modules

| Module | What it is |
|---|---|
| `deletion.py` | The CODE half — `plan` (the sweep, a pure function of a worktree's bytes: the pages that go, then one scrub op per referring page with its frontmatter already scrubbed, its body verbatim and its base hash recorded), `scrubbed` (one page's planned bytes), `validate`/`apply_declared` (the shape and the two written-sweep bounds, then the base hashes and a corpus walk for a latecomer, then perform), the readers every other surface goes through (`deleted_paths`, `scrubbed_paths`, `expected_bytes`, `provenance_scrubs`, `lane_for`), and the bytes-level primitives the rest of the package reaches through it rather than re-derive: `page_refusal` (the ONE confinement predicate), `read_text`, `sha256`, `page_stem`, `link_stem`, `corpus_pages`, `plan_bytes`, `oversize_reason`. Its link scanner is hand-mirrored from the frozen contract linter and must stay that way |
| `sweep.py` | The WRITER: `SweepDraft`/`PageBody`, the frame over the knowledge repo's own skill (`build_sweep_system_prompt`), `build_sweep_writer`, the prompt (`build_sweep_prompt` — index, marker, fenced halves), `validate_draft` (the set bound, then the per-body bounds — title kept, no `---`, never emptied, `MAX_BODY_GROWTH_BYTES` above and `MAX_UNREFERENCING_LINES_DROPPED` below, the second because the first is one-sided and admits a body cut down to its title — then `deletion.validate` over the composed bytes), `split_head`/`compose`, `write`/`write_sync`, and `FakeSweepWriter`. The ONE module here that loads a model stack |
| `brief.py` | The writer's brief as a FILE: `SKILL_RELPATH`, `read_skill` (symlink-refused, size-capped before the bytes), `with_skill`, and the two prompt constants (`DETAILS_MARKER`, `PAGE_LINE`). Loads no model stack and no store, so the frame and the filesystem seam can be read without one |
| `store.py` | `repairs` persistence: `record_applied` (the one write — a removal that LANDED), `recent`, `repair`, `counts_by_status` (the whole-table histogram a surface may draw a part-to-whole from). Pure — decides nothing, authorizes nothing, and never narrows a read to `kind = 'delete'`: the retired kinds' rows are what the table was kept for |
| `schema.py` | The DDL behind `startup_ddl_lock` (including the rename from `repair_proposals`, the migration of its three retired statuses, and the ordering that rename made load-bearing), the vocabularies, and the op record: `DELETE_OP_NAMES`, `DELETE_OP_FIELDS`/`SCRUB_OP_FIELDS`, `target_paths` |
| `settings.py` | `RepairSettings.from_env` — the sweep writer's model and the bound on one plan's size. The ONE place this package reads the environment |
| `errors.py` | `RepairError` — the one class, and every sentence raised in it is written to be published to whoever asked for the removal |

**Who enters this package, and from where.** `librarian.processing.process_delete_item` reaches
`deletion`, `sweep`, `brief`, `errors` and `settings` — the whole removal flow. `server/review.py`
reaches `schema` and nothing else, for the ledger's DDL at startup. `admin/service.py` reaches
`store` and `schema`, read-only. Every edge is pinned in `tests/test_architecture.py`.

## The two vocabularies, and why one is wider than the other

`WRITABLE_KIND` is what this version inserts (`delete`). `KINDS` is what a ROW MAY CARRY — that
plus `RETIRED_KINDS`. The difference is not cosmetic: `ALTER TABLE … ADD CONSTRAINT … CHECK`
validates the rows already in the table, so narrowing the CHECK to what code writes would abort the
whole startup DDL sequence on every deployed database that holds an elective repair's row. The same
argument runs for `STATUSES`, where only `applied` is still written.

A removal carries **no `content_key`**. The column and its partial unique index belong to the
elective loop, whose whole problem was deriving the same repair twice; a person decides each
removal, nothing re-derives one, and two removals of the same pages must both be recorded.

## Rules for a change here

- **Never let a model choose a page to delete.** The paths come from a person, through the queue
  row's hints, and `deletion.validate` judges every one of them again in the tree the commit is
  made in. A road where a model names a path is a different system.
- **Structure is code's; prose is a model's.** Frontmatter, the referring set and every bound are
  `deletion.py`'s; only a body is `sweep.py`'s, and `validate_draft` proves the difference on the
  bytes rather than trusting the brief.
- **Ask the linter's question the linter's way.** The link regexes here are hand-mirrored from the
  frozen contract linter because this package talks to it through FILES. A scanner that sees fewer
  links than the linter leaves a dead link and a veto at apply time.
- **Keep `sweep.py` the only model stack.** The console imports `store` and `schema`, and the MCP
  server imports `schema`; `pydantic_ai` arriving there through a DDL module would be an
  import-graph accident that makes both processes heavier for nothing.
- **A refusal is published.** Every sentence raised here reaches the person who asked, so it may
  name repo-relative paths and gate codes and must never name this host's worktree, an absolute
  path, or a caught exception's own words.
