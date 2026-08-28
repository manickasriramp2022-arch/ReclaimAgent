# ReclaimAgent
#
# `make demo` is the one command a reviewer needs: it generates a synthetic
# batch, runs the recovery pipeline plus the naive baseline, verifies the audit
# trail, renders the HTML report and opens it. Fully offline.

PY      ?= python3
VENV    ?= .venv
BIN     := $(VENV)/bin
SEED    ?= 42
SIZE    ?= 250
SEEDS   ?= 30
BATCH   := data/batch_$(SEED).jsonl

.DEFAULT_GOAL := help
.PHONY: help venv install demo generate run benchmark report replay verify queue test lint types check fmt clean distclean ci

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

$(BIN)/python:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install --quiet --upgrade pip

venv: $(BIN)/python ## Create the virtualenv

install: venv ## Install the package and dev dependencies
	@$(BIN)/pip install --quiet -e ".[dev]"

demo: install ## Generate, run, sweep, verify, report, open (the whole pitch, offline)
	@echo "=== 1/5  generating a synthetic test-mode batch ==="
	@$(BIN)/reclaim generate --seed $(SEED) --size $(SIZE)
	@echo ""
	@echo "=== 2/5  running the recovery pipeline and the naive baseline ==="
	@$(BIN)/reclaim run --batch $(BATCH) --no-llm
	@echo ""
	@echo "=== 3/5  is the delta real, or one lucky batch? sweeping $(SEEDS) seeds ==="
	@$(BIN)/reclaim benchmark --seeds $(SEEDS) --size $(SIZE) 2>&1 | tail -12
	@echo ""
	@echo "=== 4/5  verifying the audit trail ==="
	@$(BIN)/reclaim verify-audit
	@echo ""
	@echo "=== 5/5  rendering the report ==="
	@$(BIN)/reclaim report --open

generate: install ## Generate a batch (SEED=42 SIZE=250)
	@$(BIN)/reclaim generate --seed $(SEED) --size $(SIZE)

run: install ## Run the pipeline over the generated batch
	@$(BIN)/reclaim run --batch $(BATCH) --no-llm

run-llm: install ## Run with the Anthropic classifier fallback enabled
	@$(BIN)/reclaim run --batch $(BATCH) --llm

benchmark: install ## Re-run the baseline comparison across SEEDS seeds
	@$(BIN)/reclaim benchmark --seeds $(SEEDS) --size $(SIZE)

report: install ## Render the HTML report for the latest run
	@$(BIN)/reclaim report

verify: install ## Verify the audit log and recompute every metric from it
	@$(BIN)/reclaim verify-audit

queue: install ## Print the human escalation queue
	@$(BIN)/reclaim queue

replay: install ## Replay a case (CASE=@success or CASE=@stopped or a case id)
	@$(BIN)/reclaim replay --case $(or $(CASE),@success)

test: install ## Run the test suite
	@$(BIN)/python -m pytest

cov: install ## Run the test suite with coverage
	@$(BIN)/python -m pytest --cov=reclaim --cov-report=term-missing

lint: install ## Lint
	@$(BIN)/ruff check src tests
	@$(BIN)/ruff format --check src tests

fmt: install ## Format
	@$(BIN)/ruff check --fix src tests
	@$(BIN)/ruff format src tests

types: install ## Type-check
	@$(BIN)/mypy

check: lint types test ## Lint, type-check and test

ci: check ## Everything CI runs, including verify-audit and the seed sweep
	@$(BIN)/reclaim generate --seed 1234 --size 120
	@$(BIN)/reclaim run --batch data/batch_1234.jsonl --no-llm
	@$(BIN)/reclaim verify-audit
	@$(BIN)/reclaim benchmark --seeds 12 --size 120
	@$(BIN)/reclaim report

clean: ## Remove generated data, logs and reports
	rm -rf out/*.jsonl out/*.json out/*.html out/*.txt data/*.jsonl
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage

distclean: clean ## Also remove the virtualenv
	rm -rf $(VENV) *.egg-info src/*.egg-info
