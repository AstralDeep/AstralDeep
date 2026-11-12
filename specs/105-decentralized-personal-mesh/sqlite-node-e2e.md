# Personal node on SQLite: suites and end-to-end boot

**Date:** 2026-11-12. **Branch:** `feat/105-sqlite-node`. Local only, nothing pushed. This is a
local test observation, not protected CI and not live acceptance.

## Backend suites

Run with `scripts/run_backend_web_tests.py --group <group>` inside the isolated runner image
(`ad105-ci-tests:local`, PostgreSQL 17 fixture `ad105-codex-pg`), source root is the worktree with
the pinned components initialized.

| Group | Collected | Skipped | Failed | Notes |
|---|---|---|---|---|
| `tests_1` | 11,853 | 3 | 1 | 11,849 passed, 5 passing subtests |
| `tests_2` | 11,457 | 13 | 0 | 11,444 passed |
| `persistent_agents` | 1,008 | 0 | 0 | |
| `modules` | 8,996 | 17 | 9 | 2,037 of these are `scripts/tests` |

Root total: 23,310 collected, 16 skipped, 1 failed, so 23,293 passed. Codex recorded 23,248 passed and
16 skipped at `236354d0`. The 45 extra passes are tests added since.

### Failures and baselines

| Failure | Cause | Also fails on `c4536a83` |
|---|---|---|
| `test_voice_dependency_locks_065::test_windows_livekit_direct_pin_and_win_amd64_wheel_are_exact` | Projection `1dd06b8` adds `astral-rote==0.1.0` to `windows-client/requirements.in` and not to `requirements-release.lock.txt`. Deep's pin of that Projection commit exposes it. | No. Baseline pins Projection `ca7ba29`, where input and lock agree. Needs a Projection lock regeneration on a Windows-compatible resolver. Left failing. |
| `security_benchmark ... test_real_dispatch` (2 tests) | The runner mounts the source read-only and the tests write `backend/verification/.runs`. | Same environment fault. Both pass with a writable runs directory. |
| `test_install_doctor::test_real_checkout_composition_and_contract_pass`, `test_verify_composition::test_current_composition_has_exact_pins_canonical_urls_and_contracts`, `test_verify_composition::test_real_checkout_verification_executes_only_local_git_commands` | Need initialized submodules and canonical `origin` URLs. CI deselects all three. | Same. All three pass on the host with the worktree's submodules initialized. |
| `test_local_component_contract::test_local_component_contract_matches_manifest_and_package_metadata` | The runner image has `astralplane 0.1.7` installed and the test reads the installed version. | Image fault. Passes in the image built from this branch (0.1.12). |
| `test_verify_project_schema::test_pinned_project_registry_matches_qualified_digest`, `test_verify_operation_schema_088::test_exact_pinned_operation_and_assignment_files_match_current_digest`, `test_clean_composition_checkout::test_clean_checkout_initializes_exact_offline_composition` | Regression from the Plane merge. The composition verifier did not know the two new Plane schema modules (`mesh_event_schema`, `run_ledger_schema`) and their f-string statements. Two tests held the old digest. | No. Fixed in `1d5cb8dc`. The verifier now computes the PostgreSQL registry digest `dde30296...5cb27`, equal to `config/astral-composition.json`. |

## Node boot on SQLite

Harness: `evidence/sqlite-node/`. Image `sqlite-node-lane:105` is built from this worktree with
`docker build`. Three containers share a private network and nothing else is reachable.

- `sn105-issuer` serves an RS256 JWKS at `/realms/astral/protocol/openid-connect/certs` and mints
  tokens (`issuer.py`). The node trusts it through `KEYCLOAK_AUTHORITY`. Mock auth is off.
- `sn105-model` is a deterministic OpenAI-compatible server (`fakemodel.py`). It calls the named tool
  once and then answers from the tool result.
- `sn105-node` runs with `ASTRAL_NODE_PROFILE=personal`, `DATABASE_URL=sqlite:////node-data/plane.sqlite3`
  on a Docker volume, and `EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS=sn105-model`.

Facts from `report-stock.txt`:

- `/healthz` and `/readyz` return ok with `db: ok` and nine in-process agents.
- The SQLite file has 151 tables, schema `089.022` and migration digest
  `e4ae392e...f6ef5`, the value pinned in `config/astral-composition.json`.
- No token and a forged token get 401. An owner token minted by the local issuer gets 200.
- The owner saves a provider through the chrome save frame, creates a chat, grants `tools:read` to
  `general-1`, and sends a message. The node calls the model, dispatches `generate_dynamic_chart`
  through the normal permission and dispatch path, and sends the result back to the model.

### Result

With the pinned Plane (`cbf621a`), the tool runs and the turn then fails when it persists the reply:
`SQLite write lock was not available in time`. It reproduced on every run (`report-stock.txt`).

With one line changed in Plane (`evidence/sqlite-node/plane-sqlite-begin.patch`) the whole turn
completes: user message, tool result with `_source_tool: generate_dynamic_chart`, final model text,
operation `completed` (`report-plane-fix.txt`).

## What blocks a clean SQLite node

Both items are in AstralPlane `feat/105-plane-sqlite` at `cbf621a`, not in Deep.

1. `src/astralplane/database/sqlite.py`, `SQLiteConnection.begin_if_needed`. A transaction that starts
   with a plain SELECT begins as `BEGIN DEFERRED`. A later write calls `reserve_writer`, which runs
   `UPDATE schema_meta SET value=value WHERE 0`. When another connection commits in between, SQLite
   returns `database is locked` at once, because the read snapshot is stale, and the busy timeout does
   not apply. Deep's `fenced_transaction` reads the operation row first and then locks it with
   `FOR UPDATE`, so a chat turn hits this whenever any other thread commits. The patch begins every
   read-write transaction as `BEGIN IMMEDIATE`. It serializes read-write transactions, which suits a
   one-owner node. A retry on this error class would be the alternative.
2. `src/astralplane/repositories/tool_policy.py:185`, `list_scoped_agent_owners_for_administration`
   uses `RIGHT(agent_id, length(?))`. SQLite has no `RIGHT`. Two startup sweeps log
   `no such function: RIGHT` (orphaned-draft permission sweep and legacy directory-id ownership
   sweep). They are non-fatal. Registering `right` in `sqlite_functions.py` fixes both.

## Deep fix made on the way

The orchestrator expected `backend/knowledge` to exist, as the compose bind mount provides. A node
started with `docker run` had no such directory, the first chat turn failed with
`skill_lookup_unavailable`, and the turn never reached the model. `user_skills.store_for` now creates
the directory with mode 0700 (`4cc12578`).

## Commands

```
docker build -t sqlite-node-lane:105 .
evidence/sqlite-node/run-variant.sh stock
PLANE_SQLITE_PATCHED=<patched sqlite.py> evidence/sqlite-node/run-variant.sh plane-fix
```

The node container needs no PostgreSQL. `docker exec sn105-node python` against
`file:/node-data/plane.sqlite3?mode=ro` shows the schema rows quoted above.
