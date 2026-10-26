# Feature Specification: macOS Harness Parity

**Feature Branch**: `claude/102-macos-harness-parity` (Deep, Projection, and `astraldeep.github.io`)

**Created**: 2026-10-25

**Status**: Draft, owner-directed; implementation starts on approval of this document

**Input**: Owner direction on 2026-10-25: add an autoupdate feature like SPACE, Claude Code, Codex, Pi and Hermes; check Codex's feature 099 work; cut taglines, helper text and secondary labels, fixing layout where a screen then needs it; change the default color theme to a black, gray and purple scheme for all clients while keeping the other presets; elevate UX, DX and AX without breaking anything. Scope is the macOS client and the sandbox server only. CI is out of scope. Server changes deploy directly to `sandbox.ai.uky.edu`. The owner has authorized bending the constitution's hard rules where that helps the ecosystem, and is removing the Mac app from the Mac App Store in favor of direct download from `astraldeep.github.io`.

## Decisions already taken by the owner

1. Commands run the way Claude Code and Codex CLI run them: a governed local subprocess with captured output, protected by secure delegation and exact approval. This replaces 099's "visible Terminal keystrokes only" posture.
2. Browser work happens in an in-app browser pane when that works better than pixel clicks. The user's own browser stays reachable through the existing pixel path.
3. The new default theme applies to every client. Direction: "Claude and Codex black with gray accents and maybe purple accents." Existing presets remain selectable.
4. Copy is cut at the source, including server-owned strings that reach other clients.
5. Notarization uses the owner's App Store Connect API key stored as keychain profile `astral-notary`.
6. The Mac App Store lane is retired for macOS. The DirectDownload build is the only Mac distribution.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Operate my Mac from chat without approval fatigue (Priority: P1)

As the signed-in owner on this Mac, I ask Astral to do desktop work. I approve once for the task's scope, and Astral keeps working without a card for every click. Approving from the same Mac works. I can interrupt a turn with a key, see each tool step as it happens, and nothing runs after I stop it.

**Why this priority**: Codex's 099 host is complete but has never executed a physical effect against production. Review found that approving on the same Mac voids the approval, every input raises its own card, and the approval click wipes the Terminal grant. Nothing else in this feature can be live-verified until this works.

**Independent Test**: From the installed Mac app signed in to the sandbox, enable computer access, ask Astral to open TextEdit and type a sentence. One approval card appears; Astral completes four or more input actions with no further cards; the typed text is read back from the window. Pressing Esc during a later turn cancels it. Stop ends the session and a queued action produces no effect.

**Acceptance Scenarios**:

1. **Given** a computer task whose first input action needs review, **When** the card appears, **Then** it offers Approve once, Allow for this task in the named application or window, and Deny; the task grant is time-boxed, bound to the owner, host, session, chat and application, and revoked on pause, Stop, takeover, authority loss, host replacement or application change.
2. **Given** the approval controller is the Mac being controlled, **When** I click Approve, **Then** the pointer movement and click that approving itself causes do not pause the session or void the approval; the next effect proceeds.
3. **Given** an approval card for a pointer action, **When** it is shown, **Then** it carries a cropped screenshot with the target marked and the window title, and never raw coordinates alone.
4. **Given** a running turn, **When** I press Esc or Command-period in the workbench, **Then** the server cancels the turn, the host performs no further effects, and the transcript shows the cancellation; Stop in the toolbar does the same and ends the computer session.
5. **Given** a password field or Secure Keyboard Entry is active, **When** a keyboard action arrives, **Then** only keyboard input is refused with a precise reason while pointer actions, observation and the host registration continue.
6. **Given** I paused the session locally, **When** a remote Resume arrives from the server or another client, **Then** it is refused; only local Resume continues the session.
7. **Given** takeover detection, **When** my mouse moves a few pixels, **Then** the session does not pause; a click, key press, scroll or movement beyond a threshold pauses it.
8. **Given** a task is running, **When** I open Activity, **Then** a non-blocking side panel lists one row per tool call with action, target, approval state, result and thumbnail, and the same steps appear inline in the transcript.
9. **Given** selected workspace folders exist, **When** the agent lists computers, **Then** the folders are reported so the agent can address them.
10. **Given** an input action completes, **When** the result returns, **Then** it carries a fresh screenshot so the agent does not need a second round trip.

