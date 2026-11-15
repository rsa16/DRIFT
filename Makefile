PYTHON ?= python3
VENV := .venv
BIN := $(VENV)/bin
PY := $(BIN)/python

.PHONY: setup check fix audit secret-scan

setup:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install -e '.[test,dev]'
	git config core.hooksPath .githooks

check:
	$(BIN)/ruff format --check drift tests
	$(BIN)/ruff check drift tests
	$(BIN)/mypy drift tests
	$(BIN)/pytest -q
	@if [ -f web/package.json ]; then \
		pnpm -C web install --frozen-lockfile && \
		pnpm -C web check && \
		pnpm -C web build; \
	else \
		echo "check: web skipped (no web/package.json yet)"; \
	fi

fix:
	$(BIN)/ruff format drift tests
	$(BIN)/ruff check --fix drift tests

audit:
	@$(BIN)/pip freeze --exclude-editable | grep -v '^pip==' > $(VENV)/audit-requirements.txt
	$(BIN)/pip-audit -r $(VENV)/audit-requirements.txt

secret-scan:
	docker run --rm -v "$(CURDIR):/repo" ghcr.io/gitleaks/gitleaks:v8.30.1 \
		detect --source /repo --config /repo/.gitleaks.toml --redact --no-banner --exit-code 1
