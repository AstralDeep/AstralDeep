# Spec 065 Verification Record

**Recorded**: 2026-08-02 (America/New_York)
**Status**: local implementation diagnostic; not an immutable release candidate
**Local base**: `16e401b` plus uncommitted work
**Remote state at verification start**: local `065-conversational-voice` was four commits ahead of
and two commits behind `origin/065-conversational-voice`; no implementation push is claimed.

This record deliberately separates source/build/test evidence, local live evidence, login-bound
evidence, and unavailable release evidence. It contains no transcript, recap, audio, credential,
token, provider body, or PHI content.

## Current outcome

The local tree implements the included RTC-only conversational voice path using the self-hosted
LiveKit server, `Systran/faster-whisper-large-v3`,
`speaches-ai/Kokoro-82M-v1.0-ONNX`, and fixed voice `af_heart`. Final transcripts enter the ordinary
authenticated chat dispatcher. Spoken lifecycle output is produced by the separate media worker and
is tied to committed lifecycle/result metadata rather than being inserted into model context.

The real local direct-RTC probe has exercised LiveKit media, Silero VAD, Whisper transcription,
transcript proof validation, and ordinary dispatch. The synthetic local user was rejected by the
ordinary dispatcher with `permission_denied` because it intentionally had no normal user LLM
configuration. This is the expected fail-closed boundary: the included speech endpoint credential
does not become an LLM credential. The user completed sign-in on Android, iOS, and macOS without
automation handling credentials. Android and iOS completed the signed-in control/lifecycle
checkpoints, and the user later completed current-build macOS sign-in and heard the included
`af_heart` greeting. The user then spoke a real request: transcription entered the ordinary
dispatcher, title generation succeeded, but the first tool-planning LLM call failed before any tool
was selected or run. No result was committed and no success recap was synthesized.

The real request failure was diagnosed without exposing credentials, transcript text, or provider
bodies. The user's encrypted in-product configuration is present for the LLM Factory endpoint and
`google/gemma-4-31B-it`; its save-time connection test, model inventory, title generation, bounded
plain completion, auto/forced-tool completion, streaming completion, and the full current tool
catalog all returned successfully in separate probes. The observed tool-planning failure is
therefore classified as intermittent or request-specific rather than a deterministic credential,
model, or tool-catalog incompatibility. The client factory now disables hidden SDK retries, applies
a bounded default request timeout, and the orchestrator retries only proven transient status,
timeout, connection, or maintenance-page failures while failing fast on other 4xx, malformed, or
unknown responses. Existing test fixtures remain UUID-scoped and cannot overwrite this user row.

Every client now presents request refusal as a persistent prominent “did not start” alert and
failure, cancellation, or abandonment as a persistent prominent “did not complete” alert. The
notice uses a non-color cue, assertive accessibility semantics, bounded safe server text, and a
recovery action while typed chat stays usable. A post-result TTS/playout failure instead states that
the text result remains available. Stale or unrelated lifecycle churn cannot erase the terminal
notice; a different non-older turn or explicit voice reset/end can. The backend also reconciles an
ended session whose accepted turn lost its in-process finalizer against the exact durable operation
and committed-result proof, then projects the repaired terminal state to current same-user sockets.

The first authenticated web activation after the prior hardening exposed a real acoustic-boundary
race: the worker reopened recognition when its own `AudioSource` drained, before the authenticated
client had reported local playout completion. Ambient/render-tail audio then crossed the normal VAD
and dispatch path. The repaired worker now holds capture until both the exact worker-source terminal
and exact owner/connection/generation/grant-fenced client terminal have arrived, in either order.
Routine foreground heartbeats, stale announcements, duplicates, and stale `media_state:listening`
cannot release the hold; missing client proof fails the media path closed after eight seconds.
After rebuilding the live backend and worker, both a selected-chat run and a later fresh/no-chat
run transitioned greeting to listening and were ended without user speech. Total `voice_turn` rows
stayed `26 -> 26` across both checks and active sessions returned to zero each time, so neither
greeting produced a recognition turn or silently adopted a fresh chat.

The signed-in iOS checkpoint also exposed an honesty defect in independent media controls:
microphone-off feedback incorrectly said that assistant speech was muted. The repaired canonical
composer keeps microphone-only sessions in the shared `listening` state and supplies the explicit
message `Microphone is off.`; speech-only mute says `Assistant speech is muted.`, and the combined
case says `Microphone and assistant speech are muted.`. The shared server message applies across
clients, while the Apple controller mirrors the same distinction immediately during its control
request. No new protocol state was introduced or repurposed.

## Six-surface control, state, and accessibility comparison

The table reports the strongest evidence actually collected in this local Mac session. “Automated
pass” is not a claim that the full candidate-bound C0–C6 or physical acoustic journey has passed.

