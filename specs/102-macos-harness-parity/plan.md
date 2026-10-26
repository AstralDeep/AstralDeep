# Implementation Plan: macOS Harness Parity

**Branch**: `claude/102-macos-harness-parity` | **Date**: 2026-10-25 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/102-macos-harness-parity/spec.md`, contracts in `contracts/`, decisions in `research.md`.

## Summary

Make the direct-download Mac app a first-class agent harness against the sandbox server: fix the 099 approval flow so same-Mac tasks run with one approval, add governed local command execution with captured output, add a DOM-aware in-app browser pane, add a Sparkle-based updater with a signed minimum-version gate and a one-command release pipeline, switch the default theme to Graphite everywhere, cut taglines and helper copy at the source with three layout fixes, and roll the exact server and client pair out to the sandbox with live verification. Work fans out to opus and sonnet agents in lanes with disjoint file ownership; Fable orchestrates, reviews seams and runs the rollout.

## Technical Context

**Language/Version**: Python 3.11 (Deep, Projection Python); Swift 5 language mode, Xcode 27.1, macOS 14.0 deployment target (Projection Apple clients); Node.js built-ins (website).

**Primary Dependencies**: FastAPI/ASGI, Keycloak, astralprims, AstralPlane (unchanged); SwiftUI, AppKit, WebKit, ScreenCaptureKit, CryptoKit; new: Sparkle 2.10.1 (SPM binary target, Projection Apple clients only). No Python dependency added.

**Storage**: Existing Plane data only. Task grants, command jobs, origin approvals and browser snapshots are ephemeral in orchestrator and host memory. No schema change. Theme default is a server constant; stored user themes unchanged.

**Testing**: pytest for Deep and Projection Python with 90 percent changed-line coverage; XCTest via `xcodebuild test` for the Mac app and `swift test` for AstralCore; Node test runner for the website; live verification against the sandbox for every story.

**Target Platform**: macOS 14+ DirectDownload build, arm64; sandbox server image. Other clients: server-owned data only.

**Project Type**: multi-repository: Deep orchestrator, Projection component (Apple clients plus shared chrome), website feed.

**Performance Goals**: command results for short commands in one round trip; browser snapshot under 60,000 characters; update check never blocks launch; no wall-clock assertions in tests.

**Constraints**: owner-bound session authority, exact approval or task grant before every effect, untrusted tagging of captured content, no Astral tokens in child processes, Developer ID identity and designated requirement unchanged, build numbers strictly increasing.

**Scale/Scope**: one owner, one Mac, one session at a time; 4 concurrent command jobs; 1 browser pane.

## Constitution Check

The owner authorized bending hard rules on 2026-10-25 where that helps the ecosystem. Each deviation is explicit in `spec.md`.

- Principle VII: command execution is owner-scoped, default-off, exact-approval gated, task-grantable except for a destructive list, audited at proposal, start, finish, timeout and interruption, and cleared by an exact-implementation host review rather than a renamed tool. Delegated tokens and every runtime authorization control stay in force.
- Principle XII: the new host verbs are Mac-only local capabilities; Windows and Android hosts do not advertise them and the divergence is recorded. Theme default and copy cuts are server-owned data and reach every client; fixtures and parsers for other clients are updated in the same change so none decodes a missing field.
- Principle XI: hosted CI is not consulted. Local gates run: Deep pytest for touched suites, Projection Python suite for touched modules, `swift test`, `xcodebuild test` for the macOS destination, website `npm test` and `npm run build`.
- Principle XIV: Projection changes land on the Projection branch and are built, tested and signed from there; Deep moves `components/AstralProjection` and `config/astral-composition.json` to the exact Projection commit it deploys.
- Principle VIII: no new primitive. Cards reuse Text, Image, Table and Card.
- Principle VI: every new source file opens with a header of at most three sentences and carries no other comments.
- Release publication stays owner-triggered through the local release command's `--publish` flag; no protected publisher, GitHub App or token broker is introduced.

## Project Structure

### Documentation (this feature)

```text
specs/102-macos-harness-parity/
├── spec.md
├── plan.md
├── research.md
├── contracts/
│   ├── task-grant.md
│   ├── local-exec.md
│   ├── browser-verbs.md
│   ├── update-feed.md
│   ├── theme-graphite.md
│   └── copy-cuts.md
├── tasks.md
└── qualification.md          # written during rollout
```

### Source Code

Deep (`/Users/sam/Desktop/Work/AstralDeep`)

```text
backend/shared/protocol.py                         # COMPUTER_HOST_VERBS additions
backend/orchestrator/computer_use_policy.py        # verb classes, scopes, timeouts, summaries, destructive list
backend/orchestrator/computer_sessions.py          # TaskGrant, approved_origins, pause_reason, approval window
backend/orchestrator/computer_grants.py            # new: grant model, matching, revocation, audit
backend/orchestrator/remote_confirmation.py        # grant decision, same-host tolerance, card crop attachment
backend/orchestrator/hitl_confirmation.py          # grant short-circuit for covered verbs
backend/orchestrator/orchestrator.py               # cancel on Stop, workspace_roots passthrough, default theme hook
backend/orchestrator/tool_security.py              # unchanged patterns
backend/orchestrator/terminal_host_review.py       # generalized exact-implementation review
backend/orchestrator/taint.py                      # new sinks
backend/orchestrator/projection_surfaces/theme.py  # Graphite, default, default-send helper
backend/orchestrator/projection_surfaces/*.py      # copy cuts
backend/orchestrator/welcome.py                    # copy cuts
backend/agents/general/mcp_tools.py                # preset copy
backend/agents/computer_use/mcp_tools.py           # run_command tools, macOS descriptions, result screenshots
backend/agents/computer_use/browser_tools.py       # new: browser_* tools
backend/tests/...                                  # new and updated tests
scripts/release_macos.py                           # new: release command
scripts/package_macos_candidate.py                 # reused inspect_app
tests/release/ (Projection)                        # identity guard update
```

Projection (`/Users/sam/Desktop/Work/AstralProjection`)

```text
apple-clients/AstralCore/Sources/AstralCore/Computer/ComputerHostProtocol.swift   # new verbs and fields
apple-clients/AstralCore/Sources/AstralCore/Computer/ComputerBrowserPolicy.swift  # new
apple-clients/AstralCore/Sources/AstralCore/Computer/ComputerCommandPolicy.swift  # new: env allowlist, cwd rules
apple-clients/AstralCore/Sources/AstralCore/Chrome/ConsoleModel.swift             # optional labels
apple-clients/AstralApp/AstralApp/Computer/MacComputerHost.swift                  # approval window, folders, secure input, local pause
apple-clients/AstralApp/AstralApp/Computer/MacComputerInput.swift                 # takeover threshold, approval activity
apple-clients/AstralApp/AstralApp/Computer/MacComputerDriver.swift                # result screenshots, card crop
apple-clients/AstralApp/AstralApp/Computer/MacComputerCommands.swift              # new: process host
apple-clients/AstralApp/AstralApp/Computer/MacBrowserPane.swift                   # new
apple-clients/AstralApp/AstralApp/Computer/MacBrowserDriver.swift                 # new
apple-clients/AstralApp/AstralApp/Resources/browser_snapshot.js                   # new
apple-clients/AstralApp/AstralApp/Views/MacBrowserPaneView.swift                  # new
apple-clients/AstralApp/AstralApp/Views/MacWorkbenchView.swift                    # activity panel, stop gating, chip, copy
apple-clients/AstralApp/AstralApp/Views/ConsoleConversation.swift                 # inline tool steps
apple-clients/AstralApp/AstralApp/Views/ChatView.swift, ComponentView.swift, ComponentChrome.swift, ConsoleShell.swift, RootView.swift, Screens.swift  # copy and layout
apple-clients/AstralApp/AstralApp/Computer/MacComputerControls.swift              # layout, browser pane toggle
apple-clients/AstralApp/AstralApp/Updates/UpdateCoordinator.swift                 # new
apple-clients/AstralApp/AstralApp/Updates/UpdatePolicyGate.swift                  # new
apple-clients/AstralApp/AstralApp/Updates/UpdateSettingsSection.swift             # new
apple-clients/AstralApp/AstralApp/AstralAppMain.swift                             # updater wiring, menu
apple-clients/AstralApp/AstralApp.xcodeproj                                       # Sparkle, 1.9 (70)
apple-clients/AstralApp/AstralApp/Info.plist (macOS)                              # SU keys
apple-clients/NativeAppearance/Theme.swift                                        # Graphite
backend/webrender/static/client.js, astral.css                                    # Graphite tables
backend/webrender/chrome/console_model.py, web_landing.py, component_model.py     # copy cuts
src/astralprojection/chrome/personalization.py, *.py                              # Graphite, copy cuts
windows-client/astral_client/theme.py; android-client/.../Theme.kt                # Graphite tables
contracts/ui_protocol.json, contracts/fixtures/console/*.json                     # optional console labels
tests/...                                                                         # updates
```

Website (`/Users/sam/Desktop/Work/astraldeep.github.io`)

```text
site/appcast.xml                     # generated by the release command
site/data/mac-update-policy.json(.sig)
scripts/appcast.mjs                  # new: parse and cross-check
scripts/build.mjs                    # cross-check call
tests/appcast.test.mjs               # new
```

## Interfaces and Decisions

1. Verbs are added to the closed sets in Deep, Swift and nowhere else; Windows keeps its list. `test_computer_use_076.py` asserts policy equals protocol and is updated.
2. Task grants live in orchestrator memory keyed by session; `hitl_confirmation` consults them before raising a card for covered verbs; `remote_confirmation.handle_decision` creates them from the third decision; `computer_sessions` revokes them with pause, stop, takeover and authority changes.
3. Same-host approval tolerance is a host-side classification with a server-side acknowledgement event, so the server log distinguishes approval activity from takeover.
4. Command execution is host-side `posix_spawn` in a process group with ring buffers; the server holds no process. Background jobs are addressed by job id; the server stores job ids per session for cleanup on Stop.
5. Browser verbs are host-side WebKit operations with an isolated content world script; the server owns origin approvals and consequential cards.
6. The updater is Sparkle with an `UpdateCoordinator` delegate; the minimum-version gate is CryptoKit Ed25519 over a JSON document signed by the appcast key; failures fail open.
7. The release command is Python in Deep so it can reuse `package_macos_candidate.inspect_app`; publication requires `--publish`.
8. Graphite is sent as effective colors when nothing is stored so every client renders it without a client change; the Mac baked default changes so there is no flash.
9. Copy cuts remove strings at the source; the console contract makes labels optional before the server stops sending them.

## Execution Order and Lanes

Fable is the orchestrator: writes tasks, dispatches lanes, reviews seams, runs builds, signs, installs, deploys and records. Lanes run as subagents with disjoint file ownership. A lane never edits a file owned by another lane; shared edits are placed in the foundational phase and finished before lanes start.

| Lane | Model | Owns |
|---|---|---|
| F (foundational) | opus | `shared/protocol.py`, `computer_use_policy.py` verb tables, Swift `ComputerHostProtocol.swift`, `ConsoleModel.swift`, `computer_grants.py` skeleton, tests for each |
| S1 server approvals and commands | opus | `computer_grants.py`, `computer_sessions.py`, `remote_confirmation.py`, `hitl_confirmation.py`, `orchestrator.py`, `terminal_host_review.py`, `taint.py`, `agents/computer_use/mcp_tools.py`, their tests |
| S2 server browser | sonnet | `agents/computer_use/browser_tools.py`, `computer_use_policy.py` browser summaries and origin card, tests; one registration hook in `mcp_tools.py` agreed with S1 |
| H1 Mac host approvals and commands | opus | `MacComputerHost.swift`, `MacComputerInput.swift`, `MacComputerDriver.swift`, `MacComputerObservation.swift`, `MacComputerCommands.swift`, `ComputerCommandPolicy.swift`, `ComputerWorkspace.swift` fixes, host tests |
| H2 Mac browser pane | opus | `MacBrowserPane.swift`, `MacBrowserDriver.swift`, `browser_snapshot.js`, `MacBrowserPaneView.swift`, `ComputerBrowserPolicy.swift`, pane toggle in `MacComputerControls.swift`, tests |
| U updater and release | sonnet | `Updates/*.swift`, `AstralAppMain.swift`, pbxproj, Info.plist, `UpdateSettingsSection` hook, `scripts/release_macos.py`, website `appcast.mjs`, `build.mjs`, `tests/appcast.test.mjs`, identity guard test |
| M1 Mac workbench UX | opus | `MacWorkbenchView.swift`, `ConsoleConversation.swift`, `AppModel.swift` cancel and activity model, status chip placement, their tests |
| T theme tables | sonnet | Deep `theme.py`, `general/mcp_tools.py`; Projection `personalization.py`, `client.js`, `astral.css`, Windows `theme.py`, Android `Theme.kt`, parity and theme tests |
| M2 Mac theme and copy | sonnet | `Theme.swift`, `ChatView.swift`, `ComponentView.swift`, `ComponentChrome.swift`, `ConsoleShell.swift`, `RootView.swift`, `Screens.swift`, `MacComputerControls.swift` layout, `MarkdownBlockView.swift`, `OfflineChartView.swift`, Swift copy and theme tests |
| C server copy | sonnet | Deep `welcome.py`, `projection_surfaces/*.py` copy; Projection `console_model.py`, `web_landing.py`, `component_model.py`, `chrome/*.py`, `terminal.py`, `ui_protocol.json`, fixtures, Python copy tests |

Order

1. F finishes and its tests pass. Fable reviews the verb and grant shapes against the contracts.
2. S1, H1, U, T and C start in parallel.
3. S2 starts when S1 has landed the `mcp_tools.py` registration hook; H2 starts when F has landed the Swift verbs. S2 and H2 run in parallel with the rest.
4. M1 starts when H1 has landed the activity and cancel seams in `AppModel.swift`; M2 starts when M1 has finished `MacWorkbenchView.swift` and when T has fixed Graphite values.
5. Fable integrates: builds AstralCore and the Mac app, runs the Swift suites, runs Deep and Projection Python suites for touched modules, reviews the critical seams (grant revocation, same-host tolerance, command kill paths, browser secret refusal, update signature failure).
6. Rollout (US7): Fable with the owner.

## Verification

- Unit and contract tests per lane, written before implementation, covering golden path, boundary, denial and failure.
- Fable runs: `python -m pytest backend/tests -k "computer or grant or command or browser or theme or welcome or chrome" -q` in the Deep container; Projection `python -m pytest tests -q` for touched modules; `swift test --package-path apple-clients/AstralCore`; `xcodebuild test -scheme AstralApp -destination 'platform=macOS'`; website `npm test`, `npm run check`, `npm run build`.
- Live verification against the sandbox after rollout, per story, recorded in `qualification.md` with timestamps and the exact image and build identities.

## Rollout and Rollback

1. Build the server image from the exact candidate tree (`git archive` of Deep plus components) as `astraldeep:sha-<rev>`; owner opens SSH and types sudo; scripts take the paired backup, cut over with the persisted override, verify `/healthz`, `/readyz`, LETS and PostgreSQL, keep the previous image and override for rollback.
2. Build, sign and notarize the Mac candidate 1.9 (70) with the release command without `--publish`; install it over build 69 keeping 69 recoverable; verify signature and identity.
3. Owner signs in; run each story's independent test; record.
4. On the owner's word, run the release command with `--publish` for 1.9 (70) so later builds arrive through the feed.

## Risks

- Same-host approval tolerance is new behavior in the takeover path; a wrong threshold either re-creates the deadlock or masks real takeover. Mitigation: bounded 10-second window, two clicks, Astral windows only, server-side event.
- Command execution widens what the agent can do on the owner's Mac. Mitigation: default-off, roots required, exact approval, destructive list, sanitized environment, audit, task-scoped grants only.
- WKWebView refuses some sign-ins. Mitigation: clear result and fallback to the desktop path.
- Sparkle plus the exact entitlement assertion in the packager: Sparkle needs no entitlement, but the framework must be embedded and signed by the same Team; verified by `codesign --verify --deep --strict` in the release command.
- Shared files between lanes. Mitigation: ownership table above; Fable resolves any overlap before dispatch.
