# Feature Specification: Remote Computer Control — Drive My Own PC From My Phone

**Feature Branch**: `076-remote-computer-control` (AstralDeep + AstralProjection)

**Created**: 2026-09-02

**Status**: Draft — implementation in progress on the feature branches

**Input**: User description: "Add a way for a remote connection to be established between say an iPhone running the AstralDeep app and a Windows computer currently running the AstralDeep Windows client app. I want to be able to send tasks to the Windows machine as if I were sitting at the computer — exactly like how Claude and ChatGPT do it." Owner amendment (2026-09-02): "Since you are on a Windows computer, do Android → Windows as the test instead of iPhone; the iPhone implementation follows on a Mac."

## Why now (verified problem statement)

A code audit of `main` at `AstralDeep@e04a353` / `AstralProjection@95f81af` (2026-09-02) established the following against primary sources.

1. **AstralDeep can reach a remote SSH host (feature 063) but cannot touch the user's own desktop.** The 063 agent runs a *fixed, closed* SSH verb set against machines the user registers by address and credential (`backend/orchestrator/remote_transport.py`). A Windows PC running the desktop client is behind NAT, has no SSH server, and is *already authenticated* to the orchestrator — yet nothing in the product can ask it to do anything except host a BYO agent bundle.

2. **The transport for this already exists and is proven live.** The Windows client keeps an authenticated UI WebSocket open and relays JSON frames to/from local processes over it: `ui_event action=agent_tunnel` inbound (`components/AstralProjection/windows-client/win_agent/byo_host.py:2104-2113`) and a bare `{"type":"agent_tunnel",…}` push outbound (`backend/shared/local_transport.py:78-83`), owner-bound by the socket's session `sub` (`backend/orchestrator/orchestrator.py:1914-1915`), rate-capped per owner (`:1975-1988`) and honest-offline on disconnect (`:1946-1969`). Feature 057/058 proved it end to end on a signed-in Windows host (2026-07-14).

3. **The client has no way to see or act on its own screen.** A repo-wide search of `windows-client/` finds no `QScreen.grabWindow`, `SendInput`, `mouse_event`, `keybd_event`, `pyautogui`, `mss` or `Pillow`; the only capture is the test harness's `QWidget.grab()` of the client's own window (`windows-client/tests/screenshot.py:71-72`). The client-hosted tools listener (`win_agent/tools.py:835-963`: `read_file`, `write_file`, `run_command`, `open_path`, clipboard, toast) is **disabled in the shipping profile** (`deployment/release-profile.json`, `legacy_tools.disposition = "disabled"`) and is an *inbound* listener the orchestrator must dial — unreachable through NAT and structurally the wrong direction.

4. **The model cannot see images.** Every tool result becomes a string before it reaches the model (`orchestrator.py:17569-17599` `_tool_result_to_llm_content` returns `str`; `:16008-16022` appends it as a `role:"tool"` message). There is no `image_url`/content-part assembly anywhere in `backend/`; the existing `read_image` tool ships base64 *as JSON text*. A look-then-act loop is impossible without a multimodal message path.

5. **The confirmation gate is hard-wired to one agent.** The 063 destructive-confirmation mechanism (`backend/orchestrator/remote_confirmation.py`) is the only proven propose → approve-on-a-button → resume path on every client, but it is keyed on the literal `agent_id == "remote-compute-1"` (`remote_confirmation.py:40`, `orchestrator.py:18305`, `:18334`). A second machine-control agent gets no gate.

6. **Per-machine addressing is missing from the tunnel registry.** `TunnelSocket`s are keyed `(owner_sub, agent_id)` (`orchestrator.py:1208`); `register_ui` carries a random `device_id`, ROTE capabilities and `agent_host.{host_id, platform, client_version}` — no machine name, no per-machine capability list (`backend/shared/protocol.py:2872-2981`, `:2640-2701`). A user with two PCs cannot say which one.

