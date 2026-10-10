# Clean non-production reset

The reset (`stigmergy.ops.reset`) is intentionally destructive and supports only `test` and
`staging`. It touches three things:

- Postgres: it drops every table in the connected database's current schema except the platform
  tables (`tenants`, `platform_operators`, `platform_tokens`, `platform_sessions`,
  `platform_actions`, `access_requests`, `workspace_erasures`) and the roster tables
  (`roster_groups`, `roster_principals`, `roster_tokens`, `roster_sessions`,
  `roster_slack_channels`), then creates the current schema. Every workspace keeps its row, people,
  layers, tokens, and sessions; its queue, change ledger, index, uploads, and audit rows are gone.
- Evidence: it deletes every object in the bucket named by `--bucket`.
- One knowledge checkout, named by `--repo`: it removes `wiki/`, `sources/`, and `layers/`, writes
  the empty scaffold (`wiki/notes`, `wiki/concepts`, `wiki/entities`, `wiki/projects`, `sources`),
  and keeps the checkout's `README.md`, `CLAUDE.md`, and `.gitignore` (or copies them from
  `--control-source`).

It commits and pushes nothing, and never touches a workspace's GitHub repository or any other
checkout. Name one workspace's checkout, not the deployed `STIGMERGY_REPO`, which is the root of
the writer's checkouts, one per workspace id.

First derive the exact confirmation value:

```text
<environment>:<database>:<bucket>:<absolute-repository-path>
```

The repository path is resolved to its physical absolute path. Then run from a platform checkout
with the target's `STIGMERGY_EVIDENCE_*` variables exported, where `CHECKOUT` is the absolute path
of the workspace's knowledge checkout:

```bash
python -m stigmergy.ops.reset \
  --environment staging \
  --dsn "$STAGING_DSN" \
  --database "$STAGING_DATABASE" \
  --bucket "$STIGMERGY_EVIDENCE_BUCKET" \
  --repo "$CHECKOUT" \
  --embedding-dim 2560 \
  --embedding-model openai/text-embedding-3-large \
  --confirm "staging:$STAGING_DATABASE:$STIGMERGY_EVIDENCE_BUCKET:$CHECKOUT"
```

The command refuses a production environment, a repository path too close to the filesystem root,
a database other than the one named, an evidence store whose bucket differs from `--bucket`, or any
confirmation mismatch. Commit and push the resulting knowledge scaffold with the trusted writer
identity. The reset leaves every workspace's index unbuilt, so the writer rebuilds each one from its
repository on its next pass; a workspace whose repository was not emptied comes back with its old
pages. Then run the deployment validation sequence in `OPERATIONS.md`.
