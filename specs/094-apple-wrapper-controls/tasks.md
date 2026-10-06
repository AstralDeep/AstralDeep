# Tasks: Apple Controls and Web/Mobile Consistency

**Date**: 2026-10-05. Inputs: spec.md, plan.md, research.md, data-model.md and contracts/control-semantics.md. Tests are required by FR-016/017 and repository policy. Paths beginning `../AstralProjection/` belong to the component repository; do not edit their Deep submodule copies.

## Phase 1: Setup

- [x] T001 Resolve ownership, remote collision preflight, explicit feature pointer and task branches; record in specs/094-apple-wrapper-controls/clarification.md.
- [x] T002 Read both constitutions and research the live/source discrepancies; record decisions in specs/094-apple-wrapper-controls/research.md.

## Phase 2: Foundation

- [x] T003 Define form/action/theme/guidance authority and fixture boundaries in specs/094-apple-wrapper-controls/contracts/control-semantics.md and data-model.md.
- [x] T004 Verify production-compatible toolchains and tracked lint/test gates; document candidate/adoption separation in specs/094-apple-wrapper-controls/plan.md and quickstart.md.

## Phase 3: US1 — Correct Account Forms (P1)

**Independent test**: Four uniquely named provider operations, non-first saved defaults, labeled options, unchanged-key preservation and failure/denial without a query.

- [x] T005 [US1] Add failing native-profile metadata/interactivity/action-budget regressions in ../AstralProjection/tests/rote/test_adapter_contracts.py, then repair ../AstralProjection/backend/rote/adapter.py.
- [x] T006 [P] [US1] Test and normalize Apple defaults/options/checklists/conditions/submissions in ../AstralProjection/apple-clients/AstralCore and ../AstralProjection/apple-clients/AstralApp/AstralApp/Views/ComponentView.swift; make malformed actions unavailable and pending operations distinct.
- [x] T007 [P] [US1] Test and repair Android options/form operation ownership in ../AstralProjection/android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/render/renderers/Input.kt and its renderer tests.
- [x] T008 [P] [US1] Preserve labeled option wire values through Windows/web form renderers and regression tests in ../AstralProjection/windows-client/tests/test_param_visible_when.py and ../AstralProjection/tests.
- [x] T009 [P] [US1] Emit complete provider labels with saved raw values in backend/orchestrator/projection_surfaces/llm.py; implement the owner's persist-before-probe Save decision in backend/llm_config/ws_handlers.py and all callers; update surface/config tests and retain blank-key/endpoint/denial protections. Persisted success and connection warning must be distinct on every client.
- [x] T010 [US1] Qualify Load models, Test connection, Save and Save TypeSafe key with safe fixtures; record exact outcomes in specs/094-apple-wrapper-controls/control-inventory.md.

## Phase 4: US2 — Dashboard and Wrapper Parity (P1)

**Independent test**: Matched hierarchy/content/theme; prompt load without submission; navigation preserves active work; Drafts and Advanced function or accurately explain authorized failures.

- [x] T011 [P] [US2] Align Apple dashboard cards/filters/action layout with shared web roles in ../AstralProjection/apple-clients/AstralApp/AstralApp/Views/ConsoleIntroSurfaceView.swift and ConsoleShell.swift; test narrow/large-text layout in AstralAppTests. Watch stack Home/empty Chat also collapse redundant introductory headings, with a regression at 162/198/216-point widths.
- [x] T012 [P] [US2] Verify/repair Android dashboard/navigation using ../AstralProjection/android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/ui/Screens.kt and app UI tests.
- [x] T013 [P] [US2] Add failing native Drafts tests and implement owner-scoped primitive composition/result refresh in backend/orchestrator/projection_surfaces/drafts.py, backend/orchestrator/chrome_events.py and backend/tests/chrome/test_surface_drafts.py.
- [x] T014 [P] [US2] Exercise native Advanced ingress/delivery/fencing through backend/tests/test_guidance_ingress_088.py and test_turn_selection_surface_089.py; repair any evidenced seam, preserve authority and correct client failure/retry presentation.
- [x] T015 [US2] Record role/form-factor parity and approved watch/admin/tour dispositions in specs/094-apple-wrapper-controls/control-inventory.md, including prompt loading/clearing, draft preservation and compact Watch navigation with native clock contrast.

