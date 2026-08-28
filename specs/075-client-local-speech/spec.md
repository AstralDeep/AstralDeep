# Feature Specification: Client-Local Conversational Speech

**Feature Branch**: `codex/075-client-local-speech`

**Created**: 2026-08-28

**Status**: Draft

**Input**: User description: "Keep the existing LLM Factory speech path and add an explicit flag that selects either remote speech or on-device speech for the web and native clients. LLM Factory ASR/TTS experienced OOM failures and recovered during specification; local speech remains the approved resilience path while the remaining voice reliability, cleanup, backlog-planning, testing, and pull-request work proceeds."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Hold a Conversation Using Local Speech (Priority: P1)

As a signed-in user, I can start a voice conversation on a supported client while the deployment is configured for local speech, speak naturally, hear prompt acknowledgements and answers, and continue the same durable conversation without sending microphone audio to the remote speech service.

**Why this priority**: Remote speech recently failed during ASR/TTS OOM events even though its model inventory remained reachable. A complete local path preserves the core user journey during future remote outages while improving audio privacy and removing that availability dependency.

**Independent Test**: Configure local speech, disable access to every remote speech endpoint, start a voice conversation on one supported client, submit two spoken turns, and verify both turns are accepted into the ordinary conversation and their authorized spoken responses play locally.

**Acceptance Scenarios**:

1. **Given** local speech is selected and the client proves that local recognition and synthesis are ready, **When** the user starts voice, **Then** the session becomes ready without allocating remote speech work or sending microphone audio off the device.
2. **Given** an active local voice session, **When** the user finishes speaking, **Then** the final text is bound to the current user, device, session, conversation, and turn before it enters the ordinary authenticated conversation path.
3. **Given** the spoken request is accepted, **When** acknowledgement, progress, result, or failure speech is authorized, **Then** the client speaks only the bounded text authorized for that current session and reports the local playout outcome.
4. **Given** local speech is active, **When** the remote speech service is unavailable, **Then** the local conversation continues without attempting a hidden remote fallback.
5. **Given** local synthesized speech is playing, **When** recognition would otherwise capture that output, **Then** recognition remains suppressed until playout has ended and the bounded echo-suppression interval has passed.

---

### User Story 2 - Select and Roll Back the Speech Pipeline Safely (Priority: P1)

As an operator, I can explicitly select either the existing remote speech pipeline or the client-local pipeline for a deployment, observe the selected mode without credentials or endpoints being exposed, and roll back by changing the selection and restarting the service.

**Why this priority**: The new path must not weaken or remove the current implementation. A single authoritative selection prevents clients from making inconsistent privacy or reliability choices.

**Independent Test**: Exercise identical supported clients against one deployment in each selection and verify that remote mode remains behaviorally unchanged, local mode never invokes remote speech, and an unknown selection prevents voice from being advertised.

**Acceptance Scenarios**:

1. **Given** remote speech is selected, **When** a client checks capability or starts voice, **Then** the existing media-worker, speech-profile, security, and readiness behavior remains unchanged.
2. **Given** local speech is selected, **When** a client checks capability, **Then** it receives a versioned local-speech requirement and must prove its own local readiness before activation.
3. **Given** a missing legacy selection, **When** the system starts, **Then** it preserves the remote mode for backward compatibility.
4. **Given** an unknown or malformed selection, **When** the system starts or advertises voice, **Then** voice fails closed and typed conversation remains available.
5. **Given** local speech is selected, **When** a client cannot satisfy it, **Then** the client reports a specific local unavailability reason and does not switch itself to remote speech.

---

### User Story 3 - Use Local Speech Across Shipping Clients (Priority: P1)

As a user, I receive the same server-owned local-speech contract on the web, Windows, Android, iOS, macOS, and watchOS clients, subject to each device proving that its installed operating-system speech capability can process the configured language locally.

**Why this priority**: Voice is a platform capability rather than a single-client experiment. Inconsistent or hidden client behavior would recreate the current all-client reliability problem and violate user privacy expectations.

**Independent Test**: Run the shared local-speech conformance journey on every shipping client, including supported, permission-denied, local-language-unavailable, backgrounded, muted, interrupted, and stop-speech cases.

**Acceptance Scenarios**:

1. **Given** a shipping client with local speech available, **When** local mode is selected, **Then** it supports activation, greeting, recognition, ordinary request acceptance, authorized result speech, mute, stop, interruption, end, and foreground recovery.
2. **Given** a client or browser whose recognition could use a network service, **When** it cannot prove local-only processing, **Then** it is ineligible for local mode.
3. **Given** a device requires a local language asset, **When** installation is possible, **Then** installation occurs only after an explicit user action and readiness is checked again afterward.
4. **Given** a supported app remains installable on an older operating-system version that lacks local speech, **When** the user opens it, **Then** typed conversation remains available and local voice is honestly unavailable.
5. **Given** any client receives a local-speech frame with stale, malformed, duplicated, out-of-order, or mismatched ownership data, **When** it processes the frame, **Then** it refuses the frame without speaking or submitting its text.

---

### User Story 4 - Recover Remote Voice Reliability (Priority: P2)

As a user who remains on remote speech, I receive faster failure feedback, a prompt cached greeting, and consistent media recovery rather than a silent or thirty-second stall.

**Why this priority**: The existing pipeline remains the rollback and compatibility path. Keeping it operational prevents the new feature from becoming a one-way migration.

**Independent Test**: Select remote speech; simulate slow ASR, failed TTS warming, a media-grant rotation, and a transient disconnect; verify bounded failures, a ready greeting before admission, and recovery on every applicable client.

**Acceptance Scenarios**:

1. **Given** remote ASR stops responding, **When** an utterance reaches recognition, **Then** all retry attempts share one bounded operation deadline rather than multiplying the full deadline.
2. **Given** a remote worker is advertised as ready, **When** its first session begins, **Then** the greeting and earliest acknowledgement phrases are already available for immediate playout.
3. **Given** a Windows or Android remote media connection is interrupted within its valid session lease, **When** connectivity returns, **Then** the client obtains a current media grant and rejoins without submitting stale media or transcript data.
4. **Given** any speech phase fails, **When** operators inspect content-free telemetry, **Then** they can distinguish activation, connection setup, recognition attempt, synthesis/cache, and client playout durations without seeing audio, transcripts, credentials, or endpoint details.

---

### User Story 5 - Preserve Privacy, Authorization, and Durable Conversation Semantics (Priority: P1)

As a user or security reviewer, I can rely on local speech being only a different input/output transport: it cannot bypass identity, permissions, sensitive-output rules, audit provenance, normal durable conversation acceptance, or cancellation.

**Why this priority**: Moving recognition to an untrusted client must not turn user-controlled text into privileged worker output or create a second agent dispatch route.

**Independent Test**: Attempt cross-user, cross-device, stale-session, stale-conversation, replayed-final, altered-text, sensitive-output, hidden-reasoning, and permission-denied cases and verify the ordinary security and conversation boundaries remain authoritative.

**Acceptance Scenarios**:

1. **Given** a local recognizer returns text, **When** the client submits it, **Then** the text has no more authority than other authenticated user input and cannot directly invoke a tool or agent outside the ordinary dispatcher.
2. **Given** a local final is replayed or altered, **When** it reaches the server, **Then** it is rejected or deduplicated according to its current session and turn binding.
3. **Given** a result is sensitive or speech is otherwise disallowed, **When** local output is considered, **Then** the same consent and output policy used by remote speech decides whether any text may be spoken.
4. **Given** local speech is selected, **When** an operator reviews logs, metrics, audit records, or durable storage, **Then** no raw microphone audio, local speech buffer, hidden reasoning, credential, or unrestricted transcript copy appears there.

### Edge Cases

