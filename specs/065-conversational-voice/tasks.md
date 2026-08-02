# Tasks: Conversational Voice Interface Across All Clients

**Input**: Design documents from `specs/065-conversational-voice/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`

**Tests**: The specification explicitly requires golden, denial, malformed-event, race, failure,
cleanup, accessibility, cross-client, timing, privacy, and live end-to-end verification. Test tasks
therefore precede their implementation tasks and must fail for the intended reason before product
code is changed.

**Organization**: Tasks are grouped by user story. All P1 stories precede the P2 interruption and
recovery story. Shared authority, persistence, contracts, and media isolation are foundational
because no story may introduce a second dispatch or authorization path.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel after its phase prerequisites because it touches different files and
  does not depend on another incomplete task in the same phase.
- **[US1]…[US6]**: Maps the task to the corresponding user story in `spec.md`.
- Every task names the exact file or directory it changes or verifies.

---

## Phase 1: Setup and Dependency Approval

**Purpose**: Establish the collision-safe implementation base, obtain the constitution-required
dependency approval, and lock every new artifact before runtime code imports it.

- [X] T001 Re-fetch `origin`, confirm feature-064 remains separately owned/unpushed, validate branch/spec ownership and `.specify/feature.json`, reserve target revision `065.001` without guessing 064's predecessor, and preserve the existing owner edit in `apple-clients/AstralApp/AstralApp.xcodeproj/project.pbxproj`
- [X] T002 Record the 2026-07-31 lead-developer decision approving the RTC-only replacement, prohibit the Agents closure, define the exact final-closure audit/fingerprint gates in `specs/065-conversational-voice/dependency-approval.md`, and require both T190's completed local artifact inventory and T004's final owner-reviewed closure approval before merge or distribution
- [ ] T003 Before the next requested implementation push, select one clean committed candidate, regenerate all ten native coverage reports and canonical local evidence for that exact SHA, run `BASE_SHA="$(git rev-parse origin/main)" make prepare-release-evidence` in strict mode, and record its result in `specs/065-conversational-voice/verification.md`. Explicitly record that implementation commits `dfea619`, `43fba94`, and `2332234` were pushed before this Feature-065 diagnostic gate was completed; a later run does not retroactively make those pushes compliant
- [ ] T004 After T168 supplies immutable-candidate worker evidence and T180 supplies the installed protected-policy identity, complete the distribution closure: verify the DHI base signature/SBOM/VEX, build and test both architectures, obtain zero-HIGH/CRITICAL scans, populate final image/platform digests, bind that protected policy, obtain owner review of the exact closure fingerprint, and only then authorize artifact export or distribution in `backend/voice_agent/CLOSURE.json`, `specs/065-conversational-voice/dependency-approval.md`, and `specs/065-conversational-voice/verification.md`
- [X] T005 [P] Add `livekit==1.1.14` and its exact Windows wheel/native closure to `windows-client/requirements.in` and regenerate `windows-client/requirements-release.lock.txt` with hashes
- [X] T006 [P] Vendor LiveKit web client 2.21.0 with its upstream license, expected digest, and reviewed full dependency notices/checksum in `backend/webrender/static/vendor/livekit-client.umd.min.js`, `backend/webrender/static/vendor/LICENSE.livekit-client`, `backend/webrender/static/vendor/livekit-client.sha256`, `backend/webrender/static/vendor/THIRD_PARTY_NOTICES.livekit-client`, and `backend/webrender/static/vendor/THIRD_PARTY_NOTICES.livekit-client.sha256`
- [X] T007 [P] Pin `io.livekit:livekit-android:2.27.0`, lock transitives, and restrict JitPack content to AudioSwitch in `android-client/gradle/libs.versions.toml`, `android-client/app/build.gradle.kts`, and `android-client/settings.gradle.kts`
- [X] T008 [P] Add LiveKit Swift 2.15.3 only to the iOS/macOS AstralApp targets, never AstralCore/watchOS, in `apple-clients/AstralApp/AstralApp.xcodeproj/project.pbxproj` after explicitly preserving and integrating the owner’s pre-existing project-file diff
- [X] T009 [P] Add digest-pinned single-node local/staging/production LiveKit configurations, secret-free Compose service topology, worker-local `OPENAI_*` to `VOICE_SPEECH_*` mapping, and port/TURN guidance in `deploy/livekit/livekit.local.yaml`, `deploy/livekit/livekit.staging.yaml`, `deploy/livekit/livekit.production.yaml`, `deploy/livekit/README.md`, `docker-compose.yml`, and `docker-compose.staging.yml`
- [X] T010 [P] Create the isolated test-only validator manifests with `jsonschema==4.25.1`, `openapi-spec-validator==0.7.2`, all transitives, and Python-3.11 hashes in `tooling/contract-ci/requirements.in` and `tooling/contract-ci/requirements.lock.txt`

**Checkpoint**: Exact dependencies are approved and locked; feature 065 is based on the final 064
schema/client state; voice evidence and all-language changed-code inputs are understood by the
deterministic pre-push/protected gates; no secret value or unrelated project-file change has been
absorbed.

---

## Phase 2: Foundational Authority, Persistence, Contracts, and Media Boundary

**Purpose**: Build the shared primitives every user story needs before enabling any microphone or
assistant playback.

**⚠️ CRITICAL**: No user-story implementation begins until this phase is complete and its tests
fail first, then pass.

### Foundational tests and fixtures

- [X] T011 [P] Create C0–C6 positive/negative shared contract vectors, proof golden vectors, quantum bounds, and aggregate-reservation cases in `backend/tests/fixtures/voice_065/client_conformance.json`
- [X] T012 [P] Write meta-schema, local-reference, discriminator, strict-extra-field, REST mapping, worker-pool registration, worker-grant outer/nested equality and five-minute expiry semantics, packet-size, UUID4, proof, and quantum validation tests in `tooling/contract-ci/tests/test_validate_voice_contracts.py`
- [X] T013 [P] After the authorized 064 handoff, write representative actual-final-064→065.001 upgrade, wrong-predecessor refusal, repeat-run, constraints/indexes, tombstone, rollback/recovery, and schema-revision tests in `backend/tests/test_voice_migration_065.py` and `backend/tests/test_schema_revision_guard.py`
- [X] T014 [P] Write no-queue `voice_interactive` admission, two-running-turn-per-user, and capacity-refusal-before-acceptance tests in `backend/tests/test_voice_admission_065.py` and `backend/tests/test_work_admission.py`
- [X] T015 [P] Write non-empty workspace copy-forward, immutable execution-base digest, private-stage isolation, three-view component/layout rebase, conflict notice, flat fallback, abort, cleanup, and reverse-completion tests in `backend/tests/test_conversation_publication_voice_065.py`
- [X] T016 [P] Write authenticated durable-dispatch tests for claims/token lifetime, `llm_context_user_id`, delegation, user/System-LLM separation, `finally` scrubbing, cancellation, and typed-turn compatibility in `backend/tests/test_durable_user_turn_dispatch_065.py`
- [X] T017 [P] Write strict `register_ui.device_id`, `connection_generation`, control-binding mint/expiry/rotation/redaction, correlated `new_chat`, and `voice_playout_event` admission tests in `backend/tests/test_voice_protocol_065.py`
- [X] T018 [P] Write row-lock/CAS ownership, generation fencing, grant revision, worker lease, idle receipt-time, and replay-idempotency repository tests in `backend/orchestrator/tests/test_voice_sessions_065.py`
- [X] T019 [P] Write fake-clock coordinator claim, sequence, stale-fence, phrase-key, sample-reservation, and crash-recovery tests in `backend/orchestrator/tests/test_voice_coordinator_065.py`
- [X] T020 [P] Write fixed-origin DNS/peer/SNI/TLS and redirect/proxy-refusal tests plus launch-ceiling tests for DNS/connect/TLS/write/read/total/close at 3/5/5/10/30/35/0.25 seconds, request/response/header/address/chunk bounds at 4 MiB/8 MiB/32 KiB/8/64 KiB, cancellation, and redacted errors in `backend/shared/tests/test_streaming_egress_065.py`
- [X] T021 [P] Write LiveKit room/client-grant/direct-worker-grant/disconnect, 300-second maximum grant, 3-second operation/10-second maximum, exact-profile 10-second readiness-cache/1–30-second range, no-store response, orchestrator-only API secret, least-privilege claim, worker-grant revision, and stale participant tests in `backend/orchestrator/tests/test_livekit_service_065.py`
- [X] T022 [P] Write worker import-isolation, no-Agents/no-LLM/no-tools/no-database/no-`livekit-api`, HTTP-upgrade challenge replay/expiry, pool registration/capacity, assignment/grant mismatch, wrong-direction frame, queue/rate/size, and buffer-cleanup tests in `backend/voice_agent/tests/test_worker_boundary_065.py`

### Foundational implementation

