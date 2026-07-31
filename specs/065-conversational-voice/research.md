# Phase 0 Research: Conversational Voice Interface

**Feature**: 065-conversational-voice
**Completed**: 2026-07-31
**Method**: live-tree inspection plus official upstream release/source/documentation checks.

## Decision 1 — Use LiveKit as the media substrate, not as a second agentic runtime

**Decision**: Run the official self-hosted LiveKit server and a separate first-party LiveKit
Agents worker. The worker performs VAD, turn segmentation, ASR, TTS, playback interruption, and
media cleanup only. It has no AstralDeep LLM resolver, agent registry, tools, user delegation
authority, or durable conversation store.

The final transcript is delivered over reliable ordered LiveKit room data to the originating
client (or over the watch bridge to watchOS). The client submits it once over its already
authenticated AstralDeep WebSocket using the ordinary `chat_message` action and stable voice
session/turn/submission IDs. A domain-separated, short-lived worker HMAC covers the complete
immutable binding and normalized final-text digest for at most two minutes; the client copies it, and ingress rejects any
altered, expired, or transplanted final before stripping proof material. The worker cannot act as a user. Assistant speech commands travel
over a separately authenticated, session/generation-scoped worker-control channel.

Before transcription publication, VAD start initiates a coordinator handshake: the worker echoes
the last worker-applied visible-chat revision, PostgreSQL snapshots that owner-validated destination
and allocates `turn_id`/`submission_id`/`request_generation`, and `turn_bound` returns the immutable
binding. LiveKit reliable data is not a durable queue, so the worker keeps each bounded final only
in memory and republishes it until the coordinator sends fully correlated `transcript_accepted` or
`transcript_rejected`; the client likewise retries the identical ordinary submission until
`user_message_acked` or `voice_submission_rejected`. Database uniqueness makes both replay layers
idempotent. Accepted/rejected dispositions echo submission, request, voice-turn, chat, session,
generation, grant revision, and current UI-connection correlation as applicable. Rejection is
terminal for those IDs; `explicit_user_retry` requires a fresh user action and fresh IDs, preventing
a capacity refusal from becoming a hidden reconnect queue.

Chat navigation is linearized rather than guessed from timestamps: the client pauses new capture,
the server advances one desired context revision, and ordered `session_context_applied` from the
worker re-enables capture. A recognition already started keeps the previously applied chat; a new
VAD cannot begin while desired and applied revisions differ.

Proof-valid voice ingress uses a bound-destination mode inside the ordinary dispatcher. It checks
the existing recognition-time chat and all normal owner/permission/admission gates but never invokes
missing-chat auto-creation, changes `_ws_active_chat`/visible socket scope, forces the user back to an
old chat, or resurrects a deleted chat. A delayed accepted turn publishes to its original chat and
background status without navigation. A deleted/unauthorized old bound chat is rejected before any
message/task; deletion of the currently applied chat ends media, while deletion of an older origin
after navigation rejects only an unaccepted turn and suppresses every later voice announcement for
an already accepted turn.

**Rationale**: This preserves verified Keycloak claims, the in-memory raw token used for RFC 8693
delegation, user/System LLM selection, owner isolation, confirmation, PHI, egress, retry,
cancellation, persistence, and audit at the current `handle_chat_message` seam. It also makes
typed text—not audio or a worker-side conversation—the authoritative record.

**Alternatives rejected**:

- A complete LiveKit voice agent with its own LLM/tools would duplicate and weaken AstralDeep's
  authority and history.
- A worker-to-backend “act as user” endpoint would introduce a broad impersonation capability.
- Sending binary audio over AstralDeep's JSON UI socket would mix media with the authority plane,
  defeat transport bounds, and burden every control connection.
- Forking the LiveKit server has no demonstrated need; supported server, Agents, SDK, data, and
  custom STT/TTS interfaces cover the design.

## Decision 2 — Pin the verified upstream release set and multi-architecture server digest

