# Implementation Plan: Conversational Voice Interface Across All Clients

**Branch**: `065-conversational-voice` | **Date**: 2026-07-31 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/065-conversational-voice/spec.md`, resolved
decisions in [research.md](research.md), persistence design in [data-model.md](data-model.md),
and interfaces in [contracts/](contracts/).

**Planning boundary and current status**: The planning and task-generation workflows themselves
produced design artifacts only. Implementation and local verification are now in progress under
[tasks.md](tasks.md), with evidence and explicit remaining release limits in
[verification.md](verification.md). No local implementation result performs or authorizes a merge,
distribution, or release. The optional Spec Kit Git hooks were not run; changes remain uncommitted
until the owner explicitly requests a commit.

## Summary

Add an included, server-owned conversation action to web, Windows, Android, macOS, iOS, and
watchOS. A pinned self-hosted LiveKit server carries realtime media, and a separate first-party
Python voice worker performs only VAD, exact-model speech recognition, and exact-model speech
synthesis. A final transcript returns to the originating client's authenticated AstralDeep
connection with an immutable server binding and short-lived worker HMAC proof, then is submitted
once through the ordinary `chat_message` path; altered or transplanted text fails closed. The worker
has no LLM, tool, delegation, or conversation authority.

The orchestrator owns durable user/turn binding, one-session-per-user leases, takeover,
foreground/background selection, deadline-aware serialized progress scheduling, sensitive-result gating, and the
completion recap. Every accepted voice turn is durably admitted with a running lease. A newer spoken turn becomes
the voice foreground immediately while prior accepted work continues in the background under the
same authorization path. Whole-task per-chat locking is replaced by short admission/snapshot and
terminal-publication locks, so two accepted same-chat turns execute concurrently against immutable
task-local stages; the ordinary user bubble commits at acceptance, while deterministic three-way
assistant/canvas publication uses a separately linked private result commit, never reruns tools, and
never overwrites a concurrent canvas change. Completion
speech uses the normal committed result's authoritative `summary_text`
when present; otherwise deterministic code derives an at-most-80-word recap only from committed
user-visible content. PHI or uncertain sensitivity yields only a generic notice until a fresh
result-bound consent action.

Request outcomes are also text-first on every client. Refusal and pre-dispatch rejection produce a
persistent, visually prominent “did not start” notice; failure, cancellation, and abandonment
produce “did not complete.” The notice carries only the bounded safe server explanation and an
explicit recovery action, uses non-color and assertive accessibility cues, survives unrelated or
same-turn stale lifecycle churn, and leaves typed chat usable. A newer non-older turn or explicit
voice reset/end may clear it. Post-result TTS/playout failure is separate and says the committed
text result remains available rather than claiming that execution failed.

The official LiveKit server is used without a fork. Web, Android, iOS, macOS, and Windows use
pinned official client SDKs. Because the official Swift SDK has no watchOS/WebRTC slice, watchOS
uses a foreground-only authenticated PCM WebSocket bridge in the voice-worker service; that relay
participates in the same LiveKit room and still uses the exact platform ASR, Kokoro model, and
`af_heart` voice. Platform speech is not a parity fallback.

## Technical Context

**Language/Version**: Python 3.11 for the orchestrator and isolated voice worker; vanilla
JavaScript (repository ESLint baseline) for web; Python 3.11 + PySide6 for Windows; Kotlin/JVM 17
and Android API 26+; Swift 5.9-compatible project sources plus SwiftPM tools 6.1 required by the
pinned LiveKit package, built with Xcode 26.6 for iOS 17+, macOS 14+, and watchOS 10+;
YAML/JSON/OpenAPI for deployment and wire contracts.

**Primary Dependencies**: Existing FastAPI, PostgreSQL/psycopg2, Keycloak, AstralDeep WebSocket
and async-task infrastructure, `astralprims`, ROTE, PHI/audit/permission gates, PySide6,
Jetpack Compose, SwiftUI, and `aiohttp`. New exact pins, subject to the Constitution V approval
record: `livekit/livekit-server:v1.13.5@sha256:3497163e15c48fef6e7830c78716f9e9d5edc28abf7aa90b61c86e93bbc306b1`,
worker `livekit==1.1.14`, `numpy==2.4.6`, `onnxruntime==1.28.0`, and
`websockets==17.0.1`; orchestrator-only
`livekit-api==1.2.0`; the vendored Silero VAD v6.0 ONNX payload at upstream commit
`fba061dc5559f696e62171e9a0741782b0fdc23c` with SHA-256
`597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004`; web
`livekit-client` 2.21.0; Windows `livekit==1.1.14`; Android
`io.livekit:livekit-android:2.27.0`; and Apple LiveKit 2.15.3. The worker uses direct RTC and
AstralDeep-owned endpointing rather than LiveKit Agents. The zero-transitive WebSocket pin is used
only for the bounded authenticated worker-control channel; speech HTTP remains first-party stdlib
code. Every transitive package, native artifact, model artifact, hash, license, and notice is locked
rather than floated.

**Storage**: PostgreSQL through guarded, idempotent `_init_db()` migration only. Add
`voice_session` and `voice_turn`; add immutable execution-base commit/revision anchors plus
component/layout digests and
versioned `workspace_layout` metadata to the existing `conversation_commit` coordination contract;
and add the no-queue `voice_interactive` admission
class. The authorized feature-064 handoff is integrated: `064.001` is the asserted sole predecessor
and `065.001` is the target `SCHEMA_REVISION`. The migration fails closed for any other source
revision rather than guessing or overwriting predecessor work.
Audio, transcript copies/digests/proofs, speech text, bearer grants, and
credentials are never stored in these tables.

Voice chat identifiers are retained owner-validated correlation/tombstone values, not foreign keys
to `chats` and never authorization by themselves. The chat deletion/revocation path explicitly
ends current media, rejects unaccepted old turns, suppresses speech for accepted turns, aborts only
their private result stages, and then permits hard deletion while preserving bounded replay and
fencing metadata.

**Testing**: pytest + fake clocks/media/egress and real PostgreSQL; Ruff; locked ESLint and
Playwright web tests with Istanbul output; hash-locked isolated JSON Schema/OpenAPI validation;
PySide pytest plus Windows coverage and a frozen smoke; Android ktlint, lint, JVM tests, Kover,
assemble, connected tests; recursive strict swift-format, Swift tests, XCTest/UI tests, xccov, and
unsigned Xcode builds; Compose/config validation; exact speech-service and LiveKit integration
tests; candidate-bound live acoustic and multi-user staging evidence. The existing protected
`scripts/check_changed_coverage.py` gate combines backend/tooling/Windows Python, web Istanbul,
Android app/core Kover, and iOS/macOS/watchOS xccov inputs and MUST fail below 90% changed-code
coverage for the candidate.

**Target Platform**: Linux containers for AstralDeep, LiveKit, and the voice worker; evergreen
browser; Windows desktop; Android; iOS; macOS; watchOS. Production uses trusted HTTPS/WSS, exposed
LiveKit ICE UDP/TCP and TURN as required. Local macOS verification uses Docker, a real browser,
PySide, Android emulator/device, and available Apple simulators/devices.

**Project Type**: Clinical multi-agent web service with a separate media worker and six thin
client surfaces consuming one server-owned composer/voice contract.

**Performance Goals**: Under the spec's `voice-warm-standard` profile and at least 100 measured
trials per client, warm activation reaches greeting/listening within 3 seconds for at least 95%,
acknowledgement begins within 1.5 seconds of durable acceptance for at least 95%, and recap begins
within 2 seconds of result availability for at least 95%. The coordinator targets the next
utterance at 14 seconds so 100% of deterministic and staged active intervals remain at or below the
hard 20.0-second gap. Authenticated client playout events provide operational observations and an
ephemeral in-memory acoustic probe provides audible proof, immediately discarding waveforms and
retaining only non-content timing measurements. One output stream uses
deadline-aware preemptible speech quanta (at most a 1.5-second attributed recap opening and four-
second other chunks) across at most two active turns per user, reserving a positive 250 ms measured
stream-handoff budget; the second of two coincident terminal
openings targets a start by 1.75 seconds. Default recap is at most 80 words/30 seconds in aggregate
across resumed chunks. Worker commands and client
manifests mechanically cap each 24 kHz quantum at 96,000 samples and a result opening at 36,000;
no schema-valid track can consume the aggregate 30-second allowance at once. Five simultaneous users remain isolated
for 30 minutes; a second accepted same-chat turn holds a running lease without waiting for the first.

**Acoustic self-speech fence**: Normal capture resumes only after both authenticated client playout
and worker-source completion, followed by a generation-fenced 500-millisecond acoustic-tail guard.
The worker keeps at most eight ephemeral SHA-256 fingerprints of speech it actually published for
three seconds after terminal playout. An exact normalized match whose VAD began in that window takes
a dedicated `self_speech_suppressed` path: the recognizing row is abandoned with the existing
`malformed_final`/`none` durable tuple, no retry/error frame or proof is emitted, and no dispatch is
possible. Different speech and the same phrase after expiry remain eligible. Authenticated explicit
barge-in advances the speech epoch and reopens capture immediately instead of waiting for the tail;
ordinary playback completion, mute, end, reconnect, and stale epochs retain their stricter fences.
Only digests, epochs, timers, and content-free disposition metadata exist; teardown clears them.

**Constraints**: One foreground media session and at most two active voice-originated turns per
authenticated user; five-minute true-idle
expiry; foreground-only capture/playback; exact ASR/TTS/voice with no silent fallback; no raw or
synthesized audio retention; no environment-backed user/System LLM; fixed-destination bounded
streaming egress; fail-closed PHI handling; short-lived least-privilege grants; no overlapping
assistant audio; typed chat remains available under every voice failure; no upstream LiveKit fork
unless a separately approved, tested gap is proven.

**Scale/Scope**: Six client surfaces, one single-node LiveKit launch service, one separately
scalable voice-worker class, at least five concurrent voice users, two new tables plus bounded
additive commit/layout versioning columns/config, one shared composer/control vocabulary, one REST surface, one
worker-control schema, one media-plane contract, and protocol/permission/packaging/drift work on
every client.

## Constitution Check

*GATE: evaluated before Phase 0 and re-checked after Phase 1 against Constitution v2.8.0.*

| Principle | Status | Design evidence / required gate |
|---|---|---|
| I. Primary Language | PASS | Backend and media service remain Python 3.11. Client code stays in each already-maintained native language. |
| II. UI Delivery | PASS | `composer_model.py` owns ordered semantic controls and states; clients adapt native chrome. No new primitive or React/Vite source of truth is introduced. |
| III. Testing | PASS WITH ENFORCEMENT WORK | Every changed branch carries golden, denial, race, failure, and cleanup tests. Feature 065 must produce backend/tooling/Windows Python, web Istanbul, Android app/core Kover, and iOS/macOS/watchOS xccov reports and make the existing protected combined `scripts/check_changed_coverage.py --fail-under 90` result blocking for all candidate-changed code rather than rely on the current Python-only soft job. |
| IV. Code Quality | PASS | Python: root `ruff.toml` + `ruff check .`; web JS: locked `tooling/web-ci/eslint.config.mjs`; Kotlin: `ktlintCheck` + Android lint; Swift: `apple-clients/.swift-format` strict recursive lint. Generated/vendored SDK files are hash/license checked and excluded only through narrow tracked configuration. |
| V. Dependencies | PASS WITH RECORDED ARCHITECTURE APPROVAL | On 2026-07-31 the repository owner/lead developer explicitly approved the RTC-only replacement and session decisions. The final exact worker/orchestrator closures, base-image digests, native/model artifacts, licenses, CVEs, locks, image impact, and isolated test-validator lock remain mechanically audited and must receive matching PR review before merge or distribution. LiveKit Agents and its restricted/native-heavy closure are prohibited. |
| VI. Documentation | PASS | Research, data model, OpenAPI, JSON Schemas, media topology, quickstart, operator topology, permissions, privacy boundary, rollback, and evidence requirements are explicit. |
| VII. Security | PASS BY DESIGN | Keycloak remains the user authority. Final text re-enters the normal dispatch path with user claims/token in memory and a short-lived worker HMAC over its immutable binding/digest; no worker impersonation API exists. Every REST mutation is bound to the registered UUID4 device and current UI connection by a redacted short-lived control binding. Media grants are short-lived and scoped; takeover is generation-fenced; speech egress is fixed and bounded; secrets/content are redacted; PHI classification fails closed. |
| VIII. User Experience | PASS | One accessible conversation control/state model, truthful fixed progress vocabulary, barge-in, mute/stop/takeover/recovery, visible transcripts, typed fallback, and sensitive-detail consent apply across all clients. Every request refusal/failure/cancellation/abandonment also has a persistent, prominent, non-color, assertively announced text notice that distinguishes “did not start” from “did not complete,” preserves only the bounded safe explanation and recovery action, survives unrelated/stale lifecycle churn, and never disables typed chat; post-result speech failure instead points to the available text result. |
| IX. Database Migrations | PASS | T024 integrated the authorized 064 handoff and binds `064.001` as the sole predecessor of `065.001`. Two additive tables plus guarded additive `conversation_commit` rebase metadata/admission config use repeat-safe `_init_db()`; representative-data upgrade, wrong-predecessor refusal, idempotency, and concurrent-rebase tests cover the migration. Disable/drain/recovery/retirement live evidence remains a production-readiness gate rather than an ownership ambiguity. |
| X. Production Readiness | CONDITIONAL PRE-MERGE GATE | No stub path is accepted. Before merge, qualifying evidence must run the immutable candidate against persistent staging with real Keycloak, PostgreSQL, LiveKit, voice workers, exact speech inventory, public WSS/ICE/TURN, representative migrated data, and all client flows. The repository's external staging host is currently inactive, so merge is blocked until it is provisioned; local Compose does not substitute. |
| XI. Continuous Integration | PASS WITH EXTENSIONS | Before feature-code pushes, extend the deterministic collector/protected schemas for voice-worker, LiveKit config/model, client, and all-language coverage inputs. Then extend clean image build, boot/readiness, prod exit-78, full backend module suites, client gates, secret/image scans, candidate staging, and release evidence. Voice-worker closure/image and LiveKit digests/config identities become candidate-bound inputs. |
| XII. Cross-Client Consistency | PASS | `ui_protocol.json`, concrete protocol validators, and web/Windows/Android/Apple dispositions classify every required voice frame/action as handled. iOS/macOS use the official SDK; watchOS's explicit media adapter preserves the same server-owned behavior and exact models. No platform is silently omitted. |
| XIII. Research Integrity | PASS | Pinned versions and watchOS support were checked against official release/source material on 2026-07-31. Code-shaped, simulator-proven, physical-device-proven, Windows-native-proven, staged, and released states remain distinct. |

**Gate result**: design and local implementation may proceed under the recorded RTC-only owner
approval. Distribution cannot resume until the replacement closure fingerprint and image audit are
complete, and implementation cannot merge until a qualifying external same-candidate staging
topology exists and all non-waivable trust/staging checks pass.

The only platform-evidence exceptions eligible under the existing Constitution X machinery are
the already-defined bounded client-runner/device checks. They remain at most seven days, require
protected release-owner registration in the append-only ledger outside the candidate tree, and
require a durable next-release resolution receipt. Candidate staging, trust/policy integrity,
security, schema migration, exact speech readiness, and PHI/isolation checks are non-waivable.
Candidate workflows cannot approve their own exception or mutate protected debt state.

For Feature 065, “all non-waivable trust/staging checks” explicitly includes Spec 060 T120:
a publisher reviewer distinct from the requester with self-review disabled; trusted-only creation of
signer-eligible refs or an approved deployed-verifier migration; rollback-safe protected
disposable/failure tags; and uniquely labeled protected publisher hosts with independent orphan
recovery across cancellation, runner loss, host restart, and stale-lease expiry. T180 cannot
complete while any of those four trust gaps remains open.

Task T189 establishes the deterministic diagnostic collector. Setup task T003 binds a fresh set of
all ten native reports and the next run to one clean committed candidate before a requested push
containing feature code or release-evidence changes; every later requested implementation push
requires an equivalent fresh run against the intended base:

```bash
BASE_SHA="$(git rev-parse origin/main)" make prepare-release-evidence
```

Its output is diagnostic only. Protected CI must independently reconstruct canonical inputs and
bind the backend image digest, voice-worker image digest, LiveKit image digest/config hash, client
artifacts, staging identity, policy identity, and tests to the candidate SHA. Publication remains
in the separately pinned protected publishers using the native short-lived job token,
environment approval, create-only collision policy, and no repository-scoped GitHub App,
installation token, or custom token broker.

## Architecture and Data Flow

```text
authenticated client
  ├─ mic/audio ───────────────> self-hosted LiveKit ───────> voice worker
  │                                                       (VAD + exact ASR/TTS only)
  ├─ VAD bind ───────────────> VoiceCoordinator/PostgreSQL ─> immutable turn/chat/UUID binding
  ├─ bound final transcript <─ reliable room data / watch bridge ─┘
  └─ existing authenticated /ws chat_message + voice_origin
          └─> running-lease durable user-turn dispatcher
                └─> normal LLM/agent/tool/PHI/confirmation/audit path
                      └─> atomic committed result + summary_text?
                            └─> VoiceCoordinator
                                  ├─> approved ack/progress/waiting text
                                  └─> authoritative summary or committed-view fallback
                                        └─> scoped worker control ─> exact Kokoro/af_heart audio
