# Operations

## Deployed services

The Fly application uses one image and separate `app`, `worker`, and `slack` process groups.

- `app`: streamable HTTP MCP, bridge upload endpoints, GitHub index webhook, and master backoffice.
- `worker`: the only knowledge writer and scheduled gardener.
- `slack`: Socket Mode adapter; deployment pins it to one machine.

The worker checks the garden schedule during continuous queue load and purges expired upload staging
objects at least every five minutes; neither maintenance path depends on an idle queue.

Postgres stores queue/index/audit state. R2 stores private evidence and compressed exact patches.
The private knowledge repository stores current Markdown and control files.

## Supported commands

- `stigmergy-server`: deployed API or local stdio server.
- `stigmergy-librarian run`: long-running writer.
- `stigmergy-librarian-boot` and `stigmergy-librarian-credential`: worker bootstrap.
- `stigmergy-slack`: Slack adapter.
- `stigmergy-index --rebuild`: full derived-index reconciliation.
- `stigmergy-issue-token` and `stigmergy-admin-token`: credential bootstrap and rotation.
- `stigmergy-bridge`: supported local MCP client.

Queue inspection/retry, gardener triggers, entity operations, changes, contradictions, and health
are backoffice capabilities rather than separate product CLIs.
Entity merges require either one shared external ID present on every selected identity or an exact
same-entity assertion copied from a cited immutable source path. Source assertions must name every
selected identity completely and without same-label or contained-name ambiguity.

## Deployment

```bash
make test
make lint
make deploy-staging
make rebuild-staging
```

`scripts/deploy_staging.sh` validates and temporarily bakes the knowledge repository's identity,
entity-registry, and Slack-channel controls into the image, deploys all process groups, and pins the
single-writer/Slack process counts. The temporary copies are restored to empty tracked defaults on
every exit path.

After deployment, validate:

1. Fly release and all required machines are healthy.
2. `/health` and authenticated MCP initialization succeed.
3. The worker heartbeat advances and no processing lease is stale.
4. A text capture lands with one source, one commit, and one Changes entry.
5. Search sees the landed result only for authorized identities.
6. The master backoffice shows the capture, friendly diff, exact patch, entities, contradictions,
   gardener runs, and index-health state.
7. A full rebuild indexes repository HEAD, records row count/time, and clears the dirty marker.
8. Slack authentication and channel mapping are healthy; a controlled brain reaction lands through
   the same capture path when a safe test channel is available.

## Nightly reconciliation

The knowledge repository owns a pinned GitHub Actions workflow scheduled at `17 4 * * *` with
manual dispatch. It checks out the pinned platform revision and runs a full rebuild against the
selected repository HEAD. Failure remains a failed workflow; there is no successful feature-flag
no-op. The backoffice warns after 26 hours without success or after the configured convergence
grace period while the index remains dirty.

A production full rebuild accepts only the exact, clean knowledge-repository root at its checked-out
HEAD, with `ops/identities.json`, `ops/entity-registry.json`, and `ops/slack-channels.json` present.
It verifies HEAD again before publishing the rebuilt index so concurrent repository movement leaves
the previous index intact and the dirty marker visible.
