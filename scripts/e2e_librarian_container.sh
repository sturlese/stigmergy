#!/usr/bin/env bash
# Containerized-librarian e2e: the librarian runs from the SAME image
# `fly.toml`'s `worker` process group runs, through the SAME entry point, and files to the bare git
# remote FROM EMPTY VOLUMES — then is stopped with SIGTERM and killed with SIGKILL to prove the two
# shutdown paths.
#
# Same shape as `scripts/e2e_librarian.sh` (which drains with a HOST worker and proves the filing
# path itself): this one exists because a deployment artifact nothing exercises locally is an
# artifact whose first real run is on staging.
#
# Empty volumes are the point, not a detail: "the container cloned the repo itself" is only an
# assertion if the remote starts with nothing but the seed, and "one commit per filed page" is only
# an assertion if the queue started empty. Offline and keyless by construction — the composition's
# `librarian` service runs the offline double and carries no model key of any kind.
#
# >>> THIS DESTROYS THE LOCAL DOGFOOD. <<<
# `docker compose down -v` wipes the composition's volumes, and the composition has no named volume
# by design (see docker-compose.yml). That takes the index — a disposable cache, rebuildable from
# git — AND the durable capture queue, which is rebuildable from nothing: a queued capture exists
# nowhere else until the librarian files it. Same for the local evidence plane. Do not run this on a
# machine holding captures you have not drained.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-.venv/bin/python}"   # CI overrides with its own interpreter (PY=python)

# ── environment hygiene: the operator's own credentials must not reach this run ───────────────────
# The database pin and the two credential groups are the floor every e2e shares; the argument for
# each is in the file itself.
. "$(dirname "$0")/e2e_isolate.sh"

# What is specific to THIS e2e. The worker's configuration lives in `docker-compose.yml` and is
# handed to the CONTAINER, so a stray variable here cannot reach it — but the DRIVER runs on the
# host and resolves the same settings when it seeds, submits and reads back:
#   * STIGMERGY_LIBRARIAN_BACKEND / STIGMERGY_REPO would aim host-side resolution somewhere else;
#   * STIGMERGY_LIBRARIAN_BRANCH would seed a branch the container does not clone.
unset STIGMERGY_LIBRARIAN_BACKEND STIGMERGY_REPO STIGMERGY_LIBRARIAN_WORKTREE_ROOT \
      STIGMERGY_LIBRARIAN_BRANCH STIGMERGY_LIBRARIAN_REPO_URL

# The image is ONE image for both process groups, so it carries the server's deploy-time bake —
# `deploy/` holds empty committed defaults, overwritten by `scripts/deploy_staging.sh` from your
# knowledge-repo checkout for the duration of a deploy. The worker never reads any of these files;
# placeholders are enough to build, and creating them here (only when absent) keeps this e2e
# runnable whatever state `deploy/` is in — including the directory being absent entirely, which
# is how this script's first CI run failed: the bare redirection into a missing directory.
#
# **One line per Dockerfile `COPY deploy/...`, and that correspondence is now a test**
# (`tests/test_deployment_config.py`): ADR 029 added the channels COPY and not the placeholder,
# which is invisible to `make test` (it never builds the image) and fails CI's container e2e at
# the COPY — the identical failure this comment already described, repeated because the rule
# lived only in prose.
mkdir -p deploy
[ -f deploy/identities.json ] || echo '{}' > deploy/identities.json
[ -f deploy/entity-registry.json ] || echo '{"entities": {}}' > deploy/entity-registry.json
[ -f deploy/slack-channels.json ] || echo '{}' > deploy/slack-channels.json
[ -f deploy/stewards.json ] || echo '{}' > deploy/stewards.json

cleanup() { docker compose --profile librarian down -v >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== empty volumes -> postgres + minio + a bare git remote =="
docker compose --profile librarian down -v >/dev/null 2>&1 || true
docker compose up -d --wait

echo "== build the deployed image (server + worker, one image) =="
docker compose --profile librarian build librarian

echo "== the containerized filing path =="
"$PY" scripts/e2e_librarian_container.py
