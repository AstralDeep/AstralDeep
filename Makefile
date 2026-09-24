# Developer and CI entry points for AstralDeep: compose lifecycle, containerized backend, web, and
# projection tests, lint, secret scan, and release evidence. Wraps scripts/*.py and
# AstralProjection's tooling.

.DEFAULT_GOAL := help

# CURDIR's drive letter differs per shell (Cygwin/MSYS/native)
DRIVE_LETTERS := a b c d e f g h i j k l m n o p q r s t u v w x y z                  A B C D E F G H I J K L M N O P Q R S T U V W X Y Z
UNIX_PWD := $(patsubst /cygdrive/%,/%,$(CURDIR))
PWD_DRIVE := $(firstword $(subst /, ,$(UNIX_PWD)))
ifeq ($(filter $(PWD_DRIVE),$(DRIVE_LETTERS)),)
HOST_PWD := $(CURDIR)
else
HOST_PWD := $(PWD_DRIVE):$(patsubst /$(PWD_DRIVE)%,%,$(UNIX_PWD))
endif

export MSYS_NO_PATHCONV := 1
export MSYS2_ARG_CONV_EXCL := *

.PHONY: help up down restart apply-config build ps logs logs-db shell psql \
        bootstrap composition-preflight sync sync-backend sync-components \
        test test-backend test-web test-projection check-060-selection test-060 \
        lint lint-backend lint-web secret-scan \
        prepare-release-evidence

PYTHON ?= python
NODE ?= node
WEB_CI := components/AstralProjection/tooling/web-ci
PROJECTION := components/AstralProjection
COMPONENT_INSTALLER := $(PYTHON) scripts/install_local_components.py
COMPOSITION_VERIFIER := $(PYTHON) scripts/verify_composition.py
COMPONENT_BUILD_TOOLS := setuptools==83.0.0 wheel==0.45.1 hatchling==1.27.0 uv_build==0.12.15

FEATURE_060_FOCUSED_TESTS := \
	tests/test_release_contract_schemas.py \
	tests/test_staging_fixtures_060.py \
	tests/test_documentation_060.py \
	tests/test_status_lifecycle_060.py \
	tests/test_ui_protocol_manifest.py \
	tests/test_quickstart_commands.py \
	tests/test_ci_javascript_lint.py

FEATURE_060_TEST_CONTAINER := docker run --rm \
	-e PYTHONDONTWRITEBYTECODE=1 \
	-v "$(HOST_PWD):/app:ro" \
	-w /app/backend \
	astraldeep:latest

RELEASE_COVERAGE_FLAGS ?= \
	--backend-python build/060/coverage/backend.xml \
	--voice-worker-python build/065/coverage/voice-worker.xml \
	--tooling-python build/060/coverage/tooling-python.xml

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

logs: ## Follow logs for the astraldeep container
	docker compose logs -f astraldeep

logs-db: ## Follow logs for the postgres container
	docker compose logs -f postgres

shell: ## Open a bash shell inside the astraldeep container
	docker exec -it astraldeep bash

psql: ## Open psql against the postgres container (uses DB_USER/DB_NAME from .env)
	docker exec -it astraldeep-postgres psql -U $$DB_USER -d $$DB_NAME

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

test-backend: ## Run pytest inside the astraldeep container
	docker exec astraldeep bash -c "cd /app/backend && python -m pytest -q"

check-060-selection: ## Collect the focused 060 setup/contract suite; empty selection fails
	$(FEATURE_060_TEST_CONTAINER) python -m pytest -p no:cacheprovider --collect-only -q $(FEATURE_060_FOCUSED_TESTS)

test-060: check-060-selection ## Run the focused 060 setup/contract suite
	$(FEATURE_060_TEST_CONTAINER) python -m pytest -p no:cacheprovider -q $(FEATURE_060_FOCUSED_TESTS)

WEB_SPECS := \
  tests/continuity-contract-060.spec.js tests/voice-conversation-065.spec.js \
  tests/persistent-agents-079.spec.js tests/offline-worker-088.spec.js \
  tests/canvas-review-088.spec.js tests/native-export-088.spec.js \
  tests/work-reads-088.spec.js tests/guidance-notes-088.spec.js \
  tests/workspace-topbar-088.spec.js tests/native-chart-088.spec.js \
  tests/first-task-088.spec.js tests/selection-088.spec.js

PLAYWRIGHT_IMAGE = $(shell cat $(WEB_CI)/playwright-image.txt)

test-web: ## Run the web client's browser suites in the pinned Playwright image
	docker run --rm --volume "$(HOST_PWD)/$(PROJECTION):/workspace" \
	  --workdir /workspace/tooling/web-ci "$(PLAYWRIGHT_IMAGE)" \
	  node node_modules/@playwright/test/cli.js test $(WEB_SPECS) \
	  --browser=chromium --workers=1

PROJECTION_GITDIR := $(wildcard .git/modules/AstralProjection)
ifeq ($(PROJECTION_GITDIR),)
PROJECTION_GIT_MOUNT :=
else
PROJECTION_GIT_MOUNT := -v "$(HOST_PWD)/.git/modules/AstralProjection:/.git/modules/AstralProjection:ro"
endif

test-projection: ## Run the AstralProjection Python suite in the product image
	docker run --rm -e PYTHONDONTWRITEBYTECODE=1 $(PROJECTION_GIT_MOUNT) 	  -v "$(HOST_PWD)/$(PROJECTION):/components/AstralProjection:ro" 	  -w /components/AstralProjection 	  astraldeep:latest python -m pytest tests/ -q -p no:cacheprovider

test: test-backend test-projection test-web ## Run all tests

prepare-release-evidence: ## Collect, normalize, and parse local release evidence (diagnostic, BASE_SHA required)
	python3 scripts/prepare_release_evidence.py --base-sha "$${BASE_SHA}" --candidate-sha "$$(git rev-parse HEAD)" $(RELEASE_COVERAGE_FLAGS) --repository-profile deep --coverage-mode strict

lint-backend: ## Run ruff from the repo root (ruff is NOT in the image; ruff.toml lives here — matches ci.yml)
	ruff check .

lint-web: ## Run the tracked ESLint config over the web client (matches ci.yml)
	cd $(PROJECTION) && $(NODE) tooling/web-ci/node_modules/eslint/bin/eslint.js \
	  --config tooling/web-ci/eslint.config.mjs --max-warnings=0 \
	  "backend/webrender/static/**/*.js" "tooling/web-ci/**/*.mjs" "tooling/web-ci/**/*.js"

lint: lint-backend lint-web ## Run all linters

GITLEAKS_VERSION ?= 8.30.1
GITLEAKS_SHA256 ?= 551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb

secret-scan: ## Scan the complete git history for secrets (pinned gitleaks, in a container)
	docker run --rm -v "$(HOST_PWD):/repo" alpine:3 sh -c '\
	  set -e; \
	  apk add --no-cache curl tar git >/dev/null; \
	  curl -sSL -o /tmp/g.tgz \
	    "https://github.com/gitleaks/gitleaks/releases/download/v$(GITLEAKS_VERSION)/gitleaks_$(GITLEAKS_VERSION)_linux_x64.tar.gz"; \
	  echo "$(GITLEAKS_SHA256)  /tmp/g.tgz" | sha256sum -c -; \
	  mkdir -p /tmp/gl && tar -xzf /tmp/g.tgz -C /tmp/gl; \
	  git config --global --add safe.directory /repo; \
	  /tmp/gl/gitleaks git --redact --config /repo/.gitleaks.toml \
	    --gitleaks-ignore-path /repo/.gitleaksignore --log-opts="--all" /repo'

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'