- [X] T023 Implement the hash-locked JSON Schema/OpenAPI meta-validator and fixture runner in `tooling/contract-ci/validate_voice_contracts.py` so malformed or semantically invalid contracts fail CI
- [X] T024 Integrate the authorized feature-064 handoff, record its actual final revision, then implement guarded repeat-safe `voice_session`/`voice_turn`, conversation-commit/layout metadata, indexes/constraints/delete actions, `voice_interactive` admission defaults, and the strictly predecessor-bound target `SCHEMA_REVISION = '065.001'` in `backend/shared/database.py`
- [X] T025 Implement canonical component/layout digests, `user_acceptance` and linked private `assistant_result` stages, immutable base selection, deterministic three-way merge, layout-reference validation, conflict notices, targeted abort, and base-anchor release in `backend/orchestrator/conversation_publication.py`
- [X] T026 Replace whole-task workspace locking with short admission/snapshot and terminal-publication locks while preserving current-anchor reads and snapshot retention in `backend/orchestrator/history.py` and `backend/orchestrator/workspace.py`
- [X] T027 Generalize the async runner into an authenticated durable user-turn dispatcher with verified in-memory claims/token binding, explicit LLM context, stable idempotency, running-lease ownership, and unconditional scrubbing in `backend/orchestrator/async_tasks.py`
- [X] T028 Extend strict shared frame parsing for canonical device/connection UUID4s, correlated `new_chat`/`chat_created`, `voice_origin`, fully correlated acknowledgements/rejections, control bindings, and content-free playout events in `backend/shared/protocol.py`
- [X] T029 Bind `device_id`, current `connection_generation`, Keycloak lifetime, and short-lived memory-only control bindings to authenticated UI registrations and socket teardown in `backend/orchestrator/orchestrator.py`
- [X] T030 Implement the PostgreSQL-backed session/turn repository, ownership/takeover CAS, grant refresh idempotency metadata, context revisions, worker/control leases, foreground selection, and five-minute true-idle state in `backend/orchestrator/voice_sessions.py`
- [X] T031 Implement coordinator state/claim scaffolding, monotonic scheduling adapters, deterministic IDs, bounded phrase keys, result-sample reservation CAS, and stale-generation fences in `backend/orchestrator/voice_coordinator.py`
- [X] T032 Implement the approved fixed-destination streaming HTTP/WSS connector with DNS pinning, original Host/SNI, TLS, no redirects/proxies, byte/frame/time bounds, cancellation, and typed redacted failures in `backend/shared/streaming_egress.py`
- [X] T033 Implement LiveKit readiness, scoped client and direct-worker room grants through orchestrator-only `livekit-api`, worker assignment/control bind, participant rotation/removal, and disconnect primitives with no-store/redacted outputs in `backend/orchestrator/livekit_service.py`
- [X] T034 Create the isolated Python worker entrypoint, config allowlist, challenge-authenticated pool control client, bounded session multiplexing/queues, shutdown cleanup, and runtime import guard in `backend/voice_agent/main.py` and `backend/voice_agent/session.py`
- [X] T035 Add the default-on operational `FF_CONVERSATIONAL_VOICE` kill switch plus non-content voice state/reason/timing metric registration and redaction policy in `backend/shared/feature_flags.py` and `backend/orchestrator/runtime_observability.py`

**Checkpoint**: Contracts validate; migrations and concurrency primitives are repeat-safe; the one
normal authenticated dispatcher is usable by typed and future voice turns; the worker remains a
media-only principal.

---

## Phase 3: User Story 1 — Start a Natural Voice Conversation (Priority: P1) 🎯 MVP

**Goal**: A signed-in user on any shipping client explicitly starts/stops one foreground voice
session, grants microphone permission, hears one fixed-profile greeting, sees honest state, can
take over from another device, and idles out after five true-idle minutes.

**Independent Test**: On each client, start from voice-off, grant or deny microphone access, hear
one `af_heart` greeting only after explicit activation and worker context sync, see one final
transcript, stop without cancelling accepted work, exercise takeover, and prove five-minute
listening-only expiry within ±5 seconds.

### Tests for User Story 1

- [X] T036 [P] [US1] Write authenticated create/get/update/end/takeover owner/device/connection/generation/grant-revision API contract tests in `backend/tests/test_voice_session_api_065.py`
- [X] T037 [P] [US1] Write one-session-per-user, exact activation-id retry, takeover fencing, foreground suspension, no-cancel end, lease expiry, and five-minute true-idle tests in `backend/orchestrator/tests/test_voice_session_lifecycle_065.py`
- [X] T038 [P] [US1] Write pinned-RTC signature/behavior guards, connect-return and initial-publication reconciliation, synchronous-callback queueing, authorized manual subscription, finite 16-kHz/32-ms stream overrun, exact Silero recurrent-state endpointing, greeting-only null turn, explicit capture gate, no-speech/empty-final, permission/capability failure, reconnect fencing, and ephemeral-buffer tests in `backend/voice_agent/tests/test_session_start_065.py`
- [X] T039 [P] [US1] Write web activation, getUserMedia/autoplay, deny/revoke/no-device, greeting, visible state, stop, takeover, and idle tests in `backend/tests/test_voice_client_conformance_065.py` and `tooling/web-ci/tests/voice-conversation-065.spec.js`
- [X] T040 [P] [US1] Write PySide activation, QtMultimedia permission/device, greeting, stop, takeover, and idle reducer tests in `windows-client/tests/test_voice_contract_065.py` and `windows-client/tests/test_voice_lifecycle_065.py`
- [X] T041 [P] [US1] Write Android permission, audio-device, activation, greeting, stop, takeover, and idle tests in `android-client/app/src/test/kotlin/com/personalailabs/astraldeep/app/voice/VoiceSessionController065Test.kt`
- [x] T042 [P] [US1] Write dependency-free shared Apple session/control/permission state tests in `apple-clients/AstralCore/Tests/AstralCoreTests/VoiceContract065Tests.swift`
- [x] T043 [P] [US1] Write iOS/macOS activation, permission, greeting, stop, takeover, scene state, and idle controller tests in `apple-clients/AstralApp/AstralAppTests/VoiceSessionController065Tests.swift`
- [x] T044 [P] [US1] Write watchOS activation, microphone permission, bridge-ready, greeting, stop, takeover, and idle tests in `apple-clients/AstralWatchTests/VoiceContract065Tests.swift` and `apple-clients/AstralWatchTests/WatchVoiceBridge065Tests.swift`

### Implementation for User Story 1

- [X] T045 [US1] Implement `getVoiceCapability`, `createVoiceSession`, `takeOverVoiceSession`, `updateVoiceSession`, `endVoiceSession`, and `stopVoiceSpeech` with strict headers/bodies/no-store errors in `backend/orchestrator/api.py`
- [x] T046 [US1] Implement transactional start/takeover/suspend/resume/end/lease-expiry/idle-expiry state transitions and audited generation fencing in `backend/orchestrator/voice_sessions.py`
- [X] T047 [US1] Implement the single-owner AstralDeep direct-RTC state machine with `auto_subscribe=False`, validated participant/microphone subscription, finite 16-kHz mono 32-ms `AudioStream`, published small-queue `AudioSource` tracks, exact Silero VAD recurrent state/endpointing, callback-to-event queueing, overrun abort, capture gating, generation-fenced reconnect/republication reconciliation, worker context acknowledgement, greeting lifecycle, and bounded partial/final buffers in `backend/voice_agent/session.py`
- [X] T048 [US1] Implement the fixed Kokoro `af_heart` WAV/24-kHz greeting path with duration/format validation and no fallback in `backend/voice_agent/speech_adapters.py`
- [X] T049 [US1] Create the ordered server-owned composer action/state model with capability, owner, takeover, permission, busy, mute, and listening semantics in `backend/webrender/chrome/composer_model.py`
- [X] T050 [US1] Add the web composer host, local LiveKit controller, explicit media activation, greeting/listening states, final-transcript preview, stop/takeover, idle teardown, and typed fallback in `backend/webrender/templates/shell.html` and `backend/webrender/static/client.js`
- [X] T051 [US1] Add the PySide reducer/controller, LiveKit RTC room, QtMultimedia source/sink, activation/permission/takeover/idle/end handling, and final-transcript presentation in `windows-client/astral_client/voice.py` and `windows-client/astral_client/app.py`
- [X] T052 [US1] Add the Android voice controller, direct LiveKit room/audio integration, runtime permission flow, activation/takeover/idle/end handling, and transcript presentation in `android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/voice/VoiceSessionController.kt`, `android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/ui/AppViewModel.kt`, and `android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/ui/AdaptiveShell.kt`
- [x] T053 [US1] Add dependency-free voice frames/state/reducer/control mappings shared by iOS, macOS, and watchOS in `apple-clients/AstralCore/Sources/AstralCore/Protocol/Voice.swift`
- [x] T054 [US1] Add the iOS/macOS LiveKit session controller, microphone/audio-session integration, activation/takeover/idle/end handling, and transcript presentation in `apple-clients/AstralApp/AstralApp/Voice/VoiceSessionController.swift`, `apple-clients/AstralApp/AstralApp/AppModel.swift`, and `apple-clients/AstralApp/AstralApp/Views/ChatView.swift`
- [x] T055 [US1] Implement the foreground watch PCM client, AVAudioEngine/AVAudioConverter capture/playback, one-time bridge handshake, activation/takeover/idle/end handling, and transcript presentation in `apple-clients/AstralWatch/WatchVoiceBridge.swift`, `apple-clients/AstralWatch/WatchModel.swift`, and `apple-clients/AstralWatch/Views/WatchChatView.swift`
- [X] T056 [P] [US1] Add accessible web voice state styling, focus/pressed/busy/muted/listening cues, and non-color permission/error feedback in `backend/webrender/static/astral.css`
- [X] T057 [P] [US1] Package QtMultimedia and LiveKit native artifacts and declare offline runtime evidence in `windows-client/AstralDeep.spec` and `windows-client/deployment/runtime-manifest.json`
- [X] T058 [P] [US1] Declare `RECORD_AUDIO` and real capture/playback capabilities without unrelated permissions in `android-client/app/src/main/AndroidManifest.xml` and `android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/transport/DeviceCaps.kt`
- [x] T059 [P] [US1] Add microphone usage strings, macOS audio-input entitlement, and microphone/data privacy declarations in `apple-clients/AstralApp/Info.plist`, `apple-clients/AstralApp/WatchInfo.plist`, `apple-clients/AstralApp/AstralApp-macOS.entitlements`, `apple-clients/AstralApp/AstralApp/PrivacyInfo.xcprivacy`, and `apple-clients/AstralWatch/PrivacyInfo.xcprivacy`
- [x] T060 [US1] Implement server/worker lease sweeping, idle-only expiry, logout/auth/chat-current fencing, media cleanup, and no accepted-task cancellation in `backend/orchestrator/voice_sessions.py` and `backend/voice_agent/session.py`
- [ ] T061 [US1] Run the basic selected-chat/no-chat activate→greet→listen→final-transcript→stop checkpoint on all available clients and record simulator/diagnostic limits in `specs/065-conversational-voice/verification.md`

