# Tasks: Source-Bound Agent Actions

**Input**: [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md), [contract](contracts/source-bound-decision.md)

**Tests**: Required for the security core, denials, failures, approval, and actual dispatch. Native tests are omitted by owner instruction; web is included.

## Phase 1: Setup

- [ ] T001 Inventory canonical registered tools, aliases, scopes, and effect behavior in `backend/agents/`, `backend/orchestrator/`, and `backend/tests/test_source_bound.py`.
- [ ] T002 Verify Python 3.11, pinned Plane/Projection, local PostgreSQL, and web test setup in `backend/tests/test_source_bound_dispatch.py`.

## Phase 2: Foundational

- [ ] T003 [P] Define bounded source-origin, derived-origin, and unknown-lineage models in `backend/orchestrator/source_bound.py`; byte equality with signed user text must not upgrade trust.
- [ ] T004 Define canonical action declarations for read, mutation, disclosure, privilege, owner sensitivity, and aliases in `backend/orchestrator/source_bound.py`; missing/conflicting declarations must block possible effects.
- [ ] T005 Add model tests for origin union, mode-dependent tools, aliases, malformed declarations, and restart-unknown posture in `backend/tests/test_source_bound.py`.

## Phase 3: User Story 1 — Source Cannot Authorize (P1)

**Goal**: External or tool-produced instructions remain data across transformations and cannot cause an unrequested effect.

**Independent test**: A signed read succeeds; a tool result asking for a write reaches the real dispatcher and is denied before any physical effect.

- [ ] T006 [US1] Record source classes for structured results and presentation components, retained context, uploads, and remote replies in `backend/orchestrator/orchestrator.py` and `backend/orchestrator/source_bound.py`.
- [ ] T007 [US1] Bind signed actor/owner intent, canonical action, target, arguments, source state, and delegated authority at the shared `_authorize_and_prepare` path in `backend/orchestrator/orchestrator.py`.
- [ ] T008 [US1] Recheck the bound decision immediately before physical invocation, including retry and uncertainty, in `backend/orchestrator/orchestrator.py`.
- [ ] T009 [US1] Add real registered-dispatch tests for direct, derived, retained, delegated, delayed, alias, malformed-policy, and missing-lineage denials in `backend/tests/test_source_bound_dispatch.py`.

## Phase 4: User Story 2 — Exact Owner Approval (P2)

**Goal**: One reviewed source-derived action can run once under all ordinary gates.

**Independent test**: Approve one exact destination and material, then change owner/target/arguments or replay; only the original physical attempt may proceed.

- [ ] T010 [US2] Trigger exact source-derived review independently of optional taint/HITL flags, reusing the existing `authorize_action` card in `backend/orchestrator/orchestrator.py` and `backend/orchestrator/hitl_confirmation.py`.
- [ ] T011 [US2] Bind approval to owner/session, canonical action, destination, material/argument digest, current authority, expiry, and one physical attempt in `backend/orchestrator/hitl_confirmation.py`.
- [ ] T012 [US2] Make missing pre-effect audit receipt deny the effect; record allow, deny, approval, revocation, and uncertain outcome without raw sensitive values in `backend/orchestrator/hitl_confirmation.py` and `backend/orchestrator/orchestrator.py`.
- [ ] T013 [US2] Add denial, approved effect, replay, edited argument, expiry, revocation, audit failure, and unknown outcome tests in `backend/tests/test_source_bound_dispatch.py`.

## Phase 5: User Story 3 — Coverage and Operators (P3)

**Goal**: Every registered effect and alias has a reviewable declaration and failures are visible before use.

**Independent test**: Register an unclassified or contradictory effectful tool; interactive, delegated, and unattended channels refuse it with an audit diagnostic.

- [ ] T014 [US3] Add startup/runtime declaration inventory and precise operator diagnostics for unclassified tools in `backend/orchestrator/source_bound.py` and `backend/orchestrator/orchestrator.py`.
- [ ] T015 [US3] Verify classifications and denials across bundled, external, user-authored, delegated, scheduled, and retry paths in `backend/tests/test_source_bound_dispatch.py`.
- [ ] T016 [US3] Verify privacy-preserving decision records, audit-chain reconstruction, and failure behavior in `backend/tests/test_source_bound_dispatch.py`.

## Final Phase: Verification

- [ ] T017 Run targeted backend tests, all affected module suites, grouped backend web tests, Ruff, and changed-line coverage; record commands/results in `specs/097-source-bound-agent-actions/quickstart.md`.
- [ ] T018 Exercise the approval card and denial through the live local web backend, including altered/replayed action, and record results in `specs/097-source-bound-agent-actions/quickstart.md`.
- [ ] T019 Review the diff, validate that no Plane/protocol/native source changed, keep sensitive/generated files untracked, and record Apple/Android tests as user-requested omissions in `specs/097-source-bound-agent-actions/tasks.md`.

## Dependencies

T001–T002 precede T003–T005. US1 requires the foundational models. US2 requires US1's bound decision. US3 verifies the completed classification and approval paths. Verification follows all stories.

## Parallel Opportunities

Origin and action-declaration design can be reviewed independently before integration, but both touch `source_bound.py` and must be merged sequentially. Research on external-agent inventory can proceed while model tests are written.

## Implementation Strategy

Land the host enforcement core first, then exact approval and inventory coverage. Keep existing security gates active and test the physical effect path. No new client contract is planned; web testing is required, native testing intentionally omitted.