---

### User Story 2 - Run commands on my Mac the way Claude Code does (Priority: P1)

As the owner, I ask Astral to run a command. The approval card shows the exact command, working directory and timeout, and offers to allow that program in that folder for the rest of the task. The command runs on my Mac. Astral gets stdout, stderr and the exit code back and reasons on them. Long jobs can be read incrementally and interrupted.

**Why this priority**: The owner named this as the single largest gap to parity. Today the only path is typing into a visible Terminal and reading the result from a screenshot.

**Independent Test**: With one workspace folder selected, ask Astral to count the lines in a file there. A `run_command` card appears naming the Mac, `wc -l <file>`, the folder and a timeout. Approve. The agent reports the count from captured stdout. Deny a second command: zero effect. Request a command with a working directory outside the folder: refused before launch. Run `sleep 30` with a 5-second timeout: killed and reported as timed out. Start `tail -f` in the background, read its output twice, interrupt it.

**Acceptance Scenarios**:

1. **Given** a selected workspace folder, **When** the agent proposes `run_command` with a working directory inside it, **Then** the card shows the Mac name, the exact command, the working directory, the timeout and any environment additions; Approve runs it and the result carries stdout and stderr tails, exit code, duration and a truncation flag through the ordinary tool path.
2. **Given** no selected folder or a working directory outside every folder, **When** `run_command` is proposed, **Then** it is refused before anything launches.
3. **Given** I choose Allow `<program>` in `<folder>` for this task, **When** later commands use the same program under that folder, **Then** they run without a card until the task ends, the session pauses or stops, or authority changes; commands matching the destructive list (privilege escalation, recursive deletion, piping downloads into a shell, force pushes, disk or system modification) always require exact review.
4. **Given** a command exceeds its timeout, **When** the deadline passes, **Then** its whole process group is terminated and the result says timed out; the agent does not retry blindly.
5. **Given** `background` is requested, **When** the command starts, **Then** a job id returns; `read_output` reads from an offset with a bounded ring; `interrupt_command` signals it; all jobs end with the session, Stop or app quit.
6. **Given** the sandbox server, **When** the agent lists its tools, **Then** the command tools appear on the existing computer-use agent with macOS-specific descriptions, and the tool-security scanner clears them through an exact-implementation host review rather than a renamed tool.
7. **Given** a command runs, **When** its environment is built, **Then** it inherits a sanitized login-shell environment with no Astral tokens, and its output is tagged untrusted for the model.

---

### User Story 3 - Do browser work in an Astral browser pane (Priority: P1)

As the owner, I ask Astral to open a page in the Astral browser pane, find a control, enter text, click, scroll and read the result. I watch the pane, can take over at any time, am asked once per new site, and know that passwords and payment fields are never filled by the agent.

**Why this priority**: Browser interaction is the second gap the owner named. Today the agent has a screenshot and pixel clicks with no way to open a URL, read a page or address an element.

**Independent Test**: Ask Astral to complete the 099 deterministic browser fixture (navigation, a form, a button, scrolling, a status value) in the pane. The first navigation asks for origin approval once. The agent completes the fixture using `browser_*` actions with zero pixel clicks, and verifies the status value from a snapshot. A page containing an instruction to reveal a token is read but the instruction is inert. A password field is refused.

**Acceptance Scenarios**:

