#!/usr/bin/env bash
# Deploy stigmergy-server's HTTP transport to Fly.io staging — the "two commands" deploy the spec
# names (acceptance 11): this script, and (for a rollback) `fly releases`/`fly deploy --image`
# (operator runbook: docs/reference/operator-runbook.md).
#
# What this script does NOT do: create a Fly app, set Fly secrets, or create/touch the Supabase
# project or the R2 bucket. Those are one-time operator setup steps, documented in the runbook —
# this script only bakes the versioned ops files (identities, entity registry, slack channels,
# from your knowledge-repo checkout into the `deploy/` staging directory, then runs
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
#
# ONE list, `name:empty-default`, and that is the point of it: the set this script CLEARS on the
# way in and the set it RESTORES on the way out have to be the same set. They were not. The script
# used to clear by deleting the whole directory, while the trap only ever knew the files below —
# so when a subdirectory arrived beside them (`deploy/workflows/`, cron templates and a README, all
# tracked, since deleted with the crons themselves), one `make deploy-staging` deleted them from
# the working tree and nothing here could put them back. Blast radius larger than the repair, silently, for as long as it took someone to
# run a deploy and read `git status`. Derived from one list, the two cannot drift apart again.
BAKED=(
  'identities.json:{}'
  'entity-registry.json:{"entities": {}}'
  'slack-channels.json:{}'
)

restore_deploy_defaults() {
  mkdir -p "$DEPLOY_DIR"
  for entry in "${BAKED[@]}"; do
    printf '%s\n' "${entry#*:}" > "$DEPLOY_DIR/${entry%%:*}"
  done
}
trap restore_deploy_defaults EXIT

if [ ! -f "$STIGMERGY_REPO/ops/identities.json" ]; then
  echo "deploy: $STIGMERGY_REPO/ops/identities.json not found (set STIGMERGY_REPO=<path to your" >&2
  echo "        knowledge-repo checkout> if it is not a sibling of this repo)" >&2
  exit 2
fi

# The roster must parse under the grammar the SERVER will read it with, before the image that
# reads it ships. Since ADR 045 D7 there is one value shape, and a leftover `"*"` invalidates the
# WHOLE file rather than one entry — so deploying ahead of the roster rewrite is a total 401
# outage, not a partial one. Same posture as the missing-file check above: exit 2, with the
# parser's own sentence, which names the line to write instead.
#
# THREE outcomes, and the third is the one worth spelling out. A file that parses bakes; a file
# that does not stops the deploy; and a check that could not RUN — no interpreter here has this
# package importable, which is every invocation outside `make deploy-staging` — says so on stderr
# and lets the deploy through. It has to be that way round: this script is copied and run
# standalone by its own tests, and a preflight that failed closed on its own absence would make
# "I could not look" indistinguishable from "I looked and it is broken". Loud, so a skipped check
# is impossible to miss (`CLAUDE.md`'s fourth testing line, applied to an operator script).
PREFLIGHT_PY=""
for candidate in "$(dirname "$0")/../.venv/bin/python" .venv/bin/python python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c "import stigmergy.server.identity" >/dev/null 2>&1; then
    PREFLIGHT_PY="$candidate"
    break
  fi
done

if [ -z "$PREFLIGHT_PY" ]; then
  echo "deploy: WARNING — no interpreter here can import stigmergy, so the ops files were NOT" >&2
  echo "        checked against the grammar the server reads them with. They are being baked" >&2
  echo "        unvalidated; run \`make deploy-staging\` (which builds the venv) to get the check." >&2
else
  for ops_file in identities slack-channels; do
    src="$STIGMERGY_REPO/ops/$ops_file.json"
    [ -f "$src" ] || continue
    case "$ops_file" in
      identities)     subject=identity ;;
      slack-channels) subject=channel ;;
    esac
    if ! "$PREFLIGHT_PY" -c "
import pathlib, sys
from stigmergy.server.errors import IdentityError
from stigmergy.server.identity import group_map_from_text
try:
    group_map_from_text(pathlib.Path(sys.argv[1]).read_text(), origin=sys.argv[1],
                        subject=sys.argv[2])
except IdentityError as ex:
    print(f'deploy: {ex}', file=sys.stderr)
    raise SystemExit(1)
" "$src" "$subject"; then
      echo "deploy: refusing to bake an ops file the server would refuse to read" >&2
      exit 2
    fi
  done
fi

# Clear what this script writes, and NOTHING else — never the directory itself. `deploy/` also
# holds tracked files this script knows nothing about, and the next one added re-opens the wound
# if the delete is by directory rather than by name.
mkdir -p "$DEPLOY_DIR"
for entry in "${BAKED[@]}"; do
  rm -f "$DEPLOY_DIR/${entry%%:*}"
done
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
