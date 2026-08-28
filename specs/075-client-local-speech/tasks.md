# Tasks: Client-Local Conversational Speech

**Input**: Design documents from `specs/075-client-local-speech/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`

**Tests**: Required. Every behavior change is test-first; observe the narrow test fail for the
intended reason before implementation. Every changed language/lane must retain at least 90% changed-
line coverage.

**Repository labels in descriptions**: `Deep` = `/Users/sam/Desktop/Work/AstralDeep`, `Plane` =
`/Users/sam/Desktop/Work/AstralPlane`, `Projection` = `/Users/sam/Desktop/Work/AstralProjection`,
and `wiki` = `/Users/sam/Desktop/Work/kos-wiki`.

## Phase 1: Setup and recoverable ownership

**Purpose**: Establish exact local branches/baselines without starting hosted CI or overwriting
unrelated work.

- [ ] T001 Re-fetch and revalidate clean `origin/main` ownership, remote feature-number uniqueness, submodule anchors, and current status in Deep `.git/HEAD`, Plane `.git/HEAD`, Projection `.git/HEAD`, Primitives `.git/HEAD`, LETS `.git/HEAD`, and wiki `.git/HEAD`; stop on newly ambiguous ownership
- [ ] T002 [P] Create local unpushed `codex/075-client-local-speech` branches from refreshed `origin/main` in Plane `.git/HEAD` and Projection `.git/HEAD`, recording exact baselines in `specs/075-client-local-speech/quickstart.md`
- [ ] T003 [P] Add a dependency-drift assertion for Feature 075 to Deep `backend/tests/test_voice_dependency_locks_065.py` and Projection `tests/test_protocol.py`, proving no new third-party runtime/model package is introduced
- [ ] T004 Inventory the obsolete Deep root `android-client/`, `apple-clients/`, root `.gitignore`, and `.git/info/exclude` paths without moving/deleting anything; record the exact recoverable cleanup targets in `specs/075-client-local-speech/quickstart.md`

---

## Phase 2: Foundational contracts and persistence

**Purpose**: Freeze the shared wire vocabulary and qualify honest persistence before any local
conversation story. This phase blocks all user stories.

### AstralPlane 075.001 — tests before migration

- [ ] T005 [P] Add failing backend/transport/immutability/idempotency record tests in Plane `tests/repositories/test_voice_records.py` and `tests/repositories/test_voice.py` for exact `llm_factory|client_local` combinations and exhaustive rejection of mixed local/remote fields
- [ ] T006 [P] Add failing representative `074.004`→`075.001`, backfill, preserved-turn, idempotence, wrong-predecessor, injected-rollback, and structural-drift tests in Plane `tests/integration/test_pre_split_upgrade.py`
- [ ] T007 [P] Add failing fresh-database, parallel-start, digest-verifier, repeat-run, and forward-recovery tests for `075.001` in Plane `tests/integration/test_empty_database_startup.py`, `tests/test_schema_migrations.py`, and `tests/test_revision.py`
- [ ] T008 Implement guarded `PLANE_SCHEMA_075_STATEMENTS`/migration registry entry, exact predecessor verification, nullable-add/backfill/NOT-NULL sequence, verified constraint replacement, conditional remote-field nullability, exhaustive `voice_session_speech_backend_075_check`, and current structural digest in Plane `src/astralplane/database/migrations.py`
- [ ] T009 Update Plane current predecessor/revision metadata and recognized baselines to `074.004`/`075.001` in `src/astralplane/database/revision.py`, `src/astralplane/database/baseline.py`, and `src/astralplane/__init__.py` without rewriting historic migration bytes/digests
- [ ] T010 Implement immutable `speech_backend` and backend-discriminated insert/read validation with no transcript/audio/capability fields in Plane `src/astralplane/repositories/voice.py`
- [ ] T011 Document quiesce/backup/upgrade/remote-smoke/local-profile/restore-or-forward-repair procedure in Plane `docs/migration-and-recovery.md`
- [ ] T012 Run the narrow Plane repository/revision/migration suites, demonstrate their red→green sequence, and create one local qualified Plane candidate commit covering `src/astralplane/`, `tests/`, and `docs/migration-and-recovery.md` without pushing

### Shared Projection contract — tests before manifest

- [ ] T013 [P] Add failing strict schema-v2 resource, unchanged-remote-v1, extra-key, disposition, every-consumer, and native-CI-path-filter tests in Projection `tests/test_protocol.py`, `tests/test_resources.py`, `tests/webrender/test_voice_renderer_065.py`, and `tests/ci/test_workflows.py`
- [ ] T014 Add canonical supported/unavailable/stale/denial/local-final/announcement/playout vectors in Projection `contracts/fixtures/voice_075/client_local_conformance.json`, separate from `contracts/fixtures/voice_065/client_conformance.json`
- [ ] T015 Update Projection `contracts/ui_protocol.json` with REST-v2 requirements, `client_local/v1`, exact local frame fields, `speech_revision`, closed reasons/dispositions, and a remote-v1 byte invariant; add exact `contracts/fixtures/voice_075/client_local_conformance.json` push/PR filters to `.github/workflows/android-ci.yml` and `.github/workflows/apple-ci.yml`
- [ ] T016 [P] Add failing half-duplex/local-unavailable/typed-fallback ROTE tests in Projection `tests/rote/test_voice_rote_capabilities_065.py`, `tests/rote/test_android_profile.py`, `tests/rote/test_apple_profiles.py`, and `tests/rote/test_windows_profile.py`
- [ ] T017 Implement local capability/disposition adaptation and typed fallback in Projection `backend/rote/capabilities.py`, `backend/rote/adapter.py`, and `backend/rote/fallback.py`

