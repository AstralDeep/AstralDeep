# Feature Specification: Canvas-First Adaptive UI/UX

**Feature Branch**: `066-canvas-first-uiux`

**Created**: 2026-08-03

**Status**: Draft

**Input**: User description: "Canvas-first adaptive UI/UX for the web client (and native-client consistency). Rework the web client so the generated-UI canvas is unmistakably the primary surface on every device class, with the text chat present only when the user is composing or the system is speaking. The web client must adapt to desktops, tablets, phones and unusual window shapes: ROTE must receive a live client-capability envelope reported at registration AND re-reported when it changes, so server-side adaptation never goes stale. Voice controls must always be visible with honest state. Sends must never vanish silently. Failed turns must never blank the canvas. Keyless custom OpenAI-compatible endpoints must be configurable end to end. After web verification, align the Android and Windows clients and produce an Apple-handoff document."

**Grounding**: Every requirement below traces to a defect or gap observed live on 2026-08-03 against `main@253378d` (local stack, mock auth, UK LLM factory): 21 cataloged findings including two blocking product bugs — (a) every first message of a new chat failed at the conversation-operation identity fence, and (b) keyless custom LLM endpoints were unusable because the runtime always sent a placeholder bearer token that strict servers reject. Working-tree fixes for both exist and this feature formalizes them with tests.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Canvas is the star on every screen (Priority: P1)

As a user on any device — desktop, tablet, phone, or an oddly-shaped window — the generated interface (canvas) occupies the overwhelming majority of the screen. The text conversation appears only where and when it earns its place: while I compose, and when the system has something to say. On a wide screen the chat lives in a collapsible rail/overlay that I can dismiss entirely; on a phone it stays behind a compact "Messages" control. There is no window width at which the app looks broken or the composer becomes unusable.

**Why this priority**: This is the product's core identity — server-driven generative UI. Today the desktop rail permanently consumes 380px, the 600–1024px range crushes the composer input to ~6 visible characters, and the canvas never gets the full width.

**Independent Test**: Load a chat with a rich multi-component canvas; sweep window width from 320px to 2560px; at every width the canvas is the dominant surface, the composer input remains usable, and chat visibility follows the collapse rules.

**Acceptance Scenarios**:

1. **Given** a desktop-width window with a populated canvas, **When** the user collapses the chat surface, **Then** the canvas expands to use the freed width and the composer remains reachable.
2. **Given** a window between 600px and 1024px wide, **When** the user types in the composer, **Then** the input field shows at least 20 characters without scrolling and no control is clipped.
3. **Given** a phone-width window, **When** a turn completes, **Then** the canvas remains the primary surface and new conversation text is reachable through the Messages control (with an unread indicator) without covering the entire canvas.
4. **Given** any width, **When** the assistant produces conversation text, **Then** the chat surface makes itself noticed (opens, peeks, or badges — per layout mode) without permanently stealing canvas space.

---

### User Story 2 - Adaptation never goes stale (Priority: P1)

As a user who resizes windows, rotates devices, docks/undocks, or grants microphone permission mid-session, the system's understanding of my device tracks reality. The client reports a capability envelope — viewport size, pixel ratio, touch/pointer, microphone availability and permission state, audio output, geolocation availability, connection type, reduced-motion preference — at registration and re-reports whenever it changes, so server-side component adaptation and future renders match the surface I'm actually looking at.

**Why this priority**: The server currently receives capabilities exactly once per connection; a phone-sized window resized from desktop keeps desktop-adapted content (and vice versa) until reconnect. Live re-adaptation exists server-side but the web client never feeds it.

**Independent Test**: Register at desktop size, resize to phone width, trigger a re-render; the server-side device profile observed in logs/output reflects the new viewport without a reconnect.

**Acceptance Scenarios**:

1. **Given** a session registered at 1920×1080, **When** the window is resized below tablet width and settles, **Then** the server receives an updated capability envelope within 2 seconds of the resize settling.
2. **Given** a session where microphone permission changes from undetermined to granted, **Then** the server-visible envelope reflects the new permission state without reload.
3. **Given** rapid continuous resizing, **Then** capability re-reports are debounced (no more than one report per settle) and never destabilize the connection.
4. **Given** a capability change while disconnected, **Then** the next registration carries the current envelope.

---

### User Story 3 - The composer never lies and never loses my words (Priority: P1)

As a user, the composer is trustworthy: voice controls are always present with real icons and honest states (start when available; visibly disabled with a plain-language reason when not; permission prompts when needed) even before any server voice frame arrives. The connection state is visible. If I type and send while the app is connecting or disconnected, my message is either queued with a visible indicator or refused loudly — it never silently vanishes. Text typed before registration completes is preserved.

**Why this priority**: Observed live: voice controls render as a near-invisible text stub ("Mic") and disappear entirely if the server never sends a composer frame; three consecutive saves during a dead socket vanished with zero feedback; a message typed during page load was consumed and dropped.

**Independent Test**: Load the page with voice unavailable — a recognizable disabled voice button with a reason is present. Sever the socket, click send — the message visibly queues or the send is refused with feedback; on reconnect the queued message sends or remains editable.

**Acceptance Scenarios**:

1. **Given** no composer/voice frame has arrived, **Then** a voice start control renders disabled with an honest reason, using a real icon consistent with the other composer controls.
2. **Given** the socket is disconnected, **When** the user submits a message, **Then** the message stays visible (input or queued bubble) with a "reconnecting" indicator, and is sent or explicitly surrendered to the user on reconnect — never dropped.
3. **Given** the page just loaded and registration is in flight, **When** the user types and presses Enter, **Then** the text is preserved and dispatched after registration completes.
4. **Given** voice becomes available (worker admitted), **Then** the voice control enables without reload and its state changes are announced accessibly.

---

### User Story 4 - Honest, legible turn lifecycle (Priority: P2)

As a user I always know what the system is doing and what happened. Turn progress shows phases near the composer (routing, running tools, composing answer) instead of a distant bare "Working…". A failed turn keeps my message visible, explains the failure inline, offers retry, and never blanks the canvas back to an empty state. Assistant text renders markdown properly. Per-turn boilerplate (provenance/sample-data notes) is presented as unobtrusive metadata, not as separate assistant messages. New chats get real generated titles.

**Why this priority**: Observed live: a failed turn erased the user's bubble, blanked the canvas to the empty state, and whispered "The operation could not be completed." in the topbar; bubbles show literal `**asterisks**`; every turn appends a boilerplate bubble; every chat is titled "New Chat".

**Independent Test**: Force a turn failure (LLM unreachable); the user message remains with an inline error and working retry; the previous canvas stays. Run a normal turn; phases appear near the composer; the final bubble renders markdown; no boilerplate-only bubble; the chat gains a generated title.

**Acceptance Scenarios**:

1. **Given** a turn in progress, **Then** the status is visible adjacent to the conversation surface and communicates at least three distinct phases over the turn's life.
2. **Given** a turn that fails server-side, **Then** the user's message remains in the transcript, an inline failure notice with retry appears, the canvas retains its prior content, and retry re-runs the same message.
3. **Given** an assistant reply containing markdown, **Then** the transcript renders it (bold, lists, code) rather than showing markup characters — with the same sanitization guarantees as canvas content.
4. **Given** a completed first turn in a new chat, **Then** the chat appears in Recent chats with a generated title (not "New Chat") within one turn.
5. **Given** a turn whose reply includes provenance/sample-data boilerplate, **Then** that text appears as metadata attached to the reply or canvas — not as an additional standalone assistant bubble.

---

### User Story 5 - Calm canvas chrome (Priority: P2)

As a user reading a rich canvas, component furniture stays out of the way: provenance and per-component actions (refine, history, share, export) reveal on hover/focus/long-press instead of stamping a visible action row under every component; page-level actions (export/share) sit in stable chrome that never overlaps content while scrolling; a brand-new chat greets me with the same welcome examples as a fresh session instead of a bare void.

