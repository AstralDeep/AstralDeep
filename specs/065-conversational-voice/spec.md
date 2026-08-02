# Feature Specification: Conversational Voice Interface Across All Clients

**Feature Branch**: `065-conversational-voice`

**Created**: 2026-07-31

**Status**: Implementation and local verification in progress; merge/release gates remain open

**Input**: User description: "Add a natural conversational interface to every AstralDeep client. Use the platform-provided `Systran/faster-whisper-large-v3` ASR model and `speaches-ai/Kokoro-82M-v1.0-ONNX` TTS model with the `af_heart` voice through the deployment's existing speech endpoint and credentials. Users must not configure a speech URL or key. A control on the chat input bar starts a voice conversation; recognized speech enters the normal agentic query path; the assistant acknowledges every new query, gives natural truthful spoken feedback at least once every 20 seconds while work continues, and speaks a concise summary when the result finishes. Use the LiveKit project, support all clients, and verify end to end on the live Docker deployment and available simulators, pausing when user login is required."

## Verified Baseline and Owner Decisions

This section records constraints already established by the request and facts verified against the live tree on 2026-07-31. It does not prescribe the detailed implementation plan.

### Verified baseline

- AstralDeep already exposes authenticated batch transcription and synthesis endpoints plus a realtime transcription proxy in `backend/orchestrator/api.py`, and already has a structured voice renderer in `backend/webrender/voice.py`. These are incomplete foundations, not a working cross-client conversational experience.
- The current web composer contains attachment, background-run, text-entry, and send controls but no voice control (`backend/webrender/templates/shell.html`). Windows and the shared iOS/macOS composer also have no conversational voice control. Android and watchOS offer platform dictation, but neither uses the requested platform ASR/TTS path or provides an automatic agentic conversation.
- The legacy voice proxy uses a different configuration contract, does not authenticate its realtime socket as a Keycloak user, does not send the supplied provider credential upstream, and defaults to a stale TTS model identifier. It cannot be treated as production-ready merely because its basic mocked tests pass.
- The normal chat path already owns authenticated message submission, authorization, delegated agent/tool execution, confirmation gates, PHI controls, audit provenance, durable conversation history, progress events, background-task continuity, and committed final results. Voice must enter and leave through that path rather than create a second agentic runtime.
- The linked [LiveKit server repository](https://github.com/livekit/livekit) provides the self-hostable realtime media server, rooms, transport, and authentication substrate. LiveKit's separate [Agents framework](https://github.com/livekit/agents) and platform SDKs were evaluated as possible voice-pipeline, turn-taking, interruption, and client components. The subsequent approved architecture rejects LiveKit Agents and uses the official server plus pinned direct-RTC/client components; an upstream server fork is not expected unless planning proves a specific blocking gap.
- Official LiveKit documentation states that its model interfaces can target custom OpenAI-compatible STT/TTS endpoints. The official [Kokoro integration guide](https://docs.livekit.io/agents/models/tts/kokoro/) and [OpenAI plugin reference](https://docs.livekit.io/reference/python/livekit/plugins/openai/) cover the required class of endpoint. Exact compatibility remains a pinned-version contract test, not an assumption.
- The named `.env` entries were verified as present without reading or recording either secret value. No secret value belongs in this specification, Git, client state, logs, telemetry, or audit payloads.

### Owner decisions captured from the request

| ID | Decision |
|---|---|
| D-001 | Feature number `065` is reserved for this work. Feature `064` has separate ownership and must not be touched. |
| D-002 | “All clients” means all six shipping surfaces: web, Windows, Android, macOS, iOS, and watchOS. No watch-only system-speech carve-out satisfies this feature. |
| D-003 | Conversation mode is an interruptible, turn-based voice session with automatic end-of-turn detection and automatic submission of a final non-empty transcript. It is not hold-to-record dictation and does not require a second Send action. |
| D-004 | Only one client may actively capture and speak for a user at a time. Starting on another client requires an explicit takeover and ends the earlier media session without cancelling its already-submitted agent work. |
| D-005 | Raw audio is ephemeral and is not retained. The final transcript becomes the ordinary user chat message; the authoritative assistant result remains the ordinary committed text/structured result. |
| D-006 | The launch speech profile is fixed to ASR `Systran/faster-whisper-large-v3`, TTS `speaches-ai/Kokoro-82M-v1.0-ONNX`, `af_heart`, and the service's advertised 24 kHz output. There is no end-user voice, model, endpoint, or key picker in this feature. |
| D-007 | The deployment's existing `OPENAI_BASE_URL` and `OPENAI_API_KEY` values are the operator-provided inputs for the speech service only. This does not revive environment-backed LLM configuration: those values must remain incapable of configuring or replacing a user's LLM or the System LLM. |
| D-008 | The request's final “ASR summary” step is interpreted as a **TTS** summary: the completed request's authoritative text summary is used when present; otherwise, a short faithful recap is generated from the committed content shown to the user. Speech never becomes the authoritative result. |
| D-009 | Voice media uses a pinned, self-hosted LiveKit deployment and the supported direct-RTC/client components needed for the six clients; LiveKit Agents is not embedded in the media worker. Modifying upstream LiveKit server source is permitted only if a documented, tested gap cannot be solved through supported configuration or extension points. |
| D-010 | Live verification uses the running Docker deployment, a real browser, the desktop client, Android emulator/device, and Apple simulators/devices available on the Mac. Work pauses at the real login boundary for the user. A Mac-only run cannot substitute for later Windows-native packaging/audio evidence. |

## Clarifications

### Session 2026-07-31

- Q: When a completed result contains PHI or similarly sensitive content, what may TTS say aloud? → A: Speak only a generic sensitive-result notice, then require an explicit tap or spoken request before reading the sensitive details aloud.
- Q: When the user speaks another query while the current agentic turn is still running, what should happen? → A: Move the current turn to durable background execution and make the newly accepted query the foreground turn without waiting for the earlier turn to finish.
- Q: If the user changes the visible chat while conversation mode remains active, where should their next spoken turn go? → A: Follow the newly visible chat for future turns while keeping existing turns bound to their originating chats.
- Q: How long may conversation mode remain listening with no user speech or interaction before ending automatically? → A: End after five continuous minutes of idle listening.
- Q: How should AstralDeep create the concise completion recap sent to TTS? → A: Use the completed request's authoritative text summary when present; otherwise create a short faithful recap from the committed content shown on screen.

### Session 2026-08-02

- Q: How should clients surface a spoken request that does not start or complete? → A: Show a persistent, visually prominent, non-color, assertively announced text notice at the conversation controls that says whether the request did not start or did not complete, preserves the bounded safe server explanation and recovery action, and leaves typed chat usable. A TTS/playout failure after a committed result is a distinct notice that the text result remains available. Unrelated session churn must not erase a terminal request notice; a newer turn or explicit voice reset/end may clear it.

## Scope

### In scope

- A consistent, accessible conversation control at the primary chat composer on web, Windows, Android, macOS, and iOS, plus the equivalent primary chat action on watchOS.
- A foreground, interruptible voice session that greets the user, listens for a turn, shows what was heard, submits it automatically, speaks lifecycle feedback, and returns to listening after a terminal result.
- Reuse of the normal authenticated agentic dispatch and committed conversation record for every recognized query.
- Platform-provided speech service configuration, LiveKit media transport, exact launch models/voice, availability checks, isolation, graceful degradation, and operational observability.
- Truthful immediate acknowledgements, bounded spoken progress at intervals no longer than 20 seconds, concise spoken completion/failure summaries, interruption, cancellation, reconnection, and cross-device takeover semantics.
- Privacy, PHI, authorization, audit, permission, egress, and credential controls appropriate to clinical voice data.
- Protocol, capability, parity, drift-guard, packaging, permission-manifest, automated-test, and live-verification work for all six client surfaces.

### Out of scope

- Replacing AstralDeep's existing LLM selection, agent registry, delegation, tool authorization, confirmation, audit, or conversation-history mechanisms.
- Direct speech-to-speech model execution that bypasses a text transcript and the ordinary agentic dispatch path.
- User-provided speech endpoints, speech API keys, voice selection, voice cloning, custom voices, or per-agent voices.
- Telephone/SIP calling, video avatars, multi-user conference calls, recorded calls, downloadable raw audio, or offline speech.
- Continuous microphone capture while the app is backgrounded or the device is locked. Already-submitted work may continue under the existing background-task rules, but voice capture and unsolicited playback pause.
- Treating persistent `Audio` UI primitives, platform dictation, or on-device text-to-speech as completion of the conversational media channel.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Start a natural voice conversation (Priority: P1)

A signed-in user selects the conversation control beside the chat input. After any first-use operating-system permission prompt, the assistant gives a short greeting in the `af_heart` voice, clearly indicates that it is listening, and accepts an ordinary spoken turn without requiring the user to hold a button or press Send. The same recognizable control, states, and behavior are available on every shipping client, adapted to its form factor.

**Why this priority**: Without a trustworthy start/stop experience and real media path, none of the later agentic or progress behavior is usable. The explicit activation authorizes microphone capture and ordinary playback and supplies the browser's audio-playback gesture, but it does not by itself authorize sensitive result details to be read aloud.

**Independent Test**: On each client, start from voice-off, activate the control, grant microphone access, hear one short greeting, observe a visible and accessible listening state, speak one sentence, and see a single final transcript. Repeat with permission denied and with no microphone to verify a clear non-recording state and usable text fallback.

**Acceptance Scenarios**:

1. **Given** a signed-in user on any shipping client with voice available, **When** the user activates conversation mode, **Then** the control changes through connecting to listening, the assistant gives one concise greeting, and microphone capture begins only after the explicit action and required permission.
2. **Given** conversation mode is inactive, **When** ambient speech occurs, **Then** AstralDeep captures and sends no audio and plays no unsolicited assistant speech.
3. **Given** microphone permission is undecided, **When** the user activates conversation mode, **Then** the client requests the platform permission once through the normal operating-system flow and accurately reflects the resulting authorized, denied, restricted, or unavailable state.
4. **Given** permission is denied, restricted, or no microphone exists, **When** activation finishes, **Then** no session is represented as listening, the user receives a specific recovery message, and typed chat remains fully usable.
5. **Given** the assistant is greeting or speaking, **When** the active client accepts explicit stop or the client/worker accepts barge-in, **Then** explicit stop fences local playout synchronously before network acknowledgement, the stale speech epoch is fenced, and acoustic silence meets SC-007's p95 500-millisecond and 100% 1-second limits before the client transitions to listening without replay.
6. **Given** conversation mode is active, **When** the user selects its control again, **Then** capture and playback stop, the control returns to off, and already-submitted agent work continues unless the user separately invokes the normal cancellation action.
7. **Given** a user already has an active voice session on another client, **When** they request activation on this client, **Then** they see which device owns the session and must explicitly take it over; successful takeover ends media on the earlier client without duplicating or cancelling submitted work.
8. **Given** conversation mode is listening with no active turn or user-input gate, **When** five continuous minutes pass without user speech or interaction, **Then** the session ends capture and playback, releases its media ownership, and tells the user it ended for inactivity; processing and waiting periods do not count toward this timeout.

---

### User Story 2 - Speak into the normal agentic system (Priority: P1)

The user speaks a request and pauses naturally. AstralDeep determines the end of the turn, shows the final recognized text as the user's message, and submits exactly one ordinary chat turn. The same selected LLM, agents, tools, scopes, confirmation requirements, PHI policies, audit provenance, progress trail, persistence, and workspace behavior apply as if the identical text had been typed.

**Why this priority**: The key value is not transcription by itself; it is a conversational entrance to the full AstralDeep system without a parallel voice agent that weakens authorization or loses history.

**Independent Test**: Submit the same request once by typing and once by speaking. Confirm that both reach the same chat action and agentic dispatch seam, use the same user/agent/tool authorization decisions, create one user message apiece, and produce equivalent persisted results. Exercise an allowed tool, a denied tool, a confirmation-gated operation, a PHI-sensitive request, and a background task.

**Acceptance Scenarios**:

1. **Given** a final non-empty transcript, **When** end-of-turn is detected, **Then** exactly one authenticated, correlated normal chat message is submitted with that transcript and current chat context.
2. **Given** partial transcripts, silence, background noise, or an empty final transcript, **When** recognition ends, **Then** no user message or agent run is created until a valid final transcript exists.
3. **Given** a voice-originated turn, **When** it reaches agent dispatch, **Then** every existing identity, owner-isolation, delegation, permission, policy, PHI, egress, confirmation, retry, cancellation, and audit gate remains in force.
4. **Given** a request that requires explicit user confirmation, additional permission, or login, **When** the gate is reached, **Then** voice mode describes what is needed but does not synthesize, infer, or bypass approval; only the existing server-validated confirmation mechanism can authorize the operation.
5. **Given** a reconnect, media retry, duplicated final-transcript event, or client resume, **When** the same recognized turn is observed again, **Then** it is deduplicated by stable user/chat/turn identity and never dispatches twice.
6. **Given** the user's own LLM is not configured or becomes unavailable, **When** a recognized request is submitted, **Then** the existing LLM setup/unavailability behavior remains authoritative; platform speech credentials are not used as an LLM fallback.
7. **Given** a voice-originated request that becomes a background task, **When** the user leaves the chat or stops voice mode, **Then** the task follows existing durable background semantics and its text result remains available without keeping the microphone active.

---

### User Story 3 - Hear acknowledgement, honest progress, and a concise result (Priority: P1)

After AstralDeep accepts each spoken query, the assistant says “On it!” or a similarly short natural acknowledgement within the SC-002 latency budget. If work continues, it gives concise, varied, truthful reassurance often enough that no processing interval exceeds 20 seconds without audible feedback. When a non-sensitive committed result arrives, it stops progress speech and speaks the request's authoritative text summary when one is present; otherwise it generates a short, faithful recap from the committed content displayed to the user. For PHI or similarly sensitive results, it announces only that a sensitive result is ready and waits for an explicit tap or spoken request before reading details aloud. Errors, refusals, cancellations, and actions waiting on the user receive equally honest terminal or waiting messages.

**Why this priority**: Agentic tasks can take minutes. Silence makes the system appear broken, while fabricated or overlapping progress damages trust. A bounded, state-backed speech contract is central to the requested experience.

**Independent Test**: Run controlled turns lasting 2 seconds, 19 seconds, 21 seconds, 65 seconds, and several minutes. Capture timestamped audio lifecycle events and verify the immediate acknowledgement, the maximum 20-second in-flight gap, varied/non-fabricated messages, clean terminal transition, and no progress audio after completion, failure, cancellation, mute, disconnect, or a user-input gate.

**Acceptance Scenarios**:

1. **Given** a recognized query has been durably accepted, **When** its agentic turn begins, **Then** the assistant speaks one acknowledgement such as “On it!”, “I’m on it,” or “Let me take care of that,” meeting SC-002's per-client p95 of 1.5 seconds and exactly-once rule.
2. **Given** a turn is still actively processing, **When** 20 seconds would otherwise pass since the end of the last acknowledgement, progress message, or other truthful assistant speech for that turn, **Then** another concise natural progress message begins before that gap is exceeded.
3. **Given** real sanitized lifecycle information is available, **When** a progress message is chosen, **Then** it may describe the broad user-facing phase; otherwise it uses a neutral acknowledgement and never invents a completed step, result, elapsed promise, tool outcome, or certainty.
4. **Given** consecutive progress announcements in one session, **When** the wording is selected, **Then** the exact same phrase is not repeated back-to-back and the variation does not imply unverified progress.
5. **Given** a turn completes while progress audio is queued or playing, **When** the committed result arrives, **Then** stale progress is cancelled, no progress starts afterward, and the final summary is serialized without overlapping another assistant utterance.
6. **Given** a successful non-sensitive committed result, **When** it becomes authoritative, **Then** the spoken recap uses the request's authoritative text summary when present or otherwise generates a short faithful recap only from the committed content shown to the user; it meets SC-004's per-client p95 of 2 seconds and does not replace or mutate the full visible result.
7. **Given** a successful result containing PHI or similarly sensitive content, **When** it becomes authoritative, **Then** the assistant speaks only a generic notice that a sensitive result is ready and waits for a fresh explicit tap or spoken request before reading any sensitive detail aloud.
8. **Given** a failure, refusal, or cancellation, **When** the turn becomes terminal, **Then** the assistant speaks an honest concise outcome and does not phrase it as success.
9. **Given** a turn is waiting for login, permission, approval, or another user action, **When** that state begins, **Then** the assistant explains the required action once and suspends repetitive 20-second processing reassurance until work actually resumes.
10. **Given** the user mutes speech while leaving conversation mode active, **When** work continues or finishes, **Then** visible progress and results continue normally but no audio plays until the user explicitly unmutes; muted announcements are not replayed in a burst.
11. **Given** generated acknowledgement or progress text, **When** the turn later continues, **Then** that ephemeral speech has not been inserted into the authoritative conversation transcript or model context as an assistant answer.
12. **Given** a spoken request is refused, cancelled, abandoned, or fails, **When** that outcome reaches a client, **Then** the client presents a persistent, visually prominent, non-color, assertively announced notice distinguishing “did not start” from “did not complete,” includes the bounded safe explanation and recovery action, and keeps typed input usable; a spoken-playback failure after a committed result instead says that the text result remains available and MUST NOT claim that the request failed.

---

### User Story 4 - Interrupt, recover, and continue without crosstalk (Priority: P2)

The user can naturally interrupt greeting, progress, or final-summary speech with a new utterance. AstralDeep stops the old playback and recognizes the new turn. If an earlier agentic turn is still running, it continues under the existing durable background-task rules while the newly accepted query becomes the foreground turn without waiting for the earlier turn to finish; neither request is confused with or allowed to cancel the other. Conversation mode follows the currently visible chat for each future turn, while already-submitted turns and their announcements remain bound to their originating chats. Media-network changes that recover within the current authenticated session lease do so without duplicate turns or replayed summaries.

**Why this priority**: Turn-taking, interruption, echo control, and reconnection distinguish a conversation from dictation plus audio playback. They are also common sources of duplicated consequential work.

**Independent Test**: Interrupt every speech type from speaker and headset routes, switch networks during listening and during synthesis, background and foreground a native client, reconnect after a committed result, and take over from another device. Verify stable turn ownership, no feedback-loop transcript, no duplicate dispatch, and no stale playback.

**Acceptance Scenarios**:

1. **Given** the assistant is speaking, **When** the active client accepts explicit stop or the client/worker accepts the user's barge-in event, **Then** explicit stop fences local playout synchronously before network acknowledgement, acoustic silence meets SC-007's p95 500-millisecond and 100% 1-second limits, captured speech excludes the assistant's own output to the practical limit of platform echo control, and the new final transcript is correlated to a new turn.
2. **Given** an earlier agent task is still running, **When** the user's next transcript is durably accepted, **Then** the earlier turn transitions to durable background execution and the new query becomes the foreground turn in the same accepted-turn transition without waiting for the earlier turn to finish; neither is cancelled, and each turn's progress and final speech remains correlated, attributable, and serialized.
3. **Given** conversation mode is active in one chat, **When** the user makes another chat visible, **Then** the next spoken turn is submitted to the newly visible chat while every earlier turn, progress announcement, and result remains bound and attributed to its originating chat.
4. **Given** a network interruption, **When** media reconnects before the authenticated session lease expires—45 seconds in the launch profile and configurable only from 15 through 300 seconds—**Then** the session returns to an honest state without automatically resubmitting a transcript or replaying already-acknowledged/final speech; if that actual lease expires first, media ends and the client offers typed fallback plus fresh activation.
5. **Given** media cannot recover, **When** the failure becomes terminal, **Then** capture and playback stop, the user sees a specific text-only recovery path, and any already-submitted agent work continues under normal chat semantics.
6. **Given** a native app moves to the background or the device locks, **When** the client accepts that lifecycle event, **Then** it synchronously fences capture and unsolicited local playback before any network acknowledgement, and the resulting acoustic silence meets SC-007's p95 500-millisecond and 100% 1-second limits; on return the client states whether the task is still running or already completed before resuming speech.
7. **Given** the user explicitly says or selects the normal cancel action, **When** cancellation is accepted, **Then** the agent task follows existing best-effort cancellation semantics and voice progress stops; merely interrupting or disabling voice does not imply task cancellation.

---

### User Story 5 - Use one safe, included voice capability everywhere (Priority: P1)

Users never enter a speech-service URL, API key, model, or voice. When the operator-configured service and exact launch profile are healthy, every client offers conversation mode. When any required part is unavailable, the system fails closed and explains that typed chat remains available; it never silently substitutes a user LLM credential, another speech endpoint, another model, or an on-device voice.

**Why this priority**: “Included by default” is a product requirement, while server-held secrets, exact model selection, and honest health are security and supportability requirements.

**Independent Test**: Verify healthy startup with the exact advertised models and voice, then independently remove/deny the media server, ASR model, TTS model, voice, endpoint credential, and network route. Confirm consistent availability state on every client, no credential exposure, no silent fallback, and uninterrupted typed chat.

**Acceptance Scenarios**:

1. **Given** a normal end user, **When** they inspect onboarding and settings, **Then** no speech endpoint, speech API key, model, or voice setup is requested or exposed.
2. **Given** the platform speech profile is healthy, **When** capability is advertised, **Then** it reflects a real bounded readiness check for the exact ASR model, TTS model, and `af_heart` voice rather than mere presence of an environment variable.
3. **Given** any required speech/media dependency is missing or unhealthy, **When** a client receives capability state, **Then** conversation mode is visibly unavailable or ends safely with a specific reason, while typed chat remains available.
4. **Given** an upstream response or failure, **When** it is surfaced to the user, logs, metrics, or audit, **Then** provider credentials, raw upstream bodies, internal URLs, raw audio, and PHI-bearing transcripts are not disclosed.
5. **Given** two authenticated users or two chats, **When** both use voice concurrently, **Then** neither can hear, receive, address, take over, or infer the other's room, audio, transcript, announcement, or result.
6. **Given** self-hosted media access is issued to a client, **When** it connects, **Then** access is short-lived, user/chat/session scoped, and incapable of revealing or exercising the server API secret.

---

### User Story 6 - Experience equivalent controls and states on every client (Priority: P1)

A user moving among web, Windows, Android, macOS, iOS, and watchOS finds the same capability and lifecycle vocabulary. Layout adapts to screen size, but availability, activation, takeover, listening, processing, speaking, muted, waiting, unavailable, and error meanings do not drift. Screen-reader and keyboard/switch users can operate every state without relying only on color, animation, or sound.

**Why this priority**: Cross-client parity is a constitutional requirement and explicit user scope. Shipping one polished client with five partial copies would not deliver this feature.

**Independent Test**: Drive one shared conformance suite against all six clients, then exercise each live client against the same authenticated backend and real media/speech services. Compare action names, states, labels, turn IDs, timing evidence, permission denial, takeover, transcript submission, progress, terminal summary, and fallback behavior.

**Acceptance Scenarios**:

1. **Given** two similarly sized clients with equivalent device capabilities, **When** their chat composers render, **Then** the conversation action has equivalent placement, label, ordering, states, and functionality from one server-owned contract.
2. **Given** watchOS's smaller form factor, **When** chat controls render, **Then** the same server-owned conversation action and state machine are available through its primary chat affordance; system dictation/on-device speech alone does not satisfy parity.
3. **Given** a keyboard, screen reader, Switch Control, VoiceOver, TalkBack, or comparable assistive technology, **When** focus reaches the control, **Then** its accessible name, current on/off/busy/muted state, action, permission problem, and errors are perceivable and operable.
4. **Given** a device reports microphone capability, **When** permission is later denied or hardware becomes unavailable, **Then** the runtime authorization state overrides the static capability report and is reflected consistently.
5. **Given** a shared voice protocol or action changes, **When** drift guards run, **Then** the authoritative protocol manifest and all client dispositions/conformance tests agree; no client silently ignores a required voice frame.
6. **Given** implementation verification on the Mac reaches a real Keycloak sign-in screen, **When** credentials are required, **Then** automated interaction stops and requests the user to complete login before authenticated E2E evidence continues.

### Edge Cases

- The user activates voice but never speaks; after five continuous minutes of idle listening with no active turn or user-input gate, the session ends, releases media ownership, and submits no empty turn.
- Recognition returns partial text and then an error; partial text is shown as non-authoritative, is not submitted, and can be retried without duplication.
- Speech ends just as the 20-second progress deadline or final result arrives; one serialized truthful utterance wins, and stale queued speech is cancelled.
- A task completes while the app is muted, backgrounded, disconnected, or owned by another client; the text result persists, and old audio is not replayed automatically on resume or takeover.
- The user changes chats during an in-flight voice turn; future spoken turns follow the newly visible chat, while announcements and results for existing turns remain bound to their originating chat and are not spoken as if they belong to the new chat.
- Two background tasks finish together; completion summaries are queued, clearly attributed, bounded, and dismissible rather than overlapping.
- The ASR recognizes a destructive or sensitive request incorrectly; normal authorization/confirmation gates still prevent voice from becoming an approval bypass, and the visible transcript lets the user identify the error.
- A committed result contains PHI or similarly sensitive content while voice mode is active; only the generic readiness notice is automatic, and no sensitive detail is spoken until a fresh explicit tap or spoken request is accepted for that result.
- Input language is outside the launch voice's supported spoken locale; the transcript is preserved when recognition succeeds, but the client clearly communicates any spoken-output limitation and never silently switches away from `af_heart`.
- The TTS service fails after a query was accepted; the agent task and visible result continue, voice enters an honest degraded state, and retry cannot duplicate the task.
- The ASR service fails while TTS remains healthy, or vice versa; capability and recovery distinguish listening from speaking rather than presenting a generic healthy state.
- The media server is reachable but TURN/UDP/TCP media establishment fails; the user receives a bounded connection failure instead of an indefinite listening indicator.
- A client microphone captures assistant playback; echo control and turn rules prevent synthesized speech from being auto-submitted as a new user query.
- Provider cold start exceeds normal latency; the UI remains honest, the activation/query timeout is bounded, and no fake-ready state or silent model substitution occurs.
- A speech model or `af_heart` disappears from the provider's model inventory after deployment; readiness fails visibly and typed chat remains usable.
- The user logs out, loses authorization, deletes the chat, or the account session expires during voice use; media ends, scoped access becomes unusable, and no later audio/result crosses the expired identity boundary.
- A client or worker crashes with microphone state active; platform capture stops with process/session teardown, stale session ownership expires, and a later client can take over safely.

## Requirements *(mandatory)*

### Functional Requirements

#### Shared activation and session lifecycle

- **FR-001**: The system MUST expose one server-owned conversational-voice capability and action contract consumed by web, Windows, Android, macOS, iOS, and watchOS; clients MUST NOT independently invent names, ordering, state meanings, or availability rules.
- **FR-002**: Every full chat composer MUST place an accessible conversation control alongside the existing input actions, and watchOS MUST expose the equivalent action in its primary chat controls.
- **FR-003**: The shared state vocabulary MUST distinguish at minimum: off, unavailable, connecting, greeting, listening, speech detected, transcribing, acknowledging, processing, waiting on user, speaking progress, speaking result, muted, reconnecting, error, and ended.
- **FR-004**: Microphone capture and assistant playback MUST begin only after an explicit user activation; activation MUST request and honor the platform's runtime microphone authorization.
- **FR-005**: A client MUST communicate microphone capability and runtime authorization separately; static hardware capability MUST NOT be treated as proof that recording is permitted.
- **FR-006**: On successful activation, the assistant MUST give a short `af_heart` greeting and enter turn-based listening with automatic end-of-turn detection. A continuous listening period with no active turn, no user-input gate, and no user speech or interaction MUST end automatically after five minutes and release media ownership; processing and waiting periods MUST NOT consume this idle allowance.
- **FR-007**: The user MUST be able to stop capture, stop current speech, mute/unmute assistant speech, or end conversation mode at any time through visible and accessible controls.
- **FR-008**: The system MUST allow only one active media session per user and MUST provide an explicit, audited takeover flow between the user's clients.
- **FR-009**: Ending, muting, interrupting, or transferring a media session MUST NOT implicitly cancel already-submitted agent work; task cancellation MUST use the existing explicit cancellation contract. When a new spoken query is accepted while the current turn is still active, the current turn MUST transition to durable background execution and the new query MUST become the foreground turn in the same accepted-turn transition without waiting for the earlier turn to finish.
- **FR-010**: Conversation capture and unsolicited playback MUST pause when a client is backgrounded or locked; already-submitted work MAY continue under the existing background-task policy.

#### Recognition and ordinary agentic dispatch

- **FR-011**: Conversation mode MUST use ASR model `Systran/faster-whisper-large-v3` for final transcripts on every client; platform dictation MAY remain a separate text-entry aid but MUST NOT satisfy this requirement.
- **FR-012**: If a recognition backend emits partial text, clients MUST display it only as provisional; the launch batch adapter emits no invented partial text. Clients MUST display the final recognized text as the ordinary user message that is submitted.
- **FR-013**: A final non-empty recognized turn MUST auto-submit exactly once through the same authenticated, correlated `chat_message` path used by typed input; partial, empty, failed, and cancelled recognition MUST NOT submit.
- **FR-014**: Voice-originated text MUST retain the active user, chat, device, turn, request-generation, and submission identities needed for owner isolation, ordering, idempotency, resume, and audit. Each turn MUST snapshot the currently visible chat when recognition begins; a later chat switch MUST route future turns to the newly visible chat without rebinding any existing turn, announcement, or result.
- **FR-015**: The recognized text MUST pass through the existing LLM resolution, orchestrator, agents, tools, permissions, policies, delegation, PHI, confirmation, egress, retry, cancellation, persistence, and audit paths without a voice-only bypass.
- **FR-016**: The LiveKit voice participant MUST act only as a media/turn bridge into AstralDeep; it MUST NOT independently choose an LLM, answer the user, invoke tools, or maintain a competing authoritative conversation.
- **FR-017**: Voice input MUST grant no additional scope or approval authority. A spoken utterance MAY enter the normal text path, but every consequential action MUST still satisfy its existing server-side confirmation and human-presence rules.
- **FR-018**: Media retries, reconnection, client resume, and repeated upstream events MUST be idempotent and MUST NOT create duplicate messages, tool calls, jobs, or summaries.

#### Spoken lifecycle feedback

- **FR-019**: After a recognized query is durably accepted, the system MUST speak exactly one concise acknowledgement selected from an approved natural phrase set that includes “On it!” and semantically similar variants, meeting SC-002's per-client p95 latency of 1.5 seconds.
- **FR-020**: While a voice-originated turn is actively processing and speech is enabled, the system MUST ensure that no interval longer than 20 seconds elapses between the end of the last truthful acknowledgement/progress/assistant utterance for that turn and the start of the next progress utterance.
- **FR-021**: Progress timing MUST be bound to stable user, chat, and turn/task identity rather than to a physical media or UI socket, and MUST survive normal reconnects without duplicate playback.
- **FR-022**: Progress speech MUST be concise, natural, truthful, and derived from sanitized user-facing lifecycle state. It MUST NOT reveal chain-of-thought, hidden prompts, credentials, raw tool arguments/results, PHI-bearing intermediate content, or unverified claims of progress.
- **FR-023**: Consecutive progress messages MUST NOT repeat the exact same wording back-to-back, and variation MUST NOT change the asserted state.
- **FR-024**: A terminal result, failure, refusal, cancellation, user-input gate, mute, session end, disconnect, or chat transfer MUST cancel or suspend stale queued progress speech as appropriate.
- **FR-025**: On successful completion, the system MUST use the completed request's authoritative text summary as the spoken-recap source when that summary is present. If no authoritative text summary exists, the system MUST generate a short faithful recap only from the committed content displayed to the user, preserving material conclusions, caveats, and next actions; it MUST NOT derive the recap from transient progress, hidden reasoning, or intermediate or uncommitted content. A non-sensitive recap MAY begin automatically; when the result contains PHI or similarly sensitive content, automatic speech MUST be limited to a generic sensitive-result notice and the system MUST require a fresh explicit tap or spoken request bound to that result before reading any sensitive detail aloud.
- **FR-026**: On failure, refusal, abandonment, or cancellation, the system MUST synthesize an accurate terminal explanation and MUST NOT use success language. Every client MUST also show a persistent, visually prominent, non-color, assertively announced text notice that distinguishes a request that did not start from one that did not complete, preserves the bounded safe server explanation and recovery action, and leaves typed chat usable. Unrelated session churn MUST NOT erase that notice; a newer turn or explicit voice reset/end MAY clear it.
- **FR-027**: When work is blocked on login, permission, confirmation, or other user input, the system MUST speak the required action once, enter a waiting state, and pause the 20-second processing cadence until processing resumes.
- **FR-028**: Acknowledgements and progress phrases MUST be ephemeral media events, not assistant chat messages, model-context entries, durable UI audio components, or substitutes for the committed result.
- **FR-029**: Spoken messages for concurrent or background turns MUST be serialized, bounded, and clearly attributable to the foreground or earlier background turn; overlapping assistant audio is prohibited, and background progress/final announcements MUST NOT be represented as the foreground turn's state or result.

#### Turn-taking, audio behavior, and recovery

- **FR-030**: The user MUST be able to interrupt any greeting, progress message, or result summary by speaking or selecting stop speech. Explicit stop MUST fence and clear local playout synchronously before awaiting any network response. Measured from client/worker acceptance of barge-in or client acceptance of explicit stop under `voice-warm-standard`, stale audible output MUST cease at p95 within 500 milliseconds and in 100% of trials within 1 second; the stale speech epoch MUST be fenced before capture reopens.
- **FR-031**: Assistant playback MUST NOT be recognized and resubmitted as user speech under supported device/audio routes; echo control, speech-state gating, and deduplication MUST be verified together.
- **FR-032**: A media interruption that reconnects within the authenticated session lease—45 seconds in the launch profile, with configuration constrained to 15–300 seconds—MUST restore an honest session state without replaying an acknowledgement/result or resubmitting a transcript already accepted; expiry MUST end media and require a fresh activation.
- **FR-033**: An unrecoverable ASR, TTS, media, permission, or network failure MUST stop the affected audio behavior, preserve any already-submitted text task/result, identify the failed capability, and retain typed-chat fallback. A TTS or playout failure after a committed result MUST state that the text result remains available and MUST NOT be presented as request-execution failure.
- **FR-034**: Audio buffers, generated speech, queues, timers, and room participation MUST be released on logout, expiry, chat deletion, takeover, client crash detection, session end, and worker shutdown.

#### Included service profile and LiveKit boundary

- **FR-035**: The feature MUST use a pinned self-hosted distribution of the official `livekit/livekit` server plus supported direct-RTC/client components required for the media worker and six shipping clients; the worker MUST NOT embed LiveKit Agents or another agentic runtime, and any upstream source modification MUST have a documented necessity, bounded patch, compatibility tests, and maintenance plan.
- **FR-036**: The operator-provided speech endpoint and credential MUST be sourced from the deployment's existing `OPENAI_BASE_URL` and `OPENAI_API_KEY` inputs, either directly inside the isolated voice service or through an explicit voice-only mapping at the service boundary.
- **FR-037**: `OPENAI_BASE_URL` and `OPENAI_API_KEY` MUST remain incapable of configuring, selecting, or acting as fallback credentials for any user LLM or System LLM; existing in-product encrypted LLM configuration remains authoritative.
- **FR-038**: The speech endpoint, API credential, LiveKit API key/secret, and media-room secrets MUST remain server-side. The `livekit-api` package and API key/secret MUST be orchestrator-only; the direct-RTC worker and clients MAY receive only separate least-privilege, user/chat/session/worker-scoped room grants lasting no more than 300 seconds, never the API secret.
- **FR-039**: TTS MUST use model `speaches-ai/Kokoro-82M-v1.0-ONNX`, voice `af_heart`, and the compatible advertised audio format/rate on every client; silent substitution of another model, voice, or on-device synthesizer is prohibited.
- **FR-040**: Capability readiness MUST verify that media establishment and the exact ASR, TTS, and voice profile are usable under explicit launch-profile budgets: each LiveKit administrative probe has a 3-second operation deadline (configuration MUST remain greater than zero and no more than 10 seconds); worker activation has an 8-second readiness deadline (configuration MUST remain 1–15 seconds); and the sequential worker-registration speech preflight has a 51-second aggregate ceiling composed of a 5-second model inventory, at most two 15-second ASR attempts, and at most two 8-second TTS attempts. Production capability reads use the already-preflighted local worker-pool projection rather than an unbounded remote speech probe. Readiness results expire after 10 seconds in the launch profile and that cache TTL MUST remain 1–30 seconds. Any exceeded budget fails the affected capability closed; configuration presence alone MUST NOT yield a ready state.
- **FR-041**: Backend access to configured speech/media services MUST enforce fixed approved destinations, verified TLS outside explicit local development, no proxy or redirect following, and redacted failures through the approved streaming-egress path. Its launch ceilings are DNS 3 seconds, connect 5 seconds, TLS handshake 5 seconds, write 10 seconds, read 30 seconds, total session 35 seconds, and close 0.25 seconds; request 4 MiB, response 8 MiB, headers 32 KiB across at most 64 fields, at most 8 resolved addresses, and 64-KiB write chunks. Speech adapters MUST narrow these limits to a 5-second/512-KiB model inventory, two 15-second ASR attempts with at most 60 seconds of 16-kHz mono audio and 64 KiB of response, and two 8-second TTS attempts whose response is bounded by the requested 24-kHz sample ceiling plus a 65,536-byte WAV allowance. Candidate code MUST NOT raise these ceilings without an explicit security/dependency review and matching tests/evidence.
- **FR-042**: The system MUST fail closed when platform speech credentials, media credentials, required models, or `af_heart` are absent or rejected, while leaving typed chat operational and explaining the unavailable capability.
- **FR-043**: End users MUST NOT be asked for or shown a speech URL, speech key, LiveKit URL, LiveKit secret, model picker, or voice picker in onboarding, settings, or conversation mode.

#### Security, privacy, audit, and observability

- **FR-044**: Every media session, token issuance, transcript submission, takeover, interruption, degraded transition, and terminal outcome MUST be bound to the authenticated Keycloak user and isolated by user/chat/session.
- **FR-045**: Raw microphone audio and synthesized audio MUST be ephemeral and MUST NOT be stored in conversation history, uploads, audit payloads, databases, caches beyond active processing, crash reports, or telemetry.
- **FR-046**: The final transcript MUST follow the existing message-retention and audit policy because it is the user's ordinary chat message; logs and metrics MUST record only bounded non-content metadata needed for operations unless an existing authorized audit rule explicitly requires content.
- **FR-047**: Speech credentials, internal endpoints, media secrets, raw upstream response bodies, raw audio, and PHI-bearing transcript/summary text MUST be excluded or redacted from logs, metrics, tracing, client errors, and audit metadata.
- **FR-048**: Voice media carrying PHI MUST receive transport protection, access isolation, trusted-service handling, and local audible-disclosure controls equivalent to or stronger than typed PHI; any end-to-end media encryption boundary and unavoidable plaintext processor MUST be documented and tested. Activating voice mode alone MUST NOT count as consent to read a later sensitive result aloud.
- **FR-049**: Structured observability MUST measure session counts/states, readiness, connection and recovery outcomes, time to listening, transcription latency, acknowledgement latency, progress-cadence gaps, time to first result audio, interruption latency, TTS failures, deduplication, and resource cleanup without recording content.
- **FR-050**: Capacity limits MUST be per authenticated user and deployment policy, produce an honest retryable unavailable state, and never be enforced solely by an unauthenticated global in-memory socket count.

#### Cross-client contract and verification

- **FR-051**: Any new or changed voice frame, action, capability, state, or field MUST be added to the authoritative shared protocol manifest and classified by every client; a required voice contract MUST NOT be silently ignored.
- **FR-052**: Device/form-factor adaptation MUST come from the shared server-owned definition and declared ROTE capabilities, not client identity; similarly sized clients with the same capabilities MUST present equivalent layout and function.
- **FR-053**: Every client MUST provide equivalent accessible names, pressed/busy/muted/listening/speaking state, focus behavior, non-color cues, assertive terminal-error announcements, persistent request-failure notices, and permission recovery for the conversation controls.
- **FR-054**: Android and Apple permission manifests/entitlements, Windows packaging, and every client's runtime capability reporting MUST accurately include the audio capture/playback capabilities actually shipped.
- **FR-055**: Automated verification MUST cover golden, denial, malformed-event, low/no-audio, timeout, upstream error, cancellation, duplicate, reconnect, takeover, multi-user isolation, PHI redaction, credential non-disclosure, 20-second cadence, final-summary fidelity, accessibility, protocol drift, and cleanup paths.
- **FR-056**: Live E2E verification MUST exercise the exact candidate against the real Docker backend, real Keycloak, real configured LiveKit and speech services, representative agent/tool work lasting beyond 20 seconds, and every affected client target. Authentication requiring user credentials MUST pause for user login rather than use fabricated success.
- **FR-057**: Mac-hosted verification MAY exercise web, the PySide desktop client, Android emulator, and Apple simulators/devices, but MUST NOT be reported as Windows-native packaging/audio or physical acoustic-loop evidence; those remain explicit release evidence on an appropriate Windows and physical-device environment.

### Key Entities

- **Voice Service Profile**: The operator-owned, non-user-editable speech/media capability, including exact ASR/TTS/voice identifiers, readiness state, supported format/rate, and references to server-held credentials without exposing their values.
- **Voice Session**: One authenticated user's active media relationship to one client, including stable session identity, owning device, currently visible chat, lifecycle state, mute state, start/activity/expiry timestamps, and takeover lineage. The session may follow chat navigation, but it contains no retained raw audio and never changes an existing turn's chat binding.
- **Voice Turn**: The correlation between one captured utterance, provisional/final transcript state, the ordinary submitted chat message, foreground/background execution state, the agentic task/turn, and its terminal committed result.
- **Spoken Announcement**: An ephemeral, turn-bound utterance with a kind (greeting, acknowledgement, progress, waiting, result, failure), sanitized speakable text, lifecycle timestamps, interruption status, deduplication identity, and, for a result recap, a source reference identifying either the authoritative text summary or the committed-visible-content fallback. It is not a chat message.
- **Media Access Grant**: Short-lived, least-privilege authorization for one authenticated participant to join the intended room/session, excluding the server API secret and speech credential.
- **Voice Capability State**: The server-owned availability and reason vocabulary consumed consistently by every client, including separate hardware capability, permission authorization, media readiness, ASR readiness, and TTS readiness.

## Success Criteria *(mandatory)*

### Measurement Protocol

- SC-001, SC-002, SC-004, and the acoustic interruption/reconnect portions of SC-007 are measured separately on every shipping client against the same
  immutable candidate under the named `voice-warm-standard` staging profile. The exact LiveKit,
  worker, ASR, TTS, and `af_heart` readiness probes MUST have remained green for at least ten
  minutes; five unscored warm-up trials precede collection; and the client-to-public-staging path
  MUST sustain at least 5 Mbps in each direction with measured p95 round-trip latency no greater
  than 120 ms, p95 jitter no greater than 30 ms, and packet loss no greater than 1% before and
  throughout the run. A profile breach invalidates the run rather than silently excluding slow
  product trials.
- Each per-client percentile in SC-001, SC-002, SC-004, and SC-007 uses at least 100 eligible measured
  trials and the nearest-rank percentile. Timeouts, reconnects, and product failures remain in the
  denominator as misses. Durable-acceptance/result-availability boundaries use server monotonic
  event time; audible-start boundaries require the matched client playout event plus an ephemeral
  acoustic observation whose waveform bytes are discarded before the next trial begins.
- The SC-005 review set contains at least 100 synthetic/non-PHI turns: 20 successful turns with an
  authoritative summary, 20 successful fallback-recap turns, 20 failures, 15 refusals, 10
  cancellations, and 15 sensitive-result cases. A fixed rubric scores terminal-state accuracy,
  unsupported claims, material caveat preservation, next-action preservation, fabricated progress,
  and pre-consent disclosure; no case may be dropped after selection.
- The SC-012 panel uses at least five independent raters and a balanced synthetic/non-PHI set of at
  least 30 clips covering greeting, acknowledgement, progress, interruption, and recap. Clip order
  is blinded, each rater uses the same 1–5 naturalness/clarity rubric, and the arithmetic mean and
  phrase-variation checks are retained as candidate-bound evidence.

### Measurable Outcomes

- **SC-001**: Under the `voice-warm-standard` protocol, at least 95% of measured activations begin the greeting or reach listening within 3 seconds on each client after microphone permission is already granted.
- **SC-002**: Under the `voice-warm-standard` protocol, at least 95% of accepted spoken turns on each client begin an audible acknowledgement within 1.5 seconds of durable transcript acceptance, and 100% receive exactly one acknowledgement before any scheduled progress speech.
- **SC-003**: Across deterministic and candidate-bound staged active turns lasting longer than 20 seconds, 100% have no audible-feedback gap over 20.0 seconds while speech is enabled and processing is not waiting on the user; no progress utterance begins after a terminal event.
- **SC-004**: Under the `voice-warm-standard` protocol, at least 95% of successful committed results on each client begin their spoken recap within 2 seconds of becoming available to the voice layer; each default recap is no longer than 30 seconds or 80 spoken words unless the user asks to hear more. In source-precedence tests, 100% use the authoritative completed text summary when one is present, and fallback recaps occur only when it is absent and contain no information outside the committed content shown to the user.
- **SC-005**: Human review of the fixed 100-turn minimum matrix above finds no material contradiction, invented completion, lost critical caveat, or incorrect terminal state in at least 95% of spoken recaps, with zero fabricated progress claims and zero sensitive details spoken before per-result consent.
- **SC-006**: In parity tests, 100% of final transcripts produce exactly one ordinary chat submission and the same authorization/confirmation verdict as the identical typed text; reconnect and duplicate-event scenarios produce zero duplicate messages, tool calls, or jobs; a new query during active work produces one durable background transition plus one foreground submission in the same accepted-turn transition without cancelling either turn; and every post-navigation turn routes to the newly visible chat while prior turns remain in their originating chats.
- **SC-007**: The complete live voice journey—activate, greet, listen, auto-submit, acknowledge, hear at least two progress updates, receive a terminal recap, interrupt with p95 local audible silence within 500 milliseconds and 100% within 1 second, mute, reconnect before the actual 45-second launch-profile lease expires without duplicate submission/replay, and stop—passes on web, Windows, Android, macOS, iOS, and watchOS before release, subject only to a constitution-compliant bounded platform-unavailability exception.
- **SC-008**: Isolation tests with at least five simultaneous voice users for 30 minutes produce zero cross-user/cross-chat audio, transcript, room, announcement, takeover, or result leakage and zero unintended session termination caused by another user.
- **SC-009**: Failure injection for missing credentials, missing model/voice, denied permission, media failure, ASR failure, TTS failure, request refusal/cancellation/abandonment, timeout, and network loss yields an honest usable text fallback in 100% of cases, visibly distinguishes request non-start/non-completion from post-result speech failure on every client, and exposes zero secret values or raw upstream bodies.
- **SC-010**: Storage and telemetry inspection after representative sessions finds zero retained raw microphone or synthesized audio and zero speech/media credentials in client state, logs, metrics, traces, audit metadata, databases, or crash artifacts.
- **SC-011**: Accessibility verification finds 100% of voice controls on all six clients have meaningful accessible names, operable activation/stop/mute actions, perceivable current state and persistent assertively announced terminal errors, keyboard/switch reachability where supported, and no state conveyed by sound or color alone.
- **SC-012**: The fixed listening-panel protocol above rates the `af_heart` greeting, acknowledgement, progress, interruption, and recap experience at an arithmetic mean of at least 4.0 out of 5 for naturalness and clarity, with no back-to-back identical progress phrase in the evaluated sessions.
- **SC-013**: Inactivity tests on every client show that 100% of sessions with no active turn, no user-input gate, and no user speech or interaction end and release media ownership at five minutes within a ±5-second tolerance, while sessions actively processing or waiting on the user do not time out as idle.

## Assumptions

- The initial spoken-output locale is English (United States), matching the requested `af_heart` voice metadata. The multilingual ASR model may recognize additional languages, but expanding guaranteed spoken-language quality or selecting other voices requires a later feature.
- Automatic end-of-turn submission is intentional. The visible final transcript and normal confirmation gates are the safety controls; this feature does not insert a transcript-confirmation tap into every ordinary voice turn.
- Conversation mode is foreground-only. Existing durable background execution continues after capture/playback pauses, and the visible committed result remains the source of truth.
- One active media session per authenticated user is an acceptable default for privacy, cross-device coherence, and duplicate prevention.
- The completed request's authoritative text summary is the preferred spoken-recap source. If it is absent, a short recap is generated only from committed user-visible content; no transient, hidden, intermediate, or uncommitted content may become its source, and the recap does not become a second authoritative answer.
- `af_heart` remains the only launch voice even on watchOS. Platform dictation and on-device synthesis may remain outside conversation mode as accessibility/text-entry aids, but they do not count as feature parity.
- The exact model inventory supplied by the user is available at specification time. Release readiness still performs a live exact-model/voice check and fails visibly if that inventory changes.
- The deployment operator, not end users, owns LiveKit and speech-service configuration. Formal lead-developer approval for new third-party runtime dependencies and pinned versions must still be recorded in the implementation PR under Constitution V.
- A self-hosted LiveKit deployment can be added to the deployment topology with production TLS, required WebRTC/TURN reachability, scoped token issuance, and adequate capacity. The plan must document local-development and production topologies separately.
- The user's instruction to use the `.env` values for speech is deliberate and voice-only. Feature 054's behavioral guarantee—environment values never configure any user/System LLM—remains non-negotiable; any mechanism-absence guard wording must be narrowed without weakening that guarantee.
- Real login credentials remain user-entered. E2E work stops at each real login boundary and resumes after the user completes authentication.

## Dependencies and Constraints

- Existing authenticated chat submission, conversation snapshots, async task lifecycle, progress/status events, cancellation, authorization, PHI, audit, LLM configuration, and cross-device continuity contracts.
- Existing server-driven chrome/ROTE/UI-protocol ownership. The current tree has no shared composer-action contract, so planning must define one without creating six divergent hard-coded definitions.
- The requested speech endpoint, exact models, `af_heart`, and deployment-provided credential values.
- A pinned, self-hosted LiveKit server plus supported direct-RTC and web/Python/Kotlin/Swift client components. These are new third-party/runtime infrastructure and require explicit lead-developer approval, license/version review, lockfile/image updates, and compatibility testing. The media worker deliberately does not embed LiveKit Agents or another agentic runtime.
- An approved bounded streaming-egress path for authenticated HTTP and WebSocket media/model traffic. The existing buffered HTTP helper alone does not prove realtime safety.
- Platform microphone/audio permissions and packaging: browser user-gesture/autoplay rules; Windows audio modules and frozen build; Android recording permission/runtime handling; Apple microphone usage descriptions, entitlements, privacy manifests, and audio-session behavior.
- Candidate-bound automated and live evidence for all affected clients. Simulators do not prove Windows-native packaging or real acoustic echo cancellation, so those require their own release evidence.