**Decision**: Use the following upstream versions verified on 2026-07-31 and lock every
transitive/native artifact by the repository's normal mechanism:

| Component | Pin | Evidence / note |
|---|---|---|
| LiveKit server | `v1.13.5@sha256:3497163e15c48fef6e7830c78716f9e9d5edc28abf7aa90b61c86e93bbc306b1` | OCI index contains linux/amd64 and linux/arm64 manifests. |
| LiveKit Agents | `livekit-agents==1.6.7` | Its source pins worker RTC `livekit==1.1.13`. |
| LiveKit API | `livekit-api==1.2.0` | Used by the orchestrator for tokens, rooms, dispatch, data, and disconnect. |
| Silero VAD | `livekit-plugins-silero==1.6.7` | Server-side VAD/endpointing; model artifact is downloaded at image build and hashed. |
| Web | `livekit-client` 2.21.0 | Vendor official UMD locally with license and hash; no CDN/build-system fork. |
| Windows | `livekit==1.1.14` | Independent client SDK pin; Python >=3.9 and Windows wheel exist. |
| Android | `io.livekit:livekit-android:2.27.0` | Project minSdk 26 exceeds SDK minimum; lock transitives and content-filter JitPack to AudioSwitch only. |
| iOS/macOS | LiveKit 2.15.3 | Add only to AstralApp targets; do not add to watch-shared AstralCore. |

**Rationale**: The versions are mutually current and supported at planning time. A digest-pinned
server prevents tag drift, while platform lockfiles and hashes make candidate evidence
reconstructable.

**Approval gate**: The user's requirement establishes the LiveKit-family direction but does not
replace Constitution V. The implementation PR must record lead approval of exact pins,
transitives, native binaries, Apache-2.0/third-party notices, CVE review, image/package-size
impact, and the Silero model artifact. No installation occurs during planning.

**Alternatives rejected**: floating `latest`, CDN delivery, unpinned Maven/SPM/PyPI dependencies,
and source forks cannot produce candidate-bound evidence.

## Decision 3 — Repair one durable dispatcher and remove whole-task chat serialization

**Decision**: Generalize the current async chat runner before voice uses it. The dispatcher will:

1. accept verified user, chat, connection, request-generation, submission, and idempotency IDs;
2. copy verified UI-session claims and the raw user token into a temporary in-memory virtual
   session;
3. set `llm_context_user_id` explicitly;
4. call the existing `handle_chat_message` path;
5. bind and scrub temporary session/token state in `finally`;
6. never persist or log the raw token; and
7. acquire a no-queue `voice_interactive` execution lease before normal acceptance, so an accepted
   second voice turn actually begins agent execution while the first continues.

The current `_workspace_locks[chat_id]` spans the whole task and therefore cannot remain around
agent/tool execution. Replace it with two short critical sections:

1. **admit/snapshot**: owner/idempotency validation and execution lease; atomically publish a
   `user_acceptance` commit under the client request generation without terminalizing the operation,
   carrying forward the complete authoritative component and layout rows under the new normal
   revision while adding only the new user message; allocate/store a separate server result request generation, and create its linked private
   `assistant_result` stage with an immutable retained base commit/revision anchor plus canonical
   component/layout digests; after the transaction broadcast the user message/correlated
   acknowledgement and release the chat lock;
2. **terminal publication**: atomically rebase the private turn stage over the current committed
   revision and publish the result; then release the lock.

The LLM/agent/tool loop runs outside the chat lock against its immutable task-local stage. A newer
accepted turn becomes foreground and the previous turn becomes background without cancellation.
If execution capacity is unavailable, admission returns a retryable refusal before creating a
normal message/task and must not say “On it!”; it is not accepted into a hidden queue.

