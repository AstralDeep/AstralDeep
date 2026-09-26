# Tasks: Native UI v2 parity

## Phase 1: Setup and reference prerequisites

- [x] T001 Verify local/remote ownership, reserve feature090, record scope and preserve unrelated changes in `specs/090-native-ui-v2/spec.md` and `../kos-wiki/wiki/synthesis-astral-native-ui-v2.md`.
- [x] T002 Build/start current Docker backend, verify composition/health/runtime source, and record setup evidence in `specs/090-native-ui-v2/verification.md`; repair `Dockerfile` build prerequisites when required.
- [x] T003 [US3] Capture/inspect current signed-in web states at every required viewport in `../kos-wiki/assets/astral-native-ui-v2/`; index hashes/state/source anchors in `../kos-wiki/wiki/synthesis-astral-native-ui-v2.md` and commit/push the vault before native UI changes.

## Phase 2: Shared foundation

- [x] T004 Add valid/malformed/legacy console contract and breakpoint fixtures in `components/AstralProjection/contracts/fixtures/` and tests in `components/AstralProjection/tests/rote/` and `tests/chrome/`.
- [x] T005 Implement negotiated console chrome and ROTE presentation/capability-change adaptation in `components/AstralProjection/backend/webrender/chrome/` and `backend/rote/`; preserve non-negotiating Windows/watch payloads.
- [x] T006 Reuse authenticated landing catalog and shared chrome delivery in `backend/orchestrator/web_landing.py` and `orchestrator.py`; add owner/role/denial tests under `backend/tests/` and `backend/orchestrator/tests/`.
- [x] T007 Extend the existing native guidance selection and agent-intro surface contract in `backend/orchestrator/projection_surfaces/`, shared Projection builders/adapters and `components/AstralProjection/contracts/ui_protocol.json`, preserving request/owner/connection fences and ordinary dispatch.
- [ ] T008 Verify web behavior/shared-definition extraction through `components/AstralProjection/tooling/web-ci/tests/` and preserve exact web reference appearance; qualify relevant Python source changes at90% changed-line coverage.

## Phase 3: Apple experience (US1)

Goal: match the web on iPhone, iPad and Mac. Independent test: same signed-in landing, chat/result, history, fullscreen, composer and settings walkthrough at comparable dimensions. Exclude the bottom web voice warning and verify native voice-worker connection, recording, playback and failure recovery during T015–T016.

- [x] T009 [US1] Add console/ROTE/selection decoding, capability aggregation and malformed-state tests in `components/AstralProjection/apple-clients/AstralCore/Sources/` and `Tests/`; update native dispositions/mirrors while preserving watch-specific fallback correctness.
- [x] T010 [US1] Integrate console/ROTE/selection in `components/AstralProjection/apple-clients/AstralApp/AstralApp/AppModel.swift`, including reset/reconnect/owner fences, Load/Run behavior and current viewport/scale/input/voice aggregation; test in `AstralAppTests/`.
- [x] T011 [US1] Implement sidebar/drawer, History, agent search, account/settings and landing scenarios in `components/AstralProjection/apple-clients/AstralApp/AstralApp/Views/RootView.swift`, `ChatListView.swift` and native console views; match reference layouts.
- [x] T012 [US1] Implement single conversation feed, response previews/actions, reasoning/summary placement and fullscreen in `components/AstralProjection/apple-clients/AstralApp/AstralApp/Views/ChatView.swift`, retaining component/capture state and stale-action denials.
- [x] T013 [US1] Implement bottom composer/More/selection and settings rail/sheet in `components/AstralProjection/apple-clients/AstralApp/AstralApp/Views/ChatView.swift` and `Screens.swift`; retain attachments, voice, server settings and mandatory setup.
- [x] T014 [US1] Render all six v2 types and use licensed Open Sans in `components/AstralProjection/apple-clients/AstralApp/AstralApp/Views/ComponentView.swift`, native component views/resources and app-target typography; test values/actions/fallbacks/accessible labels.
- [ ] T015 [US1] Run strict formatting, core/drift/mirror tests, affected app/UI tests, unsigned iOS/macOS builds and watch shared-core regressions from `components/AstralProjection/.github/workflows/apple-ci.yml`; resolve failures and measure changed coverage.
- [ ] T016 [US1] Exercise real iPhone/iPad/macOS against Docker, including keyboard/rotation/split-view/resize/theme/history/fullscreen/selection/denied actions; save comparisons and exact results in `specs/090-native-ui-v2/verification.md` and vault; commit/push vault checkpoint before Android edits.

- [ ] T029 [US1] Update and verify watchOS for the new system, with server-owned content/actions, watch-appropriate ROTE presentation, continuity, owner fences and available live voice verification; include its evidence before Android implementation.

## Phase 4: Android experience (US2)

Goal: match accepted Apple/web behavior on phone/tablet. Independent test: same live walkthrough plus rotation/keyboard and capability-change checks. Exclude the bottom web voice warning and verify native voice-worker connection, recording, playback and failure recovery during T022–T023. This phase cannot start before T016 records Apple implementation and available verification.

