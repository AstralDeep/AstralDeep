# Tasks: Rewrite integration

Input: feature 088 spec, plan, research, inventory, data model and contracts. Tests are required by the spec and repository policy. `[P]` means disjoint files within that phase after its prerequisites, not permission to skip named dependencies. Proposed files are created only after their existing owner interfaces are verified.

## Phase 1: Setup

Preserve ownership and make verification reproducible.

- [x] T001 Freeze donor/retained capability provenance and assign acceptance owners in specs/088-rewrite-integration/source-inventory.json (FR-025).
- [x] T002 Record exact baseline, local component branch identities and environment/tooling gaps in specs/088-rewrite-integration/verification.md; preserve donor and unrelated trees (FR-001, FR-026).
- [x] T003 Verify existing ignore rules and install only declared local component/test tooling through scripts/install_local_components.py; retain isolated tooling manifests and record commands in specs/088-rewrite-integration/verification.md (FR-027).

## Phase 2: Foundation

Establish interfaces and baseline regression locations before implementation; these tasks do not require implementing later stories.

- [x] T004 Verify completed runtime/UI contracts against actual existing facades and all requirement-to-task mappings in specs/088-rewrite-integration/contracts/ and tasks.md; resolve critical analysis findings before runtime edits (FR-023, FR-025).
- [x] T005 Record baseline tests and five reproduced-defect integration scenarios in specs/088-rewrite-integration/verification.md and source-inventory.json; keep donor evidence separate (SC-003).

## Phase 3: US1 — Simple first task (P1)

Independent test: one Send starts ordinary chat/public research; all existing controls remain reachable, drafts are owner-safe, and 320px/200% text remains usable. Initial welcome/shell work can proceed independently of later durable APIs.

- [ ] T006 [P] [US1] Add focused welcome hierarchy/action/retirement and accessibility regressions in backend/tests/test_welcome.py and backend/tests/test_welcome_identity.py (FR-003, FR-004, FR-028).
- [ ] T007 [US1] Simplify default welcome and disclose additional examples using existing primitives in backend/orchestrator/welcome.py; preserve opt-in permissions and ordinary chat_message dispatch (FR-003, FR-004, FR-005).
- [ ] T008 [US1] Simplify shell copy/composer styling and accessible names in components/AstralProjection/backend/webrender/templates/shell.html and static/astral.css; preserve theme roles, voice and attachment hooks (FR-003, FR-028).
- [ ] T009 [US1] Add owner-change/logout/reconnect and both bootstrap-order browser regressions under components/AstralProjection/tests/; implement verified-owner draft/config lifecycle in components/AstralProjection/backend/webrender/static/client.js if needed, without importing donor global state (FR-011, FR-012, SC-003).
- [ ] T010 [US1] Add shared recent-work/result view builders in components/AstralProjection/src/astralprojection/chrome/work.py and authorized adapters in backend/orchestrator/projection_surfaces/work.py after T027; expose detail through registered surfaces (FR-003, FR-023).
- [ ] T011 [US1] Wire server-owned navigation and full advanced composition selections through components/AstralProjection/backend/webrender/chrome/menu_model.py and backend/orchestrator/chrome_events.py after T027/T037; retain attachment/voice/history and role filtering (FR-003, FR-004, FR-023).
- [ ] T012 [US1] Add public offline document/manifest and safe static-only worker packaging under components/AstralProjection/backend/webrender/static/ with resource/cache tests in tests/test_resources.py; never cache personalized shell/API/auth (FR-023, FR-028).
- [ ] T013 [US1] Run integrated ordinary-Send/effect-review, narrow/zoom/keyboard and five-participant first-use checks; retain actual observations in specs/088-rewrite-integration/verification.md (SC-001, SC-002, SC-007).

## Phase 4: US2 — Preserve ecosystem identity and capabilities (P1)

Independent test: existing accounts, providers, tools and clients still work; upgrade preserves populated data and IAM denials.

- [ ] T014 [P] [US2] Add institutional issuer/client/BFF/native compatibility and owner denial fixtures under backend/tests/ using existing auth interfaces; preserve cookie CSRF, azp and RFC 8693 behavior (FR-002).
- [ ] T015 [US2] Integrate new work ingress with current Keycloak/session context in backend/orchestrator/auth.py and proposed backend/orchestrator/work_api.py; do not introduce donor SessionService or identity tables (FR-002, FR-010).
- [ ] T016 [US2] Add provider breadth and user/system separation cases under backend/llm_config/tests/; qualify optional local inference framing/accounting through backend/llm_config/client_factory.py without removing existing providers (FR-019).
- [ ] T017 [US2] Bind provider endpoint/key retention edits explicitly in backend/orchestrator/projection_surfaces/llm.py and existing encrypted provider services; test changed destination, keep/replace/remove and redaction (FR-019).
- [ ] T018 [US2] Exercise retained clinical/bundled/external tools, attachments, voice, workspace, remote execution and automatic skills/memory through existing backend/tests/ and module suites; record each retained inventory row (FR-001, FR-025).
- [ ] T019 [US2] Qualify populated/repeated Plane upgrade and recovery with exact matching application pins in components/AstralPlane/tests/test_schema_migrations.py and specs/088-rewrite-integration/verification.md after all schema tasks; preserve owner associations, unresolved effects and audit (FR-026, SC-005).
- [ ] T020 [US2] Run real institutional web/native sign-in and two-owner/revocation/egress/provider denials against isolated candidate staging; record exact artifacts and human-performed sign-in in specs/088-rewrite-integration/verification.md (FR-002, SC-006).