**Checkpoint**: US1 works independently on every client with explicit media consent, one greeting,
honest state, takeover, typed fallback, and idle cleanup; it does not yet claim agentic dispatch or
progress/result speech.

---

## Phase 4: User Story 2 — Speak Into the Normal Agentic System (Priority: P1)

**Goal**: One final transcript becomes exactly one ordinary authenticated chat turn with identical
LLM, agent, authorization, confirmation, PHI, audit, persistence, background, and cancellation
semantics to typed text.

**Independent Test**: Submit identical typed and spoken requests through allowed, denied,
confirmation-gated, PHI, LLM-unconfigured, duplicate/reconnect, background, navigation, and deleted
chat cases; compare acceptance and authorization outcomes and prove no duplicate operation or tool
call.

### Tests for User Story 2

- [x] T062 [P] [US2] Write canonical text normalization, two-minute HMAC golden vectors, altered/expired/transplanted proof, wrong worker/binding, and non-persistence tests in `backend/tests/test_voice_transcript_proof_065.py`
- [x] T063 [P] [US2] Write typed-versus-voice parity tests for Keycloak owner, user/System LLM choice, RFC 8693 delegation, tool allow/deny, confirmation, PHI, egress, audit, retry, and cancellation in `backend/tests/test_voice_dispatch_parity_065.py`
- [x] T064 [P] [US2] Write partial/empty/final, exact-once submission, duplicate LiveKit/client replay, fully correlated acceptance/rejection, explicit-retry-new-IDs, and no hidden queue tests in `backend/tests/test_voice_submission_065.py`
- [x] T065 [P] [US2] Write no-selected-chat correlated/idempotent `new_chat`, changed-payload reuse, delayed response after navigation, no early grant/greeting, and hydration tests in `backend/tests/test_voice_new_chat_activation_065.py`
- [x] T066 [P] [US2] Write two-running-same-chat-turn, immutable-context, reverse-completion, one-tool-call-each, foreground/background, component/layout conflict, and no-rerun performance tests in `backend/tests/perf/voice_concurrent_turns.py`
- [x] T067 [P] [US2] Write current/older-origin chat deletion and authorization-loss tests covering current media end, unaccepted rejection, accepted voice abandonment without operation cancellation, private-stage abort, physical deletion, and replay tombstones in `backend/tests/test_voice_chat_deletion_065.py`
- [x] T068 [P] [US2] Add C2 transcript/submission/rejection vectors to each real client parser test at `backend/tests/test_voice_client_conformance_065.py`, `windows-client/tests/test_voice_contract_065.py`, `android-client/core/src/test/kotlin/com/personalailabs/astraldeep/core/protocol/VoiceContract065Test.kt`, and `apple-clients/AstralCore/Tests/AstralCoreTests/VoiceContract065Tests.swift`

### Implementation for User Story 2

- [x] T069 [US2] Implement canonical transcript normalization/digest, domain-separated short-lived proof issuance/verification, constant-time comparison, and proof scrubbing in `backend/orchestrator/voice_sessions.py`
- [x] T070 [US2] Implement recognition-start binding, immutable chat/context/turn/submission/request allocation, bounded partial/final publication, proof attachment, and accepted/rejected buffer clearing in `backend/voice_agent/session.py`
- [x] T071 [US2] Validate complete `voice_origin` equality/freshness and preserve ordinary `chat_message` semantics when it is absent in `backend/shared/protocol.py`
- [x] T072 [US2] Integrate voice-origin text into `handle_chat_message` only after proof/owner/admission validation and emit strict `user_message_acked`/`voice_submission_rejected` frames in `backend/orchestrator/orchestrator.py`
- [x] T073 [US2] Add bound-destination mode that never auto-creates, navigates, widens socket scope, mutates `_ws_active_chat`, or resurrects a missing chat in `backend/orchestrator/async_tasks.py`
- [x] T074 [US2] Acquire the running no-queue operation lease before message acceptance, flip the prior voice turn to background atomically, and preserve both operations in `backend/orchestrator/work_admission.py` and `backend/orchestrator/voice_sessions.py`
- [X] T075 [US2] Publish the ordinary user bubble and full component/layout copy-forward under `user_acceptance`, then allocate the separate private result generation/stage without terminalizing the operation in `backend/orchestrator/conversation_publication.py`
- [X] T076 [US2] Publish the terminal private result through deterministic component/layout rebase, current-anchor CAS, safe conflict notice, and one-time operation terminalization in `backend/orchestrator/history.py` and `backend/orchestrator/conversation_publication.py`
- [x] T077 [US2] Extend owner-scoped `new_chat` operation idempotency and strict `chat_created` correlation without adding a voice-only creation endpoint in `backend/orchestrator/orchestrator.py`
- [x] T078 [P] [US2] Implement bounded transcript retry-until-correlated-ack/reject, partial presentation, final `chat_message` construction, and navigation-safe clearing in `backend/webrender/static/client.js`
- [X] T079 [P] [US2] Implement the same transcript binding, retry, rejection, and background-turn behavior in `windows-client/astral_client/voice.py` and `windows-client/astral_client/protocol.py`
- [X] T080 [P] [US2] Implement the same transcript binding, retry, rejection, and background-turn behavior in `android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/voice/VoiceSessionController.kt` and `android-client/core/src/main/kotlin/com/personalailabs/astraldeep/core/protocol/Messages.kt`
- [X] T081 [P] [US2] Implement the same transcript binding, retry, rejection, and background-turn behavior for iOS/macOS in `apple-clients/AstralCore/Sources/AstralCore/Protocol/Voice.swift` and `apple-clients/AstralApp/AstralApp/Voice/VoiceSessionController.swift`
- [X] T082 [P] [US2] Implement the same transcript binding, retry, rejection, and background-turn behavior through the watch bridge in `apple-clients/AstralWatch/WatchVoiceBridge.swift` and `apple-clients/AstralWatch/WatchModel.swift`
- [x] T083 [US2] Emit fully correlated `transcript_accepted`/`transcript_rejected` worker-control dispositions and bounded exact replay until terminal disposition in `backend/orchestrator/voice_coordinator.py` and `backend/voice_agent/session.py`
- [x] T084 [US2] Wire one owner/chat voice-aware delete/revocation transaction before physical chat deletion and suppress unavailable-destination publication/speech without cancelling accepted side effects in `backend/orchestrator/history.py` and `backend/orchestrator/voice_sessions.py`

**Checkpoint**: US2 is independently testable with synthetic media; a voice final is exactly one
normal user turn, and same-chat concurrent work preserves all authority and committed content.

---

## Phase 5: User Story 3 — Hear Acknowledgement, Honest Progress, and a Concise Result (Priority: P1)

**Goal**: Accepted turns receive one prompt acknowledgement, truthful varied progress before any
20-second gap, serialized terminal speech, authoritative-summary-first recap, deterministic
committed-visible fallback, and consent-gated sensitive details.

**Independent Test**: Use 2/19/21/65-second and multi-minute fake-clock turns plus terminal,
failure, refusal, cancellation, waiting, mute, two-overlapping-turn, and sensitive-result cases;
prove phrase truth, timing, serialization, recap provenance, and no post-terminal progress.

### Tests for User Story 3

- [X] T085 [P] [US3] Write fake-clock exactly-one acknowledgement-on-acceptance, 1.5-second start threshold, 14-second target, 100% hard-20-second gap, phrase variation, worker/client finish timing, wait/mute/terminal cancellation, and restart tests in `backend/orchestrator/tests/test_voice_cadence_065.py`
- [x] T086 [P] [US3] Extend overlapping-turn performance tests for positive 250-ms handoff, reserved due-turn capacity, 1.5-second result openings, 4-second quanta, second opening by 1.75 seconds, and no gap over 20 seconds in `backend/tests/perf/voice_concurrent_turns.py`
- [X] T087 [P] [US3] Write authoritative `summary_text` precedence, committed-visible traversal, sanitization, caveat/next-action preservation, 80-word/30-second cap, non-English posture, and no hidden/uncommitted input tests in `backend/orchestrator/tests/test_voice_recap_065.py`
- [X] T088 [P] [US3] Write fail-closed PHI/unknown classification, generic notice, exact result-bound tap/spoken consent, one-time consumption, ambiguity, expiry, owner, and replay tests in `backend/orchestrator/tests/test_voice_sensitive_recap_065.py`
- [x] T089 [P] [US3] Write success/failure/refusal/cancellation/waiting/background attribution, no transcript/model-context pollution, mute-no-burst, terminal-fence, safe request-outcome lifecycle fanout, current-generation, and cross-user isolation tests in `backend/tests/test_voice_announcements_065.py` and `backend/tests/test_durable_user_turn_dispatch_065.py`
- [x] T090 [P] [US3] Write exact Kokoro synthesis, 24-kHz mono WAV parsing, <=96,000 sample quantum, <=36,000 opening, <=720,000 aggregate, over-budget refusal, interruption, and no-publish-on-error tests in `backend/voice_agent/tests/test_tts_announcements_065.py`
- [X] T091 [P] [US3] Add announcement-manifest/local-playout valid/invalid C3 vectors to `backend/tests/fixtures/voice_065/client_conformance.json` and backend/client schema tests