## Phase 5: US3 — Accepted Appearance (P1)

**Independent test**: All five presets, live replacement updates, visible swatches, arbitrary color/cancel/rejection, persisted state and system appearance.

- [x] T016 [P] [US3] Test/repair accepted theme reduction, replacement repaint, swatch styling and system appearance in ../AstralProjection/apple-clients/AstralApp/AstralApp/AppModel.swift, Views/ComponentView.swift, AstralAppMain.swift and NativeAppearance/Theme.swift.
- [x] T017 [US3] Add validated arbitrary Apple color input with cancel and server-result authority in ../AstralProjection/apple-clients/AstralApp/AstralApp/Views/ComponentView.swift and AstralAppTests; deliver the exact accepted palette after custom saves through backend/orchestrator/projection_surfaces/theme.py and authenticated ingress tests, preserving all saved role overrides.
- [x] T018 [P] [US3] Add validated arbitrary Android color input and failure/cancel tests in ../AstralProjection/android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/render/renderers/Input.kt and app renderer tests.
- [ ] T019 [US3] Run preset/color/reopen comparisons across applicable clients and watch shared appearance; record evidence in specs/094-apple-wrapper-controls/verification.md. All five presets and accepted custom-color/reopen checks pass on current iPhone/iPad/Mac products; Android's complete synthetic device lane passes. The owner completes normal Watch SSO; its five accepted palettes, compact Home/empty Chat and native-clock Daylight contrast are live-verified, with Midnight restored. Android candidate trusted-HTTPS/IAM acceptance remains unverified.

## Phase 6: US4 — Complete Journeys and Recovery (P2)

**Independent test**: Ordinary destinations, validation/cancel/denial, initial failure/recovery, surface result ownership and retained edits without a chat query.

- [x] T020 [P] [US4] Add first-connection/recovery and surface-state regressions, repair Apple Views/Screens.swift, AstralWatch/WatchModel.swift and Android ui/Screens.kt in ../AstralProjection with their app tests. Registered-token expiry, retained-form server ownership and Advanced retry destination/authorization repairs pass deterministic gates and signed iPhone/iPad/Mac reconnect checks. Watch recovery additionally binds broker refresh, physical registration, default-read permissions, recent-chat replies and REST token delivery to the current owner/server/session. Remaining whole-client qualification belongs to T024/T025.
- [x] T021 [US4] Inventory every ordinary settings/wrapper control from server definitions and client affordances in specs/094-apple-wrapper-controls/control-inventory.md; test safe successes, invalid input, denials, duplicate taps and cancellation using disposable fixtures.
- [x] T022 [US4] Verify no unauthorized credential mutation or effects, upload/deletion or query from canceled/navigation actions; record test and live evidence in specs/094-apple-wrapper-controls/verification.md.

## Phase 7: Integration and Qualification

