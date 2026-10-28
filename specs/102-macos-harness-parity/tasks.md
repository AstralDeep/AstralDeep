# Tasks: macOS Harness Parity

**Input**: Design documents from `/specs/102-macos-harness-parity/`

**Prerequisites**: plan.md, spec.md, research.md, contracts/

**Tests**: Required. Each lane writes its tests first and shows them failing before implementation. Changed Python lines keep 90 percent coverage. Swift changes carry XCTest coverage for golden path, denial and failure.

**Organization**: Phases follow plan.md. Lane tags name the owning subagent lane; [P] marks tasks that can run in parallel with other [P] tasks in the same phase.

## Format: `[ID] [P?] [Story] [Lane] Description with exact paths`

## Phase 1: Setup

- [ ] T001 [F] Bump Projection `apple-clients/AstralApp/AstralApp.xcodeproj/project.pbxproj` to `MARKETING_VERSION = 1.9` and `CURRENT_PROJECT_VERSION = 70` in every configuration; update `tests/release/test_client_identity_continuity.py` to the new literals.
- [ ] T002 [P] [F] Add the Sparkle 2.10.1 SPM package to `AstralApp.xcodeproj` (exact version, product `Sparkle`, Embed and Sign, macOS platform filter) and commit `Package.resolved`.
- [ ] T003 [P] [F] Generate the EdDSA key with `generate_keys`, export it with `-x` to the owner's password manager location named in `qualification.md`, and record the public key for `SUPublicEDKey`.

## Phase 2: Foundational (blocking)

- [ ] T004 [F] Tests: extend Deep `backend/tests/test_computer_use_076.py` to expect `run_command`, `read_output`, `interrupt_command` and the ten `browser_*` verbs in `shared/protocol.py` and `computer_use_policy.py` with classes, scopes, timeouts and session requirements per `contracts/local-exec.md` and `contracts/browser-verbs.md`; then implement the constants and classifications.
- [ ] T005 [P] [F] Tests: Swift `ComputerHostProtocolTests.swift` expects the new verbs, field tables and validation (including `cwd`, `timeout_s`, `snapshot_id`, `ref` shapes); update the assertion that rejected `run_command`; implement in `AstralCore/Sources/AstralCore/Computer/ComputerHostProtocol.swift`.
- [ ] T006 [P] [F] Tests then implementation: `backend/orchestrator/computer_grants.py` with `TaskGrant`, `GrantStore` (create, match, touch, revoke, expire, list), audit event names per `contracts/task-grant.md`; `backend/tests/test_computer_grants.py` covering matching by app and window, command program and folder, sliding and hard expiry, revocation reasons.
- [ ] T007 [P] [F] Tests then implementation: `AstralCore/Sources/AstralCore/Chrome/ConsoleModel.swift` makes `title`, `subtitle`, `start_here`, `example`, `empty_*`, `message_placeholder` optional and empty-tolerant; update `ConsoleModelTests.swift`; update `contracts/ui_protocol.json` console labels and `contracts/fixtures/console/chrome-console.json`, `watch-menu.json` accordingly.
- [ ] T008 [F] Tests then implementation: Deep `backend/orchestrator/projection_surfaces/theme.py` adds `PRESETS["graphite"]` first with the values in `contracts/theme-graphite.md`, `_DEFAULT_PRESET = "graphite"`, a `default_theme_preferences()` helper returning the preset name and colors; update `backend/tests/chrome/test_surface_theme.py`, `test_surface_components.py`, `backend/tests/test_projection_controllers.py`.

**Checkpoint**: Fable reviews verb shapes, grant model and theme values against the contracts before dispatching lanes.

## Phase 3: User Story 1 - Operate my Mac without approval fatigue (P1)

Server (lane S1)