Terminal rebase is deterministic and never reruns a tool or consequential side effect. It compares
execution base, task-local candidate, and latest committed views for both components (stable
`component_id`) and layouts (`layout_key` plus canonical digest). Non-conflicting additions/updates/
deletes apply over latest. If another turn changed the same key, latest wins and this turn's ordinary
assistant message includes its result plus a safe conflict notice. Candidate layout references are
validated against merged components; invalid candidate layout drops/flattens without replacing a
valid latest layout. The transaction rewrites only its own stage, advances its revision pointers and
the chat anchor, and never deletes another stage. Aborts clean only their own rows; retained committed
versions are pruned only by targeted snapshot-aware cleanup. External side effects and audit records
are not repeated or concealed. Conversation messages retain accepted/terminal timestamps, while
render revisions remain atomic and monotonic in publication order.
If two turns finish in reverse order, their user bubbles remain in acceptance order while their
attributed assistant results appear in truthful terminal-publication order; candidate assistant/
canvas data is never exposed before its atomic terminal commit.

**Rationale**: Live-tree inspection found both that `_dispatch_async_chat` creates a virtual socket
without binding the original claims or setting the LLM context—which can make a user task look like
a System job and lose the delegation token—and that `_serialized_chat`/`_dispatch_async_chat` hold
the per-chat lock around all of `handle_chat_message`. Fixing both shared seams avoids encoding those
defects in voice and improves ordinary background turns too.

**Alternatives rejected**: a voice-specific dispatcher would perpetuate two authorization paths;
calling “queued” an immediate start contradicts the clarified behavior; optimistic commit with
whole-task rerun could duplicate tools; and last-writer-wins canvas replacement could erase a
concurrent result.

## Decision 4 — Use supported LiveKit interfaces with AstralDeep-bounded Speaches adapters

**Decision**: Keep LiveKit `AgentSession` and Silero VAD, but implement narrow
`SpeachesRealtimeSTT` and `SpeachesTTS` adapters against LiveKit's supported STT/TTS interfaces.
The adapters fix these launch values in code/config:

- ASR: `Systran/faster-whisper-large-v3`
- TTS: `speaches-ai/Kokoro-82M-v1.0-ONNX`
- voice: `af_heart`
- synthesis: WAV validated as 24 kHz, mono, bounded duration/bytes

Realtime ASR uses Speaches' transcription intent and emits provisional/final LiveKit speech
events. TTS accepts only coordinator-issued bounded text and streams decoded frames to the room.
The worker suppresses ASR ingestion while its own output is active, combines that with platform
AEC and VAD, and reopens capture immediately for barge-in after stopping stale speech.

**Rationale**: The pinned generic OpenAI plugin supports custom endpoints and realtime STT, but
its realtime implementation obtains a shared `aiohttp` session rather than exposing the complete
destination-controlled streaming connector required by AstralDeep's egress policy. A small
supported adapter provides exact redirect, DNS, peer, buffer, timeout, proxy, and redaction
semantics without modifying upstream LiveKit.

**Streaming egress contract**: `backend/shared/streaming_egress.py` validates one fixed
operator-configured origin, resolves and pins allowed addresses for the connection pool, preserves
the original hostname for Host/SNI and certificate checks, rejects redirects and environment
proxies, requires TLS outside explicit local development, bounds connect/handshake/read/write/
session time, limits WebSocket frames and HTTP bodies, and emits typed redacted errors. A private
speech endpoint is allowed only as the exact configured operator destination; user/model-supplied
URLs never enter this path.

**Alternatives rejected**:

- The legacy raw `aiohttp` proxy is unauthenticated/unbounded and exposes caller-selected values.
- Buffered batch STT is a viable failure fallback for testing but does not provide the intended
  provisional realtime experience and is not the launch path.
- Browser/platform ASR or TTS would violate the exact-model and included-service requirements.

## Decision 5 — Isolate deployment speech inputs from all LLM configuration

**Decision**: At the Compose service boundary only, map:

```text
OPENAI_BASE_URL -> VOICE_SPEECH_BASE_URL
OPENAI_API_KEY  -> VOICE_SPEECH_API_KEY
```