### Deep contract and exact component pins

- [ ] T018 [P] Add failing Draft-2020-12/OpenAPI/golden-vector/strict-extra-key/local-v2/remote-v1 contract tests in Deep `backend/tests/test_voice_contract_075.py` and `backend/tests/test_voice_protocol_065.py`
- [ ] T019 Add failing exact Plane `075.001` commit/revision/migration-digest pin assertions in Deep `backend/tests/test_voice_migration_065.py`, `backend/tests/test_schema_revision_guard.py`, and `scripts/tests/test_verify_composition.py`
- [ ] T020 Advance Deep `components/AstralPlane` plus Plane commit/schema/migration digest in `config/astral-composition.json` only to the exact locally qualified T012 candidate; change no Deep SQL or database pool
- [ ] T021 Run Deep `backend/tests/test_voice_migration_065.py`, `backend/tests/test_schema_revision_guard.py`, `scripts/tests/test_verify_composition.py`, `backend/tests/test_voice_contract_075.py`, and `backend/tests/test_voice_protocol_065.py` against the exact local Plane object and keep the Projection gitlink/hash unchanged until its later standalone qualification

**Checkpoint**: Plane `075.001`, the shared local wire contract, ROTE reasons, and Deep's exact Plane
pin are test-green locally. No product branch has been pushed.

---

## Phase 3: User Story 1 — Hold a conversation using local speech (Priority: P1) 🎯 MVP

**Goal**: An eligible client can start a half-duplex `client_local` session, submit one bound local
final through the ordinary dispatcher, and speak only current server-authorized text with no remote
speech media/service.

**Independent Test**: With every remote speech endpoint blocked, a supported web client completes
two turns through real authenticated session/ordinary dispatcher fakes, starts greeting within the
defined fence, sends zero audio bytes off-device, and preserves typed chat.

### Selector and local session admission

- [ ] T022 [P] [US1] Add failing missing/default, exact-value, blank/malformed, parse-once, no-client-override, no-worker-construction, typed-chat-preservation, and v1-safe-upgrade tests in Deep `backend/orchestrator/tests/test_voice_backend_selection_075.py`, `backend/orchestrator/tests/test_voice_bootstrap_065.py`, and `backend/tests/test_voice_session_api_065.py`
- [ ] T023 [US1] Implement immutable `VOICE_SPEECH_BACKEND` parsing and backend-neutral strategy selection in Deep `backend/orchestrator/voice_backend.py`, `backend/orchestrator/voice_bootstrap.py`, and `backend/orchestrator/voice_api.py`
- [ ] T024 [P] [US1] Add failing authenticated/no-store/rate-limit/strict-capability/create/takeover/header-binding/idempotence/no-media/backend-mismatch tests in Deep `backend/tests/test_voice_v2_api_075.py` and `backend/orchestrator/tests/test_voice_local_session_075.py`
- [ ] T025 [US1] Implement exact v2 Pydantic models and `/api/voice/v2/capability`, `/api/voice/v2/status`, `/api/voice/v2/sessions`, and takeover routes in Deep `backend/orchestrator/voice_api.py`
- [ ] T026 [US1] Implement backend-aware local session creation/takeover/state projection with no room/participant/worker/grant in Deep `backend/orchestrator/voice_runtime.py`, `backend/orchestrator/voice_sessions.py`, and `backend/orchestrator/voice_media.py`

### Local WebSocket binding and ordinary dispatch

- [ ] T027 [P] [US1] Add failing parser tests for every `voice_local_*` frame, missing/extra keys, UUID/locale/digest/byte bounds, stale/out-of-order sequences, and remote-v1 isolation in Deep `backend/tests/test_voice_local_protocol_075.py`
- [ ] T028 [US1] Implement strict schema-v2 local frame types/parser and top-level routing in Deep `backend/shared/protocol.py` and `backend/orchestrator/orchestrator.py`
- [ ] T029 [P] [US1] Add failing current-socket/device/server-held-control/session/revision/chat/context/foreground/mute/binding-expiry tests for ready/start/bind/failure in Deep `backend/tests/test_voice_control_binding_integration_065.py` and `backend/orchestrator/tests/test_voice_local_session_075.py`
- [ ] T030 [US1] Implement `voice_local_ready`→`voice_local_session_ready` and recognition-start→turn-bound lifecycle with two-minute expiry in Deep `backend/orchestrator/voice_control_binding.py`, `backend/orchestrator/voice_sessions.py`, and `backend/orchestrator/orchestrator.py`
- [ ] T031 [P] [US1] Add failing canonicalization, digest, exact replay, altered replay, empty/oversize/control, stale/cross-owner/device/chat, deletion/takeover/logout/end, and capacity tests in Deep `backend/tests/test_voice_local_admission_075.py`, `backend/tests/test_voice_submission_065.py`, and `backend/tests/test_voice_multiuser_isolation_065.py`
- [ ] T032 [US1] Implement sibling `admit_local_transcript` returning existing `TranscriptAdmission` without weakening worker-HMAC `admit_transcript` in Deep `backend/orchestrator/voice_sessions.py`
- [ ] T033 [P] [US1] Add failing permission/PHI/confirmation/tool/LLM-selection/audit/commit/cancellation parity and no-direct-agent/write tests in Deep `backend/tests/test_voice_dispatch_parity_065.py` and `backend/tests/test_voice_admission_065.py`
- [ ] T034 [US1] Route admitted local finals through the exact ordinary `handle_chat_message` path and correlated ack/rejection handling in Deep `backend/orchestrator/orchestrator.py`

