# Research: Client-Local Conversational Speech

**Feature**: `075-client-local-speech`
**Date**: 2026-08-28
**Scope**: AstralDeep orchestration/persistence, AstralProjection web/native clients, the retained LLM Factory worker path, and candidate-bound qualification.

## R-001 — Keep one server-owned deployment selector

**Decision**: Add `VOICE_SPEECH_BACKEND=llm_factory|client_local` to AstralDeep. Missing means `llm_factory`; an explicitly unknown, empty, or malformed value makes voice unavailable while typed chat remains available. The value is immutable for a process lifetime and an active session never changes backend.

**Rationale**: The backend determines privacy, readiness, protocol, and audit behavior. Allowing a client preference or automatic fallback would let an untrusted endpoint change whether audio leaves the device and would make one session's provenance ambiguous.

**Alternatives considered**:

- Per-user/client preference: rejected because selection is deployment policy, not an untrusted privacy override.
- Automatic local-to-remote or remote-to-local fallback: rejected because failure must be visible and auditable.
- Remove the remote path: rejected because it is recovered, already shipped, and remains the compatibility/default profile.

## R-002 — Version local speech without widening remote v1

**Decision**: Preserve the existing `llm_factory` capability, session, LiveKit/watch, worker-control, transcript-proof, and playout bytes for v1 clients. A `client_local` deployment advertises REST schema v2 and the `client_local/v1` profile; every new local WebSocket frame carries `schema_version: "2"` and a distinct local type. On a local deployment, the legacy v1 capability/session surfaces return their existing safe unavailable shape with `client_contract_upgrade_required` and never start remote speech. Clients that do not understand v2 fail voice closed. Shared JSON Schema and `ui_protocol.json` entries define every new frame across all clients.

**Rationale**: Existing clients use exact-key validators. Additive keys are not automatically backward compatible. A backend-discriminated union makes old remote behavior testable byte-for-byte and makes old clients reject local mode honestly.

**Alternatives considered**:

- Add optional local keys to v1: rejected because strict existing decoders may fail or, worse, ignore privacy-critical fields.
- Ship local mode under an unversioned transport string: rejected because it provides no capability/contract negotiation.

## R-003 — Represent local sessions honestly in AstralPlane

**Decision**: Add guarded AstralPlane migration `075.001` from exact predecessor `074.004`. Add `voice_session.speech_backend` with `llm_factory|client_local`, add `client_local` to the transport vocabulary, and make remote-media-only columns conditionally nullable under a named constraint. Retain generation, speech/control revision, chat, owner, lease, takeover, and lifecycle fields for both modes. No audio, transcript text, proof, engine, endpoint, or capability inventory is persisted.

**Rationale**: The current Plane repository and PostgreSQL constraint accept only `livekit|watch_pcm_websocket`, and remote room/grant metadata is mandatory. Synthetic room names or pretending a local session is LiveKit would corrupt the durable model. Plane owns this schema and migration; Deep must repin the exact qualified revision.

**Alternatives considered**:

- Keep backend only in memory: rejected because reconnect, takeover, cleanup, and audit would lose the session's selected profile.
- Store `client_local` in the current column without migration: impossible because the database and repository validation reject it.
- Create parallel local-speech tables: rejected because session/turn ownership and retention must remain one lifecycle.

## R-004 — Bind recognition before final; keep final dispatch ordinary

**Decision**: In local mode, the client sends the strict authenticated `voice_local_recognition_started` schema-v2 frame on the current UI WebSocket after the first speech/partial event. For every local input the server validates the socket user, registered device, current connection generation, server-held unexpired control binding, owner connection, session generation, speech revision, current chat/context, foreground/capture state, sequence, and client-turn UUID; the bearer control value is not retransmitted in WebSocket frames. It creates the normal durable recognition row and returns `voice_local_turn_bound`. The client then sends `voice_local_final`. A sibling `admit_local_transcript` canonicalizes/digests the text, validates the exact binding/idempotence using the client/socket/session fences instead of a worker HMAC, and produces the same `TranscriptAdmission` consumed by the ordinary `handle_chat_message` dispatch path. It cannot direct-call an agent, tool, LLM, persistence writer, or result publisher. `voice_local_recognition_failed` abandons a bound but unfinished recognition. The existing remote `admit_transcript`, v1 `VoiceOrigin`, and remote `chat_message` bytes do not change.

**Rationale**: Binding overlaps network latency with the user's utterance, preserves the existing durable recognition lifecycle, and avoids both a serial REST request and any relaxation of the proof-only v1 `VoiceOrigin`. The authenticated client is allowed to submit its own user text but cannot impersonate the remote worker or mint its HMAC proof. The new frame is only a stricter ingress adapter; it cannot call an agent, tool, LLM, persistence writer, or result publisher except through the existing ordinary dispatcher.

**Alternatives considered**:

- Give the client `VOICE_CONTROL_SECRET` or a worker proof key: rejected as an authority leak.
- Accept an unsigned local final as an ordinary typed frame with no session binding: rejected because replay, takeover, and chat-switch fences would be weaker than voice v1.
- A new transcript REST endpoint that dispatches directly: rejected because it would become a second conversation entry path and add serial latency.
- Widen the existing v1 `VoiceOrigin`: rejected because it is intentionally worker-proof-only and current clients validate it exactly.

## R-005 — Deliver server-authorized text, not client-selected chat text

**Decision**: For `client_local`, the existing coordinator emits the strict schema-v2 `voice_local_announcement` to the owner socket after the same cadence, sensitive-output, refusal, cancellation, and recap policy. The frame carries bounded text plus device/connection/session/generation/speech-revision/turn/announcement/sequence/kind/locale/expiry, mute-revision, and consent-revision fences. The client synthesizes only that text and reports the distinct `voice_local_playout_event` outcome `started|finished|interrupted|failed`; the exact remote v1 media playout frame is unchanged. Recognition is stopped while TTS is active and for a 500 ms post-terminal echo fence.

**Rationale**: Speaking arbitrary DOM, chat, or model output would bypass the server's sensitive-result and hidden-reasoning policies. Client callbacks are operational observations, not proof of audibility.

**Alternatives considered**:

- Let clients speak the latest visible result: rejected because UI order and visibility are not speech authorization.
- Send local PCM from Astral: rejected because that is remote TTS under another transport and defeats the local profile.

## R-006 — Browser local speech is capability-gated and intentionally narrow

**Decision**: Require the unprefixed Web Speech API shape with `processLocally=true`, a positive `SpeechRecognition.available()` result for `en-US`, and explicit user-gesture installation when the state is downloadable. Never use `webkitSpeechRecognition` and never set `processLocally=false`. Require `speechSynthesis` plus at least one matching voice with `localService=true`; report only categorical capability, not a voice inventory. Unsupported browsers remain typed-only in a `client_local` deployment.

