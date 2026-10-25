# Feature Specification: macOS Agent Workbench

**Feature Branch**: `codex/099-macos-agent-workbench`

**Created**: 2026-10-24

**Status**: Draft — implementation and qualification in progress

**Input**: Implement the useful interaction and operating-system capabilities of SPACE, Claude Code, Codex, OpenCode, Pi, and Hermes in Astral, prioritizing the Mac app operating a browser, clicking desktop controls, and running commands against the production service at `sandbox.ai.uky.edu`, while preserving generative UI and security.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Connect and control my Mac (Priority: P1)

As the signed-in owner, I enable this Mac for computer tasks, see exactly which permissions are available, and ask Astral to operate a benign native application. A persistent local indicator identifies the active task and offers Pause and Stop.

**Why this priority**: The current Mac client cannot execute computer requests. All local interaction depends on a real, visible, revocable host.

**Independent Test**: Enable consent in the direct-download Mac build, start an owner-authenticated session, observe the desktop, open a benign application, interact with its controls, and read back the actual result.

**Acceptance Scenarios**:

1. **Given** a signed-in Mac with local control disabled, **When** a task requests its desktop, **Then** Astral explains how to enable access and performs no local effect.
2. **Given** enabled local consent and granted system permissions, **When** the owner starts a session, **Then** Astral discovers the Mac and its displays, shows an active local indicator, and executes only requests addressed to that active host/session.
3. **Given** missing or revoked system permission, a locked desktop, a paused session, or a disconnected authenticated channel, **When** a request arrives, **Then** the host refuses it with an actionable reason and performs no effect.
4. **Given** an active task, **When** the person uses the local keyboard/mouse or selects Pause/Stop, **Then** future remote effects cease; resumption requires the appropriate current session authority.

### User Story 2 — Complete browser work from a chat (Priority: P1)

As the owner, I ask Astral to navigate my browser, find a control, enter text, click, scroll, and inspect the result. The browser remains visible and I can take over. Astral prefers a narrower supported integration when one is available and uses desktop interaction only within the approved session.

**Why this priority**: Browser interaction is the user's primary missing capability and a common harness workflow.

**Independent Test**: From ordinary authenticated chat, complete a deterministic browser fixture containing navigation, form entry, a button, scrolling, and a resulting status value; compare the final browser state with the requested outcome.

**Acceptance Scenarios**:

1. **Given** an approved browser interaction, **When** Astral observes and acts, **Then** it can identify the current app/window, navigate to the fixture, fill the form, activate its button, and verify the resulting value.
2. **Given** the host observes changed focus, window identity, or display geometry before an effect, **When** the host can establish that the previous observation is no longer valid, **Then** Astral refreshes its observation or reports a stale target rather than claiming success. The pixel-based transport does not prove browser-document identity; every action requires result verification.
3. **Given** page or accessibility text containing instructions to disclose secrets or expand permissions, **When** Astral reads it, **Then** the content remains untrusted evidence and cannot authorize an effect.
4. **Given** a login, password manager, payment, upload, or other consequential boundary, **When** the task reaches it, **Then** the ordinary identity, disclosure, file, and confirmation policies still apply; the user completes authentication.

### User Story 3 — Run commands and work with selected files (Priority: P1)

As the owner, I run an explicitly reviewed command on my Mac, see the real result, and read or change files within folders I have selected. Astral distinguishes local Mac execution from remote isolated execution and shows the target before approval.

**Why this priority**: A harness must be able to accomplish real OS work without confusing the server filesystem, remote runtime, and local Mac.

**Independent Test**: Approve a harmless command in the Mac's visible Terminal, create and read a synthetic file in a selected test directory, and verify the result independently. Reject an out-of-root path and an unapproved command with zero effects.

**Acceptance Scenarios**:

1. **Given** an approved terminal launch and exact reviewed keyboard actions, **When** Astral executes a command, **Then** the command runs on the selected Mac and its observable result is returned through the normal tool path.
2. **Given** a selected workspace, **When** Astral lists/reads/writes a permitted file, **Then** bounds, overwrite rules, and source-bound approval apply; unrelated files remain intact.
3. **Given** a path escape, symlink escape, missing grant, oversized payload, or refused confirmation, **When** an operation is attempted, **Then** it fails before modifying local state.
4. **Given** a timeout or disconnect after a command or write may have started, **When** the task resumes, **Then** Astral reports uncertainty and inspects current state before any retry.

