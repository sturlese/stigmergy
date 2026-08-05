#!/usr/bin/env bash
# Index e2e in docker: fixture repo -> build -> run the golden
# questions (fake embedder) -> WIPE VOLUMES -> rebuild -> identical hit lists.
#
# This is the idempotency proof that the index is a cache (D10): nothing about a wipe may
# change what a query returns. Offline by construction — the fake embedder is deterministic,
# so any diff is a real nondeterminism bug, not embedding drift.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-.venv/bin/python}"   # CI overrides with its own interpreter (PY=python)
FIXTURE=tests/index/fixtures/repo
QUESTIONS=tests/index/fixtures/e2e-questions.json
OUT=out/e2e

# The shared floor (see `scripts/e2e_isolate.sh`). This script wipes the composition's volumes and
# rebuilds an index, so an operator's exported `$STIGMERGY_INDEX_DSN` — which this line used to defer to
# — pointed the wipe-and-rebuild at their own database. The index is a disposable cache (D10), so the
# blast radius here is smaller than the other two scripts'; the reason to pin is the same one, and a
# rule that holds in two scripts out of three is a rule nobody can rely on.
. "$(dirname "$0")/e2e_isolate.sh"

mkdir -p "$OUT"
cleanup() { docker compose down -v >/dev/null 2>&1 || true; }
trap cleanup EXIT

run_pass() { # $1 = report file
  docker compose up -d --wait
  "$PY" evals/run_retrieval.py --embedder fake --rebuild --repo "$FIXTURE" \
      --golden "$QUESTIONS" --report "$1"
}

echo "== pass 1: empty volumes -> build -> query =="
docker compose down -v >/dev/null 2>&1 || true
run_pass "$OUT/report-1.json"

echo "== wipe volumes =="
docker compose down -v

echo "== pass 2: rebuild from scratch -> query =="
run_pass "$OUT/report-2.json"

echo "== compare hit lists =="
# the whole report (per-arm rankings included) must be byte-identical across the wipe
if ! diff -u "$OUT/report-1.json" "$OUT/report-2.json"; then
  echo "E2E FAILED: wipe -> rebuild changed the hit lists (the index is not behaving as a cache)"
  exit 1
fi
echo "E2E OK: wipe -> rebuild -> identical hit lists ($(basename "$OUT")/report-{1,2}.json)"

echo "== substrate check (the index lint runs where the index just got built) =="
"$PY" -c "from stigmergy.index.cli import index_main; index_main(['--check', '--repo', '$FIXTURE'])"
