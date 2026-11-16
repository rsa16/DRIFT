UV ?= uv
PY := $(UV) run --frozen
DEMO_FIXTURE := build/demo-fixture
DEMO_SNAPSHOTS := build/demo-snapshots

.PHONY: setup check fix docs-check audit secret-scan demo-fixture demo-snapshot

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
		pnpm -C web test && \
		pnpm -C web build; \
	else \
		echo "check: web skipped (no web/package.json yet)"; \
	fi

fix:
	$(PY) ruff format drift tests
	$(PY) ruff check --fix drift tests

docs-check:
	$(PY) pytest -q tests/test_docs_links.py

demo-fixture:
	$(PY) python -m drift.ingest record --fixture $(DEMO_FIXTURE) \
		--cutout-size-deg 0.9

demo-snapshot:
	$(PY) python -m drift.ingest build --fixture $(DEMO_FIXTURE) \
		--snapshot-dir $(DEMO_SNAPSHOTS)

audit:
	@$(UV) export --frozen --no-emit-project --all-extras --format requirements-txt > .venv/audit-requirements.txt
	$(PY) pip-audit -r .venv/audit-requirements.txt

secret-scan:
	docker run --rm -v "$(CURDIR):/repo" ghcr.io/gitleaks/gitleaks:v8.30.1 \
		detect --source /repo --config /repo/.gitleaks.toml --redact --no-banner --exit-code 1