- [x] T023 Review shared authorization/adaptation seams; update exact changed-file digests/reasons in ../AstralProjection/provenance/transformations.json and run its guard. Final AppModel, WatchModel, chart and compact Watch digests match Projection 7fa5ffb; ledger 8d7053cd and immutable replay/protocol/workflow guard pass 135 with four existing PowerShell skips. Post-extraction tests are bound independently; root verifies current source, coverage inputs and products.
- [ ] T024 Run affected Python/JS/Swift/Kotlin lint, meaningful suites and changed-line coverage at the candidate revision; record commands/results/baselines in specs/094-apple-wrapper-controls/verification.md. Current affected gates and independently source-bound coverage are recorded; exact pinned Xcode, Linux backend qualification and the retained browser/platform/timing failures remain open. App coverage is measured under the existing policy; no threshold is waived.
- [ ] T025 Build/test iOS/iPadOS/macOS/watch/Android and run Windows regression; personally live-verify affected form factors with real backend/identity in specs/094-apple-wrapper-controls/verification.md. iPhone/iPad/Mac owner checks, the full Mac suite, current Android fixtures and full Windows offscreen regression pass. Watch owner SSO and supported navigation/appearance checks also pass on its normally signed current product. Android candidate trusted-HTTPS/IAM and canonical native/platform qualification remain open.
- [x] T026 Adopt only the exact CI-qualified Projection revision in Deep; otherwise document the outstanding qualification and unchanged pin in specs/094-apple-wrapper-controls/verification.md. Never fake adoption/deployment.
- [x] T027 Reconcile task/spec/inventory state, preserve local recoverable commits without product push, and update curated ../kos-wiki/wiki/astral-apple-clients.md, synthesis-astral-native-ui-v2.md, index.md and log.md with a separate vault commit/push. The final four-file curated completion checkpoint is committed and pushed to vault main as 126e4cd9295d30ca94fb87b817966a8fa49372fd, independently verified with git ls-remote; prior 6e478109b719aeda372afca04a99cf2c21725ad7 and concurrent history are preserved. Product branches remain local. T019/T024/T025 retain incomplete qualification explicitly.

## Authorized release follow-through

The owner's later authorization includes qualification, paired merges, Sandbox deployment and upload. Every uploaded Apple product is fixed to 1.8/build 67; no separate pre-upload inventory query or App Review submission is included. [release-readiness.md](release-readiness.md) records the source boundaries and protected infrastructure blocker.

- [x] T028 Prepare all fourteen Apple project configurations at 1.8/67; update identity/provenance, build both Release archives and independently verify iOS/Watch/Mac plists and complete manifests. Remove the Deep workflow's run-number overrides and pass its meaningful release/build-surface regression.
- [x] T029 Integrate current main without losing concurrent work; push the ordinary component candidate and open/attach draft Projection PR54. Deep remains local with unchanged pins because its canonical/protected release prerequisites are absent.
- [ ] T030 Close exact-head Projection Python/web/Windows/Android/Apple hosted CI and retain native artifacts, coverage, failures and declared skips.
- [ ] T031 Close remaining real affected-client evidence and functional isolated staging, issuer, runners/environments and independently protected decision prerequisites. Current canonical preparation fails closed; local image/boot success with the old Projection pin is diagnostic only.
- [ ] T032 Merge qualified Projection, adopt its exact SHA/manifest in Deep, complete composed canonical qualification, then merge Deep through the required protected gate.
- [ ] T033 Deploy the exact approved immutable composed image to Sandbox and verify normal IAM/service/client behavior through the protected deployment path.
- [ ] T034 Bind the protected Apple publisher to approved immutable decision/inputs and pinned toolchain; Store-sign, export, validate and upload 1.8/67 for iOS/embedded Watch and Mac, retaining provider delivery receipts.
- [ ] T035 Publish the next curated vault checkpoint for the exact release-preparation and qualification state before declaring that checkpoint complete.

## Dependencies and Parallel Work

Setup/foundation and read-only analysis gate precede implementation. T005 unblocks full native action qualification; renderer tests may be developed concurrently. Apple owns T006/011/016/017 and its T014/020 client presentation; mobile owns Android/Windows T007/008/012/018/020; server owns T009/013/014; root owns ROTE/web integration, inventory, gates and evidence. No concurrent edits to one file; coordinate shared semantics before integration. Tests expose defects before repairs. T010/015/019/021/022 depend on their relevant fixes; integration gates depend on the full candidate. Owner requests the full scope, so delivery does not stop at the first story.

## Requirement Coverage

FR-001: T015/021; FR-002: T011/012/015; FR-003: T006/007/011/012/025; FR-004: T005/009/013/014/015; FR-005: T011/012/015; FR-006: T006/007/014/015/020; FR-007: T005/006/007/008/010; FR-008: T006/007/008/009; FR-009: T006/007/009/010; FR-010: T013/014; FR-011: T016/019; FR-012: T017/018/019; FR-013: T021; FR-014: T014/020/021; FR-015: T005/013/014/022; FR-016: T024/025/026; FR-017: T010/019/021/022/024/025.