### Implementation for User Story 3

- [X] T092 [US3] Extend the ordinary atomic completion/result contract with optional `summary_text` and `summary_source` without treating the legacy notification scrape as authoritative in `backend/orchestrator/conversation_publication.py`
- [X] T093 [US3] Implement `CommittedVisibleTextExtractor`, deterministic whitespace/semantic filtering, material caveat/action preservation, output-language policy, and bounded recap construction in `backend/orchestrator/voice_recap.py`
- [X] T094 [US3] Implement fail-closed sensitivity evaluation and strict context-bound spoken-control resolution for read/stop/mute/cancel without granting new authority in `backend/orchestrator/voice_recap.py`
- [X] T095 [US3] Implement allowlisted greeting/acknowledgement/progress/wait/terminal phrase keys, no immediate repetition, sanitized lifecycle selection, and one-output-stream ordering in `backend/orchestrator/voice_coordinator.py`
- [X] T096 [US3] Implement row-locked result sample reservation, deterministic announcement claims/IDs, 14-second scheduling target, deadline arbitration, 250-ms handoff budget, terminal preemption, and crash recovery in `backend/orchestrator/voice_coordinator.py`
- [x] T097 [US3] Connect normal operation/task lifecycle, user-input gates, cancellation, failure/refusal, and atomic committed results to progress fences and recap source selection, broadcast bounded visible lifecycle outcomes, and reconcile ended-media turns from the exact durable operation/result proof without rerunning work in `backend/orchestrator/orchestrator.py`, `backend/orchestrator/async_tasks.py`, `backend/orchestrator/voice_sessions.py`, and `backend/orchestrator/voice_bootstrap.py`
- [X] T098 [US3] Implement bounded `speak` execution, pre-synthesis duration checks, content-free announcement manifests, distinct ephemeral tracks, interruption, and lifecycle echoes in `backend/voice_agent/speech_adapters.py` and `backend/voice_agent/session.py`
- [X] T099 [US3] Validate source/client playout events, rate/size/order/fence fields, server receipt timestamps, missing-event degradation, and scheduling observations in `backend/orchestrator/voice_coordinator.py`
- [X] T100 [P] [US3] Match direct LiveKit tracks to manifests, enforce sample budgets, render serialized speech, interrupt locally, and emit content-free playout events in `backend/webrender/static/client.js`
- [X] T101 [P] [US3] Implement the same manifest/sample/render/interrupt/playout contract in `windows-client/astral_client/voice.py`
- [X] T102 [P] [US3] Implement the same manifest/sample/render/interrupt/playout contract in `android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/voice/VoiceSessionController.kt`
- [X] T103 [P] [US3] Add announcement/playout frames, strict quantum validation, result attribution, and reducer states in `apple-clients/AstralCore/Sources/AstralCore/Protocol/Voice.swift`
- [X] T104 [P] [US3] Implement direct LiveKit manifest/sample/render/interrupt/playout behavior for iOS/macOS in `apple-clients/AstralApp/AstralApp/Voice/VoiceSessionController.swift`
- [X] T105 [P] [US3] Enforce manifest-before-PCM sequence ranges, exact sample count, interruption, result attribution, and playout events on watchOS in `apple-clients/AstralWatch/WatchVoiceBridge.swift`
- [x] T106 [US3] Add deterministic 2/19/21/65-second and multi-minute timing scenarios that enforce exactly one acknowledgement, 100% no-gap-over-20.0-seconds, no post-terminal progress, and ephemeral acoustic-probe boundaries in `backend/tests/perf/voice_concurrent_turns.py`

**Checkpoint**: US3 meets the deterministic acknowledgement/cadence/recap/consent contract with
no overlapping audio, fabricated progress, hidden-source recap, or sensitive disclosure.

---

## Phase 6: User Story 5 — Use One Safe, Included Voice Capability Everywhere (Priority: P1)

**Goal**: Operators configure one exact platform speech/media profile; users configure nothing;
readiness proves the exact models/voice/media path, and every failure is isolated, redacted,
capacity-bounded, and leaves typed chat operational.

**Independent Test**: Start healthy, then independently remove/reject LiveKit, worker, ASR, TTS,
`af_heart`, credentials, route, TURN, and capacity; verify uniform exact-profile availability,
zero secret exposure or LLM fallback, per-user isolation, and usable typed chat.

### Tests for User Story 5

- [x] T107 [P] [US5] Write exact model inventory, bounded batch ASR, Kokoro `af_heart`, WAV/24-kHz, worker 8-second readiness/1–15-second range, capability 10-second cache/1–30-second range, cold-start, and capability reason tests in `backend/orchestrator/tests/test_voice_readiness_065.py` and `backend/orchestrator/tests/test_voice_media_065.py`
- [X] T108 [P] [US5] Extend sentinel environment tests to prove `OPENAI_*` reaches only worker-local `VOICE_SPEECH_*` and cannot configure user/System LLM resolution in `backend/tests/test_llm_env_inert.py` and `backend/tests/test_voice_env_isolation_065.py`
- [X] T109 [P] [US5] Build a strict fake OpenAI-compatible speech service and test Bearer auth, exact identifiers, the 51-second worst-case sequential startup budget comprising 5-second/512-KiB inventory, two 15-second/64-KiB ASR attempts with at most 60 seconds of audio, and two 8-second sample-bounded TTS attempts; also test bounded in-memory multipart transcription/final events, redirects, DNS/private policy, malformed audio, 401/404/429/5xx, cancellation, and body/secret redaction in `backend/voice_agent/tests/fake_speech_service.py`, `backend/voice_agent/tests/test_speech_preflight_065.py`, `backend/voice_agent/tests/test_speech_adapters_065.py`, and `backend/voice_agent/tests/test_tts_announcements_065.py`
- [X] T110 [P] [US5] Write legacy `/api/voice/stream`, caller-selected synthesis/upload, configuration-only-ready, and typed-fallback regression tests in `backend/tests/test_voice_legacy_retirement_065.py`
- [x] T111 [P] [US5] Write short-lived least-privilege client LiveKit/watch grants, worker-pool challenge authentication/assignment fencing, separate worker RTC join grant/revision, replay, wrong room/user/device/worker/generation/revision, no-store, API-secret isolation, and redaction tests in `backend/tests/test_voice_grants_065.py`
- [x] T112 [P] [US5] Write five-user room/session/turn/takeover/capacity isolation and cross-user leakage tests in `backend/tests/test_voice_multiuser_isolation_065.py`
- [x] T113 [P] [US5] Write database/filesystem/log/metric/trace/audit/crash-state inspections for zero audio, transcript duplicate, recap text, endpoint/key, token/ticket, provider body, and PHI leakage in `backend/tests/test_voice_zero_retention_065.py`

### Implementation for User Story 5

- [X] T114 [US5] Complete `SpeachesBatchSTT` and `SpeachesTTS` with exact fixed identifiers, explicit credentials, ambient-provider/proxy discovery disabled, bounded memory-only multipart/final handling, 24-kHz WAV validation, bounds, cancellation, and typed redacted errors in `backend/voice_agent/speech_adapters.py`
- [X] T115 [US5] Implement bounded exact-profile probes and expiring capability cache across LiveKit, worker, ASR, TTS, voice, transport, and capacity in `backend/orchestrator/livekit_service.py`
- [X] T116 [US5] Add operator-only voice aliases, separate LiveKit/control secrets, safe comments, and no user-facing speech settings in `.env.example`, `docker-compose.yml`, and `docker-compose.staging.yml`
- [X] T117 [US5] Implement authenticated `GET /api/voice/capability` with exact fixed profile, component statuses, stable reason codes, rate limits, and no internal endpoint/credential disclosure in `backend/orchestrator/api.py`
- [X] T118 [US5] Remove or fail closed the unauthenticated legacy realtime proxy, unbounded batch upload, caller-selected model/voice, and configuration-only health paths in `backend/orchestrator/api.py` while retaining any still-authorized non-conversation endpoint only under strict exact-profile contracts
- [X] T119 [US5] Mint least-privilege client grants, one-time watch tickets, and separate room-scoped direct-worker RTC grants with deterministic current-revision remint, nonce/revision/expiry fencing, no arbitrary client data publish, no API-secret disclosure, and no bearer persistence in `backend/orchestrator/livekit_service.py` and `backend/orchestrator/voice_sessions.py`
- [x] T120 [US5] Implement the challenge-response worker-pool control WSS, bounded capacity/profile registration, coordinator-selected idempotent `session_bind` delivery/redaction of the separate worker RTC grant, higher-revision reconnect binds, per-session direction/sequence/rate/size authorization, and database-lease takeover in `backend/orchestrator/api.py` and `backend/voice_agent/session.py`
- [X] T121 [P] [US5] Centralize voice secret/content/provider-body redaction and safe problem mapping in `backend/shared/streaming_egress.py` and `backend/orchestrator/runtime_observability.py`
- [X] T122 [P] [US5] Emit non-content readiness/session/turn/reconnect/dedup/takeover/cadence/interruption/TTS/cleanup metrics with bounded labels in `backend/orchestrator/runtime_observability.py`
- [X] T123 [US5] Enforce deployment and per-user session/turn capacity with honest retryable refusal before acknowledgement and no unauthenticated global socket counter in `backend/orchestrator/voice_sessions.py` and `backend/orchestrator/work_admission.py`
- [X] T124 [US5] Configure trusted production WSS, host-reachable local URLs, ICE UDP/TCP/TURN, ingress isolation, fixed worker egress, resource limits, and no loopback production advertisement in `deploy/livekit/*.yaml`, `docker-compose.yml`, and `docker-compose.staging.yml`
- [X] T125 [US5] Keep typed chat healthy while voice is unavailable/degraded and publish one server-owned recovery reason without silent model/voice/platform-speech fallback in `backend/webrender/chrome/composer_model.py` and `backend/orchestrator/api.py`

