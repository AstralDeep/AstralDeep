# Implementation Plan: macOS Agent Workbench

**Branch**: `codex/099-macos-agent-workbench` | **Date**: 2026-10-24 | **Spec**: [spec.md](spec.md)

## Summary

Deliver a native Mac computer executor and a clearer workbench using Astral's existing authenticated computer transport, exact approvals and generated UI. Browser work uses visible screenshot/input interaction; commands use the existing reviewed visible-Terminal path. Neither is represented as a direct PTY or complete DOM service. Compare six harnesses using pinned evidence and qualify actual outcomes.

## Technical Context

**Language/Version**: Python 3.11; AstralCore retains its Swift tools 5.9 manifest and Swift 5 language mode. The app retains its existing Swift 5 language mode, MainActor default isolation and pinned Xcode 26.6/build 17F113 compiler floor; this does not promise compilation with the older Swift 5.9 compiler. Local Xcode 27.1 versus CI 26.6 is recorded, not equated.
**Primary Dependencies**: Existing FastAPI/ASGI, Keycloak, astralprims/Projection/Plane; SwiftUI/AppKit, ApplicationServices/CoreGraphics, ScreenCaptureKit and Foundation. No new third-party baseline runtime.
**Storage**: Existing Plane data only. Local consent/root bookmarks are owner/server-bound; requests, screenshots, window handles and authority are ephemeral.
**Testing**: pytest computer/source-bound/HITL suites and measured changed-line coverage; SwiftPM/XCTest/direct-download builds; Projection protocol/provenance/architecture gates; actual authenticated native/browser/file/terminal acceptance.
**Target Platform**: Direct-download macOS host; other clients retain controllers. Store/release stays author-only. No new primitive or push type.
**Performance Goals**: Bounded serial effect queue, bounded observations and existing operation deadlines; no hosted-runner wall-clock assertions.
**Constraints**: Exact review, current-owner authority, no auth/TCC/command bypass, no offline host-event replay, no arbitrary app arguments, no symlink escapes or blind retries.
**Scale/Scope**: Existing one-session policy, up to8 displays, bounded local ledgers/files; see contract.

## Constitution Check

- Native rendering and host source change in the independent Projection worktree. Deep owns policy/dispatch and subsequently adopts an exact qualified component revision. Shared capabilities/menu/theme stay server-owned; Mac arrangement changes do not create a new catalog.
- Preserve Keycloak/RFC8693, owner isolation, exact source-bound approvals, PHI/egress, emergency Stop and pre-effect audit. Native driver receives no IAM/provider credentials or unrestricted agent IPC.
- No schema/Plane change planned. A newly required durable store requires separate governed Plane work, never Deep SQL.
- Changed Python requires90% coverage; root Ruff targets py311. Swift uses tracked formatting/CI. Changed extracted files update Projection's provenance digest ledger. Tests prove behavior, not comment wording.
- Qualifying staging uses isolated candidate containers, real PostgreSQL/LETS/IAM and the actual Mac client; preserve unrelated running projects. Bind evidence to exact image/client/component identities. Existing-data migration evidence is required if any migration is discovered.
- No new frame vocabulary. Mac promotes its host dispositions; iOS remains controller-only with its existing ignored host-presence/session dispositions. Verify every affected shared client surface live; missing evidence remains a gate, no waiver assumed.
- Before a product push, run deterministic `scripts/prepare_release_evidence.py` with exact base/candidate and actual coverage reports. Local output stays diagnostic; protected CI reconstructs trusted inputs. No bootstrap or protected-debt exception is assumed; any structural provider blocker must follow ConstitutionX's exact default-branch verifier/allowlist and seven-day draft-only process.
- Preserve registered identity and hardened runtime. Local installation/qualification is distinct from signed/notarized/public release. Store submission/release-lane activation is not implicit. Publication remains in separately pinned protected Actions with native short-lived identity, never a token broker.

Post-design: no constitution exception claimed; staging, exact component qualification and live verification remain required.

## Project Structure

Artifacts: spec/research/data-model/contracts/quickstart/tasks/analysis/qualification in this feature directory.