1. **Given** the pane is enabled in Computer controls, **When** the agent navigates to an origin not yet approved in this session, **Then** an approval card names the origin; approved once, later navigations within that origin proceed; denied, nothing loads.
2. **Given** a loaded page, **When** the agent requests a snapshot, **Then** it receives a bounded accessibility-style tree with stable element references, the URL, title and a snapshot id; click, fill, press and scroll accept a reference; a reference from an older snapshot that no longer resolves returns a stale-reference error, not a misdirected action.
3. **Given** page text containing instructions, **When** it is returned, **Then** it is wrapped as untrusted content and cannot authorize or widen any action.
4. **Given** a password, card or one-time-code field, **When** the agent tries to fill or read it, **Then** the host refuses outright and the field's value is redacted in snapshots; a submit control inside a form with such fields, or a control whose text means buy, pay, delete or send, returns confirmation required and proceeds only after the ordinary source-bound approval.
5. **Given** a JavaScript dialog, download, upload or popup, **When** it occurs, **Then** dialogs are surfaced and queued for the agent to answer; downloads, uploads and popups are blocked in this version with a clear result.
6. **Given** the pane is open, **When** I type or click in it, **Then** the session pauses as with any takeover; the pane shows who is driving and a Take over control.
7. **Given** a site refuses embedded web views, **When** navigation fails that way, **Then** the result names the cause and suggests the user's own browser through the existing desktop path.

---

### User Story 4 - Keep the Mac app current automatically (Priority: P1)

As the owner, when a new build is published, the Mac app notices within a day or when I ask, downloads and verifies it, and offers to install. It never restarts while a task is running. A build the server no longer supports tells me to update before it connects. Publishing a release is one local command.

**Why this priority**: The owner's first request. There is no updater of any kind today; the only machine-readable release record is a single JSON file on the Downloads site, and replacement is done by hand.

**Independent Test**: Install the first updater-enabled build, build the next one, serve it from a local feed, and check for updates: the app shows the release notes, installs and relaunches. Corrupt the signature: the update is refused and the installed version keeps working. Start a computer task, then choose Install: the app shows that it will restart when idle and relaunches only after the task ends.

**Acceptance Scenarios**:

1. **Given** the published feed lists a newer build, **When** the scheduled check runs or I choose Check for Updates in the app menu, **Then** the app shows the version and release notes with Install, Later and Skip this version.
2. **Given** a downloaded update, **When** its signature does not verify against the embedded public key or its code signature does not match the installed app's identity, **Then** the update is refused, the installed version keeps running, and the failure is visible in the status indicator.
3. **Given** a computer, browser or command task is running, **When** install and relaunch is chosen, **Then** relaunch is postponed with a visible "restarts when idle" state and proceeds when the task ends or on quit.
4. **Given** Settings, **When** I open the update section, **Then** I can toggle automatic checks, automatic install, and the preview channel; the current build and last check time are shown.
5. **Given** the signed minimum-version policy names a build newer than mine, **When** the app launches, **Then** it shows an update-required state with the Check control before connecting; when the policy cannot be fetched, the app connects normally.
6. **Given** a release is ready, **When** the release command runs, **Then** it builds, signs, notarizes with the `astral-notary` profile, staples, zips, signs the archive, writes the appcast and the Downloads record, creates an immutable GitHub prerelease or release, and opens the website change; the website build verifies that the appcast and the Downloads record describe the same asset.
7. **Given** two consecutive releases, **When** their build numbers are compared, **Then** they strictly increase, and the identity guard tests accept the new numbers.

---

### User Story 5 - Graphite default theme everywhere (Priority: P2)

As any Astral user who never chose a theme, I see a near-black workspace with gray surfaces, light-gray text and purple accents on every client. Users who chose a theme keep it. The other presets stay available.

**Why this priority**: Owner request. It is server-owned and reaches every client, so it is deliberately sequenced after the Mac-only safety and capability work.