- [ ] T009 [US1] [S1] Tests then implementation: the `grant_task` decision on both card paths: `hitl_confirmation.evaluate` for input verbs (`authorize_action`) and `remote_confirmation` for destructive-classed verbs; creates a `TaskGrant` bound to owner, host, session and chat from the proposal's application or window; macOS pointer verbs require `hwnd`; the server caches the last `list_windows` result and last screenshot bytes per session and attaches application name, window title and a Pillow crop (when importable) to the card.
- [ ] T010 [US1] [S1] Tests then implementation: `hitl_confirmation.py` consults `GrantStore` for covered verbs before raising a card and records `computer_grant.used`; excluded verbs stay on exact review.
- [ ] T011 [US1] [S1] Tests then implementation: `computer_sessions.py` uses the existing `pause_reason` (`local_pause` refuses remote resume; `local_input` resumes as today), revokes grants on pause, stop, takeover, authority loss and host replacement; `cancel_task` cancels the turn without ending the computer session; Stop ends the session and cancels the turn; `shared/protocol.py` `ComputerHostDescriptor` accepts optional `workspace_roots`.
- [ ] T012 [US1] [S1] Tests then implementation: `orchestrator.py` passes `workspace_roots` from the host descriptor into `list_computers` and `start_session` results, cancels the current turn when the host reports Stop, and calls `theme.default_theme_preferences()` in the `register_ui` preferences block when nothing is stored.
- [ ] T013 [US1] [S1] Rewrite computer-use tool descriptions in `backend/agents/computer_use/mcp_tools.py` for macOS (Command-L to focus the address bar, `hwnd` from `list_windows`, prefer `run_command` over Terminal keystrokes, `browser_*` for the pane), attach the result screenshot returned by input verbs as an image part, and remove the dead `wait` round trip.

Host (lane H1)

- [ ] T014 [US1] [H1] Tests then implementation: `MacComputerInput.swift` takeover requires click, key, scroll or movement over 24 points within 250 ms, and only when the frontmost application 300 ms after the event is not Astral; input in Astral's own windows never pauses (the browser pane's page content is the exception, owned by H2); the floating panel gains an "Approval waiting" state with a bring-Astral-forward button.
- [ ] T015 [US1] [H1] Tests then implementation: `MacComputerObservation.swift` and `MacComputerHost.swift` treat secure input as keyboard-only refusal (`secure_input_active`) without withdrawing the host; local Pause sets `pause_reason: local` and the host refuses remote resume.
- [ ] T016 [US1] [H1] Tests then implementation: `MacComputerHost.swift` descriptor includes `workspace_roots` names and paths; `Host.current()` cached once; session panel reports `obscured_by_astral_panel` instead of a generic refusal.
- [ ] T017 [US1] [H1] Tests then implementation: `MacComputerDriver.swift` returns a post-effect screenshot with every input verb result and produces a 512-pixel marked crop plus window title for proposals; punctuation keys added to the key table in `MacComputerInput.swift` and `ComputerHostProtocol.swift` field table.
- [ ] T018 [US1] [H1] Tests then implementation: `ComputerWorkspace.swift` trims truncated reads to a UTF-8 boundary and preserves mode and extended attributes on overwrite.

Workbench (lane M1)

- [ ] T019 [US1] [M1] Tests then implementation: `AppModel.swift` sends `cancel_task` for the current turn on Esc and Command-period and when Stop is pressed; exposes an `activity` model of tool-call rows (action, target, approval state, result, thumbnail) fed from existing status and component frames.
- [ ] T020 [US1] [M1] Tests then implementation: `MacWorkbenchView.swift` replaces the Activity sheet with a non-blocking trailing side panel bound to the activity model, gates Stop on an active session, adds the key equivalents, and reserves a slot for the update status chip; `ConsoleConversation.swift` renders tool steps inline.

**Checkpoint**: Fable runs a TextEdit task live against the sandbox with one approval card.

## Phase 4: User Story 2 - Run commands like Claude Code (P1)