```

The LiveKit worker never dispatches a query. Chat navigation pauses new capture until the worker's
ordered `session_context_applied` acknowledges the server-owned revision; an already-started turn
keeps its prior context. At VAD start the worker must obtain a server-created binding for that
worker-applied chat-context revision. On web/Windows/Android/iOS/macOS it
then sends the bounded reliable, ordered transcript envelope to the client participant. The client
queues the final envelope until its existing authenticated AstralDeep WebSocket verifies its
short-lived HMAC/text digest and accepts the stable `chat_message`/`voice_origin` submission; a
reconnect retries the exact IDs until a fully correlated `user_message_acked` accepts it or a fully
correlated `voice_submission_rejected` terminally clears it. The worker likewise retains the bounded
final only in memory until `transcript_accepted` or `transcript_rejected`, because LiveKit does not
buffer reliable data for disconnected participants. A rejection never auto-replays; an explicit
retry uses fresh IDs. On watchOS, the bounded PCM bridge returns
the same envelope and the watch submits it through its normal authenticated connection. Client room
grants cannot publish arbitrary room data, and clients accept transcript envelopes only from the
server-designated worker participant.

After authenticated `register_ui` with strict stable `device_id` and a freshly client-generated
`connection_generation`, each UUID4 device/UI-connection pair receives a short-lived
memory-only `voice_control_binding`; every mutating REST operation must present that binding plus
the normal user bearer and matching device/connection headers. If no chat is selected, explicit
activation first uses a correlated/idempotent extension of the ordinary `new_chat` action and
hydrates its echoed owner-validated UUID4; a delayed response cannot switch the user's newer visible
chat or start voice. No media/grant/greeting begins before that match exists. Before every assistant utterance, the worker sends a strict
content-free announcement manifest that binds its direct LiveKit track or watch PCM range. Clients
accept only the expected worker and report matched local playback with the dedicated top-level
`voice_playout_event` frame.

The current `_dispatch_async_chat` virtual socket must first be generalized into an authenticated
durable user-turn dispatcher. It carries verified user claims and the raw user token only in memory,
sets `llm_context_user_id`, binds the temporary UI session for task lifetime, calls the existing
`handle_chat_message`, accepts stable idempotency IDs, and scrubs all temporary credentials in
`finally`. That repair applies to ordinary async turns as well as voice and prevents accidental
System-LLM resolution or loss of RFC 8693 delegation.

After proof verification, the voice-origin branch selects a bound-destination mode inside that same
dispatcher. It owner-checks the existing recognition-time chat and preserves every normal
authorization/admission/audit gate, but it never invokes the missing-chat creation fallback, changes
`_ws_active_chat`, broadens the current socket's visible scope, forces hydration/navigation to the
older chat, or resurrects a deleted chat. A delayed old-chat turn publishes to that chat's subscribers
and background-status surfaces only. Missing/deleted/unauthorized bound chat yields the correlated
terminal rejection and no message/task; if that chat is the session's current chat, media also ends,
whereas deletion/revocation of an older post-navigation origin rejects only an unaccepted turn and
suppresses every later voice announcement for an already accepted turn while the newer current-chat
session continues.

Physical chat deletion is one voice-aware owner/chat transaction: lock the affected session/turn
rows, fence current media, terminally reject unaccepted turns, mark accepted turns speech-ineligible,
terminalize the accepted voice correlation as destination-abandoned without cancelling its underlying
operation, clear any stage-scoped execution-base anchor, abort only the affected private result
stage, and then delete the normal chat content. Normal message/commit references on retained voice rows use
`ON DELETE SET NULL`; retained user/session/turn/submission/chat identifiers remain for bounded
idempotent rejection. Replaying an unaccepted old final therefore returns the same correlated
rejection; replaying an already accepted old tuple can never dispatch again and receives a terminal
chat-unavailable disposition without changing the original operation. In either case, the retained
chat UUID can neither block/cascade the delete nor authorize, recreate, or publish
to that chat. Accepted execution is not implicitly cancelled; any continuing side effects and audit
follow ordinary deletion policy, while chat-result publication and all later voice output for the
deleted destination stay suppressed.

The same refactor removes the whole-task `_workspace_locks[chat_id]` hold. One short admission
transaction obtains a no-queue `voice_interactive` running lease, publishes a `user_acceptance`
commit under the client request generation with the new user bubble plus a complete normal versioned
copy-forward of the authoritative components/layouts (and no assistant candidate), without
terminalizing the operation, and creates a linked
private `assistant_result` commit under a stored server request generation with a retained execution-base commit/revision
anchor plus canonical component/layout digests; the base FK is stage-scoped and clears on terminal
commit/abort so it cannot pin an ancestor chain. Agent/tool work executes outside the chat lock. A short locked terminal transaction
performs a three-view rebase (execution base, private candidate, latest committed) by stable component
ID and layout key. Non-conflicting deltas publish normally; same-key conflicts preserve latest and
append a safe notice. Layout references are revalidated against merged components, with invalid
candidate layout dropped/flattened rather than overwriting latest. Publication rewrites only this
commit's rows and leaves every other private stage and retained committed version intact. The system
never reruns an LLM/tool/side effect to resolve publication order. If no execution lease is available,
it returns a terminal explicit-retry refusal before normal acceptance and does not speak an
acknowledgement. Only the result commit terminalizes the operation.

Existing owner-bound confirmation/user-input gates remain live operational controls, not candidate
assistant results: they may be published immediately through the normal gate/status path while the
operation and private result commit remain staged. They expose no uncommitted result/canvas content,
retain every existing authorization/expiry/idempotency rule, and resume the same operation/stage
after the user's normal response.

Canonical `operation_status` frames remain retained on every client through their first-terminal,
monotonic reconciliation rules, but retention is separate from presentation. Each client derives its
working label and indeterminate activity indicator only from accepted scoped nonterminal operations;
terminal success clears that presentation (including hydration and reconnect replay), while terminal
failure/refusal/cancellation/retry guidance uses the existing prominent non-busy outcome surface.
When operations overlap, completing one operation cannot hide another accepted operation that is
still nonterminal.

One per-user scheduler serializes all assistant audio for at most two active voice turns. It targets
each next utterance at 14 seconds, pre-synthesizes coincident result openings, cancels stale progress,
and alternates preemptible attributed chunks. Admission to the stream reserves another equally due
turn's four-second maximum quantum plus 250 ms measured handoff, so positive switching latency cannot
push the second start past its 20-second deadline.
Terminal openings are at most 1.5 seconds before yielding, allowing the second of two simultaneous
completions to begin by the 1.75-second scheduling target; longer <=80-word/30-second recaps resume
later with ordinal, sensitivity-safe attribution. Each `speak` command and announcement manifest
carries a strict quantum role/index: all quanta are <=96,000 samples at 24 kHz and result openings
are <=36,000, while the coordinator separately caps the sum of a result's chunks at 720,000 samples.
Over-budget synthesis fails before track publication. Barge-in cancels unfinished old recap chunks.
Deterministic overlapping-long-turn tests prove the hard arbitration bound, and real staged
speech measures the p95 two-second result-start criterion.

Composer session controls map only to the authenticated REST operation IDs in the OpenAPI contract;
there is no generic voice mutation frame. Media capability is advertised with `register_ui`.
Content-free, device/connection/generation/grant-revision-fenced `voice_playout_event` is the sole
new client-to-server voice frame on the UI socket; it is not a `ui_event` action, creates no
operation, and server receipt time—not the client clock—drives operational cadence observation.

## Project Structure

### Documentation (this feature)

```text
specs/065-conversational-voice/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── dependency-approval.md             # implementation gate; PR review remains authoritative
├── verification.md                    # candidate-bound command/result/evidence index
├── contracts/
│   ├── voice-rest.openapi.yaml
│   ├── voice-control.schema.json
│   ├── worker-control.schema.json
│   └── media-plane.md
├── checklists/
│   └── requirements.md
└── tasks.md                           # generated and analyzed after planning
```

### Source Code (repository root)

```text
backend/
├── voice_agent/                     # NEW: isolated direct-RTC media worker; no Agents/LLM/tools
│   ├── main.py
│   ├── session.py                   # RTC room/audio + Silero VAD + owned turn lifecycle
│   ├── speech_adapters.py           # bounded Speaches realtime STT + fixed Kokoro TTS
│   ├── watch_bridge.py              # authenticated foreground PCM relay
│   ├── requirements.in
│   ├── requirements.lock.txt
│   └── tests/
├── orchestrator/
│   ├── api.py                       # EDIT: retire legacy voice bypass; authenticated REST
│   ├── orchestrator.py              # EDIT: composer frames, chat seam, terminal hooks
│   ├── async_tasks.py               # EDIT: authenticated running-lease user-turn dispatch
│   ├── history.py                   # EDIT: atomic three-way terminal rebase/publication
│   ├── conversation_publication.py  # EDIT: immutable execution base + rebase disposition
│   ├── voice_sessions.py            # NEW: leases, ownership, takeover, generation fences
│   ├── voice_coordinator.py         # NEW: foreground turns, cadence, serialized speech
│   ├── voice_recap.py               # NEW: committed summary/fallback + sensitive consent
│   ├── livekit_service.py           # NEW: readiness, rooms, scoped RTC grants/disconnect
│   └── tests/                       # NEW/EDIT: state, auth, race, cadence, recap tests
├── shared/
│   ├── database.py                  # EDIT: two tables, commit metadata/admission + revision
│   ├── streaming_egress.py          # NEW: fixed, DNS-pinned, bounded HTTP/WSS egress
│   ├── protocol.py                  # EDIT: strict voice/composer validators
│   ├── ui_protocol.json             # EDIT: authoritative frames/actions/capabilities
│   └── feature_flags.py             # EDIT: operational voice kill switch
├── webrender/
│   ├── chrome/composer_model.py     # NEW: ordered server-owned composer actions/state
│   ├── templates/shell.html         # EDIT: voice control host
│   └── static/
│       ├── client.js                # EDIT: shared reducer/controller integration
│       ├── astral.css               # EDIT: accessible state styling
│       └── vendor/                  # NEW: pinned UMD + license/hash, no CDN
└── tests/                           # EDIT: env isolation, migration, web/protocol/E2E
    ├── fixtures/voice_065/client_conformance.json # NEW: shared six-client valid/invalid vectors
    └── perf/voice_concurrent_turns.py # NEW: same-chat/rebase + overlapping speech-deadline gate

