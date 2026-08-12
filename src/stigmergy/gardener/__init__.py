"""`stigmergy.gardener` — corpus health on demand: eight deterministic checks plus a bounded model
editorial sweep, persisted as findings + a `job_runs` row, printed as a severity-grouped report.

Findings-only, structurally: this package imports no git plumbing and holds no path under `wiki/`
— it fixes nothing, writes nothing to the repo, and only NAMES the command for a fix.

Per-module import edges are pinned by `tests/test_architecture.py`. Two are easy to get wrong:
import `views.staleness`, never `views.regenerate` (which module-level-imports the git write
stack); and the SLA notice posts to the digest's Slack channel, so `run.py` resolves that
channel's audiences and `notice.scope_findings_to_channel` redacts through `server.acl` before
anything posts — the notice reads findings rows, never `pages_index`, so no mechanical ACL guard
sees this path on its own.
"""