**Checkpoint**: US5 independently proves “included by default” without end-user secrets or silent
fallback and fails closed without taking down typed chat.

---

## Phase 7: User Story 6 — Equivalent Controls and States on Every Client (Priority: P1)

**Goal**: Web, Windows, Android, iOS, macOS, and watchOS consume one composer/capability/protocol
contract with complete dispositions, equivalent accessible behavior, accurate permissions, and no
client-specific state drift.

**Independent Test**: Run the exact C0–C6 fixture through every real client reducer and transport,
then compare live composer order, labels, state, permission recovery, takeover, transcript,
announcement, lifecycle, and accessibility behavior against one authenticated backend.

### Tests for User Story 6

- [x] T126 [P] [US6] Implement backend C0–C6 schema/protocol/composer conformance and invalid-vector tests in `backend/tests/test_voice_client_conformance_065.py` and `backend/tests/test_ui_protocol_manifest.py`
- [x] T127 [P] [US6] Implement web composer/render/accessibility/static-asset/worker-identity tests, including persistent “did not start”/“did not complete” request notices, safe plain-text explanations, assertive semantics, stale-state retention, typed fallback, and distinct post-result speech failure, in `backend/tests/webrender/test_voice_renderer_065.py` and full C0–C6 Playwright coverage in `tooling/web-ci/tests/voice-conversation-065.spec.js`
- [x] T128 [P] [US6] Implement Windows C0–C6 contract, lifecycle, manifest, accessibility, packaged-audio, and E2E tests, including prominent request-outcome alerts, exact rejection correlation/no replay, non-older-turn clearing, typed fallback, and distinct post-result speech failure, in `windows-client/tests/test_voice_contract_065.py`, `windows-client/tests/test_voice_lifecycle_065.py`, and `windows-client/tests/e2e_voice_065.py`
- [x] T129 [P] [US6] Implement Android C0–C6 core/controller/accessibility tests and connected media journey, including prominent TalkBack request-outcome notices, exact rejection correlation/no replay, non-older-turn clearing, typed fallback, and distinct post-result speech failure, in `android-client/core/src/test/kotlin/com/personalailabs/astraldeep/core/protocol/VoiceContract065Test.kt`, `android-client/app/src/test/kotlin/com/personalailabs/astraldeep/app/voice/VoiceSessionController065Test.kt`, and `android-client/app/src/androidTest/kotlin/com/personalailabs/astraldeep/app/VoiceConversation065InstrumentedTest.kt`
- [x] T130 [P] [US6] Implement Apple C0–C6 shared/controller/accessibility tests and iOS/macOS UI journeys, including prominent VoiceOver request-outcome notices, exact rejection correlation/no replay, non-older-turn clearing, typed fallback, and distinct post-result speech failure, in `apple-clients/AstralCore/Tests/AstralCoreTests/VoiceContract065Tests.swift`, `apple-clients/AstralApp/AstralAppTests/VoiceSessionController065Tests.swift`, and `apple-clients/AstralApp/AstralAppUITests/VoiceConversationUITests.swift`
- [x] T131 [P] [US6] Implement watchOS C0–C6 bridge/reducer/accessibility tests including exact ticket-bound worker identity plus persistent request-outcome notices, rejection correlation/no replay, non-older-turn clearing, dictation fallback, and distinct post-result speech failure in `apple-clients/AstralWatchTests/VoiceContract065Tests.swift` and `apple-clients/AstralWatchTests/WatchVoiceBridge065Tests.swift`
- [X] T132 [US6] Extend every protocol-manifest drift guard to reject ignored or unknown required voice frames/actions/capabilities in `windows-client/tests/test_protocol_manifest.py`, `android-client/core/src/test/kotlin/com/personalailabs/astraldeep/core/protocol/ProtocolManifestTest.kt`, and `apple-clients/AstralCore/Tests/AstralCoreTests/ManifestDriftTests.swift`

### Implementation for User Story 6

- [X] T133 [US6] Add every required composer, capability, binding, session, turn, acknowledgement/rejection, transcript-origin, announcement, playout, and correlated chat field/action/disposition to `backend/shared/ui_protocol.json`
- [x] T134 [US6] Finalize canonical control order, labels, icons, visibility, enabled/pressed/busy/muted/listening/speaking/error states, role gates, and reason messages in `backend/webrender/chrome/composer_model.py`
- [x] T135 [US6] Add microphone/audio-output/full-duplex/transport capabilities and form-factor adaptation without client-identity branching in `backend/rote/capabilities.py`
- [x] T136 [P] [US6] Consume the composer model generically, expose equivalent keyboard/screen-reader state and recovery, classify every required frame/action, and render a persistent assertive non-color request-outcome notice with safe text, non-older-turn clearing, typed fallback, and separate speech-failure guidance in `backend/webrender/templates/shell.html` and `backend/webrender/static/client.js`
- [x] T137 [P] [US6] Consume the same composer/action/state contract, accessible names, non-color cues, focus/error announcements, complete dispositions, and a persistent accessible request-outcome alert with safe text, non-older-turn clearing, typed fallback, and separate speech-failure guidance in `windows-client/astral_client/app.py`, `windows-client/astral_client/voice.py`, `windows-client/astral_client/protocol.py`, and `windows-client/astral_client/protocol_manifest.py`
- [x] T138 [P] [US6] Consume the same composer/action/state contract, TalkBack semantics, permission recovery, complete dispositions, and a prominent persistent request-outcome card with safe text, non-older-turn clearing, typed fallback, and separate speech-failure guidance in `android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/ui/AdaptiveShell.kt`, `android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/voice/VoiceSessionController.kt`, `android-client/core/src/main/kotlin/com/personalailabs/astraldeep/core/protocol/Wire.kt`, and `android-client/core/src/main/kotlin/com/personalailabs/astraldeep/core/protocol/ProtocolManifest.kt`
- [x] T139 [P] [US6] Decode the same frames/states/actions with strict unknown-required failure and reusable request-outcome/accessibility semantics, safe bounded messages, and non-older-turn notice reduction in `apple-clients/AstralCore/Sources/AstralCore/Protocol/Frames.swift`, `apple-clients/AstralCore/Sources/AstralCore/Protocol/Dispositions.swift`, and `apple-clients/AstralCore/Sources/AstralCore/Protocol/Voice.swift`
- [x] T140 [P] [US6] Render equivalent iOS/macOS control placement, VoiceOver/Switch Control state, permission recovery, and a prominent persistent request-outcome notice with safe text, typed fallback, and separate speech-failure guidance in `apple-clients/AstralApp/AstralApp/Views/ChatView.swift`, `apple-clients/AstralApp/AstralApp/Voice/VoiceSessionController.swift`, and `apple-clients/AstralApp/AstralApp/AppModel.swift`
- [x] T141 [P] [US6] Render the same action/state machine through the watch primary chat affordance with VoiceOver, a prominent persistent request-outcome notice, dictation fallback, separate speech-failure guidance, and no platform-speech substitution in `apple-clients/AstralWatch/Views/WatchChatView.swift` and `apple-clients/AstralWatch/WatchModel.swift`
- [x] T142 [US6] Add hash-checked build/test copying of the one canonical C0–C6 fixture into platform test bundles without maintaining client-specific variants in `android-client/app/build.gradle.kts` and `apple-clients/AstralCore/Package.swift`
- [x] T143 [US6] Verify shipped permissions, entitlements, privacy manifests, native artifacts, and runtime capability reports agree with each client build in `windows-client/deployment/runtime-manifest.json`, `android-client/app/src/main/AndroidManifest.xml`, and `apple-clients/AstralApp/AstralApp.xcodeproj/project.pbxproj`
- [x] T144 [US6] Add cross-client control/state/accessibility comparison output with explicit six-surface pass/fail evidence in `specs/065-conversational-voice/verification.md`
- [x] T145 [US6] Gate the canonical fixture validator, all client drift/conformance suites, and the web Istanbul, Android app/core Kover, and iOS/macOS/watchOS xccov coverage producers needed by the protected combined changed-code gate in `.github/workflows/ci.yml`, `.github/workflows/android-ci.yml`, and `.github/workflows/apple-ci.yml`
- [x] T146 [US6] Run the independent C0–C6 checkpoint through all six real reducers and record no ignored required frame/action plus simulator-only limitations in `specs/065-conversational-voice/verification.md`