### Authorized local announcement and cleanup

- [ ] T035 [P] [US1] Add failing server-policy-only, 600-byte/10-second, greeting-null-turn, mute/consent/foreground revision, ordered-outcome, and post-acceptance-TTS-failure tests in Deep `backend/tests/test_voice_local_announcement_075.py`, `backend/tests/test_voice_announcements_065.py`, and `backend/tests/test_voice_playout_runtime_065.py`
- [ ] T036 [US1] Implement policy-derived `voice_local_announcement`, monotonic mute/consent fences, and distinct content-free `voice_local_playout_event` in Deep `backend/orchestrator/voice_coordinator.py`, `backend/orchestrator/voice_runtime.py`, `backend/orchestrator/voice_sessions.py`, and `backend/orchestrator/orchestrator.py`
- [ ] T037 [P] [US1] Add failing immediate-stop, background/interruption/mute/end/takeover/logout/reconnect, abandoned-preacceptance-turn, no-worker/no-egress, no-retention, and 500-ms-echo-contract tests in Deep `backend/tests/test_voice_local_cleanup_075.py`, `backend/tests/test_voice_lifecycle_cleanup_065.py`, and `backend/tests/test_voice_zero_retention_065.py`
- [ ] T038 [US1] Implement backend-local lifecycle shutdown/cleanup and buffer fences in Deep `backend/orchestrator/voice_runtime.py`, `backend/orchestrator/voice_bootstrap.py`, and `backend/orchestrator/orchestrator.py`

### Web MVP adapter

- [ ] T039 [P] [US1] Add failing web journeys for ready/download/install/final/stale-announcement/TTS-error/mute/stop/hidden/echo/typed-fallback/blocked-egress in Projection `tooling/web-ci/tests/voice-conversation-065.spec.js`
- [ ] T040 [P] [US1] Add failing static safety assertions for unprefixed API, `processLocally=true`, positive `available(en-US)`, matching `localService=true`, no `webkitSpeechRecognition`, no cloud fallback, and no local-mode LiveKit/media grant in Projection `tests/webrender/test_voice_renderer_065.py`
- [ ] T041 [US1] Implement the isolated local Web Speech recognition/synthesis state machine, explicit install gesture, local interim display, bound-final submission, serialized playout, and immediate cleanup in Projection `backend/webrender/static/client.js`
- [ ] T042 [US1] Add accessible probing/install/unavailable/typed-fallback copy only where needed in Projection `backend/webrender/templates/shell.html`, `backend/webrender/voice.py`, and `tests/chrome/test_voice_composer_model.py`
- [ ] T043 [US1] Add and pass the blocked-speech-endpoints two-turn web/Deep integration journey in Deep `backend/tests/test_voice_client_conformance_065.py` and Projection `tooling/web-ci/tests/voice-conversation-065.spec.js`

**Checkpoint**: The web MVP works independently in `client_local`, remote speech is blocked with
zero calls, local final dispatch is ordinary/authenticated, and remote v1 remains unchanged.

---

## Phase 4: User Story 2 — Select and roll back the speech pipeline safely (Priority: P1)

**Goal**: Operators can select either backend only through deployment configuration, malformed
selection fails voice closed, sessions never switch in flight, and rollback needs no data rewrite.

**Independent Test**: Start separate candidates in each exact selector posture, prove capability and
session behavior, drain/end a local session, restart as Factory, and complete remote smoke with the
same conversations and no client override.

- [ ] T044 [P] [US2] Add failing process-lifetime immutability, active-session backend mutation denial, selector-restart, legacy v1 local-unavailable, and no-data-rewrite tests in Deep `backend/orchestrator/tests/test_voice_backend_selection_075.py` and `backend/tests/test_voice_v2_api_075.py`
- [ ] T045 [US2] Implement backend immutability/drain/end guards and v1-safe `client_contract_upgrade_required` projection in Deep `backend/orchestrator/voice_backend.py`, `backend/orchestrator/voice_api.py`, and `backend/orchestrator/voice_sessions.py`
- [ ] T046 [P] [US2] Add selector/kill-switch/typed-fallback/environment-isolation configuration tests in Deep `backend/tests/test_voice_env_isolation_065.py` and `backend/tests/test_voice_deployment_topology_065.py`
- [ ] T047 [US2] Document `VOICE_SPEECH_BACKEND`, default/malformed behavior, restart/drain procedure, no client override, and rollback smoke in Deep `.env.example`, `docker-compose.yml`, and tracked `docs/production-deployment.md` without exposing speech credentials/endpoints
- [ ] T048 [US2] Add and pass a local→remote→local restart/rollback integration with preserved conversations and no schema rewrite in Deep `backend/tests/test_voice_backend_rollback_075.py`

**Checkpoint**: Both profiles are independently selectable/restartable and rollback is operationally
documented/tested without hidden fallback or conversation loss.

---

## Phase 5: User Story 3 — Use local speech across shipping clients (Priority: P1)

**Goal**: Web, Windows, Android, iOS, macOS, and watchOS implement the same strict contract; each
eligible runtime uses local-only ASR/TTS and every ineligible runtime gives a stable typed fallback.

**Independent Test**: Every client consumes the canonical fixture and passes supported,
unsupported, permission, locale/asset, stale-frame, announcement, mute/stop/interruption, and typed-
fallback journeys; physical-device evidence is collected later as a distinct release gate.