deploy/livekit/                      # NEW: pinned single-node local/staging/prod configs
├── livekit.local.yaml
├── livekit.staging.yaml
├── livekit.production.yaml          # secret-free network/TURN policy; secrets injected
└── README.md

Dockerfile.voice                     # NEW: locked runtime plus voice-worker-test target
docker-compose.yml                   # EDIT: LiveKit + worker + test-profile service + env mapping
docker-compose.staging.yml           # EDIT: candidate-bound media topology/readiness
.env.example                         # EDIT: operator-only LiveKit/voice aliases; no user UI

windows-client/
├── astral_client/
│   ├── app.py                       # EDIT: composer control and lifecycle
│   ├── voice.py                     # NEW: reducer + LiveKit/QtMultimedia adapter
│   ├── protocol.py                  # EDIT
│   └── protocol_manifest.py         # EDIT
├── AstralDeep.spec                  # EDIT: QtMultimedia + LiveKit native collection
├── requirements.in                  # EDIT: exact LiveKit SDK
├── requirements-release.lock.txt    # REGENERATE with hashes
└── deployment/runtime-manifest.json # EDIT: packaged native evidence

android-client/
├── gradle/libs.versions.toml         # EDIT: exact SDK pin
├── app/build.gradle.kts              # EDIT: SDK + locked narrow repository
├── app/src/main/AndroidManifest.xml  # EDIT: RECORD_AUDIO only
├── app/src/main/kotlin/.../
│   ├── ui/AdaptiveShell.kt           # EDIT: server-owned conversation action
│   ├── ui/AppViewModel.kt            # EDIT: state/lifecycle
│   ├── transport/DeviceCaps.kt       # EDIT: real media capabilities
│   └── voice/VoiceSessionController.kt # NEW
└── core/src/main/kotlin/.../protocol # EDIT: wire/reducer/disposition

