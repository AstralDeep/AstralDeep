# Tasks: macOS Agent Workbench

**Input**: spec.md, plan.md, research.md, data-model.md, contracts/mac-host.md. Tests precede behavior; preserve all security and release gates. Check a task only against actual evidence.

## Phase1 — Setup and artifact gates

- [x] T001 Inventory local/remote feature directories, reserve099 and isolate Deep/Projection worktrees; record baseline in specs/099-macos-agent-workbench/research.md.
- [x] T002 Pin all six harness sources and compare actual UI/source evidence in specs/099-macos-agent-workbench/research.md.
- [x] T003 Independently review spec/plan/tasks coverage and constitution alignment; record results in specs/099-macos-agent-workbench/analysis.md.
- [x] T004 Commit scoped design artifacts and refresh curated kos-wiki pages/index/log under the standing checkpoint rule.

## Phase2 — Host foundation

- [x] T005 [P] Add failing closed-envelope, args-bound, replay, epoch/deadline/queue tests in Projection apple-clients/AstralCore/Tests/AstralCoreTests/ComputerHostProtocolTests.swift and ComputerPhysicalBoundaryTests.swift.
- [x] T006 Implement existing-v1 models/fence with4096request/128retired-session/32queue bounds in Projection apple-clients/AstralCore/Sources/AstralCore/Computer/; run focused tests.
- [x] T007 Wire optional descriptor and current-channel nonqueued event sends in Projection Protocol/Frames.swift, Protocol/Dispositions.swift and Transport/WSClient.swift; test author-only omission and no reconnect replay.

## Phase3 — US1 Native host

- [x] T008 [US1] Add injected-driver lifecycle/permission/stop tests then implement MacComputerHost and AppModel.swift ownership/connection hooks in Projection apple-clients/AstralApp/AstralApp/Computer/.
- [x] T009 [US1] Implement bounded capture/window/app/marked-input driver and test geometry/keys/focus/terminal denials and explicit reviewed hwnd focusing in Projection Computer/MacComputerDriver.swift and MacComputerInput.swift.
- [x] T010 [US1] Add consent/TCC/root and persistent Pause/Resume/Stop controls in Projection Computer/MacComputerControls.swift, plus DirectDownload build variant with actual sandbox check; test Store/controller-only behavior.

## Phase4 — US2 Browser/native workflow

- [x] T011 [P] [US2] Test then normalize Mac application/bundle aliases and reject arbitrary macOS application argv while retaining Windows launch behavior in Deep backend/orchestrator/computer_use_policy.py and backend/agents/computer_use/mcp_tools.py.
- [ ] T012 [US2] Run real native/browser positive and stale/focus/permission-denial workflows; retain synthetic postcondition evidence outside Git and summarize in specs/099-macos-agent-workbench/qualification.md.

## Phase5 — US3 Commands and selected files

- [x] T013 [US3] Add actual temporary-filesystem tests then implement selected-root openat/O_NOFOLLOW bounded read/write/list/delete with atomic refusal/overwrite in Projection Computer/MacComputerFiles.swift.
- [ ] T014 [US3] Verify reviewed visible-Terminal command and selected-root round-trip plus no-approval/outside-root/symlink zero-effect denials; record in specs/099-macos-agent-workbench/qualification.md.

## Phase6 — US4 Task authority and continuation

- [x] T015 [P] [US4] Add failing exact-approval/current-task/decline/expiry/cancel/uncertainty tests, implement narrow continuation through ordinary dispatch in Deep backend/orchestrator/hitl_confirmation.py and required orchestrator seam; separately test exact terminal text/chord review and the separately labeled same-host approve-and-resume decision with manual-pause/Stop/stale-target denials.
- [x] T016 [US4] Test and harden host replacement/late result/cancel uncertainty boundaries in Deep backend/orchestrator/computer_hosts.py as needed; clear Terminal grants on pause/authority changes in computer_sessions.py and restrict remote_confirmation.py grants to exact current-session shell launch/explicit confirmation. No unrelated approval may mint Terminal access; do not invent a v2 receipt guarantee.
- [ ] T017 [US4] Live-verify approval continuation, local takeover, Pause/Stop/disconnect and no effect replay in specs/099-macos-agent-workbench/qualification.md.

## Phase7 — US6 Native workspace

- [x] T018 [P] [US6] Add focused layout/state/interaction tests, implement a Mac workbench using existing negotiated console presentation and server menus in Projection Views/RootView.swift, ConsoleShell.swift and MacWorkbenchView.swift.
- [x] T019 [US6] Make generated split canvas directly interactive, reduce transcript card density, preserve scroll intent and expand composer/server-derived actions in Projection Views/ConsoleConversation.swift and ChatView.swift; preserve current canvas/owner/export fences.
- [ ] T020 [US6] Verify compact/wide keyboard/accessibility/approval/Stop and generated-content interactions live; record in specs/099-macos-agent-workbench/qualification.md.

## Phase8 — US5 Integration and production

