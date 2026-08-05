# Sourced by every e2e script. NOT executable on its own: `. scripts/e2e_isolate.sh`.
#
# ── why this file exists ──────────────────────────────────────────────────────────────────────────
# The Makefile does `-include .env` + `export`, so EVERY variable in the operator's gitignored env
# file is already in an e2e script's environment. Three families of them silently redirect a run at
# something a person is using, and every one of the three has already caused real damage or come
# within one variable of it:
#
#   * $STIGMERGY_INDEX_DSN selects the DATABASE. Deferring to the environment (`${VAR:-default}`) means
#     an operator's dogfood or staging DSN WINS — and these scripts submit fixture captures (one
#     carrying a seeded secret), drain them, and `docker compose down -v` the volumes afterwards. A
#     queued capture exists nowhere else until the librarian files it, so that is unrecoverable.
#   * the STIGMERGY_EVIDENCE_* group selects the evidence plane. Pointed at Cloudflare R2 it archives
#     this run's fixture captures — the secret-bearing one included — into the durable production
#     bucket, and makes every "the bucket holds exactly N objects" assertion read a bucket that is
#     not this run's.
#   * the librarian App group makes `githubapp.configured()` true, and `processing._file` then pushes
#     to the real knowledge repo on GitHub instead of to the run's own bare remote. That exact leak
#     already happened once from `make test` — 19 fixture commits, to the company's repo — which is
#     why `tests/librarian/conftest.py` clears those four variables structurally rather than by
#     asking.
#
# PINNED UNCONDITIONALLY, never `${VAR:-...}`: an escape hatch is a rule someone has to remember, and
# remembering is what failed. Same doctrine `tests/conftest.py` applies (it overwrites
# unconditionally) and `tests/testdb.py` enforces at the connection seam with no override flag — the
# property must not depend on what is in one operator's env file.
#
# Unset rather than overridden for the two credential groups, so the code's own documented defaults
# (which ARE the composition's values) apply and this file does not become a second place those
# defaults are written down.
#
# Each script unsets whatever else can make IT prove something other than what it claims; this is the
# floor they share, not the whole of any one script's hygiene.

export STIGMERGY_INDEX_DSN="postgresql://stigmergy:stigmergy@localhost:54321/stigmergy"

unset STIGMERGY_EVIDENCE_ENDPOINT STIGMERGY_EVIDENCE_BUCKET \
      STIGMERGY_EVIDENCE_ACCESS_KEY_ID STIGMERGY_EVIDENCE_SECRET_ACCESS_KEY

unset STIGMERGY_LIBRARIAN_APP_ID STIGMERGY_LIBRARIAN_INSTALLATION_ID \
      STIGMERGY_LIBRARIAN_PRIVATE_KEY STIGMERGY_LIBRARIAN_PRIVATE_KEY_FILE