### Shared native protocol consumers

- [ ] T049 [P] [US3] Add failing Apple v2 fixture/parser/builder/disposition and unchanged remote-`VoiceOrigin` tests in Projection `apple-clients/AstralCore/Tests/AstralCoreTests/VoiceContract065Tests.swift` and `apple-clients/AstralCore/Tests/AstralCoreTests/ManifestDriftTests.swift`
- [ ] T050 [US3] Implement Apple local capability/session/frame parsing, local-final builder, and closed dispositions in Projection `apple-clients/AstralCore/Sources/AstralCore/Protocol/Voice.swift`, `Frames.swift`, and `Dispositions.swift`
- [ ] T051 [P] [US3] Add failing Android v2 fixture/parser/builder/disposition and unchanged remote-v1 tests in Projection `android-client/core/src/test/kotlin/com/personalailabs/astraldeep/core/protocol/VoiceContract065Test.kt` and `ProtocolManifestTest.kt`
- [ ] T052 [US3] Implement Android local protocol models/parsers/manifest mapping in Projection `android-client/core/src/main/kotlin/com/personalailabs/astraldeep/core/protocol/Wire.kt`, `Messages.kt`, and `ProtocolManifest.kt`
- [ ] T053 [P] [US3] Add failing Windows v2 local-final/frame/disposition and unchanged remote-proof tests in Projection `windows-client/tests/test_voice_contract_065.py` and `windows-client/tests/test_protocol_manifest.py`
- [ ] T054 [US3] Implement Windows strict local protocol/session/final/announcement models in Projection `windows-client/astral_client/protocol.py` and `protocol_manifest.py`

### Windows local adapter and package

- [ ] T055 [P] [US3] Add fake-helper/fake-TTS plus first-party helper unit tests for readiness, bounded PCM, final dedupe, half-duplex/500-ms fence, crash/stop/logout, and no file/secret/network leakage in Projection `windows-client/tests/test_local_speech_075.py` and `windows-client/asr-helper/tests/AstralSpeechHelper.Tests.csproj`
- [ ] T056 [US3] Define and test the length-bounded inherited-pipe helper protocol, deterministic warning-as-error product project/build/hash inputs, scrubbed environment, no listening socket, and no temporary audio in Projection `windows-client/asr-helper/PROTOCOL.md` and `windows-client/asr-helper/AstralSpeechHelper.csproj`; declare/lock owner-approved Microsoft test/coverage packages only in `windows-client/asr-helper/tests/AstralSpeechHelper.Tests.csproj` and `windows-client/asr-helper/tests/packages.lock.json`, with no production PackageReference or published test asset
- [ ] T057 [US3] Implement first-party System.Speech helper source/build metadata in Projection `windows-client/asr-helper/` and integrate bounded desktop-owned PCM in `windows-client/astral_client/voice.py`
- [ ] T058 [P] [US3] Add failing QtTextToSpeech/helper/plugin/frozen-package/typed-fallback and test-dependency-isolation probes in Projection `windows-client/tests/test_voice_package_065.py` and `windows-client/tests/test_voice_lifecycle_065.py`, proving the product project/publish/package imports no test package or DLL; add failing `windows_csharp` Cobertura/path/threshold/profile tests in Deep `scripts/tests/test_check_changed_coverage.py`
- [ ] T059 [US3] Implement local Qt TTS, helper lifecycle, announcement serialization, stop/fallback, PyInstaller collection, and the measured C# coverage producer in Projection `windows-client/astral_client/voice.py`, `windows-client/AstralDeep.spec`, and Deep `scripts/check_changed_coverage.py`

### Android local adapter

- [ ] T060 [P] [US3] Add deterministic API-26–32 typed-only, API-33 installed/pending/unavailable locale, recognizer final/error/destroy, TTS local-voice/error/done, echo-fence, and final-binding tests in Projection `android-client/app/src/test/kotlin/com/personalailabs/astraldeep/app/voice/VoiceSessionController065Test.kt`
- [ ] T061 [P] [US3] Add Android v2 UI/disposition/fixture tests in Projection `android-client/app/src/test/kotlin/com/personalailabs/astraldeep/app/ui/VoiceViewModel065Test.kt` and `android-client/app/src/test/kotlin/com/personalailabs/astraldeep/app/VoiceFixtureBundle065Test.kt`
- [ ] T062 [US3] Implement injected API-33 on-device recognizer support/install check, main-thread lifecycle/destroy, local non-network TextToSpeech, serialized announcements, and 500-ms fence in Projection `android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/voice/VoiceSessionController.kt`
- [ ] T063 [US3] Add tested recognition/TTS service visibility without cloud declarations in Projection `android-client/app/src/main/AndroidManifest.xml` while retaining minSdk 26 in `android-client/gradle/libs.versions.toml`
- [ ] T064 [US3] Add real-device local-only qualification hook with no cloud fallback in Projection `android-client/app/src/androidTest/kotlin/com/personalailabs/astraldeep/app/VoiceConversation065InstrumentedTest.kt`

### Apple iOS/macOS/watchOS local adapters