### User Story 4 — Understand, steer, and review the task (Priority: P1)

As the owner, I see the plan, current operation, target machine/application, approval state, observations, and result while retaining Astral's generated canvas. I can interrupt or steer the task without losing its evidence or starting an invisible duplicate.

**Why this priority**: The useful shared UX across the harnesses is a legible, steerable tool loop, not merely access to tools.

**Independent Test**: Complete a multi-step task through an approval, inspect the resulting screenshot and file, pause and resume, then interrupt a second task; verify visible states match actual effects.

**Acceptance Scenarios**:

1. **Given** a multi-step task requiring review, **When** the owner approves an exact pending action, **Then** Astral re-enters ordinary authorization, executes that action once, updates its review card, and resumes only the still-current originating task. When approving on the controlled Mac itself, a separate clearly labeled approval may explicitly resume the same session on the reviewed target; ordinary approval never silently overrides Pause or Stop.
2. **Given** a declined, expired, superseded, wrong-owner, or stopped proposal, **When** an old decision arrives, **Then** no effect or automatic continuation occurs.
3. **Given** active computer work, **When** the owner opens the computer-management surface or local controls, **Then** task state, permission failures, and Pause/Resume/Stop remain accessible with a keyboard and assistive technology.
4. **Given** a non-Mac controller, **When** it views the same task, **Then** it receives the same server-owned task/result/approval content; a client that cannot host local execution reports that capability honestly.

### User Story 5 — Use the qualified production/client pair (Priority: P1)

As the owner, I use the installed Mac app with the production sandbox service, rather than a development-only demonstration. Updates retain identity, generated UI, configured providers, and existing user data.

**Why this priority**: Local unit tests alone do not satisfy the requested operational outcome.

**Independent Test**: Install the qualified direct-download Mac build, sign in through the existing identity provider, and complete the native, browser, terminal, file, refusal, and Stop scenarios against the qualified production deployment.

**Acceptance Scenarios**:

1. **Given** a qualified candidate and a recorded rollback, **When** the service is updated, **Then** health and readiness pass and the exact server/component/client identities are retained with the evidence.
2. **Given** the real installed Mac client and real owner identity, **When** the acceptance tasks run, **Then** the physical effects and denials are independently verified through the normal dispatcher.
3. **Given** an unavailable prerequisite, **When** qualification runs, **Then** the result records the missing gate and never labels that scenario passed, deployed, or released.

### User Story 6 — Work in a clear native Mac workspace (Priority: P1)

As a user or developer, I navigate chats, work in the conversation alongside generated results, and inspect activity and approvals without a dashboard obscuring my task. Common actions have discoverable shortcuts and advanced options remain available without crowding the workflow.

**Why this priority**: The user explicitly authorized rebuilding the Mac UI using the most usable patterns from all six harnesses.

**Independent Test**: At compact and wide window sizes, find/start chats, submit a task, interact with generated results, inspect activity/approvals, and reach local-computer and developer options using the keyboard.

**Acceptance Scenarios**:

1. **Given** a wide Mac window, **When** the owner works on a task, **Then** task navigation, conversation/generated work and optional activity inspection have a clear hierarchy with collapsible or resizable regions.
2. **Given** a compact window or larger accessibility text, **When** the layout adapts, **Then** the composer, task content, approvals and Stop remain reachable.
3. **Given** server-owned capabilities and menus, **When** the shell presents actions, **Then** it uses those definitions and truthful state rather than duplicate static capabilities or fabricated task data.

### Edge Cases