**Why this priority**: Observed live: an 8-widget dashboard renders 8 always-on action rows plus footers (even under a one-line caption); the floating Export/Share buttons overlap table columns and chart legends while scrolling; "New chat" lands on an empty canvas with no examples.

**Independent Test**: Render the dashboard example; at rest, no per-component action rows are visible until pointer/focus lands on a component; scrolling never overlaps page actions with content; clicking New chat shows welcome examples.

**Acceptance Scenarios**:

1. **Given** a canvas with N components at rest, **Then** zero always-on per-component action rows are visible, and every action remains reachable via hover/focus (pointer) or an explicit affordance (touch), including keyboard access.
2. **Given** any scroll position, **Then** page-level actions never visually overlap component content.
3. **Given** the user opens a new chat, **Then** the welcome examples canvas appears (same purge-on-first-send rules as today).
4. **Given** a component with provenance, **Then** provenance remains discoverable (single unobtrusive marker) and its meaning is explained on interaction.

---

### User Story 6 - Keyless custom endpoints work end to end (Priority: P2)

As a user whose OpenAI-compatible endpoint requires no API key (self-hosted vLLM/sglang, a campus LLM factory, a LAN gateway), I can select "Custom", enter only the endpoint URL and model, test the connection, and save — and every later call (chat turns, probes, background work) succeeds because the system sends no Authorization header when no key is stored. The mandatory first-run dialog keeps its contract: it stays non-dismissible until a provider is saved, preserves my typed values across failed probes, and shows busy states during test/save.

**Why this priority**: Observed live: custom required a key at save/test; the runtime's placeholder bearer was rejected (HTTP 403) by the operator's own documented factory endpoint; a failed probe re-rendered the mandatory dialog as a closable settings dialog and wiped the typed key. Working-tree fixes exist (key-optional custom; auth-header omission) and must be formalized with tests.

**Independent Test**: Against a keyless endpoint that rejects arbitrary bearer tokens: complete first-run setup with an empty key (test passes, save persists), run a chat turn successfully; against a key-requiring endpoint, an empty-key save is refused by the probe with an honest error.

**Acceptance Scenarios**:

1. **Given** provider "Custom" with a base URL and empty key, **When** the user tests the connection against a keyless endpoint, **Then** the probe succeeds and reports latency/model.
2. **Given** a stored keyless custom config, **When** any turn or background call runs, **Then** no Authorization header is sent; **and** a stored real key is always sent intact.
3. **Given** the mandatory first-run dialog and a failed probe, **Then** the dialog remains non-dismissible, typed values (including the key) survive, the error is announced accessibly, and Test/Save show busy states while in flight.
4. **Given** an empty-key save against an endpoint that requires a key, **Then** the save is refused with the endpoint's authentication error surfaced.

---

### User Story 7 - First message of a new chat always works (Priority: P1)

As a user, my very first message in a fresh session or new chat — typed or via a welcome example — starts a turn that completes like any other. The durable operation admitted before the conversation existed adopts the chat the turn creates; identity protections against cross-conversation contamination remain fully strict everywhere else.

**Why this priority**: Observed live: 100% of first-messages-of-new-chats failed at the conversation-operation identity fence (operation admitted with no chat identity vs. the chat created mid-turn), which also poisoned subsequent client state. This has silently broken the web first-contact experience since the fence shipped. The working-tree fix (durable chat binding at the single legitimate None→created transition) must be regression-tested at every fence.

**Independent Test**: From a fresh session, click a welcome example — the turn completes with canvas content; repeat with a typed message in a New chat; verify a cross-conversation identity mismatch still refuses publication.

**Acceptance Scenarios**:

1. **Given** a fresh session with no chats, **When** the user sends their first message, **Then** the turn completes and publishes to the newly created chat.
2. **Given** an operation admitted for chat A, **When** publication targets chat B, **Then** the turn is refused (strictness preserved).
3. **Given** a failed first turn, **Then** the client returns to a sane state: the next chat open or send works without reload.