Only the isolated voice worker receives and reads `VOICE_SPEECH_*`. Compose explicitly
overrides/unsets the legacy names inside the orchestrator container while interpolating them into
the worker aliases. The worker passes those values
explicitly to the bounded adapters and disables ambient proxy/provider credential discovery. The
orchestrator, user agents, System jobs, and clients do not receive them and continue to resolve
LLMs only from encrypted in-product configuration. LiveKit API/secret and the worker-control
challenge key are separate operator-only deployment secrets. LiveKit job metadata carries only
opaque session/generation correlation; it never carries the worker-control credential or a client
grant.

**Rationale**: This honors the requested existing `.env` inputs while preserving feature 054's
behavioral guarantee. The existing environment-inert LLM test remains unchanged; new sentinel
integration tests prove speech credentials reach only the speech service and cannot become user
or System LLM fallback.

**Alternative rejected**: allowing the OpenAI SDK/plugin to discover `OPENAI_*` implicitly inside
the main process creates exactly the cross-purpose fallback the repository forbids.

## Decision 6 — Persist ownership and deduplication; keep content/audio ephemeral

**Decision**: Add `voice_session` and `voice_turn` in PostgreSQL. A partial unique index enforces
one non-ended session per user. Row locks/compare-and-swap generation fencing govern start,
takeover, refresh, end, and expiry. `voice_turn` binds a stable client turn to one user, session
generation, immutable chat snapshot, ordinary submission, durable task, and committed result.
Minimal announcement timing/phrase-key metadata supports failover and cadence without storing
speech content. An unended media session always has a non-null authorized visible chat; deleting or
revoking that current chat ends media instead of introducing an unsynchronizable null worker bind.
The stored chat UUIDs are retained, indexed correlation/tombstone strings rather than `chats`
foreign keys or proof of authority. One voice-aware deletion/revocation transaction fences current
media, rejects unaccepted finals, suppresses accepted-turn speech, clears stage-scoped base anchors,
terminalizes accepted voice correlations as destination-abandoned without cancelling their
underlying operations, aborts only affected private result stages, and permits ordinary hard deletion
while keeping the bounded owner/session/turn/submission/chat tuple needed to reject every later replay
without a second dispatch.

The existing publication store gains an immutable execution-base commit/revision self-anchor,
component/layout digests, and a rebase counter on `conversation_commit`, together with commit/revision metadata and
commit-scoped uniqueness on `workspace_layout`. These are coordination/versioning fields, not a new
content store: base/candidate/latest content remains in the normal versioned component/layout rows,
and the self-anchor prevents base cleanup only while a stage references it. Commit/abort clears that
stage-scoped FK atomically while retaining non-content revision/digests, avoiding a permanent
ancestor chain. Current reads follow the chat's commit/revision anchor, and private stages remain
isolated until their own atomic terminal publication.

Raw/generated audio, partial transcripts, final transcript copies/digests/proofs, recap text,
bearer tokens, tickets, and credentials are not stored. The final transcript exists only as the ordinary user message;
the result and `summary_text` exist only in the normal committed conversation/task record.
The existing hash-chained audit records bounded non-content events for session start/end, grant
issuance, takeover, transcript acceptance correlation, interruption/degradation, consent, and
terminal outcome. Voice tables are coordination state, not a substitute audit log.

Reconnect grant refresh is a UUID4-idempotent CAS. The first request fixes the next revision,
participant identity, issued/expiry claims, and deterministic watch-ticket nonce; ordered
`media_grant_rotated`/`media_grant_applied` installs it in the worker before release. A lost response
can be retried with the same ID during the bounded no-store recovery window without advancing again
or persisting a bearer. A different refresh ID with stale expectations receives only credential-
free current revision/state. The prior short-lived LiveKit JWT may still join until expiry, but its
identity/revision cannot start recognition.