- [ ] T021 [US2] [S1] Tests then implementation: `computer_use_policy.py` destructive list and shell-aware matcher; `summary_for` renders the command card (Mac, command, cwd, timeout, env, background); grant decision label `Allow <argv0> in <folder> for this task`.
- [ ] T022 [US2] [S1] Tests then implementation: `backend/agents/computer_use/mcp_tools.py` adds `run_command`, `read_output`, `interrupt_command` tools with `scope: tools:execute`, result shaping, untrusted tagging through datamarking, PHI gate, per-session job id tracking and cleanup on Stop; `taint.py` adds the command sinks.
- [ ] T023 [US2] [S1] Tests then implementation: `terminal_host_review.py` generalized to clear the exact computer-use implementation class and these tool names; `backend/tests/test_tool_security*.py` proves a renamed or external tool is still blocked.
- [ ] T024 [US2] [H1] Tests then implementation: `AstralCore/.../Computer/ComputerCommandPolicy.swift` with the environment allowlist, forbidden env names, cwd-inside-root resolution with symlink handling, timeout bounds; `ComputerCommandPolicyTests.swift`.
- [ ] T025 [US2] [H1] Tests then implementation: `AstralApp/Computer/MacComputerCommands.swift` process host on a separate executor from the physical-effect queue: `posix_spawn` in a process group, sanitized environment, stdin write, stdout and stderr rings, foreground wait with timeout and group kill, background jobs with `read_output` offsets and `wait_s`, `interrupt_command` signals, cleanup on session end, Stop, pause and quit, `uncertain` state after socket loss; wire into `MacComputerHost.drain()` and descriptor advertisement when roots exist; `MacComputerCommandsTests.swift` with a fake spawner and one real `/bin/echo` run.

**Checkpoint**: Fable runs `wc -l` and a timed-out `sleep` live.

## Phase 5: User Story 3 - Browser pane (P1)

- [ ] T026 [US3] [S2] Tests then implementation: `computer_sessions.py` `approved_origins`; `computer_use_policy.py` origin and consequential card summaries; `backend/agents/computer_use/browser_tools.py` registering the ten tools through the hook S1 added in `mcp_tools.py`, with untrusted wrapping, `_sanitize` limits raised for snapshots, no model digest, "page content" image caption; `taint.py` browser sinks.
- [ ] T027 [US3] [H2] Tests then implementation: `AstralCore/.../Computer/ComputerBrowserPolicy.swift`: scheme and origin rules, secret-field detection, redaction, consequential classifier; `ComputerBrowserPolicyTests.swift`.
- [ ] T028 [US3] [H2] Implementation: `AstralApp/Resources/browser_snapshot.js` snapshot with stable refs, interactive and full modes, bounded output, fill, click, press, scroll, wait helpers, frame-aware refs; test against fixture HTML in a headless WKWebView (`MacBrowserSnapshotTests.swift`).
- [ ] T029 [US3] [H2] Tests then implementation: `AstralApp/Computer/MacBrowserPane.swift` (WKWebView, non-persistent store, content rule list, navigation policy, dialog queue, download and popup refusal, generation tokens, `takeSnapshot`) and `MacBrowserDriver.swift` (actor executing the ten verbs through the host fence and authorize closure, `stale_ref`, `confirmation_required`, `secret_field`, NSEvent fallback marked as non-takeover); descriptor advertises `browser_*` when the pane toggle is on.
- [ ] T030 [US3] [H2] Implementation: `AstralApp/Views/MacBrowserPaneView.swift` with URL bar, back and forward, "Astral is driving" chip, Take over; pane toggle in `MacComputerControls.swift`; takeover on user input in the pane.

**Checkpoint**: Fable runs the 099 browser fixture in the pane live with zero pixel clicks.

## Phase 6: User Story 4 - Autoupdate (P1)