**Rationale**: The [Web Speech API draft](https://webaudio.github.io/web-speech-api/) defines `processLocally`, `available()`, and `install()`, but support remains experimental. [Microsoft's Edge documentation](https://learn.microsoft.com/en-us/microsoft-edge/web-platform/speech-recognition-api) documents the current local recognition/install flow. Feature detection and a strict local-only requirement prevent a browser cloud recognizer from being mislabeled.

**Alternatives considered**:

- UA sniffing: rejected because implementation/version does not prove current capability.
- A JavaScript Whisper runtime/model download: rejected because it adds a large runtime/model closure and does not cover all shipping clients.
- Prefixed recognition fallback: rejected because it cannot guarantee on-device processing.

## R-007 — Use Apple platform speech with explicit on-device enforcement

**Decision**: iOS/macOS/watchOS use `SFSpeechRecognizer` only after authorization, locale, availability, and `supportsOnDeviceRecognition` pass; each request sets `requiresOnDeviceRecognition=true`. Use a retained, delegate-backed `AVSpeechSynthesizer` for output. Add `NSSpeechRecognitionUsageDescription` only to bundles that invoke Speech, keep existing microphone wording, and review the privacy manifests/store disclosure without claiming every device/language is supported.

**Rationale**: Apple's [`SFSpeechRecognizer`](https://developer.apple.com/documentation/speech/sfspeechrecognizer) and [`requiresOnDeviceRecognition`](https://developer.apple.com/documentation/speech/sfspeechrecognitionrequest/requiresondevicerecognition) provide the runtime local-processing gate; [`AVSpeechSynthesizer`](https://developer.apple.com/documentation/avfaudio/avspeechsynthesizer) supplies lifecycle callbacks and immediate stop. Runtime model/language availability still varies, particularly on watchOS.

**Alternatives considered**:

- Permit server recognition when on-device support is absent: rejected because the selected backend forbids hidden fallback.
- Reuse watch PCM mode: rejected because it sends microphone audio off-device.

## R-008 — Require Android API 33+ for provable local locale readiness

**Decision**: Require Android API 33+ for the strict local profile. First require `SpeechRecognizer.isOnDeviceRecognitionAvailable()`, create with `createOnDeviceSpeechRecognizer()`, and use `checkRecognitionSupport()`/`RecognitionSupport.getInstalledOnDeviceLanguages()` to prove `en-US` is installed before activation. A separately user-triggered `triggerModelDownload()` may prepare a missing asset, but readiness is rechecked before session creation. Require microphone permission, main-thread lifecycle, manifest visibility for recognition/TTS services, and deterministic `destroy()`. Use `TextToSpeech` only after initialization, locale checks, and selection of a voice whose `isNetworkConnectionRequired` is false; report terminal callbacks through `UtteranceProgressListener`. API 26–32 remain supported for typed chat but are local-voice ineligible.

**Rationale**: The Android [`SpeechRecognizer`](https://developer.android.com/reference/android/speech/SpeechRecognizer), API-33 [`RecognitionSupport`](https://developer.android.com/reference/android/speech/RecognitionSupport), and [`TextToSpeech`](https://developer.android.com/reference/android/speech/tts/TextToSpeech) APIs meet the no-new-package constraint when runtime capability is present. The on-device factory begins at API 31, but the installed/pending/supported on-device language inventory needed by the strict pre-activation locale gate begins at API 33.

**Alternatives considered**:

- Ordinary `createSpeechRecognizer()`: rejected because the selected service may use a network implementation.
- Raise the app minSdk to 31: rejected because voice capability must not remove typed-chat support from older installs.

## R-009 — Use a first-party Windows ASR helper and Qt local TTS

**Decision**: Use the already locked PySide6 Addons `QTextToSpeech` module for TTS and update PyInstaller collection/probes for its Windows engine plugin. Implement ASR as a small first-party Windows helper over an inherited, length-bounded stdin/stdout pipe using installed Windows speech APIs and in-memory PCM. Build and hash the helper from source in the Windows packaging lane; sign both helper and outer application in the protected release lane. It has no listening socket, temporary audio file, inherited network credential, or Python runtime dependency.

**Rationale**: Qt provides TTS but not speech recognition. A first-party helper avoids adding an unapproved Python WinRT dependency while making the native artifact explicit and auditable. Qt documents [`QTextToSpeech`](https://doc.qt.io/qtforpython-6/PySide6/QtTextToSpeech/QTextToSpeech.html); Windows' installed speech recognizer accepts audio streams through [`SetInputToAudioStream`](https://learn.microsoft.com/en-us/dotnet/api/system.speech.recognition.speechrecognitionengine.setinputtoaudiostream).

**Alternatives considered**:

- Add a Python `winrt` package: rejected pending separate runtime-dependency approval and closure audit.
- Invoke PowerShell per utterance: rejected because provenance, framing, process lifetime, and secret inheritance are weaker.
- Use a microphone-owning helper: rejected because it would race the existing Qt audio/session controller.

## R-010 — Bound remote retries and make fixed speech ready before admission

**Decision**: Give all attempts for one ASR operation one total deadline rather than two full 15-second budgets; give TTS retries the same one-deadline property. Keep fast retries for immediate retryable statuses. Build the runtime phrase cache after successful inference preflight and synchronously warm the greeting plus earliest acknowledgement before worker registration; warm the remaining closed-vocabulary phrases off the session path. Add low-cardinality phase timers and fix Windows/Android remote media-grant renewal without adding connection pooling in this feature.

**Rationale**: The recovered service passed the exact preflight in 1.393 seconds. A generated 1.664-second phrase took TTS 0.723 seconds on first use and 0.153/0.142 seconds warm; ASR returned the exact phrase in 0.418/0.355/0.361 seconds. The former ASR 500s came from LLM Factory OOM, not connection setup. The exact 30-second symptom matches two independent 15-second attempts. Fixed-origin connection reuse is security-sensitive and cannot repair OOM; it should be a separately measured hardening only if phase telemetry shows material setup cost.

**Alternatives considered**:

- Remove retries: rejected because fast transient 429/connection failures remain recoverable.
- Register while greeting warm-up is still running: rejected because the first admitted session can miss the cache.
- Add generic HTTP pooling now: deferred because measured endpoint setup is small and the existing transport intentionally avoids shared connection state.

## R-011 — Add no third-party runtime or downloaded model

**Decision**: Use only existing locked dependencies, browser/OS APIs, and the first-party Windows helper. Do not add a browser model bundle, Android/Apple model, third-party speech SDK, or Python package. Any implementation discovery that requires one stops for explicit lead approval and a dependency audit.

**Rationale**: The repository constitution requires prior approval for product dependencies. Platform APIs cover the supported capability subset; unsupported clients already have an honest typed fallback.

**Alternatives considered**:

- Exact Whisper/Kokoro on every client: rejected because model/runtime size and hardware limits make it unsuitable for browser/watch and would create multiple release closures.

## R-012 — Qualify code, local-only behavior, and audibility separately

**Decision**: Unit/contract tests use deterministic recognizer/TTS fakes. Local integration tests block remote speech endpoints and assert zero calls. Final evidence uses the same candidate artifacts with real Keycloak/PostgreSQL/ordinary dispatch, supported physical devices, a supported browser, a native Windows host, and both `llm_factory` and `client_local` deployment modes. Audible claims require device/acoustic evidence; callbacks alone are not audibility proof. No GitHub Actions, push, or PR occurs until locally executable gates and artifacts are complete.

**Rationale**: Simulator/API presence cannot prove installed language models, local-only execution, interruption behavior, packaging, or audible output. The Constitution requires candidate-bound staging, changed-code coverage, privacy/security, and affected-client evidence.

**Alternatives considered**:

- Treat emulator/simulator callbacks as release proof: rejected because they do not exercise the platform speech service or physical audio route.
- Use hosted CI before local work is complete: rejected by the owner's explicit usage constraint and normal local-before-push policy.
