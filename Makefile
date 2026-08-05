# stigmergy — convenience targets.
#
# One package, one venv. `make venv` bootstraps .venv from pyproject.toml; every
# other target uses it, so a clean checkout needs nothing but `make test`.
.DEFAULT_GOAL := help

# Operator convenience: a gitignored .env at the repo root (KEY=value lines, no quotes needed)
# is loaded and exported into every target, so credentialed targets (r2-smoke, a local
# --rebuild against staging) run without a wall of exports. Never commit it (.gitignore).
-include .env
export

VENV := .venv
PY := $(VENV)/bin/python
STAMP := $(VENV)/.deps-ok

help: ## Show this help
	@# Three details this one line gets wrong easily, each of which broke the listing once:
	@#
	@# 1. `[a-zA-Z0-9_-]`, not `[a-zA-Z_-]`: a DIGIT in a target name does not match the narrower
	@#    class, so `e2e`, `e2e-write` and `r2-smoke` were silently absent from this listing — real
	@#    targets an operator could only find by reading the file.
	@# 2. `$(firstword $(MAKEFILE_LIST))`, not all of it: `-include .env` puts that file in the list
	@#    too, and grep across TWO files prefixes every match with its filename — so with an env file
	@#    present (which is every credentialed machine) the first column printed "Makefile" for every
	@#    row instead of the target name. The help was unusable on exactly the machines that need it.
	@# 3. The column width is the LONGEST TARGET plus one (`e2e-librarian-container`, 23), and a
	@#    target added without checking it wraps the whole row.
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(firstword $(MAKEFILE_LIST)) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'

$(STAMP): pyproject.toml
	@# The stamp is written only after a successful install, so an interrupted bootstrap
	@# (Ctrl-C, network blip) is retried instead of leaving a deps-less venv behind.
	rm -rf $(VENV)
	python3 -m venv $(VENV) 2>/dev/null || uv venv --python 3.12 $(VENV)
	$(PY) -m pip -q install --upgrade pip
	$(PY) -m pip -q install -e ".[dev]"
	touch $(STAMP)

venv: $(STAMP) ## Bootstrap the virtualenv (one-time; re-runs if pyproject.toml changes)

# Runs against the `stigmergy_test` database, never the dogfood `stigmergy` one: the Postgres
# fixtures truncate capture_queue at setup, and that once deleted real captures out from under a
# live demo. No env var is set here ON PURPOSE — `tests/testdb.py` defaults to stigmergy_test, and
# setting $STIGMERGY_TEST_DSN would flip the suite into CI mode, where a machine with no docker
# FAILS instead of skipping cleanly. Note the `-include .env` above: a $STIGMERGY_INDEX_DSN kept
# there for the dogfood (or for staging) IS exported into this target, which is exactly why the
# suite no longer reads that variable.
#
# The same `-include .env` + `export` also hands the suite every OTHER credential in that file,
# and one of them reaches out of the machine: `STIGMERGY_LIBRARIAN_APP_ID` and friends make
# `githubapp.configured()` true, so the librarian tests pushed their fixture commits to the real
# knowledge repo on GitHub instead of to the fixtures' own bare remote — 19 failures, and real
# writes to the company's knowledge repo from `make test`. The suite is defended structurally
# rather than by removing the export: `tests/conftest.py` has a repo-wide autouse fixture that
# clears those four variables for EVERY test in the whole suite (not one package's conftest —
# a file relying on its own per-file copy of the same guard turned out to have none). Same
# doctrine as the DSN guard above — the property must not depend on which variables happen to be
# in one operator's .env, or on which test FILE remembered to defend itself.
#
test: venv ## Run the whole suite against stigmergy_test (invariant + increments, coverage gate 75%)
	$(PY) -m pytest -q --cov-fail-under=75

lint: venv ## Ruff over the repo
	$(VENV)/bin/ruff check src tests evals scripts

db-up: ## Start the platform composition (postgres+pgvector + minio)
	docker compose up -d --wait

db-down: ## Stop it and WIPE volumes — DESTROYS the local queue (the index is a disposable cache, local evidence is throwaway; a queued capture exists nowhere else)
	docker compose down -v

e2e: venv ## DESTROYS the local queue (down -v). Index e2e in docker: build -> golden -> wipe -> rebuild -> identical hit lists
	bash scripts/e2e.sh

e2e-write: venv ## DESTROYS the local queue (down -v). Write-path e2e: submit -> archive -> claim -> die -> reclaim -> purge
	bash scripts/e2e_write.sh

