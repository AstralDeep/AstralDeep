# Implementation Plan: Client-Local Conversational Speech

**Branch**: `codex/075-client-local-speech` | **Date**: 2026-08-28 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/075-client-local-speech/spec.md`

## Summary

Keep the recovered LLM Factory ASR/TTS path as the default and byte-compatible Feature-065 remote
profile, while adding one server-owned `VOICE_SPEECH_BACKEND=llm_factory|client_local` selector.
The local profile uses explicit OS/browser local-only recognition and local synthesis on every
shipping client that can prove the required capability; unsupported clients remain typed-only and
never silently use remote speech. AstralDeep still owns authenticated session, turn, announcement,
authorization, PHI, audit, and ordinary conversation dispatch. AstralPlane adds an honest durable
backend discriminator and conditional media fields. AstralProjection adds the shared v2 contract,
ROTE adaptation, and web/Windows/Android/Apple adapters.

Remote reliability work gives each ASR/TTS operation one total deadline across retries, blocks
worker registration until exact inference preflight plus greeting/earliest-acknowledgement warm-up
succeed, adds content-free phase timings, and repairs Windows/Android grant recovery. It deliberately
does not add generic connection pooling: recovered measurements show warm TTS at 0.142–0.153 seconds
and ASR at 0.355–0.418 seconds, while the earlier exact 30-second symptom matches two 15-second ASR
attempts during LLM Factory OOM failure.

## Technical Context

**Language/Version**: Python 3.11; vanilla JavaScript; Python/PySide6; Kotlin/JVM 17 with Android API 33 local-speech eligibility and existing minSdk 26 typed support; Swift 5.9-compatible sources; JSON Schema 2020-12; OpenAPI 3.1; first-party C#/.NET Framework Windows helper source

**Primary Dependencies**: Existing FastAPI/ASGI, Keycloak/RFC 8693, AstralPlane PostgreSQL facade, AstralProjection/ROTE/webrender, LiveKit direct RTC/watch relay, ONNX Runtime/Silero, fixed LLM Factory Whisper/Kokoro adapters, PySide6 QtTextToSpeech/QtMultimedia, Android SpeechRecognizer/TextToSpeech, Apple Speech/AVFAudio, browser Web Speech APIs; no new third-party runtime/model dependency

**Storage**: Existing PostgreSQL `voice_session`/`voice_turn` through AstralPlane migration `075.001`; bounded ephemeral local and worker speech buffers; no audio, transcript copy/digest/proof, engine inventory/path, endpoint, credential, or local capability persistence

**Testing**: pytest/ruff/diff-cover; JSON Schema/OpenAPI/manifest/fixture/drift guards; JavaScript lint/unit/coverage/Playwright; PySide6 offscreen pytest plus packaged helper/plugin probe; pinned development-only .NET tests, `dotnet format`, Cobertura output, and the shared changed-line parser for first-party C# helper source; Android Gradle ktlint/lint/unit/Kover/assemble/connected tests; Swift format/Swift Package/XCTest/xcodebuild/xccov; isolated PostgreSQL migration/recovery; deterministic 20-trial latency-evidence validation; candidate-bound real Keycloak/PostgreSQL/LLM Factory and physical-client/acoustic qualification

**Target Platform**: Linux Python 3.11 orchestrator/voice-worker containers; PostgreSQL and Keycloak; same-origin supported browser; Windows desktop; Android API 26+ app with local voice enabled only on eligible API 33+ devices; supported iOS, macOS, and watchOS hardware

**Project Type**: Three-repository composed web service, embedded data library, presentation package, web/desktop/mobile clients, and first-party native helper

**Performance Goals**: 95% local activations listening within 3 seconds; 95% local greetings begin within 1 second of ready; 95% local finals receive visible/audible acknowledgement within 2.5 seconds excluding downstream model execution; nonresponsive remote ASR/TTS ends within one configured total deadline; current remote v1 latency/bytes do not regress

**Constraints**: Server-only immutable-per-process backend selection; no client override or silent fallback; local microphone audio never leaves the device; half-duplex with 500 ms post-playout echo fence; server-authorized bounded announcements only; ordinary dispatcher/security/audit path only; existing remote v1 exact bytes retained; local schema-v2 handling on all six clients; typed fallback always available; no new third-party dependency; Python 3.11 compatibility; changed-line coverage at least 90%; product push/PR and hosted CI only after locally executable work is complete

**Scale/Scope**: AstralPlane schema/repository; AstralDeep voice API/coordinator/socket/worker/config/composition/telemetry; Projection protocol/ROTE/web/Windows/Android/iOS/macOS/watchOS plus packaging/privacy; two selectable deployment profiles; one initial `en-US` policy; cleanup of obsolete root Apple/Android residue

## Constitution Check

*GATE: Passed before Phase 0 research and re-checked after Phase 1 design. No waiver is used.*

| Gate | Status and design |
| --- | --- |
| Repository authority and dependency direction | PASS — Deep selects/orchestrates and uses only Plane's public facade; Plane alone owns the migration/repository; Projection owns protocol rendering/adaptation and every client. Primitives/LETS are not changed. |
| Server authority | PASS — `VOICE_SPEECH_BACKEND` is parsed once; clients only report bounded untrusted eligibility. No client preference, endpoint, credential, model, voice, automatic fallback, or active-session backend mutation exists. |
| Existing security and dispatch | PASS — local text is user-controlled and a sibling `admit_local_transcript` verifies client/session fences instead of worker HMAC, then returns the same admission consumed by ordinary `handle_chat_message`. Keycloak, owner/device/socket/control, LLM-selection, permission/policy, PHI, confirmation, tool, audit, execution, cancellation, commit, and publication gates remain decisive. |
| Privacy and retention | PASS by design — local audio remains on-device; interim text remains client-memory-only; no audio/transcript copy/digest/proof/engine/path/endpoint/credential/hidden reasoning reaches durable state, logs, metrics, audits, crash reports, or generic frame capture. Tests block remote speech egress in local mode. |
| Dependency control | PASS — only existing locked runtime libraries and platform/browser APIs are used. The Windows helper is first-party source built in the existing packaging lane and introduces no shipped package dependency. Its pinned Microsoft test adapter/coverage tooling is development-only and owner-approved in the 2026-08-28 conversation; it lives only in the test project's separate manifest/lock, is excluded from publish/package inputs, and has an automated runtime-isolation guard. The PR dependency note remains mandatory. Discovery of any additional dependency stops for explicit approval. |
| Schema ownership/migration/recovery | PASS by design — Plane `075.001` requires exact `074.004`, performs nullable-add/backfill/NOT-NULL, replaces only verified named constraints, enforces exhaustive remote/local rows, is repeat-safe, tests representative upgrades/empty DB/wrong predecessor, and documents backup restore or forward-migration recovery. Deep repins only the exact qualified Plane revision/digest. |
| Cross-client contract | PASS by design — REST schema v2 plus `client_local/v1` strict frames land with `ui_protocol.json`, fixtures, ROTE, and web/Windows/Android/iOS/macOS/watch dispositions. Remote v1 is unchanged; old clients receive a v1-safe unavailable/upgrade result in local deployments. |
| Language quality gates | PASS by plan — Python uses repository ruff `py311`, pytest/coverage/diff-cover; JavaScript uses Projection's committed ESLint/test/Playwright coverage; Kotlin uses ktlint, Android lint, unit/connected tests, Kover; Swift uses strict recursive swift-format, XCTest/xcodebuild, xccov; C# helper uses deterministic Windows build, `dotnet format`, pinned tests, Cobertura output, and an explicit `windows_csharp` producer in the shared changed-line policy. Every changed lane retains at least 90% changed-line coverage. |
| Performance and resource bounds | PASS by design — strict frame/text/rate/sequence/expiry limits, bounded buffers, one serialized local playout owner, total remote deadlines, admission only after fixed-phrase readiness, and low-cardinality phase timings. A privacy-safe matrix bound to all three candidate SHAs closes the six-client posture inventory; SC-001/002/003/011 use 20 consecutive non-discarded trials per supported matrix slot, explicit monotonic clock boundaries, at least 19 passing trials, and physical/loopback audio onset where audibility is claimed. Unknown/missing slots fail. Connection pooling is excluded until measurements justify a separate security review. |
| Runtime staging | REQUIRED, NOT YET CLAIMED — final candidate qualification must use persistent external staging with representative migrated PostgreSQL data, real Keycloak, ordinary dispatcher/agent dependencies, configured LLM Factory, and the same exact artifacts exercised by supported physical clients. Unit/simulator evidence cannot satisfy this gate. |
| Native/acoustic evidence | REQUIRED, NOT YET CLAIMED — supported browser, Windows host, Android device, iOS, macOS, and watchOS must prove local-only operation, interruption, typed fallback, and audible output. Callback success is not audibility. Missing evidence blocks merge/release readiness. |
| Temporary platform exception | NOT USED — `candidate_staging` and `apple_first_login_llm` remain non-waivable; no seven-day client exception ledger is requested. Unavailable native evidence is reported as unavailable, not passed. |
| Candidate-independent release verification | PASS by plan — local collection is diagnostic. Protected workflows/policy from current default branch reconstruct trusted inputs and independently validate candidate identities, digests, staging, coverage, trust, privacy, and any allowed exception. Candidate workflows remain unprivileged. |
| Local-before-push evidence | PASS by plan — after exact local candidate commits exist, run `python3 scripts/prepare_release_evidence.py --repo . --evidence-dir build/075/evidence --coverage-dir build/075/coverage --base-sha <DEEP_BASE> --candidate-sha <DEEP_CANDIDATE> --coverage-mode strict --repository-profile deep --output build/075/local-release-evidence-deep.json`; run the same Deep-owned script with `--repo ../AstralProjection`, Projection's exact base/candidate, `--repository-profile projection`, and every named Projection report slot to produce `build/075/local-release-evidence-projection.json`. Plane has no false shared profile: retain and hash-bind its own pytest coverage XML and diff-cover result beside its exact base/candidate. Results are diagnostic and cannot authorize release. |
| Principle-X bootstrap | NOT USED — all implementation, test, coverage, migration, packaging, and local evidence work can precede the first product push. No structurally remote-only canonical input is being used to bypass local-first order. If such a blocker emerges, work stops rather than inventing an exception. |
| Publication/release | NOT IN SCOPE — no merge, deploy, store submission, tag, package, image publication, signing, or protected debt/exception mutation is authorized. Any later publication retains separately pinned protected publishers with native short-lived job identity and create-only collision policy; no repository-scoped GitHub App/token broker is introduced. |
| Knowledge-vault checkpoint | PASS by plan — durable decisions and completed spec/plan/tasks, implementation, candidate push/PR, merge, and release-state changes receive separate curated kos-wiki updates, commits, and pushes. |

### Post-design re-check

Phase 1 resolves all critical design questions. The contracts use separate v2 local frames instead
of widening exact remote v1; WebSocket input validates the server-held current control binding
without retransmitting its bearer; local admission has no worker-proof bypass; delayed announcements
carry mute/consent revisions; and Android eligibility is narrowed to API 33+ because the installed
on-device locale inventory begins there. No schema, dependency, trust, privacy, cross-client, or
release-evidence violation remains. Runtime/native evidence is a required later gate, not a claimed
planning result.

## Project Structure

### Documentation (this feature)

```text
specs/075-client-local-speech/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── checklists/
│   └── requirements.md
├── contracts/
│   ├── voice-rest-v2.openapi.yaml
│   ├── voice-local.schema.json
│   ├── voice-latency-evidence.schema.json
│   └── media-plane.md
└── tasks.md                    # analyzed test-first implementation ledger
```

### AstralPlane (durable voice backend identity)

```text
src/astralplane/
├── contracts/
├── database/                   # guarded 075.001 migration/revision/digest
└── repositories/               # immutable backend-aware session records/validation

