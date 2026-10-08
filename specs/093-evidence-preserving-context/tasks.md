# Tasks: Evidence-preserving agent context

Input: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/context.md](contracts/context.md). Full feature authorized in one PR. Tests are mandatory under Constitution III.

## Phase 1: Setup

- [X] T001 Reconcile authorized branch with main and verify exact feature paths in specs/093-evidence-preserving-context/spec.md.
- [X] T002 Complete clarification/design/contracts and requirements-quality review in specs/093-evidence-preserving-context/plan.md.
- [X] T003 Verify .gitignore/.dockerignore, declared Python 3.11 environment, exact components and root pyproject.toml lint configuration.

## Phase 2: Foundation

- [X] T004 Specify/test all four independent default-off controls and legacy precedence in backend/tests/test_evidence_context.py.
- [X] T005 Add independent flags/configuration in backend/shared/feature_flags.py and .env.example; host adapter lazily initializes only enabled work.

## Phase 3: US1 Exact recall (P1)

Goal: recover omitted facts without repeated source operations.
Independent test: reconstruct permitted Unicode text page by page; deny foreign/revoked/corrupt/expired sources and refuse unsafe admission.

- [X] T006 [P] [US1] Add reconstruction/identity/capacity/Unicode/retention/concurrency/restart denial tests in backend/tests/test_evidence_archive.py.
- [X] T007 [US1] Implement exact grants/immutable archive in backend/orchestrator/evidence_archive.py with <=16 KiB/page, <=8 MiB/observation, <=64 MiB/conversation and <=24h absolute expiry plus stricter global/privacy limits (FR001-009,015,019; SC001-002,009).
- [X] T008 [US1] Bind fresh source policy/permission/delegation/PHI/integrity/lifecycle checks in backend/orchestrator/evidence_context.py; absent retention grants refuse capture (FR005-009,015).
- [X] T009 [US1] Add ordinary evidence agent tools and trusted dispatch binding in backend/agents/evidence/evidence_agent.py and backend/agents/evidence/mcp_server.py; register through backend/orchestrator/local_agents.py (FR005,015,019).
- [X] T010 [US1] Capture/preview only after admission and suppress lossy transforms; preserve untrusted taint/datamarking in backend/orchestrator/orchestrator.py and backend/orchestrator/taint.py (FR003,006,009,019).
- [X] T011 [US1] Verify ordinary-dispatch denials, same-turn revocation, actual source outcome, deletion and transient delivery in backend/tests/test_evidence_context.py (SC001-002,009).

## Phase 4: US2 Safe compaction (P1)

Goal: failed/invalid compaction cannot remove history or host authority.
Independent test: injected failure/cancellation/stale completions preserve all original messages and protected host state; success reduces within budget.

- [X] T012 [P] [US2] Add failure/empty/malformed/cancellation/stale/current-user/tool-group/protected-state/budget tests in backend/tests/test_safe_compaction.py.
- [X] T013 [US2] Implement immutable proposal/validation/result in backend/orchestrator/safe_compaction.py, preserving all system/developer/current-user/tool groups verbatim and generated evidence authority (FR010-013,018; SC003,009).
- [X] T014 [US2] Bind exact owner provider/content route, bounded calls and host/fence snapshot in backend/orchestrator/evidence_context.py; refuse unavailable/changed authority and persistent dispatch without separate reservation (FR005,012,014,015).
- [X] T015 [US2] Integrate enabled path before legacy history slicing/compaction/tombstones; pause honestly on irreducible context in backend/orchestrator/orchestrator.py (FR011,018-019).
- [X] T016 [US2] Verify owner-provider/no-fallback/flags/cancellation/host-change/context-limit behavior in backend/tests/test_evidence_context.py (SC002-003,009).

## Phase 5: US3 Honest evidence and accounting (P2)

Goal: distinguish source states and whole-operation known/unknown costs on every client.
Independent test: replay/late completion reconciles known synthetic charges once; literal source survives every client adapter.

- [X] T017 [P] [US3] Add physical-attempt/cache/retry/failure/cancellation/late/unknown/dedup/restart tests in backend/tests/test_context_usage.py.
- [X] T018 [US3] Implement owner/conversation attempt ledger and stable bounded audit identities in backend/orchestrator/context_usage.py, backend/audit/schemas.py and backend/audit/repository.py (FR014-016; SC005,009).
- [X] T019 [US3] Instrument direct/auxiliary/stream/title/tool-summary provider attempts and recall bytes in backend/orchestrator/orchestrator.py and backend/orchestrator/evidence_context.py (FR016; SC005).
- [X] T020 [P] [US3] Add literal-source/status/unknown-cost/fallback tests in backend/tests/test_context_presentation.py.
- [X] T021 [US3] Implement server-owned KeyValue/Badge/Card/Alert presentation in backend/orchestrator/context_presentation.py, with transient pages and explicit watch action handoff (FR013,017; SC004).
- [ ] T022 [US3] Exercise full/preview/summary/missing/blocked/usage UI against candidate live backend on web/macOS/iOS/watchOS (owner omitted Android and Windows OS/live; record both untested) and retain evidence in specs/093-evidence-preserving-context/verification.md (SC004).

## Phase 6: US4 Evaluation and promotion (P2)

Goal: independently assess packing, compaction and their combination without assumed research gains.
Independent test: fixed paired oracle/cost/unknown/failure cases prevent promotion; held-out results remain frozen.

- [X] T023 [P] [US4] Add fixed-split/paired/failure/unknown/promotion tests in backend/tests/test_context_evaluation.py.
- [X] T024 [US4] Implement 24 synthetic workflows with separate 12/12 development/holdout, candidate/fixture/provider/scoring identities and four controls in backend/context_evaluation/workflows.json and backend/context_evaluation/evaluate.py (FR020-021; SC006-007,009).
- [X] T025 [US4] Record actual available real-provider cost and five-user formative evidence or explicit missing promotion inputs in specs/093-evidence-preserving-context/verification.md; neither missing nor failed evidence permits default promotion (SC007-008).

## Phase 7: Qualification

- [X] T026 Review critical authorization/retention/accounting/concurrency seams and complete focused/legacy tests, root lint and >=90% changed-line coverage; record exact results in specs/093-evidence-preserving-context/verification.md.
- [ ] T027 Run complete backend/module/CI image/boot/secret/composition gates with existing <=30-minute deterministic job budgets and record in specs/093-evidence-preserving-context/verification.md.
- [ ] T028 Update PR with concrete behavior/checks/limits, publish exact candidate and verify required hosted green CI before normal merge; update specs/093-evidence-preserving-context/verification.md and curated vault checkpoints.

## Dependencies and parallel work

T001-005 precede story implementation. Archive T006-007 and compaction T012-013 are independent modules and can execute in parallel. T008-011 and T014-016 share the host adapter/runtime and integrate sequentially. Accounting T017-018 can run independently once the public contracts are fixed; T019 follows it. Presentation T020-021 follows archive result shapes; evaluation T023-024 follows feature functions. Full T022/025-028 qualify the integrated candidate. No two agents edit the same source file concurrently.

## Implementation strategy

The owner selected the full feature, so no partial-story PR is substituted. Validate independent modules first, then ordinary dispatch and every control combination, then all required local/hosted/live checks. Missing live runners/sign-ins cannot be replaced with mocks or a passing CI label. Keep both defaults off; promotion remains blocked until every required acceptance measurement exists and passes.
