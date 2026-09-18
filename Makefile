.DEFAULT_GOAL := help

# Path translation for Docker volume mounts under Cygwin / Git Bash / native.
#
# Docker CLI on Windows wants Y:/... and what make puts in CURDIR depends on which
# make is first on PATH: Cygwin's says /cygdrive/y/..., MSYS2's says /y/..., a native
# one says Y:/... already. cygpath translates for the flavour that shipped it, not
# the one that set CURDIR, and the ordinary Windows dev box has them mismatched --
# Cygwin make with Git's MSYS cygpath -- where `cygpath -m /cygdrive/y/WORK` returns
# Y:/Program Files/Git/cygdrive/y/WORK. Docker Desktop then creates that directory
# and mounts it empty, so every container target below runs against nothing and says
# so in whatever way that tool says "no files": 0 tests collected, 0 leaks found.
# make's own text functions cannot be mismatched, so use those and call nothing.
DRIVE_LETTERS := a b c d e f g h i j k l m n o p q r s t u v w x y z                  A B C D E F G H I J K L M N O P Q R S T U V W X Y Z
UNIX_PWD := $(patsubst /cygdrive/%,/%,$(CURDIR))
PWD_DRIVE := $(firstword $(subst /, ,$(UNIX_PWD)))
ifeq ($(filter $(PWD_DRIVE),$(DRIVE_LETTERS)),)
HOST_PWD := $(CURDIR)
else
HOST_PWD := $(PWD_DRIVE):$(patsubst /$(PWD_DRIVE)%,%,$(UNIX_PWD))
endif

# The mirror of the same hazard, on the other side of the colon: under an MSYS2
# make, MSYS rewrites arguments that look like absolute POSIX paths, so the
# container-side /workspace becomes Y:/Program Files/Git/workspace before docker
# sees it. These two say "leave my arguments alone" and are inert everywhere else.
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
COMPONENT_BUILD_TOOLS := setuptools==83.0.0 wheel==0.45.1 hatchling==1.27.0 uv_build==0.12.3

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
	--tooling-python build/060/coverage/tooling-python.xml

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

# The web client's own suites. These live in the Projection component and are
# what CI's `web` job runs; nothing in this file used to reach them, which is
# how twelve failing browser tests sat unnoticed on a feature branch. They need
# no stack: the browser suite drives client.js against a synthetic DOM, and the
# Python suite runs in the product image against a read-only mount.
# Invoked through node against the locked CLI rather than through npx: npx is
# a .cmd on Windows and does not survive a POSIX shell's PATH.
# The same twelve specs ci.yml runs, in the same order, in the same pinned
# image. Naming one of them was the mistake this target was made to stop: a
# lane that runs part of a suite reports green for the part nobody is looking
# at. The image is not incidental either -- several of these specs shell out
# to python3 with a POSIX PYTHONPATH, so they cannot run on a Windows host at
# all, and running them anywhere else would be running something other than
# what CI runs.
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

# Several of these tests shell out to git to ask what is tracked, and as a submodule
# this component's .git is a pointer -- "gitdir: ../../.git/modules/AstralProjection"
# -- at a directory the mount does not contain, so git says "not a git repository"
# and the test reads as a product failure. Mounting the component where that
# relative pointer expects to find it, plus the gitdir it names, makes both it and
# the module's own core.worktree resolve. Read-only: nothing here needs to write.
PROJECTION_GITDIR := $(wildcard .git/modules/AstralProjection)
ifeq ($(PROJECTION_GITDIR),)
PROJECTION_GIT_MOUNT :=
else
PROJECTION_GIT_MOUNT := -v "$(HOST_PWD)/.git/modules/AstralProjection:/.git/modules/AstralProjection:ro"
endif

test-projection: ## Run the AstralProjection Python suite in the product image
	docker run --rm -e PYTHONDONTWRITEBYTECODE=1 $(PROJECTION_GIT_MOUNT) 	  -v "$(HOST_PWD)/$(PROJECTION):/components/AstralProjection:ro" 	  -w /components/AstralProjection 	  astraldeep:latest python -m pytest tests/ -q -p no:cacheprovider

# Known: three of these fail in this image for reasons that are about the image and
# the host, not the code, and they fail on main too. test_transformation_record
# compares file modes, and a Docker bind mount from Windows reports every file 755.
# The two in test_resources build a wheel and read the offline cache, which needs
# `python -m build` and a network the product image has neither of. Do not deselect
# them; a suite nobody can read the result of is how twelve failing browser tests
# went unnoticed on a feature branch for a week. CI runs all three on a real
# checkout and they pass there.
test: test-backend test-projection test-web ## Run all tests

## ---------- Release evidence (feature 060) ----------

# Deterministic local pre-push evidence command (T107). Diagnostic only: the
# emitted JSON always states protected_release_authorization: false and only
# the protected-decision GitHub job can produce a trusted release decision.
prepare-release-evidence: ## Collect, normalize, and parse local release evidence (diagnostic, BASE_SHA required)
	python3 scripts/prepare_release_evidence.py --base-sha "$${BASE_SHA}" --candidate-sha "$$(git rev-parse HEAD)" $(RELEASE_COVERAGE_FLAGS) --repository-profile deep --coverage-mode strict

## ---------- Lint ----------

lint-backend: ## Run ruff from the repo root (ruff is NOT in the image; ruff.toml lives here — matches ci.yml)
	ruff check .

lint-web: ## Run the tracked ESLint config over the web client (matches ci.yml)
	cd $(PROJECTION) && $(NODE) tooling/web-ci/node_modules/eslint/bin/eslint.js \
	  --config tooling/web-ci/eslint.config.mjs --max-warnings=0 \
	  "backend/webrender/static/**/*.js" "tooling/web-ci/**/*.mjs" "tooling/web-ci/**/*.js"

lint: lint-backend lint-web ## Run all linters

## ---------- Secrets ----------

# The same scan ci.yml runs, at the same pinned version and checksum, over the
# complete history. It is here because the one thing that made this job fail
# for weeks -- a lookahead in a rule, which RE2 cannot compile, so gitleaks
# panicked before scanning a byte -- is invisible until something runs it.
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

## ---------- Help ----------

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'
