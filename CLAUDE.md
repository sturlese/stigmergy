# Stigmergy project doctrine

This repository owns the platform. The knowledge repository is a separate Git checkout configured
with `STIGMERGY_REPO`; it owns current Markdown knowledge and its operational control files.

Read the implementation specification at `specs/karpathy-team-wiki.md` before changing architecture
or product behavior. Active documentation lives in `docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`, and
`docs/RESET.md`. Code and tests are authoritative when older prose disagrees.

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
10. Every model-backed path is built by `kernel.llm` from its closed allowlist through the single
    OpenRouter boundary (spec §4.1). Same-model provider failover is allowed; model fallback and
    direct Anthropic, OpenAI, or Gemini credentials are not. No new model without updating the
    allowlist and the specification together.
11. Comments and docstrings explain only a local non-obvious invariant or mechanism. History and
    design narration belong in Git and the specification.

## Package orientation

- `kernel`: shared foundation — ACL information flow, deadlines, approved model construction,
  normalization, result types, and usage repair. Look here before adding a cross-package helper.
- `capture`: normalized envelopes, evidence, uploads, acquisition, extraction, OCR, queue, and
  sources.
- `bridge`: the local MCP client, local files, public URLs, and private Google Drive export.
- `knowledge`: page contracts, filing context, structured plans, writer, linter, repair primitives,
  contradictions, authorization guard, authorship gate, and the live librarian skill
  (`librarian_skill.md`).
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
- `evals/` (outside the package): frozen canonical corpus, golden retrieval/QA sets, measured
  release gates, and the evaluation copy of the librarian skill under `evals/filing/`.

## Working rules

- Preserve unrelated work in a dirty tree.
- Use `rg` for discovery. Make surgical edits; never rewrite a file to change a few lines.
- A defect gets a reproducing test before its production fix.
- Use real Git and Postgres where the behavior depends on them.
- Keep adapters thin; acquisition differences end at `CaptureService`.
- This deployment is a clean cut (spec §2): no migrations, compatibility branches, or dual formats
  for the replaced implementation.
- Update platform code, knowledge-repository controls, tests, workflows, documentation, and the
  eval corpus together. `knowledge/librarian_skill.md` is the canonical librarian skill; its copies
  under `tests/librarian/fixtures/` and `evals/filing/` must match byte for byte
  (`tests/knowledge/test_librarian_contract.py`).
- `deploy/identities.json`, `deploy/entity-registry.json`, and `deploy/slack-channels.json` are
  empty safe defaults. `scripts/deploy_staging.sh` overwrites them with the knowledge repository's
  real `ops/` files before a deploy; never commit those baked versions.

For an issue, defect, or feature, follow `.claude/skills/land-a-change/SKILL.md`. After deployment or
a cross-subsystem release, follow `.claude/skills/validate-deployment/SKILL.md`.

## Testing

- The suite is keyless and forces fake model backends; `tests/conftest.py` strips model credentials.
  Never use real credentials or paid models in tests.
- `make lint` runs Ruff over `src`, `tests`, `evals`, and `scripts`.
- `make test` runs the complete keyless suite with a `--cov-fail-under=75` gate.
- `make test-system` runs the real Postgres and Git acceptance paths.
- The suite claims the `stigmergy_test` database (or a `stigmergy_test_<n>` lane) for the whole run:
  one suite per lane at a time, never in parallel.
- CI also builds the deployment image and runs a pinned `gitleaks` scan.

## Completion

Run focused tests first, then `make lint`, then `make test`. Cross-repository changes are complete
only when both repositories are committed, pushed, deployed, and the live service, writer, Slack
adapter, index, backoffice, and scheduled reconciliation have evidence of health.