- A local speech engine is present at capability check but disappears, crashes, or loses its language asset during a session.
- Recognition permission and microphone permission have different states, including restricted, denied, and not-yet-determined.
- The user backgrounds, locks, suspends, or closes a client while recognition or synthesis is active.
- A local final arrives after mute, stop, takeover, chat switch, session expiry, connection replacement, or logout.
- Local synthesis reports completion without audible output, reports an error after the text result has already committed, or never invokes a terminal callback.
- A browser exposes speech recognition but cannot prove local processing, or reports that a language is downloadable but installation is refused or interrupted.
- Two devices attempt local sessions for the same user, and the second must follow the existing explicit takeover policy.
- The selected local output language is unavailable while recognition is available, or vice versa.
- An announcement is duplicated, arrives out of order, expires before playout, or is received while the client is hidden or speech-muted.
- The operator changes the selected pipeline while old sessions or old client versions remain connected.
- The remote speech service returns a successful model inventory while real recognition or synthesis inference is failing, including an ASR/TTS OOM condition that later recovers.
- A local engine returns an empty final, oversized text, unsupported language, control characters, or text that differs from the digest submitted with it.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide one server-owned `VOICE_SPEECH_BACKEND` deployment selection with exactly two valid values: `llm_factory` for the existing remote speech pipeline and `client_local` for the client-local speech pipeline.
- **FR-002**: The system MUST preserve the remote speech pipeline, including its current worker isolation, exact speech profile, media transport, authorization, and failure behavior, when remote speech is selected.
- **FR-003**: The system MUST default a missing legacy selection to `llm_factory` and MUST fail voice closed for any explicit unknown or malformed selection.
- **FR-004**: A client MUST NOT select, override, or silently fall back between speech pipelines; it may report only bounded capability observations used by server policy.
- **FR-005**: The selected speech pipeline MUST be visible through a versioned, authenticated capability contract that exposes no endpoint, credential, secret, local file path, or unrestricted engine inventory.
- **FR-006**: Local activation MUST require current user, device, connection, foreground, microphone, audio-output, recognition-permission, synthesis, configured-language, and local-processing eligibility.
- **FR-007**: Local activation MUST preserve existing single-owner session, explicit takeover, lease, generation, visible-conversation, chat-context, mute, stop, and end semantics.
- **FR-008**: In local mode, microphone audio MUST remain on the originating device and MUST NOT be sent to Astral, LLM Factory, a browser cloud recognizer, or any other remote speech service.
- **FR-009**: A web client MUST require an unprefixed recognition capability that can explicitly guarantee local processing; exposing a generic or cloud-capable speech interface alone MUST NOT make the browser eligible.
- **FR-010**: A native client MUST require an operating-system recognition path that explicitly requests local processing and MUST refuse local mode when that guarantee or the configured language is unavailable.
- **FR-011**: Optional local language-asset installation MUST require an explicit user action, MUST provide bounded progress/failure feedback, and MUST NOT start a voice session until readiness is re-proven.
- **FR-012**: Interim local recognition text MUST remain ephemeral and local to the active client display.
- **FR-013**: Each nonempty local final MUST be canonicalized, bounded, correlated, and bound to the authenticated user, device, connection, session generation, visible conversation, chat-context revision, and one client turn before acceptance.
- **FR-014**: Local final submission MUST provide idempotent replay handling and MUST reject stale, altered, oversized, unsupported-language, cross-owner, cross-device, or cross-conversation input.
- **FR-015**: A local transcript MUST enter the same ordinary authenticated conversation dispatcher used by typed and remote-voice input and MUST retain all LLM configuration, permission, PHI, tool, audit, persistence, cancellation, and result-commit gates.
- **FR-016**: Local transcript text MUST be treated as user-controlled input and MUST NOT inherit worker, system, tool, or agent authority.
- **FR-017**: The server MUST remain the sole authority for greeting, acknowledgement, progress, waiting, result, sensitive-notice, failure, refusal, and cancellation speech content.
- **FR-018**: A local-speech client MUST speak only a bounded, current, server-authorized announcement whose session, generation, sequence, turn, foreground, mute, consent, and expiry fences all validate.
- **FR-019**: Local clients MUST report content-free playout started, finished, interrupted, or failed outcomes without those observations becoming proof that sound was audible.
- **FR-020**: Recognition MUST be suspended while local synthesized speech is active and MUST resume only after a terminal playout outcome plus a bounded echo-suppression interval.
- **FR-021**: Mute, stop, backgrounding, interruption, takeover, logout, and session end MUST stop local capture and local speech immediately before any best-effort network acknowledgement.
- **FR-022**: Local speech failure after durable text acceptance MUST NOT roll back, duplicate, or misstate the committed conversation result.
- **FR-023**: Sensitive-result consent, output-language policy, hidden-reasoning exclusion, and refusal policy MUST apply identically to local and remote speech.
- **FR-024**: Web, Windows, Android, iOS, macOS, and watchOS MUST classify and handle every required local-speech capability, session, transcript, announcement, control, and terminal state in the same contract revision.
- **FR-025**: Clients that cannot satisfy local mode MUST present a stable reason and typed-conversation fallback without invoking remote speech.
- **FR-026**: Remote-mode clients MUST retain bounded media-grant recovery and MUST reject stale grants, worker identities, transcripts, and playout frames.
- **FR-027**: Remote recognition retries MUST share one total operation deadline, and worker admission MUST not precede readiness of the earliest fixed greeting and acknowledgement speech.
- **FR-028**: The system MUST record low-cardinality, content-free timing and outcome evidence for local capability, activation, recognition, submission, authorized speech, playout, interruption, and remote speech phases.
- **FR-029**: Logs, metrics, audit records, exceptions, and durable voice metadata MUST exclude audio, interim text, unrestricted transcript text, speech credentials, transcript proofs, remote endpoints, local engine paths, and hidden reasoning.
- **FR-030**: Local audio and synthesis buffers MUST be bounded in memory, cleared at their terminal lifecycle boundary, and never written to ordinary files, caches, analytics, or durable storage.
- **FR-031**: The local pipeline MUST introduce no user-editable provider endpoint, credential, voice-model selector, or hidden network permission.
- **FR-032**: Typed conversation MUST remain available whenever either speech pipeline is disabled, unavailable, unsupported, or recovering.
- **FR-033**: The deployment and release workflow MUST validate both selectable pipelines independently and MUST NOT weaken remote-mode evidence requirements merely because local mode exists.
- **FR-034**: A pipeline change MUST require an explicit deployment restart or equivalent configuration reload and MUST NOT silently convert an already-active session between pipelines.
- **FR-035**: Older clients that do not understand the local contract MUST fail voice closed when local mode is selected while preserving typed conversation.