7. **Every client already renders what the phone side needs.** The `image` primitive exists (`components/AstralPrimitives/src/astralprims/primitives.py:178-186`); the web renderer allowlists `data:image/…` (`webrender/renderer.py:45-58`), the Windows renderer decodes `data:` into a `QPixmap` (`windows-client/astral_client/renderer.py:1313-1341`), Android renders via Coil (`Media.kt`) and iOS via `AsyncImage` (`ComponentView.swift:422-440`). `ui_upsert` replaces a component in place by id on every client (`AppModel.swift:1579-1590`, `Canvas.kt`, `client.js:5464-5490`). The 063 "Remote machines" surface renders on all four clients with zero per-client code (`src/astralprojection/chrome/workspace.py:70-182`).

## Owner Decisions (2026-09-02)

| # | Question | Decision |
|---|---|---|
| D1 | Which model drives the look-then-act loop | **The owner's configured BYO model handles images.** Screenshots go to the provider as image parts; if the provider rejects them the loop degrades to the non-visual mode (window/keyboard/shell verbs) and says so. |
| D2 | Consent and approvals | **Pre-consent on the PC + confirm consequential actions on the phone.** The Windows client has an explicit "Allow remote control" toggle; sessions are started from the phone by the same signed-in owner; input verbs run freely inside a session; `run_command`, file writes/deletes, and anything the model flags as consequential (purchase, send, credentials) produce an approval card on the phone. The PC shows a persistent banner with Stop; local human input pauses the session. |
| D3 | Live test rig | **Android emulator on the owner's Windows box** (debug build targets `10.0.2.2:8001`) driving the Windows client on the same machine; the iPhone client follows on a Mac. |
| D4 | Delivery | Branches + PRs per repo; CI gates are not a merge precondition for this pass; the composition pin in AstralDeep is still repinned honestly. |
| D5 | New dependencies | **None.** Capture and input injection use Qt (`QScreen`) and stdlib `ctypes` (`user32.SendInput`, `SetCursorPos`, `GetLastInputInfo`); JPEG encoding uses Qt's bundled image plugin. Backend uses stdlib only. |
| D6 | Feature flag | `FF_COMPUTER_USE`, **default off**, read once at import like every 0xx flag; the owner's development `.env` enables it. Flag-off is byte-identical (pinned by test). |
| D7 | Durable inventory | **Deferred.** v1 lists a computer while its client is connected (plus an in-process last-seen cache). A durable `computer_host` Plane record is a follow-up; approvals reuse the 063 `remote_operation_proposals` store. |
| D8 | Shell commands (live rig, 2026-09-02) | **No arbitrary-shell verb.** The tool-security analyzer hard-blocks any `run_command`-shaped tool (Constitution VII), so the model was never offered it and reached for a terminal window instead. Commands run the way a person runs them — in a terminal — and *typing into a terminal* is the approval-gated step: the host refuses keystrokes into a command interpreter unless the session carries the owner's approval (`confirm_action`, or approving `open_app` of a shell, unlocks it for 3 minutes). |

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Turn my PC into something my phone can drive (Priority: P1)

On the Windows client, the user opens Settings → **Remote control**, reads what it means, and switches on **Allow my other devices to control this computer**. The client immediately announces itself to the orchestrator as a *computer host* with its name, platform, screens and the verbs it supports. On any other signed-in client the user opens Settings → **My computers** and sees that PC listed as online. Switching the toggle off makes the PC disappear from every other client within seconds and ends any running session.

**Why this priority**: consent is the security boundary; nothing else may exist before it. It also proves the registration path, presence, and the surface on every client in one slice.

**Independent Test**: enable on the PC → the PC appears on a second client's "My computers" within 5 s; disable → it disappears within 5 s and a running session (US2) ends with reason `consent_revoked`; a second user never sees it.

**Acceptance Scenarios**:

1. **Given** the toggle is off, **When** any client asks to control the PC, **Then** the request is refused with `computer_unavailable` and nothing is executed.
2. **Given** the toggle is on, **When** the client reconnects after a network drop, **Then** the PC re-announces itself without user action and any session that was active is reported `ended: host_offline` on the phone, never left hanging.
3. **Given** the toggle is on, **When** the orchestrator has `FF_COMPUTER_USE` off, **Then** the client's announcement is ignored, no menu item appears anywhere, and no frame or component type of this feature is observable on the wire (flag-off byte-identity).

### User Story 2 - Send a task from my phone and watch it happen (Priority: P1)