apple-clients/
├── AstralCore/Sources/AstralCore/    # EDIT: dependency-free protocol/reducer shared with watch
├── AstralApp/AstralApp/
│   ├── Voice/VoiceSessionController.swift # NEW: iOS/macOS SDK adapter
│   ├── Views/ChatView.swift          # EDIT: composer control
│   ├── AppModel.swift                # EDIT: state/lifecycle
│   └── PrivacyInfo.xcprivacy         # EDIT: microphone/data declaration
├── AstralApp/Info.plist              # EDIT: microphone usage
├── AstralApp/AstralApp-macOS.entitlements # EDIT: audio-input sandbox
├── AstralWatch/
│   ├── WatchVoiceBridge.swift        # NEW: PCM bridge + AVAudioEngine/Converter
│   ├── Views/WatchChatView.swift     # EDIT: equivalent action/state
│   ├── WatchModel.swift              # EDIT: lifecycle/reducer
│   └── PrivacyInfo.xcprivacy         # EDIT
├── AstralApp/WatchInfo.plist         # EDIT: microphone usage
└── AstralApp/AstralApp.xcodeproj/    # EDIT carefully; preserve unrelated owner changes

tooling/web-ci/                        # EDIT: fake-media Playwright/a11y/reconnect tests
tooling/contract-ci/                   # NEW: isolated hash-locked schema/OpenAPI validator
├── requirements.in
├── requirements.lock.txt
└── validate_voice_contracts.py
.github/workflows/                    # EDIT: exact media/client/staging/evidence gates
```

**Structure Decision**: Keep one orchestrator and one authoritative UI/control contract. Add a
separate media worker because realtime audio libraries, ephemeral audio buffers, and speech
credentials must not enter the main agent process. Use official SDK adapters where supported;
the watch bridge is an explicit last-mile adapter inside that same media boundary, not a second
agentic system. Do not edit the sibling Astral-Primitives repository because no new primitive is
needed.

## Implementation Sequencing

1. Record exact dependency approval and, before feature-code pushes, extend the deterministic local
   evidence collector, protected evidence schemas, and combined all-language changed-code gate for
   the new worker/LiveKit/model/config/client inputs. Then repair the authenticated durable user-turn seam and correlated
   acceptance/rejection acknowledgement plus idempotent correlated `new_chat`; split the whole-task
   chat lock into short admission/publication locks with component-and-layout no-rerun three-way rebase; add
   schema, same-chat concurrency, state-machine, recap, egress, and authorization tests before
   enabling media.
2. Add digest-pinned LiveKit/config and the isolated direct-RTC worker with strict exact-model
   readiness, vendored Silero inference, AstralDeep-owned endpointing/reconnect/playout state,
   custom bounded speech adapters, transcript proofs, announcement manifests, idempotent grant
   rotation/worker acknowledgement, service-authenticated pool assignment plus room-scoped worker
   RTC grants, and no Agents/LLM/tool imports.
3. Retire the unauthenticated legacy `/api/voice/stream`/caller-selected synthesis paths; expose
   device/UI-connection-bound REST and shared composer/control contracts behind a default-on operational
   kill switch whose readiness still fails closed.
4. Land shared manifest/validators and all six client dispositions together; then implement web,
   Windows, Android, iOS/macOS, and the watch relay without allowing any required-frame ignore.
5. Run deterministic tests and package gates, then live Docker/browser/simulator verification.
   Pause at each real Keycloak login boundary for the user. Qualifying same-candidate staging is
   required before merge; physical-device acoustic, Windows-native, five-user soak, and any
   permitted bounded platform evidence complete the merge/release evidence matrix.

## Phase 0 — Research

[research.md](research.md) records the official version/digest evidence and resolves topology,
authority, speech adapters/egress, configuration isolation, persistence, progress, recap,
sensitive speech, watchOS, lifecycle, deployment, and verification alternatives. No unresolved
clarification marker remains.

## Phase 1 — Design and Contracts

[data-model.md](data-model.md) defines the additive schema and ephemeral entities;
[voice-rest.openapi.yaml](contracts/voice-rest.openapi.yaml),
[voice-control.schema.json](contracts/voice-control.schema.json),
[worker-control.schema.json](contracts/worker-control.schema.json), and
[media-plane.md](contracts/media-plane.md) define the client/control/media boundaries; and
[quickstart.md](quickstart.md) is the implementation and live-validation runbook.

**Post-design constitution re-check**: conditional PASS as recorded above. The plan adds no
unresolved technical ambiguity. The RTC-only dependency direction has explicit owner approval; the
remaining exact closure-fingerprint/distribution gate and qualifying external staging topology are
both required
before merge.

## Phase 2 — Tasks and Analysis

`$speckit-tasks` has generated dependency-ordered work in [tasks.md](tasks.md). The artifact must
pass `$speckit-analyze` after every cross-artifact remediation and before `$speckit-implement`.
