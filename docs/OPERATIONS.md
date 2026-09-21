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
The master-only `POST /admin/api/knowledge/recompile` control submits the same writer request as
gardening with `mode: recompile`; it is not an index rebuild and has no separate installed CLI. It
recompiles only derived notes, concepts, links, and entity anchors from committed immutable sources
in an isolated worktree. A failed source, repair, or gate abandons the entire candidate. Successful
recompiles retain valid source-backed identities and use the existing `garden` trigger in the
unified change ledger, with `operation: recompile` in the run report.
Entity merges require either one shared external ID present on every selected identity or an exact
same-entity assertion copied from a cited immutable source path. Source assertions must name every
selected identity completely and without same-label or contained-name ambiguity.

## Deployment

```bash
make test
make lint
make deploy-staging PARITY_ARTIFACT=/absolute/path/to/parity-result.json
make rebuild-staging
```

Staging deployment and rebuild first refresh the configured knowledge checkout. The checkout must
be clean, attached to a branch with a configured upstream, and able to fast-forward to that
upstream; otherwise the operation fails closed. The refresh yields the checkout's canonical physical
root and its verified commit SHA, and subsequent work is bound to that exact revision.

`scripts/deploy_staging.sh` serializes staging deployments with a fail-closed lock. It materializes
the identity, entity-registry, and Slack-channel JSON controls from committed blobs at the verified
knowledge revision, rather than from the mutable working tree, validates them, deploys all process
groups, and pins the single-writer/Slack process counts. The temporary copies are restored to empty
tracked defaults on every exit path.

`make rebuild-staging` uses the same refreshed physical checkout and verified revision. It passes
`STAGING_DSN` only through the process environment, so the database credential is neither included
in command arguments nor written to command output.

Deploy the cloud server before publishing a bridge that depends on its MCP contract. During rollback,
restore the compatible bridge first, then roll back the cloud server; this keeps local acquisition
from calling an unavailable or newer server contract.

### Slack application

`deploy/slack-app-manifest.json` is the Slack configuration contract. Import it from the app's
**App Manifest** page, reinstall or reauthorize the app whenever its OAuth scopes change, and issue
an app-level Socket Mode token with `connections:write`.

The bot needs `files:read` to download user attachments and `groups:history` to capture mapped
private channels. `chat:write` also covers private delivery when the asker may see more than the
channel audience. It does not need `im:write`, `files:write`, or `reactions:write`: users provide
attachments, and capture does not write reactions. Invite the bot to every mapped public or private
channel.

After installation, verify that the token's granted scopes match the manifest. Slack returns them
in the `x-oauth-scopes` response header; a checked-in manifest alone does not update an already
installed token.

After deployment, validate:

1. Fly release and all required machines are healthy.
2. `/health` and authenticated MCP initialization succeed.
3. The worker heartbeat advances and no processing lease is stale.
4. A text capture lands with one source, one commit, and one Changes entry.
5. A controlled multi-idea capture produces a cohesive, linked set of cold-readable pages with local
   citations; a rejected plan leaves only its immutable source and reports `plan_rejected`.
6. Search sees the landed result only for authorized identities, and one `describe_entity` call
   returns a bounded ACL-visible dossier with content-bearing evidence and relationships.
7. An ACL-isolation probe proves hidden entity evidence, counts, names, and source titles do not
   appear in a restricted reader's dossier.
8. A master recompile preserves immutable sources, records `operation: recompile` under the `garden`
   ledger trigger, and either advances the complete derived candidate or advances nothing.
9. The master backoffice shows the capture, friendly diff, exact patch, entities, contradictions,
   gardener runs, and index-health state.
10. A full rebuild indexes repository HEAD, records row count/time, and clears the dirty marker.
11. Slack authentication and channel mapping are healthy; a controlled brain reaction lands through
   the same capture path with a supported attachment when a safe test channel is available.

For a librarian/compiler release, validate the recorded real-model parity artifact before any
deployment command: `python evals/filing/parity.py --artifact <result.json>`. The command is a
fail-closed release gate: a non-zero result blocks rollout. The artifact must bind both independent
implementation runs to the same corpus, immutable initial graph, source-case hashes, Stigmergy
commit, librarian skill hash, recorded raw gates, a verified content-addressed blind-review bundle,
and exactly three complete production-level repeats bound to the configured librarian setting. Schema v6
models those current invariants directly, including each case's initial plan, optional bounded correction,
and replayed effective output; it has no staged-planner phases or live legacy compatibility fields. Keep
the artifact and its review bundle outside the candidate checkout.

The gate trusts the release operator and verifies evidence integrity within that operational boundary.
Give the reviewer only the blind packet and response schema, retain the response verbatim, and unblind
it mechanically. The hashes prove byte identity and binding, not reviewer identity against a malicious
operator with Fly credentials. That actor is out of scope for the local deployment flow: protecting
against it requires a protected CI environment to own both deployment authority and reviewer
attestation, rather than a signing key stored on the same release host.

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

The rebuild reads committed HEAD only and fails closed above 50 MiB for one page, 512 MiB across the
candidate corpus, 500,000 watched entries, or 250,000 eligible entries. It also reconciles recovered
committed source pages into the ACL-aware source projection, so `read_page` can serve readable
evidence after recovery without exposing it beyond its audience.
