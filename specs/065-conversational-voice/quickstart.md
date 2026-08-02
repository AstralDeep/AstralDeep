# Implementation and Verification Quickstart

**Feature**: 065-conversational-voice
**Purpose**: Ordered implementation and evidence runbook. Commands are run from the AstralDeep
repository root unless noted. Stop at every real login prompt and let the user authenticate.

## 1. Safety and Ownership Preflight

```bash
git fetch --prune origin
git status --short --branch
git rev-parse --abbrev-ref HEAD
git log -5 --oneline --decorate
```

Expected branch: `065-conversational-voice`. Re-inventory remote spec trees before Spec Kit work.
The authorized feature-064 handoff is now integrated: its final schema revision `064.001` is the
strict predecessor of target `065.001`. Preserve that boundary and fail closed if the checked-out
schema no longer matches it; do not edit the separately owned 064 spec or reconstruct its work.

The current working tree already contains an unrelated owner edit to
`apple-clients/AstralApp/AstralApp.xcodeproj/project.pbxproj`. Preserve and review that diff before
any project-file change; never replace or regenerate it blindly.

The owner has approved the RTC-only architecture. Before distribution, retain matching PR review
for the exact LiveKit server, orchestrator API, worker RTC/NumPy/ONNX/Silero,
web/Windows/Android/Apple pins, all transitives/native artifacts, licenses/notices, CVEs,
hashes/locks, model artifact, package/image impact, and isolated test-only validator lock.

## 2. Configuration Without Secret Disclosure

The existing deployment `.env` contains the operator-provided speech inputs. Verify presence
without printing values; never include `.env`, expanded Compose output, token responses, or
provider bodies in logs/evidence:

```bash
python3 - <<'PY'
from pathlib import Path

required = {"OPENAI_BASE_URL", "OPENAI_API_KEY"}
present = set()
for raw in Path(".env").read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    if key.strip() in required and value.strip():
        present.add(key.strip())
missing = sorted(required - present)
if missing:
    raise SystemExit("missing required speech inputs: " + ", ".join(missing))
print("required speech deployment inputs are present")
PY
```

Compose maps these inputs only into the voice worker as `VOICE_SPEECH_BASE_URL` and
`VOICE_SPEECH_API_KEY`. The orchestrator and agents must not receive or read them. Configure
separate operator-only LiveKit API key/secret, public WSS URL, and voice-control signing secret via
the deployment secret mechanism. Generate strong values locally/through the secret manager; do not
commit defaults or ask end users for them. `livekit-api` and the API key/secret are orchestrator-
only. The worker receives a separate short-lived room join grant only after its control channel is
authenticated; clients receive separate least-privilege grants.

Local development may explicitly use non-TLS loopback/LAN media and speech endpoints. Staging and
production must use trusted TLS, public/reachable WSS and ICE/TURN, and an advertised address usable
by every test device. A container-local `localhost` URL is not valid client configuration.

## 3. Implement in Dependency Order

### 3.1 Shared authority and persistence first

1. Verify the integrated guarded `064.001`→`065.001` migration and its wrong-predecessor refusal,
   then retain the repeat-safe `voice_session` and
   `voice_turn` migrations, immutable execution-base commit/revision anchors and component/layout digests on
   `conversation_commit`, commit/revision versioning on `workspace_layout`, and the no-queue
   `voice_interactive` admission class plus representative-data/idempotency/rollback tests. Keep
   retained voice chat UUIDs out of `chats` FKs; make normal message/commit references nullable on
   hard delete and prove bounded voice tombstones do not block or cascade away correlation history.
2. Refactor `_dispatch_async_chat` into the authenticated durable user-turn dispatcher and replace
   its whole-task `_workspace_locks[chat_id]` hold with short admission/snapshot and terminal
   publication locks. In one admission transaction publish a `user_acceptance` commit under the
   client request generation with the new user bubble plus a complete normal versioned carry-forward
   of current components/layouts and no candidate assistant output, without terminalizing the running operation, allocate/store a distinct
   result request generation, and create its linked private `assistant_result` commit. Then broadcast
   the ordinary user message and fully correlated `user_message_acked`; keep only assistant/canvas
   candidate output private until that result commit terminally publishes. Prove two accepted
   same-chat turns hold simultaneous running leases, use
   immutable context, and component-plus-layout three-way publish without rerunning tools, deleting
   another stage, or erasing a concurrent canvas/layout change. Also prove user LLM selection,
   System LLM separation, raw-token delegation lifetime/
   scrubbing, stable idempotency, denial/confirmation/PHI parity, and cleanup before voice calls it.
3. Add session/turn stores, row-lock/CAS takeover, generation fencing, one-user ownership,
   five-minute true-idle expiry, foreground/background selection, reconnect deduplication, and the
   default `voice_interactive` capacity of ten running/no queued plus two running per user. Verify
   excess work is refused before acknowledgement instead of relabeled as started.
4. Add completion `summary_text`/source to the normal atomic result contract, the committed-visible
   fallback extractor, fail-closed sensitive gate, and one-result consent.
5. Add fake-clock VoiceCoordinator tests for acknowledgement, worker source events, authenticated
   client playout events/server receipt timing, acoustic-evidence boundaries, serialization,
   terminal/wait/mute cancellation, background results, and restarts.
6. Add bound-destination mode to the shared dispatcher: a proof-valid delayed voice turn may target
   only its existing owner-validated recognition-time chat, cannot auto-create/resurrect/switch or
   mutate `_ws_active_chat`, and publishes without navigating the current client. A missing/deleted/
   unauthorized bound chat returns the strict terminal rejection before any message/task. Implement
   the shared voice-aware chat deletion/revocation transaction and prove that it fences current
   media, rejects unaccepted turns idempotently, suppresses accepted-turn speech, clears stage
   anchors/aborts only affected private stages, terminalizes accepted voice correlations without
   cancelling their operations, allows physical deletion, rejects every later replay without a
   second dispatch, and never treats a retained chat UUID as authorization.

