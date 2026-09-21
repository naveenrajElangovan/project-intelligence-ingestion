PYTHON ?= python3
LINT_IMPORTS ?= $(dir $(PYTHON))lint-imports
QUALITY_PATHS := app tests scripts tools
export COVERAGE_FILE ?= /tmp/project-intelligence-ingestion.coverage

.PHONY: check dependencies format format-check imports lint pre-commit test typecheck

check: format-check lint typecheck imports dependencies test

format:
	$(PYTHON) -m ruff format $(QUALITY_PATHS)

format-check:
	$(PYTHON) -m ruff format --check $(QUALITY_PATHS)

lint:
	$(PYTHON) -m ruff check $(QUALITY_PATHS)

typecheck:
	$(PYTHON) -m mypy app

imports:
	$(LINT_IMPORTS) --no-cache

dependencies:
	$(PYTHON) -m deptry . --exclude '.venv|\.git|corpora|\.quality-runs' --optional-dependencies-dev-groups dev --per-rule-ignores 'DEP002=uvicorn,DEP004=pytest'

test:
	$(PYTHON) -m pytest --cov=app --cov-report=term-missing

pre-commit:
	$(PYTHON) -m pre_commit run --all-files