e2e-librarian: venv ## DESTROYS the local queue (down -v). Librarian e2e: compose (postgres+minio+bare git remote) -> N captures -> commits on the remote
	bash scripts/e2e_librarian.sh

# The DEPLOYED artifact's e2e, and the reason it is a second target rather than a phase of the one
# above: this one builds the image (`fly.toml`'s two process groups share it) and drains with the
# worker CONTAINER, so it is slower, it needs docker to build rather than only to run, and its
# failure means something different — the deployment is wrong, not the filing path.
e2e-librarian-container: venv ## DESTROYS the local queue (down -v). Deployed-worker e2e: build the image -> the librarian CONTAINER files to the bare remote -> SIGTERM/SIGKILL
	bash scripts/e2e_librarian_container.sh

# The librarian's credentials and the agent's API key live in the gitignored root env file, which
# `-include .env` + `export` above hands to every target and which a directly-invoked
# `.venv/bin/stigmergy-librarian` inherits NOTHING from. That gap cost four separate detours in one day
# in a single day — twice for the agent credential, once for the App credentials, once for
# a lost session — and each one looked like a product defect until it was diagnosed. So the target
# exists to make the environment the TOOL's problem instead of the operator's memory.
#
# Everything it can be wrong about is refused by name rather than downstream: the env file's absence
# here, and the credential/App/skill/linter/lease preconditions in `worker.startup_checks`, which runs
# before a single item is claimed. `--backend sdk` is explicit and not the default anywhere else: the
# double files fabricated pages, and a walk that silently ran it would commit them to the real repo.
#
# `once`, not `run`: a walk drains by hand so a human can read each outcome before the next claim.
# Pass extra flags through `LIBRARIAN_ARGS` (e.g. `make librarian-walk LIBRARIAN_ARGS=--json`).
librarian-walk: venv ## One librarian filing walk with the REAL agent against $STIGMERGY_REPO (needs the env file)
	@test -f .env || { echo "make librarian-walk: there is no .env at the repo root, and a real walk cannot run without it. It carries the Claude credential the agent authenticates with and the three librarian GitHub App variables the commit is authored with; see docs/reference/operator-runbook.md for the list and how to obtain each."; exit 2; }
	$(VENV)/bin/stigmergy-librarian --backend sdk once $(LIBRARIAN_ARGS)

librarian-status: venv ## Queue depth, the item in flight (and whether its lease looks stale), and the measured capture->filed p50/p95
	$(VENV)/bin/stigmergy-librarian status $(LIBRARIAN_ARGS)

# Same shape and same reason as `librarian-walk` above: the three Slack credentials come from the
# environment ONLY, and a target is the one thing that loads the env file, so this
# exists to keep them out of a shell history and out of `.mcp.json`. Extra flags via `SLACK_ARGS`.
slack-run: venv ## Run the Slack bot against $STIGMERGY_REPO (needs the env file: SLACK_APP_TOKEN, SLACK_BOT_TOKEN, SLACK_TEAM_ID, OPENAI_API_KEY)
	@test -f .env || { echo "make slack-run: there is no .env at the repo root, and the bot cannot start without it. It carries SLACK_APP_TOKEN (xapp-, Socket Mode), SLACK_BOT_TOKEN (xoxb-), SLACK_TEAM_ID (T...) and OPENAI_API_KEY; see docs/reference/operator-runbook.md for how to obtain each."; exit 2; }
	$(VENV)/bin/stigmergy-slack --repo $${STIGMERGY_REPO:-../knowledge-repo} $(SLACK_ARGS)

retrieval-golden: venv ## Recall@5 per arm over evals/retrieval_golden.json (fake embedder; pass EMBEDDER=openai for the real measurement)
	$(PY) evals/run_retrieval.py --embedder $(or $(EMBEDDER),fake) $(RETRIEVAL_ARGS)

# The LOCAL index, with the REAL embedder. `make test` leaves `pages_index` built by the fake one
# (`fake-hashed-bow-256`), which answers nothing usefully — so a dogfood session or a Slack bot run
# needs this first. Same env-file reason as the targets above: OPENAI_API_KEY reaches a target, not
# a bare shell. `EMBEDDER=fake` for a keyless rebuild.
index-rebuild: venv ## Rebuild the LOCAL index from $STIGMERGY_REPO with the real embedder (needs the env file's OPENAI_API_KEY)
	$(VENV)/bin/stigmergy-index --rebuild --repo $${STIGMERGY_REPO:-../knowledge-repo} --embedder $(or $(EMBEDDER),openai) $(INDEX_ARGS)