## Phase 5: US3 — Durable work (P1)

Independent test: restart/retry/cancel/control operations preserve logical identity, charges and effect fences; ephemeral sources are reacquired.

- [ ] T021 [P] [US3] Add one-shot/legacy profile, mixed capacity, owner isolation and idempotency fixtures in components/AstralPlane/tests/repositories/test_assignments.py and test_work_admission.py (FR-006, FR-007).
- [ ] T022 [US3] Implement additive assignment execution profile and source-less one-shot constructor using existing models/repositories in components/AstralPlane/src/astralplane/repositories/assignment_models.py and assignments.py; preserve legacy validators/capacity (FR-004, FR-006).
- [ ] T023 [US3] Add guarded 088 migration/profile/idempotency receipts and contract metadata in components/AstralPlane/src/astralplane/database/migrations.py; reserve exact revision only after component collision check (FR-006, FR-026).
- [ ] T024 [US3] Add wait/wake receipts, current version/control validation and terminal result projection to existing Plane assignment repository with denial/concurrency tests; preserve cancellation/reconciliation semantics (FR-007).
- [ ] T025 [US3] Add transient-input/reconstruction disposition and result-availability metadata to Plane action models/repository; test no private plaintext retention and stale settlement-only behavior (FR-009, FR-018).
- [ ] T026 [US3] Integrate one-shot episode handling and both current execution fences into backend/persistent_agents/runner.py, service.py and execution.py; use current dispatcher, authority and provider resolution (FR-006, FR-010).
- [ ] T027 [US3] Implement owner-authorized operation service and versioned commands/read models in proposed backend/orchestrator/work_service.py and work_api.py, registering through backend/orchestrator/api.py; preserve old payload-free reconciliation (FR-004, FR-006, FR-007).
- [ ] T028 [US3] Implement source read/extraction/grounding and bounded retry in the one-shot handler under backend/persistent_agents/; reacquire non-retained content with new charged action identities and no unsupported conclusions (FR-009, FR-013).
- [ ] T029 [US3] Add PostgreSQL/dispatch concurrency and crash-window fixtures under backend/persistent_agents/tests/ for duplicate acceptance, reservation/permit/publication, pause/cancel and revocation; adapt the ephemeral-source regression (SC-003, SC-006).
- [ ] T030 [US3] Add current-authority SSE/poll delivery, stable pagination/resync and lifecycle controls in backend/orchestrator/work_api.py with late-response and revocation tests under backend/tests/ (FR-006, FR-007, FR-010).

## Phase 6: US4 — Agents, guidance and private notes (P2)

Independent test: create/revise/select/disable/delete agent/skill/note; current values and revisions govern dispatch, existing skills remain available and Forget prevents future use.

- [ ] T031 [P] [US4] Add declarative agent lifecycle and retained executable/remote agent tests under backend/tests/ and components/AstralPlane/tests/repositories/ (FR-015).
- [ ] T032 [US4] Extend existing agent revision metadata and Deep authoring service for declarative activation/archive/clone/history with normal trust validation in components/AstralPlane/src/astralplane/repositories/agents.py and backend/orchestrator/projection_surfaces/authoring.py (FR-015).
- [ ] T033 [US4] Implement Plane skill head/revision catalog and encrypted-current-note metadata/CAS/purge with guidance reference invalidation through repositories/preferences.py and guarded database/migrations.py; add owner/revision/expiry/Forget tests (FR-016, FR-017, FR-018).
- [ ] T034 [US4] Route all old/new mutable user-skill paths through one revision facade in backend/orchestrator/user_skills.py; qualify controlled legacy Markdown materialization/conflicts while preserving files, packs, recipes and existing behavior (FR-016, FR-026).
- [ ] T035 [US4] Implement encrypted explicit-note service and bounded deterministic expansion in backend/personalization/ with owner/revision-bound encryption and no value-history persistence; preserve existing automatic memory (FR-017, FR-018).
- [ ] T036 [US4] Integrate selected agent/skill/note head checks at acceptance/prepare/permit/result/publication in backend/orchestrator/work_service.py and backend/persistent_agents/execution.py; test edit/Forget versus queued/in-flight work (FR-018, SC-006).
- [ ] T037 [US4] Add shared guidance/agent/note views and exact selected-revision forms in components/AstralProjection/src/astralprojection/chrome/guidance.py plus authorized backend/orchestrator/projection_surfaces/guidance.py adapters (FR-015, FR-016, FR-017, FR-023).
- [ ] T038 [US4] Exercise agent/guidance/private-note workflows live, including correction/expiry/Forget and no private text in audit/source/effect history; record evidence in specs/088-rewrite-integration/verification.md (FR-015, FR-016, FR-017, FR-018).