| Surface | Composer control and state | Accessibility contract | Automated evidence | Live evidence | Current verdict |
|---|---|---|---|---|---|
| Web | Server-owned voice action in the chat composer; activate, listening, muted, speaking, error, recovery, and stop states | Accessible name/pressed/busy/live status plus keyboard operation, non-color status, and a persistent assertive request-outcome alert | Backend conformance/renderer/manifest tests passed and all 32 locked Chromium scenarios passed, including rejection/failure distinction, safe text, no replay, stale-state retention, and typed fallback | The rebuilt signed-in browser exposed active microphone/end/mute controls and reached greeting then listening from both a selected chat and the welcome/no-chat state. Each ended cleanly with zero active sessions; aggregate rows stayed `26 -> 26`, so no greeting became a recognition turn and the no-chat run did not silently adopt a chat | **Pass for implemented local greeting/lifecycle and automated terminal-alert slices**; committed result remains open |
| Windows | Equivalent composer action and voice state reducer; no caller speech configuration | Qt accessible labels/state plus a persistent plain-text non-color alert exposed with the Qt Alert role | Current full offscreen suite: 685 passed, 6 skipped, including exact rejection correlation/no replay, failure persistence, timestamp-fenced clearing, distinct speech failure, and typed fallback | PySide on macOS is diagnostic only; Windows-native audio and package run unavailable | **Pass for source/offscreen slice**; Windows-native and authenticated E2E open |
| Android | Equivalent composer action; permission, listening, mute, speaking, recover, and stop controller states | TalkBack descriptions/state plus a prominent persistent assertive outcome card with icon, title, safe body, and guidance | Core 100 passed; current app 236 passed; connected emulator suite 27 passed with 1 expected release-evidence skip; ktlint, lint, Kover verification/XML, and assemble passed. Older different-turn lifecycle/rejection frames cannot erase a newer notice | Signed-in `emulator-5554` completed selected/no-chat greeting/lifecycle, reconnect, cold-relaunch, and teardown-race checks described below. The new alert behavior is automation-verified; a fresh spoken failure was not injected into the signed-in emulator | **Pass for signed-in local lifecycle and automated terminal-alert slices**; committed result and physical acoustic journey open |
| iOS | Shared Apple reducer with iOS/macOS LiveKit controller and equivalent composer placement | VoiceOver/Switch Control labels, values, hints, non-audio state, and a prominent persistent request-outcome notice | AstralCore 166 passed; current focused iOS controller target passed 39/39; strict format and generic iOS build passed. Older different-turn lifecycle/rejection frames cannot erase a newer notice, and post-result speech failure retains the result timestamp | The normally ad-hoc-signed rebuild preserved the user's sign-in and completed the control/lifecycle journey described below. The new alert behavior is automation-verified; a fresh spoken failure was not injected into iOS | **Pass for source/test/build and signed-in simulator control/lifecycle slice**; committed result and physical audio remain open |
| macOS | Same shared reducer/controller contract and composer placement as iOS | VoiceOver/keyboard state plus the same prominent persistent request-outcome notice | AstralCore 166 passed; current focused macOS controller target passed 45/45; strict format and unsigned macOS build passed | The user completed current-build sign-in, heard the included greeting, and submitted one spoken request. It failed during the first LLM tool-planning call before tool execution; no committed result or spoken recap was produced | **Pass for source/test/build, authenticated greeting, ASR, and failure-path slice**; successful committed result and physical acoustic checks remain open |
| watchOS | Primary chat affordance drives the ticket-bound PCM bridge; no platform-speech substitution | VoiceOver labels/state plus the same persistent request-outcome notice and dictation fallback | Current focused watch target passed 23/23; strict format and generic watchOS build passed. The shared timestamp fence and watch controller retain newer terminal notices across stale churn | App remains at the device QR/code login boundary; current preserved-data sandbox scan found no network-cache, token, provider, voice, audio, or crash marker | **Pass for source/simulator-build/retention slice**; authenticated bridge and physical Watch audio open |

All clients classify the required voice protocol dispositions in their checked-in manifest guards.
The Android drift guard was additionally tightened during this run to reject missing required voice
frames. The shared UI protocol pins correlated `new_chat`/`chat_created` fields. An independent local
C0–C6 checkpoint exercised the canonical fixture through the backend/web renderer, shipped web
client, Windows reducer, Android wire/controller, shared Apple plus iOS/macOS controllers, and
watchOS reducer with zero ignored required frame/action. It remains local simulator/offscreen
evidence, not physical, Windows-native, staging, or complete voice-journey evidence.

## Backend, media, and worker evidence