- [ ] T065 [P] [US3] Add fake authorization/on-device/locale/final/announcement-expiry/TTS-error/echo/interruption/background/route tests in Projection `apple-clients/AstralApp/AstralAppTests/VoiceSessionController065Tests.swift`
- [ ] T066 [US3] Implement injected Speech/AVAudioEngine/retained-AVSpeechSynthesizer local adapter separate from LiveKit in Projection `apple-clients/AstralApp/AstralApp/Voice/VoiceSessionController.swift`
- [ ] T067 [P] [US3] Add failing target-specific Speech framework, usage-description, privacy-manifest, and no-overdeclaration tests in Projection `apple-clients/AstralCore/Tests/AstralCoreTests/VoiceContract065Tests.swift` and `apple-clients/AstralApp/AstralAppTests/VoiceSessionController065Tests.swift`
- [ ] T068 [US3] Add Speech linkage and `NSSpeechRecognitionUsageDescription` only to invoking targets in Projection `apple-clients/AstralApp/Info.plist`, `apple-clients/AstralApp/WatchInfo.plist`, `apple-clients/AstralApp/AstralApp.xcodeproj/project.pbxproj`, `apple-clients/AstralApp/AstralApp/PrivacyInfo.xcprivacy`, and `apple-clients/AstralWatch/PrivacyInfo.xcprivacy`
- [ ] T069 [P] [US3] Add watch-local fake contract/adapter tests while retaining remote PCM regressions in Projection `apple-clients/AstralWatchTests/VoiceContract065Tests.swift` and `WatchVoiceBridge065Tests.swift`
- [ ] T070 [US3] Implement capability-gated watch-local recognition/synthesis in a new Projection `apple-clients/AstralWatch/WatchLocalSpeech.swift` without routing local mode through `WatchVoiceBridge.swift`

### Cross-client conformance

- [ ] T071 [US3] Make web, Windows, Android, Apple core, and watch tests consume Projection `contracts/fixtures/voice_075/client_local_conformance.json` and fail on any missing/ignored required frame or disposition
- [ ] T072 [P] [US3] Add Deep drift/parity assertions for every six-client disposition and local contract hash in `backend/tests/test_voice_client_conformance_065.py`, `backend/tests/test_ui_protocol_manifest.py`, and `backend/tests/test_projection_protocol_integration.py`
- [ ] T073 [US3] Run the narrow Projection contract/controller suites covering `tests/`, `tooling/web-ci/tests/`, `windows-client/tests/`, `android-client/app/src/test/`, and `apple-clients/`, then create one local qualified Projection candidate commit without pushing
- [ ] T074 [US3] Advance Deep `components/AstralProjection` plus exact UI contract hash in `config/astral-composition.json` only to the T073 qualified candidate, then pass focused Deep composition/drift tests

**Checkpoint**: All six clients classify and handle the same contract revision; unsupported devices
remain typed-only; local and remote media branches cannot be silently mixed.

---

## Phase 6: User Story 5 — Preserve privacy, authorization, and durable semantics (Priority: P1)

**Goal**: Local capability/text cannot gain authority, speech data is not retained, and all ordinary
conversation security/audit/commit semantics remain identical.

**Independent Test**: The complete negative matrix rejects stale/cross-owner/device/chat/control and
altered replay; blocked-egress/retention scans find none of the forbidden content; a valid final
still exercises permission, PHI, confirmation, tool, audit, cancellation, and commit paths.

- [ ] T075 [P] [US5] Expand cross-user/device/socket/control/session/chat/takeover/reconnect/replay denial tests in Deep `backend/tests/test_voice_multiuser_isolation_065.py`, `backend/tests/test_voice_control_binding_integration_065.py`, and `backend/tests/test_voice_local_admission_075.py`
- [ ] T076 [P] [US5] Expand permission/policy/PHI/confirmation/tool/LLM-selection/audit/cancellation/commit parity tests in Deep `backend/tests/test_voice_dispatch_parity_065.py` and `backend/tests/test_voice_admission_065.py`
- [ ] T077 [P] [US5] Add local blocked-egress and forbidden audio/interim/final/digest/engine/path/endpoint/credential/reasoning retention scans in Deep `backend/tests/test_voice_zero_retention_065.py` and `backend/tests/test_voice_env_isolation_065.py`, plus Projection `tests/webrender/test_voice_renderer_065.py`, `windows-client/tests/test_local_speech_075.py`, `android-client/app/src/test/kotlin/com/personalailabs/astraldeep/app/voice/VoiceSessionController065Test.kt`, `apple-clients/AstralApp/AstralAppTests/VoiceSessionController065Tests.swift`, and `apple-clients/AstralWatchTests/VoiceContract065Tests.swift`
- [ ] T078 [US5] Harden local admission/cleanup/redaction until T075–T077 pass without changing remote proof semantics in Deep `backend/orchestrator/voice_sessions.py`, `voice_runtime.py`, `runtime_observability.py`, and `orchestrator.py`
- [ ] T079 [P] [US5] Add content-free local capability/activation/recognition/submission/announcement/playout/interruption telemetry tests and forbidden-label checks in Deep `backend/tests/test_voice_telemetry_075.py` and `backend/tests/test_voice_observability_065.py`
- [ ] T080 [US5] Implement reviewed low-cardinality local timings/outcomes and v2 status projection in Deep `backend/orchestrator/runtime_observability.py`, `voice_bootstrap.py`, and `voice_api.py`
- [ ] T081 [US5] Verify representative Plane `voice_session`/`voice_turn` rows and audits contain only permitted backend/correlation/outcome metadata in Plane `tests/integration/test_pre_split_upgrade.py` and Deep `backend/tests/test_voice_zero_retention_065.py`

**Checkpoint**: Local speech is an untrusted-input adapter only; security, persistence, and privacy
negative tests are independently green.

---