### Key Entities

- **Speech Pipeline Selection**: The operator-owned choice between remote and client-local speech, including its validation state and contract revision.
- **Client Local-Speech Capability**: Bounded observations about local-only recognition, synthesis, permissions, configured language, and readiness for one authenticated device and connection.
- **Voice Session**: The existing owner-scoped, generation-fenced conversation voice lifecycle, extended to record which speech pipeline was selected without changing user ownership or chat authority.
- **Local Recognition Final**: Ephemeral user-controlled text plus correlation and fence data for one completed local utterance; it has no privileged worker authority.
- **Authorized Text Announcement**: Bounded server-approved speech text with session, turn, sequence, policy, foreground, mute, and expiry fences for local synthesis.
- **Playout Observation**: A content-free client report that local speech started, finished, was interrupted, or failed.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With local speech selected and required language assets installed, at least 95% of supported-client voice activations reach a ready listening state within 3 seconds.
- **SC-002**: At least 95% of local-session greetings begin within 1 second after activation becomes ready.
- **SC-003**: At least 95% of ordinary local utterances receive visible and audible acknowledgement within 2.5 seconds after the user stops speaking, excluding the downstream conversational-model execution time.
- **SC-004**: A complete two-turn local conversation succeeds while all remote speech endpoints are unreachable, with zero microphone-audio bytes sent off device.
- **SC-005**: Every shipping client passes the same supported, unavailable, permission-denied, stale-frame, mute, stop, interruption, sensitive-output, and typed-fallback conformance journeys before local mode is enabled for that client release.
- **SC-006**: Remote mode produces byte-compatible capability and media behavior for existing compatible clients until they opt into the new contract revision.
- **SC-007**: A nonresponsive remote recognition operation terminates within one total configured deadline and never multiplies that deadline by the number of attempts.
- **SC-008**: Windows and Android recover an interrupted valid remote media session within the existing lease window without replaying stale credentials or user turns.
- **SC-009**: No tested local-mode denial, replay, takeover, reconnect, or client-spoofing case bypasses ordinary conversation authorization, ownership, PHI, permission, or audit gates.
- **SC-010**: Automated retention checks find no raw audio, interim recognition text, unrestricted transcript text, credentials, local engine paths, or hidden reasoning in logs, metrics, audits, exceptions, or durable voice records.
- **SC-011**: When local speech is unsupported or becomes unavailable, users receive a specific explanation and usable typed conversation within 2 seconds, with zero silent remote fallbacks.
- **SC-012**: Local speech can be disabled and remote speech restored solely through the documented deployment selection, without data migration or loss of existing conversations.

## Assumptions

- The initial configured speech language remains United States English; additional languages require their own policy and qualification evidence.
- Platform-local engines and voices may differ in accuracy, cadence, and timbre from the fixed remote profile; local mode is an explicitly different profile rather than an equivalence claim.
- A device or browser that cannot prove local-only recognition is unsupported for local mode, even if it exposes a generic speech interface.
- Required local language assets may be installed by the operating system or browser after explicit user consent, but Astral does not download or distribute a model silently.
- Existing authentication, device binding, session ownership, conversation persistence, permission, PHI, audit, and server-owned speech-policy mechanisms remain authoritative.
- LLM Factory ASR/TTS recovered from the observed OOM incident during specification and passed Astral's exact preflight plus a short synthesized/retranscribed comparison; this recovery does not remove the approved `client_local` resilience requirement.
- The remote speech service is not modified by this feature; operational repair and capacity management of that service remain a separate deployment responsibility.
- No voice session automatically changes pipeline while active; users end and restart voice after an operator changes deployment configuration.