| Check | Result | Scope and limitation |
|---|---|---|
| Compose health | Rebuilt backend and voice-worker images plus PostgreSQL and digest-pinned LiveKit are running; `/readyz` returns backend/database ready and the rebuilt backend has zero restarts. The current backend image is `sha256:869d634bf25c2312404cc95ba31a9648b0672a3407329dbb4fc3cca360ec9716`; current worker is Linux/arm64 `sha256:f54348a42b6e4fe12424a766d032832bbba76e8de1733d7a6ece532c7f23869e`; LiveKit is `sha256:3497163e15c48fef6e7830c78716f9e9d5edc28abf7aa90b61c86e93bbc306b1` at source revision `3b9f118327b257301083a7c4aa46076c8012918a` | Local four-service topology only; Keycloak/test-worker, immutable dual-architecture candidate, public WSS/TURN, and qualifying external staging are absent |
| Durable terminal repair | The maintenance sweep correlates ended accepted turns only to the exact owner/chat/request/connection/operation kind and durable operation terminal. A completed operation requires exact committed acceptance/result proof to become `succeeded`; otherwise it fails closed. Failed/retryable/cancelled operations map to honest terminal states, preserve an exact committed result when present, clear an aborted result link otherwise, and notify current same-user UI sockets once per repaired turn. The observed stranded turn changed from `processing` to `failed`, retained `recap_source=terminal_status`, cleared the aborted result link, and remained stable across the next restart | This repairs future in-process finalizer loss and makes failure visible to connected clients; it does not recreate a disconnected historical notification or synthesize missing content |
| LLM transient hardening | Per-call clients use a 60-second default timeout and disable SDK-owned retries. The orchestrator owns a three-attempt budget only for 408/409/425/429/5xx, timeout/connection exceptions, and an internal HTML-maintenance marker; other 4xx, malformed responses, and unknown exceptions fail after one attempt. Logs/audits receive only bounded class/status/existing-category metadata and never raw provider bodies | Reduces intermittent failure impact without hiding deterministic configuration/request defects; it does not prove the prior exact provider response because that body was intentionally not retained |
| Real direct-RTC probe | Post-rebuild probe passed RTC capture, Silero VAD, Whisper ASR, proof validation, and ordinary dispatch; greeting and final transcript frames were observed; the ordinary-dispatch refusal was correlated and digest-valid | Content-free synthetic probe; expected `permission_denied` at missing normal user-LLM boundary |
| Speech profile | Authenticated capability returned ready for LiveKit, worker, ASR, TTS, and voice. The content-free worker preflight passed exact Whisper/Kokoro/`af_heart`/WAV 24 kHz. Selected-provider inventory SHA-256 is `1f143aafa0647ecfbf491e81bd7019545aefeaed41b8604a2d5ff2b8f94dc8b4`; fixed-profile SHA-256 is `fd663421899c76f54cff4d8a425d24860a0e2f4e8297a9600a9603ce5eb0cc3b`. Live attributed result openings synthesized at 32,768 and 33,792 samples, below the 36,000-sample opening bound | Local configured service, not an inventory/availability SLA claim |
| Cadence/lifecycle | Deterministic 2/19/21/65-second, multi-minute, concurrent-turn, result-attribution, mute, terminal-fence, and ephemeral-acoustic-boundary tests passed | The candidate-bound 100-trial percentile matrix remains open |
| Direct client playout | Web, Windows, Android, and Apple implementations enforce manifest-before-media identity, sequence, sample, aggregate, serialization, and interruption bounds. Web and Windows now clear local playout before starting the generation-fenced stop request, matching the existing Android/Apple local-first behavior; delayed-success and transport-failure tests prove the local fence does not wait on HTTP. Android additionally rendered a greeting through the reproduced publication/subscriber race | The candidate-bound acoustic p95 <=500-millisecond / 100% <=1-second interruption oracle, physical speaker/microphone/AEC behavior, and natural spoken barge-in remain open |
| Capture-after-playout fence | Worker capture remains closed across source drain; the coordinator releases it only after matching client and worker terminals. Missing proof times out closed after eight seconds; stale/duplicate/older announcements, routine capture enables, mute/end/reconnect/rotation, and stale listening are covered | Live web greeting produced no turn after the repaired images were deployed; physical echo-tail and spoken barge-in remain open |
| Admission/isolation | Five real repository/runtime sessions plus a 15-thread PostgreSQL admission race passed owner/room/device/connection/worker/takeover/capacity isolation checks; no content appeared in controls, errors, or metrics | Deterministic local isolation evidence, not the five-user 30-minute soak |
| Locked worker test profile | The ordinary one-shot `voice-worker-test` remains `network_mode: none`, read-only, non-root, capability-dropped, and free of environment, ports, volumes, dependencies, credentials, and product data. The freshly rebuilt Linux/arm64 test image is `sha256:aebe3588b4fd3d3c720cde29cf1aa2f505386901d9e5d8c957a6464a5cc6c4a8`; its default suite passed `261` with the opt-in integration case deselected. The separate internal-network lane then passed one real-server test in 2.25 seconds and removed every disposable container/network. It pre-pulls the digest-pinned server and builds the worker before minting separate 90-second grants, so cold image preparation cannot consume grant lifetime. Nonzero client PCM crossed real LiveKit/Opus into the production ASR adapter; reliable correlated transcript/result data and nonzero 24-kHz TTS PCM returned through the production RTC session. | Local uncommitted diagnostic only. The RTC-only dependency audit requires this exact run and its digests to be repeated as candidate-bound evidence, so T168 remains open; this also does not satisfy T174 staging/topology evidence. |
| RTC diagnostic-retention boundary | LiveKit server profiles pin vendor logging to `warn` and Pion to `error`; web selects `silent`, Windows and the worker disable LiveKit loggers, Android selects `LoggingLevel.OFF` and disables WebRTC logging, and Apple calls `LiveKitSDK.disableLogging()` before constructing a room | Prior local info-level participant diagnostics included SDP and ephemeral ICE fields. After zero active sessions were confirmed, the LiveKit container was recreated and those old logs were removed; post-hardening web, Android, and server checks exposed no signaling/SDP/ICE/bearer fields. This is bounded local evidence, not T177 certification |
| Recent failed-run retention sample | Aggregate-only checks over the backend/worker/LiveKit logs found zero matches for the observed transcript text, bearer/grant/control/proof markers, SDP/ICE markers, or provider-payload markers; recent worker and backend audio-file counts were both zero | Bounded 20-minute local sample only; no log bodies or credentials were retained in this record, and T177 remains open |
| Apple authenticated transport retention | Audited REST, authentication/refresh, WebSocket, app, and voice paths now use `NoStoreHTTP`: ephemeral sessions, no URL cache, credential store, or cookie store, cache-bypassing requests, and `no-store`/`no-cache` headers. Both app entry points eagerly purge the current bundle cache database, journal/WAL/SHM, and `fsCachedData`, and fail closed on a removal error | AstralCore passed 159/159, including 11 focused transport/purge cases; strict format passed. Fresh preserved-data iOS and watch launches had zero active cache/token/provider/voice/audio/crash markers. The signed sandboxed macOS build had the same zero result across all three active cache roots. Four known artifacts from an older unsandboxed global cache were moved recoverably to Trash; because Trash could not be inspected and was not permanently emptied, T177 remains open |
| Aggregate local privacy audit | Inspected a 46-ended-session snapshot, 26 turns, schema shape, 1,775 audit rows including 186 recent rows, all four service logs, 1,194 runtime-root files, and the available Android/Apple simulator sandboxes without recording content. Later lifecycle-only checks, including iOS and macOS control runs, raised the database to 54 total/0 active sessions while turns stayed 26; those later rows were not folded into a second full aggregate audit. | Database schema had no audio/transcript/recap/token/provider-body column or JSON/LOB payload; audit/log/runtime exact-marker scans found zero observed secret, transcript, bearer/JWT, SDP/ICE, provider-body, or audio matches. Old screenshots under backend temporary storage, inaccessible Trash, uninspected browser storage, physical clients, Windows, staging/protocol captures, and the missing successful ordinary result/recap keep this bounded evidence from certifying T177 |

## Command evidence

The following completed results were observed from the current local tree:

| Area | Command or lane | Result |
|---|---|---|
| Web | locked ESLint, product-isolation check, and Chromium `voice-conversation-065.spec.js` with fake media | Current suite 32 passed and ESLint passed with zero warnings. New cases cover persistent prominent alerts, safe message bounds, request non-start/non-completion, stale notice retention, explicit clearing, no rejection replay, typed fallback, and distinct post-result speech failure. Earlier diagnostic coverage hashes remain non-candidate evidence. |
| Web/backend static | client conformance, renderer, and manifest suites | 73 passed |
| Windows | full offscreen PySide suite on macOS | Current suite 685 passed, 6 skipped, including the prominent terminal-alert, exact rejection/no-replay, timestamp ordering, typed fallback, and speech-failure cases. Earlier coverage producer hashes remain non-candidate evidence. |
| Windows focused | voice contract, lifecycle, playout, and terminal-notice aggregate | Passed; the full suite above includes all current focused assertions |
| Android | `ktlintCheck :app:lintDebug :core:test :app:testDebugUnitTest :core:koverVerify :app:koverXmlReport :core:koverXmlReport :app:assembleDebug` | Successful with core 100/100 and app 236/236. Ktlint, lint, Kover verify/XML, and assemble passed. App/core Kover XML SHA-256 values are `6593b45457ffc296cec345215743d99aa7400bdc40dfd6b58eac11017dca659d` / `d0a7de9759b5d64699abd52ac6229c12a5c805cb1799d054eb8db9c19936ff65`; debug APK SHA-256 is `254868198527da356065207e7cc6d24d9463eda38530c9aadd16d14ac6efee50`. These are local dirty-tree diagnostics, not candidate evidence. |
| Android connected | `:app:connectedDebugAndroidTest` on `emulator-5554` | 27 passed, 1 expected release-evidence skip; XML SHA-256 `db67a70531e05c02ca9a12f6400aa5fb85a75a39af95813129ad0419dc39852c` |
| Apple shared | strict recursive `swift-format`; `swift test --package-path apple-clients/AstralCore --enable-code-coverage` | Strict format passed and AstralCore passed 166/166 |
| Apple app/controller | exact iOS, macOS, and watchOS focused targets | `xcresulttool` confirmed iOS 39/39, macOS 45/45, and watchOS 23/23 with zero failures or skips. These targets cover the shared timestamp fence and controller-specific notice retention; they are simulator/local Mac tests, not physical-device passes. |
| Apple builds/format/coverage | strict recursive `swift-format`; unsigned generic iOS/macOS/watchOS builds; retained earlier real iOS per-file xccov replay | Strict format and all three fresh unsigned builds passed after the terminal-notice ordering change. Earlier signed artifact and per-file diagnostic hashes remain recorded below, but no equivalent candidate-retained real macOS/watchOS per-file report exists, so T173/T180 remain open. |
| Backend terminal repair/provider hardening | host integrated voice matrix; focused LLM matrix; rebuilt Python 3.11 image matrix | Host voice matrix passed 1,010 tests with 3 expected skips after its one path-sensitive source guard was made repository-root safe. Focused LLM retry/redaction tests passed 56/56. The rebuilt Python 3.11 image passed 166/166 across durable dispatch, session repair/notifier, conversation publication, client factory, streaming, wave-0 LLM, and voice-dispatch parity. Ruff and diff checks passed. |
| Backend timing/announcements | cadence, announcement, and concurrent-turn performance aggregate | 60 passed, including 9 deterministic timing/performance cases |
| Voice boundary budgets | Python 3.11 LiveKit/readiness/session/interruption/speech-adapter focused aggregate | 128 passed. Launch defaults/ranges now have explicit assertions for the 45-second session lease, 300-second grant ceiling, LiveKit operation/cache limits, 8-second worker readiness, 250-ms worker quiescence operations, and 5/15/8-second speech-adapter budgets; these deterministic bounds do not claim the open acoustic percentile result |
| Backend recap-matrix diagnostic | Fixed synthetic/non-PHI matrix evaluator plus existing recap, sensitive-consent, and integrated terminal-announcement tests | Exact `20/20/20/15/10/15` distribution passed `100/100` cases (`100.0%`): terminal-state accuracy `100/100`, unsupported-claim rejection `100/100`, material-caveat preservation `55/55` applicable, next-action preservation `55/55` applicable, fabricated-progress violations `0/100`, and pre-consent disclosures `0/15`. Focused Python 3.11 aggregate passed `65`; evaluator branch coverage was `98%`; Ruff and diff checks passed. Fixture SHA-256 is `ed720d634dd166c2e84caa1a8e59a72918532cef7479da79297c9d63a1ab8afc` and evaluator SHA-256 is `c07da69f1f89a40c37ec05c29cbc7a005f777f366094395e06c88a9f2e7f193f`. This is deterministic text-only diagnostic evidence, not the SC-005 human review or SC-012 listening panel, so T178 remains open. |
| Backend media-control honesty | Canonical composer publisher/model tests after the microphone-off message repair | 16 passed. Microphone-only remains canonical state `listening` with message `Microphone is off.`; speech-only remains `muted` with `Assistant speech is muted.`; the combined case reports both controls. Ruff check passed, Ruff formatting was applied to the two touched Python files, and the subsequent format check passed. |
| Backend chat deletion | deletion/publication/session aggregate rerun | 35 passed |
| Backend five-user isolation | focused isolation suite, five repeated runs, and related coordinator/runtime/session/control/admission aggregate | 3 passed; 15/15 repeated invocations passed; 95 related tests passed |
| Backend integrated Python 3.11 | runtime-image voice/orchestrator/shared/performance/schema set, with repository-layout checks separated because `tooling/` and Compose files are intentionally absent from the runtime image | 460 passed, 5 skipped; repository-mounted contract/closure/layout partition 80 passed; environment-only runtime partition 3 passed |
| Backend exact CI sequence | Python 3.11 root, every explicit nested module suite, then `tests/perf/concurrent_surfaces.py`, using an isolated disposable PostgreSQL database | 5,302 passed, 31 skipped, 2 deselected; 1,900 passed, 7 skipped; performance 1 passed. Cobertura covered 114,862/140,344 executable lines (81.84% overall diagnostic coverage), SHA-256 `ab0689b0349947c9c391a71aaba49cf518c2a5fa1e6e95780c220e20f920d32f` |
| Backend flags-off CI sequence | Same Python 3.11 root/nested/performance sequence with every CI rollback flag forced off, using a separate isolated disposable PostgreSQL database | 5,298 passed, 35 skipped, 2 deselected; 1,900 passed, 7 skipped; performance 1 passed |
| Release-tooling Python | Exact maintained-script coverage lane and release-policy tests | The final combined Python 3.11 run passed 499 tests plus the 28-document link check; 4,187/4,629 lines (90.45%), XML SHA-256 `42053b2855fa202b36873714ff88ac698e962e4e81b68e0d1448b167d49c6ab6` |
| Local cross-language coverage plumbing | Collector, evidence-input, schema, producer, protected-workflow, and decision-attestation tests plus parser hardening | The focused provenance/exporter review suite passed 394 cases; the post-repair release-contract/validator/workflow aggregate passed 275; the current release-contract/quickstart/workflow slice passed 215; the current publisher/bridge workflow suite passed 36; and the exact 20-file release-tooling lane above passed 499. Strict mode requires ten explicit native producer slots: backend, voice-worker, tooling, Windows, web, Android app/core, and iOS/macOS/watchOS. Reports are parsed once and bound by raw, mapped-semantic, and native-semantic identities; aggregate contributions are filtered to producer-owned paths; worker runtime copies map to their tracked shared sources without assigning overwritten shims to two producers; Watch may contain Core but contributes only Watch; changed Core lines must map completely in at least one iOS/macOS archive; and a useful report witness must be a bounded in-range source line from the immutable candidate tree. Regressions reject irrelevant-metadata duplicates, producer masking, wrong Apple targets, path-only/out-of-range witnesses, candidate `sitecustomize`, unbound report bytes, false OCI identity, and mutated raw Apple/Windows artifacts. Protected policy now normalizes candidate-produced Apple xcresults in a fresh job, binds backend/web to the separately attested deployed OCI digest, binds Windows to every member of the build-once artifact, and attests the exact final decision. Decision consumers use a bounded safe extractor rather than raw `unzip`, verify the installed signer digest, and bind the single expected decision member to exact numeric run/artifact identities. The publisher treats the candidate checkout as data, installs the verifier only from the owner-pinned hash lock, writes tag-ownership state before dispatch, and dispatches the exact protected bridge at the new tag ref with tag/readiness/artifact inputs; the 2026-03-10 API-returned `workflow_run_id` is polled directly. Freshness is checked with a 900-second margin before dispatch/signing and again before draft/official mutations. Only the caller/publisher carry scoped `actions: write`; both also carry read-only deployment provenance access, and exact non-self-approved environment review/deployment binding occurs before tag creation. This remains local/static diagnostic plumbing: no immutable candidate diff, real ten-report strict decision, installed protected authorization, live protected/release run, or T003/T180 closure is claimed. Spec 060 T120 remains blocked by the live reviewer, tag-cleanup, durable-orphan, and direct tag-workflow authority contradictions below. |
| Live-DB test isolation | Register pipeline, BYO host registration, and repository guard in the Python 3.11 backend container; host Ruff and diff checks | Three consecutive 16-case runs passed (48 total). Tests now use disposable UUID-scoped mock owners and remove their LLM/user rows; the interactive `test_user` config fingerprint remained unchanged and residual disposable LLM/user row counts were `0/0` after every repeated run |
| Python lint | `ruff 0.15.21 check .` | all checks passed |
| Voice worker Python 3.11 | Earlier no-cache Compose test image plus the exact post-fence Python 3.11 diagnostic image | Earlier candidate slice passed 257 with Linux/arm64 image ID/repo digest `sha256:4dcd04169afc4b60339c5a172a22c88e3ffcff9decf39174506e006bb4bad64f`; the post-fence image passed all 261 worker tests. The latter is a local diagnostic image, not retained candidate evidence |
| Self-speech/playout hardening | Full host voice backend/orchestrator suites; worker host suite; exact Python 3.11 worker image; focused main-agent rerun | 550 passed; 251 passed with 2 staging skips; 261 passed; focused dual-terminal/timeout/reconnect plus history cases passed 15 with 1 environment skip. Ruff, pycompile, and diff checks passed |
| Focused privacy/topology/reconnect hardening | Backend client/deployment/retention aggregate; worker session; Windows, Android, web, and Apple voice paths | Prior backend/worker privacy and topology aggregates remain green. Current terminal-alert additions pass web 32/32, full Windows 685 with 6 skips, Android app 236/236 plus core 100/100 and connected 27 with 1 skip, AstralCore 166/166, and focused iOS/macOS/watchOS 39/45/23. |
| Worker distribution closure | strict fake/exact-profile suite, real isolated LiveKit lane, closure/packaging guards, current image audit, and LiveKit scan | The locked default worker suite passed 261 with one integration deselection; the opt-in real-LiveKit lane passed 1/1; the current repository dependency/closure/topology aggregate passed 56 with one expected host skip. The local test-image digest is `sha256:aebe3588b4fd3d3c720cde29cf1aa2f505386901d9e5d8c957a6464a5cc6c4a8`; LiveKit is pinned to `sha256:3497163e15c48fef6e7830c78716f9e9d5edc28abf7aa90b61c86e93bbc306b1`; integration Compose/config SHA-256 values are `d83c7c17fb936354bc8bafb9f4c9215da28cdbcbb09196abf72653b54d475cd2` / `b935d38ad1f39cbb57cdfdf883e02c5a474783f325924f78fad39b1d7f052d85`; `CLOSURE.json`, worker lock, and Silero VAD SHA-256 values are `9ef9e195cd73ba3ff536b7c4d3ec4c15f8ab13472c7fcfe8dc10e4749da6b074`, `fb86c9318d01ce59afaccba57842ddde1d098444e527c70b272b81af4ebc61b3`, and `597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004`. Exact speech inventory/profile digests remain `1f143aafa0647ecfbf491e81bd7019545aefeaed41b8604a2d5ff2b8f94dc8b4` / `fd663421899c76f54cff4d8a425d24860a0e2f4e8297a9600a9603ce5eb0cc3b`. The inventory is not a final approval fingerprint. DHI-backed signature/SBOM/VEX, same-candidate dual-architecture evidence, clean worker scans, owner approval, and candidate-bound repetition are absent; the fallback worker scan found 18 HIGH and 6 CRITICAL findings. T004 and T168 remain open. |
| Contract validator | isolated hash-locked validator plus `git diff --check` | Passed C0–C6: 5 aggregate cases, 23 voice positives, 25 worker positives, 40 negatives, 6 proof vectors, 2 OpenAPI positives; diff check clean |
| Spec Kit Analyze | Exact `SPECIFY_FEATURE_DIRECTORY=specs/065-conversational-voice` ownership/preflight and fresh read-only cross-artifact analysis after numeric interruption/reconnect/readiness/egress and T120/T168/T180 corrections | 57 functional requirements, 13 success criteria, 180 contiguous unique tasks, and 100% requirement coverage; zero ambiguities, duplications, constitution issues, critical issues, unmapped tasks, or other findings. Ledger remains 166 complete and 14 open |
| Six-surface C0–C6 reducer checkpoint | Canonical fixture through backend/web renderer and shipped web client, Windows reducer, Android wire/controller, shared Apple and iOS/macOS controllers, and watchOS reducer | Current failure-notice/order additions pass web 32, full Windows 685 with 6 skips, Android core 100/app 236/connected 27 with 1 skip, AstralCore 166, and focused iOS/macOS/watchOS 39/45/23; no required frame/action is ignored. Mac simulator/offscreen limits retained. |

