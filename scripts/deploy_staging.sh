#!/usr/bin/env bash
# Deploy the current platform image and all three Fly staging process groups.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$HERE/deploy"

platform_root="$(git -C "$HERE" rev-parse --show-toplevel 2>/dev/null)" || {
  echo "deploy: platform directory is not a Git worktree" >&2
  exit 2
}
platform_root="$(cd "$platform_root" && pwd -P)"
if [ "$platform_root" != "$HERE" ]; then
  echo "deploy: deploy script must run from the platform Git worktree root" >&2
  exit 2
fi
if [ -n "$(git -C "$HERE" status --porcelain=v1 --untracked-files=all)" ]; then
  echo "deploy: platform checkout has tracked or untracked changes; use an exact clean checkout" >&2
  exit 2
fi

mkdir -p "$DEPLOY_DIR"
if ! mkdir "$DEPLOY_DIR/.staging-deploy.lock" 2>/dev/null; then
  echo "deploy: staging deployment is already in progress" >&2
  exit 2
fi
trap 'rmdir "$DEPLOY_DIR/.staging-deploy.lock" >/dev/null 2>&1 || true' EXIT

PREFLIGHT_PY="${STIGMERGY_PYTHON:-$HERE/.venv/bin/python}"
if [ ! -x "$PREFLIGHT_PY" ] ||
   ! "$PREFLIGHT_PY" -c "import stigmergy.server.identity" >/dev/null 2>&1; then
  echo "deploy: $PREFLIGHT_PY cannot import stigmergy; run make venv or set STIGMERGY_PYTHON" >&2
  exit 2
fi

# The filing parity gate is the project's release gate for a librarian contract change. An operator
# deploying a released version to their own deployment may turn it off explicitly.
PARITY_ARTIFACT="${STIGMERGY_PARITY_ARTIFACT:-}"
if [ "${STIGMERGY_PARITY_GATE:-on}" = "off" ]; then
  echo "deploy: filing parity gate off (STIGMERGY_PARITY_GATE=off)" >&2
else
  if [ -z "$PARITY_ARTIFACT" ] || [ ! -f "$PARITY_ARTIFACT" ]; then
    echo "deploy: STIGMERGY_PARITY_ARTIFACT must name a recorded passing parity artifact," >&2
    echo "deploy: or set STIGMERGY_PARITY_GATE=off to deploy a released version without one" >&2
    exit 2
  fi
  if ! PYTHONDONTWRITEBYTECODE=1 "$PREFLIGHT_PY" "$HERE/evals/filing/parity.py" \
    --artifact "$PARITY_ARTIFACT" --repo-root "$HERE"; then
    echo "deploy: recorded parity gate rejected this candidate" >&2
    exit 2
  fi
fi

# The operator's legal pages (one Markdown file per page), copied onto every machine.
legal_args=()
if [ -n "${STIGMERGY_LEGAL_SOURCE:-}" ]; then
  [ -d "$STIGMERGY_LEGAL_SOURCE" ] || { echo "deploy: STIGMERGY_LEGAL_SOURCE is not a directory" >&2; exit 2; }
  legal_args+=(--env STIGMERGY_LEGAL_DIR=/srv/legal)
  for page in "$STIGMERGY_LEGAL_SOURCE"/*.md; do
    [ -f "$page" ] && legal_args+=(--file-local "/srv/legal/$(basename "$page")=$page")
  done
fi

cd "$HERE"
fly deploy --ha=false --yes ${legal_args[@]+"${legal_args[@]}"}

# One machine each for the app and the writer. The Slack adapter runs only where it is enabled
# (STIGMERGY_SLACK_MACHINES=1); its secrets stay set either way.
SLACK_MACHINES="${STIGMERGY_SLACK_MACHINES:-0}"
case "$SLACK_MACHINES" in
  0|1) ;;
  *) echo "deploy: STIGMERGY_SLACK_MACHINES must be 0 or 1" >&2; exit 2 ;;
esac
fly scale count app=1 worker=1 slack="$SLACK_MACHINES" --yes
