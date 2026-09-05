# Tasks: Persistent Agents

**Input**: `specs/079-persistent-agents/{spec,plan,research,data-model}.md` and `contracts/`.
**Tests**: Required by FR-020 and Constitution III. Write failure/denial tests before the corresponding implementation and measure changed-code coverage.
**Organization**: Shared setup/foundation, then independently verifiable user stories. Cross-repository ownership is explicit; uncommitted component work stays recoverable and cannot bypass exact-pin qualification.

## Phase 1: Setup

- [x] T001 Verify active ownership, clean component baselines, Python 3.11 test runtime and ignore/build rules; record initial results in `specs/079-persistent-agents/verification/results.md`.
- [x] T002 [P] Add fail-closed persistent-agent configuration and bounded defaults in `backend/shared/feature_flags.py`, `backend/persistent_agents/__init__.py`, and `.env.example`; document operator behavior in `specs/079-persistent-agents/quickstart.md` (FR-019).

## Phase 2: Foundation

- [x] T003 Write real-PostgreSQL tests for schema upgrade over representative 075 data, repeat migration, owner isolation, malformed records and cap races in `components/AstralPlane/tests/repositories/test_assignments.py` and `components/AstralPlane/tests/test_schema_migrations.py` (FR-018, FR-020).
- [x] T004 Implement guarded additive 079.001 tables, constraints and current-schema verification in `components/AstralPlane/src/astralplane/database/{migrations,revision}.py`; document recovery in `components/AstralPlane/docs/migration-and-recovery.md` (FR-002, FR-020).
- [x] T005 Implement validated frozen assignment records, bounded canonical JSON and stateless owner-scoped repository foundation in `components/AstralPlane/src/astralplane/repositories/assignments.py`; expose the repository through `api.py` and `__init__.py` (FR-001, FR-002, FR-018).
- [x] T006 [P] Implement strict Deep request/source/limit models, safe error vocabulary and the application-scoped Plane adapter in `backend/persistent_agents/models.py` and `backend/persistent_agents/store.py`, with validation tests in `backend/persistent_agents/tests/test_models.py` (FR-004, FR-005, FR-011, FR-018).

## Phase 3: US1 - Standing assignment and meaningful results (P1)

**Independent test**: Consented public-page assignment reads a real public source, preserves a finding, waits, and skips model work/notifications on an unchanged check. No new provider credentials.

- [x] T007 [P] [US1] Add create/consent/source/quiet-check and disconnected-tool tests in `backend/persistent_agents/tests/test_service.py` and `test_runner.py` (FR-001, FR-003, FR-004, SC-001, SC-003).
- [x] T008 [US1] Implement idempotent creation, owner-cap checks, bounded due claims and atomic source-batch/cursor ingestion in `components/AstralPlane/src/astralplane/repositories/assignments.py` (FR-001, FR-002, FR-003, FR-006).
- [x] T009 [US1] Implement explicit grant capture/derivation, source-tool authorization and bounded output normalization in `backend/persistent_agents/service.py` and `runner.py`; add persistent machine class in `backend/orchestrator/chain_authority.py` (FR-004, FR-005, FR-009, FR-010).
- [x] T010 [US1] Implement supervised polling, coalesced wakeups, admission, bounded structured planning, persisted checkpoints, source-error handling and quiet unchanged checks in `backend/persistent_agents/runner.py` (FR-002, FR-003, FR-017, SC-003, SC-007).
- [x] T011 [US1] Wire startup/shutdown and authenticated owner API under `/api/persistent-agents` in `backend/persistent_agents/api.py` and `backend/orchestrator/orchestrator.py`; preserve disabled behavior (FR-001, FR-019).
- [x] T012 [P] [US1] Build Projection-owned assignment cards/forms/activity and Deep-authorized snapshots in `components/AstralProjection/src/astralprojection/chrome/personalization.py` and `backend/orchestrator/projection_surfaces/personalization.py`, with view tests (FR-016, FR-017).

## Phase 4: US2 - Recovery without duplicate work (P1)

**Independent test**: Crash at claim, reservation, dispatch, result and checkpoint boundaries; recover with competing workers; completed source/action/results publish once and uncertain effects do not replay.

- [x] T013 [P] [US2] Add lease, two-worker, duplicate/reordered event, A→B→A, stale-completion and action-outcome fault tests in `components/AstralPlane/tests/repositories/test_assignments.py` and `backend/persistent_agents/tests/test_recovery.py` (FR-006, FR-007, SC-002).
- [x] T014 [US2] Implement lease renewal/recovery, stable effect ledger, immutable outcome receipts and atomic checkpoint/event/activity completion in `components/AstralPlane/src/astralplane/repositories/assignments.py` (FR-002, FR-006, FR-007).
- [x] T015 [US2] Implement reconstructable step execution, safe read retries, uncertain-effect holds, state-version conflicts and stale-output refusal in `backend/persistent_agents/runner.py` and `store.py` (FR-006, FR-007).
- [x] T016 [US2] Implement bounded history/payload retention, dedupe-capacity holds and owner deletion from either fenced terminal stopped/completed lifecycle through Plane in `components/AstralPlane/src/astralplane/repositories/assignments.py` and Deep service tests; cover unresolved effects and account retirement (FR-018).