---

### User Story 9 - Voice conversations actually start, locally and in production (Priority: P1)

As a user clicking the microphone button on any deployment where the voice stack is configured, a voice session starts; when it cannot, the composer tells me exactly why in words — never a silent failure backed by an HTTP 503. As the operator, I can diagnose "voice is down" in one step: the system names how many workers are admitted, the last admission-refusal reason, and the speech-endpoint preflight verdict.

**Why this priority**: Live on the production sandbox: clicking the mic yields `POST /api/voice/sessions → 503` with no admitted worker, while all four containers show healthy. Diagnosed contributing causes so far: the speech endpoint only began serving both speech models (and their audio routes) after the last worker restart window; restart ordering placed the worker before the orchestrator; and admission refusals are only visible by grepping two containers' logs. The composer meanwhile shows a disabled chip with no reason surfaced near the click. (Local verification with the same code and a valid speech key is part of this feature; the factory's ASR + TTS routes are confirmed live as of 2026-08-03.)

**Independent Test**: On a locally composed stack (orchestrator + LiveKit + voice worker against the real speech endpoint), click the mic — the session establishes and the state machine advances past `connecting`; kill the worker — the composer degrades to an honest reason within one refresh; restart the orchestrator while the worker stays up — the worker re-registers within its backoff and voice recovers without operator action.

**Acceptance Scenarios**:

1. **Given** a correctly configured deployment, **When** the user clicks voice-start, **Then** a session establishes and the composer advances through the documented states (connecting → greeting/listening).
2. **Given** no admitted worker, **When** the user clicks voice-start, **Then** the composer surfaces the refusal reason in words within 2 seconds — the raw failure is never the only signal.
3. **Given** the orchestrator restarts while a worker is running, **Then** the worker re-registers within its reconnect backoff and voice availability returns without container restarts; registration refusals are logged with their exact reason on both sides.
4. **Given** an operator investigating "voice is down", **Then** a single authenticated status surface (or one documented command) reports: admitted worker count, each worker's identity and session load, the last admission refusal with reason, and the last speech-preflight verdict.
5. **Given** the speech endpoint recovers (models/routes appear) after the worker container started, **Then** the worker reaches readiness without manual restarts (bounded preflight re-checks), or the deployment docs prescribe the restart with rationale.

---

### User Story 8 - Native clients stay consistent; Apple pass is prepared (Priority: P3)

As a user moving between web, Windows, and Android, the canvas-first principles read the same: canvas dominant, conversation on demand, honest composer states. Where the native clients already embody this (Android StackedShell), nothing regresses; where they diverge, they are aligned. A handoff document with annotated final screenshots (web desktop/tablet/phone, Windows, Android) and a parity checklist enables the iOS/macOS/watchOS pass to be completed on a Mac without re-deriving decisions.

**Why this priority**: Consistency is required by the cross-client SDUI architecture, but the web is the reference implementation and must land first.

**Independent Test**: Run the parity checklist against Windows and Android builds; every checked item matches the web reference or has a documented, justified divergence; the handoff document exists in the feature directory with the screenshot set.

**Acceptance Scenarios**:

1. **Given** the aligned Windows client, **When** a rich turn completes, **Then** canvas/conversation arrangement and composer honesty match the parity checklist.
2. **Given** the Android client, **When** the same turn runs, **Then** its stacked arrangement passes the same checklist rows.
3. **Given** the handoff document, **Then** it contains the final screenshot set, the capability-envelope contract, layout-mode rules, and the open items for Apple.

---

### Edge Cases