### 3.2 Media infrastructure and speech worker

1. Add the digest-pinned LiveKit service/config and exact locked worker image.
2. Implement `streaming_egress` with fixed-origin DNS/peer/TLS/redirect/proxy/time/frame/body/session
   controls and redacted typed errors.
3. Build the no-Agents/no-LLM/no-tools direct-RTC worker with the exact vendored Silero VAD,
   AstralDeep-owned endpointing/reconnect/playout state, exact bounded batch ASR, exact Kokoro TTS,
   barge-in, self-speech suppression, and bounded ephemeral buffers.
   Pin SDK behavior with tests for manual identity/source subscription, connect-return/initial-state
   reconciliation, synchronous callback queueing, finite 16-kHz/32-ms stream overrun, output-epoch
   double-clear interruption, changed track SID on full reconnect, and fresh-Room terminal recovery.
4. Implement the challenge-authenticated worker-pool control channel, coordinator-selected
   `session_bind` assignment with a separate room-scoped worker RTC grant, recognition-time chat
   binding, two-layer final-
   transcript accepted/rejected dispositions and bounded replay, normalized-text digest/HMAC proof, idempotent media-grant
   refresh with worker `media_grant_rotated`/`media_grant_applied`, announcement-media manifests,
   and the strict control schema; test replay, expiry, altered text/proof, wrong
   user/session/room/worker/generation/grant revision, navigation races, malformed frames, overload,
   disconnect, and cleanup.
5. Implement the authenticated watch PCM relay and test codec/sequence/rate/size/duration/ticket
   bounds, no disk spill, and identical transcript/TTS behavior.
6. Replace—not preserve as a bypass—the legacy unauthenticated `/api/voice/stream`, unbounded batch
   upload, caller-selected voice/model, and configuration-only health behavior.

### 3.3 Shared UI contract and all clients

Land the authoritative composer model, REST/OpenAPI implementation, strict protocol validators,
`ui_protocol.json`, ROTE capability fields, and every client disposition together. Session controls
map only to the named REST operations; media capability plus stable canonical `device_id` use
`register_ui`, alongside a fresh client-generated `connection_generation`; the sole new inbound UI
socket frame is content-free `voice_playout_event` (not a `ui_event` action); final text remains `chat_message` with validated
`voice_origin`. The ordinary `new_chat` action/response gains strict correlation fields needed for
no-chat activation. Navigation pauses new capture until the desired chat-context revision is worker-
acknowledged; a speech turn already in progress remains bound to the old chat. Required voice
frames/actions must be `HANDLED`, never silently ignored.

- **Web**: local pinned UMD + license/hash; explicit getUserMedia/autoplay action; room/audio/data
  controller; accessible composer state; visibility/pagehide/logout cleanup; fake-media Playwright.
- **Windows**: reducer/controller; LiveKit RTC + QtMultimedia `QAudioSource/QAudioSink`; stop on
  inactive/lock/logout/quit; collect native wheel/DLLs and QtMultimedia in PyInstaller + manifest.
- **Android**: exact SDK/locks and narrowly filtered JitPack transitive; `RECORD_AUDIO` runtime
  permission; replace conversation-mode system dictation; lifecycle/audio focus/routes; Compose
  accessibility and connected media tests.
- **iOS/macOS**: exact SPM pin on AstralApp targets only; microphone usage, macOS audio-input
  entitlement, privacy manifest, voice-chat audio session/interruption/routes, scene/logout cleanup.
- **watchOS**: keep AstralCore free of LiveKit; add microphone usage/privacy declaration,
  AVAudioEngine/Converter PCM bridge, equivalent reducer/control/accessibility, foreground cleanup.

### 3.4 Activation with no selected chat

Voice activation is explicit, but it must also work from a fresh client with no selected chat. Use
this deterministic sequence on all six clients:

1. Keep microphone capture, room publication, and playback off. Send the additive strict form of the
   existing `new_chat` action through ordinary UI transport with `schema_version: "1"`, current
   canonical UUID4 `connection_generation`, and stable UUID4 `submission_id` and
   `request_generation`; voice does not gain a second chat-creation route.
2. The server uses existing `operation_record` owner-scoped idempotency under operation kind/
   namespace `chat_create`/`new_chat`, storing the normalized input digest. An exact retry returns
   the same logical chat; reuse of either ID with changed input is rejected. `chat_created` strictly
   echoes schema version, all three correlation IDs, and the owner-validated chat UUID4.
3. Adopt/hydrate that chat only if the response matches the still-pending activation IDs and current
   UI connection and the user has not navigated since submission. A delayed/stale response remains
   harmless: it may not switch the visible chat or activate voice.
4. Mint one activation identifier and call the voice-session create REST operation with the newly
   created chat, the client-generated/registered device identifier, the current UI connection
   generation, short-lived `voice_control_binding`, and the client's real reported capability.
5. Only after a successful create response, current media grant, expected worker identity, and
   applied chat context may the client join media, publish microphone audio, play the greeting, and
   begin listening. The greeting is the only announcement whose `turn_id` is `null`.
6. A chat-create, context-sync, session-create, or media-join failure leaves media off, exposes typed
   fallback, and creates no client-assumed or orphaned voice session. Retrying the same stable
   identifiers is idempotent; a deliberate second activation mints new ones.

When a chat is already selected, steps 1–3 are skipped, but ownership, connection generation, and
worker context are still verified before media starts. Conformance tests must prove that a client
never invents a chat identifier, never greets before context sync, and cannot accidentally bind a
fresh activation to a previously visible chat.

### 3.5 Owner binding, announcements, and playout evidence