- [ ] T021 Update Projection provenance/transformations.json, run Swift formatting/full core/app/protocol/architecture gates and independent critical-seam review; retain exact revision evidence.
- [ ] T022 [US5] Adopt only the exact qualified Projection revision and digest in Deep config/astral-composition.json and component gitlink; run composition/backend/module/client gates with>=90%changed Python coverage.
- [ ] T023 [US5] Qualify exact candidate staging against real IAM/PG/LETS and installed Mac client; record all positive/denial checks and rollback in specs/099-macos-agent-workbench/qualification.md.
- [ ] T024 [US5] Deploy only the qualified immutable server/client pair, preserve existing data, verify production health and real native/browser/terminal/file/Stop paths; update specs/099-macos-agent-workbench/qualification.md and curated vault checkpoint.
- [ ] T025 [US5] Qualify the fresh runtime dependency closure and adapt demonstrated stale baseline dispatch/ledger/identity fixtures without weakening declarations, write classifications, approvals, PHI or CI gates; rebuild the exact candidate and complete all three whole backend groups. Retain failed baseline/candidate evidence and the precise public-digest scanner exclusions.
- [ ] T026 [US1] Repair installed-app permission-request feedback and activation refresh, inspect raw OS permission facts, complete normal macOS authorization and real sign-in/physical consent, and verify takeover/Stop through the signed candidate. No TCC or authentication bypass.
- [x] T027 [US1] Stamp the existing canonical submission/request identities and captured registration generation on every native host control envelope; exercise real server-ingress code with actual producer frames and authenticated-owner fixtures, including announce, replies during approval, session events and malformed/stale/flag-off denials. Keep payloads closed, delivery nonqueued and backend admission strict. Real-IAM/native registration acceptance remains in T023.
- [ ] T028 [US5] Replace the demonstrated prohibited registry soak with bounded concurrent coherence/immutability proof, repair operator-private staging env-file placement, retain failure/coverage evidence and finish fresh whole groups. Qualify supported representative-data staging while keeping human native acceptance explicitly open; retain the unavailable historical input and use T029 for a separately reviewed stronger current-production procedure.

- [ ] T029 [US5] Bind staging recovery to a trusted-policy-only exact current-production image profile, preserve the fixed synthetic predecessor migration, verify restored encrypted key/chat/audit/blob bytes and owner denials, prove candidate-only audit appends absent, version and validate complete evidence, independently review and run the actual paired rehearsal. Candidate-policy diagnostics do not authorize promotion.

- [ ] T030 [US1] Exercise actual native adapter decisions through internal raw-OS IO boundaries, preserving live-only defaults, public APIs, per-effect authority checks and physical-consent rules. Add deterministic AX/display/event/file/control positives and denials, retain real syscall/CGEvent semantics, and qualify at least 90% changed-code coverage without exclusions or fabricated line maps. Diagnostic unhosted runs do not replace app/native/physical acceptance.
- [ ] T031 [US5] Bind macOS coverage extents to pinned compiler/native-binary/source geometry through an explicit native domain and owning denial tests. Reject stale/truncated/mismatched evidence; do not pad trailing source rows or grant a generic tail exemption.
- [ ] T032 [US5] Collect actual missing nonnative producer reports through existing isolated gates, preserve exact provider/source/report identities and validate the complete strict matrix before promotion.
- [ ] T033 [US5] Prove whole compiler-inactive Swift inputs from immutable complete source, a closed compiler-parsed OS guard and the exact native-derived target/toolchain. Keep explicit versioned inactive records separate from executable coverage, require genuine active counts for every applicable producer, and qualify the trusted helper and native inputs before accepting any missing-file disposition.
- [ ] T034 [US5] Align the iOS release UI inventory with the existing SettingsControls suite and collect same-build Mac unit/UI/staging observations with protected independent reconstruction. Preserve exact suite identities, failure propagation, draft/default-ref guards, secret scoping and the 30-minute job budget; verify the exact provider run.
- [ ] T035 [US5] Retain the investigated raw-profile/native UUID limitation and assess stronger future proof when the platform supports it. This is additional evidence research: current v1 requires genuine observations, exact source/compiler geometry, same-build products and protected provider/artifact reconstruction, and makes no UUID-association claim. Do not add an unsupported UUID field or treat an empty binary-ID check as proof; existing qualification gates remain required.

- [x] T036 [US1] Persist the owner-requested local opt-in across relaunch, separating saved choice from live access. Reproduce restart/reset failures; test owner/server isolation, explicit off, missing TCC/desktop/monitor/distribution, synchronous lock revocation and old-request denial; preserve physical initial enable/local Resume and live-verify the signed installed app across quit/reopen.

## Dependencies and Parallel Work

T001–T004 precede implementation; T005→T006→T007 precede host integration. Host/native/files work is one Projection owner. Deep T011/T015/T016 can proceed independently with one backend owner. UI T018/T019 can proceed in distinct view files; coordinate AppModel/RootView control hooks with the host owner. T012/T014/T017/T020 need a built functioning pair. T021 precedes T022; all live gates precede T024. T025 closes demonstrated runtime/fixture prerequisites for T021–T023; T026 precedes every physical host acceptance task.

## Coverage and Strategy

FR001–004/008/010: T005–T012/T016/T017. FR005/009: T011/T013/T014. FR006/007: T015–T017. FR011/012/018/019: T007/T018–T022. FR013: T010/T021. FR014: T006/T008/T012/T016/T017. FR015: T002/T003. FR016: all test-first tasks/T021–T023. FR017: T022–T024. SC001–008 map to the corresponding physical acceptance tasks; no live success is inferred from unit tests.

Complete the native host first while the backend and native layout proceed in separate scopes. Do not ship an incomplete host, bypass a gate, or call an unverified deployment complete.