- Duplicate or malformed requests, stale session IDs, host replacement, expired local deadlines, delayed responses, and reconnect never replay a physical effect automatically.
- Display scaling, multiple displays, negative global display origins, changed display layouts, and windows moving between displays preserve coordinate meaning or cause a fresh observation.
- Secure text entry, protected windows, denied capture, missing applications, unsupported keys, and app termination produce bounded errors rather than silent success or broad fallbacks.
- Disconnect, pause, lock, logout, and local consent withdrawal clear terminal grants and pending local authority.
- Observations, screen contents, clipboard, and files may contain sensitive material; existing provider-disclosure, PHI, and audit policies remain authoritative and no raw content is placed in repository evidence.
- App Store and direct-download builds have explicit execution dispositions; enabling a server flag cannot turn an author-only distribution into a host.
- User authentication and operating-system permission dialogs are completed by the user; no developer token or permission-database bypass is introduced.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The direct-download Mac app MUST offer explicit default-off local-computer consent and identify missing operating-system permissions before advertising affected capabilities.
- **FR-002**: Computer requests MUST be accepted only through the authenticated owner's current host channel and active session, with per-request validation, local expiry, duplicate suppression, and a final current-authority check immediately before each physical effect.
- **FR-003**: The Mac host MUST support screenshot capture, app/window observation, focusing/opening applications, pointer clicks/move/drag/scroll, bounded text/key input, and approved workspace/clipboard operations, advertising only implemented capabilities.
- **FR-004**: Browser work MUST complete the observation–action–verification loop on the declared supported browser; the user MUST retain visibility and local takeover.
- **FR-005**: Local command execution MUST use the existing approved visible-terminal path until a separate command execution contract has been specified and qualified. The feature MUST NOT add or rename a tool to evade the current arbitrary-command prohibition.
- **FR-006**: Every consequential action MUST retain the ordinary scope, owner, delegation, source-bound review, emergency-stop, PHI, egress, and pre-effect audit checks. Broad task consent MUST NOT substitute for exact approvals required by current policy. Terminal admission MUST be bound to the current owner/host/session, granted only by a recognized approved shell launch or explicitly validated terminal-specific confirmation (including exact per-action keyboard review with a named terminal/window), and cleared on pause or any authority loss; unrelated approval MUST NOT grant terminal input. Exact text/key review is separate from standing terminal admission; per-action review may authorize only that one exact input and MUST NOT restore a session-wide grant.
- **FR-007**: Approval continuation MUST be bound to the originating owner/chat/task and current cancellation state, execute the stored exact action at most once, and stop on denial, expiry, changed ownership, or uncertain completion.
- **FR-008**: The Mac MUST display an accessible persistent active-session indicator with Pause, Resume, and Stop; local input MUST pause remote effects without treating injected events as user input.
- **FR-009**: File operations MUST be bounded to user-selected workspace roots, resolve symlinks safely, preserve unrelated data, and enforce overwrite/deletion review and payload bounds.
- **FR-010**: A lost channel, lock, withdrawn consent, pause, Stop, host replacement, or expired request MUST prevent subsequent local effects; uncertain effects MUST remain explicitly uncertain and MUST NOT be blindly retried.
- **FR-011**: Plans, approvals, progress, observations, and results MUST continue to use Astral's server-owned structured UI and existing primitive vocabulary; no parallel frontend or product chrome catalog is introduced.
- **FR-012**: Existing controller clients MUST preserve their server-owned task and approval behavior; Mac-only host facilities MUST be declared as local capabilities and unsupported hosting MUST remain explicit elsewhere.
- **FR-013**: The App Store distribution MUST remain author/controller-only. The direct-download host variant MUST retain the registered app identity, production identity flow, hardened-runtime posture, and explicit distribution metadata.
- **FR-014**: Results MUST distinguish accepted transport, attempted operation, verified postcondition, failed operation, interruption, and uncertain completion. Captured content MUST remain lower-trust evidence.
- **FR-015**: Research MUST cite pinned primary evidence for all six named harnesses, distinguish publicly available source from documented proprietary behavior, and map selected patterns to acceptance evidence without claiming untested universal parity.
- **FR-016**: Changed behavior MUST include golden-path, boundary, denial, and failure tests, at least 90% coverage on changed executable Python lines, and live verification on each affected client.
- **FR-017**: Production rollout MUST use an exact qualified artifact/component set, preserve data and configured identities, record the prior deployment and rollback, and verify ordinary authenticated native/browser/command/file tasks and refusals after rollout.

