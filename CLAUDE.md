# Stigmergy project doctrine

This repository owns the platform. The knowledge repository is a separate Git checkout configured
with `STIGMERGY_REPO`; it owns current Markdown knowledge and its operational control files.

Read the implementation specification at `specs/karpathy-team-wiki.md` before changing architecture
or product behavior. Code and tests are authoritative when older prose disagrees.

## Invariants

1. Git and Markdown are current knowledge. Postgres is operational state and a rebuildable index.
2. Every adapter produces the same kind-free `CaptureEnvelope` before queueing.
3. Original bytes and readable source pages are immutable during ordinary filing and gardening.
4. One serialized writer owns every Git mutation and records one commit and one change ledger row.
5. ACL policy is shared. A write may never expose narrower evidence to broader readers.
6. The librarian owns notes and concepts. Entity files are minimal machine data owned by entity
   primitives; raw entity files are not ordinary searchable pages.
7. No active write state waits for human approval. Supported uncertainty is represented honestly.
8. Corpus-health findings are preventable or autonomously repairable; they are not human tasks.
9. Product capabilities are available through Slack, MCP, or the backoffice. Installed CLI commands
   are service, bootstrap, security, index-operation, or local-bridge entry points.
10. Comments and docstrings explain only a local non-obvious invariant or mechanism. History and
    design narration belong in Git and the specification.

## Package orientation

- `capture`: normalized envelopes, evidence, uploads, acquisition, extraction, queue, and sources.
- `bridge`: the local MCP client, local files, public URLs, and private Google Drive export.
- `knowledge`: page contracts, filing context, structured plans, writer, linter, repair primitives,
  contradictions, authorization guard, and authorship gate.
- `entities`: opaque identity records, scoped claims, registry derivation, merge, rename, and delete.
- `changes`: exact patches and append-only mutation metadata.
- `index`: canonical corpus selection, lexical/vector ranking, incremental updates, full rebuild,
  and convergence health.
- `server` and `answer`: ACL-scoped MCP tools, HTTP transport, webhook, retrieval, and cited answers.
- `slack`: authenticated Socket Mode transport, identity/channel mapping, canonical snapshots, and
  notifications.
- `admin`: master-only operational API and browser UI.
- `librarian`: long-running worker/bootstrap, Git transport, credentials, schedule, and model setup.
- `ops`: explicitly guarded non-production reset tooling.

## Working rules

- Preserve unrelated work in a dirty tree.
- Use `rg` for discovery and `apply_patch` for edits.
- A defect gets a reproducing test before its production fix.
- Use real Git and Postgres where the behavior depends on them.
- Keep adapters thin; acquisition differences end at `CaptureService`.
- Do not add compatibility branches, migrations, or dual formats for the abandoned test contract.
- Update platform code, knowledge-repository controls, tests, workflows, and documentation together.

For an issue, defect, or feature, follow `.claude/skills/land-a-change/SKILL.md`. After deployment or
a cross-subsystem release, follow `.claude/skills/validate-deployment/SKILL.md`.

## Completion

Run focused tests first, then Ruff, then the complete suite. Cross-repository changes are complete
only when both repositories are committed, pushed, deployed, and the live service, writer, Slack
adapter, index, backoffice, and scheduled reconciliation have evidence of health.
