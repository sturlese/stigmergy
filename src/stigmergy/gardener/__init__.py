"""`stigmergy.gardener` — corpus health on demand: the deterministic checks plus a bounded model
editorial sweep, persisted as findings + a `job_runs` row, printed as a severity-grouped report.

Findings-only, structurally: this package imports no git plumbing and holds no path under `wiki/`
— it fixes nothing, writes nothing to the repo, and only NAMES the command for a fix.

Per-module import edges are pinned by `tests/test_architecture.py`, and the one easy to get wrong
is `views`: import `views.staleness`, never `views.regenerate`, which module-level-imports the git
write stack.

Nothing here talks to Slack. The findings this package writes reach a person through
`stigmergy.digest` (which broadcasts, and scopes every page it names) and through the admin
console — never from a gardener process, which holds no caller identity to scope to and no
credential to post with.
"""
