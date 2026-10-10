# Stigmergy project doctrine

This repository owns the platform. Each tenant's knowledge repository is a separate private Git
repository in the platform's GitHub organization; the writer keeps one checkout per tenant under
`STIGMERGY_REPO`. A repository holds Markdown knowledge only.

Read the implementation specification at `specs/karpathy-team-wiki.md` and the tenancy contract at
`specs/multi-tenancy.md` before changing architecture or product behavior. Active documentation lives in `docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`, `docs/RESET.md`,
`docs/DATA-PROTECTION.md`, and `docs/ERASE.md`. Code and tests are authoritative when older prose disagrees.

## Invariants

1. Git and Markdown are current knowledge. Postgres is operational state and a rebuildable index.
2. Every adapter produces the same kind-free `CaptureEnvelope` before queueing.
3. Original bytes and readable source pages are immutable during ordinary filing and gardening.
4. One serialized writer owns every Git mutation and records one commit and one change ledger row.
   Erasing a layer is the one history rewrite; it remaps the ledger and leaves a receipt.
5. ACL policy is shared. A capture files in exactly one layer and everything derived from it stays
   there; a write may never expose narrower evidence to broader readers. The librarian never sees a
   layer (`knowledge.layered` views), and readers see the union of their layers fused by virtual path.
6. The librarian owns notes, concepts, entity, and project pages. The wikilink is the only relation between
   pages; entity titles and aliases are unique within a layer, and backlinks are derived by the index.
7. No active write state waits for human approval. Supported uncertainty is represented honestly.
8. Corpus-health findings are preventable or autonomously repairable; they are not human tasks.
9. Product capabilities are available through Slack, MCP, or the backoffice. Installed CLI commands
   are service, bootstrap, security, index-operation, or local-bridge entry points.
10. Every model-backed path is built by `kernel.llm` from its closed allowlist through the single
    OpenRouter boundary (spec §4.1): chat models through `build_model`, the typed-decision
    classifier (capture layers, filing-time aliases) through `choose`. Same-model provider failover
    is allowed; model fallback and direct Anthropic, OpenAI, or Gemini credentials are not. No new
    model without updating the allowlist and the specification together.
11. Comments and docstrings explain only a local non-obvious invariant or mechanism. History and
    design narration belong in Git and the specification.
12. Nothing crosses tenants. Every tenant-owned table carries `tenant_id` under the forced
    row-level security policy of `kernel.tenancy`, the only code that binds a connection to a
    tenant or to platform scope; platform scope serves credential lookup and sign-out, the writer's
    claim and schedule, evidence liveness, upload, audit, and access-request expiry, the index's
    dimension check, workspace deletion, a refiled workspace's import and handover (the one sanctioned copy between workspaces), and the master console only. The service's role cannot bypass row-level
    security. Every entry point names its tenant before reading anything else, and a writer job
    touches exactly one tenant.

## Package orientation

- `kernel`: shared foundation — tenancy (connection binding and row-level policy), ACL information
  flow, layer paths and page fusion, deadlines, approved model construction, normalization, result
  types. Look here before
  adding a cross-package helper.
- `capture`: normalized envelopes, evidence, uploads, acquisition, extraction, OCR, queue, and
  sources.
- `bridge`: the local MCP client, local files, public URLs, and private Google Drive export.
- `knowledge`: page contracts, filing context, structured plans, writer, linter, repair primitives,
  contradictions, authorization guard, authorship gate, and the live librarian skill
  (`librarian_skill.md`).
- `entities`: administrator operations over entity pages (merge, rename) and the shared name index.
- `changes`: exact patches and append-only mutation metadata.
- `index`: canonical corpus selection, lexical/vector ranking, incremental updates, full rebuild,
  and convergence health.
- `server` and `answer`: ACL-scoped MCP tools, HTTP transport, webhook, retrieval, cited answers,
  tenants and platform operators (`server/platform.py`), and the per-tenant usage ledger.
- `slack`: authenticated Socket Mode transport, identity/channel mapping, canonical snapshots, and
  notifications.
- `admin`: backoffice API, sign-in, roster administration, the scoped wiki service, and the master
  console (`admin/console.py`); the console UI is the React app in `web/`, built into the package
  with `make web`.
- `librarian`: long-running worker/bootstrap, per-tenant checkouts (`workspaces.py`), new tenants'
  repositories (`provision.py`), Git transport, credentials, schedule, and model setup.
- `ops`: explicitly guarded non-production reset tooling, layer erasure (requests and the writer
  job), whole-workspace deletion (`erase_workspace.py`), and refiling a workspace into a fresh one
  (`replay_workspace.py`).
- `evals/` (outside the package): frozen canonical corpus, golden retrieval/QA sets, and measured
  release gates.

## Working rules

- Preserve unrelated work in a dirty tree.
- Use `rg` for discovery. Make surgical edits; never rewrite a file to change a few lines.
- A defect gets a reproducing test before its production fix.
- Use real Git and Postgres where the behavior depends on them.
- Keep adapters thin; acquisition differences end at `CaptureService`.
- This deployment is a clean cut (spec §2): no migrations, compatibility branches, or dual formats
  for the replaced implementation.
- Update platform code, knowledge-repository controls, tests, workflows, documentation, and the
  eval corpus together. `knowledge/librarian_skill.md` is the single canonical librarian skill and
  ships inside the platform package.
- People, layers, tokens, and Slack channel layers live in Postgres per tenant (`server/roster.py`)
  and are managed in the backoffice. The layer contract is `specs/audience-layers.md`. Nothing reads
  a roster file at runtime; `stigmergy-admin import` loads a legacy file roster into a workspace
  once. `stigmergy-admin operator bootstrap` creates the first platform operator, who creates
  workspaces in the master console.

For an issue, defect, or feature, follow `.claude/skills/land-a-change/SKILL.md`. After deployment or
a cross-subsystem release, follow `.claude/skills/validate-deployment/SKILL.md`.

## Testing

- The suite is keyless and forces fake model backends; `tests/conftest.py` strips model credentials.
  Never use real credentials or paid models in tests.
- `make lint` runs Ruff over `src`, `tests`, `evals`, and `scripts`.
- `make test` runs the complete keyless suite with a `--cov-fail-under=75` gate.
- `make test-system` runs the real Postgres and Git acceptance paths.
- The suite claims the `stigmergy_test` database (or a `stigmergy_test_<n>` lane) for the whole run:
  one suite per lane at a time, never in parallel. It connects as `stigmergy_app`, a role that
  cannot bypass row-level security, and acts for the test tenant unless a test binds another.
- CI also builds the deployment image and runs a pinned `gitleaks` scan.

## Completion

Run focused tests first, then `make lint`, then `make test`. A change is complete when it is
committed, pushed, and green in CI; for a deployment you operate, when it is also deployed and the
live service, writer, Slack adapter (where enabled), index, backoffice, and scheduled reconciliation
have evidence of health.
