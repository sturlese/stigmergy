"""`stigmergy.gardener` — corpus health on demand: the deterministic checks, persisted as
findings + a `job_runs` row, printed as a severity-grouped report.

Findings-only, structurally: this package imports no git plumbing and holds no path under `wiki/`
— it fixes nothing, writes nothing to the repo, and only NAMES the command for a fix.

Every check is deterministic. This package asks no model, holds no model budget and reads no
model name: a run either completes or fails, and "the corpus was half looked at" is not one of
its outcomes.

Per-module import edges are pinned by `tests/test_architecture.py`. This package holds no git
plumbing at all: it reads the corpus through `index.corpus`, the same parser the index build runs,
and commits nothing.

Nothing here talks to Slack. The findings this package writes reach a person through the admin
console — never from a gardener process, which holds no caller identity to scope to and no
credential to post with.
"""
