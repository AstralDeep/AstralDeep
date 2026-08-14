# Tasks: Astral Multi-Repository Decomposition and LETS Integration

**Input**: Design documents in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/`

**Tests**: Required. This migration changes repository ownership, persistence, authorization, client release trust, and research claims. Narrow tests run with each cluster; broad local qualification is intentionally deferred to the final phase.

**Organization**: Tasks are grouped by independently testable user story. Projection and Plane extraction precede the composed-system story because a clean composition cannot be tested until both independent packages exist. Every pushed product checkpoint is followed by a separate curated kos-wiki commit and push.

**Repository path legend**:

- **Deep**: `Y:/WORK/MCP/AstralDeep`
- **Projection worktree**: `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074`
- **Plane worktree**: `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074`
- **Primitives**: `Y:/WORK/MCP/AstralPrimitives`
- **LETS**: `Y:/WORK/MCP/LETS`
- **Vault**: `Y:/WORK/kos-wiki`

## Phase 1: Setup (Migration Safety and Exact Baselines)

**Purpose**: Establish collision-safe branches, exact live baselines, and guards before replacing tracked content. Do not create archive tags or archive branches.

- [X] T001 Refresh all five product remotes without pruning and record exact live heads, defaults, visibility, workflow state, working-tree state, and immutable source SHAs in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/baseline.json`
- [X] T002 Verify the signed LETS `v1.0.10` tag object `5ed575066a0c61a51dc55278fa7412f60772fac7` peels to `82dbe4f5ddf410cc86778784bb612440725ec66d` and record the verification result in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/baseline.json`
- [X] T003 Inventory path names and sizes, but not contents, for ignored LETS paper/results, Plane database/logs, Projection dependencies, and Deep Android local signing configuration in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/local-state-continuity.md`
- [X] T004 [P] Implement exact-root, expected-branch, reparse-point, and clean-index validation in `Y:/WORK/MCP/AstralDeep/scripts/migration/preflight_074.ps1`
- [X] T005 [P] Implement a staged-path denylist for credentials, databases, logs, uploads, generated agents, local manuscript sources, and generated evidence in `Y:/WORK/MCP/AstralDeep/scripts/migration/check_staged_paths_074.py`
- [X] T006 [P] Add regression tests for root validation, reparse-point refusal, and sensitive staged-path refusal in `Y:/WORK/MCP/AstralDeep/scripts/tests/test_migration_guards_074.py`
- [X] T007 Normalize AstralDeep, LETS, and AstralPrimitives fetch/push origins to their canonical HTTPS URLs in `Y:/WORK/MCP/AstralDeep/.git/config`, `Y:/WORK/MCP/LETS/.git/config`, and `Y:/WORK/MCP/AstralPrimitives/.git/config`; fetch with `--no-prune`; verify branch collisions; retain Deep on `074-multirepo-lets-integration`; and create LETS `codex/074-astral-case-study` plus Primitives `codex/074-canonical-identity` from refreshed `main`
- [X] T008 Normalize AstralPlane fetch/push origin to `https://github.com/AstralDeep/AstralPlane.git` in `Y:/WORK/MCP/AstralPlane/.git/config`, fetch with `--no-prune`, and stop if the refreshed `master` differs from the baseline recorded in `execution/baseline.json`
- [X] T009 Verify AstralPlane legacy push workflows are disabled or inert, create and push `main` at refreshed `origin/master`, change the GitHub default to `main`, retain `master` unchanged, and record verified refs/workflow posture in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/baseline.json`
- [X] T010 Normalize AstralProjection origin to `https://github.com/AstralDeep/AstralProjection.git`, fetch with `--no-prune`, verify legacy push workflows are disabled or inert, create and push `main` at refreshed `origin/master`, change the GitHub default to `main`, retain `master` unchanged, and record verified refs/workflow posture in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/baseline.json`
- [X] T011 Create `codex/074-extract-data-plane` from Plane `main` in the new exact path `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074` after proving that the branch and path do not already exist
- [X] T012 Create `codex/074-extract-projection` from Projection `main` in the new exact path `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074` after proving that the branch and path do not already exist
- [X] T013 Record the ignored status of `Y:/WORK/MCP/AstralDeep/android-client/keystore.properties` and `local.properties`, retain both originals in place, and prohibit removal of their source directory until the copies in Projection pass ignore and signed-build checks in `execution/local-state-continuity.md`
- [X] T014 Record and push the baseline/default-branch checkpoint in `Y:/WORK/kos-wiki/wiki/synthesis-astral-repository-decomposition-lets-integration.md`, `Y:/WORK/kos-wiki/index.md`, and `Y:/WORK/kos-wiki/log.md`

---

## Phase 2: Foundational (Cross-Repository Contracts and Extraction Tooling)

**Purpose**: Establish shared contract files and one-way dependency guards required by every story.

**Critical**: No story implementation starts until these tasks pass.

