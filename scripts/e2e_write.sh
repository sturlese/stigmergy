#!/usr/bin/env bash
# Write-path e2e in docker: `docker compose up` brings postgres +
# minio, and the whole capture path runs against them FROM EMPTY VOLUMES — submit over the real
# MCP protocol, archive, claim under concurrency, kill a claimer, reclaim, purge.
#
# Empty volumes are the point, not a detail: "identical material yields exactly one object" is
# only a real assertion if the bucket starts empty, and an exactly-once claim count is only real
# if the queue does. Offline by construction — the fake embedder builds the index and nothing in
# the driver calls a model, so this needs no API key.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-.venv/bin/python}"   # CI overrides with its own interpreter (PY=python)

# ── environment hygiene: the operator's own credentials must not reach this run ───────────────────
# The same guard `e2e_librarian.sh` gets, for the same reason, and it was missing here. This script
# rebuilds an index, submits fixture captures, claims and purges them, and asserts on the WHOLE
# evidence bucket's object count — so an operator's exported `$STIGMERGY_INDEX_DSN` (this line used to
# defer to it) aimed all of that at their dogfood queue, and an exported `STIGMERGY_EVIDENCE_*` archived
# the fixtures into the production bucket while making every object-count assertion read somebody
# else's bucket. `scripts/e2e_isolate.sh` carries the full argument for each variable it pins.
#
# The librarian App group is in that shared floor rather than only in `e2e_librarian.sh` deliberately:
# nothing in THIS script pushes to git today, so unsetting them changes nothing here — and that is
# exactly why it belongs to the shared floor instead of being re-decided per script the day one of
# them grows a push.
. "$(dirname "$0")/e2e_isolate.sh"

cleanup() { docker compose down -v >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== empty volumes -> postgres + minio =="
docker compose down -v >/dev/null 2>&1 || true
docker compose up -d --wait

echo "== write path =="
"$PY" scripts/e2e_write.py