**Independent Test**: Sign in on the Mac with no stored theme: the sign-in screen and workbench are Graphite with no flash of the old palette. Open Theme settings: Graphite is listed first and shown as the default. Choose Midnight: Midnight applies and persists. Sign in on the web shell with no stored theme: Graphite.

**Acceptance Scenarios**:

1. **Given** the preset tables in the server, the chat tools, the Projection chrome, the web shell, Windows, Android and the Apple palette, **When** the parity test runs, **Then** Graphite exists in all of them with identical values.
2. **Given** a user with no stored theme, **When** they register a client, **Then** the server sends the Graphite colors as the effective theme so every client renders it without a client code change.
3. **Given** the Mac app launches, **When** the sign-in screen appears before any server frame, **Then** it is already Graphite.
4. **Given** a stored preset or custom colors, **When** the user registers, **Then** their choice wins over the default.
5. **Given** status colors, **When** rendered on Graphite surfaces, **Then** success, warning, error and info remain distinguishable with at least 3:1 contrast against the surface, and the banner bar carries an icon so state is not conveyed by hue alone.

---

### User Story 6 - Quiet copy and clear layouts (Priority: P2)

As a Mac user, I see controls and results, not taglines, explanatory sentences or captions. Where a screen depended on that text, the layout now carries the meaning.

**Why this priority**: Owner request. It touches shared server strings, so it follows the Mac-only work and lands with its tests.

**Independent Test**: Open the workbench, composer, canvas, Settings, Computer controls and Theme surfaces. No tagline or helper sentence is visible. The empty composer is obviously a text field with a send control. Viewing history shows an inline "Back to live" control instead of a sentence. Settings fields show labels and values only. The forbidden-copy test list covers every cut string.

**Acceptance Scenarios**:

1. **Given** the copy inventory in `contracts/copy-cuts.md`, **When** the cuts land, **Then** every string marked CUT is gone from the Mac rendering and from the server source where it originates; strings marked KEEP or state remain; accessibility labels and hints remain.
2. **Given** the composer, **When** empty, **Then** it has no placeholder sentence; the field outline and the send control carry the affordance; when history is being viewed, an inline "Viewing history" chip with Back to live replaces the sentence.
3. **Given** the canvas header, **When** a result is shown, **Then** the header shows the result title or the agent name instead of "{agent} Interface".
4. **Given** Settings forms and the Computer controls sheet, **When** rendered, **Then** field captions and help sentences are not drawn; labels stand alone; permissions and workspaces are grouped under headings.
5. **Given** the native console contract, **When** the server omits a label it used to require, **Then** native clients still decode the console; the contract makes those fields optional.

---

### User Story 7 - Qualified rollout to the sandbox and this Mac (Priority: P1)

As the owner, the server and the Mac build that implement this feature are deployed together, verified live, and recorded, with a rollback path.

**Why this priority**: The owner's instruction is to push updates directly to the sandbox and make the Mac client work. Code that is not deployed and verified does not count.

**Independent Test**: The sandbox runs the candidate image with the existing data intact; the installed Mac build is the signed candidate; each story's independent test passes against production; the qualification record names the image, the build and the results; the previous image and backup are restorable.

**Acceptance Scenarios**:

1. **Given** the candidate tree, **When** the image is built and deployed, **Then** the deployment takes a paired backup first, cuts over, verifies health, and can roll back to the saved image and override without data loss.
2. **Given** the signed candidate Mac build, **When** installed, **Then** it is notarized, keeps the registered identity and designated requirement, and connects to the sandbox.
3. **Given** the live pair, **When** each story's independent test runs, **Then** the results, denials and failures are recorded in `qualification.md` with timestamps.

### Edge Cases