- [X] T015 Define the canonical mutable-source ownership map for Deep, Projection, Plane, Primitives, and LETS in `Y:/WORK/MCP/AstralDeep/contracts/component-ownership.json`
- [X] T016 [P] Add a schema for extraction provenance, selected tracked paths, source blob IDs, legacy baseline, and manifest digest in `Y:/WORK/MCP/AstralDeep/contracts/extraction-provenance.schema.json`
- [X] T017 [P] Add the runtime composition schema from the planning contract in `Y:/WORK/MCP/AstralDeep/contracts/system-composition.schema.json`
- [X] T018 [P] Promote the planned case-study evidence contract to the implementation-owned schema at `Y:/WORK/MCP/AstralDeep/contracts/case-study-evidence.schema.json` without mutating LETS before its case-study phase
- [X] T019 Implement deterministic tracked-blob inventory and provenance-manifest generation in `Y:/WORK/MCP/AstralDeep/scripts/migration/build_extraction_manifest.py`
- [X] T020 [P] Add unit tests for canonical ordering, blob-ID capture, digest stability, omitted ignored files, and source-revision mismatch in `Y:/WORK/MCP/AstralDeep/scripts/tests/test_build_extraction_manifest.py`
- [X] T021 Implement machine-readable source-owner and duplicate-tree validation in `Y:/WORK/MCP/AstralDeep/scripts/verify_component_ownership.py`
- [X] T022 [P] Add owner-map, unmanaged-duplicate, generated-copy, and missing-owner tests in `Y:/WORK/MCP/AstralDeep/scripts/tests/test_verify_component_ownership.py`
- [X] T023 Create the independently installable AstralProjection package scaffold, package-data declarations, and inactive unprivileged CI definition in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/pyproject.toml`, `src/astralprojection/__init__.py`, and `workflows-disabled/ci.yml`
- [X] T024 Create the independently installable Python 3.11 AstralPlane package scaffold, public API boundary, and inactive unprivileged CI definition in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/pyproject.toml`, `src/astralplane/__init__.py`, and `workflows-disabled/ci.yml`
- [X] T025 [P] Add a Projection architecture test that forbids imports from AstralDeep orchestration, policy, persistence, and transport packages in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/tests/architecture/test_dependency_direction.py`
- [X] T026 [P] Add a Plane architecture test that forbids imports from AstralDeep, AstralProjection, AstralPrimitives, LETS, UI, agent, and transport packages in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/tests/architecture/test_dependency_direction.py`
- [X] T027 Define Deep-owned typed ports for presentation queries/commands and durable-state services in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/component_ports.py`
- [X] T028 [P] Add contract and implementation-schema tests proving Deep ports contain no component-private implementation types and the promoted composition/evidence contracts enforce their schema-expressible canonical, digest-bound fields in `Y:/WORK/MCP/AstralDeep/backend/tests/test_component_ports_074.py` and `Y:/WORK/MCP/AstralDeep/scripts/tests/test_contract_schemas_074.py`; cross-record semantic validation remains T204
- [X] T029 Add a primitive-vocabulary decision gate that inventories all extracted UI payload types against AstralPrimitives `0.3.0` in `Y:/WORK/MCP/AstralDeep/scripts/verify_primitive_coverage.py`
- [X] T030 [P] Add existing-vocabulary, unknown-primitive, and version-floor tests for the primitive decision gate in `Y:/WORK/MCP/AstralDeep/scripts/tests/test_verify_primitive_coverage.py`
- [X] T031 Run the foundational narrow tests, stage only the exact files above, and record their command/output digests in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/foundational-checks.json`
- [X] T031A Raise changed-Python branch coverage for the foundational manifest, ownership, primitive-vocabulary, schema, and component-port gates to at least 90% and record the per-file report in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/foundational-checks.json`

---

## Phase 3: User Story 3 - Maintain Every Client from AstralProjection (Priority: P2)

**Goal**: Make AstralProjection the sole mutable owner of web rendering, ROTE adaptation, the UI protocol, and complete Windows, Android, and Apple client products.

**Independent Test**: Build Projection independently, render the shared fixture through web/Windows/Android/Apple consumers, and prove Projection imports no AstralDeep private module.

### Extraction and package boundary

- [X] T032 [US3] Remove only legacy tracked files from `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074` after validating the exact worktree root, leave local/generated files untouched, and immediately replace the removed legacy ignore file with a hardened cross-language `.gitignore` covering Python build/test artifacts before any further build or import
- [X] T033 [P] [US3] Import the immutable Deep revision's tracked `backend/webrender/**` source and pure tests into the initial import-compatible `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/backend/webrender/` and `tests/webrender/` paths, recording every source blob in `provenance/extraction.json`
- [X] T034 [P] [US3] Import the immutable Deep revision's tracked `backend/rote/**` source and pure tests into the initial import-compatible `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/backend/rote/` and `tests/rote/` paths, recording every source blob in `provenance/extraction.json`
- [X] T035 [P] [US3] Import `backend/shared/ui_protocol.json` and shared client conformance fixtures into `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/contracts/ui_protocol.json` and `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/contracts/fixtures/`
- [X] T036 [P] [US3] Import the complete tracked Windows client product into `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/windows-client/` with source blobs recorded in `provenance/extraction.json`
- [X] T037 [P] [US3] Import the complete tracked Android client product into `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/android-client/`, copy the two ignored local properties from Deep without reading/logging/staging them, verify their ignore disposition, and record tracked source blobs in `provenance/extraction.json`
- [X] T038 [P] [US3] Import the complete tracked Apple client products into `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/apple-clients/` with source blobs recorded in `provenance/extraction.json`, preserve the single declared Git symlink without dereferencing it, and reject every undeclared or destination-escaping link target
- [X] T039 [P] [US3] Import Projection-owned web tooling, client scripts, notices, and client documentation into `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/tooling/`, `scripts/`, `docs/`, and `NOTICE`
- [X] T040 [US3] Complete canonical extraction provenance, legacy default baseline, selected paths/blob IDs, and manifest SHA-256 in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/provenance/extraction.json`
- [X] T041 [US3] Preserve `webrender` and `rote` import compatibility while adding the stable metadata/resource facade and public exports in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/src/astralprojection/__init__.py`
- [X] T042 [US3] Package templates, JavaScript, CSS, fonts, images, vendor bundles, checksums, and notices through resource accessors in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/src/astralprojection/resources.py`
- [X] T043 [P] [US3] Add resource-presence, digest, traversal-refusal, and wheel-install tests in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/tests/test_resources.py`
- [X] T044 [US3] Define component, frame, chrome, theme, layout, degradation, and device-capability view models without Deep implementation types in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/src/astralprojection/models.py`
- [X] T045 [US3] Refactor audit, feedback, onboarding, and admin surfaces into pure supplied-state view builders in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/src/astralprojection/chrome/admin.py`
- [X] T046 [US3] Refactor agent, authoring, draft, and attachment surfaces into pure supplied-state view builders in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/src/astralprojection/chrome/agents.py`
- [X] T047 [US3] Refactor LLM, personalization, dreaming, pulse, and scheduler surfaces into pure supplied-state view builders in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/src/astralprojection/chrome/personalization.py`
- [X] T048 [US3] Refactor remote-machine, feature-flag, workspace, history, and timeline surfaces into pure supplied-state view builders in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/src/astralprojection/chrome/workspace.py`
- [X] T049 [P] [US3] Add golden, empty-state, denial-state, sanitization, accessibility, theme, layout, and degradation tests for the pure view builders in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/tests/chrome/`
- [X] T050 [US3] Add Deep-owned authorized query/command controllers that construct Projection view models in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/projection_controllers.py`
- [X] T051 [P] [US3] Add controller authorization, owner-isolation, failure, and redaction tests in `Y:/WORK/MCP/AstralDeep/backend/tests/test_projection_controllers.py`
- [X] T052 [US3] Publish UI protocol version/digest metadata and stable manifest access in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/src/astralprojection/protocol.py`
- [X] T053 [P] [US3] Repoint Windows protocol/runtime drift tests to `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/contracts/ui_protocol.json` in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/windows-client/tests/`
- [X] T054 [P] [US3] Repoint Android protocol/runtime drift tests to the standalone Projection contract in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/android-client/`
- [X] T055 [P] [US3] Repoint Apple protocol/runtime drift tests to the standalone Projection contract and remove the missing-manifest skip in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/apple-clients/AstralCore/Tests/`
- [X] T056 [US3] Rewrite Projection-owned workflow paths and repository identities while leaving copied CI/release workflows inactive in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/workflows-disabled/`
- [X] T057 [US3] Preserve Android application ID, redirect URI, version-code monotonicity, and signing-key expectations in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/android-client/app/build.gradle.kts` and `docs/android-release-continuity.md`
- [X] T058 [US3] Preserve Apple bundle IDs/team/profile expectations and add a protected monotonic build-number base in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/scripts/apple_build_number.py`
- [X] T059 [P] [US3] Add Apple build-number non-regression and missing-offset refusal tests in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/tests/release/test_apple_build_number.py`
- [X] T060 [US3] Implement the bounded Windows legacy/new-repository updater trust model in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/windows-client/astral_client/integrity.py`
- [X] T061 [P] [US3] Add Windows updater tests for legacy bridge maximum, Projection workflow identity, wrong repository/workflow, downgrade, and identical-bridge-byte requirements in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/windows-client/tests/test_integrity.py`
- [X] T062 [US3] Document the non-publishing dual-signature bridge procedure and stable product/store identities in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/docs/release-trust-transition.md`
- [X] T063 [US3] Run the Projection package, renderer, adaptation, architecture, resource, shared-protocol, and exact-index sensitive-path checks and save command/output digests in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/provenance/checks.json`
- [X] T064 [US3] Commit the exact paths under `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074` as an ordinary descendant with `Source-Repository`, `Source-Commit`, and `Source-Manifest-SHA256` trailers, push `codex/074-extract-projection`, and verify its remote SHA
- [X] T065 [US3] Record and push the exact Projection source/destination revisions, behavior, tests, Android continuity state, bridge status, and risks in `Y:/WORK/kos-wiki/wiki/synthesis-astral-repository-decomposition-lets-integration.md`, `Y:/WORK/kos-wiki/index.md`, and `Y:/WORK/kos-wiki/log.md`

---

## Phase 4: User Story 4 - Maintain Durable State from AstralPlane (Priority: P2)

**Goal**: Make AstralPlane an independently testable embedded persistence/storage library with explicit transactional contracts and safe recovery.

**Independent Test**: Upgrade representative pre-split PostgreSQL and blob state, exercise every extracted durable domain, inject failures, and restore the prior compatible composition without data loss or owner drift.

### Core library and known defect repairs

- [X] T066 [US4] Remove only legacy tracked files from `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074` after validating the exact worktree root, while leaving ignored database, logs, and environment state untouched
- [X] T067 [US4] Import only the immutable Deep revision's selected durable-state source and tests into `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/` and `tests/`, recording source blobs in `provenance/extraction.json`
- [X] T068 [US4] Define public transaction, query, command-result, schema, repository, blob, outbox, lifecycle, and recovery protocols in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/contracts/__init__.py`
- [X] T069 [US4] Implement explicit pool/connection scopes in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/database/pool.py` plus caller-owned transactions and detached immutable command metadata in `src/astralplane/database/transaction.py`
- [X] T070 [P] [US4] Add regression tests proving `execute()` never returns a cursor tied to a released pooled connection in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/tests/test_command_result.py`
- [X] T071 [US4] Replace lexical `?`/`%` SQL rewriting with native psycopg parameter contracts in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/database/sql.py`
- [X] T072 [P] [US4] Add literal, comment, wildcard, JSON operator, percent, and placeholder regression tests in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/tests/test_sql_parameters.py`
- [X] T073 [US4] Implement one explicit, concurrency-safe boot initializer instead of running schema initialization on every `Database()` construction in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/database/bootstrap.py`
- [X] T074 [P] [US4] Add multi-instance, concurrent-start, already-current, interrupted-start, and failure-state tests in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/tests/test_bootstrap.py`
- [ ] T075 [US4] Implement one guarded repeat-safe schema runner in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/database/migrations.py` and declared `DataPlaneRevision` in `src/astralplane/database/revision.py`
- [X] T076 [US4] Remove agent/filesystem/UI reconciliation from schema DDL and expose explicit post-migration hooks in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/reconciliation.py`
- [X] T077 [P] [US4] Add empty, representative legacy, repeated, concurrent, partially applied, and incompatible-revision migration tests in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/tests/test_schema_migrations.py`

### Durable domain clusters

- [X] T078 [P] [US4] Extract conversation, message, session, and history repositories behind public contracts in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/repositories/history.py`
- [X] T079 [P] [US4] Extract workspace, canvas, layout, and publication-state repositories in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/repositories/workspaces.py`
- [X] T080 [P] [US4] Extract attachment, artifact, materialization, and blob metadata repositories in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/repositories/artifacts.py`
- [X] T081 [P] [US4] Extract feedback, onboarding, and personalization persistence in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/repositories/preferences.py`
- [X] T082 [P] [US4] Extract scheduler, admission, scheduled-publication, and asynchronous effect state in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/repositories/scheduler.py`
- [X] T083 [P] [US4] Extract durable voice-session/turn metadata without real-time media logic in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/repositories/voice.py`
- [X] T084 [P] [US4] Extract durable remote-machine and execution metadata without SSH/transport execution in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/repositories/remote.py`
- [X] T085 [US4] Extract append-only audit persistence and hash-chain primitives in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/repositories/audit.py`
- [X] T086 [US4] Implement authenticated audit-retention anchors so prefix pruning verifies from the retained boundary in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/audit_retention.py`
- [X] T087 [P] [US4] Add genesis, retained-prefix, tampered-anchor, missing-anchor, and rollback tests in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/tests/test_audit_retention.py`
- [X] T088 [US4] Implement a PostgreSQL transactional outbox with lease, retry, dead-letter, and idempotency state in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/outbox.py`
- [X] T089 [P] [US4] Add commit/rollback, duplicate-delivery, worker-crash, lease-expiry, ordering, and dead-letter tests in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/tests/test_outbox.py`
- [X] T090 [US4] Route audit sink delivery through the transactional outbox and remove lossy JSONL retry behavior in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/audit_delivery.py`
- [X] T091 [P] [US4] Add restart and unavailable-sink tests proving audit events are neither dropped nor reported delivered early in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/tests/test_audit_delivery.py`
- [X] T092 [US4] Implement durable purge tombstones and incomplete-purge visibility for attachment/blob deletion in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/purge.py`
- [X] T093 [P] [US4] Add database-success/blob-failure, blob-success/database-failure, retry, owner-isolation, and recovery tests in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/tests/test_purge.py`
- [X] T094 [US4] Replace imports of Deep policy/lifecycle modules with neutral IDs, enums, callbacks, and supplied context in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/domain.py`
- [X] T095 [US4] Expose repository factories and one stable AstralPlane public façade in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/api.py`
- [ ] T096 [P] [US4] Add owner-isolation, attribution, transaction rollback, concurrency, idempotency, and failure-visibility contract tests across all repositories in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/tests/contract/`
- [X] T097 [US4] Define schema/API/minimum-consumer compatibility metadata and inspection in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/compatibility.py`
- [X] T098 [P] [US4] Add producer-side compatibility schema and semantic-version tests in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/tests/test_compatibility.py`
- [X] T099 [US4] Document PostgreSQL plus blob/workspace state as one backup, upgrade, rollback, and recovery unit in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/docs/migration-and-recovery.md`
- [ ] T100 [P] [US4] Create representative pre-split database/blob fixtures with synthetic non-PHI data in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/tests/fixtures/pre_split/`
- [ ] T101 [US4] Run upgrade, repeat-upgrade, transactional failure, blob failure, and documented recovery tests against the representative fixtures and save digests in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/provenance/checks.json`
- [X] T102 [US4] Complete canonical extraction provenance, legacy default baseline, selected paths/blob IDs, and manifest SHA-256 in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/provenance/extraction.json`
- [X] T103 [US4] Run `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/tests/architecture/test_dependency_direction.py` and prove the package imports no Deep, Projection, Primitives, LETS, UI, agent, media, or transport implementation
- [X] T104 [US4] Run `Y:/WORK/MCP/AstralDeep/scripts/migration/check_staged_paths_074.py` against the exact Plane index and prove `app/app.db`, logs, venv, credentials, and user state are absent
- [X] T105 [US4] Commit the exact paths under `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074` as an ordinary descendant with `Source-Repository`, `Source-Commit`, and `Source-Manifest-SHA256` trailers, push `codex/074-extract-data-plane`, and verify its remote SHA
- [X] T106 [US4] Verify with `merge-base --is-ancestor` that the pushed Plane replacement descends from the recorded legacy `master` tip and record the proof in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/plane-checkpoint.json`
- [X] T107 [US4] Record and push the exact Plane source/destination revisions, seven repaired defects, tests, migration/recovery behavior, and risks in `Y:/WORK/kos-wiki/wiki/synthesis-astral-repository-decomposition-lets-integration.md`, `Y:/WORK/kos-wiki/index.md`, and `Y:/WORK/kos-wiki/log.md`

---

## Phase 5: User Story 1 - Compose the Independent Astral System (Priority: P1) 🎯 MVP

**Goal**: Compose one runnable AstralDeep system from exact independent component revisions with no duplicate mutable implementation trees.

**Independent Test**: Clone AstralDeep afresh, initialize the four exact submodules, build/start with representative existing data, and complete an authenticated interaction without source copying.

- [X] T108 [P] [US1] Add AstralProjection at the exact pushed extraction commit as `Y:/WORK/MCP/AstralDeep/components/AstralProjection` using canonical HTTPS URL `https://github.com/AstralDeep/AstralProjection.git`
- [X] T109 [P] [US1] Add AstralPlane at the exact pushed extraction commit as `Y:/WORK/MCP/AstralDeep/components/AstralPlane` using canonical HTTPS URL `https://github.com/AstralDeep/AstralPlane.git`
- [X] T110 [P] [US1] Add canonical-identity AstralPrimitives commit `03870e55563f7522e95e298490e6a8638f2b8385` as `Y:/WORK/MCP/AstralDeep/components/AstralPrimitives` using canonical HTTPS URL `https://github.com/AstralDeep/AstralPrimitives.git`; runtime and vocabulary remain version 0.3.0
- [X] T111 [P] [US1] Add LETS signed-v1.0.10 commit `82dbe4f5ddf410cc86778784bb612440725ec66d` as `Y:/WORK/MCP/AstralDeep/components/LETS` using canonical HTTPS URL `https://github.com/AstralDeep/LETS.git`
- [X] T112 [US1] Remove every `.gitmodules` floating branch selector and verify the four canonical component mappings in `Y:/WORK/MCP/AstralDeep/.gitmodules`
- [X] T113 [US1] Create the authoritative compatible revision, contract version, protocol digest, Plane revision, LETS profile, and availability-mode declaration in `Y:/WORK/MCP/AstralDeep/config/astral-composition.json`
- [ ] T114 [US1] Implement missing/uninitialized/dirty/wrong-SHA/wrong-URL/incompatible/private-access diagnostics in `Y:/WORK/MCP/AstralDeep/scripts/verify_composition.py`
- [ ] T115 [P] [US1] Add composition tests for exact pins, canonical URLs, no floating branch, inaccessible private component, dirty component, stale gitlink, incompatible contracts, and LETS v1.0.10 public exports (`LETSClient`, `ReplicaAuthorizer`, `AstralDeepAuthorizer`, `Receipt`, `ReceiptVerifier`) in `Y:/WORK/MCP/AstralDeep/scripts/tests/test_verify_composition.py`
- [ ] T116 [US1] Install Projection, Plane, Primitives, and LETS from exact local component paths with no dependency resolver substitution in `Y:/WORK/MCP/AstralDeep/pyproject.toml`
- [ ] T117 [US1] Update container build ordering and package-data copying for initialized components in `Y:/WORK/MCP/AstralDeep/Dockerfile`
- [ ] T118 [P] [US1] Include component sources while recursively excluding secrets/runtime state in `Y:/WORK/MCP/AstralDeep/.dockerignore`, and prove the exclusions with a synthetic nested-sentinel Docker-context test before any real image build
- [ ] T119 [US1] Update local bootstrap/sync/preflight targets and submodule-aware CI paths for composition validation in `Y:/WORK/MCP/AstralDeep/Makefile` and `Y:/WORK/MCP/AstralDeep/.github/workflows/ci.yml`
- [ ] T120 [US1] Replace hard-coded shell, kiosk, and static paths with AstralProjection resource accessors in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/orchestrator.py` and `backend/orchestrator/web_auth.py`
- [ ] T121 [US1] Replace Deep durable-state imports with the AstralPlane public façade in `Y:/WORK/MCP/AstralDeep/backend/shared/database.py` and all consumers listed by `contracts/component-ownership.json`
- [ ] T122 [P] [US1] Add server UI protocol producer/consumer drift checks against the Projection submodule in `Y:/WORK/MCP/AstralDeep/backend/tests/test_projection_protocol_integration.py`
- [ ] T123 [P] [US1] Add Plane revision/startup compatibility and failure-attribution tests in `Y:/WORK/MCP/AstralDeep/backend/tests/test_plane_integration.py`
- [ ] T124 [US1] Remove Projection-owned tracked source trees and mutable tooling from `Y:/WORK/MCP/AstralDeep/backend/webrender`, `backend/rote`, `windows-client`, `android-client`, `apple-clients`, and `tooling/web-ci` only after all runtime/build/test references use the submodule
- [ ] T125 [US1] Remove Plane-owned tracked implementation from `Y:/WORK/MCP/AstralDeep/backend/shared/database.py` and extracted repositories only after all consumers use stable Plane contracts, retaining only deliberate compatibility adapters
- [ ] T126 [US1] Update `Y:/WORK/MCP/AstralDeep/contracts/component-ownership.json` and generated-copy rules so the ownership checker reports zero unmanaged duplicate source trees
- [ ] T127 [P] [US1] Add a clean-checkout bootstrap test using an isolated temporary clone in `Y:/WORK/MCP/AstralDeep/scripts/tests/test_clean_composition_checkout.py`
- [ ] T128 [US1] Add composed product rollback ordering, component re-pin, schema compatibility, and private-submodule access recovery instructions in `Y:/WORK/MCP/AstralDeep/docs/component-composition.md`
- [ ] T129 [US1] Build and start the composed system with representative existing data and save composition/startup evidence in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/composed-startup.json`
- [ ] T130 [US1] Complete an authenticated golden orchestration/data/render interaction with LETS mode off and record redacted correlation IDs in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/composed-golden.json`
- [ ] T131 [US1] Run the ownership, composition, protocol, Plane compatibility, clean-checkout, and flag-off parity tests and record exact command/output digests in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/us1-checks.json`
- [ ] T132 [US1] Commit and push the exact composition changes under `Y:/WORK/MCP/AstralDeep` on `074-multirepo-lets-integration`, then verify the remote branch SHA
- [ ] T133 [US1] Record and push the five exact component/composition revisions, clean-checkout result, authenticated golden flow, behavior changes, and risks in `Y:/WORK/kos-wiki/wiki/synthesis-astral-repository-decomposition-lets-integration.md`, `Y:/WORK/kos-wiki/wiki/project-astral.md`, `Y:/WORK/kos-wiki/index.md`, and `Y:/WORK/kos-wiki/log.md`

---

## Phase 6: User Story 2 - Enforce Finite Agent Authority with LETS (Priority: P1)

**Goal**: Add finite, lineage-bound LETS authority after every existing Astral gate and verify it again at the final physical-effect boundary for governed agents.

**Independent Test**: Govern a dynamic agent, execute one authorized effect, deny exhaustion/replay/wrong binding/outage, close or revoke the lifecycle, and correlate durable Astral/LETS evidence while flag-off behavior remains unchanged.

### Neutral durable state in AstralPlane

- [ ] T134 [P] [US2] Add `AgentAuthorityBinding`, lifecycle states, owner/runtime-generation uniqueness, policy/machine/config epochs, and lease metadata in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/authority/models.py`
- [ ] T135 [P] [US2] Add `AuthorityLifecycleOperation`, stable request fingerprint, retry state, and reconciliation metadata in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/authority/lifecycle.py`
- [ ] T136 [P] [US2] Add `ProtectedEffectOperation`, effect digest, nonce, audience, outcome/uncertainty, and audit correlation metadata in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/authority/effects.py`
- [ ] T137 [P] [US2] Add durable `ReceiptClaim` uniqueness, sequence watermark, and external-authority anchor metadata in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/authority/claims.py`
- [ ] T138 [US2] Add guarded repeat-safe authority binding/operation/claim/outbox migrations in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/database/migrations.py` and bump `src/astralplane/database/revision.py`
- [ ] T139 [US2] Expose neutral owner-isolated authority repositories and transactions through `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/src/astralplane/authority/repository.py`
- [ ] T140 [P] [US2] Add migration repeatability, active-binding uniqueness, owner isolation, request-fingerprint conflict, claim replay, sequence, and rollback tests in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/tests/authority/`

### Astral-owned configuration, lifecycle, and gateway

- [ ] T141 [US2] Implement strict `off|shadow|enforce` configuration parsing, governed cohorts, authenticated trust-manifest loading, TLS/secret-file requirements, timeouts, and readiness posture in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/lets_config.py`
- [ ] T142 [P] [US2] Add invalid-mode, missing-secret, HTTP production URL, credential-in-URL, redirect, missing-anchor, and redaction tests in `Y:/WORK/MCP/AstralDeep/backend/tests/test_lets_config.py`
- [ ] T143 [US2] Implement the fixed six-scope capability/transition/resource-dimension profile and hard-deny unknown mappings in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/lets_scope_profile.py`
- [ ] T144 [P] [US2] Add exact six-scope, incomplete allocation, unknown scope/tool, ambiguous audience, and changed digest tests in `Y:/WORK/MCP/AstralDeep/backend/tests/test_lets_scope_profile.py`
- [ ] T145 [US2] Implement the authenticated HTTPS adapter solely over LETS public `LETSClient`, `AstralDeepAuthorizer`, `Receipt`, and lifecycle contracts in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/lets_client.py`
- [ ] T146 [P] [US2] Add strict response parsing, response-size, total-timeout, no-redirect, TLS, same-ID retry, fingerprint-conflict, and redacted error tests in `Y:/WORK/MCP/AstralDeep/backend/tests/test_lets_client.py`
- [ ] T147 [US2] Implement canonical protected-effect context and digest generation after all argument/credential rewrites without sending raw arguments, credentials, PHI, or user content to LETS in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/protected_dispatch.py`
- [ ] T148 [P] [US2] Add canonicalization, exclusion, semantic-change, nonce entropy, and deterministic digest tests in `Y:/WORK/MCP/AstralDeep/backend/tests/test_protected_dispatch.py`
- [ ] T149 [US2] Implement dynamic/user-authored root/spawn/renew/quiesce/resume/close/revoke/expire lifecycle convergence using durable Plane operations in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/lets_lifecycle.py`
- [ ] T150 [P] [US2] Wire governed dynamic-agent admission, revision supersession, runtime generation, pause, reconnect, deletion, and compromise events in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/agent_lifecycle.py`
- [ ] T151 [P] [US2] Wire governed BYO runtime admission, host fencing, reconnect, retirement, and revocation in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/user_agents.py`
- [ ] T152 [US2] Implement startup/background reconciliation for pending or indeterminate lifecycle and authorization operations in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/lets_reconciler.py`
- [ ] T153 [P] [US2] Add crash-before-call, commit-lost-response, restart, renewal, disconnect/reconnect, epoch rotation, close, revoke, and exhaustion lifecycle tests in `Y:/WORK/MCP/AstralDeep/backend/tests/test_lets_lifecycle.py`
- [ ] T154 [US2] Implement host-binding checks plus LETS `ReceiptVerifier.verify_and_claim()` immediately before the physical actuator in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/lets_gateway.py`
- [ ] T155 [US2] Enforce monotonic receipt ordering with a binding-scoped authorization-through-claim lock in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/lets_gateway.py`
- [ ] T156 [US2] Change retry handling so every physical attempt gets a distinct durable operation ID, nonce, and receipt while pre-response transport retries reuse exact canonical semantics in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/tool_retry.py`
- [ ] T157 [US2] Represent lost non-idempotent results as `outcome_uncertain` and forbid blind replay in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/tool_retry.py`
- [ ] T158 [P] [US2] Add retry-before-response, same-ID conflict, claimed-receipt replay, crash-after-claim, lost-result, idempotent recovery, and compensation tests in `Y:/WORK/MCP/AstralDeep/backend/tests/test_lets_retry_semantics.py`

### Complete dispatch coverage

- [ ] T159 [US2] Route normal single, parallel, chained, and recursive tool calls through one governed final gateway in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/orchestrator.py`
- [ ] T160 [P] [US2] Route REST, WebSocket, component re-execution, and credential probes through the same protected dispatch context in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/api.py` and `backend/orchestrator/chrome_events.py`
- [ ] T161 [P] [US2] Route inbound/outbound MCP tool calls through typed `astraldeep.lets/v1` caller-capability metadata outside tool arguments in `Y:/WORK/MCP/AstralDeep/backend/shared/mcp_client.py`
- [ ] T162 [P] [US2] Route orchestrator and individual-agent A2A execution through the same protected gateway in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/a2a_orchestrator_executor.py` and `backend/shared/a2a_executor.py`
- [ ] T163 [P] [US2] Route background, scheduled, and asynchronous work through the same protected gateway in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/background_dispatch.py`
- [ ] T164 [P] [US2] Require bounded stream-open authorization and fresh poll/actuator authorization for polling and push streams in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/streaming.py`
- [ ] T165 [US2] Verify in-process effects at the last call site before `tool_fn(**arguments)` in `Y:/WORK/MCP/AstralDeep/backend/shared/base_agent.py`
- [ ] T166 [US2] Add the public LETS verifier, persistent replay store, signed trust manifest, and exact host-context checks to generated agent runtimes in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/agent_generator.py`
- [ ] T167 [US2] Add the same verifier/replay/host-binding contract to `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/windows-client/win_agent/agent.py`, pin the exact LETS v1.0.10 protected-executor dependency in `windows-client/requirements.in`, and mechanically regenerate `windows-client/requirements-release.lock.txt`
- [ ] T168 [US2] Label externally reachable agents without a conforming actuator as dispatch-mediated only and prevent protected-executor claims in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/external_agents.py`
- [ ] T169 [US2] Correlate redacted Astral audit, Plane operation, LETS request, receipt digest, claim, denial, and effect outcome evidence in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/lets_audit.py`
- [ ] T170 [US2] Expose typed, redacted health/readiness and user-facing deny reason codes without secrets or raw receipts in `Y:/WORK/MCP/AstralDeep/backend/orchestrator/lets_health.py`

### Conformance and rollout evidence

- [ ] T171 [P] [US2] Add byte/behavior parity tests proving master flag false and mode off make no LETS calls and fabricate no success evidence in `Y:/WORK/MCP/AstralDeep/backend/tests/test_lets_off_parity.py`
- [ ] T172 [P] [US2] Add shadow-versus-enforce tests proving shadow never blocks existing behavior and enforce fails closed in `Y:/WORK/MCP/AstralDeep/backend/tests/test_lets_modes.py`
- [ ] T173 [P] [US2] Add tests proving every pre-existing Astral identity, delegation, owner, permission, policy, security, taint, confirmation, PHI, egress, and audit denial occurs before LETS authorization in `Y:/WORK/MCP/AstralDeep/backend/tests/test_lets_gate_ordering.py`
- [ ] T174 [P] [US2] Add tamper/mismatch tests for issuer, key, signature, tenant, envelope, lease, lineage, subject, policy, machine, epoch, audience, operation, transition, cost, nonce, effect digest, sequence, time, and replay in `Y:/WORK/MCP/AstralDeep/backend/tests/test_lets_receipt_binding.py`
- [ ] T175 [P] [US2] Add timeout, unavailable warden, malformed response, stale receipt, key rotation, clock rollback, replay-store loss/fullness, and authority-anchor failure tests in `Y:/WORK/MCP/AstralDeep/backend/tests/test_lets_fail_closed.py`
- [ ] T176 [P] [US2] Add concurrent same-binding and multi-binding tests proving no out-of-order physical effects or cross-owner claims in `Y:/WORK/MCP/AstralDeep/backend/tests/test_lets_concurrency.py`
- [ ] T177 [P] [US2] Add a parameterized dispatch matrix covering REST, WebSocket, A2A, MCP, background, scheduled, chained, recursive, component, probe, poll, push, in-process, generated, BYO, remote, and external paths in `Y:/WORK/MCP/AstralDeep/backend/tests/test_lets_dispatch_matrix.py`
- [ ] T178 [US2] Add an end-to-end invariant test proving zero governed physical effects occur without exactly one successfully claimed matching receipt in `Y:/WORK/MCP/AstralDeep/backend/tests/test_lets_effect_invariant.py`
- [ ] T179 [US2] Close the primitive decision in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/primitive-decision.json`: record verified no-change when coverage is complete, or for every proven gap implement/export/document/serialize/test/version the primitive under `Y:/WORK/MCP/AstralPrimitives/src/astralprims`, `tests`, and `pyproject.toml`, push the coordinated release, and update Projection/Deep pins before compatibility is claimed
- [ ] T180 [US2] Run Plane authority tests, LETS `v1.0.10` public-client/verifier tests, Deep conformance tests, and generated/BYO verifier tests; record command/output digests in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/us2-checks.json`
- [ ] T181 [US2] Commit and push the Plane authority checkpoint on `codex/074-extract-data-plane`, verify its remote SHA/ancestry, and update `Y:/WORK/MCP/AstralDeep/config/astral-composition.json` to that exact commit
- [ ] T182 [US2] Commit and push the Projection BYO verifier checkpoint on `codex/074-extract-projection`, verify its remote SHA/ancestry, and update `Y:/WORK/MCP/AstralDeep/config/astral-composition.json` to that exact commit
- [ ] T183 [US2] Commit and push the LETS integration changes under `Y:/WORK/MCP/AstralDeep` on `074-multirepo-lets-integration`, then verify the remote SHA
- [ ] T184 [US2] Record and push exact Plane/Projection/Deep/LETS revisions, rollout posture, repaired bugs, conformance results, paper impact, and residual risks in `Y:/WORK/kos-wiki/wiki/synthesis-astral-repository-decomposition-lets-integration.md`, `Y:/WORK/kos-wiki/wiki/project-astral.md`, `Y:/WORK/kos-wiki/wiki/project-lets.md`, `Y:/WORK/kos-wiki/index.md`, and `Y:/WORK/kos-wiki/log.md`

---

## Phase 7: User Story 5 - Preserve Repository and Release Provenance (Priority: P2)

**Goal**: Make renamed identities, normal ancestry, extraction provenance, compatibility checkpoints, and staged release trust independently auditable.

**Independent Test**: Verify canonical URLs/default branches, ordinary descendant replacement commits, exact source blob provenance, retained legacy `master`, and an authenticated offline updater trust transition.

- [X] T185 [P] [US5] Replace current operational old/case-mismatched repository URLs and stale ownership/path guidance in `Y:/WORK/MCP/AstralDeep/AGENTS.md`, `.specify/memory/constitution.md`, maintained `docs/`, package metadata, and `.github/workflows/`, while preserving explicitly historical citations and the owner-authorized migration record
- [X] T186 [P] [US5] Replace current operational old/case-mismatched repository URLs and schema IDs in `Y:/WORK/MCP/LETS/pyproject.toml`, `Dockerfile`, `CHANGELOG.md`, `docs/`, and `protocol/`
- [X] T187 [P] [US5] Replace current operational old/case-mismatched repository URLs in `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/pyproject.toml`, `README.md`, and `docs/`
- [X] T188 [P] [US5] Replace current operational old/case-mismatched repository URLs in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/pyproject.toml`, `README.md`, client metadata, and `docs/`
- [X] T189 [P] [US5] Replace current operational old/case-mismatched repository URLs in `Y:/WORK/MCP/AstralPrimitives/pyproject.toml`, `CLAUDE.md`, and maintained documentation
- [ ] T190 [US5] Implement canonical URL, redirect-dependence, exact gitlink, normal-ancestry, retained-master, provenance-schema, and source-blob verification in `Y:/WORK/MCP/AstralDeep/scripts/verify_migration_provenance.py`
- [ ] T191 [P] [US5] Add wrong-case, redirect-only, orphan, force-divergence, missing blob, manifest tamper, changed legacy master, and archive-ref prohibition tests in `Y:/WORK/MCP/AstralDeep/scripts/tests/test_verify_migration_provenance.py`
- [ ] T192 [US5] Create a machine-readable release trust transition with legacy repository/workflow fence, bridge maximum, new Projection identity, and identical artifact digest in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/contracts/windows-release-trust.json`
- [ ] T193 [P] [US5] Add an offline dual-bundle/identical-byte verification harness without publishing a release in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/scripts/verify_windows_bridge.py`
- [ ] T194 [P] [US5] Add tests for exact byte identity, old identity past bridge maximum, new identity before transition, wrong tag/repository/workflow, and downgrade in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/tests/release/test_windows_bridge.py`
- [ ] T195 [US5] Inventory required Projection GitHub environments, secrets, variables, store identities, approval rules, and last Apple/Android build numbers without secret values in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/docs/release-environment-inventory.md`
- [ ] T196 [US5] Keep release workflows inactive until final local qualification and encode their activation preconditions in `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/docs/release-workflow-activation.md`
- [ ] T197 [US5] Document normal-commit rollback for Projection, Plane, Deep gitlinks, and the prohibition on automatic Plane schema downgrade in `Y:/WORK/MCP/AstralDeep/docs/migration-rollback-074.md`
- [ ] T198 [US5] Run provenance, ancestry, URL, retained-master, release-trust, source-ownership, and sensitive-path checks and save digests in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/us5-checks.json`
- [ ] T199 [US5] Commit and push URL/provenance changes under `Y:/WORK/MCP/LETS`, `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074`, `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074`, and `Y:/WORK/MCP/AstralPrimitives` on their named branches; advance only changed compatible Plane/Projection/Primitives gitlinks in `Y:/WORK/MCP/AstralDeep/config/astral-composition.json`; retain the signed LETS v1.0.10 gitlink unless T212 requires a successor; then commit/push Deep
- [ ] T200 [US5] Produce one exact five-repository `MigrationCheckpoint`, composition linkage, product feature refs, and manifest digest in `Y:/WORK/MCP/AstralDeep/migration/checkpoint-074.json`
- [ ] T201 [US5] Verify every pushed feature ref equals local `HEAD`, every component replacement descends from its recorded baseline, and no feature-074 archive ref exists; update `Y:/WORK/MCP/AstralDeep/migration/checkpoint-074.json`
- [ ] T202 [US5] Record and push all exact remote revisions, default branches, retained legacy refs, release-transition state, verification, and risks in `Y:/WORK/kos-wiki/wiki/synthesis-astral-repository-decomposition-lets-integration.md`, `Y:/WORK/kos-wiki/index.md`, and `Y:/WORK/kos-wiki/log.md`

---

## Phase 8: User Story 6 - Evaluate and Report the Astral LETS Case Study (Priority: P3)

**Goal**: Produce reproducible exact-revision Astral case-study evidence and update the local named/anonymous LETS manuscript without altering or mislabeling v1.0.10 evidence.

**Independent Test**: Re-run the case study from the exact five-repository composition, validate all raw/result digests, rebuild named/anonymous manuscripts, and prove the anonymous artifact contains no prohibited identifying tokens.

- [ ] T203 [P] [US6] Copy the canonical evidence schema from Deep into `Y:/WORK/MCP/LETS/benchmarks/astraldeep/case-study-evidence.schema.json` and implement the tracked off/shadow/enforce runner across six scopes, lifecycle, parallel/recursive dispatch, outage, replay, exhaustion, and revocation in `run_case_study.py`
- [ ] T204 [P] [US6] Implement environment/machine/config/component revision capture plus semantic evidence validation for unique retained artifact paths, artifact-reference integrity, ordered timestamps, canonical digests, and secret/credential/PHI exclusion in `Y:/WORK/MCP/LETS/benchmarks/astraldeep/capture_environment.py`
- [ ] T205 [P] [US6] Add tests for exact revision capture, schema validation, canonical digests, secret redaction, missing inputs, and refusal to mix baselines in `Y:/WORK/MCP/LETS/tests/benchmarks/test_astraldeep_case_study.py`
- [ ] T206 [US6] Create an ignored local evidence root and run manifest in `Y:/WORK/MCP/LETS/results/astraldeep-case-study/manifest.json` without staging any result or manuscript artifact
- [ ] T207 [US6] Run flag-off baseline and v1.0.10 reference scenarios, retaining raw outputs and labeling them distinctly in `Y:/WORK/MCP/LETS/results/astraldeep-case-study/baseline/`
- [ ] T208 [US6] Run shadow and enforce Astral integration scenarios from exact component commits, retaining raw outputs in `Y:/WORK/MCP/LETS/results/astraldeep-case-study/integration/`
- [ ] T209 [US6] Measure p50/p95/p99 authorization/end-to-end latency, throughput, refusal reasons, lifecycle convergence, recovery, storage growth, budget conservation, and unreceipted effects in `Y:/WORK/MCP/LETS/results/astraldeep-case-study/summary.json`
- [ ] T210 [US6] Validate the complete evidence bundle against `Y:/WORK/MCP/LETS/benchmarks/astraldeep/case-study-evidence.schema.json` and record every file digest in `Y:/WORK/MCP/LETS/results/astraldeep-case-study/manifest.json`
- [ ] T211 [US6] Compare used LETS runtime/API behavior to the signed v1.0.10 source and record an evidence-backed unchanged-runtime or successor-required disposition in `Y:/WORK/MCP/LETS/results/astraldeep-case-study/version-disposition.json`
- [ ] T212 [US6] Implement a fail-closed successor-release gate that blocks paper result finalization and emits a separately reviewable LETS defect/release handoff whenever `version-disposition.json` requires runtime or wire-semantic changes in `Y:/WORK/MCP/LETS/benchmarks/astraldeep/check_version_disposition.py`; never alter `v1.0.10`
- [ ] T213 [US6] Add the accurate Armstrong, Klusty, Logan, Leach, and Bumgardner AMIA 2026 Astral citation with PMCID `PMC13274365` to `Y:/WORK/MCP/LETS/paper/submission/references.bib`
- [ ] T214 [US6] Add the exact-revision Astral use-case method, architecture distinction, finite-authority profile, rollout posture, and limitations to the named manuscript in `Y:/WORK/MCP/LETS/paper/submission/main.tex`
- [ ] T215 [US6] Replace draft result claims and tables only with validated values from `Y:/WORK/MCP/LETS/results/astraldeep-case-study/summary.json` in `Y:/WORK/MCP/LETS/paper/submission/main.tex`
- [ ] T216 [US6] Update the anonymous conditional text in `Y:/WORK/MCP/LETS/paper/submission/main.tex` and its `paper-anon.tex` wrapper with neutral self-citation and no prohibited names, organizations, repository URLs, public fingerprints, or deanonymizing acknowledgments
- [ ] T217 [P] [US6] Add named-citation, version-label, result-manifest, and anonymous-token checks in `Y:/WORK/MCP/LETS/paper/submission/check_submission.py`
- [ ] T218 [US6] Build and visually verify the local named and anonymous PDFs, retaining build logs and renders under `Y:/WORK/MCP/LETS/paper/submission/build/` without staging them
- [ ] T219 [US6] Run case-study reproducibility and manuscript checks from the exact composition and record their digests in `Y:/WORK/MCP/LETS/results/astraldeep-case-study/reproduction.json`
- [ ] T220 [US6] Commit and push only tracked changes under `Y:/WORK/MCP/LETS/benchmarks/astraldeep` and `Y:/WORK/MCP/LETS/tests/benchmarks` to the LETS feature branch; do not stage or push `paper/` or `results/`
- [ ] T221 [US6] Record and push exact evidence revisions/digests, LETS version disposition, measured-result changes, AMIA citation status, paper locality, and risks in `Y:/WORK/kos-wiki/wiki/project-lets.md`, `Y:/WORK/kos-wiki/wiki/project-astral.md`, `Y:/WORK/kos-wiki/wiki/synthesis-astral-repository-decomposition-lets-integration.md`, `Y:/WORK/kos-wiki/index.md`, and `Y:/WORK/kos-wiki/log.md`

---

## Phase 9: Final Local Qualification and Review Checkpoints

**Purpose**: Run broad CI-equivalent and live checks only after narrow migration work is stable; no production deployment, release, store submission, paper submission, or merge is authorized.

- [ ] T222 [P] Re-run AstralPlane unit, architecture, migration, PostgreSQL integration, recovery, outbox, purge, audit-retention, and authority suites from `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/pyproject.toml`
- [ ] T223 [P] Re-run AstralProjection Python renderer/ROTE/architecture/resource/protocol suites from `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/pyproject.toml`
- [ ] T224 [P] Run Projection Windows tests with `QT_QPA_PLATFORM=offscreen` from `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/windows-client/`
- [ ] T225 [P] Run Projection Android `ktlintCheck`, lint, core/app unit coverage, and assemble gates from `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/android-client/gradlew`
- [ ] T226 [P] Run Projection Apple Swift/Xcode gates on a qualified macOS host from `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/apple-clients/README.md`, or record the exact unavailable-host blocker and stop T236/T237 without weakening the gate
- [ ] T227 [P] Run AstralPrimitives serialization, export, documentation, and version-floor suites from `Y:/WORK/MCP/AstralPrimitives/pyproject.toml`
- [ ] T228 [P] Run the immutable LETS v1.0.10 acceptance, integration, protocol, executor, replay, and Astral case-study harness tests from `Y:/WORK/MCP/LETS/pyproject.toml`
- [ ] T229 Run the full local AstralDeep CI-equivalent backend and nested module suites, Ruff, coverage, composition, ownership, protocol, Plane, and LETS conformance checks from `Y:/WORK/MCP/AstralDeep/.github/workflows/ci.yml`
- [ ] T230 Run a clean composed build/start against representative existing data and complete authenticated web plus available native-client golden flows, recording results in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/final-live-verification.json`
- [ ] T231 Run bounded LETS outage, lost-response, replay, revocation, exhaustion, clock, concurrency, restart, and recovery fault/soak scenarios and record raw evidence under `Y:/WORK/MCP/LETS/results/astraldeep-case-study/final-qualification/`
- [ ] T232 Run offline Windows bridge, client identity continuity, Android version/signing expectation, Apple build-offset, and release-workflow activation-precondition checks; record results in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/release-qualification.json`
- [ ] T233 Recompute every component contract, provenance, source-owner, case-study, and system-composition digest and update `Y:/WORK/MCP/AstralDeep/config/astral-composition.json` and `migration/checkpoint-074.json` only when the exact pins match tested commits
- [ ] T234 Verify all five product indexes exclude credentials, databases, logs, uploads, generated agents, ignored Android properties, manuscript sources, and evidence results; save the redacted report in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/final-sensitive-path-check.json`
- [ ] T235 Verify Projection and Plane feature commits remain ordinary descendants of recorded baselines, `main` remains the default, `master` remains unchanged, and no archive or force-rewrite occurred; update `Y:/WORK/MCP/AstralDeep/migration/checkpoint-074.json`
- [ ] T236 After every local gate passes, promote only unprivileged PR CI definitions into `Y:/WORK/MCP/.migration-worktrees/AstralPlane-074/.github/workflows/ci.yml` and `Y:/WORK/MCP/.migration-worktrees/AstralProjection-074/.github/workflows/ci.yml`, keep release workflows inactive, then commit/push final qualified Plane, Projection, Primitives-if-changed, LETS-tracked, and Deep changes in dependency order while verifying each remote SHA
- [ ] T237 Search all PR states for each exact product head, create at most one draft PR per changed repository against `main`, and record URLs plus explicit no-merge/no-release posture in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/draft-prs.json`
- [ ] T238 Keep production on the monorepo-derived deployment, permit only the newly qualified unprivileged PR CI, and keep signing/deployment/store/paper/release workflows inactive until separate owner decisions; record the verified posture in `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/delivery-boundary.json`
- [ ] T239 Update and push final curated kos-wiki pages with exact product/wiki SHAs, test commands/results, live verification, paper/evidence state, fixed bugs, unresolved gates, and no-deployment status in `Y:/WORK/kos-wiki/wiki/synthesis-astral-repository-decomposition-lets-integration.md`, `Y:/WORK/kos-wiki/wiki/project-astral.md`, `Y:/WORK/kos-wiki/wiki/project-lets.md`, `Y:/WORK/kos-wiki/index.md`, and `Y:/WORK/kos-wiki/log.md`
- [ ] T240 Verify the final pushed wiki SHA and all product feature SHAs from their remotes, then append the immutable checkpoint set to `Y:/WORK/MCP/AstralDeep/specs/074-multirepo-lets-integration/execution/final-checkpoint.json`

---

## Dependencies and Execution Order

### Phase dependencies

- **Setup (Phase 1)**: Starts immediately; default-branch changes and worktree creation are prerequisites for destination writes.
- **Foundational (Phase 2)**: Depends on Setup; blocks every user story.
- **US3 / Projection (Phase 3)** and **US4 / Plane (Phase 4)**: Depend on Foundational and can proceed in parallel in their independent worktrees.
- **US1 / Composition (Phase 5)**: Depends on the pushed US3 and US4 extraction commits plus unchanged pinned Primitives and LETS baselines.
- **US2 / LETS (Phase 6)**: Depends on US1 composition and the Plane public transaction boundary; Plane and Projection checkpoint updates must be pushed before Deep advances their gitlinks.
- **US5 / Provenance (Phase 7)**: Depends on pushed extraction/integration checkpoints so ancestry and release transition can be audited against real commits.
- **US6 / Case Study (Phase 8)**: Depends on a locally qualified US2 enforcement composition; measured evidence must never precede implementation.
- **Final Qualification (Phase 9)**: Depends on all requested story work; broad hosted CI remains deferred until the local gate is complete.

### User-story dependency graph

```text
Setup -> Foundational -> US3 Projection ----\
                            US4 Plane -------+-> US1 Composition -> US2 LETS -> US6 Paper
                                             \-> US5 Provenance -----------/
All completed stories -----------------------------------------------> Final Qualification
```

### Parallel examples

- **Foundational**: T016-T018 schemas, T025-T026 dependency tests, and T029-T030 primitive gate can proceed independently.
- **US3**: T033-T039 tracked imports can proceed by disjoint destination path; client protocol tasks T053-T055 and identity tasks T057-T061 can proceed by platform.
- **US4**: Repository clusters T078-T084 can proceed by durable domain after T068-T077; tests T087, T089, T091, and T093 use disjoint modules.
- **US1**: T108-T111 submodule additions must be serialized when editing `.gitmodules`, but T118, T122, T123, and T127 touch independent files.
- **US2**: Plane models T134-T137, Deep tests T142/T144/T146/T148, lifecycle wiring T150-T151, dispatch adapters T160-T164, and conformance suites T171-T177 can be parallelized by file after their shared APIs exist.
- **US5**: URL fixes T185-T189 and release verification work T193-T195 are repository/file independent.
- **US6**: Harness T203, environment capture T204, and tests T205 can proceed before controlled experiments; manuscript edits must wait for T210-T211.

## Implementation Strategy

### Minimum viable checkpoint

The first useful checkpoint is **US3 + US4 + US1**: Projection and Plane are independent, AstralDeep composes exact revisions, LETS/Primitives remain independently pinned, and the flag-off golden flow works. This is reviewable but not deployable.

### Incremental delivery

1. Establish exact live baselines, `main` defaults, and clean fresh worktrees without archive refs.
2. Push independently tested Projection and Plane replacement commits.
3. Compose exact submodules and remove duplicate mutable trees only after cutover parity passes.
4. Land LETS state and enforcement in Plane/Projection/Deep checkpoints, keeping v1.0.10 immutable.
5. Audit canonical identity/provenance and the offline client release-trust bridge.
6. Generate measured local case-study evidence and update only local manuscript artifacts.
7. Run broad local CI/fault/live qualification, then push final checkpoints and open draft PRs only.

## Completion Guard

No task in this ledger authorizes merging a draft PR, changing production deployment, enabling release workflows, publishing a release, submitting to an app store, or publishing/submitting the paper. Any LETS runtime change discovered by T211 requires a successor release and rerun; it must never mutate or relabel `v1.0.10`.