Every voice REST mutation carries the authenticated owner's client-generated/registered `device_id` and
current UI `connection_generation`, plus the expected session generation and media-grant revision
for every existing-session mutation (create has no prior revision). The server rejects a same-user request from another device, a
prior UI connection, an old owner generation, or an old grant revision. Mute, stop-speech,
consent, end, refresh, and takeover tests cover both matching and mismatching bindings; takeover is
the only operation allowed to replace the owner binding and returns a new generation and grant.
The memory-only control binding expires after at most ten minutes and no later than its Keycloak
token/socket; only the same authenticated registered socket can receive a renewal. Tests cover
expiry, close, reconnect rotation, redaction, and non-persistence.

Every pre-acceptance denial sends the same fully correlated reason/retry disposition to worker and
client. The matching `transcript_rejected`/`voice_submission_rejected` clears the retained final and
submission immediately. `explicit_user_retry` displays a safe retry affordance but never resends or
queues automatically; a deliberate retry mints fresh turn/submission/request IDs. Repeating old IDs
returns the same terminal disposition. Test capacity, deleted/unauthorized bound chat, malformed
final, stale session, and invalid/expired proof cases across disconnect/reconnect.

Before each audio announcement, the worker sends direct LiveKit clients a reliable, ordered,
content-free `voice_announcement_media` frame on `astraldeep.voice.announcement.v1`. watchOS receives
the identical validated manifest through its authenticated bridge. The manifest binds
`session_id`, generation, media-grant revision, expected `worker_identity`, `announcement_id`,
nullable `turn_id`, announcement kind and sequence, quantum role/index, actual fixed-24-kHz sample
count, and the distinct worker audio track SID/name; the watch form instead binds the first/last
assistant PCM sequence. A manifest is one <=96,000-sample quantum, with a <=36,000-sample
`result_opening`; result frames also echo the monotonic durable cumulative reservation, and the
720,000-sample result cap is aggregate across manifests. It
contains no spoken text, summary, transcript, room secret, token, ticket, or raw audio. A null
`turn_id` is accepted only for `greeting`; every acknowledgement, progress, wait, and result
announcement requires the current turn UUID.

`voice_playout_event` is a dedicated content-free client observation frame, not a generic voice
mutation and not evidence of task acceptance. It echoes only the manifest correlation fields plus
the client-generated/registered device, current UI connection generation, local monotonic client sequence,
`started`/`finished`/`interrupted` phase, and observation time. A client emits it only when its
local renderer reaches the correlated audio boundary. Phase is exactly `started`, `finished`, or
`interrupted`; the server rate-limits it to eight <=2 KiB frames/second/device. Every receiver rejects missing, malformed,
oversized, duplicate, out-of-order, wrong-worker, wrong-device, stale-connection, stale-generation,
stale-grant, or quantum-role/index-mismatched manifest/event pairs. Rejected observations cannot
advance the 20-second cadence.

### 3.6 Shared lifecycle and language posture

The following mapping is normative for web visibility/pagehide, Windows inactive/session-lock/quit,
Android lifecycle and audio-focus events, Apple scene/audio-session interruptions, and watch bridge
or scene transitions:

| Condition | Immediate client behavior | Server/worker behavior | Allowed continuation |
|---|---|---|---|
| Background, hidden, locked, or audio interruption | Stop capture and playback immediately, cancel queued local audio, disconnect media, and best-effort PATCH automatic `foreground_active: false`, its bounded reason, plus `microphone_enabled: false`; do not change the user's speech-mute preference | Enter `suspended`, issue capture/speech stop, and continue accepted agentic work; the lease handles a client that could not send cleanup | On foreground, recheck auth, permission, capability, session state, owner connection, and chat context; refresh the grant and rejoin before enabling audio. Never autoplay missed announcements |
| Transient transport or media loss | Stop capture/playback; retain only the bounded exact envelope/proof for an unacknowledged final transcript | Fence the disconnected UI connection/media identity and preserve normal operation idempotency | Reauthenticate, obtain a fresh control binding, use credential-free current state plus stable `refresh_id`, refresh/rejoin, synchronize context, and replay the exact retained final until its fully matching acknowledgement. Never rerun an acknowledged turn |
| Logout, auth expiry, app quit, or session deletion | Hard teardown; clear grants, bridge tickets, timers, queued audio, and retained final-transcript state; best-effort end only while a valid token remains | End or lease-expire the voice session; accepted work follows normal background completion policy | No automatic resume. A later login requires a new explicit activation |
| Cross-device takeover | The displaced client immediately tears down on the new-generation/end notice and discards unacknowledged stale-generation finals | Preserve accepted work, rotate owner generation/grant, and deny all old-owner controls and observations | The new device starts only after explicit takeover, a new grant, and context sync; the old device cannot auto-reclaim ownership |
| Explicit session end | Stop capture/playback, clear voice-only state, and send the owner-bound DELETE once | Stop further voice announcements; accepted task execution and visible text completion continue normally | A later explicit activation creates a new session only through the documented REST state machine |
| Current visible chat deleted or authorization revoked | Stop capture/playout and tear down media immediately | End with `chat_deleted` or `chat_unauthorized`; reject unaccepted turns, suppress accepted-turn speech/result publication to that chat, and let accepted execution/audit follow ordinary deletion policy without resurrecting the destination | Select/create an authorized chat and explicitly start a new session; active sessions never bind null chat context |
| Older turn's origin chat deleted/revoked after navigation | Keep current-chat media active; clear a rejected old final and cancel any queued/playing announcement from an already accepted turn in that chat | Reject an unaccepted turn without chat/message/task/navigation; accepted work follows ordinary policy but emits no later voice progress/recap for that chat | Fresh explicit retry in an authorized chat with new IDs; the current-chat session continues |