tests/
├── unit/
├── integration/                # empty/current/pre-075 PostgreSQL upgrades
└── recovery/
```

### AstralProjection (contract, ROTE, and every client)

```text
contracts/
├── ui_protocol.json
└── fixtures/

backend/
├── rote/
└── webrender/static/           # browser capability/local ASR/TTS controller

windows-client/                 # reducer/controller, Qt TTS, first-party ASR helper/package
android-client/                 # API-33 local SpeechRecognizer/TTS adapter; v1 recovery
apple-clients/                  # AstralCore plus iOS/macOS/watchOS adapters/privacy/tests
tooling/web-ci/                 # JS/unit/coverage/Playwright conformance
tests/                          # protocol/resource/ROTE/drift/package tests
```

### AstralDeep (selection, authority, dispatch, remote reliability)

```text
config/
└── astral-composition.json      # exact qualified Plane/Projection pins

backend/orchestrator/
├── voice_api.py                 # v1-safe behavior plus v2 capability/local session routes
├── voice_sessions.py            # local binding/admission/announcement + shared lifecycle
├── voice_control_binding.py     # current socket/control authority (bearer stays server-side)
├── livekit_service.py           # remote v1 capability/grant path retained
└── ...                          # narrow websocket/dispatch/telemetry integration seams