**Rationale**: Process-local dictionaries cannot enforce cross-client ownership or deduplicate
reconnect/takeover across replicas and restarts. A separate `voice_announcement` content table is
unnecessary and would create retention risk.

## Decision 7 — Schedule truthful speech from committed task state and playout events

**Decision**: The VoiceCoordinator starts acknowledgement only after durable acceptance plus an
active execution lease. It
chooses from a fixed allowlisted phrase map (including “On it!”), never repeats the same phrase key
back-to-back, and uses only sanitized committed lifecycle categories: accepted/active,
waiting on user, success, failure, refusal, or cancellation.

Before each utterance the expected worker publishes a content-free announcement manifest that
binds a distinct ephemeral LiveKit audio track (or watch PCM sequence range) to session, generation,
grant revision, announcement, turn, kind, sequence, quantum role/index, and actual sample count.
Unknown, over-budget, role/index-mismatched, or audio-before-manifest media is
not rendered. The worker reports source publication/playout started/finished/interrupted events.
Every client also emits a dedicated top-level content-free `voice_playout_event` frame—not a
`ui_event` action—at matched local render start/finish/interruption through its authenticated UI
socket. The coordinator validates control binding, owner/device/UI connection/session/generation/grant revision,
announcement, and sequence, then stamps server receipt time; the client wall clock is diagnostic
only. It schedules from the later valid source/client finish observation and prepares the next
neutral or state-backed phrase at a 14-second target. If a client
playout event fails to arrive within the bounded transport window, the session becomes reconnecting
or unavailable instead of claiming the audio was heard. Every command carries session/turn/
generation/sequence fences. Terminal,
waiting, mute, interruption, takeover, disconnect, or end cancels/suspends stale queued speech.
Concurrent/background completions enter one bounded serialized scheduler and never masquerade as
the foreground turn. At most two active turns share one output stream. Recap openings are meaningful,
attributed, pre-synthesized when coincident, and at most 1.5 seconds before yielding; other speech
quanta are at most four seconds. Before starting a quantum, deadline arbitration reserves another
equally due turn's four-second maximum plus a measured 250 ms stream handoff. With equal progress
deadlines, the first starts by 15.5 seconds and the second by 19.75 seconds; a path unable to meet the
handoff budget fails voice honestly. Arbitration cancels stale progress for a terminal turn and
alternates simultaneous terminal openings so the second targets 1.75 seconds. Longer recaps resume
after other due openings using sensitivity-safe earlier/latest ordinal attribution. Barge-in cancels
unfinished prior recap chunks. Deterministic overlapping-turn tests inject the positive 250 ms
handoff and prove the bound; staged acoustic evidence measures real p95 behavior and handoff latency.
The coordinator labels every command `single`, `result_opening`, or `result_continuation` with a
bounded index and exact 24 kHz sample ceiling. The worker measures synthesis before publication and
rejects anything above 96,000 samples, or above 36,000 for an opening; the content-free manifest
echoes the role/index and actual sample count so every client rejects an over-budget track. The
720,000-sample/30-second result limit is a separate per-result aggregate counter across chunks.
Before sending a result command, a row-locked coordinator CAS conservatively reserves that command's
sample ceiling and quantum index; interruption/failure never refunds it. The worker and clients echo
the resulting cumulative reservation, so restart/retry cannot reset or exceed the aggregate budget.

Authenticated client lifecycle is a bounded operational observation, not acoustic proof. Candidate
test instrumentation/capture separately measures first/last locally rendered audio for the audible
SLO, and metrics distinguish source, client-receipt, and acoustic observations. The early target and
explicit reservation absorb bounded synthesis/transport/handoff latency; a stalled path transitions
honestly.

**Rationale**: Scheduling from timer creation or text generation rather than source playout can
violate the audible-gap requirement. Allowlisted state language is natural enough to reassure
without fabricating tool or reasoning progress.

**Alternative rejected**: asking an LLM to invent periodic status risks false claims, PHI/tool
leakage, latency, and context pollution. Announcements are not chat messages.