Language support is evaluated independently for every final transcript, not once per session. The
client submits recognized text unchanged through the normal user-message path. For normalized
`en` or any `en-*` tag (including `en-US` and `en-GB`), the worker renders the ordinary
acknowledgement/progress/result policy with the fixed `af_heart` `en-US` voice and does not claim
localized pronunciation. For any non-English language, or
canonical `und` when detection is unknown, all clients preserve the visible transcript and text result but speak only the
safe English lifecycle/result-ready notice, expose the explicit `en-US` output limitation, and do
not translate, synthesize a localized recap, switch voices/models, or make a client-side language
guess. The next `en`/`en-*` turn returns to the ordinary path without recreating the session.

## 4. Deterministic Contract and Unit Gates

Add `jsonschema==4.25.1`, `openapi-spec-validator==0.7.2`, and every transitive test-only dependency
to `tooling/contract-ci/requirements.in` plus a committed Python-3.11, hash-locked
`requirements.lock.txt`; do not install them into either runtime image. The committed
`validate_voice_contracts.py` meta-validates both JSON Schemas and OpenAPI and exercises accepted/
rejected instances (plain JSON/YAML parsing is insufficient):

```bash
VOICE_CONTRACT_ENV_DIR="$(mktemp -d)"
python3 -m venv "$VOICE_CONTRACT_ENV_DIR/venv"
"$VOICE_CONTRACT_ENV_DIR/venv/bin/python" -m pip install \
  --disable-pip-version-check --require-hashes \
  -r tooling/contract-ci/requirements.lock.txt
"$VOICE_CONTRACT_ENV_DIR/venv/bin/python" tooling/contract-ci/validate_voice_contracts.py
git diff --check
```

The validator and cross-language contract tests must include valid/invalid instances for every
discriminator branch, greeting-only null turn, UUID4/runtime constraint, context-binding handshake,
packet byte bound, non-empty final/proof vectors, correlated acknowledgement, announcement-media/
playout matching, idempotent grant rotation/recovery, REST operation mapping, and local `$ref`; malformed, generic
`voice_action`, stale, or extra fields fail closed on backend and every client reducer. CI creates a
fresh isolated environment from the same hash lock and archives no credential or instance content.
Include valid single/opening/continuation quantum vectors plus invalid >96,000-sample tracks,
>36,000-sample result openings, result index/role mismatches, non-result continuations, over-budget
worker commands/lifecycle durations, missing/mismatched cumulative-reservation echoes, and a
multi-chunk result whose row-locked next reservation would cross the 720,000-sample aggregate cap;
crash/retry must not refund or reset the reservation.

Run narrow backend suites first (exact filenames are finalized by `$speckit-tasks`), including:

```bash
docker compose --profile test build --no-cache astraldeep voice-worker voice-worker-test
make up
make sync
docker exec astraldeep bash -c \
  "cd /app/backend && python -m pytest -q tests/test_voice_* orchestrator/tests/test_voice_*"
docker compose --profile test run --rm --no-deps voice-worker-test \
  python -m pytest -q /app/backend/voice_agent/tests
docker exec astraldeep bash -c \
  "cd /app/backend && python -m pytest -q tests/test_llm_env_inert.py tests/test_schema_revision_guard.py"
```

The strict fake speech server must verify Bearer authentication, exact models/voice, bounded
in-memory multipart transcription, WAV/24 kHz, redirect rejection, DNS/private policy, timeouts, frame/body
bounds, malformed audio, 401/404/429/5xx, cancellation, and provider-body/secret redaction.

Run the opt-in real-RTC lane separately; it preserves the ordinary test service's
`network_mode: none`, creates a disposable internal Compose network, injects random test-only LiveKit
credentials without printing them, and destroys the project and its tmpfs-backed buffers on exit:

```bash
python3 tooling/voice-worker/run_livekit_integration.py
```

The lane uses the locked worker test image and digest-pinned LiveKit server. Its in-process strict
fake speech origin exercises the production fixed-origin transport, exact Whisper/Kokoro/`af_heart`
profile, 24 kHz output, real client/worker grants, audio tracks, reliable transcript/announcement
data, correlation, and teardown. It never uses the operator speech endpoint or stores audio.

Then mirror every explicit backend/module invocation in `.github/workflows/ci.yml`; the default
`backend/pytest.ini` invocation alone does not discover nested module suites:

```bash
ruff check .
docker exec astraldeep bash -c \
  "cd /app/backend && python -m pytest -q -m 'not integration'"
docker exec astraldeep bash -c \
  "cd /app/backend && python -m pytest audit/tests llm_config/tests orchestrator/tests \
  onboarding/tests personalization/tests scheduler/tests dreaming/tests verification/tests \
  agents/tests agents/journal_review/tests agents/ml_services/tests agents/summarizer/tests \
  agents/web_research/tests feedback/tests security_benchmark/tests shared/tests -q"
docker compose --profile test run --rm --no-deps voice-worker-test \
  python -m pytest -q /app/backend/voice_agent/tests
docker exec -e PERF_CONCURRENT_FLOOR_MS=5000 astraldeep bash -c \
  "cd /app/backend && python -m pytest tests/perf/concurrent_surfaces.py \
  tests/perf/voice_concurrent_turns.py -q"
```

Produce backend/voice-worker/tooling/Windows Python, web Istanbul, Android app/core Kover, and
iOS/macOS/watchOS xccov reports, then run the existing protected-policy implementation of
`scripts/check_changed_coverage.py --fail-under 90`. The combined changed-code result must cover
all maintained languages changed by the candidate and must be blocking; the candidate cannot rely
on the current Python-only soft diff-coverage job.

For the voice-worker producer, run the exact `voice-worker-test` workflow's “Build the voice-worker
runtime and test images” and “Run the isolated voice-worker suite with Cobertura coverage” steps
from `.github/workflows/ci.yml`. They write `build/065/coverage/voice-worker.xml`, validate the XML,
and fail below 90%; the bootstrap verifier rejects a missing or stale replacement.