Projection owns `apple-clients/AstralCore/Sources/AstralCore/Computer/{ComputerHostProtocol,ComputerHostFence}.swift`, Protocol/{Frames,Dispositions}, Transport/WSClient; `AstralApp/Computer/{MacComputerHost,MacComputerDriver,MacComputerInput,MacComputerFiles,MacComputerControls}.swift`, AppModel integration, Views/{RootView,ConsoleShell,MacWorkbenchView}, tests, direct-download build configuration and provenance ledger.

Deep owns narrow changes in `backend/orchestrator/{computer_use_policy,computer_hosts,computer_sessions,remote_confirmation,hitl_confirmation}.py`, `backend/agents/computer_use/mcp_tools.py`, surrounding dispatch integration only where needed, focused tests and qualified composition adoption.

## Interfaces and Decisions

1. `ComputerHostDescriptor` emits the exact v1 descriptor. `Outbound.registerUI` takes an optional descriptor; omission preserves existing behavior.
2. `ComputerHostRequest` validates closed envelope/verb arguments. `ComputerHostFence` reserves one request and emits a ticket bound to connection/session epochs, args digest and receive-relative monotonic deadline; recheck at each effect/response.
3. `MacComputerHost` owns consent/permissions/descriptor/session/serial queue/heartbeat/fence/driver. AppModel supplies current-channel sends and invalidates on socket, owner, server or account changes. No generic offline send queue.
4. Driver uses ScreenCaptureKit bounded JPEG, AX window inventory/focus, marked CGEvents, NSWorkspace validated app bundles and descriptor-relative file access. Missing monitoring/permission/focus causes refusal. Terminal focus needs the existing server grant.
5. Normalize Mac .app names and terminal bundle aliases in Deep; refuse arbitrary Mac app argv while preserving qualified Windows launches. No direct subprocess or tool-name workaround for command restrictions.
6. Exact approval resumes only its still-current originating task after stored action success. Denied/expired/stopped/audit-failed/uncertain work never resumes automatically. Reuse ordinary dispatcher and existing continuation seam.
7. Mac workbench uses real history/console/workspace/activity models and existing component rendering. Make generated results directly interactive, sidebar chat-focused, toolbar quiet, composer expandable, options discoverable. Commands dispatch existing server actions.

## Review Focus

- Queued/delayed input after Stop/host or owner replacement: zero new effects.
- Old approval cannot resume a different task/chat.
- Unknown focus, secure fields and terminal-capable editor windows cannot bypass keyboard review.
- Real symlink races and atomic create/refuse/overwrite/deletion behavior need filesystem tests.
- OS delivery success does not prove semantic browser/native success; assert physical postconditions.

## Execution Order

Write tests first, develop host/server/layout in separate file ownership scopes, then personally verify and review critical seams. Qualify Projection before adopting its exact revision in Deep. Run focused checks then prescribed backend/module/client gates. Qualify staging, install the candidate Mac app, exercise actual positive/denial workflows, then roll out the exact qualified pair with rollback and production verification. Update curated vault at checkpoints. Continue from this plan without a routine design-approval pause under the user's standing instruction.

## Complexity Tracking

No exception. Reusing v1 and reviewed Terminal interaction is narrower than a new shell authority. A native local host is necessary because server-side execution cannot operate the user's Mac.

Builder check rulings: v1 fences geometry/focus/session, not DOM/document identity; verify browser outcomes. Terminal admission boolean is separate from exact source-bound text/chord review. Clear grants on pause/authority loss and grant only current-session approved shell/explicit confirmation. Physical driver work must not block the main actor from honoring Stop.

Shared-client regression matrix: Mac native layout uses negotiated console geometry/capabilities; shared menu/theme/actions remain unchanged. Qualify the changed approval/continuation behavior with Mac and web controllers, and each affected native controller under the repository client gates. No platform waiver is assumed.

Same-Mac integration ruling: exact review itself changes local focus and pauses remote effects. Add explicit reviewed window handles to Mac input and a separate same-host approve-and-resume decision, tightly scoped as in the contract. Do not exempt main-window activity from takeover or secretly restore prior focus. New risk/task/target changes still require review. This extends v1 argument semantics without a new frame.

Terminal ruling: no historical grant carryover ledger. Optional explicit terminal_app+hwnd keyboard review may issue only a one-call internal permit after exact consumed source approval; native checks actual bundle and scope. Global grant stays cleared. Workbench split geometry moves to optional server ROTE console.mac_workbench negotiated with exact Mac opt-in, retained across device refresh; native no longer defines breakpoints.