## Phase 7: User Story 4 — Recover remote voice reliability and speed (Priority: P2)

**Goal**: The retained Factory path fails within one total deadline, admits workers only after real
inference plus first-phrase warm readiness, records useful content-free phase timing, and recovers
valid Windows/Android media grants without stale replay.

**Independent Test**: Deterministic clocks make a 15+15 second retry impossible; registration cannot
precede greeting/ack cache readiness; real recovered Factory inference passes; Windows/Android
recover once within the lease and reject stale grant/worker/turn data.

- [ ] T082 [P] [US4] Add monotonic-clock ASR/TTS shared-total-deadline, remaining-budget, fast-retry, cancellation, and redacted-log tests in Deep `backend/voice_agent/tests/test_speech_adapters_065.py`, `test_worker_latency_067.py`, and `test_endpoint_trim_066.py`
- [ ] T083 [US4] Replace per-attempt timeout multiplication with one injected monotonic operation deadline in Deep `backend/voice_agent/speech_adapters.py` while preserving `backend/shared/streaming_egress.py` bounds and adding no pool
- [ ] T084 [P] [US4] Add startup-order tests proving inventory+real ASR+real TTS, synchronous greeting/earliest-ack warm-up before registration, async remaining warm-up, and fail-closed warm failure in Deep `backend/voice_agent/tests/test_speech_preflight_065.py`, `test_preflight_recheck_066.py`, `test_tts_phrase_cache_066.py`, and `test_worker_runtime_integration_065.py`
- [ ] T085 [US4] Implement preflight/fixed-phrase warm ordering before `PoolClient.run_forever` in Deep `backend/voice_agent/main.py` and bounded cache behavior in `backend/voice_agent/speech_adapters.py`
- [ ] T086 [P] [US4] Add content-free configuration/preflight/warm-up/registration/connection-setup/recognition/synthesis/playout phase-timing tests in Deep `backend/tests/test_voice_telemetry_075.py` and `backend/orchestrator/tests/test_voice_status_066.py`
- [ ] T087 [US4] Implement remote phase metrics/status without identifiers, URLs, bodies, text, audio, or secrets in Deep `backend/orchestrator/runtime_observability.py`, `voice_bootstrap.py`, and `voice_api.py`
- [ ] T088 [P] [US4] Add failing Windows current-state/grant recovery, socket rotation, stale-worker/grant/session rejection, single-rejoin, and no-duplicate-final tests in Projection `windows-client/tests/test_voice_lifecycle_065.py`
- [ ] T089 [US4] Implement Windows current-state/grant recovery with one bounded rejoin and stale-proof rejection in Projection `windows-client/astral_client/voice.py` and `windows-client/astral_client/protocol.py`
- [ ] T090 [P] [US4] Add failing Android current-state/grant recovery, socket rotation, stale-worker/grant/session rejection, single-rejoin, and no-duplicate-final tests in Projection `android-client/app/src/test/kotlin/com/personalailabs/astraldeep/app/voice/VoiceSessionController065Test.kt`, `VoiceControlApi065Test.kt`, and `VoiceLiveKitPublicationReconciliation065Test.kt`
- [ ] T091 [US4] Implement Android current-state/grant recovery with one bounded rejoin and stale-proof rejection in Projection `android-client/app/src/main/kotlin/com/personalailabs/astraldeep/app/voice/VoiceSessionController.kt`
- [ ] T092 [US4] Add and pass the recovered-Factory preflight/warm greeting/ack/induced-total-timeout/recovery plus remote-v1 stale-proof/grant regression matrix in Deep `backend/tests/test_voice_deployment_topology_065.py`, `backend/tests/test_voice_grants_065.py`, and `backend/tests/test_voice_worker_closure_065.py`

**Checkpoint**: Remote v1 is preserved, OOM/unresponsive inference no longer creates multiplied
deadlines, the first greeting/ack is warm before admission, and affected clients recover safely.

---

## Phase 8: Polish, approved small fixes, cleanup, and qualification

**Purpose**: Finish repository hygiene, isolated small Apple debt, full local verification,
candidate-bound evidence, wiki checkpoints, and only then intentional product pushes/PRs.

### Recoverable repository cleanup

- [ ] T093 Revalidate T004's exact source/destination manifest and abort on any path/content drift, then move the root Android SDK pointer into ignored Deep `components/AstralProjection/android-client/local.properties`, relocate obsolete Deep root `android-client/` and `apple-clients/` residues to one explicit dated recoverable `/Users/sam/.Trash/AstralDeep-obsolete-clients-075-*` directory, and record its exact path and manifest digest in `specs/075-client-local-speech/quickstart.md`
- [ ] T094 Remove only obsolete root-client patterns/comments from Deep `.gitignore` and the exact obsolete `/apple-clients/` entry from Deep `.git/info/exclude`, then prove against T004's manifest that `components/AstralProjection/android-client/`, `components/AstralProjection/apple-clients/`, and Windows residue are unchanged

### Isolated small Apple follow-ups