`backend/tests/perf/voice_concurrent_turns.py` and the corresponding explicit CI invocation must
include two voice turns admitted to the same chat while the first is
blocked in a deterministic tool: both operations reach `running`, the second starts before the
first terminal time, each tool executes once, foreground/background correlation is exact, reverse-
order completion produces monotonic commits, and component/layout conflict cases preserve the
latest committed value plus a safe notice without replaying either side effect. Assert each private
stage remains intact until its own publication; publishing/aborting one never deletes the other.
Exercise an invalid candidate layout reference, flat fallback, targeted cleanup, and reverse-order
publication against current-anchor queries. Start from a non-empty component/layout workspace and
prove `user_acceptance` keeps current reads byte/canonically equivalent, carries both row sets under
its new anchor/revision, and gives the linked result matching execution-base digests.
The same file uses a fake playout clock for two overlapping long turns and two coincident terminal
events. It proves at most two active turns, one serialized stream, <=1.5-second attributed terminal
opening quanta, <=4-second continuation quanta, the second terminal opening scheduled by 1.75
seconds, a 14-second preparation target, positive 250 ms stream handoffs, reserved capacity for the
other due turn, no per-turn gap over 20.0 seconds, and immediate stale-progress cancellation. A separate
staged exact-Kokoro run measures rather than assumes the real p95 two-second start criterion.

## 5. Client Gates

### Six-client fixture and conformance matrix

Commit the shared positive and negative vectors at
`backend/tests/fixtures/voice_065/client_conformance.json` and load that exact file from each client
suite (copying it into a platform test bundle only as a hash-checked build step). The fixture IDs
and required assertions are:

- **C0 — composer parity**: canonical control order, action keys, labels, icons, visibility,
  enabled/pressed/busy state, accessibility names/state, and every required protocol disposition are
  identical to the server-owned model; no client hard-codes a divergent button or ignores a required
  frame/action.
- **C1 — activation and binding**: selected-chat and no-selected-chat startup, stable retry IDs,
  client-generated/registered owner device/current connection, wrong-device/prior-connection denials,
  correlated/idempotent `new_chat`/`chat_created`, stale delayed creation after navigation, takeover
  generation rotation, and no microphone or greeting before worker context sync.
- **C2 — transcript delivery**: partials stay presentation-only; a non-empty final carries the
  bound chat/context, detected language, normalized-text digest, and short-lived HMAC proof; altered,
  expired (more than two minutes), or transplanted proof fails. It replays byte-for-byte only until its acknowledgement
  matches submission/request/turn/chat/current connection or its fully correlated rejection arrives,
  then clears terminally. Explicit retry uses fresh IDs and never duplicates/queues an operation.
  Delayed old-chat acceptance never switches the visible chat; deleted/unauthorized bound chat never
  auto-creates or dispatches. Deleting an already accepted turn's destination abandons only its
  voice/publication correlation, never re-dispatches it, and never implies operation cancellation.
- **C3 — announcement/playout**: a direct-client `voice_announcement_media` or watch-bridge
  equivalent is accepted only from the grant's exact worker identity; the dedicated
  `voice_playout_event` frame is content-free, owner/connection/generation/revision fenced, and
  correlated to observed local playback, quantum role, and quantum index. Stale, duplicate, missing,
  text-bearing, out-of-order, >4-second, >1.5-second result-opening, role/index-mismatched, and
  aggregate-overrun vectors fail closed and cannot satisfy cadence; direct/watch audio beyond the
  manifest's declared sample count is never rendered.
- **C4 — turn language**: consecutive `en`, `en-US`, `en-GB`, unsupported, `und`, then `en-US`
  finals prove per-turn policy, unchanged visible text, fixed `af_heart` `en-US` rendering for every
  English tag, the explicit non-English limitation, safe English-only notice, and recovery without
  session recreation.
- **C5 — lifecycle**: foreground/background, interruption or focus loss, reconnect, logout/auth
  expiry, explicit end, and takeover prove immediate local teardown, no stale autoplay, bounded
  exact replay, fresh-grant/context gates, and continued visible completion of accepted work.
- **C6 — null-turn and identity edge cases**: greeting with null turn is accepted; every other
  announcement with null turn is rejected; unexpected worker, room, device, connection,
  generation, and grant revision are rejected, including the watch relay worker-identity case.

These are the exact planned test paths. `$speckit-tasks` may split cases within a listed file, but
must not replace this shared matrix with client-specific, weaker fixtures:

| Client | Exact unit/contract test paths | Exact integration/UI test path | Required shared fixture coverage |
|---|---|---|---|
| Web | `backend/tests/test_voice_client_conformance_065.py`; `backend/tests/webrender/test_voice_renderer_065.py` | `tooling/web-ci/tests/voice-conversation-065.spec.js` | C0–C6, including visibility/pagehide/logout and direct LiveKit worker identity |
| Windows | `windows-client/tests/test_voice_contract_065.py`; `windows-client/tests/test_voice_lifecycle_065.py` | `windows-client/tests/e2e_voice_065.py` | C0–C6, including inactive/session-lock/quit and packaged worker/audio support |
| Android | `android-client/core/src/test/kotlin/com/personalailabs/astraldeep/core/protocol/VoiceContract065Test.kt`; `android-client/app/src/test/kotlin/com/personalailabs/astraldeep/app/voice/VoiceSessionController065Test.kt` | `android-client/app/src/androidTest/kotlin/com/personalailabs/astraldeep/app/VoiceConversation065InstrumentedTest.kt` | C0–C6, including background/audio-focus loss and direct SDK worker identity |
| iOS | `apple-clients/AstralCore/Tests/AstralCoreTests/VoiceContract065Tests.swift`; `apple-clients/AstralApp/AstralAppTests/VoiceSessionController065Tests.swift` | `apple-clients/AstralApp/AstralAppUITests/VoiceConversationUITests.swift` | C0–C6 on the iOS destination, including scene/audio-session interruption and direct SDK worker identity |
| macOS | `apple-clients/AstralCore/Tests/AstralCoreTests/VoiceContract065Tests.swift`; `apple-clients/AstralApp/AstralAppTests/VoiceSessionController065Tests.swift` | `apple-clients/AstralApp/AstralAppUITests/VoiceConversationUITests.swift` | C0–C6 on the macOS destination, including inactive/route/logout behavior and direct SDK worker identity |
| watchOS | `apple-clients/AstralCore/Tests/AstralCoreTests/VoiceContract065Tests.swift`; `apple-clients/AstralWatchTests/VoiceContract065Tests.swift` | `apple-clients/AstralWatchTests/WatchVoiceBridge065Tests.swift` | C0–C6 through the PCM bridge, with an explicit assertion that the ticket-bound worker identity exactly matches every relayed announcement |