- [ ] T031 [US4] [U] Tests then implementation: `AstralApp/Updates/UpdateCoordinator.swift` (SPUStandardUpdaterController ownership, delegates per `contracts/update-feed.md`, published state); `UpdateCoordinatorTests.swift` with a fake updater for postpone, channels, error surfacing.
- [ ] T032 [US4] [U] Tests then implementation: `AstralApp/Updates/UpdatePolicyGate.swift` (fetch, cache 24 hours, CryptoKit Ed25519 verify against `SUPublicEDKey`, serial and expiry rules, fail open); `UpdatePolicyGateTests.swift` with signed fixtures and tampered fixtures.
- [ ] T033 [US4] [U] Implementation: macOS `Info.plist` SU keys; `AstralAppMain.swift` creates the coordinator for macOS, gates `bootstrap()` on the policy result, adds `CommandGroup(after: .appInfo)` with Check for Updates and Updates…; `AstralApp/Updates/UpdateSettingsSheet.swift` (native sheet with the toggles, current build, last check) and `UpdateStatusChip.swift` for the workbench slot.
- [ ] T034 [US4] [U] Tests then implementation: Deep `scripts/release_macos.py` per `contracts/update-feed.md` (archive, export, inspect, zip, notarize with `astral-notary`, staple, `sign_update`, `generate_appcast` with staged newest archive, `SHA256SUMS`, provenance, downloads record, policy signing, `--publish`); `backend/tests/test_release_macos.py` with subprocess fakes covering ordering, failure stops and publish gating.
- [ ] T035 [US4] [U] Tests then implementation in the website: `scripts/appcast.mjs` parse and cross-check against `data/downloads.json`; call from `scripts/build.mjs`; `tests/appcast.test.mjs`; `site/data/mac-update-policy.json` and `.sig` placeholders generated by the release command.

**Checkpoint**: Fable installs 1.9 (70), builds 1.9 (71) locally, serves a loopback feed and verifies install, refusal on a tampered signature, and postponed relaunch during a task.

## Phase 7: User Story 5 - Graphite everywhere (P2)

- [ ] T036 [P] [US5] [T] Tests then implementation: Graphite in Deep `backend/agents/general/mcp_tools.py`; Projection `src/astralprojection/chrome/personalization.py` (first), `backend/webrender/static/client.js`, `astral.css` `:root`, `windows-client/astral_client/theme.py` and `PALETTE`, `android-client/.../Theme.kt` and `DEFAULT_PALETTE`; `ThemeView` default name in Projection `models.py` and Deep `projection_controllers.py`; update `tests/test_theme_preset_parity.py`, `tests/chrome/test_personalization_views.py`, `tests/test_no_hex_literals_089.py`, `windows-client/tests/test_theme_live.py`, `ThemeTest.kt`.
- [ ] T037 [US5] [S1] Test: `register_ui` without a stored theme sends Graphite preset and colors; with stored Midnight sends Midnight unchanged (`backend/tests/test_native_theme_ingress.py` or new).
- [ ] T038 [US5] [M2] Tests then implementation: `NativeAppearance/Theme.swift` Graphite as `AstralPalette()` default and explicit `midnight` case, every case sets `surface2`, default fallback `.graphite`, desaturated status roles; Mac sweep per `contracts/theme-graphite.md` (`ComponentView.swift`, `MarkdownBlockView.swift`, `RootView.swift`, `MacComputerControls.swift`, `BannerBar` icon, `OfflineChartView.swift` series injection); update `SettingsControlTests.swift`, `WebPrimitiveStyleTests.swift`, `OfflineChartTests.swift`, `CanvasCapture088Tests.swift`, `WorkspaceExportPipeline088Tests.swift`, `ConsoleWatchTests.swift`.

## Phase 8: User Story 6 - Quiet copy and clear layouts (P2)

