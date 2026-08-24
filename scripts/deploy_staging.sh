#!/usr/bin/env bash
# Deploy the current platform image and all three Fly staging process groups.
set -euo pipefail

STIGMERGY_REPO="${STIGMERGY_REPO:-../stigmergy-brain}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$HERE/deploy"

# This one list defines both the temporary bake and its fail-closed restoration.
BAKED=(
  'identities.json:{}'
  'entity-registry.json:{"version": 1, "entities": {}, "redirects": {}}'
  'slack-channels.json:{}'
)

restore_deploy_defaults() {
  mkdir -p "$DEPLOY_DIR"
  for entry in "${BAKED[@]}"; do
    printf '%s\n' "${entry#*:}" > "$DEPLOY_DIR/${entry%%:*}"
  done
}
trap restore_deploy_defaults EXIT

for ops_file in identities entity-registry slack-channels; do
  if [ ! -f "$STIGMERGY_REPO/ops/$ops_file.json" ]; then
    echo "deploy: required control file $STIGMERGY_REPO/ops/$ops_file.json not found" >&2
    exit 2
  fi
done

PREFLIGHT_PY="${STIGMERGY_PYTHON:-$HERE/.venv/bin/python}"
if [ ! -x "$PREFLIGHT_PY" ] ||
   ! "$PREFLIGHT_PY" -c "import stigmergy.server.identity" >/dev/null 2>&1; then
  echo "deploy: $PREFLIGHT_PY cannot import stigmergy; run make venv or set STIGMERGY_PYTHON" >&2
  exit 2
fi

if ! "$PREFLIGHT_PY" -c "
import sys
from stigmergy.server.controls import ControlError, validate_root
try:
    validate_root(sys.argv[1])
except ControlError as error:
    print(f'deploy: {error}', file=sys.stderr)
    raise SystemExit(1)
" "$STIGMERGY_REPO"; then
  echo "deploy: refusing to bake an invalid control-file set" >&2
  exit 2
fi

# Clear only files controlled by this script.
mkdir -p "$DEPLOY_DIR"
for entry in "${BAKED[@]}"; do
  rm -f "$DEPLOY_DIR/${entry%%:*}"
done
cp "$STIGMERGY_REPO/ops/identities.json" "$DEPLOY_DIR/identities.json"
echo "deploy: baked $STIGMERGY_REPO/ops/identities.json -> deploy/identities.json"
cp "$STIGMERGY_REPO/ops/entity-registry.json" "$DEPLOY_DIR/entity-registry.json"
echo "deploy: baked $STIGMERGY_REPO/ops/entity-registry.json -> deploy/entity-registry.json"
cp "$STIGMERGY_REPO/ops/slack-channels.json" "$DEPLOY_DIR/slack-channels.json"
echo "deploy: baked $STIGMERGY_REPO/ops/slack-channels.json -> deploy/slack-channels.json"


cd "$HERE"
fly deploy --ha=false --yes

# Staging runs exactly one machine per process group.
fly scale count app=1 slack=1 worker=1 --yes