- [ ] T095 [P] Add failing Feature-066 R-3 tests proving the default microphone placeholder degrades after ten seconds to an explicit voice-unavailable typed-fallback state in Projection `apple-clients/AstralApp/AstralAppTests/VoiceSessionController065Tests.swift` and `apple-clients/AstralWatchTests/VoiceContract065Tests.swift`
- [ ] T096 Implement only Feature-066 R-3's ten-second default-microphone degradation in Projection `apple-clients/AstralApp/AstralApp/Views/ChatView.swift` and `apple-clients/AstralWatch/Views/WatchChatView.swift`
- [ ] T097 [P] Add failing Feature-066 R-4/R-5 content-keyed quiet-at-rest predicate and visually distinct speaker-stop/speaker-muted glyph tests in Projection `apple-clients/AstralApp/AstralAppTests/VoiceSessionController065Tests.swift` and `apple-clients/AstralWatchTests/VoiceContract065Tests.swift`
- [ ] T098 Implement only Feature-066 R-4/R-5's content-keyed quiet predicate and distinct glyph mapping in Projection `apple-clients/AstralApp/AstralApp/Views/ChatView.swift` and `apple-clients/AstralWatch/Views/WatchChatView.swift`; do not include Feature-066 R-1/R-2 or drawer clamping

### Deterministic latency evidence

- [ ] T099 [P] Add failing Draft-2020-12 and parser tests for a privacy-safe six-client matrix bound to exact Plane/Projection/Deep SHAs, opaque posture IDs, required supported/typed-only slots, missing/unknown/duplicate/mismatched slot rejection, 20 consecutive non-discarded monotonic trials, cold/warm markers, 19-of-20 threshold decisions, clock boundaries, physical/loopback audio-onset proof, two-second typed fallback, and forbidden content in Deep `backend/tests/test_voice_latency_evidence_075.py` and `scripts/tests/test_measure_voice_latency_075.py`
- [ ] T100 Implement the bounded content-free matrix/evidence schema and deterministic completeness/threshold parser in Deep `specs/075-client-local-speech/contracts/voice-latency-evidence.schema.json` and `scripts/measure_voice_latency_075.py`

### Documentation, exact candidates, and local CI-equivalent gates

- [ ] T101 [P] Reconcile final behavior/commands/reasons with Deep `specs/075-client-local-speech/`, tracked `docs/production-deployment.md`, Plane `docs/migration-and-recovery.md`, Projection client READMEs, and Feature-066 R-3/R-4/R-5 in `specs/066-canvas-first-uiux/tasks.md` without adding stale implementation claims
- [ ] T102 Create and freeze clean local Plane and Projection `[skip ci]` content commits plus one empty non-skipping automatic-CI trigger child each; repin Deep to those exact trigger SHAs, create Deep's equivalent content/trigger pair, record all six identities in `specs/075-client-local-speech/quickstart.md`, and require any later tree/SHA change to return to T102
- [ ] T103 Run Plane lock/sync/ruff/architecture/full pytest+branch coverage/diff-cover/real isolated-PostgreSQL upgrade+empty-start/recovery/evidence/build/actionlint commands from Deep `specs/075-client-local-speech/quickstart.md` against the exact T102 Plane trigger commit; retain exact reports and zero skipped required migrations
- [ ] T104 [P] Run Projection locked Python install, ruff, pytest/branch coverage/diff-cover, resource/protocol/ROTE, web lint/unit/coverage/Playwright, Windows offscreen/package, `dotnet format`, helper unit, and Cobertura gates using Projection `.github/workflows/ci.yml`, `tooling/web-ci/package.json`, and `windows-client/README.md` against the exact T102 Projection trigger commit
- [ ] T105 [P] Run Projection Android `ktlintCheck`, lint, core/app unit tests, Kover verify/XML, assemble, and connected local-only/remote-recovery tests from `android-client/gradlew` against the exact T102 Projection trigger commit
- [ ] T106 [P] Run Projection strict recursive swift-format, AstralCore tests/coverage, unsigned iOS/macOS/watchOS xcodebuild tests, xccov union, privacy/package, and local-speech test hooks using `apple-clients/README.md` and `.github/workflows/apple-ci.yml` against the exact T102 Projection trigger commit
- [ ] T107 Run Deep `make sync`, ruff, root backend pytest, every touched module-local orchestrator/voice-agent/shared suite, doc links, composition, security/privacy, and container image/boot checks with both selector postures using `Makefile` and `.github/workflows/ci.yml` against the exact T102 Deep trigger commit
- [ ] T108 Run Plane's own candidate-aware diff-cover gate and separately run Deep `scripts/check_changed_coverage.py` with `deep` and `projection` repository profiles, including the new `windows_csharp` producer; require at least 90% for every changed lane and retain/hash all three repositories' report identities under Deep `build/075/coverage/`
- [ ] T109 Run a healthy LLM Factory exact preflight/synthesized-retranscribed comparison, blocked-endpoint local two-turn integration, and deterministic non-physical latency-parser fixtures against the exact T102 candidates, recording only content-free evidence under Deep `build/075/`

### Candidate-bound live evidence, PRs, automatic CI, and vault

