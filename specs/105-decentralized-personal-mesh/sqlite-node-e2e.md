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

With Plane `cbf621a` the tool ran and the turn then failed when it persisted the reply with
`SQLite write lock was not available in time`. It reproduced on every run (`report-stock.txt`). One
changed line in Plane made the whole turn complete (`report-plane-fix.txt`).

### Result with the pinned Plane `f8f2a63`

Plane `feat/105-plane-sqlite` at `f8f2a63` holds both fixes in the library. `f8f2a63` begins every
read-write transaction as `BEGIN IMMEDIATE` and `ddff26e` provides `RIGHT()` for SQLite. Deep pins that
revision in `config/astral-composition.json`. The PostgreSQL migration digest `dde30296...5cb27` and the
SQLite structure digest `5f45079d...4d0b` are unchanged. The SQLite migration digest is now
`a6681e87...9a84`. All three were computed through Plane's public facade and
`scripts/verify_composition.py`, which reports the five pins as exact.

`evidence/sqlite-node/run-pinned.sh` rebuilds the node image from this branch (`docker build -t
deepint-node:105 .`, Plane 0.1.12 installed in the image), then starts an issuer, the deterministic
model and the node on a private `--internal` Docker network and a fresh volume. No PostgreSQL is
reachable and the node has no `postgres` database URL. The output is `report-pinned.txt`:

- `/healthz` and `/readyz` are ok with `db: ok` and nine in-process agents.
- The SQLite file has 151 tables and the pinned migration digest in `schema_meta`.
- No token and a forged token get 401. The owner token gets 200.
- The owner saves a provider, creates a chat and grants `tools:read`. The turn ends with the operation
  `completed`. The saved chat holds the user message, the `generate_dynamic_chart` result with
  `_source_tool` set and the model's final answer `FAKE-MODEL-OK: tool result received (236 chars)`.
- The node log has zero `write lock was not available`, zero `database is locked`, zero `no such
  function` and zero tracebacks.

This is a local test observation on one machine, not protected CI.

## What blocked a clean SQLite node, now fixed in Plane

Both items were in AstralPlane `feat/105-plane-sqlite` at `cbf621a`, not in Deep.

1. `SQLiteConnection.begin_if_needed` began a read-write transaction that started with a SELECT as
   `BEGIN DEFERRED`. A later write found a stale read snapshot and failed at once with `database is
   locked`. Fixed by `f8f2a63`, which holds the writer from the first statement.
2. `list_scoped_agent_owners_for_administration` uses `RIGHT(agent_id, length(?))` and SQLite has no
   `RIGHT`. Two startup sweeps logged `no such function: RIGHT`. Fixed by `ddff26e`.

## Deep fix made on the way

The orchestrator expected `backend/knowledge` to exist, as the compose bind mount provides. A node
started with `docker run` had no such directory, the first chat turn failed with
`skill_lookup_unavailable`, and the turn never reached the model. `user_skills.store_for` now creates
the directory with mode 0700 (`4cc12578`).

## Commands

```
docker build -t deepint-node:105 .
evidence/sqlite-node/run-pinned.sh deepint-node:105 "$PWD/evidence/sqlite-node/report-pinned.txt"
```

The earlier stock and patched runs used `run-variant.sh stock` and `run-variant.sh plane-fix`.

The node container needs no PostgreSQL. `docker exec sn105-node python` against
`file:/node-data/plane.sqlite3?mode=ro` shows the schema rows quoted above.