## Phase 7: US5 — Monitoring and saved results (P2)

Independent test: initial/unchanged/changed/insufficient-evidence observations, exact approved saving and effective Stop; held schedules do not starve another owner.

- [ ] T039 [P] [US5] Add held-job fairness, finite allowance, pause/Stop/last-run and cron/interval/one-shot regression fixtures in components/AstralPlane/tests/repositories/test_scheduler.py and backend/scheduler/tests/ (FR-008, FR-014, SC-003).
- [ ] T040 [US5] Add optional scheduler policy/observation and atomic occurrence-to-assignment admission/Stop using components/AstralPlane/src/astralplane/repositories/scheduler.py and guarded migration registry; preserve existing recurrence semantics (FR-008, FR-014).
- [ ] T041 [US5] Integrate bounded eligible-owner rotation and continuation in existing backend/scheduler/ service and Plane scheduler; do not apply oldest-N before eligibility or change semantic due times to mask starvation (FR-008).
- [ ] T042 [US5] Implement initial extractive/unchanged/changed/insufficient-evidence monitoring over existing source/checkpoint contracts in backend/persistent_agents/ with exact prior-result bindings and allowance preservation (FR-013, FR-014).
- [ ] T043 [US5] Integrate exact result proposal/save/download/provenance through existing backend/orchestrator/projection_surfaces/workspace_timeline.py and persistent action approval/publication interfaces; reject stale/consumed review and reconcile unknown effects (FR-005, FR-013).
- [ ] T044 [US5] Add shared recurring-work and saved-result views through components/AstralProjection/src/astralprojection/chrome/assignments.py and workspace.py with host adapters under backend/orchestrator/projection_surfaces/ (FR-014, FR-023).
- [ ] T045 [US5] Run real monitoring all four outcomes, unchanged generation avoidance, exact result approval and Stop with cleanup and grounding-quality observations in specs/088-rewrite-integration/verification.md (SC-008).

## Phase 8: US6 — Frameworks and outcomes (P2)

Independent test: REST/MCP/A2A/SDK/framework clients share admission and controls; revocation races deny mint/use and diagnostics preserve privacy/unknowns.

- [ ] T046 [P] [US6] Add owner-scoped framework mint contention/revocation/expiry and finite-grant allowance fixtures in components/AstralPlane/tests/authority/ and backend/tests/; distinguish independent credential issuance from recursive delegation (FR-010, FR-020, SC-003).
- [ ] T047 [US6] Extend existing credentials/offline_grants repositories with scoped hash-only credentials, issuer lineage and allowance CAS, same-domain revoke/mint locks and guarded migrations; recheck current time/authority at final insert (FR-010, FR-020).
- [ ] T048 [US6] Add Deep framework credential/grant service and authorized connection surfaces in proposed backend/orchestrator/projection_surfaces/connections.py plus shared Projection builders; show secrets only once and preserve existing MCP JWT ingress (FR-020).
- [ ] T049 [US6] Add versioned work compatibility to backend/orchestrator/mcp_server_endpoint.py, mcp_projection.py and a2a_orchestrator_executor.py; use the common operation service and deny human-only commands (FR-021).
- [ ] T050 [US6] Port synchronous/asynchronous SDK and supported donor framework adapters into proposed sdk/ package with existing transport libraries and generated work contracts; keep optional framework dependencies out of backend runtime (FR-021).
- [ ] T051 [US6] Add official-client/framework conformance, denial, cancellation, stream revocation and uncertain-effect fixtures under backend/tests/ and sdk/tests/; exercise separate real client processes (FR-021).
- [ ] T052 [US6] Expose bounded owner outcomes/timing/usage and role-filtered diagnostics from existing telemetry repositories through work read models and shared Projection disclosures; distinguish unknown/estimated/uncertain from zero/success (FR-022).
- [ ] T053 [US6] Qualify adopted runtime security/egress limits, provider process cleanup, admission accounting and framework features against every inventory row in specs/088-rewrite-integration/source-inventory.json (FR-010, FR-019, FR-020, FR-021, FR-022).