From the phone the user opens **My computers**, taps **Control this computer**, and gets a chat bound to that PC. They type a task ("open Excel and put last month's numbers in a table"). The assistant takes a screenshot, decides, clicks/types, takes another screenshot, and so on; the phone shows the latest screenshot updating in place plus the assistant's narration. The PC shows a banner "AstralDeep: *Sam's phone* is controlling this computer — Pause · Stop". When the task is done the assistant says so and the session stays open until the user ends it or it times out.

**Why this priority**: this is the feature. The look-then-act loop, the live view, and the PC-side indicator are the user-visible core.

**Independent Test**: with the emulator + Windows client rig: start a session, ask for a visible action (open Notepad and type a sentence), observe the screenshot on the phone update after each step, observe the text appear on the PC, observe the banner on the PC for the session's whole life.

**Acceptance Scenarios**:

1. **Given** an active session, **When** the assistant calls `screenshot`, **Then** the phone's canvas shows the new image within 2 s and the model receives it as an image part (not JSON text).
2. **Given** the model's provider rejects image parts, **When** the first screenshot is sent, **Then** the turn continues in non-visual mode with a one-line notice and no crash, and later turns do not retry images until the session is restarted.
3. **Given** an active session, **When** the assistant calls an input verb (`click`, `type_text`, `press_keys`, `scroll`, `drag`, `move`), **Then** it executes without a confirmation card, and every call is an `agent_tool_call` audit row correlated by `session_id`.
4. **Given** an active session, **When** nothing has happened for the idle limit (default 20 min) or the hard cap (default 2 h), **Then** the session ends with `ended: idle_timeout`/`ended: max_duration` on both sides.
5. **Given** the model narrates while acting, **When** a step fails (window not found, click outside screen), **Then** the failure is a typed result the model can recover from, never an exception into the chat.

### User Story 3 - Consequential actions stop and wait for me (Priority: P1)

The assistant reaches a step that would run a shell command, write or delete a file, or that the model itself judges consequential (buying, sending, signing in). The phone shows a card describing exactly what would happen with **Approve** and **Decline**. Nothing executes until the user taps Approve; a tap on Decline or 15 minutes of silence discards the proposal.

**Why this priority**: D2. The feature assembles the lethal trifecta (private desktop + untrusted screen content + consequential action) and containment ships with it.

**Independent Test**: ask for a task that needs `run_command` → card appears, nothing runs; Approve → it runs once; Approve again on the same card → refused as already consumed; let a card expire → refused as expired.

**Acceptance Scenarios**:

1. **Given** a machine-class (scheduled/unattended) turn, **When** any mutating verb is reached, **Then** it is refused `unattended_refused` before any frame reaches the PC.
2. **Given** an approval card, **When** a *different* signed-in user replays the decision action with the proposal id, **Then** it is refused and audited.
3. **Given** the model calls `confirm_action(summary)` for a UI-level consequential step, **Then** the same card appears, the model ends its turn, and after approval the task resumes automatically with the outcome in hand.

### User Story 4 - Whoever is at the PC always wins (Priority: P2)

Someone sits at the PC while a session is active. The moment they move the mouse or type, the session pauses: injected input stops, the banner says "Paused — someone is using this computer", the phone shows the same. The person at the PC can click **Stop** on the banner or turn the toggle off; the phone user can **Resume** when the PC is idle again.

**Why this priority**: the local human's control is the last-resort safety property and the reason the visible indicator exists.

**Independent Test**: during an active session move the mouse on the PC → next verb returns `paused: local_input`; click Stop on the banner → phone shows `ended: local_stop`.

### User Story 5 - Non-visual operator mode (Priority: P2)

With a text-only model, the user can still ask the PC to open an application, focus a window by title, type text, press a key chord, run an approved command, read a file, or check the clipboard — the assistant works from `list_windows` and typed results instead of screenshots. Screenshots are still shown to the *user* on the phone.

**Independent Test**: with images disabled for the session, "open Notepad and type hello" completes using `open_app` + `list_windows` + `type_text`.

### User Story 6 - Turning it off is as reliable as turning it on (Priority: P3)