## Live-login boundary

No credentials were entered by automation. The user completed sign-in on Android, iOS, and macOS.
Android completed the signed-in RTC greeting/listening/mute/lifecycle checkpoint. Reinstalling the
normally ad-hoc-signed iOS simulator build restored the existing authenticated state without
automation handling credentials, and its post-fix microphone/assistant-mute/end checkpoint passed.
The user later completed current-build macOS sign-in without automation handling credentials, heard
the included greeting, and submitted one spoken request. That request failed during its first LLM
tool-planning call before tool execution and did not produce a committed result. watchOS remains at
its device QR/code boundary. The web shell was live-tested in the existing local context.
Windows-native verification is unavailable on this Mac.

## 2026-08-02 authenticated live checkpoint

Product commit `dfea619` was pushed after merging the same-tree remote changes. All simulators were
closed and reopened with application data preserved. Android, macOS, and watchOS remained
authenticated; the user reauthenticated iOS. No credentials were handled by automation.

The requested switch to `zai-org/GLM-5.2-FP8` could not be saved because the provider returned a
bounded `503` diagnostic indicating that the model had no live tunnels. The previously working
`google/gemma-4-31B-it` configuration therefore remained active. A subsequent macOS end-to-end turn
produced a committed text result and began its spoken result with the 1.5-second opening, but the
remaining approximately 12 seconds of reserved continuation speech were dropped after the cadence
runner reported `stream_handoff_budget_exceeded`. A later turn committed after the user had already
ended the voice session and correctly emitted no media across that explicit session fence.