- Window narrower than 320px or shorter than 400px: layout stays functional (no clipped composer), even if canvas density degrades.
- Ultra-wide (≥2560px): canvas content remains readable (bounded content width) rather than stretching lines absurdly.
- Resize mid-turn: skeletons/streams reflow without losing content; capability re-report does not cancel the turn.
- Permission revoked mid-voice-session: composer returns to an honest disabled state with reason; typed chat unaffected.
- Reconnect mid-turn: turn results still land (existing resume machinery) and the connection indicator reflects the gap; queued sends do not duplicate.
- Reduced-motion users: no attention-grabbing animation for chat reveal; badges/inline cues instead.
- Keyboard-only and screen-reader users: collapse/expand, component actions, and voice controls are reachable and announced; no hover-only functionality without a focus/keyboard equivalent.
- Multiple tabs on the same chat: collapse state is per-tab; transcripts and canvases stay consistent via existing fan-out.
- A component action row on touch devices (no hover): actions reachable via an explicit per-component affordance.
- Chat titles for one-word or non-text first messages: fall back to a truncated message or timestamp — never permanently "New Chat" after a completed turn.

## Requirements *(mandatory)*

### Functional Requirements

**Layout & canvas primacy**

- **FR-001**: The web client MUST provide three layout modes — expanded (chat rail visible), collapsed (canvas full-width with compact composer access), and stacked (small widths) — with the canvas as the dominant surface in all three.
- **FR-002**: The user MUST be able to collapse and expand the chat surface explicitly; the choice persists per device across reloads.
- **FR-003**: The chat surface MUST auto-reveal (open, peek, or badge — without permanently reclaiming space) when new assistant conversation text arrives while hidden, and MUST NOT auto-reveal for non-conversational status updates.
- **FR-004**: At every viewport width from 320px to 2560px, the composer input MUST accommodate at least 20 visible characters and no composer control may be clipped or overlapped.
- **FR-005**: The stacked arrangement MUST retain the collapsible Messages panel with unread count; opening it MUST NOT fully obscure the canvas on heights ≥ 600px (sheet/dim treatment).
- **FR-006**: Canvas content width MUST be bounded on very wide windows to preserve readability.

**Live capability envelope**

- **FR-007**: The client MUST report a capability envelope at registration containing at minimum: viewport width/height, screen width/height, pixel ratio, touch/pointer type, microphone availability, microphone permission state, audio output availability, geolocation availability, connection type, and reduced-motion preference.
- **FR-008**: The client MUST re-report the envelope when it materially changes (resize/orientation settle, permission state change, connection type change), debounced to at most one report per settle, and the server MUST update the session's device profile without reconnection.
- **FR-009**: Server-side adaptation for subsequent renders in the session MUST use the freshest envelope; the client's layout mode and the server's device profile MUST agree on the device class boundaries.
- **FR-010**: Envelope changes while disconnected MUST be reflected in the next registration.

**Composer honesty**

- **FR-011**: Voice controls MUST render from client-local default state before any server frame arrives: a visible voice-start control, disabled with an honest reason until the server confirms availability; server frames refine but never remove the affordance.
- **FR-012**: Voice controls MUST use real iconography consistent with the composer's other controls, with accessible names, pressed/busy states, and a text status line that never relies on color alone.
- **FR-013**: The client MUST display connection state (connected / reconnecting / offline) whenever it is not healthily connected.
- **FR-014**: A send attempted while unregistered or disconnected MUST either queue visibly (and dispatch exactly once on reconnect) or refuse with visible feedback; it MUST NOT be silently dropped. Input typed before registration MUST be preserved.
- **FR-015**: Chrome/settings actions invoked while disconnected MUST surface the same queue-or-refuse behavior (no silent drops).

**Turn lifecycle**

- **FR-016**: Turn status MUST be presented adjacent to the conversation/composer surface and expose phase transitions (at minimum: routing, tool activity with tool identity, composing answer).
- **FR-017**: A failed turn MUST keep the user's message in the transcript, present an inline failure with a retry affordance that re-sends the same content, and MUST NOT clear or blank existing canvas content.
- **FR-018**: The transcript MUST render assistant markdown (bold, italics, lists, links, inline code) with sanitization equivalent to canvas text rendering; chat previews MUST strip markup.
- **FR-019**: Per-turn boilerplate (provenance lines, sample-data disclaimers) MUST NOT be delivered as standalone assistant bubbles; it MUST attach as reply/canvas metadata.
- **FR-020**: A chat whose first turn completed MUST receive a generated title; on title-generation failure, a deterministic fallback (truncated first message) applies. "New Chat" MUST NOT persist past a completed turn.
- **FR-021**: After any turn failure, the client's request state MUST recover such that subsequent chat opens and sends work without a reload (no stuck open-request wedges).

