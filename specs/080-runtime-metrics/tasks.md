# Tasks: Runtime metrics

**Input**: spec.md, plan.md, research.md, data-model.md, contracts/metrics.md and quickstart.md in this directory.

## Phase 1: Setup

- [x] T001 Resolve new feature ownership and review constitution/current metrics seams in specs/080-runtime-metrics/research.md (FR-008).
- [x] T002 Define privacy, timing, reset and access contracts in specs/080-runtime-metrics/contracts/metrics.md (FR-001–FR-008, FR-010).

## Phase 2: Foundation

- [x] T003 Establish immutable-main isolated Python 3.11 test baseline and record it in specs/080-runtime-metrics/verification.md (FR-009).
- [x] T004 Review spec/plan/task coverage and existing ignore rules before implementation in specs/080-runtime-metrics/verification.md (FR-008–FR-010).

## Phase 3: US1 - Protect diagnostics

Independent test: authenticated admin succeeds, invalid/absent/non-admin principals cannot trigger collection, owner reconciliation unchanged.

- [x] T005 [P] [US1] Write denial/admin success and zero-collector-work tests in backend/tests/test_runtime_metrics_access_080.py (FR-001, FR-002, SC-001, SC-004).
- [x] T006 [US1] Apply existing admin dependency and document access in backend/orchestrator/api.py; adapt only the existing admin-success fixture in backend/tests/test_operation_api_060.py (FR-001, FR-002).

## Phase 4: US2 - Diagnose background latency

Independent test: controlled terminal projections and actual manager lifecycle yield exact aggregates without content or duplicated observations.

- [x] T007 [P] [US2] Write bucket, omission, strict-vocabulary, atomic concurrency and overflow tests in backend/tests/test_background_latency_080.py (FR-003–FR-006, SC-002, SC-003).
- [x] T008 [P] [US2] Write manager success/failure/cancel/never-started/deduplication/telemetry-failure tests in backend/tests/test_background_latency_integration_080.py (FR-003, FR-004, FR-006, FR-007, SC-002, SC-004).
- [x] T009 [US2] Add fixed atomic aggregates to backend/orchestrator/runtime_observability.py (FR-003–FR-006).
- [x] T010 [US2] Wire once-per-task terminal observation with content-free error containment in backend/orchestrator/async_tasks.py (FR-003, FR-006, FR-007).

## Phase 5: Verification and handoff

- [x] T011 Run focused tests, adjacent regressions and root Ruff; measure >=90% changed Python coverage and record exact results in specs/080-runtime-metrics/verification.md (FR-009, SC-001–SC-005).
- [x] T012 Review final implementation and operator interpretation, update specs/080-runtime-metrics/quickstart.md and verification.md, and checkpoint local candidate plus curated vault summary (FR-010).

## Dependencies and parallel work

T001–T004 precede implementation. T005/T007/T008 may be written independently; run them against main to verify the missing behavior. T006 follows T005; T009 follows T007; T010 follows T008/T009. T011 follows both stories; T012 follows validation and independent review. US1 is a separately testable security improvement; US2 uses the existing export independently of the access change.

## Before merge/deployment (separate qualification)

The local implementation tasks do not authorize publication. Real configured IAM and representative background task verification on the exact candidate, applicable full backend/module/image/boot/secret gates, independent review and ordinary release controls remain required before merge/promotion. Record outstanding evidence honestly in verification.md; do not fabricate completed tasks for external deployment evidence.