`tooling/contract-ci/validate_voice_contracts.py` validates the same fixture against JSON Schema and
OpenAPI first. Any platform parser that accepts a vector rejected by that validator, or rejects a
required valid vector, fails parity. Each path above must exercise its client's real reducer and
transport serializer rather than a test-only duplicate model.

### Web

Use the repository's digest-pinned Playwright image; no host Node installation is assumed:

```bash
PLAYWRIGHT_IMAGE="$(tr -d '\n' < tooling/web-ci/playwright-image.txt)"
docker pull "$PLAYWRIGHT_IMAGE"
docker run --rm -v "$PWD:/work" -w /work/tooling/web-ci \
  "$PLAYWRIGHT_IMAGE" sh -lc \
  'test "$(corepack npm --version)" = "11.16.0" && corepack npm ci --ignore-scripts && corepack npm run lint && corepack npm exec -- playwright test tests/voice-conversation-065.spec.js'
```

Run protocol/static-asset tests plus Playwright with fake microphone/media permissions, including
activate/deny/revoke, transcript partial/final, duplicate/reconnect, mute/stop/barge-in,
background/logout teardown, takeover, accessible states, and no external asset load.

### Windows/PySide on this Mac (diagnostic only)

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest windows-client/tests -q
```

This is not Windows-native evidence. On a Windows release runner/machine, build the EXE once from
the exact lock, verify packaged LiveKit/QtMultimedia DLLs and offline startup, then run a real
microphone/speaker/AEC route test against the same staged candidate.

### Android

The Mac currently has Android Studio's JBR/SDK and a Pixel emulator, but `gradlew` is not
executable. Use `sh` without changing its mode as an incidental edit:

```bash
JAVA_HOME='/Applications/Android Studio.app/Contents/jbr/Contents/Home' \
ANDROID_HOME='/Users/sam/Library/Android/sdk' \
sh android-client/gradlew -p android-client \
  ktlintCheck :app:lintDebug :core:test :app:testDebugUnitTest :core:koverVerify \
  :app:koverXmlReport :core:koverXmlReport :app:assembleDebug
```

Then boot the configured emulator and run connected permission/media/accessibility/lifecycle tests.
A physical Android device is still required for route, speaker-to-mic echo, headset/Bluetooth, and
real barge-in evidence.

```bash
JAVA_HOME='/Applications/Android Studio.app/Contents/jbr/Contents/Home' \
ANDROID_HOME='/Users/sam/Library/Android/sdk' \
sh android-client/gradlew -p android-client :app:connectedDebugAndroidTest
```

### Apple

```bash
xcrun swift-format lint --strict --recursive \
  --configuration apple-clients/.swift-format apple-clients
swift test --package-path apple-clients/AstralCore --enable-code-coverage

APPLE_PROJECT='apple-clients/AstralApp/AstralApp.xcodeproj'
xcodebuild -project "$APPLE_PROJECT" -scheme AstralApp \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro,OS=26.5' \
  -configuration Debug CODE_SIGNING_ALLOWED=NO -enableCodeCoverage YES \
  -only-testing:AstralAppTests test
xcodebuild -project "$APPLE_PROJECT" -scheme AstralApp \
  -destination 'platform=macOS' \
  -configuration Debug CODE_SIGNING_ALLOWED=NO -enableCodeCoverage YES \
  -only-testing:AstralAppTests test
xcodebuild -project "$APPLE_PROJECT" -scheme AstralWatch \
  -destination 'platform=watchOS Simulator,name=Apple Watch Series 11 (46mm),OS=26.5' \
  -configuration Debug CODE_SIGNING_ALLOWED=NO -enableCodeCoverage YES \
  -only-testing:AstralWatchTests test
xcodebuild -project "$APPLE_PROJECT" -scheme AstralApp \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro,OS=26.5' \
  -configuration Debug CODE_SIGNING_ALLOWED=NO -enableCodeCoverage YES \
  -only-testing:AstralAppUITests/VoiceConversationUITests test
xcodebuild -project "$APPLE_PROJECT" -scheme AstralApp \
  -destination 'platform=macOS' \
  -configuration Debug CODE_SIGNING_ALLOWED=NO -enableCodeCoverage YES \
  -only-testing:AstralAppUITests/VoiceConversationUITests test

xcodebuild -project "$APPLE_PROJECT" -scheme AstralApp \
  -destination 'generic/platform=iOS Simulator' \
  -configuration Debug CODE_SIGNING_ALLOWED=NO build
xcodebuild -project "$APPLE_PROJECT" -scheme AstralApp -destination 'platform=macOS' \
  -configuration Debug CODE_SIGNING_ALLOWED=NO build
xcodebuild -project "$APPLE_PROJECT" -scheme AstralWatch \
  -destination 'generic/platform=watchOS Simulator' \
  -configuration Debug CODE_SIGNING_ALLOWED=NO build