`FF_COMPUTER_USE=0` removes the agent, the menu item, the surfaces, the frames and the additive `register_ui` field from observable behaviour; a Windows client with the toggle on connecting to such a server sees "not enabled on this deployment" and nothing else.

### Edge Cases

- Two PCs online for one owner: every verb takes a `computer` argument (id or name); an ambiguous name is refused with the candidates listed.
- Two phones for one owner: a session has one *controller* device; a second device sees the session as "controlled by *other device*" and may only end it.
- The PC's screen is locked or the session desktop is not interactive: `screenshot` returns `screen_locked`; input verbs refuse until unlocked.
- Multi-monitor: `screenshot` defaults to the primary screen; `screens` lists them; coordinates are per screenshot (the host maps back through the scale and screen it reported).
- Huge frames: screenshots are downscaled to ≤1280 px wide JPEG; `ui_upsert` carries the data URI; the workspace keeps one component per session (replaced in place), so persistence stays O(1) per session.
- The orchestrator restarts mid-session: sessions are in-process; the host receives no `computer_session` frame, its banner watchdog ends the session after 90 s without a heartbeat; the phone reconnects to an ended session.
- Model context growth: only the most recent N (default 3) screenshots stay as images in the model's messages; older ones are replaced by a text placeholder.

## Requirements *(mandatory)*

### Functional Requirements

**Consent & registration**
- **FR-001** The Windows client MUST expose a persistent "Allow remote control" setting (default off) with plain-language explanation; the setting MUST survive restarts and MUST be revocable at any time from the client.
- **FR-002** With the setting on, the client MUST announce itself in `register_ui` via an additive `computer_host` object carrying a stable `host_id`, a human name, platform, client version, screens `[{index, width, height, scale, primary}]`, and its supported verb list. The server MUST derive the owner from the socket's session, never from the frame.
- **FR-003** The orchestrator MUST keep an owner-scoped registry keyed `(owner_sub, host_id)`; a reconnect MUST supersede the stale socket; a disconnect MUST end any active session for that host and notify the owner's other sockets.
- **FR-004** A host announcement on a server with the flag off MUST be ignored silently; nothing of this feature MUST be observable with the flag off (menu, surfaces, agent, frames).

**Sessions**
- **FR-005** A session MUST be created only by an interactive, signed-in owner action (a `chrome_computer_session_start` event or the `start_session` tool from a live human turn); machine-class turns MUST be refused.
- **FR-006** A session binds `(owner, host_id, controller device_id, chat_id)`, has an idle limit and a hard cap (configurable via env, defaults 20 min / 2 h), and states `active | paused | ended(reason)`.
- **FR-007** The host MUST show a visible, always-on-top indicator for the whole life of a session with Pause/Resume and Stop, and MUST end the session when the client exits or consent is revoked.
- **FR-008** Local human input on the host MUST pause the session (no injected input while paused); resume is an explicit phone action or a local action.
- **FR-009** Every session transition MUST be audited (`agent_lifecycle`, action types `computer_session.*`, correlation id = session id) and pushed to the owner's sockets as a `computer_session` frame.