**Checkpoint**: US6 provides one mechanically enforced capability and lifecycle vocabulary with
equivalent accessible controls across every shipping surface.

---

## Phase 8: User Story 4 — Interrupt, Recover, and Continue Without Crosstalk (Priority: P2)

**Goal**: Barge-in, overlapping requests, chat navigation, reconnect, lifecycle suspension, grant
rotation, takeover, and terminal cleanup never replay, duplicate, overlap, cross chats, or cancel
accepted work accidentally.

**Independent Test**: Interrupt each speech kind on speaker/headset routes, switch networks during
listening/synthesis, background/foreground each client, navigate chats, refresh grants, reconnect
after acceptance/result, and take over from another device; assert stable ownership and zero stale
playback, feedback-loop transcript, duplicate dispatch, or cross-chat attribution.

### Tests for User Story 4

- [x] T147 [P] [US4] Write worker speech-epoch barge-in, 250-ms quiescence/operation ceilings, pre/post-quiescence double clear, in-flight capture timeout/track replacement, local-playout-not-client-proof, stop-before-capture, self-speech suppression, AEC-gated ASR, stale-frame discard, and resumed-listening tests in `backend/voice_agent/tests/test_barge_in_065.py`
- [x] T148 [P] [US4] Write UUID4-idempotent client grant refresh plus worker RTC grant revision, SDK resume/full/terminal reconnect, 45-second launch lease expiry, allowed 15–300-second configuration range, lease-expiry fresh activation, changed publication SID, fresh-Room terminal recovery, credential-free client recovery, worker rotate/applied ordering, old-publisher drain-only behavior, replay-window expiry, and context-update ordering tests in `backend/orchestrator/tests/test_voice_reconnect_065.py`, `backend/orchestrator/tests/test_voice_session_lifecycle_065.py`, `backend/orchestrator/tests/test_voice_sessions_065.py`, and `backend/voice_agent/tests/test_reconnect_065.py`
- [x] T149 [P] [US4] Write web local-playout-fenced-before-stop-request success/failure plus visibility/pagehide/network/logout/navigation/reconnect/takeover/no-autoplay lifecycle tests in `tooling/web-ci/tests/voice-conversation-065.spec.js`
- [x] T150 [P] [US4] Write Windows local-playout-fenced-before-stop-request success/failure plus inactive/session-lock/route/quit/network/navigation/reconnect/takeover lifecycle tests in `windows-client/tests/test_voice_lifecycle_065.py`
- [x] T151 [P] [US4] Write Android background/audio-focus/route/network/navigation/reconnect/takeover lifecycle tests in `android-client/app/src/test/kotlin/com/personalailabs/astraldeep/app/voice/VoiceSessionController065Test.kt` and `android-client/app/src/androidTest/kotlin/com/personalailabs/astraldeep/app/VoiceConversation065InstrumentedTest.kt`
- [x] T152 [P] [US4] Write iOS/macOS scene/audio-session/route/network/navigation/reconnect/takeover lifecycle tests in `apple-clients/AstralApp/AstralAppTests/VoiceSessionController065Tests.swift` and `apple-clients/AstralApp/AstralAppUITests/VoiceConversationUITests.swift`
- [x] T153 [P] [US4] Write watch bridge disconnect/frame-gap/foreground/route/navigation/reconnect/takeover/no-replay lifecycle tests in `apple-clients/AstralWatchTests/WatchVoiceBridge065Tests.swift`
- [x] T154 [P] [US4] Write old-turn-background/new-turn-foreground, two completions, chat-switch attribution, explicit cancel versus interruption, and unavailable-origin cleanup tests in `backend/tests/test_voice_concurrent_lifecycle_065.py`

### Implementation for User Story 4

- [x] T155 [US4] Implement refresh-id CAS, deterministic no-store remint, credential-free conflict/current state, ordered worker rotation/applied acknowledgement, stale publisher rejection, and best-effort participant removal in `backend/orchestrator/voice_sessions.py` and `backend/orchestrator/livekit_service.py`
- [x] T156 [US4] Implement desired/applied visible-chat revisions, capture pause, worker `session_context_update`/`session_context_applied`, recognition-time snapshot, and navigation-safe resumption in `backend/orchestrator/voice_sessions.py` and `backend/voice_agent/session.py`
- [x] T157 [US4] Implement immediate speech-epoch fencing, pre/post-quiescence `AudioSource` clearing with bounded replacement fallback, assistant-output ASR gating, platform-AEC integration points, stale recap cancellation, honest client playout correlation, and prompt capture reopening in `backend/voice_agent/session.py`
- [x] T158 [P] [US4] Implement synchronous local playout fencing before explicit-stop network acknowledgement plus visibility/pagehide/logout/network/navigation suspension, fresh binding/grant/context recovery, bounded exact submission replay, and no missed-audio autoplay in `backend/webrender/static/client.js`
- [x] T159 [P] [US4] Implement synchronous local playout fencing before explicit-stop network acknowledgement plus inactive/session-lock/quit/audio-route/network/navigation suspension and recovery in `windows-client/astral_client/voice.py` and `windows-client/astral_client/app.py`
- [x] T160 [P] [US4] Implement lifecycle/audio-focus/route/network/navigation suspension and recovery in `android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/voice/VoiceSessionController.kt` and `android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/ui/AppViewModel.kt`
- [x] T161 [P] [US4] Implement scene/audio-session/route/network/navigation suspension and recovery for iOS/macOS in `apple-clients/AstralApp/AstralApp/Voice/VoiceSessionController.swift` and `apple-clients/AstralApp/AstralApp/AppModel.swift`
- [x] T162 [P] [US4] Implement strict PCM sequence/rate/size/duration bounds, reconnect/ticket refresh, interruption, navigation context, and foreground teardown in `backend/voice_agent/watch_bridge.py` and `apple-clients/AstralWatch/WatchVoiceBridge.swift`
- [x] T163 [US4] Unify logout, auth expiry, client crash lease, worker shutdown, takeover, current/old chat deletion, epoch advance, bounded stream/source close, unsubscribe/unpublish/disconnect, queue/timer/buffer/track cleanup, idempotent repeated callbacks, and accepted-work preservation in `backend/orchestrator/voice_sessions.py`, `backend/orchestrator/voice_coordinator.py`, and `backend/voice_agent/session.py`

**Checkpoint**: All six clients recover and interrupt without crosstalk, replay, stale speech,
navigation side effects, or implicit task cancellation.

---

## Phase 9: Polish, Full Verification, Staging, and Release Evidence

**Purpose**: Close cross-cutting documentation, quality, privacy, performance, packaging, live
client, and immutable-candidate evidence gates. These tasks do not authorize a merge or release;
protected CI remains authoritative.

