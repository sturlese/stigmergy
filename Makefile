.DEFAULT_GOAL := help

-include .env
export
export STAGING_DSN

VENV := .venv
PY := $(VENV)/bin/python
STAMP := $(VENV)/.deps-ok
help: ## Show available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(firstword $(MAKEFILE_LIST)) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

$(STAMP): pyproject.toml uv.lock
	rm -rf $(VENV)
	uv sync --python 3.12 --frozen --extra dev
	touch $(STAMP)

venv: $(STAMP) ## Bootstrap the virtual environment

test: venv ## Run the complete keyless suite
	$(PY) -m pytest -q --cov-fail-under=75

test-system: venv ## Run the real Postgres and Git acceptance paths
	$(PY) -m pytest -q --no-cov tests/knowledge tests/bridge tests/slack tests/admin tests/ops

lint: venv ## Run Ruff over active code and tests
	$(VENV)/bin/ruff check src tests evals scripts

db-up: ## Start local Postgres and MinIO
	docker compose up -d --wait

db-down: ## Stop the local stack and delete its test volumes
	docker compose down -v

retrieval-golden: venv ## Measure hybrid retrieval over the canonical corpus
	$(PY) evals/run_retrieval.py --embedder $(or $(EMBEDDER),fake) --rebuild \
	  --repo evals/corpus $(RETRIEVAL_ARGS)

qa-golden: venv ## Measure answer quality over the canonical corpus
	$(PY) evals/run_qa.py --embedder $(or $(EMBEDDER),fake) --llm $(or $(LLM),fake) \
	  --rebuild --repo evals/corpus $(QA_ARGS)

adversarial: venv ## Run the armed adversarial categories
	$(PY) -m pytest -q -k "adversarial_cat1 or adversarial_cat2 or adversarial_cat7" \
	  -p no:cacheprovider --no-cov

gates: venv ## Run measured release gates with real model credentials
	$(PY) evals/run_gates.py

index-rebuild: venv ## Rebuild the local index from the configured knowledge repository
	$(VENV)/bin/stigmergy-index --rebuild --repo $${STIGMERGY_REPO:-../stigmergy-brain} \
	  --embedder $(or $(EMBEDDER),openrouter) $(INDEX_ARGS)

deploy-staging: venv ## Deploy all staging process groups
	bash scripts/deploy_staging.sh

rebuild-staging: venv ## Rebuild the staging index from repository HEAD
	@set -eu; test -n "$${STAGING_DSN}" || { echo "STAGING_DSN is required" >&2; exit 2; }; refresh_record="$$(bash scripts/refresh_staging_checkout.sh "$${STIGMERGY_REPO:-../stigmergy-brain}")"; case "$$refresh_record" in staging-refresh:\ root=*\ head=*) ;; *) echo "staging-rebuild: invalid checkout refresh record" >&2; exit 2 ;; esac; root="$${refresh_record#staging-refresh: root=}"; sha="$${root##* head=}"; root="$${root% head=*}"; if [ -z "$$root" ] || [ -z "$$sha" ] || ! git -C "$$root" rev-parse --verify --quiet "$$sha^{commit}" >/dev/null 2>&1 || [ "$$(git -C "$$root" rev-parse --verify --quiet 'HEAD^{commit}' 2>/dev/null)" != "$$sha" ]; then echo "staging-rebuild: invalid checkout refresh record" >&2; exit 2; fi; STIGMERGY_INDEX_DSN="$${STAGING_DSN}" $(VENV)/bin/stigmergy-index --rebuild --repo "$$root"

r2-smoke: venv ## Verify put, get, and delete against the configured evidence store
	$(PY) scripts/r2_smoke.py

.PHONY: help venv test test-system lint db-up db-down retrieval-golden qa-golden \
	adversarial gates index-rebuild deploy-staging rebuild-staging r2-smoke