## Decision 8 — Prefer an authoritative committed summary; derive the fallback deterministically

**Decision**: Extend the normal atomic completion contract with optional `summary_text` and
`summary_source`. If the completed request already produces an authoritative text summary, commit
and use it. Do not treat the existing 200-character notification scrape as authoritative merely
because it is named `summary`.

When `summary_text` is absent, a deterministic `CommittedVisibleTextExtractor` traverses only the
same sanitized server-owned semantic payload committed and rendered for the current result on every
client; it does not scrape a client DOM or infer hidden UI. It excludes
hidden fields, raw HTML, tools, traces, progress, scripts/styles, credentials, and uncommitted
canvas state; preserves terminal status, headings, material conclusion/caveat/next-action text;
normalizes whitespace; and caps the default recap at 80 words/30 seconds. It records
`committed_visible_fallback` as the source. It does not call a second voice LLM.

Before synthesis, the full candidate passes existing confidentiality signals and the fail-closed
`PHIGate.contains_phi()` check; error/unknown means sensitive. Automatic output is then only a
generic notice. A fresh tap, or a strictly normalized allowlisted spoken phrase while one exact
result is pending, creates a one-result consent and allows that bounded recap once. An LLM never
infers consent.

Every final spoken phrase still enters the authenticated normal text-acceptance seam. Before LLM
dispatch, a deterministic, context-bound control resolver may recognize only an exact allowlist
when the referenced state exists—for example read the one pending sensitive result, stop/mute
speech, or invoke the existing cancellation action for the foreground task. The visible transcript
remains the user's ordinary text. The resolver grants no new scope, never lets the worker/client
decide intent, and sends every non-exact/ambiguous phrase through ordinary agentic dispatch.

Launch spoken output is English (United States), matching `af_heart`. If ASR successfully returns
a different language, the ordinary transcript/result is preserved, but the client communicates
the output-language limitation and the worker speaks only a safe English lifecycle/result-ready
notice. It never swaps voice/model or claims unsupported localized pronunciation.

**Rationale**: The user's clarification explicitly selects the completed text summary first and
screen content second. Keeping fallback deterministic makes it reproducible, prevents provider
credentials from becoming an LLM fallback, and constrains the source to what the user can verify.

## Decision 9 — One server-owned composer/control model, native media adapters

**Decision**: Add a server-owned composer model patterned after existing chrome models. It emits
ordered semantic controls and the canonical voice state. Extend `ui_protocol.json` and validators
with required capabilities, frames, actions, IDs, reason codes, and transport discriminator; every
client marks them handled. Runtime microphone authorization is distinct from static hardware and
audio-output capability.

Session mutations map one-to-one to the authenticated OpenAPI operation IDs (create, takeover,
patch, end, stop speech, or result-bound read consent); there is no generic WebSocket
`voice_action`. Each client persists a non-secret UUID4 installation ID and supplies it as strict
top-level `register_ui.device_id` with a freshly client-generated UI `connection_generation`; the server returns a short-lived,
memory-only `voice_control_binding` valid for at most ten minutes and never beyond the Keycloak
token/socket. Every mutating REST call requires the normal user bearer plus
matching device, connection, and binding headers, so a stale connection or another same-user device
cannot control the session without takeover. Media capability is advertised on `register_ui`. The
only new client-to-server voice socket frame is the dedicated content-free
`voice_playout_event`; it bypasses UI-action dispatch and creates no operation. A UUID4 minted on the explicit start/takeover tap is
idempotency correlation, not proof of authorization or permission; each REST handler independently
checks Keycloak owner, runtime capability/state, generation, and input. The fixed profile exposes
`output_locale: en-US` and a typed `output_language_unsupported` posture on every client.