- Approval for a task grant arrives after the target application was replaced by another: the grant is bound to the application and is refused.
- The Mac is locked, the screen sleeps or the socket drops mid-command: the running job is killed, the result is uncertain and no retry happens.
- A command writes megabytes to stdout: output is a bounded tail with a truncation flag; the full stream is never sent.
- The browser pane's page keeps a dialog open: snapshot reports the pending dialog instead of timing out.
- The appcast is reachable but the GitHub asset is not: Sparkle reports the download error and keeps the installed version.
- The minimum-version policy is malformed or its signature fails: the app treats it as absent, connects, and logs the failure to the status indicator.
- A user stored a custom theme with only some colors: unset roles fall back to Graphite, not Midnight.
- A server-owned string is cut but a client test fixture still expects it: the fixture is updated in the same change; no client is left decoding a missing required field.

## Requirements *(mandatory)*

### Functional Requirements

Approval and control (US1)

- **FR-001**: The server MUST offer a task grant on input-action review cards, bound to owner, host, session, chat, task and application or window, with a sliding expiry and a hard cap, revoked on pause, Stop, takeover, authority loss, host replacement or application change. Terminal input, file writes and deletes, destructive commands and consequential browser actions MUST stay on exact review.
- **FR-002**: When the controller is the host being controlled, the host MUST classify the pointer activity caused by acting on an approval card as approval activity, not takeover, for a bounded window after the card is shown.
- **FR-003**: Pointer-action approval cards MUST include a cropped, marked screenshot and the window title.
- **FR-004**: The Mac workbench MUST send the existing turn cancellation on Esc and Command-period, and Stop MUST cancel the turn as well as the session.
- **FR-005**: Secure-input state MUST refuse keyboard input only; takeover MUST require a click, key, scroll or movement beyond a threshold; a local Pause MUST refuse remote Resume; selected folders MUST be reported to the agent; input results MUST carry a fresh screenshot.
- **FR-006**: The workbench MUST show tool calls inline and in a non-blocking Activity panel.

Commands (US2)

- **FR-007**: The host MUST implement `run_command`, `read_output` and `interrupt_command` as declared in `contracts/local-exec.md`: exact command text or argument vector, working directory inside a selected folder, bounded timeout, sanitized environment, process-group termination, bounded output tails, exit code and duration.
- **FR-008**: Every command MUST pass the source-bound approval path with a card that shows the Mac, command, working directory, timeout and environment additions, and MAY be covered by a task grant keyed on program and folder except for the destructive list.
- **FR-009**: The scanner MUST clear the command tools only for the exact in-process implementation, following the existing terminal host review pattern.

Browser pane (US3)

- **FR-010**: The host MUST implement the ten browser verbs in `contracts/browser-verbs.md` inside the existing computer request envelope, advertise them only when the pane is enabled, and run its snapshot script in an isolated content world.
- **FR-011**: Navigation MUST be limited to http and https, approved once per origin per session, and blocked for downloads, uploads and popups in this version.
- **FR-012**: Password, payment and one-time-code fields MUST be refused and redacted; consequential controls MUST return confirmation required and proceed only after source-bound approval.
- **FR-013**: Browser observations MUST be tagged untrusted for the model and MUST never carry a model digest that bypasses spotlighting.

Updates (US4)

- **FR-014**: The Mac app MUST embed Sparkle 2.10.1, read the feed at `https://astraldeep.github.io/appcast.xml`, verify updates with the embedded EdDSA key before extraction, and expose Check for Updates in the app menu and update settings in Settings.
- **FR-015**: The app MUST postpone relaunch while a computer, browser or command task is active and show that state.
- **FR-016**: The app MUST fetch the signed minimum-version policy at launch, block connection when its build is below the minimum, and connect normally when the policy is absent or unverifiable.
- **FR-017**: A single local release command MUST produce the notarized, stapled, signed archive, the appcast, the Downloads record and the GitHub release, as described in `contracts/update-feed.md`; the website build MUST verify appcast and Downloads consistency.

Theme (US5)

