# Tasks: Real Agent Security Qualification

**Input**: [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md), [contract](contracts/qualification-report.md)

**Tests**: Required by the feature spec and the repository constitution. Each phase has denial, failure, and control cases.

## Phase 1: Setup

- [ ] T001 Verify the Python 3.11, PostgreSQL, pinned component, and backend module-test environments in `backend/security_benchmark/tests/` and `scripts/run_backend_web_tests.py`.
- [ ] T002 Pin the evaluation-only AgentDojo release and document its immutable identity in `backend/security_benchmark/requirements-eval.txt` and `backend/security_benchmark/README.md`.

## Phase 2: Foundational

- [ ] T003 [P] Define validated case, run-identity, trace, and outcome models in `backend/security_benchmark/qualification.py`; unknown or missing effect/audit states must be indeterminate.
- [ ] T004 [P] Add report schema validation and exact-candidate serialization in `backend/security_benchmark/qualification_report.py` per `contracts/qualification-report.md`.
- [ ] T005 Add unit tests for malformed cases, missing receipts, effect-state truth, and report label separation in `backend/security_benchmark/tests/test_qualification.py`.

## Phase 3: User Story 1 — Actual Boundary (P1)

**Goal**: A reported real case traverses signed human admission, ordinary dispatch, physical fixture effect, and audit.

**Independent test**: A permitted read and forbidden write leave no effect, while a deliberately weakened gate creates an observed effect and fails qualification.

- [ ] T006 [US1] Implement an isolated loopback read tool and physical effect sink in `backend/security_benchmark/drivers/real_fixture.py`, using registered scopes and namespaced state.
- [ ] T007 [US1] Implement signed registered-turn execution with a deterministic proposer and the real `handle_chat_message` / `execute_single_tool` path in `backend/security_benchmark/drivers/real_dispatch.py`; do not reuse verification's delegation override.
- [ ] T008 [US1] Record pre/post effect state and verify audit chain and cleanup in `backend/security_benchmark/drivers/real_dispatch.py`; absent evidence must withhold a pass.
- [ ] T009 [US1] Add database-backed integration tests for refusal, allowed control, forced gate regression, missing audit, and cleanup failure in `backend/security_benchmark/tests/test_real_dispatch.py`.
- [ ] T010 [US1] Remove the false in-process real claim from `backend/security_benchmark/drivers/inprocess.py`, `backend/security_benchmark/drivers/__init__.py`, and `backend/security_benchmark/README.md`; route the real label only to the qualifying driver.

## Phase 4: User Story 2 — Corpus and Adaptive Evidence (P2)

**Goal**: Corpus identity, supported population, attack attempts, effects, and benign utility remain distinct.

**Independent test**: Missing external corpus, unsupported mapping, and zero eligible cases fail; a supported attack/control pair reports separate effect and utility.

- [ ] T011 [P] [US2] Replace silent sample fallback with explicit sample/external selection and pinned AgentDojo case loading in `backend/security_benchmark/adapters/agentdojo.py`.
- [ ] T012 [P] [US2] Make ASB and InjecAgent real-corpus requests fail visibly until supported adapters exist in `backend/security_benchmark/adapters/asb.py` and `backend/security_benchmark/adapters/injecagent.py`.
- [ ] T013 [US2] Add at least 12 supported AstralDeep-specific attack/control pairs across three lower-trust surfaces with multi-hop, delegated, retained-context, and delayed-action cases in `backend/security_benchmark/adapters/astral.py`.
- [ ] T014 [US2] Add a mapping manifest and tests for supported AgentDojo cases, exclusions, version/content mismatch, empty population, and paired utility in `backend/security_benchmark/adapters/agentdojo.py` and `backend/security_benchmark/tests/test_corpus_mapping.py`.
- [ ] T015 [US2] Add a held-out adaptive research-run contract and separation from required CI evidence in `backend/security_benchmark/qualification_report.py` and `backend/security_benchmark/tests/test_qualification.py`.

## Phase 5: User Story 3 — Exact Candidate Gate (P3)

**Goal**: A release owner sees truthful candidate-bound evidence, and the offline check fails on observed unauthorized effects.

**Independent test**: A changed candidate/policy/corpus digest or incomplete teardown invalidates an older pass; the deterministic set finishes within the hosted suite budget.

- [ ] T016 [US3] Add an explicit real-dispatch qualification command, nonzero failure exits, and diagnostic-mode labels in `backend/security_benchmark/__main__.py`.
- [ ] T017 [US3] Include the required offline gate in the backend module test group without live network or whole-suite retries in `scripts/run_backend_web_tests.py` or the existing CI module workflow.
- [ ] T018 [US3] Add exact-candidate, missing-input, mutated-gate, repeatability, and teardown tests in `backend/security_benchmark/tests/test_qualification_gate.py`.
- [ ] T019 [US3] Update `backend/security_benchmark/README.md` and `specs/098-real-agent-security-qualification/quickstart.md` with exact commands and honest supported-population claims.

## Final Phase: Verification

- [ ] T020 Run targeted benchmark and affected backend tests, Ruff, changed-line coverage, grouped backend web tests, and a local live web dispatch smoke; record native tests as user-requested omissions in `specs/098-real-agent-security-qualification/quickstart.md`.
- [ ] T021 Review the diff, remove generated/sensitive artifacts, validate contract and constitution alignment, and record exact results in `specs/098-real-agent-security-qualification/tasks.md`.

## Dependencies

T001–T002 precede T003–T005. US1 requires T003–T005; US2 can start after the foundational models but its external mapping test depends on US1's fixture. US3 requires US1 and US2. Verification follows all stories.

## Parallel Opportunities

- T003 and T004 use different files.
- T011 and T012 touch separate adapters.
- Corpus research can proceed while US1 builds the real dispatch driver.

## Implementation Strategy

Deliver US1 as the first independently testable increment, then add truthful corpus coverage and exact-candidate gating. Synthetic ablations remain diagnostics throughout. No native client changes or tests are planned; web and backend evidence delimit the result.

## Current checkpoint (2026-10-20)

The scripted registered-turn fixture, effect oracle, candidate-bound report, paired local cases, and fail-closed legacy/external labels are implemented on the combined branch. The checkboxes above remain open where the real PostgreSQL integration, external corpus mapping, distinct delegated/delayed cases, mutated-gate regression, held-out study, and full CI/coverage proof have not been completed. Apple and Android tests are intentionally omitted by owner instruction; web-only client checks passed locally. See `quickstart.md` for exact evidence and limits.