## Phase 9: US7 — Native compatibility and later redesign (P2)

Independent test: existing native workflows continue during web delivery; later redesign renders the same server surfaces and effects with actual client evidence.

- [ ] T054 [US7] Define/test explicit web-first server capability/disposition matrix in components/AstralProjection/contracts/ui_protocol.json and components/AstralProjection/backend/rote/ without new primitive vocabulary; retain old capabilities and forbid incomplete review execution (FR-023, FR-024).
- [ ] T055 [P] [US7] Update Windows shared contract/disposition consumption and drift fixtures under components/AstralProjection/windows-client/; run protocol/chrome and existing workflow compatibility tests (FR-024).
- [ ] T056 [P] [US7] Update Android shared contract/disposition consumption and drift fixtures under components/AstralProjection/android-client/; run lint/JUnit/Kover/build gates (FR-024).
- [ ] T057 [P] [US7] Update Apple shared contract/disposition consumption and drift fixtures under components/AstralProjection/apple-clients/; run Swift/XCTest and affected build gates on macOS (FR-024).
- [ ] T058 [US7] Verify live native compatibility against the exact web milestone and record platform-specific evidence in specs/088-rewrite-integration/verification.md; unavailable execution remains open (FR-024, SC-007).
- [ ] T059 [P] [US7] Complete Windows redesigned navigation/composition/work/guidance/settings surfaces under components/AstralProjection/windows-client/ using server models; verify live keyboard/narrow/zoom and authority/effect flows (FR-024, FR-028).
- [ ] T060 [P] [US7] Complete Android redesigned surfaces under components/AstralProjection/android-client/ using server models; verify live device/emulator layout, lifecycle and authority/effect flows (FR-024, FR-028).
- [ ] T061 [P] [US7] Complete Apple redesigned surfaces under components/AstralProjection/apple-clients/ using server models; verify macOS/iOS/watchOS affected flows and layout live (FR-024, FR-028).

## Phase 10: Qualification and handoff

Complete all required feature and evidence work; no declaration of full integration while an inventory family or required live check is open.

- [ ] T062 Run all touched component/backend/module CI suites, changed coverage, maintained-language lint, contract generation/drift and dependency/secret/security checks; retain exact commands/results in specs/088-rewrite-integration/verification.md (FR-027).
- [ ] T063 Qualify readiness, representative backup/restore, audit chain and current-authority failure behavior in isolated exact-artifact staging; record candidate-bound evidence in specs/088-rewrite-integration/verification.md (FR-026, FR-027).
- [ ] T064 Commit reviewed qualified component changes separately and update exact contract/schema/digest/component pins in config/astral-composition.json and gitlinks; test installed wheels and fail-closed composition (FR-023, FR-026).
- [ ] T065 Reconcile every donor/retained row with integrated implementation and evidence in specs/088-rewrite-integration/source-inventory.json; full completion requires no open required rows (FR-025, SC-004).
- [ ] T066 Update operator/API migration/recovery documentation in specs/088-rewrite-integration/quickstart.md and tracked component docs, plus the curated knowledge-vault checkpoint; leave precise local handoff without product push claims (FR-027).
- [ ] T067 Run current scripts/prepare_release_evidence.py with canonical exact-candidate inputs before any separately authorized product push; preserve fail-closed missing evidence and existing protected publisher boundaries (FR-027).

## Dependencies and parallel execution

Setup/foundation T001–T005 precede runtime edits. Initial US1 welcome/shell T006–T009 are independently useful through current dispatch. Domain-backed US1 views depend on US3 services and US4 guidance; do not expose unimplemented actions. US2 identity/provider tests can run alongside US3 storage work; same-file API or migration edits are sequential and owned by one agent. US4/US5/US6 depend on US3 lifecycle contracts, then may proceed in separate files. US7 compatibility is required before web qualification; native redesign follows the qualified web milestone. Final qualification waits for every required family.

Parallel examples: US1 welcome tests and Projection source baselines; US2 auth and provider fixtures; US3 Plane profile fixtures and Deep failure-case design; US4 agent fixtures and note threat cases; US5 scheduler fixtures and result view tests; US6 protocol client fixtures and mint race fixtures; US7 Windows/Android/Apple implementations in separate trees. Root integrates shared API, migration and composition edits sequentially.

## Delivery strategy

First increment: calm welcome/shell over existing ordinary dispatch, with tests. Next: durable work/read models, identity/provider compatibility and common source/guidance contracts. Then integrate all remaining donor capabilities and qualify the complete web milestone with native compatibility. Finally complete native redesign and all evidence. An increment is not the full requested integration; task checkboxes and inventory evidence must remain truthful. Local work is authorized; product push/deploy/release are not implied.