An explicit start with no selected chat first invokes an additive strict form of the existing
authenticated `new_chat` action with schema version, current connection generation, and stable
submission/request UUID4s. Existing owner-scoped operation idempotency records the normalized input
digest; exact retry returns the same `chat_created` UUID4/correlation, while changed reuse fails. The
client starts voice only if the response still matches its pending activation/current connection and
the user has not navigated, so delayed responses cannot switch chats. No voice session grant,
microphone capture, greeting, or turn begins until that owner-validated chat is hydrated. Background/lock/audio interruption stops
local media synchronously and sets automatic `foreground_active: false`; resume rebinds the UI
connection, refreshes the grant idempotently, and waits for applied chat context. Logout/auth expiry
or deletion/revocation of the current chat ends media. Accepted execution/audit follows ordinary
deletion policy, but result publication to that unavailable chat and later voice output are
suppressed rather than crossing or resurrecting the destination.

Web vendors the exact LiveKit UMD bundle locally. Windows uses LiveKit's Python RTC plus
QtMultimedia (already available through PySide6). Android uses the exact Maven SDK and
`RECORD_AUDIO` only. iOS/macOS add the Swift package to AstralApp targets and update microphone,
sandbox, audio-session, lifecycle, and privacy declarations. All capture/playback stops on
background/lock/logout/auth expiry/takeover; submitted tasks continue.

**Rationale**: A new persistent `Audio` primitive would confuse one-shot UI media with a
foreground conversation transport. Six hard-coded local button/state definitions would violate
server-driven parity.

## Decision 10 — Bridge watchOS because the official Swift SDK has no watchOS platform

**Decision**: Implement a bounded foreground-only binary WebSocket PCM adapter in the isolated
voice-worker service and a native `AVAudioEngine`/`AVAudioConverter` watch client. A short-lived,
single-session ticket binds user/session/generation/grant revision/expected worker identity and is consumed once. The relay joins the same
LiveKit room as a trusted participant, forwards normalized mic PCM into the same VAD/ASR pipeline,
and returns the exact 24 kHz Kokoro output plus transcript/announcement/control envelopes. Frames are sequenced,
bounded, rate-checked, time-limited, and never retained.

**Rationale**: LiveKit Swift 2.15.3 declares iOS, macOS, Mac Catalyst, and tvOS only; its WebRTC
XCFramework has no watchOS slice. `AstralCore` also targets watchOS, so adding LiveKit there would
break the shared package. The explicit bridge retains exact server models and LiveKit's server-side
session while acknowledging the unsupported last mile.

**Alternatives rejected**:

- `AVSpeechSynthesizer`, watch dictation, or platform speech violates the exact model/voice rule.
- A mandatory iPhone companion relay fails independent-watch operation and creates unreliable
  ownership/lifecycle coupling.
- Claiming direct SDK support or hiding watchOS as unsupported violates all-client parity.

## Decision 11 — Launch with a pinned single-node LiveKit topology

**Decision**: Local, staging, and initial production topologies use one pinned LiveKit server with
one or more independently restartable voice workers. PostgreSQL remains the authoritative session
and task store. LiveKit carries ephemeral room/media state only. Production terminates trusted TLS,
serves WSS, exposes reachable ICE UDP/TCP and TURN according to the deployment network, uses
short-lived room grants, and never advertises a hard-coded loopback address. Local Docker generates
a host-reachable configuration for browser/emulators/simulators rather than assuming container
`localhost` works everywhere.

No Redis service is added for launch: five-user scope does not require a multi-node LiveKit
cluster, and adding Redis solely for hypothetical HA would add an unrelated operational
dependency. Horizontal LiveKit HA later requires a separately approved Redis-backed topology.

**Failure posture**: `FF_CONVERSATIONAL_VOICE` is an operational kill switch and may default on,
but capability is ready only after bounded LiveKit/worker and exact model/voice probes. Missing or
failed media/speech configuration makes voice unavailable; orchestrator health and typed chat stay
up. Worker configuration failures terminate the worker rather than starting a fake path.

## Decision 12 — Require qualifying staging before merge and keep local proof scoped honestly

