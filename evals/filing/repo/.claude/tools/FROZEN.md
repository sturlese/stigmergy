# `stigmergy_lint.py` here is a FROZEN COPY

`stigmergy_lint.py` beside this file is a byte-for-byte copy of the contract linter from the
**knowledge repo** (`stigmergy`), not a second implementation of it.

| | |
|---|---|
| **Source** | `<stigmergy>/.claude/tools/stigmergy_lint.py` |
| **Copied at commit** | `abf6790bf7e6845d0f322645b36e41fa66f9f333` |
| **Drift guard** | none — and that is the point (see below) |

## Why this copy exists, and why it is NOT drift-guarded

`gate_contract` materializes the linter **from the base commit** of the repo it is filing into, so
the mini knowledge repo beside this file has to carry one or the filing eval measures a run with
the contract gate silently inert.

Unlike `tests/librarian/fixtures/repo/.claude/tools/`, this copy is deliberately **not** kept in
sync with the real linter. A golden instrument is a yardstick: `evals/history.ndjson` compares a
score recorded today against one recorded six months ago, and that comparison is only meaningful
if every run was judged by the same rules. Resyncing this file would silently re-grade every
future run against a contract the earlier entries were never held to — the same reasoning
`evals/corpus/PROVENANCE.json` gives for freezing the corpus tree instead of regenerating it.

So: **do not resync this file to make a capture pass.** If the real linter's contract moves far
enough that this fixture stops representing production, that is a decision to retire this golden
set and start a new series — recorded in `evals/README.md`, with the old bars retired beside it —
never a quiet `cp`.

The sha above is what makes that decision possible: without it, "is this fixture behind the real
linter, and by how much?" has no answer.

It names the ONE commit every frozen copy in this fixture matches, which is not always the commit
each copy was physically taken at — these bytes are identical at its predecessor as well, so the
row was aligned to the sha that describes the whole tree truthfully rather than re-copied. A
record-only change: `../../PROVENANCE.json` carries the same sha and says the same thing.