The recap repair now treats the 250-ms handoff budget as a maximum source-start latency rather than
an artificial minimum delay. Reserved recap quanta continue immediately, and a true late handoff
still fails closed. Successful terminal turn frames carry an optional, exact-turn
`speech_outcome` (`source_finished`, `failed`, or `suppressed`); `source_finished` asserts only
worker/source completion, never client audibility. Web, Windows, Android, iOS, macOS, and watchOS
reject the field outside a succeeded turn. Result-only local playout failures are fenced to the
matching turn and surface a persistent, prominent `Speech playback failed` notice while stating
that the committed text remains available. Greeting/progress loss cannot make that claim, and mute,
background, stop, barge-in, stale media, and late worker-terminal races cannot replay or mislabel a
recap.

The first post-scheduler macOS retest committed a text result but the app then entered an AppKit
view-update livelock before recap playout: CPU reached approximately 99.5%, memory reached 2.2 GB,
and LiveKit disconnected. A local process sample at
`/tmp/AstralDeep_2026-08-02_102254_WaFr.sample.txt` attributed the loop to continuously animated
SwiftUI progress/shimmer views inside the transcript `LazyVStack`. macOS now uses static busy
affordances while iOS retains animation, and the redundant nested turn identity was removed. The
macOS microphone/output capability probe now reads the current CoreAudio route, restoring the voice
button without relying on AVFoundation device enumeration.

Post-repair integrated verification passed: 128 focused backend tests both on the host and in the
new Python 3.11 product image; web Playwright 48/48 plus locked ESLint; Windows 77 focused tests;
Android `ktlintCheck`, `:app:lintDebug`, `:core:test`, `:app:testDebugUnitTest`,
`:core:koverVerify`, and `:app:assembleDebug`; AstralCore 168/168; macOS AstralApp unit tests
136/136; strict recursive Swift formatting; and unsigned iOS/watchOS plus signed macOS builds. Ruff,
JavaScript syntax, JSON parsing, and `git diff --check` passed. The rebuilt product image manifest
list is `sha256:aa243d4e87777cb74587b679830decf673d8689c8f5b88c0096ffa83b066815f`, and its application
manifest is `sha256:ba99ff772eacf4d5f12514400e161197a98f12fa708c5d0bd2477525203f148f`;
the recreated backend returned `{"status":"ok","db":"ok","agents":10}` and the voice worker
completed its expected challenge/reconnect. The rebuilt macOS app is awaiting user sign-in for the
final audible full-recap confirmation, so no live audibility claim is made yet.

A lower-priority follow-up is to replace verbose raw provider detail in the connection-test UI with
bounded safe guidance while retaining content-free diagnostic codes on the server.

## Explicitly unverified or failing release claims

- No immutable candidate SHA or candidate-bound evidence bundle exists for these uncommitted changes.
- The worker is deliberately marked `distribution-approved=false`: `backend/voice_agent/CLOSURE.json`
  is an inventory schema rather than a final approval fingerprint, the development marker is all
  zeroes, and the required DHI login-backed signature/SBOM/VEX plus same-candidate dual-architecture
  zero-HIGH/CRITICAL scan and owner-reviewed closure fingerprint have not occurred. The local
  fallback worker scan currently reports 18 HIGH and 6 CRITICAL findings; it is not releasable.