## Phase 5: US3 - Owner controls, approval and resource limits (P1)

**Independent test**: Revise/pause/stop/revoke during active work, race a dispatch from another worker, exhaust shared budgets, and refuse stale/cross-owner/argument-changed approvals.

- [x] T017 [P] [US3] Write lifecycle-CAS, idempotent controls, stop/dispatch race, revoked authority, stale approval and budget tests in `backend/persistent_agents/tests/test_controls.py`, `test_approvals.py`, and `test_dispatch.py` (FR-009 through FR-015, SC-004, SC-005).
- [x] T018 [US3] Implement instruction revisions/control epochs, terminal stop, coalesced resume and invalidation of old claims/approvals in `components/AstralPlane/src/astralplane/repositories/assignments.py` and `backend/persistent_agents/service.py` (FR-014, FR-015).
- [x] T019 [US3] Implement cumulative mandatory call/token/time reservations and immutable bounded attempts in `components/AstralPlane/src/astralplane/repositories/assignments.py`; implement optional currency-cap trusted quotes, unknown-cost denial when selected, and per-call caps in `backend/persistent_agents/dispatch_context.py`; test an unpriced resource-capped monitor and refused unpriced currency-capped activation (FR-011).
- [x] T020 [US3] Add ordered dispatch-start/control checks, fresh per-effect permissions, lease/grant/resource validation and model-attempt accounting through narrow hooks in `backend/orchestrator/orchestrator.py` and `backend/persistent_agents/dispatch_context.py` (FR-009, FR-010, FR-011, FR-014).
- [x] T021 [US3] Implement durable exact proposals, expiry/decision replay/precondition checks, owner/proposal-bound foreground claim and admission after the worker releases its lease, full-gate approved execution, and late-result reconciliation in `backend/persistent_agents/service.py`, `api.py`, and Plane action methods; test approved-action resume/restart/competing claims and retain all existing unattended mutation denials (FR-007, FR-012, FR-013).
- [x] T022 [US3] Implement authenticated lifecycle/review UI actions and chat controls in `backend/persistent_agents/chat_tools.py`, Deep personalization handlers/controller registries, and Projection shared view; add and test coalesced/rate-bounded idempotent Plane request_check for run-now; expose full review context and actionable error/empty states (FR-014, FR-016, FR-017).
- [x] T023 [P] [US3] Update exact shared action manifest and client dispositions/drift tests in `components/AstralProjection/contracts/ui_protocol.json`, Windows/Android/Apple clients, and Deep `backend/tests/test_ui_protocol_manifest.py`; implement declared wrist status/chat-control behavior (FR-016, SC-008).

## Phase 6: US4 - Durable decomposition and delegation (P2)

**Independent test**: Split a discovered public release investigation into two bounded independent analyses, interrupt the parent after one child completes, resume and incorporate each result exactly once.

- [x] T024 [P] [US4] Add graph validation, authority subset, shared-budget, child cancellation/quarantine and parent-restart tests in `backend/persistent_agents/tests/test_delegation.py` and Plane assignment tests (FR-008, FR-009, SC-006).
- [x] T025 [US4] Implement bounded immutable plans, dependency-ready task claims, durable completed results and atomic parent incorporation in `components/AstralPlane/src/astralplane/repositories/assignments.py` (FR-008).
- [x] T026 [US4] Implement bounded delegated execution through existing authority/normal dispatch and scanned result incorporation in `backend/persistent_agents/runner.py`, retaining parent controls and cumulative limits (FR-008, FR-009, FR-011, SC-006).

## Phase 7: Qualification and handoff

**2026-09-05 checkpoint:** T001–T026 are implemented and locally tested. T029's
locally available commands were executed; Windows baseline failures and unavailable
Apple/live targets are documented rather than counted as passes. T027 and T028
are complete: exact component commits/pins pass validation, the broader security
rerun passed 348 tests, and changed Deep Python coverage exceeds 90%.
T030's driver is implemented and tested; first/second-boot candidate binding and
unauthenticated denial checks pass, but its authenticated scenarios are pending.
T031 remains open at real owner sign-in. T032's final local review and handoff are
complete with explicit baseline failures and missing native/release producers.
See `verification/results.md` for exact evidence and authorization limits.