- [x] T017 [US2] Add console/ROTE/selection models, validation, capability aggregation and dispositions in `components/AstralProjection/android-client/core/`; add contract/reset/invalid-input tests.
- [x] T018 [US2] Integrate server model, actual viewport/input/voice capabilities, owner-scoped selection and draft/Run behavior in `components/AstralProjection/android-client/app/src/main/`; retain authenticated transport and continuity fences.
- [x] T019 [US2] Implement sidebar/drawer/History/agent catalog/landing and settings rail/sheet in `components/AstralProjection/android-client/app/src/main/`, matching the saved reference and shared ROTE verdict.
- [x] T020 [US2] Implement conversation/result/fullscreen/composer/More in `components/AstralProjection/android-client/app/src/main/`, preserving state, attachments/voice/export/share and stale/denied operation handling.
- [x] T021 [US2] Add six v2 component renderers and licensed font resource under `components/AstralProjection/android-client/app/src/main/`; test accessibility/data/action semantics.
- [ ] T022 [US2] Run wrapper lint/unit/coverage/build plus committed instrumentation producer from `components/AstralProjection/.github/workflows/android-ci.yml`; resolve baseline/new failures and enforce changed-code coverage.
- [ ] T023 [US2] Verify Android phone/tablet live against Docker, rotation/keyboard/resize, current themes and matching reference flows; record evidence in `specs/090-native-ui-v2/verification.md` and vault.

## Phase 5: Cross-machine record (US3)

Goal: another machine can use current visual references and resume implementation. Independent test: follow the vault index through image/state/source/verification records without local-only assumptions.

- [ ] T024 [US3] Update `../kos-wiki/wiki/synthesis-astral-native-ui-v2.md`, affected Apple/Android/ROTE/UI-v2 pages, `index.md` and `log.md` at shared-contract, Apple, Android and verification checkpoints; commit/push each meaningful batch while preserving unrelated edits.
- [ ] T025 [US3] Finish Windows handoff/reference guidance in `../kos-wiki/wiki/synthesis-astral-native-ui-v2.md`, with source anchors, deferred scope and exact visual states; leave `components/AstralProjection/windows-client/` unchanged.

## Phase 6: Qualification and integration

- [ ] T026 Run full affected CI-equivalent suites/maintained-language lint/generated assets/changed coverage and inspect security/continuity seams; record exact commands/results/artifact/toolchain limits in `specs/090-native-ui-v2/verification.md`.
- [ ] T027 Review/commit only task changes in Projection, then update `config/astral-composition.json` exact commit/protocol digest and staged `components/AstralProjection` gitlink; run `scripts/verify_composition.py` and `scripts/install_local_components.py validate --require-gitlinks` and qualify rebuilt Docker integration.
- [ ] T028 Update `components/AstralProjection/docs/UI_V2.md`, feature evidence/tasks and vault; review final diffs and create task-scoped local commits. Report hosted CI/push/live verification separately and request any still-required external publication only against concrete reviewed changes.

## Dependencies and execution strategy

T001 → T002 → T003 → T004–T008 → T009–T016 → T017–T023 → T025–T028. T024 runs at every checkpoint, including before any phase is declared complete. US3 reference capture is deliberately an early prerequisite despite its P3 story priority. Tests accompany each seam; security/continuity regressions are preserved.

Read-only audit/review may run alongside independent work under AGENTS guidance. Within Apple, component rendering work and shell work may be delegated after shared model/fixtures are settled and file ownership is disjoint; Android has the same opportunity only after Apple checkpoint. No simultaneous edits to shared AppModel/manifest. Earliest reviewable increment is Apple plus its shared contracts and live reference evidence; the full request also requires Android and final CI qualification.

## Coverage map

FR001→T002–T003; FR002→T003/T024/T025; FR003→T016–T017; FR004→T011–T014/T019–T021; FR005→T004–T008; FR006→T005/T009/T010/T017/T018; FR007→T006/T007/T010/T015/T018/T022/T026; FR008→T011–T016/T019–T023; FR009→T012–T016/T020–T023; FR010→T005/T009/T025/T026; FR011→T005–T014/T017–T021; FR012→T016/T023/T026; FR013→T002/T015/T022/T026–T028; FR014→T029. SC001/SC005→T003/T025; SC002/SC003/SC004→T016/T023.

## September 25 collaboration checkpoint

The owner requested publishing the work-in-progress090 branch for the Windows agent. `handoff-windows.md` records ownership, the complete pushed screenshot matrix and unfinished qualification. T003 is complete; implementation task boxes remain open until their full acceptance criteria pass.


## September 25 local implementation checkpoint

Shared contract, Apple shell/rendering and Android shell/rendering implementation tasks are checked against current source and their passing regression suites. Coverage diagnostics now measure91.40% Swift and93.82% Kotlin. The remaining qualification tasks stay open for immutable candidate coverage, full CI, composed-image checks and authenticated form-factor acceptance. Current evidence and screenshot identities are in `verification.md` and the pushed kos-wiki checkpointa88f14f. Watch implementation and10navigation tests are present; T029 stays open for its remaining live acceptance. The owner is away; no authentication or physical-audio success is inferred from fixtures.