- [ ] T110 Freeze a privacy-safe six-client posture matrix bound to all exact T102 trigger SHAs, then qualify every supported slot in persistent staging with representative migrated PostgreSQL, real Keycloak/ordinary dispatcher, configured Factory, blocked-egress local mode, separate physical/loopback audibility evidence, and the 20-trial SC-001/002/003/011 matrix under Deep `build/075/staging/`; reject unknown/missing slots and treat unavailable staging/native evidence as a draft-PR blocker rather than a pass
- [ ] T111 Run Deep `scripts/prepare_release_evidence.py` separately for the exact Deep and Projection base/trigger commits with `deep` and `projection` strict profiles and all named report slots, write `build/075/local-release-evidence-deep.json` and `build/075/local-release-evidence-projection.json`, and hash-bind Plane's independent coverage/diff-cover/migration reports; prove all local results remain diagnostic
- [ ] T112 Reread wiki `CLAUDE.md`, update `wiki/synthesis-astral-client-local-speech.md`, `wiki/project-astral.md`, `wiki/astral-open-follow-ups.md`, `wiki/astral-ci-gates.md`, affected Apple pages, `index.md`, and `log.md`, then commit/push the vault separately before declaring implementation/candidate checkpoints
- [ ] T113 Re-fetch/prune and recheck remote branch/PR state, explicitly push only each T102 `[skip ci]` parent SHA to its Plane, Projection, and Deep `codex/075-client-local-speech` branch, verify no `push`/`pull_request` Actions were triggered by those parent SHAs, and open all three draft PRs in dependency order with staging/native blockers explicit; unrelated scheduled runs do not satisfy or invalidate this check
- [ ] T114 Only after all three draft PRs are visible, fast-forward Plane, Projection, and Deep `.git/refs/heads/codex/075-client-local-speech` branches to their already-qualified T102 trigger children in dependency order so normal automatic PR CI begins; do not manually dispatch or enable any workflow
- [ ] T115 Review automatic PR outcomes after T114 in each repository's `.github/workflows/`; any product, pin, or evidence-changing fix invalidates affected T102–T112 evidence and MUST repeat that exact candidate-bound loop and replace affected Deep `build/075/` records before its next trigger push, without weakening a gate or using Principle-X bootstrap for convenience
- [ ] T116 Update and separately push wiki `wiki/synthesis-astral-client-local-speech.md`, `wiki/project-astral.md`, `wiki/astral-open-follow-ups.md`, `index.md`, and `log.md` for every opened PR/final status; report exact tests/results/coverage, live verification, selector/migration/contracts, cleanup archive/recoverability, PR URLs, and residual release risks without claiming merge/deploy/release

---

## Dependencies and execution order

### Phase dependencies

- **Setup (Phase 1)** has no dependency and performs no product push.
- **Foundational (Phase 2)** depends on Setup and blocks every story. Plane migration tests precede
  implementation; Projection fixtures precede client parsers; Deep pins only exact qualified local
  component commits.
- **US1 (Phase 3)** depends on Phase 2 and provides the web/local MVP.
- **US2 (Phase 4)** depends on selector/session work from US1 but remains independently testable by
  restart/rollback.
- **US3 (Phase 5)** depends on the frozen Projection fixture/ROTE contract and Deep local backend;
  native lanes are parallel after their parser tests.
- **US5 (Phase 6)** depends on the local admission/session seams and can overlap late US3 client
  work because it primarily changes Deep security tests/files.
- **US4 (Phase 7)** depends only on foundational remote-v1 preservation and can run alongside US3/
  US5 except where Windows/Android controller files overlap.
- **Polish/qualification (Phase 8)** depends on every selected story and all narrow suites.

### User-story completion order

```text
Foundational
  └─ US1 local web MVP
       ├─ US2 selection/rollback
       ├─ US3 all-client parity ─┐
       └─ US5 security/privacy  ├─ full local qualification
Foundational ── US4 remote fix ─┘
```

### Parallel opportunities

- T005–T007, T013/T016/T018, and later test-only tasks touch separate repositories/files and can run
  in parallel before their corresponding implementation.
- After T015, Apple core (T049–T050), Android core (T051–T052), and Windows protocol (T053–T054)
  can run independently.
- Windows (T055–T059), Android (T060–T064), and Apple/watch (T065–T070) adapter lanes can run in
  parallel after their protocol tasks; web is already independently delivered by US1.
- US4 worker deadline/warm-up (T082–T087) can run beside native local-client work. T088–T091 must
  be sequenced with same-file Windows/Android controller changes.
- T103–T106 are repository/platform-local gates that can run concurrently after T102 freezes exact
  candidates; T107–T112 consume those exact identities and any SHA change restarts the loop.

## Parallel examples

### Foundational

```text
Task: T005 Plane repository contract tests
Task: T013 Projection protocol/resource contract tests
Task: T018 Deep schema/golden contract tests
```

### All-client story

```text
Task: T055–T059 Windows local adapter/package lane
Task: T060–T064 Android local adapter/device lane
Task: T065–T070 Apple/watch local adapter/privacy lane
```

### Remote reliability beside local clients

```text
Task: T082–T085 worker total-deadline and warm-admission lane
Task: T049–T070 native local-client lanes
```

## Implementation strategy

### MVP first

1. Complete Setup and Foundational with exact Plane/contract pins.
2. Complete US1 through the blocked-egress two-turn web journey.
3. Validate the MVP independently without pushing or starting hosted CI.
4. Continue US2/US3/US5 plus the independent US4 remote lane; local mode is not release-enabled from
   an incomplete MVP.

### TDD and commits

1. For each behavior task, run its narrow new test first and preserve evidence of the intended
   failure.
2. Implement the smallest production behavior that makes that test pass without weakening v1 or
   security boundaries.
3. Run the owning narrow suite, then nearby regressions.
4. Commit logical local checkpoints; hold all product pushes until T103–T112 are complete. T113
   pushes only CI-skipped parents to open every draft PR; automatic CI begins only at T114.

### Release posture

- Unit/simulator/callback success is not staging, local-only, physical-device, or audibility proof.
- No platform exception or Principle-X bootstrap is planned. `candidate_staging` and
  `apple_first_login_llm` remain non-waivable.
- Product PRs are draft until the same-SHA staging/native evidence and protected automatic checks
  close. No merge, deployment, store submission, tag, package/image release, or publication is part
  of these tasks.