- [X] T164 [P] Document operator topology, ports/TURN/TLS, exact dependency/profile readiness, secret injection, kill switch, rollback/drain/recovery/retirement, and zero-recording boundary in `deploy/livekit/README.md`, `.env.example`, and `specs/065-conversational-voice/quickstart.md`
- [x] T165 Run the isolated hash-locked JSON Schema/OpenAPI/fixture validator and `git diff --check`, recording commands/results in `specs/065-conversational-voice/verification.md`
- [x] T166 Run focused voice backend, migration, authorization, concurrency, legacy-retirement, and worker-container tests named in `specs/065-conversational-voice/quickstart.md` and record exact pass/fail totals in `specs/065-conversational-voice/verification.md`
- [x] T167 Run Ruff, every explicit backend root and nested module suite, performance gates, and backend/tooling Python coverage production from `.github/workflows/ci.yml`, recording the local base plus uncommitted-working-tree posture, report digests, and results without claiming an immutable candidate or the final combined gate in `specs/065-conversational-voice/verification.md`
- [ ] T168 Repeat the locked direct-RTC voice-worker unit/integration image against the strict fake service and digest-pinned LiveKit on the selected immutable candidate, rerunning exact ASR/TTS/voice checks and recording candidate-bound closure/image/config/model digests in `specs/065-conversational-voice/verification.md`; prior dirty-working-tree diagnostics do not complete this task
- [x] T169 Run locked ESLint plus web Playwright fake-media, accessibility, permission, reconnect, takeover, no-external-asset, and Istanbul coverage production from `tooling/web-ci/tests/voice-conversation-065.spec.js`, recording report digests and results in `specs/065-conversational-voice/verification.md`
- [x] T170 Run the full Windows PySide suite offscreen on macOS with its coverage XML producer as diagnostic-only evidence and record the report digest and native-audio limitation in `specs/065-conversational-voice/verification.md`
- [ ] T171 Build one exact locked Windows EXE, verify packaged LiveKit/QtMultimedia artifacts and offline startup, then run native microphone/speaker/AEC/barge-in against staging and record Windows-native evidence in `specs/065-conversational-voice/verification.md`
- [ ] T172 Run Android ktlint/lint/core/app/Kover/assemble/connected gates, export app/core Kover XML, and run physical route/headset/Bluetooth/AEC/barge-in checks from `specs/065-conversational-voice/quickstart.md`, recording report digests and emulator versus physical evidence in `specs/065-conversational-voice/verification.md`
- [ ] T173 Run strict recursive swift-format, AstralCore tests, iOS/macOS/watchOS unit/UI tests, unsigned builds, export all three xccov JSON reports, and run physical Mac/iPhone/Watch acoustic checks from `specs/065-conversational-voice/quickstart.md`, recording report digests and simulator versus physical evidence in `specs/065-conversational-voice/verification.md`
- [ ] T174 Build and start the exact backend, voice-worker, test-worker, PostgreSQL, Keycloak, and digest-pinned LiveKit Compose topology; verify health/readiness, host-reachable WSS/ICE/TURN, exact speech profile, typed fallback, production exit-78 posture, and the `voice-warm-standard` network/profile measurement preconditions in `specs/065-conversational-voice/verification.md`
- [ ] T175 Execute the local real-browser/PySide/Android/iOS/macOS/watchOS voice journey and all denial/degradation/PHI/navigation/takeover cases from `specs/065-conversational-voice/quickstart.md`, stopping at each Keycloak screen for user login and recording only post-login evidence in `specs/065-conversational-voice/verification.md`
- [ ] T176 Under the `voice-warm-standard` profile, run five unscored warm-ups then at least 100 eligible trials per client with nearest-rank calculations for activation p95 <=3 seconds, acknowledgement p95 <=1.5 seconds, recap p95 <=2 seconds, exactly one acknowledgement, 100% no-gap-over-20.0-seconds, local audible interruption silence p95 <=500 milliseconds and 100% <=1 second, synchronous explicit-stop fencing before network acknowledgement, and honest reconnect before the actual 45-second launch lease expires; exercise readiness/egress deadlines and byte ceilings, 2/19/21/65-second and multi-minute timing, two-turn arbitration, second-opening, five-user 30-minute soak, and capacity scenarios with failures retained in denominators and waveform bytes discarded, recording only non-content measurements in `specs/065-conversational-voice/verification.md`
- [ ] T177 Inspect representative databases, container/client filesystems, logs, metrics, traces, audit rows, protocol captures, screenshots, and crash artifacts for zero audio/credential/token/provider-body/transcript-copy/recap-content retention in `specs/065-conversational-voice/verification.md`
- [ ] T178 Complete the fixed synthetic/non-PHI recap review matrix—20 authoritative-summary successes, 20 fallback successes, 20 failures, 15 refusals, 10 cancellations, and 15 sensitive results—with >=95% rubric correctness, zero fabricated progress, and zero pre-consent disclosure; then run at least five independent blinded raters over at least 30 balanced `af_heart` clips and require mean naturalness/clarity >=4.0/5 plus no back-to-back duplicate progress phrase in `specs/065-conversational-voice/verification.md`
- [ ] T179 Provision and run the immutable candidate in qualifying external staging with real Keycloak, representative migrated PostgreSQL data, workers, exact speech inventory, public WSS/ICE/TURN, every client, the `voice-warm-standard` profile, timing/soak/privacy checks, and candidate-bound backend/worker/LiveKit/config/client digests recorded in `specs/065-conversational-voice/verification.md`
- [ ] T180 After Spec 060 T120 closes all four native GitHub trust gaps, run `scripts/check_changed_coverage.py --fail-under 90` over the immutable candidate's backend/tooling/Windows Python, web Istanbul, Android app/core Kover, and iOS/macOS/watchOS xccov reports; prepare and install the matching owner-reviewed protected-policy uptake through the existing pinned installation flow; and verify `.github/workflows/release-readiness.yml` keeps that protected verifier/publisher/native-token/exception-debt authority independent of candidate code and free of a repository-scoped GitHub App or custom broker, recording the combined result, installed identity, review, and evidence digests in `specs/065-conversational-voice/verification.md`. The four prerequisites are a distinct non-self environment reviewer, rollback-compatible protected disposable-tag policy, trusted-only signer-eligible tag creation or an equivalent deployed immutable-workflow verifier binding, and uniquely labeled protected publisher hosts with an independent orphan reaper across cancellation/runner loss/host restart/stale lease
- [x] T181 [P] Add web, Windows, Android, macOS/iOS, and watchOS regressions proving initial/hydrated terminal success leaves no `Completed` residue or busy indicator, terminal errors remain prominent without activity chrome, local/nonterminal work stays busy, and one operation's completion preserves another active operation in `backend/tests/test_client_js_contract.py`, `backend/tests/webrender/test_accessibility_060.py`, `tooling/web-ci/tests/continuity-contract-060.spec.js`, `windows-client/tests/test_status_lifecycle_060.py`, `android-client/app/src/test/kotlin/com/personalailabs/astraldeep/app/StatusLifecycleTest.kt`, `android-client/app/src/test/kotlin/com/personalailabs/astraldeep/app/ConversationContinuityTest.kt`, `android-client/app/src/test/kotlin/com/personalailabs/astraldeep/app/ui/AppViewModelReducerTest.kt`, `apple-clients/AstralApp/AstralAppTests/StatusLifecycleTests.swift`, and `apple-clients/AstralWatchTests/StatusLifecycleTests.swift`
- [x] T182 Implement nonterminal-only working presentation while retaining canonical terminal reconciliation state across web, Windows, Android, macOS/iOS, and watchOS in `backend/webrender/static/client.js`, `windows-client/astral_client/app.py`, `android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/ui/AppViewModel.kt`, `android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/ui/AdaptiveShell.kt`, `apple-clients/AstralApp/AstralApp/AppModel.swift`, `apple-clients/AstralApp/AstralApp/Views/ChatView.swift`, `apple-clients/AstralWatch/WatchModel.swift`, and `apple-clients/AstralWatch/Views/WatchChatView.swift`
- [ ] T183 Rebuild the current local backend and affected clients, then live-check first-load/idle, active-work, successful-terminal, and terminal-failure presentation on every Mac-hosted client available without credential automation; record exact results and remaining Windows-native/physical-device limitations in `specs/065-conversational-voice/verification.md`
- [x] T184 [P] Add worker, coordinator, repository, bootstrap, observability, schema, and runtime-to-media regressions proving an exact assistant-playback echo is fenced after authenticated playout, abandoned before transcript proof/dispatch with durable `malformed_final`/`none` and no visible retry frame, stores no speech text, admits different speech, expires its bounded fingerprint, preserves immediate authenticated explicit barge-in, and fails stale owner/assignment/generation/timeout/tail events closed in `backend/voice_agent/tests/test_barge_in_065.py`, `backend/voice_agent/tests/test_worker_runtime_integration_065.py`, `backend/orchestrator/tests/test_voice_coordinator_065.py`, `backend/orchestrator/tests/test_voice_bootstrap_065.py`, `backend/orchestrator/tests/test_voice_media_065.py`, and `backend/orchestrator/tests/test_voice_sessions_065.py`
- [x] T185 Implement a 500-millisecond generation-fenced post-playout capture guard plus an ephemeral bounded exact-speech SHA-256 suppression window, dedicated content-free `self_speech_suppressed` control/audit handling with no client retry frame, teardown cleanup, and an authenticated explicit-barge-in path that bypasses only the acoustic tail in `backend/voice_agent/session.py`, `backend/voice_agent/control.py`, `backend/orchestrator/voice_coordinator.py`, `backend/orchestrator/voice_sessions.py`, `backend/orchestrator/voice_bootstrap.py`, `backend/orchestrator/voice_runtime.py`, `backend/orchestrator/voice_media.py`, `backend/orchestrator/runtime_observability.py`, and `specs/065-conversational-voice/contracts/worker-control.schema.json`
- [x] T186 [P] Add a hosted macOS transcript-churn regression plus web, Windows, Android, macOS/iOS, and watchOS terminal-before-snapshot regressions proving a successful lifecycle terminal settles activity without erasing the transient answer or canvas before the authoritative snapshot in `apple-clients/AstralApp/AstralAppTests/AppModelLiveCanvasTests.swift`, `apple-clients/AstralApp/AstralAppTests/StatusLifecycleTests.swift`, `apple-clients/AstralWatchTests/StatusLifecycleTests.swift`, `android-client/app/src/test/kotlin/com/personalailabs/astraldeep/app/ConversationContinuityTest.kt`, `tooling/web-ci/tests/continuity-contract-060.spec.js`, and `windows-client/tests/test_status_lifecycle_060.py`
- [x] T187 Implement macOS-only eager transcript placement with deferred add-only autoscroll, preserve successful transient Apple/Android content until the authoritative snapshot, and continue clearing failed/refused/cancelled optimistic content in `apple-clients/AstralApp/AstralApp/Views/ChatView.swift`, `apple-clients/AstralApp/AstralApp/AppModel.swift`, and `android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/ui/AppViewModel.kt`
- [x] T188 Rebuild the exact backend, voice worker, and macOS app, then run one authenticated real-speech successful request proving bounded Mac CPU/RSS and lease renewal through commit, exactly one accepted ordinary user submission/operation despite local assistant playout, and an audible full `af_heart` recap; record only content-free IDs/counts/timings and retain Windows-native/physical-device limitations in `specs/065-conversational-voice/verification.md`
- [x] T189 Implement and statically verify the candidate-side release-evidence plumbing for voice-runtime/client identities and all ten coverage producer slots in `Makefile`, `scripts/prepare_release_evidence.py`, `scripts/check_changed_coverage.py`, the release schemas, and candidate workflows. This records authored plumbing only; it asserts neither a successful pre-push run nor installed protected authority
- [x] T190 Implement and locally verify the RTC-only worker closure inputs: exact pins and hash lock, Silero model/license/provenance, runtime/test Docker targets, canonical inventory, and RTC audit. This is local implementation readiness only and does not set `t004_complete`, approve export, or claim a final distribution fingerprint

