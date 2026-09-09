PYTHON ?= python3
QUALITY_PATHS := app tests scripts tools
export COVERAGE_FILE ?= /tmp/project-intelligence-ingestion.coverage

.PHONY: check format format-check imports lint pre-commit test typecheck

check: format-check lint typecheck imports test

format:
	$(PYTHON) -m ruff format $(QUALITY_PATHS)

format-check:
	$(PYTHON) tools/check_ruff_ratchet.py format

lint:
	$(PYTHON) tools/check_ruff_ratchet.py lint

typecheck:
	$(PYTHON) tools/check_mypy_ratchet.py

imports:
	lint-imports

test:
	$(PYTHON) -m pytest --cov=app --cov-report=term-missing

pre-commit:
	$(PYTHON) -m pre_commit run --all-files