- [x] T027 Run Plane repository/migration/regression tests on representative real PostgreSQL, then qualify exact Plane/schema/digest composition and Projection manifest pins in `config/astral-composition.json`; preserve component commit authorization boundaries (FR-020).
- [x] T028 Run focused Deep security/scheduler/authority/admission/approval/delegation suites plus new feature suite and measure changed Python coverage at least 90%; record exact commands/results in `specs/079-persistent-agents/verification/results.md` (FR-009, FR-019, FR-020, SC-005).
- [x] T029 [P] Run all locally available affected client tests/lint/build/parity gates, document any unavailable runner honestly in `specs/079-persistent-agents/verification/results.md` (FR-016, FR-020, SC-008).
- [ ] T030 Implement/run `scripts/verify_persistent_agents_079.py` for candidate-bound real public-source, controlled revision, interruption/recovery, 25-idle-assignment and control-latency evidence; distinguish real and fixture observations (SC-001 through SC-007).
- [ ] T031 Exercise creation/control/activity/approval against the live candidate on every affected client/form factor; retain exact observations and missing inputs in `specs/079-persistent-agents/verification/results.md` (FR-020, SC-008).
- [x] T032 Review all changed files, test failures and documentation for remaining defects; complete `specs/079-persistent-agents/quickstart.md`, update task state and curated knowledge-vault checkpoint, and leave a precise local handoff without claiming product push, production deployment or release (FR-020).

## Dependencies and parallel work

### Production preparation follow-up (2026-09-05)

The owner subsequently authorized PRs/merges where gates permit and production
preparation. These tasks track defects exposed by actual signed-in and native
qualification; the earlier local checkpoint is not a production-ready claim.

- [x] T033 Coordinate browser/background refresh rotation through one durable session claim, safely convert legacy grants, and verify revocation/cancellation/logout races.
- [x] T034 Fix typed URL/privacy boundaries, scan unescaped complete source evidence before redaction, retain only sanitized observations, and qualify with the installed detector.
- [ ] T035 Complete native coverage producers and strict parser checks; correct stale 079 schema/dispatch fixtures, classify broader baseline failures, and record exact final source evidence.
- [ ] T036 Build from clean committed LF source, verify installed runtime identity, run the bounded owner-approved assignment and controls/restart checks, and stop it after testing.
- [ ] T037 Open owner-qualified PRs, satisfy independent review and CI before any merge, and retain concrete protected-staging/Apple/publication blockers in `verification/production-readiness.md`.
- [x] T038 Qualify the live-discovered proven-unstarted pause/resume recovery repair, early production exit-78 gate, patched build/CI dependency locks and isolated paired-backup restore rehearsal; bind final source/image evidence without resetting the approved live-test budget.
- [x] T039 Diagnose rejected model results using bounded codes without retaining content, reserve completion capacity for reasoning models while preserving existing action identities, and qualify the final clean persistent-agent/voice suites.
- [ ] T040 Resolve the final live privacy refusal and complete a successful baseline/quiet-poll/result-recovery demonstration under a new explicitly bounded test authorization. The original assignment is stopped; its spending and failed receipts must not be reset, relabeled or bypassed.

- T001 precedes product edits; T003 precedes T004/T005. T006 can proceed alongside Plane after its public contract is fixed. Foundation must pass before story execution is enabled.
- US1 uses the foundation. US2 extends its execution ledger. US3 controls/budgets must be complete before enabling unattended work for any live owner. US4 uses US1–US3 authority/recovery controls.
- Plane owns one sequential edit lane (T004/T005/T008/T014/T016/T018/T019/T025); Deep owns another. Projection views and client parity tests run independently once action contracts are fixed. Shared files never have concurrent writers.
- T012 view tests can run alongside T009/T010. T013 recovery tests can run while Projection views are built. T017 control tests can run alongside T023 client contract work. T024 graph tests can run alongside client qualification.
- T027 exact component qualification precedes candidate image/live evidence; T028–T030 independent gates can run in parallel after relevant code is fixed. No completed gate is rerun without a code change, failure or unresolved concern.

## Implementation strategy

The first usable slice is the consented public-page monitor with quiet unchanged checks. It is not safe to enable until US2 recovery and US3 permission/budget/control gates pass. Add durable delegated investigation next, then perform the full live/client qualification. This sequencing does not omit any requested story.

## Requirement mapping

FR-001 T005/T007/T008/T011; FR-002 T005/T008/T010/T014; FR-003 T008/T010; FR-004 T006/T007/T009; FR-005 T006/T009; FR-006 T008/T013/T014/T015; FR-007 T013/T014/T015/T021; FR-008 T024/T025/T026; FR-009 T009/T017/T020/T024/T026/T028; FR-010 T009/T017/T020; FR-011 T006/T019/T020/T026; FR-012 T017/T021; FR-013 T021; FR-014 T017/T018/T020/T022; FR-015 T017/T018; FR-016 T012/T022/T023/T029; FR-017 T010/T012/T022; FR-018 T003/T005/T006/T016; FR-019 T002/T011/T028; FR-020 T001/T003/T004/T027–T032.

SC-001 T007/T030; SC-002 T013/T030; SC-003 T007/T010/T030; SC-004 T017/T030; SC-005 T017/T028/T030; SC-006 T024/T026/T030; SC-007 T010/T030; SC-008 T023/T029/T031.