- No qualifying external staging run with public WSS/ICE/TURN and real Keycloak has occurred.
- The not-yet-installed protected readiness design remains intentionally inactive after a security
  audit found durable repository/staging/provider credentials reaching candidate-controlled backend,
  web, Windows, Android, Apple, and worker execution paths. Masking, step scoping, and environment
  approval do not prevent candidate code from reading or exfiltrating those values. The protected
  caller inherits no repository secrets and requires a separate
  `RELEASE_EPHEMERAL_CREDENTIALS_READY=true` gate in addition to normal activation. Defense in depth
  no longer relies on those variables: the literal first `stage-deploy` step fails unconditionally
  before runner binding, registry access, checkout, or candidate execution until the external issuer
  integration is implemented in protected code. Future deployment exports a canonical exact runner
  identity before any candidate can start, and cleanup consumes only that output, with no secret or
  second environment approval that could suppress teardown. That Actions cleanup is not a durable
  finally block: final activation also requires a unique host scheduling label and a protected
  boot/periodic orphan reaper that proves run/attempt-bound `revoke-all` and namespace teardown after
  cancellation, runner loss, host restart, and stale leases. Final activation still requires a
  protected mint/expiry/replay/revoke lifecycle for narrow run-scoped principals/provider access plus
  candidate egress isolation; the already request-generated LiveKit/control credentials do not repair
  the other durable secrets. Spec 060 T120 is also open after the live publisher bootstrap was found to permit
  self-review with only the requester account configured, while the publisher correctly requires a
  distinct reviewer. Active no-bypass ruleset `19078549` also forbids deletion of the same strict `v*`
  tags required by disposable/failure cleanup; the workflow now fails visibly on residue, but activation
  requires a reviewed rollback namespace/policy that preserves immutable official tags. That ruleset
  also leaves new `v*` creation unrestricted, while the deployed v0.3.0 verifier pins only
  the tag-ref workflow identity rather than the workflow digest; direct dispatch of changed tag-ref
  signer bytes must be prevented by a trusted-creation authority or verifier migration before activation.
  The final decision itself is now attested before upload, and the bridge,
  controller, and publisher verify the exact installed `release-readiness.yml` signer digest. The
  all required release-readiness/publisher repository variables are absent remotely, the staging environment is absent, the protected
  caller is not installed, and no protected or release run occurred during this audit.
- No 100-trial per-client percentile set or five-user 30-minute soak has been run. The deterministic
  2/19/21/65-second and multi-minute timing matrix did pass locally, but it is not candidate-bound
  percentile or soak evidence.
- The bounded vendor-log and active Apple-cache hardening checks passed, including post-recreation
  LiveKit log inspection and clean preserved-data iOS/watchOS plus sandboxed macOS launches. Four
  pre-sandbox macOS CFNetwork artifacts containing JWT-shaped values were moved from the active
  global cache to Trash through the recoverable OS path. Trash is privacy-protected from this
  process and was not permanently emptied, so that historical data remains an explicit residual;
  no complete representative database/filesystem/log/metric/trace/audit/protocol/crash-artifact
  zero-retention inspection has been certified.
- The fixed 100-case synthetic/non-PHI recap matrix passes the deterministic text-only rubric at
  `100/100`, with zero fabricated-progress and zero pre-consent-disclosure violations. This does
  not substitute for SC-005's human review, and no five-rater/30-clip `af_heart` listening panel has
  been completed; T178 remains open.
- No Windows-native package/audio evidence exists, and no physical Android, iPhone, Watch, headset,
  Bluetooth, echo-cancellation, acoustic-loop, or natural barge-in result is claimed.
- The full signed-in success path through an ordinary user/System LLM, committed result, and spoken
  result summary has not yet been exercised. The current user-owned provider row is encrypted,
  configured, and independently healthy for model inventory, title generation, plain, streaming,
  tool-choice, and full-catalog probes. The user's real spoken request nevertheless failed during
  its first tool-planning call before selecting or running a tool. The exact retired provider body is
  intentionally unavailable; evidence supports an intermittent or request-specific upstream
  failure, not a deterministic configuration failure. The operation and result commit aborted
  honestly, and the ended turn was repaired to `failed` from durable terminal metadata.

These limitations keep T003–T004, T061, T168, and T171–T180 open. They are not converted into pass
claims by simulator, mocked, offscreen, or Mac-only evidence.

## Final integrated rerun

The backend and voice-worker were rebuilt and force-recreated at the earlier runtime checkpoint. At
that checkpoint, the backend image was
`sha256:8618c9f5ae3d05349b60e59464accb0d6c51eabc688db55fca97015eb5911064`, the worker image was
`sha256:90c31865078aa3364588fc555f18b92493136ccd8007d2d6277b16adc9afa93b`, the LiveKit image was
`sha256:3497163e15c48fef6e7830c78716f9e9d5edc28abf7aa90b61c86e93bbc306b1`, and all observed restart
counts were zero. `/readyz` returned backend/database ready. Later RTC diagnostic hardening changed
the tree; an intermediate local Linux/arm64 checkpoint used backend image
`sha256:600808451b7097acd3d465a14c2b9290d725e3c8200428b5c67fb2cbc9679bba` and worker image
`sha256:8c064c21a8001669103c44136d729b71f453fce4a184b139f663efc5b2e440c3`.
The capture-after-playout repair then rebuilt the current worker image as
`sha256:f54348a42b6e4fe12424a766d032832bbba76e8de1733d7a6ece532c7f23869e`.
The Compose test image digest is recorded above. These are uncommitted local Linux/arm64 images, not
immutable candidate or dual-architecture release hashes.

After zero active sessions were confirmed, the exact local LiveKit container was recreated to
discard the prior info-level vendor diagnostics. This nonrecoverable cleanup removed container logs
only, not retained user audio or conversation data. The recreated server remained healthy under the
`warn`/`error` logging posture, and post-change web/Android/server inspection found no signaling,
SDP, ICE-credential, or bearer-token patterns.

The post-rebuild content-free RTC probe returned `session_started=true`, `greeting_received=true`,
`final_transcript=true`, `detected_language=en`, and `digest_valid=true`; it observed one RTC
announcement-media frame and the correlated UI/control lifecycle. The final dispatch outcome was
the expected `accepted=false`, `rejection_reason=permission_denied`, `retry_policy=none` for the
synthetic user with no normal LLM authorization. No speech credential was accepted as an LLM
credential.