**Canvas chrome**

- **FR-022**: Per-component actions (refine, history, share, export) and provenance MUST be hidden at rest and revealed on hover/focus (pointer) or an explicit compact affordance (touch/keyboard); every action remains keyboard-accessible.
- **FR-023**: Page-level canvas actions (export/share) MUST live in stable chrome that never overlaps component content at any scroll position.
- **FR-024**: Opening a new chat MUST present the welcome examples canvas (subject to the existing purge-on-first-send and never-persist rules); provenance markers MUST NOT label static welcome content as AI-generated.

**LLM configuration**

- **FR-025**: The provider catalog MUST treat "Custom" as key-optional; test/load-models/save MUST accept an empty key for key-optional providers, and the probe-gated save MUST refuse configurations the endpoint actually rejects.
- **FR-026**: When no key is stored, runtime and probe calls MUST omit the Authorization header entirely; when a key is stored it MUST be sent intact. This applies to user turns, probes, and background/system LLM work.
- **FR-027**: The mandatory first-run dialog MUST remain non-dismissible until a provider is saved — including across failed probes — MUST preserve typed field values across re-renders, MUST show busy state on in-flight Test/Save, and MUST announce results accessibly.

**New-chat operation binding**

- **FR-028**: An operation admitted before its conversation existed MUST durably adopt the chat its turn creates at that single transition; all conversation-identity fences retain strict semantics for every other case (differing non-null identities always refuse).
- **FR-029**: The regression MUST be pinned by tests: first-message-of-new-chat succeeds end to end (typed and welcome-example paths); cross-conversation mismatch still refuses; the binding is idempotent and race-safe.

**Voice session reliability & diagnosability**

- **FR-033**: A voice-start attempt MUST either establish a session or surface the precise refusal reason in the composer (worker unavailable, speech service unavailable, permission, capacity) — an HTTP-level failure MUST never be the only user-visible signal.
- **FR-034**: The orchestrator MUST provide an operator-readable voice readiness surface (authenticated status endpoint and/or structured log line) naming: admitted workers (identity, session load), the most recent admission refusal and its reason, and the most recent speech-preflight verdict per worker.
- **FR-035**: Worker admission MUST be robust to restart ordering: after an orchestrator restart, a running worker MUST re-register within its existing reconnect backoff; a registration refusal MUST be logged with its exact reason on both orchestrator and worker sides.
- **FR-036**: A worker whose speech-endpoint preflight failed MUST re-check on a bounded interval so that speech-service recovery (models/routes appearing) restores voice availability without container restarts; each re-check verdict is logged.
- **FR-037**: The local development compose MUST bring up the full voice path (LiveKit + worker against a real speech endpoint) verifiably, and the deployment documentation MUST record the production topology contract: reverse-proxy voice host rules, the environment contract including worker closure digest and coordinator replica identity, and the diagnosis runbook.

**Native consistency & handoff**

- **FR-030**: The Windows and Android clients MUST pass the parity checklist for canvas-first behavior (canvas dominance, conversation on demand, honest composer states, welcome-on-new-chat) or document justified divergence per item.
- **FR-031**: The feature MUST produce an Apple-handoff document containing: final annotated screenshots (web desktop/tablet/phone, Windows, Android), the capability-envelope contract, layout-mode rules, the parity checklist with web/Windows/Android status, and the open items for the iOS/macOS/watchOS pass.

**Non-regression**

- **FR-032**: The 055 first-turn loading contract (skeleton to first content, no welcome-blanking frame), workspace identities, component refine/history, export endpoints, accessibility landmarks, and the voice session flows MUST NOT regress.

### Key Entities