**Final checkpoint**: Every FR/SC has automated or candidate-bound live evidence; all clients and
packaging gates pass honestly; exact dependency approval and staging exist; local evidence remains
diagnostic; no login, Windows-native, physical-acoustic, or staging result is inferred from a Mac
simulator or mock.

---

## Dependencies and Execution Order

### Phase dependencies

- **Setup (Phase 1)**: Started immediately. T004–T010 require the approval decision in T002.
  T013/T024 completed the authorized `064.001` handoff integration; future schema and genuinely
  overlapping client/project-file edits must preserve that exact predecessor. T189 records the
  completed local evidence machinery; T003 regenerates all ten reports and runs it against one clean
  committed candidate before the next requested push. T190's completed exact local pins, inventory,
  locks, images, and audit are the local implementation prerequisite. T004 consumes T168's immutable
  candidate evidence plus T180's installed protected-policy identity and remains the non-waivable
  final merge/distribution approval; neither local task turns diagnostic tests into release evidence.
- **Foundational (Phase 2)**: Depends on the locally buildable Setup outputs and recorded RTC-only
  approval. Non-schema tests/contracts/services may proceed while T004's external final-closure
  gate remains open; T013/T024 already integrated and tested the authorized `064.001` predecessor. The completed foundational
  phase blocks every user story because all stories use the same dispatcher, persistence, control
  binding, contracts, and media-only worker boundary.
- **US1 (Phase 3)**: Starts after Foundational and provides the smallest user-visible media/session
  slice across all six clients.
- **US2 (Phase 4)**: Starts after Foundational; full live integration uses US1 media, but its
  authority/idempotency path is independently testable with synthetic transcript envelopes.
- **US3 (Phase 5)**: Starts after Foundational; fake lifecycle/committed-result events make it
  independently testable, while its full journey integrates with US2 acceptance/publication.
- **US5 (Phase 6)**: Starts after Foundational in parallel with US1–US3; it hardens included-service
  readiness, isolation, and failure posture before any production claim.
- **US6 (Phase 7)**: Starts after Foundational in parallel; complete live parity integrates the
  behavior delivered by US1–US3 and US5.
- **US4 (Phase 8, P2)**: Follows all P1 story phases because interruption/recovery exercises their
  combined session, dispatch, speech, readiness, and client state machines.
- **Polish (Phase 9)**: Depends on every story selected for delivery. The T181–T188 follow-up
  regressions and live repair must close before the affected candidate gates are repeated; T003
  gates the next requested push, while T179 qualifying staging, Spec 060 T120 native trust
  bootstrap, T180 combined all-language coverage/protected-policy uptake, and T004 exact dependency
  approval remain non-waivable before merge.

### User-story dependency graph

```text
Setup -> Foundational
Foundational -> US1
Foundational -> US2 -> US3
Foundational -> US5
Foundational -> US6
US1 + US2 + US3 + US5 + US6 -> US4
US1 + US2 + US3 + US4 + US5 + US6 -> Polish/Staging
```

### Within each user story

1. Add the listed test/fixture tasks and verify they fail for the intended missing behavior.
2. Implement models/repositories and deterministic services before API or client integration.
3. Implement shared protocol/contract behavior before per-client adapters.
4. Run the independent checkpoint before advancing to dependent phases.
5. Never use a later live result to waive a failed deterministic, authorization, schema, or
   cross-client contract test.

---

## Parallel Opportunities

- Setup lock/vendor/validator tasks T005–T010 and local closure T190 can proceed in parallel after
  T001–T002 because they touch separate dependency surfaces; T189 supplies the collector, T003 gates
  the next requested push, and T004 remains protected merge/distribution work after T168 and T180.
- Foundational failure-first tests T011–T022 can proceed in parallel; implementation tasks then
  follow their named test seams and shared database/dispatcher ordering.
- US1 platform tests T039–T044 and platform adapters T050–T059 can proceed in parallel after the
  server/worker session contract is stable.
- US2 proof/dispatch/new-chat/concurrency/delete tests T062–T068 can proceed in parallel; client
  transcript adapters T078–T082 can proceed in parallel after the shared validator is implemented.
- US3 cadence/recap/sensitivity/worker/fixture tests T085–T091 can proceed in parallel; direct
  client playout adapters T100–T105 can proceed in parallel after the manifest contract is stable.
- US5 readiness/env/egress/legacy/grant/isolation/retention tests T107–T113 can proceed in parallel;
  redaction and metrics tasks T121/T122 are independent after their shared vocabulary exists.
- US6 client test suites T127–T131 and client implementations T136–T141 can proceed in parallel
  against the one canonical fixture and manifest.
- US4 lifecycle tests T147–T154 and platform recovery adapters T158–T162 can proceed in parallel
  after grant/context semantics are fixed.

## Parallel Example: User Story 1

```text
Task T039: Web activation and media-permission tests
Task T040: Windows activation and QtMultimedia tests
Task T041: Android activation and runtime-permission tests
Task T042/T043: Shared Apple plus iOS/macOS controller tests
Task T044: watchOS bridge activation tests
```

## Parallel Example: User Story 2

```text
Task T062: Transcript proof golden/negative tests
Task T063: Typed-versus-voice authorization parity tests
Task T065: Correlated no-chat activation tests
Task T066: Same-chat concurrency/publication tests
Task T067: Deletion/revocation/tombstone tests
```

## Parallel Example: User Story 3

```text
Task T085: Fake-clock cadence tests
Task T087: Authoritative/fallback recap tests
Task T088: Sensitive-result consent tests
Task T090: Worker quantum and exact-TTS tests
Task T091: Cross-client announcement/playout fixture vectors
```

## Parallel Example: User Story 5

```text
Task T107: Exact-profile readiness tests
Task T108: Environment/LLM isolation tests
Task T109: Strict fake speech-service tests
Task T111: Least-privilege grant tests
Task T113: Zero-retention inspection tests
```

## Parallel Example: User Story 6

```text
Task T127: Web C0–C6 conformance
Task T128: Windows C0–C6 conformance
Task T129: Android C0–C6 conformance
Task T130: iOS/macOS C0–C6 conformance
Task T131: watchOS C0–C6 conformance
```

## Parallel Example: User Story 4

```text
Task T149: Web lifecycle/reconnect tests
Task T150: Windows lifecycle/reconnect tests
Task T151: Android lifecycle/reconnect tests
Task T152: iOS/macOS lifecycle/reconnect tests
Task T153: watchOS lifecycle/reconnect tests
```

---

## Implementation Strategy

### MVP first: User Story 1

1. Complete Setup and Foundational phases.
2. Implement US1 across all six clients with exact fixed-profile greeting and media permission.
3. Stop and run the US1 independent checkpoint; do not call it agent-integrated yet.
4. Add US2 normal dispatch and US3 truthful lifecycle/result speech before any broader demo claim.

### Incremental delivery

1. **Foundation**: approved locks + contracts + migration + normal dispatcher + isolated worker.
2. **US1**: explicit start/stop/listening/takeover/idle on all clients.
3. **US2**: exact-once transcript through normal agentic authority and concurrent publication.
4. **US3**: acknowledgement/progress/recap/sensitive consent.
5. **US5**: exact included-service readiness, isolation, and fail-closed posture.
6. **US6**: mechanical parity/accessibility closure for all six surfaces.
7. **US4**: barge-in, reconnect, lifecycle recovery, and crosstalk prevention.
8. **Polish**: full gates, real-login E2E, physical/native evidence, soak, qualifying staging, and
   protected release evidence.

### Safe stopping points

- After T035: shared foundation is testable without microphone capture.
- After T061: basic voice session UX is independently demonstrable but not agent-integrated.
- After T084: recognized text is safely agent-integrated with exact-once dispatch.
- After T106: deterministic lifecycle speech and recap contracts pass.
- After T146: every client consumes one shared contract with no ignored required behavior.
- After T163: full conversational recovery behavior is implemented.
- After T188: the observed local idle/ordering/self-speech/macOS-recap defects have a repeatable
  live checkpoint; T003–T004, T061, T168, T171–T180, and T183 still require the exact
  pre-push/dependency/native/staging/presentation evidence named by those tasks.

## Notes

- `[P]` means different files and no dependency on another incomplete task in that phase.
- User-story labels map exactly to `spec.md`; P1 stories are scheduled before P2 US4.
- Tests must fail for the intended reason before implementation and cover golden, denial, malformed,
  race, timeout, cancellation, duplicate, reconnect, PHI, redaction, and cleanup paths.
- Feature 064 remains separately owned; integrate only its authorized handoff before schema or
  overlapping client/project-file edits, and never guess its final schema predecessor.
- Do not edit or regenerate `apple-clients/AstralApp/AstralApp.xcodeproj/project.pbxproj` without
  preserving the owner’s existing local change.
- No task authorizes end-user speech settings, environment-backed LLMs, a worker dispatch endpoint,
  raw audio retention, an upstream LiveKit fork, or platform speech as parity.
- Pause at every real Keycloak login boundary and let the user authenticate.
- Local Compose, Mac PySide, and simulator audio are diagnostic, not Windows-native, physical
  acoustic, or qualifying external staging evidence.
- T189 establishes the deterministic local evidence collector. Complete T003 against one clean
  committed candidate before the next requested implementation push; future requested pushes
  require a fresh equivalent run. Its output remains diagnostic and cannot authorize release.
- Commit/push/release only on explicit user request; protected CI remains merge/release authority.