- **FR-018**: The native Mac layout MUST provide clear chat/task navigation, conversation and interactive generated results, optional activity/approval inspection, an expandable composer, discoverable shortcuts, and accessible local-computer/developer options, adapted to window space.
- **FR-019**: The redesign MUST preserve existing server-provided capability/theme semantics and generated rendering; a native adaptive arrangement MUST NOT invent a parallel capability catalog.

### Key Entities

- **Local computer consent**: Owner-controlled local opt-in, permission state, selected workspace roots, and execution-capable distribution.
- **Computer host**: Stable machine identity, current authenticated connection, supported operations, display inventory, and online state.
- **Computer session**: Owner, controller, target host, active/paused/ended state, terminal grant, local authority generation, and expiry.
- **Computer operation**: Correlation identity, session, verb, bounded arguments, receive-relative deadline, local generation, effect status, and result.
- **Task continuation**: Originating task/owner/chat authority and the exact reviewed operation whose completion can resume that task.
- **Qualification record**: Candidate server/component/client identities, actual commands/results, physical effect oracles, denials, deployment/rollback state, and outstanding gates.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: The installed production-connected Mac app completes all four benign acceptance workflows: native app interaction, browser form workflow, terminal command, and selected-workspace file round-trip.
- **SC-002**: The denial matrix demonstrates zero physical effects for each tested missing permission, missing consent, wrong owner/session, duplicate, expired, paused/stopped, disconnected, and out-of-root request.
- **SC-003**: A multi-step task resumes after an exact approval only while its original task remains current; duplicate/declined/expired decisions execute zero additional effects.
- **SC-004**: Pause/Stop/local takeover prevent the next scheduled physical effect, and reconnect does not replay any completed or uncertain effect.
- **SC-005**: Every completed workflow has an independently checked postcondition and a visible result; every failed or uncertain workflow is labeled accordingly.
- **SC-006**: The research comparison covers all six harnesses and every selected pattern has a traceable implemented/tested/live-verified or outstanding disposition.
- **SC-007**: The qualified production/client pair passes the same authenticated positive and denial workflows used in staging, with exact identities and a usable rollback recorded.

- **SC-008**: The native workspace passes story6 keyboard/layout scenarios at compact and wide sizes without unreachable composer, review or Stop controls.

## Assumptions

- The user authorized implementation and production integration and asked work to continue without routine design-approval pauses. Implementation choices remain the implementer's responsibility.
- The first complete local command workflow uses reviewed Terminal interaction; a direct PTY/process service is a separate contract change and cannot be smuggled into this feature under a different tool name.
- A first-party native computer executor is separate from the draft feature 059 user-authored-agent runtime; this feature does not falsely advertise that runtime as implemented.
- Initial browser qualification uses installed Google Chrome and a deterministic benign fixture. Other browser support is claimed only after its own live acceptance.
- Existing security requirements take precedence over a harness's convenience features. The comparison measures selected workflows and never asserts blanket superiority based on feature counts.
- The user supplies VPN/server access and performs real identity sign-in and operating-system permission approvals when required.

## Clarifications

### Session 2026-10-24

- The user expanded the comparison from SPACE/Claude Code/Codex to include OpenCode, Pi, and Hermes; all six are research inputs to FR-015 and SC-006.
- Security and generated UI are explicit requirements, so existing exact-action approval and arbitrary-command restrictions remain in force.
- Critical product intent is clear. Distribution, native APIs, internal module structure, and test fixture design are implementation decisions, not unasked user approvals.

- The user explicitly permits replacing the Mac UI and downloading the harnesses for layout comparisons; FR-018/019 preserve shared semantics while allowing a new native arrangement.

## Owner clarification — 2026-10-25

The owner requests that **Allow computer tasks on this Mac** remain selected across app close/reopen. The saved opt-in is bound to the same account/server and survives temporary desktop/permission unavailability; explicit uncheck revokes it persistently. A fresh authenticated process may restore only that remembered choice after distribution, unlocked-desktop, permission and monitoring checks. Lock or permission loss still immediately ends live authority, sessions, work queues and Terminal grants; same-process reconnect/refresh cannot bypass the local rearm latch. The checkbox represents the saved choice, and a separate local Resume action handles temporary suspension. First enable and local Resume retain genuine local-input checks.