**Decision**: Deterministic tests use fake clocks/media plus a strict local fake
OpenAI-compatible service, then an exact digest-pinned LiveKit integration lane. Live macOS work
uses Docker, a real browser, PySide, the Android emulator/device, and installed Apple simulators/
devices, pausing at real Keycloak login. Release evidence additionally requires:

- the immutable backend and voice-worker image digests plus LiveKit digest/config hash;
- real Keycloak, PostgreSQL with representative pre-existing data, exact speech readiness, WSS,
  ICE/TURN, and agent/tool work lasting beyond 20 seconds;
- web fake-media + real-browser audio; Windows built-once EXE and native mic/speaker; Android
  connected + physical route/echo; iOS/macOS/watch simulator and physical acoustic evidence;
- controlled 2/19/21/65-second and multi-minute tasks;
- five simultaneous users for 30 minutes;
- storage/log/trace inspection proving zero audio/credential retention;
- a 100-result recap review and `af_heart` listening panel.

The repository's release-readiness workflow currently declares qualifying external staging
inactive. Because this feature changes runtime infrastructure, Constitution X makes a trusted
same-candidate staging run a non-waivable pre-merge gate. Local Compose cannot waive or substitute
for it. Bounded platform runner/device exceptions use only the protected seven-day ledger/approval/
resolution mechanism already established by feature 060; they do not waive staging, trust,
security, schema, speech readiness, PHI, or isolation gates.

## Official Sources Consulted

All version-sensitive sources were retrieved on 2026-07-31:

- [LiveKit server v1.13.5 release](https://github.com/livekit/livekit/releases/tag/v1.13.5)
- [LiveKit Agents 1.6.7 release](https://github.com/livekit/agents/releases/tag/livekit-agents%401.6.7)
- [LiveKit Agents 1.6.7 dependency source](https://github.com/livekit/agents/blob/livekit-agents%401.6.7/livekit-agents/pyproject.toml)
- [LiveKit web client v2.21.0](https://github.com/livekit/client-sdk-js/releases/tag/v2.21.0)
- [LiveKit Android client v2.27.0](https://github.com/livekit/client-sdk-android/releases/tag/v2.27.0)
- [LiveKit Swift client 2.15.3](https://github.com/livekit/client-sdk-swift/releases/tag/2.15.3)
- [LiveKit Swift 2.15.3 platform declaration](https://github.com/livekit/client-sdk-swift/blob/2.15.3/Package.swift)
- [LiveKit Python RTC 1.1.14 package](https://pypi.org/project/livekit/1.1.14/)
- [LiveKit API 1.2.0 release](https://github.com/livekit/python-sdks/releases/tag/api-v1.2.0)
- [LiveKit Silero plugin 1.6.7 package](https://pypi.org/project/livekit-plugins-silero/1.6.7/)
- [LiveKit OpenAI-compatible STT guide](https://docs.livekit.io/agents/models/stt/openai/)
- [LiveKit Kokoro TTS guide](https://docs.livekit.io/agents/models/tts/kokoro/)
- [LiveKit pipeline-node extension guide](https://docs.livekit.io/agents/logic/nodes/)
- [LiveKit self-hosting deployment](https://docs.livekit.io/home/self-hosting/deployment/)
- [LiveKit ports and firewall](https://docs.livekit.io/home/self-hosting/ports-firewall/)
- [LiveKit token authentication](https://docs.livekit.io/home/get-started/authentication/)
- [LiveKit reliable data packet limits and non-buffering behavior](https://docs.livekit.io/transport/data/packets/)
- [Speaches realtime transcription API](https://speaches.ai/usage/realtime-api/)

## Resolved Unknowns

No unresolved clarification marker remains. Runtime compatibility is still an implementation acceptance
test rather than an assumption: the exact configured endpoint must pass model inventory, realtime
ASR, Kokoro `af_heart`, WAV/24 kHz, authentication, timeout, redirect, buffer, and redaction tests
before capability reports ready.