**Verbs (see contracts/verbs.md)**
- **FR-010** The agent `computer-use-1` MUST expose a fixed, closed verb set: observe (`list_computers`, `screenshot`, `list_windows`, `get_clipboard`, `read_file`, `list_dir`, `wait`), input (`click`, `double_click`, `right_click`, `move`, `drag`, `scroll`, `type_text`, `press_keys`, `focus_window`, `open_app`, `set_clipboard`), consequential (`write_file`, `delete_path`; `open_app` of a shell), session (`start_session`, `end_session`, `resume_session`, `confirm_action`). There is no arbitrary-shell verb (D8).
- **FR-011** Observe verbs MUST be `tools:read` and MUST work in a session without confirmation; input verbs `tools:write` require an active (not paused) session; consequential verbs (`tools:files`, and `open_app` of a shell) MUST go through the 063 proposal gate on every reach; the approval card is the call's result (canvas + transcript, replaced in place on decision) and the model is told to end its turn; after approval the verb runs and a continuation turn resumes the task.
- **FR-012** No verb accepts a shell fragment. Keystrokes into a command interpreter (the foreground window's process is a terminal) MUST be refused by the host unless the request carries the owner's approval, which the orchestrator grants for a bounded time after an approved `confirm_action` or an approved shell `open_app` (D8).
- **FR-013** Coordinates are in the coordinate space of the most recent screenshot the host produced for that session; the host MUST map them to physical pixels itself and refuse out-of-range values.
- **FR-014** The host MUST enforce the verb list it announced; an unknown verb yields a typed `unsupported` result.

**Vision**
- **FR-015** When a tool result carries images (`_images: [{media_type, base64, caption}]`), the orchestrator MUST append them to the model's messages as image content parts following the tool message; only the most recent N screenshots stay as images.
- **FR-016** If the provider rejects the multimodal request, the orchestrator MUST retry the call text-only once, mark the session `images_unsupported`, notify the user once, and not retry images for that session.

**Surface & clients**
- **FR-017** A "My computers" surface (`projection_surfaces/my_computers.py` + pure builder in AstralProjection) MUST list the owner's hosts with presence, screens and session state, with actions: Control this computer, Pause/Resume, Stop, Forget. It MUST render on web (`render()`) and natively (`components()`) with zero per-client code.
- **FR-018** The live view MUST be an `image` component with a stable per-session id upserted in place; clients that cannot render `data:` images MUST show the alt text (labeled degrade), never nothing.
- **FR-019** New frames/actions/fields MUST be added to `contracts/ui_protocol.json` and every client's drift table in the same feature (Constitution XII).

**Security & audit**
- **FR-020** All verbs are dispatched through the full gate stack (`_run_gate_stack`); tunnel-style host frames MUST never carry delegation tokens or per-user secrets to the host.
- **FR-021** Host results MUST be registered as untrusted taint sources (screen text can carry injections) and screenshots MUST be presented to the model with a spotlighting caption.
- **FR-022** Host ingress MUST be rate-capped per owner (reuse the tunnel cap) and payloads size-capped (default 4 MiB); over-cap frames are dropped and counted.
- **FR-023** The agent MUST be safe-seeded (D2: observe verbs work out of the box) but consequential verbs MUST remain gated regardless of safe marking.

### Key Entities

- **ComputerHost** (in-process): owner_sub, host_id, name, platform, client_version, screens, verbs, socket, last_seen, consent_mode.
- **ComputerSession** (in-process + audit): session_id, owner_sub, host_id, controller_device_id, chat_id, state, reason, created_at, last_activity_at, images_supported, last_screenshot {scale, screen_index, width, height}.
- **RemoteOperationProposal** (reused 063 record): machine_id = host_id, agent_id = `computer-use-1`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001** Enable-to-visible on another client ≤ 5 s; disable-to-gone ≤ 5 s (measured on the rig).
- **SC-002** Screenshot round trip (verb call → image on phone) ≤ 2 s at 1280 px wide JPEG on LAN.
- **SC-003** Zero executions without an active session, zero consequential executions without a consumed proposal (adversarial suite).
- **SC-004** Flag-off byte-identity pinned by test; every client drift guard green for the manifest change.
- **SC-005** The Android → Windows demo task ("open Notepad and type a sentence") completes end to end on the rig with the owner's model.
- **SC-006** Zero new third-party runtime dependencies in either repo.

## Assumptions

- The Windows client runs in the interactive desktop session of the user who is signed in to AstralDeep (no service/LSA context, no UAC-elevated targets: injected input cannot reach elevated windows and this is documented as a limitation).
- The BYO provider is OpenAI-compatible and accepts `image_url` content parts (D1); Anthropic-style `image` blocks are out of scope for v1.
- The Android debug build can sign in against the local orchestrator with the owner's Keycloak realm from the emulator.

## Dependencies

- 057/058 tunnel transport and host-capable sockets; 063 proposal store and confirmation card; 040 in-process agents and safe seeding; 042/043 native chrome surfaces; astralprims `image`.

## Out of Scope

- macOS/Linux hosts (the host executor is Windows-only in v1; the protocol is host-neutral).
- Durable host inventory (D7), file transfer between phone and PC, audio, remote *viewing* of the PC by a second human, and any per-application allowlist (a follow-up candidate mirroring Cowork's app-scoped grants).