- **Capability Envelope**: The client-reported device truth — viewport/screen dimensions, pixel ratio, input modalities, microphone availability + permission, audio output, geolocation availability, connection type, reduced-motion. Versioned per session; freshest wins.
- **Layout Mode**: The client-side arrangement state (expanded / collapsed / stacked) derived from viewport + user preference; persisted per device; independent from but consistent with the server device profile.
- **Turn Status**: The user-visible lifecycle of one message — phases, terminal state (done / failed with reason + retry), located with the conversation.
- **Component Chrome**: The furniture around a canvas component — provenance marker + revealed action set — distinct from component content.
- **Connection State**: Client's socket/registration health (connected / reconnecting / offline) with queued-send holding area.
- **Parity Checklist**: Per-behavior rows (layout, composer, lifecycle, chrome) × clients (web reference, Windows, Android, Apple-pending) with pass/divergence status.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: At every viewport width in {320, 375, 500, 600, 768, 800, 1024, 1280, 1920, 2560}px, the canvas occupies ≥ 70% of window width when the chat is collapsed/stacked-closed, and the composer input shows ≥ 20 characters.
- **SC-002**: 100% of first-messages-of-new-chats (typed and welcome-example) complete successfully in the verification harness; cross-conversation identity violations still refuse in 100% of attempts.
- **SC-003**: Zero silent message loss across a scripted suite of ≥ 10 adverse sends (pre-registration, mid-disconnect, mid-reconnect): every attempt ends visibly sent, visibly queued, or visibly refused.
- **SC-004**: After a resize across a device-class boundary, the server's device profile reflects the new class within 2 seconds of settle, without reconnect, in 100% of trials.
- **SC-005**: A voice affordance is present in the composer in 100% of sessions — including sessions where no voice frame ever arrives — and its disabled state names a reason.
- **SC-006**: A keyless custom endpoint that rejects arbitrary bearer tokens completes first-run setup and a chat turn with zero authentication errors; a wrong-key configuration still fails its probe with the endpoint's error surfaced.
- **SC-007**: On a canvas of ≥ 6 components at rest, visible chrome rows count is 0 (down from N), and no scroll position overlaps page actions with content (verified across the sweep in SC-001).
- **SC-008**: A turn failure leaves the user message visible with a working retry in 100% of failure drills, and canvas content from before the turn remains rendered.
- **SC-009**: 100% of chats with a completed first turn display a non-default title within that turn's completion.
- **SC-010**: Backend, web (browser), Windows, and Android test suites pass in CI posture; changed-line coverage meets the repository gate (≥ 90%).
- **SC-011**: On a correctly configured local stack, a mic click establishes a voice session end-to-end (state machine passes `connecting`) in ≥ 9 of 10 attempts; on a stack with no admitted worker, the composer names the reason within 2 seconds in 10 of 10 attempts.
- **SC-012**: An operator can determine why voice is unavailable (worker count, last refusal reason, last preflight verdict) from one status surface or one documented command, without correlating multiple containers' raw logs.

## Assumptions

- The server-driven UI architecture is unchanged: primitives are defined centrally, rendered by the orchestrator, adapted per device — this feature restyles the web shell/chrome and the client-side arrangement, not the component vocabulary.
- The adaptive designer continues to own component arrangement inside the canvas; fixing its full-width stacking bias (KPI tiles each consuming a full row) is in scope only as far as passing device-fit hints from the fresh capability envelope; deeper designer scoring changes are out of scope.
- Voice availability depends on the deployed voice worker; this feature covers the composer's honesty about availability, not worker deployment. (Observed: the factory endpoint now serves both speech models, so the sandbox voice outage is expected to clear independently.)
- The artifact-sharing flag posture is operator-controlled and unchanged by this feature.
- Android/Windows alignment is consistency work (checklist-driven), not a native redesign; the Apple pass happens later on a Mac using the handoff document.
- Real-Keycloak flows are unaffected: all changes ride the existing session/registration machinery.
- The two working-tree bug fixes (operation chat binding; keyless auth-header omission) are part of this feature's scope and must land with regression tests.