backend/voice_agent/
├── speech_adapters.py           # total-deadline remote ASR/TTS
└── ...                          # exact preflight, fixed-phrase warm admission, timings

backend/tests/                   # selector/API/socket/auth/replay/PHI/retention/integration
specs/075-client-local-speech/   # authoritative feature artifacts/contracts
```

Obsolete root `android-client/` and `apple-clients/` local residue is relocated to a dated Trash
archive after the Android SDK pointer is copied to Projection's ignored authoritative client path.
Only obsolete root ignore entries are removed; authoritative Projection clients are untouched.

**Structure Decision**: Use the existing three-way Astral ownership boundary. Plane changes first,
Projection can then implement the frozen client contract independently, and Deep consumes the exact
Plane/Projection candidates and integrates selection/authority. Primitives and LETS remain pinned
and unchanged. Local speech is one backend-discriminated extension of the Feature-065 session/turn
lifecycle, not a parallel conversation or agent path.

## Implementation Strategy

### Phase A — Freeze contracts and persistence

1. Land executable schema-v2 REST/WebSocket contracts, canonical fixtures, dispositions, and
   server/client golden vectors without changing remote v1.
2. Implement/test Plane `075.001`, repository records, wrong-predecessor refusal, representative
   migration, repeat startup, conditional row constraints, and recovery.
3. Qualify the exact Plane candidate locally; then update Deep's Plane revision/digest pin.

### Phase B — Backend selector and local authority

1. Add strict immutable selector parsing and authenticated v2 capability/status with v1-safe local
   unavailability.
2. Create/take over local sessions with no room/worker/grant; reuse lifecycle/control operations
   through backend-aware projections.
3. Implement local ready/start/bind/final/failure/rejection, `admit_local_transcript`, ordinary
   dispatcher parity, announcement policy/revisions, playout observations, lifecycle cleanup,
   redacted phase metrics, and negative security/retention tests.

### Phase C — Cross-client local adapters

1. Update Projection protocol/fixtures/ROTE and make missing dispositions fail drift guards.
2. Implement web local-only capability/install/controller and blocked-network Playwright journeys.
3. Implement Windows bounded PCM helper protocol, Qt local TTS, packaging/hash/signature probes, and
   half-duplex lifecycle.
4. Implement Android API-33 installed-locale probe/download flow, recognizer/TTS lifecycle, service
   visibility, tests, and remote grant recovery; keep API 26–32 typed-only.
5. Implement shared Apple controller contract and iOS/macOS/watchOS adapters, permission/privacy,
   foreground/audio interruption, physical-device qualification hooks, and no watch PCM reuse in
   local mode.

### Phase D — Remote reliability and cleanup

1. Introduce one total monotonic deadline across remote ASR/TTS attempts with deterministic timeout
   and fast-retry tests.
2. Make exact inference preflight plus greeting/earliest-acknowledgement warm-up synchronous before
   registration; warm remaining phrases asynchronously and capture only bounded phase metrics.
3. Repair Windows/Android v1 current-grant recovery and stale identity/revision rejection.
4. Relocate obsolete root client residue to the recorded recoverable Trash archive and clean only
   its obsolete ignore rules.
5. Close the already-approved Feature-066 Apple R-3/R-4/R-5 items as isolated changes: a ten-second
   default-mic unavailable degradation, content-keyed quiet status, and distinct stop/mute glyphs.

### Phase E — Local gates, candidate evidence, and PRs

1. Run narrow TDD suites throughout, reconcile documentation, and freeze exact local Plane,
   Projection, and Deep candidate commits before any candidate-bound claim. Each repository keeps a
   CI-skipped content commit plus one already-created empty automatic-CI trigger child; Deep pins the
   exact trigger commits for Plane and Projection.
2. Against those exact trigger commits, mirror every changed repository's local CI commands,
   module-local suites, changed-line coverage (including C#), packaging, migration, privacy,
   composition, deterministic latency parser, recovered Factory comparison, and blocked-egress
   local integration.
3. Freeze the privacy-safe six-client qualification matrix against all exact candidate SHAs, then
   qualify every supported slot in persistent staging and on supported physical clients, including
   the 20-trial SC-001/002/003/011 matrices; run separate Deep and Projection local
   evidence parsers and hash-bind Plane's native reports. Any SHA change invalidates all affected
   matrix/evidence and returns to candidate freezing.
4. Update kos-wiki, explicitly push only the CI-skipped parent commits, and open all three draft PRs
   in Plane → Projection → Deep order. Only after every PR exists, fast-forward each branch to
   its already-qualified trigger child so normal automatic PR CI begins. Do not manually dispatch
   hosted CI.

## Complexity Tracking

No constitution violation requires justification. The additional first-party Windows helper is the
smallest no-new-dependency way to provide Windows recognition; it remains inside Projection's
existing Windows application/package boundary. Separate v2 routes/frames are required by exact v1
compatibility and do not create a second conversation dispatcher.