index-check: venv ## Lint the LIVE index: exit 1 on any ERROR finding
	$(VENV)/bin/stigmergy-index --check --repo $${STIGMERGY_REPO:-../knowledge-repo}

# The real half of the golden QA set: the real embedder AND the real model, which is the only
# instrument that runs through `AnswerBrain.search_text` and can therefore detect a regression from
# entity-first resolution — the retrieval golden calls `index.search_arms` directly and is
# structurally incapable of moving for that change.
#
# `--repo` is what makes that true rather than merely intended. Without it `run_qa` builds its
# Settings with an empty `entity_registry_path`, the alias map comes back empty, and entity-first
# resolution is INERT for the whole measurement — the one mechanism this target exists to guard.
# The frozen corpus carries its own `ops/entity-registry.json` so the resolution has something to
# resolve against.
qa-golden: venv ## Golden QA with the REAL embedder + model (needs the env file)
	$(PY) evals/run_qa.py --embedder $(or $(EMBEDDER),openai) --llm $(or $(LLM),openai) \
	  --repo $(or $(QA_REPO),evals/corpus) $(QA_ARGS)

adversarial: venv ## The armed adversarial categories (1 injection · 2 ACL/existence · 7 forged frontmatter)
	$(PY) -m pytest -q -k "adversarial_cat1 or adversarial_cat2 or adversarial_cat7" -p no:cacheprovider --no-cov

gates: venv ## The release gates: adversarial + retrieval R@5>=0.80 + QA honesty>=0.90/groundedness>=0.84 (needs the env file)
	$(PY) evals/run_gates.py

# `--stage` on purpose: staged secrets land with the NEXT deploy instead of triggering one of their
# own, so `make slack-secrets && make deploy-staging` is one rollout rather than two. The values
# come from the env file and are never echoed — the operator runbook's `fly secrets set` lines stay
# the manual alternative for anyone who would rather paste them.
# The Fly app name comes from fly.toml, so there is ONE place to change it; FLY_APP
# overrides for a second environment.
FLY_APP_NAME := $(or $(FLY_APP),$(shell sed -n 's/^app = "\(.*\)"/\1/p' fly.toml))

slack-secrets: ## Stage the three Slack secrets on Fly from the env file (applied by the next deploy)
	@test -n "$(SLACK_APP_TOKEN)" || { echo "slack-secrets: SLACK_APP_TOKEN is not set — see docs/reference/operator-runbook.md"; exit 2; }
	@test -n "$(SLACK_BOT_TOKEN)" || { echo "slack-secrets: SLACK_BOT_TOKEN is not set"; exit 2; }
	@test -n "$(SLACK_TEAM_ID)"   || { echo "slack-secrets: SLACK_TEAM_ID is not set"; exit 2; }
	@fly secrets set --stage --app $(FLY_APP_NAME) \
	  SLACK_APP_TOKEN="$(SLACK_APP_TOKEN)" SLACK_BOT_TOKEN="$(SLACK_BOT_TOKEN)" \
	  SLACK_TEAM_ID="$(SLACK_TEAM_ID)" >/dev/null && echo "staged 3 Slack secrets (applied on the next deploy)"

deploy-staging: ## Bake the knowledge repo's ops/ files from $STIGMERGY_REPO and `fly deploy`
	bash scripts/deploy_staging.sh

rebuild-staging: venv ## Rebuild the STAGING index (needs STAGING_DSN + OPENAI_API_KEY, e.g. from the local env file)
	@test -n "$(STAGING_DSN)" || { echo "rebuild-staging: STAGING_DSN is not set (put it in the gitignored local env file; deliberately NOT STIGMERGY_INDEX_DSN so 'make test' can never point at staging)"; exit 2; }
	$(VENV)/bin/stigmergy-index --rebuild --repo $${STIGMERGY_REPO:-../knowledge-repo} --dsn "$(STAGING_DSN)"

r2-smoke: venv ## R2 smoke check: put+get+delete one object (needs R2_ENDPOINT_URL/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/R2_BUCKET)
	$(PY) scripts/r2_smoke.py

.PHONY: help venv test lint db-up db-down e2e e2e-write e2e-librarian e2e-librarian-container librarian-walk librarian-status slack-run slack-secrets retrieval-golden index-rebuild index-check qa-golden adversarial gates deploy-staging rebuild-staging r2-smoke
