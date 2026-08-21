.DEFAULT_GOAL := help

# Path translation for Docker volume mounts under Cygwin / Git Bash / native.
# Docker CLI on Windows wants Y:/... not /cygdrive/y/... — cygpath -m and `pwd -W`
# both yield that form; native shells fall through to $(CURDIR).
HOST_PWD := $(shell cygpath -m "$(CURDIR)" 2>/dev/null || pwd -W 2>/dev/null || echo "$(CURDIR)")

.PHONY: help up down restart apply-config build ps logs logs-db shell psql \
        bootstrap composition-preflight sync sync-backend sync-components \
        test test-backend check-060-selection test-060 lint lint-backend \
        prepare-release-evidence

PYTHON ?= python
COMPONENT_INSTALLER := $(PYTHON) scripts/install_local_components.py
COMPOSITION_VERIFIER := $(PYTHON) scripts/verify_composition.py
COMPONENT_BUILD_TOOLS := setuptools==80.9.0 wheel==0.45.1 hatchling==1.27.0 uv_build==0.11.21

FEATURE_060_FOCUSED_TESTS := \
	tests/test_release_contract_schemas.py \
	tests/test_staging_fixtures_060.py \
	tests/test_documentation_060.py \
	tests/test_status_lifecycle_060.py \
	tests/test_ui_protocol_manifest.py \
	tests/test_quickstart_commands.py \
	tests/test_ci_javascript_lint.py

# The running product container intentionally mounts only mutable backend data,
# not the repository root. Feature-060 contract tests also inspect tracked
# specs/scripts/workflows, so run that lane in the same image with a read-only
# full-tree mount instead of assuming those paths exist inside `astraldeep`.
FEATURE_060_TEST_CONTAINER := docker run --rm \
	-e PYTHONDONTWRITEBYTECODE=1 \
	-v "$(HOST_PWD):/app:ro" \
	-w /app/backend \
	astraldeep:latest

# Canonical local producer outputs consumed by the mandatory pre-push
# diagnostic. Missing inputs fail closed; callers may override this variable
# only to point at equivalent freshly generated reports.
RELEASE_COVERAGE_FLAGS ?= \
	--backend-python build/060/coverage/backend.xml \
	--voice-worker-python build/065/coverage/voice-worker.xml \
	--tooling-python build/060/coverage/tooling-python.xml \
	--windows-python build/060/coverage/windows.xml \
	--javascript build/060/coverage/web-istanbul.json \
	--android-app build/060/coverage/android-app.xml \
	--android-core build/060/coverage/android-core.xml \
	--ios build/060/coverage/apple-ios-xccov.json \
	--macos build/060/coverage/apple-macos-xccov.json \
	--watchos build/060/coverage/apple-watchos-xccov.json

## ---------- Lifecycle ----------

bootstrap: ## Initialize exact submodules and install their locked wheels into the active Python environment
	git submodule sync --recursive
	git submodule update --init --recursive
	$(MAKE) composition-preflight
	$(PYTHON) -m pip install --disable-pip-version-check --no-cache-dir $(COMPONENT_BUILD_TOOLS)
	$(PYTHON) -m pip install --disable-pip-version-check --no-cache-dir -r backend/requirements.txt
	$(COMPONENT_INSTALLER) sync --root .

composition-preflight: ## Fail closed unless all four initialized component pins/contracts are exact and clean
	$(COMPOSITION_VERIFIER) --root .
	$(COMPONENT_INSTALLER) validate --root . --require-gitlinks

up: composition-preflight ## Build images and start all containers in the background
	docker compose up -d --build

down: ## Stop containers (volumes preserved)
	docker compose down

restart: ## Restart the astraldeep app container
	docker compose restart astraldeep

apply-config: ## Recreate the app with boot-time config and report the safe BYO flag
	docker compose up -d --force-recreate astraldeep
	docker compose exec -T astraldeep python -c 'import os; value = os.getenv("FF_BYO_AGENTS", "false").strip().lower(); print("Effective FF_BYO_AGENTS=" + ("true" if value in {"1", "true", "yes"} else "false"))'

build: composition-preflight ## Build images without starting containers
	docker compose build

ps: ## Show container status
	docker compose ps

## ---------- Observability ----------

logs: ## Follow logs for the astraldeep container
	docker compose logs -f astraldeep

logs-db: ## Follow logs for the postgres container
	docker compose logs -f postgres

## ---------- Shells ----------

shell: ## Open a bash shell inside the astraldeep container
	docker exec -it astraldeep bash

psql: ## Open psql against the postgres container (uses DB_USER/DB_NAME from .env)
	docker exec -it astraldeep-postgres psql -U $$DB_USER -d $$DB_NAME

## ---------- Sync (no host toolchain; everything runs in containers) ----------

sync-backend: ## Tar backend source via an alpine container, copy into astraldeep, restart
	docker run --rm -v "$(HOST_PWD)/backend:/src:ro" alpine:3 tar \
	  --exclude='.venv' --exclude='__pycache__' --exclude='data' --exclude='tmp' \
	  --exclude='*.pyc' -C /src -cf - . \
	  | docker cp - astraldeep:/app/backend/
	docker compose restart astraldeep

sync-components: composition-preflight ## Rebuild/recreate the app so exact local component wheels stay in sync
	docker compose build astraldeep
	docker compose up -d --no-deps --force-recreate astraldeep

sync: sync-components ## Sync the complete composed backend and exact component wheels

## ---------- Tests ----------

test-backend: ## Run pytest inside the astraldeep container
	docker exec astraldeep bash -c "cd /app/backend && python -m pytest -q"

check-060-selection: ## Collect the focused 060 setup/contract suite; empty selection fails
	$(FEATURE_060_TEST_CONTAINER) python -m pytest -p no:cacheprovider --collect-only -q $(FEATURE_060_FOCUSED_TESTS)

test-060: check-060-selection ## Run the focused 060 setup/contract suite
	$(FEATURE_060_TEST_CONTAINER) python -m pytest -p no:cacheprovider -q $(FEATURE_060_FOCUSED_TESTS)

test: test-backend ## Run all tests

## ---------- Release evidence (feature 060) ----------

# Deterministic local pre-push evidence command (T107). Diagnostic only: the
# emitted JSON always states protected_release_authorization: false and only
# the protected-decision GitHub job can produce a trusted release decision.
prepare-release-evidence: ## Collect, normalize, and parse local release evidence (diagnostic, BASE_SHA required)
	python3 scripts/prepare_release_evidence.py --base-sha "$${BASE_SHA}" --candidate-sha "$$(git rev-parse HEAD)" $(RELEASE_COVERAGE_FLAGS) --coverage-mode strict

## ---------- Lint ----------

lint-backend: ## Run ruff from the repo root (ruff is NOT in the image; ruff.toml lives here — matches ci.yml)
	ruff check .

lint: lint-backend ## Run all linters

## ---------- Help ----------

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