- [ ] T039 [P] [US6] [C] Tests then implementation: Deep cuts in `welcome.py` and `projection_surfaces/*.py` per `contracts/copy-cuts.md`; update `test_welcome*.py`, `test_wiring_030.py`, `test_first_turn_contract.py`, `test_curated_examples_060.py`, `tests/chrome/test_surface_*.py`, `test_native_console_surfaces.py`, `test_pulse.py`, `test_search_surface.py`, `test_typesafe_settings.py`.
- [ ] T040 [P] [US6] [C] Tests then implementation: Projection cuts in `backend/webrender/chrome/console_model.py`, `web_landing.py`, `component_model.py`, `src/astralprojection/chrome/*.py`, `terminal.py`; update `tests/chrome/test_console_model.py`, `test_personalization_views.py`, `test_agent_views.py`, Windows `test_console_shell.py`, `test_detail_surface_geometry.py`, `test_message_routing.py`, Android `ConsoleShellTest.kt`.
- [ ] T041 [US6] [M2] Tests then implementation: Swift cuts and layout fixes A, B, C per `contracts/copy-cuts.md` in `ChatView.swift`, `ComponentView.swift`, `ComponentChrome.swift`, `ConsoleShell.swift`, `RootView.swift`, `Screens.swift`, `MacComputerControls.swift`, `AppModel.swift` banners (coordinated with M1), `Voice.swift`; extend the forbidden-copy list in `MacWorkbenchViewTests.swift`; update `ConsoleShellPresentationTests.swift`, `SettingsControlTests.swift`, `AppModelBackgroundContinuityTests.swift`, `MacComputerHostTests.swift`, `ComponentChromeTests.swift`, UI tests and `FirstLoginUITestFixture.swift`.
- [ ] T042 [US6] [M1] Copy cuts inside `MacWorkbenchView.swift` (sidebar wordmark, empty-state strings, canvas header title) after T020.

## Phase 9: Integration (Fable)

- [ ] T043 Build `AstralCore` (`swift test`) and the Mac app (`xcodebuild test -scheme AstralApp -destination 'platform=macOS'`); fix integration failures or return them to the owning lane.
- [ ] T044 Run Deep pytest for touched suites in the container and Projection pytest for touched modules; confirm 90 percent changed-line coverage with `coverage` reports.
- [ ] T045 Review critical seams personally: grant revocation on every authority change, same-host approval window bounds, command group kill and environment sanitization, browser secret refusal and consequential handshake, update signature failure path and fail-open policy, Graphite default-send precedence.
- [ ] T046 Move Deep's `components/AstralProjection` gitlink and `config/astral-composition.json` to the exact Projection commit; commit on both branches.

## Phase 10: User Story 7 - Rollout (P1, Fable with owner)

- [ ] T047 [US7] Build the server image from the exact candidate tree; write the deploy, backup, cutover and rollback scripts under the owner's home on the sandbox; owner opens SSH and runs them with sudo; verify health, LETS and PostgreSQL; record image id and container id.
- [ ] T048 [US7] Build, sign, notarize and staple 1.9 (70) with `scripts/release_macos.py` (no `--publish`); install over build 69 keeping 69 recoverable; verify `codesign`, `spctl`, designated requirement and `SUPublicEDKey`.
- [ ] T049 [US7] Live verification of each story's independent test against the sandbox; record in `specs/102-macos-harness-parity/qualification.md`.
- [ ] T050 [US7] On the owner's word, run the release command with `--publish` for 1.9 (70), merge the website change, confirm `appcast.xml`, `downloads.json` and the policy file are live, and confirm the installed app sees no update (it is current).
- [ ] T051 Update the knowledge vault (`../kos-wiki`) per its `AGENTS.md`, then commit and push the vault.

## Dependencies

- Phase 2 blocks every lane.
- T009 to T013 and T021 to T023 are sequential within S1; T026 waits for the registration hook from T022.
- T014 to T018 and T024 to T025 are sequential within H1; T027 to T030 are sequential within H2 and wait for T005.
- T019 waits for T016 (roots) only for the descriptor shape; T020 waits for T019; T042 waits for T020.
- T038 waits for T008 values; T041 waits for T020 (shared `AppModel.swift` banners are coordinated by Fable).
- T031 to T035 are independent of the other lanes except the chip slot in T020.
- Phase 9 waits for all lanes; Phase 10 waits for Phase 9.
