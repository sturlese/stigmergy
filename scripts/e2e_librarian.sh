#!/usr/bin/env bash
# Librarian e2e in docker: `docker compose up` brings postgres + minio
# + a bare git remote, and the whole filing path runs against them FROM EMPTY VOLUMES — N captures
# including two duplicates, one with figures, one with a seeded secret and one unanchorable, drained
# by two independent workers, with the expected pages committed to the bare remote.
#
# Empty volumes are the point, not a detail: "one commit per filed page" is only an assertion if the
# remote starts with nothing but the seed, and "the queue drained" is only an assertion if it started
# empty. Offline and keyless by construction — the agent is the offline double.
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
# The database pin and the two credential groups (evidence, the librarian App) are the floor every
# e2e shares, so they live in ONE file with the full argument for each — `make` does `-include .env`
# + `export`, and this script's whole premise is that it runs against disposable state.
. "$(dirname "$0")/e2e_isolate.sh"

# What is specific to THIS e2e, because it is the only one that runs the librarian:
#   * `STIGMERGY_LIBRARIAN_BACKEND=sdk` would spend real money running a real agent on fixtures;
#   * `STIGMERGY_REPO` would aim the whole thing at the operator's real `../knowledge-repo` checkout;
#   * `STIGMERGY_LIBRARIAN_WORKTREE_ROOT` would put this run's worktrees where another librarian's
#     startup reap can see them.
unset STIGMERGY_LIBRARIAN_BACKEND STIGMERGY_REPO STIGMERGY_LIBRARIAN_WORKTREE_ROOT
# The rest of `config.Settings.from_args`' env surface, in decreasing consequence. None of these
# would aim the run at production, and every one of them can make it prove something other than what
# it claims:
#   * STIGMERGY_GITLEAKS_BIN is a DIFFERENT binary from the `command -v gitleaks` checked below, so the
#     pre-flight could pass while the worker ran something else — or nothing;
#   * STIGMERGY_LIBRARIAN_BRANCH would file onto a branch the assertions do not read;
#   * the two budgets change what "the queue drained" measures;
#   * STIGMERGY_LIBRARIAN_REFUSED_DIFF_DIR would scatter the refused-diff diagnostics.
unset STIGMERGY_GITLEAKS_BIN STIGMERGY_LIBRARIAN_BRANCH STIGMERGY_LIBRARIAN_TIMEOUT_S \
      STIGMERGY_LIBRARIAN_DEDUP_WINDOW_S STIGMERGY_LIBRARIAN_REFUSED_DIFF_DIR

command -v gitleaks >/dev/null || {
  echo "e2e_librarian: gitleaks is not on PATH, and the secrets gate cannot run without it — the"
  echo "  seeded-secret capture would be FILED instead of bounced and this e2e would pass while"
  echo "  proving the opposite. Install it (brew install gitleaks) and re-run."
  exit 2
}

cleanup() { docker compose down -v >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== empty volumes -> postgres + minio + a bare git remote =="
docker compose down -v >/dev/null 2>&1 || true
docker compose up -d --wait

echo "== the filing path =="
"$PY" scripts/e2e_librarian.py
