#!/usr/bin/env bash
# Deploy stigmergy-server's HTTP transport to Fly.io staging — the "two commands" deploy the spec
# names (acceptance 11): this script, and (for a rollback) `fly releases`/`fly deploy --image`
# (operator runbook: docs/reference/operator-runbook.md).
#
# What this script does NOT do: create a Fly app, set Fly secrets, or create/touch the Supabase
# project or the R2 bucket. Those are one-time operator setup steps, documented in the runbook —
# this script only bakes the versioned ops files (identities, entity registry, slack channels,
# stewards) from your knowledge-repo checkout into the `deploy/` staging directory, then runs
# `fly deploy` against an
# ALREADY-created app.
set -euo pipefail

STIGMERGY_REPO="${STIGMERGY_REPO:-../stigmergy-brain}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$HERE/deploy"

# `deploy/` is TRACKED — the Dockerfile COPYs every one of them, so a fresh clone must be able to
# build, so they are committed as empty defaults. That makes the real files this script bakes a
# deployment's whole identity roster (email -> ACL audiences) sitting in the working tree, one
# `git add -A` away from being published. The bake has to survive until `fly deploy` has read it
# and must not survive the script; an EXIT trap is the only form that holds on every path out,
# including the deploy failing. `tests/test_deploy_defaults.py` runs this script and checks both
# halves — that the deploy saw the real roster, and that nothing but the defaults outlived it.
restore_deploy_defaults() {
  mkdir -p "$DEPLOY_DIR"
  printf '{}\n'               > "$DEPLOY_DIR/identities.json"
  printf '{"entities": {}}\n' > "$DEPLOY_DIR/entity-registry.json"
  printf '{}\n'               > "$DEPLOY_DIR/slack-channels.json"
  printf '{}\n'               > "$DEPLOY_DIR/stewards.json"
}
trap restore_deploy_defaults EXIT

if [ ! -f "$STIGMERGY_REPO/ops/identities.json" ]; then
  echo "deploy: $STIGMERGY_REPO/ops/identities.json not found (set STIGMERGY_REPO=<path to your" >&2
  echo "        knowledge-repo checkout> if it is not a sibling of this repo)" >&2
  exit 2
fi

rm -rf "$DEPLOY_DIR"
mkdir -p "$DEPLOY_DIR"
cp "$STIGMERGY_REPO/ops/identities.json" "$DEPLOY_DIR/identities.json"
echo "deploy: baked $STIGMERGY_REPO/ops/identities.json -> deploy/identities.json"

# Entity-first resolution needs this file at the exact path fly.toml's
# `--entity-registry` flag names. ALWAYS written (an empty registry when the sibling repo has none
# yet) so the Dockerfile's `COPY` never fails on a missing source file.
if [ -f "$STIGMERGY_REPO/ops/entity-registry.json" ]; then
  cp "$STIGMERGY_REPO/ops/entity-registry.json" "$DEPLOY_DIR/entity-registry.json"
  echo "deploy: baked $STIGMERGY_REPO/ops/entity-registry.json -> deploy/entity-registry.json"
else
  echo '{"entities": {}}' > "$DEPLOY_DIR/entity-registry.json"
  echo "deploy: no $STIGMERGY_REPO/ops/entity-registry.json yet — ask will search without" \
       "entity-first resolution until one exists (not a failure)" >&2
fi

# ADR 029: the digest's audience-scoping map, for the admin console's in-process digest. Same
# always-written posture as the registry above so the Dockerfile COPY never fails; `{}` is a
# valid empty mapping — every audience then falls back to the safe empty default.
if [ -f "$STIGMERGY_REPO/ops/slack-channels.json" ]; then
  cp "$STIGMERGY_REPO/ops/slack-channels.json" "$DEPLOY_DIR/slack-channels.json"
  echo "deploy: baked $STIGMERGY_REPO/ops/slack-channels.json -> deploy/slack-channels.json"
else
  echo '{}' > "$DEPLOY_DIR/slack-channels.json"
  echo "deploy: no $STIGMERGY_REPO/ops/slack-channels.json yet — digest audiences fall back to" \
       "the safe empty default (not a failure)" >&2
fi

# the `app` and `slack` groups hold NO checkout, so `review.load_stewards`' read at
# `origin/main` had nothing to read — the doorbell never rang and every entity-proposal decision
# failed closed on a deployment whose steward was correctly configured. Baked like the three files
# above, and the trade is the one `identities.json` already accepted: a redeploy to change it. The
# worker, which HAS a checkout, still reads the repo at each item's base commit.
if [ -f "$STIGMERGY_REPO/ops/stewards.json" ]; then
  cp "$STIGMERGY_REPO/ops/stewards.json" "$DEPLOY_DIR/stewards.json"
  echo "deploy: baked $STIGMERGY_REPO/ops/stewards.json -> deploy/stewards.json"
else
  echo '{}' > "$DEPLOY_DIR/stewards.json"
  echo "deploy: no $STIGMERGY_REPO/ops/stewards.json yet — no scope resolves to a steward, so the" \
       "doorbell records an undeliverable and every review decision fails closed (not a failure," \
       "but nothing will be decidable until the map exists)" >&2
fi

cd "$HERE"
fly deploy

# Socket Mode has no leader election, and `fly deploy` creates two
# machines by default for a NEW process group — the `slack` group's own app-startup
# `pg_try_advisory_lock` (stigmergy.slack.app.acquire_singleton_lock) refuses the second one at
# runtime, but explicitly pinning the count here means a deploy never even CREATES a second
# machine to be refused. A no-op (exits 0) if the group is already at 1.
# `--yes` is not optional: without a TTY (a CI runner, or a deploy driven from a tool rather than a
# keyboard) `fly scale count` refuses with "--yes flag must be specified when not running
# interactively" and the deploy script exits non-zero AFTER the deploy itself succeeded — leaving
# exactly the two-machine slack group this line exists to prevent, and an error message that points
# at scaling rather than at the pin. Found on the first non-interactive deploy of this group.
fly scale count slack=1 --yes

# The worker gets the same pin for a DIFFERENT reason. Two Slack machines would double-handle
# events (see above). The worker's default second machine is a Fly STANDBY — created stopped,
# claiming nothing (this app's first deploy left exactly one, noticed days later) — but a
# standby is one `fly machine start` away from a second PAID poller, and nothing refuses that
# start: the queue's visibility leases keep two workers correct, and only the Anthropic invoice
# notices. The pin destroys the standby so a second runner can never appear by accident, and the
# trade — no automatic host-failure failover for the librarian — is recorded in the runbook.
# A no-op (exits 0) if the group is already at 1.
fly scale count worker=1 --yes
