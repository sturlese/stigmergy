# Clean non-production reset

The reset is intentionally destructive and supports only `test` and `staging`. It removes every
table in the connected database's current schema, every object in the explicitly named evidence
bucket, and current `wiki/`/`sources/` content in the explicitly named knowledge checkout. It then
creates only the current schema and an empty target repository scaffold while preserving current
identity, Slack-channel, template, workflow, and librarian controls.

First derive the exact confirmation value:

```text
<environment>:<database>:<bucket>:<absolute-repository-path>
```

Then run from a checkout with the target evidence credentials exported:

```bash
python -m stigmergy.ops.reset \
  --environment staging \
  --dsn "$STAGING_DSN" \
  --database "$STAGING_DATABASE" \
  --bucket "$STIGMERGY_EVIDENCE_BUCKET" \
  --repo "$STIGMERGY_REPO" \
  --embedding-dim 3072 \
  --embedding-model text-embedding-3-large \
  --confirm "staging:$STAGING_DATABASE:$STIGMERGY_EVIDENCE_BUCKET:$STIGMERGY_REPO"
```

The command refuses a production environment, an imprecise repository path, a database other than
the one named, an evidence store whose bucket differs from `--bucket`, or any confirmation mismatch.
Commit and push the resulting knowledge scaffold with the trusted writer identity, then run a full
index rebuild and the deployment validation sequence in `OPERATIONS.md`.
