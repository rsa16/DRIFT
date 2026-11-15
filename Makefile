UV ?= uv
PY := $(UV) run --frozen

.PHONY: setup check fix audit secret-scan

setup:
	$(UV) sync --frozen --all-extras
	git config core.hooksPath .githooks

check:
	$(PY) ruff format --check drift tests
	$(PY) ruff check drift tests
	$(PY) mypy drift tests
	$(PY) pytest -q
	@if [ -f web/package.json ]; then \
		pnpm -C web install --frozen-lockfile && \
		pnpm -C web check && \
		pnpm -C web build; \
	else \
		echo "check: web skipped (no web/package.json yet)"; \
	fi

fix:
	$(PY) ruff format drift tests
	$(PY) ruff check --fix drift tests

audit:
	@$(UV) export --frozen --no-emit-project --all-extras --format requirements-txt > .venv/audit-requirements.txt
	$(PY) pip-audit -r .venv/audit-requirements.txt

secret-scan:
	docker run --rm -v "$(CURDIR):/repo" ghcr.io/gitleaks/gitleaks:v8.30.1 \
		detect --source /repo --config /repo/.gitleaks.toml --redact --no-banner --exit-code 1