The rebuilt web client was then exercised in the open local browser: start, active microphone,
greeting status, assistant mute, and end-to-idle all behaved visibly and accessibly. This evidence
does not substitute for the user-owned authenticated success path or physical acoustic testing.

The current signed-in Android build was also exercised across a real backend restart while its voice
session was active. The backend's per-socket composer revision restarted at zero; Android accepted
the fresh generation immediately, kept the authoritative End/Microphone/Mute controls, and ended
through the visible UI with zero active database sessions. The same generation boundary now has
explicit passing regression coverage on web, Windows, Android, iOS, macOS, and watchOS; Android and
watchOS required implementation repairs, while the other clients already enforced the intended
generation-scoped ordering. The normally signed iOS reinstall restored the user's authenticated
state without automation handling credentials. The user later completed current-build macOS
authentication without automation entering credentials.

A later Android no-chat End run exposed a client-only teardown race: PostgreSQL had already reached
zero active sessions, but a delayed visible-chat update restored “Waiting for the voice chat context…”
beside the Start control. End and other non-acquisition controls no longer issue that chat preflight;
the controller now invalidates and generation/session/grant-fences every asynchronous update,
foreground renewal, activation, media-connect, and media event completion. That app suite passed
226/226. Exact APK `235e99dfc4bd4fa567b71ab341e4446da865d4b1e8f764e684c8a4cfd23c10af`
was installed over the signed-in emulator state. It started from a clean “Voice is available” state,
reached Listening with database counts `1 active / 49 total sessions / 26 turns`, and End immediately
restored “Voice is available” with `0 / 49 / 26`. Background/foreground remained clean, proving that
the stale projection did not recur and that no greeting was accepted as a user request.

The backend source was synced and restarted again after the capture-after-playout repair, and the
`astraldeep-voice-worker:latest` runtime was rebuilt and force-recreated from the same local source.
`/readyz` returned backend/database ready, the worker was running, and the pre-test database had zero
active voice sessions. In the signed-in browser, start exposed End/Microphone/Mute, greeting advanced
to listening only after correlated playout completion, and explicit end returned the composer to
Start with zero active sessions. The `voice_turn` count remained 26 throughout. The same cycle was
then repeated from the welcome/no-chat state: recent-history count stayed one, `voice_turn` remained
26, and active sessions again returned to zero. Together these directly regress both the
self-speech failure and unintended fresh-chat adoption without retaining transcript or audio
content. An earlier harmless typed request against the mock `test_user` identity confirmed that
live-DB fixture pollution aborts the atomic commit rather than creating false history. That fixture
path is now isolated behind disposable UUID identities. The actual signed-in user later saved and
tested an encrypted LLM Factory configuration, heard the voice greeting, and submitted a real spoken
request. Title generation succeeded, but the first tool-planning call failed before tool selection;
the operation produced no committed result. Separate plain, streaming, auto/forced-tool, and full
tool-catalog probes all returned successfully, so the failure is not reproduced as a deterministic
configuration or catalog fault.

The later iOS signed-in control checkpoint exposed and then regressed the independent-media-control
message defect. The backend canonical composer now distinguishes microphone-only, speech-only, and
combined mute messages while preserving the established `listening`/`muted` state meanings, and the
Apple controller gives the same immediate feedback. The focused backend publisher/model aggregate
passed 16 tests; Ruff check and format verification passed. The current Apple app-unit runs passed
116/116 on macOS and 115/115 on iOS with zero failures, including both new control-message cases.
Fresh signed artifacts were built and identity-checked; their executable SHA-256 values are
`13cfe6b2b35ef56660217744cbe0f3a48e50cbdce3d18826152f69a71c0a8b88` for the ad-hoc-signed iOS
simulator app and `f810a036fbe804fe3ffc6d7b66ec128f85887ea8b0e50a6341a8a96a507b0aeb` for the team-signed macOS
Release.

The rebuilt iOS app preserved sign-in and visibly completed Start, microphone-off, combined mute,
and End. The server-owned frame said `Microphone is off.` for microphone-only and `Microphone and
assistant speech are muted.` after both controls were off; End restored `Voice is available.`. The
database finished at 54 total sessions, 0 active sessions, and 26 turns. The earlier signed-in
macOS source run did create sessions and expose controls, but automation losing foreground/control
transport caused `foreground_reason=connection_lost` and terminal reason `lease_expired` rather
than a current-build success. The user later completed current-build macOS sign-in and heard the
included greeting before the real request failed at LLM tool planning. These simulator/control
slices do not close the physical-device, successful-result, staging, candidate-bound, or
release-evidence tasks.

After the prominent terminal-notice, timestamp-ordering, durable-repair, and bounded LLM retry
changes, the backend was rebuilt and force-recreated once more as local image
`sha256:869d634bf25c2312404cc95ba31a9648b0672a3407329dbb4fc3cca360ec9716`.
It reached healthy with zero restarts and `/readyz` returned `status=ok`, `db=ok`, and ten agents.
The rebuilt Python 3.11 image passed 166 focused durable-dispatch/session-repair/publication/LLM
tests. The prior stranded turn remained durably `failed` with `recap_source=terminal_status`, no
result link, and matching failed operation state; the next maintenance cycle did not repair it a
second time. The host integrated voice matrix passed 1,010 tests with 3 expected skips, and the
current Android and Apple gate results are recorded above. This is live local dirty-tree evidence,
not an immutable candidate, external-staging, physical-device, or release claim.

The current debug APK and current unsigned iOS simulator app were then installed over the existing
Android and iOS simulator applications with data-preserving upgrade commands. Both installs
completed successfully. They were deliberately not launched unattended, so preservation of the
user-owned authenticated state and a fresh visible failure journey remain morning checkpoints; no
credential or login flow was automated.

The final locked web-tooling audit initially found one high-severity denial-of-service advisory in
the CI-only transitive `brace-expansion` 5.0.7 package. The lockfile was advanced within its existing
semver range to 5.0.9; no direct or runtime dependency changed. A clean install with the pinned
Node/npm image then reported zero vulnerabilities for both the complete and production-only trees,
and the package-manager, product-isolation, and ESLint gates passed.
