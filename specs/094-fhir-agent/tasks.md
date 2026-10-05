# Tasks: FHIR Clinical Data agent

Input: spec.md and plan.md in this directory.

## Foundation

- [x] T001 Add the `fhir` flag (`FF_FHIR`, default off) in backend/shared/feature_flags.py (FR-002).
- [x] T002 Implement settings, guarded REST calls, paging and error codes in backend/agents/fhir/client.py (FR-003, FR-004, FR-006).
- [x] T003 Implement readings, thresholds, reference intervals, series and timeline events in backend/agents/fhir/clinical.py (FR-010).
- [x] T004 Implement one card builder per tool in backend/agents/fhir/presentation.py (FR-005, FR-010).

## US1 and US2: census and patient views

- [x] T005 [US1] Implement `icu_census` and `fhir_source_status` in backend/agents/fhir/mcp_tools.py.
- [x] T006 [US2] Implement `patient_overview`, `vital_sign_trends`, `laboratory_results`, `medication_review` and `patient_timeline`.
- [x] T007 [US2] Implement `query_fhir_records` with a closed resource-type list and validated filters (FR-003, FR-008).

## US3: live feed

- [x] T008 [US3] Implement `watch_icu_activity` as a push-streaming tool over topic subscriptions, with bounded duration and cleanup (FR-009).
- [x] T009 [US3] Serve the first chunk on the non-streaming path in backend/agents/fhir/mcp_server.py.

## US4: gating and safety

- [x] T010 [US4] Register the id and the gated directory in backend/orchestrator/local_agents.py; gate the subprocess in backend/start.py; filter the safe seed in backend/orchestrator/orchestrator.py (FR-002).
- [x] T011 [US4] Classify the agent as untrusted in backend/orchestrator/taint.py (FR-007) and add its public messages in backend/orchestrator/tool_feedback.py (FR-006).
- [x] T012 [US4] Update the seed expectations in backend/tests/test_remote_orchestrator_wiring_063.py.

## Tests and documentation

- [x] T013 Write backend/agents/fhir/tests with an in-memory FHIR server covering client, view models, every tool, the feed, dispatch and gating (SC-001).
- [x] T014 Document the flag and settings in .env.example and docs/fhir-agent.md; list the agent in README.md.
- [ ] T015 Record local, image-gate and live results in verification.md (SC-002, SC-003).