```

Add the new voice tests to the named existing test targets and keep these exact lanes in
`.github/workflows/apple-ci.yml`; CI provisions exact-UDID simulators when a name is ambiguous.
Verify permission denied/revoked, scene background, audio-session interruption/route changes,
transcript dedup, takeover, and accessibility. Simulator audio is diagnostic; a real
Mac/iPhone/Watch is required for acoustic loop/AEC/barge-in evidence.

## 6. Local Live Journey (Stop at Login)

1. Confirm Docker is running and rebuild both backend and worker images; a restart does not copy
   normal source edits.
2. Start PostgreSQL, Keycloak, AstralDeep, digest-pinned LiveKit, and the voice worker.
3. Check ordinary `/healthz` and `/readyz`, then authenticated voice capability. Never paste the
   no-store media-grant response into evidence.
4. Open the real web app. When Keycloak appears, **stop and ask the user to log in**. Do not type or
   fabricate credentials.
5. After login, run: activate → grant mic → greet → speak → final transcript → one normal user
   message → acknowledgement → >40-second work with at least two truthful updates → committed
   result recap → barge-in → mute/unmute → reconnect → stop.
6. Repeat denied permission, missing microphone, service/model/voice failure, typed fallback,
   confirmation/login wait, PHI generic notice + result-bound tap/spoken consent, strict spoken
   task cancellation versus an ambiguous non-control phrase, second-query backgrounding, chat
   navigation, grant refresh with rejected old publisher identity, non-English transcript with
   explicit `en-US` output limitation, and cross-device takeover. For the second query, prove the
   same-chat operation actually reaches running before the first completes; an admission label or
   hidden queue is not sufficient.
7. Launch PySide, Android emulator, iOS/macOS/watch simulators in turn and pause at each real login
   boundary. Record which evidence is simulator-only.
8. Inspect DB, container filesystems, logs, metrics, traces, audit rows, browser/client state, and
   crash artifacts for zero audio, secret, token/ticket, provider body, transcript duplicate, or
   recap-content retention.

## 7. Controlled Timing and Concurrency

Use deterministic server tasks lasting approximately 2, 19, 21, 65 seconds and several minutes.
Collect non-content worker source events, generation/revision-fenced client
`voice_playout_event` receipts, and an ephemeral in-memory locally rendered acoustic probe. Treat
client event receipt as an operational observation and the probe as audible proof; discard waveform
bytes immediately and persist only non-content timing measurements.

For percentile claims, use the spec's `voice-warm-standard` profile: exact readiness green for ten
minutes; five unscored warm-ups; at least 100 eligible measured trials per client; at least 5 Mbps
in each direction; p95 RTT <=120 ms; p95 jitter <=30 ms; and packet loss <=1% before and throughout
the run. Use the nearest-rank percentile, retain timeouts/reconnects/product failures as misses, and
invalidate the whole run if the network/profile preconditions breach. Verify:

- exactly one acknowledgement after durable acceptance and before progress;
- acknowledgement start within 1.5 seconds for at least 95% warm turns;
- next audible start no more than 20.0 seconds after prior audible end for active, unmuted,
  non-waiting turns in 100% of deterministic and staged measured intervals;
- no back-to-back identical progress key and no unverified claim;
- no progress start after terminal/wait/mute/end/takeover fence;
- result audio starts within 2 seconds for at least 95%, at most 80 words/30 seconds;
- every command/manifest/lifecycle event is one <=96,000-sample/4-second quantum; result-opening
  quanta are <=36,000 samples/1.5 seconds, and the result's accumulated quanta never exceed
  720,000 samples/30 seconds;
- authoritative summary always wins; fallback contains only committed visible facts;
- no sensitive detail before one-result consent.
- missing/stale/duplicated client playout events cannot satisfy cadence; the session degrades or
  reconnects without claiming unheard audio.
- with two overlapping long turns, each turn independently stays within the 20-second bound; with
  coincident completions, meaningful attributed recap openings alternate before longer recap chunks
  resume, and no chunk overlap occurs.

Run five distinct authenticated users/rooms concurrently for 30 minutes with takeovers,
reconnects, background results, and overlapping completion times. Assert zero cross-user/chat/room/
turn/audio/transcript/result leakage and bounded capacity errors.

Run the recap review against a fixed synthetic/non-PHI matrix of 20 authoritative-summary
successes, 20 fallback successes, 20 failures, 15 refusals, 10 cancellations, and 15 sensitive
results. Retain every selected case; require at least 95% rubric correctness, zero fabricated
progress, and zero sensitive detail before per-result consent. Separately use at least five
independent raters and at least 30 balanced, blinded synthetic/non-PHI clips spanning greeting,
acknowledgement, progress, interruption, and recap; require mean `af_heart` naturalness/clarity of at
least 4.0/5 and no back-to-back duplicate progress phrase.

## 8. Qualifying Candidate Staging and Release Evidence

Local Docker is necessary but not qualifying release evidence. Extend candidate staging to bind and
attest:

- candidate SHA and backend + voice-worker image digests;
- LiveKit OCI digest and normalized config hash;
- exact locked client artifacts and voice-worker dependency/model hashes;
- real Keycloak, representative migrated PostgreSQL data, voice-worker count/capacity;
- public trusted WSS, reachable ICE UDP/TCP/TURN, exact speech profile readiness;
- platform producer artifacts, timing/soak/privacy/accessibility/listening-panel results.

The current `.github/workflows/release-readiness.yml` states that the external staging host is
inactive. Under Constitution X, provisioning that trusted host and validating this runtime-
infrastructure candidate there is a non-waivable **pre-merge** prerequisite, not merely a release
follow-up. Do not mark local Compose, mocks, simulator audio, Mac PySide, or a different candidate
as staged/Windows-native/physical proof.

Task T189 records the completed collector and schema plumbing for all new voice identity and
coverage inputs. T003 normally requires all ten native reports plus canonical evidence to be
regenerated against one clean committed candidate before a requested implementation push:

```bash
BASE_SHA="$(git rev-parse origin/main)" make prepare-release-evidence
```

That ordinary output is diagnostic only; protected CI independently validates canonical evidence
and trust identities.

### 8.1 Bounded draft-only diagnostic bootstrap for PR 151

Constitution 2.9 permits a narrower path only because the provider cannot produce exact-SHA
evidence until the SHA exists remotely. First make the PR draft, refresh the exact provider default
branch, and prove that the clean candidate contains it and fast-forwards the prior PR head:

```bash
gh pr ready 151 --undo
git fetch --no-tags origin main 065-conversational-voice
SHA="$(git rev-parse HEAD)"
test -z "$(git status --porcelain)"
git merge-base --is-ancestor origin/main "$SHA"
git merge-base --is-ancestor origin/065-conversational-voice "$SHA"
```

Generate and pass the nine fresh Darwin-local producer inputs: `backend_python`,
`voice_worker_python`, `tooling_python`, `javascript`, `android_app`, `android_core`, `ios`,
`macos`, and `watchos`. The Windows Python report and the eight platform evidence documents must
remain absent; they are provider-bound, not locally fabricated. Use the verifier only from a
separate clean checkout at the exact current provider default SHA. Put all records outside the
candidate repository in distinct, initially nonexistent files:

```bash
POLICY_ROOT=/tmp/astraldeep-bootstrap-policy
test -z "$(git -C "$POLICY_ROOT" status --porcelain)"
test "$(git -C "$POLICY_ROOT" rev-parse HEAD)" = "$(git rev-parse origin/main)"
BOOTSTRAP_DIR="$(mktemp -d)"

