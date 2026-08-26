#!/usr/bin/env bash
# Deploy the current platform image and all three Fly staging process groups.
set -euo pipefail

STIGMERGY_REPO="${STIGMERGY_REPO:-../stigmergy-brain}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$HERE/deploy"

mkdir -p "$DEPLOY_DIR"
if ! mkdir "$DEPLOY_DIR/.staging-deploy.lock" 2>/dev/null; then
  echo "deploy: staging deployment is already in progress" >&2
  exit 2
fi

# This one list defines both the temporary bake and its fail-closed restoration.
BAKED=(
  'identities.json:{}'
  'entity-registry.json:{"version": 1, "entities": {}, "redirects": {}}'
  'slack-channels.json:{}'
)
staging_root=""

restore_deploy_defaults() {
  mkdir -p "$DEPLOY_DIR"
  for entry in "${BAKED[@]}"; do
    printf '%s\n' "${entry#*:}" > "$DEPLOY_DIR/${entry%%:*}"
  done
}

cleanup_deploy() {
  restore_deploy_defaults
  if [ -n "$staging_root" ]; then
    for entry in "${BAKED[@]}"; do
      rm -f "$staging_root/ops/${entry%%:*}"
    done
    rmdir "$staging_root/ops" "$staging_root" >/dev/null 2>&1 || true
  fi
  rmdir "$DEPLOY_DIR/.staging-deploy.lock" >/dev/null 2>&1 || true
}

trap cleanup_deploy EXIT

refresh_record="$(bash "$HERE/scripts/refresh_staging_checkout.sh" "$STIGMERGY_REPO")"
case "$refresh_record" in
  staging-refresh:\ root=*\ head=*) ;;
  *)
    echo "deploy: invalid staging checkout refresh record" >&2
    exit 2
    ;;
esac
root="${refresh_record#staging-refresh: root=}"
sha="${root##* head=}"
root="${root% head=*}"
if [ -z "$root" ] || [ -z "$sha" ] ||
   ! git -C "$root" rev-parse --verify --quiet "$sha^{commit}" >/dev/null 2>&1 ||
   [ "$(git -C "$root" rev-parse --verify --quiet 'HEAD^{commit}' 2>/dev/null)" != "$sha" ]; then
  echo "deploy: invalid staging checkout refresh record" >&2
  exit 2
fi

PREFLIGHT_PY="${STIGMERGY_PYTHON:-$HERE/.venv/bin/python}"
if [ ! -x "$PREFLIGHT_PY" ] ||
   ! "$PREFLIGHT_PY" -c "import stigmergy.server.identity" >/dev/null 2>&1; then
  echo "deploy: $PREFLIGHT_PY cannot import stigmergy; run make venv or set STIGMERGY_PYTHON" >&2
  exit 2
fi

staging_root="$(mktemp -d "$DEPLOY_DIR/.staging-controls.XXXXXX")" || {
  echo "deploy: could not create private control staging" >&2
  exit 2
}
if ! mkdir "$staging_root/ops"; then
  echo "deploy: could not create private control layout" >&2
  exit 2
fi
for entry in "${BAKED[@]}"; do
  name="${entry%%:*}"
  if ! git -C "$root" show "$sha:ops/$name" > "$staging_root/ops/$name"; then
    echo "deploy: required control file ops/$name is not present at verified HEAD" >&2
    exit 2
  fi
done

if ! "$PREFLIGHT_PY" -c "
import sys
from stigmergy.server.controls import ControlError, validate_root
try:
    validate_root(sys.argv[1])
except ControlError as error:
    print(f'deploy: {error}', file=sys.stderr)
    raise SystemExit(1)
" "$staging_root"; then
  echo "deploy: refusing to bake an invalid control-file set" >&2
  exit 2
fi

for entry in "${BAKED[@]}"; do
  name="${entry%%:*}"
  if ! cp "$staging_root/ops/$name" "$DEPLOY_DIR/$name"; then
    echo "deploy: could not install validated control file $name" >&2
    exit 2
  fi
  echo "deploy: baked ops/$name at $sha -> deploy/$name"
done


cd "$HERE"
fly deploy --ha=false --yes

# Staging runs exactly one machine per process group.
fly scale count app=1 slack=1 worker=1 --yes