- **FR-018**: The Graphite preset in `contracts/theme-graphite.md` MUST exist in every preset table, be the server default, be the Mac baked default, and be sent as effective colors to clients with no stored theme.
- **FR-019**: Status colors on the Mac MUST be desaturated for Graphite and remain distinguishable; the banner bar MUST carry an icon.

Copy (US6)

- **FR-020**: Every string marked CUT in `contracts/copy-cuts.md` MUST be removed at its source; the three layout fixes MUST land with the cuts; the native console contract MUST accept omitted optional labels; tests MUST be updated in the same change.

Rollout (US7)

- **FR-021**: Deployment MUST take a paired backup, cut over, verify health and keep a rollback; the Mac build MUST be notarized and keep its identity; live results MUST be recorded.

### Key Entities

- **TaskGrant**: owner, host, session, chat, task, scope (application bundle or window), allowed verb classes, program and folder for commands, created, last used, sliding expiry, hard cap, revocation reason.
- **LocalCommandJob**: job id, command, working directory, environment additions, timeout, start, process group, stdout and stderr rings with offsets, exit code, state (running, exited, timed out, interrupted, uncertain).
- **OriginApproval**: session, origin, approved at, approved by controller.
- **BrowserSnapshot**: snapshot id, URL, title, frame list, reference table, truncation.
- **UpdatePolicy**: minimum build, serial, expiry, signature.
- **ThemePreset**: name plus the seven server roles; Apple adds surface2, border and four status roles.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A five-step desktop task on the same Mac completes with at most one approval card where it needed five or more and failed today.
- **SC-002**: A command's exit code and output reach the agent without a screenshot, in a single tool call for short commands.
- **SC-003**: The browser fixture completes in the pane with zero pixel-coordinate actions.
- **SC-004**: An update from the first updater build to the next completes with at most two user clicks and never restarts during a task.
- **SC-005**: Every client with no stored theme renders Graphite; users with a stored theme see no change.
- **SC-006**: The forbidden-copy test lists every cut string and passes; no tagline or helper sentence is visible on the Mac shell, composer, canvas, Settings, Theme or Computer controls surfaces.
- **SC-007**: The sandbox runs the candidate image with existing data intact, and every story's independent test is recorded as passed against it.

## Assumptions

- The owner is present for actions that need their identity: SSH and sudo to the sandbox, Keycloak sign-in in the Mac app, macOS permission prompts, and the go-ahead to publish a public release.
- The Developer ID Application identity for team `593LGH35GS` and the `astral-notary` keychain profile are available on this Mac.
- Local tests are run and must pass for changed code; hosted CI is not consulted.
- Windows, Android, iOS and watchOS receive no behavior changes in this feature. Their theme preset tables gain Graphite as data, the shared Apple palette default changes, and server-owned defaults and copy cuts reach them through server-owned data, which the owner has accepted.

## Out of Scope

- Driving the user's own Chrome or Safari through a DOM protocol; a Chrome extension.
- The Mac App Store lane, App Store export options and store screenshots for macOS.
- Hosted CI repairs and release-evidence bootstrapping.
- iOS or watchOS behavior changes beyond compiling.
- A durable, cross-session command allowlist; grants are task-scoped in this version.

## Constitution deviations (owner-authorized 2026-10-25)

- Principle VII (arbitrary shell execution system-scoped and default-deny): bent. Command execution on the owner's own Mac is owner-scoped, default-off, exact-approval gated, task-grantable except for the destructive list, and fully audited. It is not system-scoped and not hard-security-flagged.
- Principle XII (protocol changes land on every in-scope client in the same feature): bent for the new host verbs. Windows and Android hosts do not advertise them; the divergence is recorded here. Theme default and copy cuts reach every client as server-owned data.
- Principle XI (CI budgets and gates): hosted CI is not a gate for this feature. Local tests still cover golden paths, denials and failures at 90% changed-line coverage.
- Principle XIV (component first, then pin): kept. Projection changes land in the Projection branch; Deep moves its pin to the exact Projection commit it deploys.