python3 "$POLICY_ROOT/scripts/verify_release_evidence_bootstrap.py" inventory \
  --repo "$PWD" \
  --github-repository AstralDeep/AstralDeep \
  --pr-number 151 \
  --candidate-sha "$SHA" \
  --feature 065-conversational-voice \
  --output "$BOOTSTRAP_DIR/inventory.json"
```

The inventory must record the local parser's handled exit `2` and exactly these provider-bound
missing inputs: `android_evidence`, `backend_evidence`, `docs_evidence`, `ios_evidence`,
`macos_evidence`, `watchos_evidence`, `web_evidence`, `windows_evidence`, and `windows_python`.
Post one provider-native, unedited comment by an allowlisted lead using the exact marker and v1
field set below; values must come from the live inventory and provider state, never hand-waved:

```text
<!-- astraldeep-release-evidence-bootstrap-v1
{"schema_version":1,"document_type":"release_evidence_bootstrap_approval","repository":"AstralDeep/AstralDeep","pr_number":151,"feature":"065-conversational-voice","branch":"065-conversational-voice","base_branch":"main","base_sha":"<exact-main-sha>","previous_head":"<exact-pr-head>","candidate_sha":"<exact-candidate-sha>","approved_paths":["<every exact changed path>"],"provider_bound_missing_inputs":["<exact sorted provider-bound inputs>"],"inventory_sha256":"<sha256>","policy_commit":"<exact-main-sha>","local_gate_attestation":{"status":"passed","candidate_sha":"<exact-candidate-sha>","commands":["<every exact passed local command>"],"evidence_input_sha256":{"<local producer>":"<report sha256>"}},"structural_blocker":"Provider evidence requires the remote exact SHA.","purpose":"Run diagnostic CI without merge or release authority.","expires_at":"<RFC3339, no more than 168 hours after comment creation>"}
-->
```

A mistaken approval comment must not be edited; post a new correct approval. Then verify and push
through the lease-bound verifier. Never run `git push` directly during this window:

```bash
python3 "$POLICY_ROOT/scripts/verify_release_evidence_bootstrap.py" verify \
  --repo "$PWD" \
  --github-repository AstralDeep/AstralDeep \
  --pr-number 151 \
  --candidate-sha "$SHA" \
  --inventory "$BOOTSTRAP_DIR/inventory.json" \
  --output "$BOOTSTRAP_DIR/verification.json"

python3 "$POLICY_ROOT/scripts/verify_release_evidence_bootstrap.py" push \
  --repo "$PWD" \
  --github-repository AstralDeep/AstralDeep \
  --pr-number 151 \
  --candidate-sha "$SHA" \
  --inventory "$BOOTSTRAP_DIR/inventory.json" \
  --preflight-output "$BOOTSTRAP_DIR/push-preflight.json" \
  --receipt-output "$BOOTSTRAP_DIR/push-receipt.json"
```

Every repair SHA requires a fresh inventory, unedited approval, verification, and lease-bound
push. Repairs may address only the approved CI/evidence execution without weakening product
behavior or gates. A skipped privileged `release-readiness` job is expected while the PR is draft.
Before `gh pr ready 151`, the final exact SHA must regenerate all ten reports and canonical
provider evidence, pass the full local parser and protected changed-code coverage, and receive the
protected release decision. T179, T180, T004, candidate staging, trust/security, schema, exact
speech, PHI, and isolation checks remain non-waivable.

## 9. Exit Criteria

Before rollback or an operator drain, follow `deploy/livekit/README.md`: disable
`FF_CONVERSATIONAL_VOICE`, recreate the backend to close admission/advertising, let leases and active
sessions drain without cancelling accepted agentic work, and stop the worker/media services only
after content-free capacity reports zero. Recovery requires the matching image/config/closure
digests and a fresh explicit client activation; never replay prior media or retained submissions.

- Every FR/SC maps to an automated or live-evidence assertion.
- No required voice frame/action is ignored by any client.
- Exact dependency approval, locks, hashes, licenses, permissions, manifests, and package checks
  are recorded.
- Migration passes on representative prior data twice and has documented recovery.
- Typed/voice authorization and LLM/delegation parity are exact.
- All deterministic/lint/coverage/package gates pass.
- All six clients pass the full staged journey; Windows and physical acoustic evidence are honest.
- Five-user soak, timing thresholds, recap review, listening panel, and zero-retention inspection
  pass against the same immutable candidate.
- Real login steps were performed by the user, not automated or fabricated.
