# Media Plane Contract

**Feature**: 065-conversational-voice
**Authority**: Media transports carry ephemeral audio and recognition envelopes only. They never
authorize or execute an AstralDeep query.

## 1. Topology

```text
web / Windows / Android / iOS / macOS
  microphone publish + assistant audio/data subscribe
          │
          ▼
  self-hosted LiveKit room  ◄──── direct-RTC voice worker participant
                                   │ exact Silero ONNX VAD + exact ASR/TTS
                                   └ scoped control WSS to VoiceCoordinator

watchOS foreground PCM WSS ───── watch relay in voice worker
                                   └ participates in the same LiveKit session
```

The launch deployment is one digest-pinned LiveKit server with independently restartable voice
workers. The room is ephemeral and opaque. PostgreSQL, not LiveKit, owns session/turn/task state.

## 2. Participant and Grant Rules

The authenticated REST session API mints all room names, identities, and grants. A client cannot
supply or override them.

Client LiveKit grants are short-lived and permit only:

- join the one assigned room as the assigned identity;
- publish one microphone audio source while foreground/authorized;
- subscribe to the worker's assistant audio and reliable data;
- no room administration, participant update/removal, arbitrary data publication, video/screen
  publication, SIP, recording/egress, or API-secret disclosure.

The server-designated worker is the only participant whose transcript data the client accepts.
Unexpected participants, identities, topic names, schema versions, or generation values are
ignored and reported without content. Every grant includes the authoritative session `generation`
and `media_grant_revision`. A reconnect refresh leaves session generation unchanged, atomically
increments the grant revision, rotates the client participant identity/nonce, updates the worker's
accepted microphone publisher through ordered `media_grant_rotated`/`media_grant_applied` control
frames, and best-effort removes the old participant. Refresh is UUID4-idempotent: retrying the same
`refresh_id` after a lost response returns the same still-current no-store grant within its short
recovery window, while a different ID with a stale expected revision receives the current
non-secret revision and must retry from that state. A minted LiveKit JWT
cannot be cryptographically revoked before its short expiry; an old identity may still signal or
join, but its media can neither start a recognition binding nor authorize a query. Takeover instead
advances session generation and ends the prior media session. Database generation/revision checks
remain decisive even if participant removal is delayed.

Grant/token/ticket values use `Cache-Control: no-store`, are never persisted by the client beyond
the active foreground session, and are excluded from logs, analytics, screenshots, crash reports,
audit metadata, and generic protocol-frame capture.

## 3. LiveKit Audio

- Client microphone capture follows the platform SDK's WebRTC format and AEC/noise controls. The
  direct-RTC worker connects with auto-subscribe disabled, validates the assigned participant/audio/
  microphone source, then subscribes through a finite-capacity 16-kHz mono `AudioStream` using
  32-ms frames. It feeds exact Silero ONNX Runtime VAD and forwards only bounded utterance audio to
  exact ASR. Queue overrun aborts the utterance; it never silently accepts the SDK's drop-oldest
  behavior as a complete recording.
- Worker TTS requests fix model `speaches-ai/Kokoro-82M-v1.0-ONNX`, voice `af_heart`, and WAV.
  Responses must parse as mono 24 kHz audio before publication; mismatch fails the utterance and
  degrades voice rather than silently resampling an unknown model/voice response.
- Raw mic and generated audio live only in bounded active ring/decoder buffers. There is no room
  recording, LiveKit egress recording, file upload, disk spill, crash attachment, or content
  telemetry.
- Room callbacks only enqueue typed events into one serialized session owner. Initial participants/
  publications are reconciled after `Room.connect()` returns; the nonexistent initial `connected`
  callback is not awaited. Reconnect fences capture/playout until context/grant/generation and
  publications are revalidated, handles changed local track SIDs, and creates a fresh Room plus
  higher-revision worker grant after terminal disconnect.
- Assistant output is serialized per user/session. Starting capture during barge-in first stops and
  fences current assistant frames. The worker advances a speech epoch, clears its small
  `AudioSource` queue, boundedly quiesces the producer, clears again, and replaces the track on
  timeout; local queue completion is not client playout proof. ASR ingestion is gated while
  assistant output is active, then promptly resumes so playback cannot become a user turn.
- Foreground loss, OS interruption, logout, auth expiry, takeover, and session end follow the exact
  cleanup table below. Accepted AstralDeep tasks are unaffected.

### Foreground and interruption lifecycle

Local capture/playout cessation is synchronous and does not wait for a network response. The
automatic `foreground_active` state is distinct from the user's persistent `speech_muted` choice.

| Event | Immediate client action | Authoritative server/worker action | Resume rule |
|---|---|---|---|
| App hidden/inactive or device locked | Stop mic publication and assistant playout, discard queued audio, then best-effort PATCH `foreground_active: false`, bounded foreground reason, and `microphone_enabled: false`. | Enter `suspended`, issue `set_capture: false`/`stop_speech`, retain accepted tasks and the short reconnect lease. | Foreground + capability check + fresh control binding if the UI socket changed + idempotent grant refresh; no old audio is replayed. Mic resumes only after applied chat context is synced. |
| OS audio interruption or route loss | Stop capture/playout synchronously; PATCH foreground false with reason `audio_interrupted`. | Suspend capture/speech; continue accepted tasks and publish text results normally. | Restore route, recheck permission/output, refresh grant, then PATCH foreground true with reason `foreground`. |
| Brief network loss or process crash | Close/drop the local track; never persist a grant, transcript proof, or audio. | Enter `reconnecting`; lease expiry ends media and bounded in-memory finals expire visibly. | Re-register UI, obtain a new control binding, read non-secret current state, and refresh with stable `refresh_id`; no stale publisher/audio is accepted. |
| Logout or auth expiry | Stop media synchronously; best-effort DELETE. | End/revoke the session, fence generation, abandon any recognizing/submitting turn as a retryable stale session, and clear worker buffers; ordinary accepted work retains normal auth/audit behavior. | No automatic resume; a new authenticated explicit start is required. |
| Explicit end | Stop media synchronously; DELETE with bound device/connection headers. | End media and speech, and abandon any recognizing/submitting turn without touching accepted work. | Accepted tasks/results continue as ordinary text; later start creates a new session. |
| Takeover | Old client stops on the fenced state frame; new client never reuses old media. | Advance generation, abandon unaccepted old-generation turns, end old worker media, and mint a new owner-bound grant. | New owner completes capability/context synchronization before capture. |
| Current visible chat deleted or no longer authorized | Stop capture/playout and tear down local media immediately; best-effort end with `chat_deleted` or `chat_unauthorized`. | End/fence media and reject unaccepted turns. Accepted execution/audit follows ordinary deletion policy, but result publication to that chat and every later voice announcement stay suppressed. | No automatic resume. Select/create an authorized chat and explicitly start a new voice session. |
| Older turn's bound chat deleted or revoked after navigation | Keep the current session/chat active; clear a rejected unaccepted final and cancel queued/playing announcements for any already accepted turn from that chat. | Reject an unaccepted final without creating a chat/message/task. Already accepted work follows ordinary deleted/unauthorized-chat policy, but all later voice progress/recap for it is suppressed. | Current-chat voice continues. An unaccepted old request requires a fresh explicit retry in an authorized chat; no deleted-chat result is spoken. |

## 4. Transcript Data Topic

Topic: `astraldeep.voice.transcript.v1`
Reliability: reliable, ordered
Publisher: expected voice-worker identity only
Maximum JSON UTF-8 envelope: 12 KiB (below LiveKit's reliable-packet ceiling); `text` maximum:
8,000 Unicode scalar values and still subject to the envelope-byte cap

Payload validates against the `voice_transcript` branch of
[voice-control.schema.json](voice-control.schema.json):

- `session_id`, session `generation`, recognition-time `media_grant_revision`, `turn_id`,
  `client_turn_id`, `submission_id`, `request_generation`, immutable `chat_id`, and
  `chat_context_revision` bind the event;
- `sequence` strictly increases within the recognition turn;
- partial events have `final: false`, are provisional UI only, and are never submitted;
- one non-empty `final: true` event includes a detected language plus a short-lived transcript proof
  and is queued locally for normal authenticated `chat_message`;
- empty, invalid, stale, unexpected-worker, cancelled, or duplicate finals create no message;
- the client retries an unacknowledged final after UI-WebSocket reconnect with the same complete
  binding and text; the strict `user_message_acked` frame ends retry only when `submission_id`,
  `request_generation`, `voice_turn_id`, `chat_id`, and the current `connection_generation` all
  match;
- the transcript envelope itself grants no action/approval scope.

Clients do not trust LiveKit data as a conversation commit. The final visible user bubble arrives
through the normal AstralDeep acknowledgement/snapshot path after server acceptance.

### Recognition binding and ordinary submission

1. Before a visible-chat PATCH, the client pauses new-turn capture. The server permits at most one
   pending context update, advances the desired `chat_context_revision`, and sends
   `session_context_update`. The worker applies it between recognition starts and returns ordered
   `session_context_applied`; only then does the server publish `chat_context_synced: true` and the
   client resume capture. A turn already started before navigation keeps its old binding.
2. At VAD start the worker mints `client_turn_id` UUID4 and sends `recognition_started`, echoing the
   current session/grant generation and last applied visible chat/revision. New VAD is disabled while
   desired/applied context differs, and an old client identity cannot initiate this exchange after a
   grant rotation.
3. In one PostgreSQL transaction the coordinator verifies the echoed applied revision, owner-
   validates and snapshots that chat, inserts
   the recognition row, and mints `turn_id`, `submission_id`, and `request_generation` UUID4 values.
   `turn_bound` returns the immutable binding before the worker publishes any transcript.
   If ASR fails or returns an empty/invalid final, the worker instead sends the strictly bounded
   `recognition_failed` frame with only that exact `client_turn_id` and an allowlisted safe reason.
   The authenticated coordinator resolves the existing turn binding and atomically abandons the
   still-`recognizing` row as `malformed_final`/`explicit_user_retry`; replayed, unbound, stale-
   generation, and cross-assignment failures have no side effect.
4. The worker canonicalizes the final (`CRLF` to `LF`, Unicode NFC, outer whitespace removed;
   NUL/other controls except tab/newline rejected), computes lowercase SHA-256 over its UTF-8 bytes,
   and attaches an HMAC proof expiring no more than two minutes after the final. The proof input is the fixed newline-delimited ASCII
   sequence `ADVT1`, session/generation/grant revision, every immutable turn/submission/chat binding,
   expected worker identity, detected language, text digest, and canonical UTC proof expiry. The
   per-session proof key is domain-separated from `VOICE_CONTROL_SECRET`. UUIDs are canonical
   lowercase UUID4s. Worker and coordinator share golden vectors; neither proof nor digest is
   stored or logged.
5. The client copies the final and proof verbatim into the existing frame—not a voice dispatch
   endpoint:

   ```json
   {
     "type": "ui_event",
     "action": "chat_message",
     "session_id": "<bound-chat-id>",
     "connection_generation": "<current-ui-connection-uuid4>",
     "submission_id": "<bound-submission-uuid4>",
     "request_generation": "<bound-request-uuid4>",
     "payload": {
       "message": "<final transcript>",
       "chat_id": "<bound-chat-id>",
       "connection_generation": "<current-ui-connection-uuid4>",
       "submission_id": "<bound-submission-uuid4>",
       "request_generation": "<bound-request-uuid4>",
       "snapshot_purpose": "commit",
       "voice_origin": {
         "schema_version": "1",
         "session_id": "<uuid>",
         "generation": 1,
         "media_grant_revision": 1,
         "turn_id": "<uuid>",
         "client_turn_id": "<uuid4>",
         "chat_context_revision": 1,
         "source_participant_identity": "<expected-worker>",
         "detected_language": "en",
         "text_digest_sha256": "<64-lowercase-hex>",
         "transcript_proof": "<64-lowercase-hex>",
         "proof_expires_at": "<canonical-utc-timestamp>"
       }
     }
   }
   ```

   The ingress requires frame/payload UUID equality, current authenticated connection ownership,
   exact agreement with the server-created binding, proof freshness, a constant-time HMAC check,
   and equality between the normalized submitted text and its digest. A verified voice-origin turn
   enters the same authenticated authorization/admission/dispatch code as typed chat in an explicit
   **bound-destination mode**: it owner-checks the already-existing bound chat, never invokes the
   missing-chat auto-create fallback, never changes `_ws_active_chat` or any visible socket scope,
   never forces hydration/navigation to the older chat, and publishes there only to that chat's
   subscribers plus ordinary background-status surfaces. Navigation after step 2 changes only
   future turns. If the bound chat was deleted or became unauthorized, ingress emits the correlated
   terminal rejection below and creates no chat, message, operation, or task. Absence of
   `voice_origin` preserves ordinary typed-chat behavior unchanged. Proof material is stripped
   before persistence.
6. Atomic normal acceptance first holds a running no-queue execution lease, commits the ordinary
   user message and voice/task correlation, and publishes the normal user bubble plus strict
   `user_message_acked` frame. Only then does agent execution continue in a task-local assistant/
   canvas stage. The worker receives `transcript_accepted.accepted_message_id`; that acceptance may
   trigger exactly one acknowledgement speech. Any pre-acceptance refusal returns fully correlated
   `transcript_rejected` to the worker and `voice_submission_rejected` to the current client, with
   `retry_policy: explicit_user_retry` or `none`. Capacity refusal occurs before message commit and
   produces neither bubble nor acknowledgement. Terminal assistant/canvas publication is a later
   atomic commit. If concurrent turns finish in reverse order, user bubbles retain acceptance order
   while attributed assistant results appear honestly in commit/completion order.

### Application replay bound

LiveKit reliable data is not a durable queue and is not buffered for a disconnected participant.
The worker therefore retains each final transcript only in memory until `transcript_accepted`,
`transcript_rejected`, session/generation end, or a two-minute deadline. It republishes the exact
same bounded envelope on the current participant's subscribe/reconnect event. Per-session retention
is capped at four finals and 48 KiB total; overflow fails the newest unaccepted turn visibly instead
of spilling to disk. The client separately retains one bounded submission per unacknowledged turn
across its UI-socket reconnect and resends the exact existing `chat_message`. A fully matching
`voice_submission_rejected` is terminal for those IDs and clears both worker and client buffers
immediately. `explicit_user_retry` means the user may deliberately speak/submit again with fresh
turn/submission/request IDs; it never authorizes automatic replay, a hidden queue, or reuse of the
rejected IDs. Repeating an already rejected ID tuple returns the same rejection disposition without
dispatch. PostgreSQL uniqueness on owner plus `client_turn_id`/`submission_id` makes every path
idempotent. Expiry without acceptance emits a safe explicit-retry failure and never claims a request
ran.

## 5. Announcement Media and Local Playout Evidence

Topic: `astraldeep.voice.announcement.v1`
Reliability: reliable, ordered
Publisher: expected voice-worker identity only
Maximum JSON UTF-8 envelope: 4 KiB

Before audio can render, the worker publishes a strict `voice_announcement_media` manifest from
[voice-control.schema.json](voice-control.schema.json). It binds the session generation, current
grant revision, announcement/turn/kind/sequence, expected worker identity, exact 24 kHz profile,
strict quantum role/index, actual duration samples, and a transport locator. `duration_samples` is
at most 96,000 (four seconds); `result_opening` is index zero and at most 36,000 samples (1.5
seconds), while only later `result_continuation` frames may use positive indexes. `single` is index
zero and cannot claim result content. Each direct-client quantum uses a distinct ephemeral worker audio track;
the manifest names its `track_sid`/`track_name`, and the worker unpublishes it on finish or
interruption. An unknown, stale, unmatched, duplicate, non-worker, or audio-before-manifest track is
buffered for at most one second and then dropped without rendering. An over-budget or role/index-
inconsistent manifest is dropped immediately. A direct client hard-stops/discards samples beyond
the declared count even if a worker track misbehaves; a watch range must decode to exactly the
declared count and any extra PCM is rejected. The watch receives the same manifest before the
declared assistant-PCM sequence range. Only the pre-turn greeting has null
`turn_id`.

The manifest is content-free and does not contain recap text. A client derives started/finished/
interrupted only from its locally matched render pipeline, then sends the dedicated top-level
authenticated UI-WebSocket frame `voice_playout_event` (not `ui_event`, not a generic action). It
includes its registered UUID4 `device_id`, current UUID4 `connection_generation`, session/
generation/grant revision, announcement binding, quantum role/index, and strictly increasing client sequence. The
server validates the active control binding/owner and accepts at most eight such <=2 KiB frames per
second per device before ordinary operation admission. It creates no task/message/operation and
grants no authority. Server receipt time is the operational timestamp; the client wall clock is
diagnostic, while ephemeral in-memory acoustic capture remains release evidence for actual sound.

## 6. Composer Control Mapping

The server-owned composer model names semantic controls; it does not authorize a generic
`voice_action`. Thin clients perform the following fixed mapping after local capability/permission
handling, and each REST handler revalidates authenticated owner, expected generation, state, and
payload. `activation_id` is a client UUID4 minted at the explicit start/takeover click and used only
for exact-request idempotency/correlation; `(user_id, activation_id)` is unique and a lost response
retry returns the same logical session/grant without another start/takeover. It is not trusted as
authorization or OS-permission proof.

Each client generates one non-secret UUID4 installation `device_id` and persists it in normal
platform application storage (web origin storage, Windows app settings, Android app storage, Apple
app/Keychain scope). The existing registration frame gains strict top-level `device_id`; each
`register_ui` supplies it plus a freshly client-generated UUID4 `connection_generation`.
After authenticating the UI socket, the server delivers a short-lived `voice_control_binding` once;
clients keep it only in memory and redact it. Every mutating voice REST call sends the standard user
bearer plus matching device, connection-generation, and control-binding headers. Signature,
expiry, authenticated user, active connection, and session owner are rechecked. Reconnect rotates
the connection and binding; another same-user device cannot control the session without explicit
takeover.
Each binding expires after at most ten minutes and never later than the Keycloak token or registered
socket; the server may issue a replacement only over that same authenticated socket. Close,
reauthentication, or reconnect revokes/rotates the binding before further control.

If explicit start/takeover occurs with no selected chat, the client first invokes an additive strict,
correlated form of the existing `new_chat` action. It supplies `schema_version: "1"`, the current
UUID4 `connection_generation`, and stable UUID4 `submission_id`/`request_generation`; the strict
`chat_created` response echoes all three plus the owner-validated chat UUID4. The server records the
exact request digest in the existing `operation_record` idempotency machinery under the
owner-scoped `new_chat` namespace: an exact retry returns the same chat, while reuse with changed
input fails closed. The client adopts/hydrates the response only if all IDs still match the pending
activation on the same current UI connection and the user has not navigated elsewhere. A delayed or
stale response may not switch chats or start media. Only after that correlated acknowledgement may
the client call the voice REST operation. No grant, microphone capture, greeting, or recognition
starts against a null chat.

| Composer action | Authenticated operation |
|---|---|
| `voice_session_start` | `createVoiceSession` |
| `voice_session_takeover` | `takeOverVoiceSession` |
| `voice_session_end` | `endVoiceSession` |
| `voice_microphone_set` | `updateVoiceSession` with `microphone_enabled` |
| `voice_speech_mute_set` | `updateVoiceSession` with `speech_muted` |
| `voice_visible_chat_update` | `updateVoiceSession` with `visible_chat_id` |
| `voice_speech_stop` | `stopVoiceSpeech` |
| `voice_sensitive_recap_request` | `consentToSensitiveRecap` |

Static/runtime media capability is an additive `register_ui` capability and the REST request echoes
the current checked values; it is not a free-standing action. The only new client-to-server voice
frame on the UI WebSocket is the dedicated content-free `voice_playout_event`; it is handled as
telemetry/control evidence before normal UI-action admission and is never dispatched as a UI
action. No voice mutation travels over the UI WebSocket.

## 7. Worker Control Channel

Each worker opens one TLS pool WebSocket to an internal orchestrator route and performs a bounded
challenge-response using the dedicated operator-managed `VOICE_CONTROL_SECRET`; the raw secret is
never sent. The authenticated worker advertises only its opaque identity, bounded capacity, and
fixed-profile readiness. The coordinator selects a worker, claims the session's database control
lease, and sends an idempotent `session_bind`; workers never poll an assignment endpoint and no
LiveKit Agents dispatch exists. The bind delivers a separate short-lived, room-scoped direct-RTC
worker join grant minted by orchestrator-only `livekit-api`. That grant
permits the designated worker identity to subscribe to the designated microphone and publish
assistant audio/data in that room; it grants no room administration, recording/egress, SIP, or API
secret. The bearer is memory-only, is replaced only by a higher worker-grant revision on reconnect,
and is redacted together with challenge material and headers. Every multiplexed frame remains
independently session/generation/sequence fenced; a connection loss does not authorize another
replica until the database lease expires or is explicitly transferred.

The challenge occurs during the HTTP upgrade rather than as an untyped WebSocket frame: an initial
request receives a short-lived single-use server nonce, and the retry supplies worker identity,
nonce, timestamp, and a domain-separated HMAC in bounded headers. The first schema-valid worker
frame is `worker_register`; the coordinator replies `worker_registered` with the accepted capacity,
connection ID, and heartbeat interval. Neither frame carries a credential. Pool-frame sequences are
separate from each bound session's per-direction sequence. After registration, the worker sends one
`pool_heartbeat` at every accepted interval even when it owns zero sessions. Its sequence starts at
one (the registration used pool sequence zero), and its worker identity and connection ID must match
the current authenticated socket. Only a valid, ordered pool heartbeat or a valid ordered session
frame refreshes the in-memory connection lease; malformed, replayed, cross-connection, and
cross-identity heartbeats fail closed. Session `heartbeat` frames remain independently sequenced and
carry only per-session media state.

For every `session_bind`, `assignment_id` must match the session's current database assignment; the
nested worker grant's room and worker identity must exactly equal the outer `room_name` and
`worker_identity`; its revision must exactly equal both the outer
`worker_rtc_grant_revision` and the persisted value; `issued_at`
must not predate the assignment; and `expires_at` must be later than server receipt but no more than
five minutes after `issued_at`. Any mismatch closes only that assignment, clears its media state,
and emits a content-free failure; it never falls back to an API credential or another room.

Frames validate against [worker-control.schema.json](worker-control.schema.json). The channel:

- carries coordinator-approved bounded speech text with a mechanical quantum role/index/sample
  ceiling and matching media/speech lifecycle metadata;
- never accepts a transcript as authorization and never invokes chat dispatch;
- never carries raw audio, tool data, hidden reasoning, provider bodies, or credentials other than
  the purpose-bound worker room grant inside `session_bind`;
- is frame/rate/queue bounded and closes on malformed, replayed, stale, cross-session, or oversized
  input;
- applies generation/sequence checks before side effects;
- installs the initial accepted publisher in `session_bind`; on refresh it applies ordered
  `media_grant_rotated`, makes the prior identity drain-only for an already immutable bound turn
  and rejects it for every new VAD/audio start, then returns `media_grant_applied` before the new
  grant is released to the client; no post-rotation old-identity audio is accepted;
- cancels queued speech on terminal, waiting, mute, end, takeover, stale generation, or disconnect;
- reports source publication/playout start/finish/interruption; each client also sends the
  content-free, generation/revision/connection-fenced `voice_playout_event` frame on its
  authenticated UI socket at local render start/finish/interruption. Server receipt time—not the client wall clock—is
  the operational observation; acoustic capture remains the proof of locally audible timing.

Direction is part of authorization, not merely documentation:

| Coordinator to worker only | Worker to coordinator only |
|---|---|
| `worker_registered`, `session_bind` (initial or higher worker-RTC-grant revision), `media_grant_rotated`, `session_context_update`, `turn_bound`, `transcript_accepted`, `transcript_rejected`, `speak`, `stop_speech`, `set_capture`, `end_session` | `worker_register`, `pool_heartbeat`, `media_grant_applied`, `session_context_applied`, `recognition_started`, `recognition_failed`, `worker_ready` (echoes assignment and applied worker-RTC-grant revision), `heartbeat`, `speech_started`/`speech_finished`/`speech_interrupted`/`speech_failed`, `media_state`, `transcript_emitted` |

A frame valid in shape but received in the wrong direction closes the session channel without a
side effect. Message IDs, per-direction sequences, generation, and service-authenticated channel
identity are checked before dispatch.

The per-user speech scheduler permits at most two active voice-originated turns and one physical
output stream. It synthesizes result openings concurrently but serializes playout. Each active turn
targets its next eligible utterance at 14 seconds after the later valid source/client finish
observation, before the hard 20-second next-start limit. Deadline-aware arbitration cancels stale
progress, gives terminal openings priority, and emits preemptible chunks: an attributed recap opening
is at most 1.5 seconds and a continuation/progress quantum at most four seconds. Before starting a
quantum, the scheduler reserves the other equally due turn's maximum four-second quantum plus a
measured 250 ms stream-handoff budget; it does not start work that would consume that reservation.
For two equal deadlines, the first due quantum begins no later than 15.5 seconds and the second no
later than 19.75 seconds. A handoff that cannot remain within 250 ms fails the media path honestly
rather than letting a deadline pass. Two coincident completions alternate their meaningful
attributed opening clauses (for example, “Your earlier request…” / “Your latest request…”), targeting
the second start by 1.75 seconds; longer recaps resume in bounded chunks. Labels are ordinal and
content-free unless existing sensitivity policy authorizes more. Barge-in/new voice capture cancels
all unfinished prior recap chunks rather than replaying them behind the new turn. The deterministic
overlapping-turn gate injects positive 250 ms handoffs and proves the 20-second bound, terminal
preemption, and simultaneous-completion ordering; the staged real-speech test enforces the SC-004
p95 target and the handoff budget.

Every `speak` command carries `quantum_role`, `quantum_index`, and `max_duration_samples` bounded to
96,000 for `single`/continuation and 36,000 for `result_opening`. Before a result command is sent, a
row-locked coordinator CAS adds that command ceiling to the turn's conservative cumulative
reservation, fixes its quantum index, and rejects a sum above 720,000 samples; failed/interrupted
commands are never refunded, while an exact deterministic `announcement_id` retry reuses rather
than increments the existing reservation. Command, manifest, lifecycle, and playout frames echo
`result_reserved_samples_after` for result quanta and forbid it for `single`. The worker measures exact 24 kHz
synthesis before publication; if it exceeds that command budget, it emits a correlated
`speech_failed` reason and publishes neither manifest nor track. Manifest and speech lifecycle echo
the role/index and actual duration. Thus 30 seconds is only the durable per-result aggregate ceiling,
never a per-track or per-command allowance, and restart/retry cannot reset it.

If the control channel fails, the worker stops issuing new assistant speech, closes or reconnects
the media session honestly, and never falls back to autonomous progress text.

## 8. watchOS PCM Bridge

The official Swift client package has no watchOS/WebRTC slice. watchOS therefore receives
`transport: watch_pcm_websocket` and connects only while the app is foregrounded and microphone
permission is authorized.

### Handshake

1. HTTPS REST returns a WSS URL plus a one-time opaque ticket, session/generation/grant revision,
   expiry, expected worker identity, and codec profiles.
2. The watch sends the ticket in the authorization header, never in the URL/query.
3. The relay atomically consumes the nonce hash and binds the socket to one user/session/
   generation/grant revision. Replay, wrong device, expiry, origin/rate/capacity failure, stale
   revision, or takeover closes before audio acceptance.
4. A JSON `bridge_ready` control envelope confirms negotiated schema, exact voice profile, and the
   same expected worker identity; no audio or transcript is accepted before it.

### Binary audio frames

Each WebSocket binary message contains one 20 ms mono signed-16-bit-little-endian PCM frame plus a
small fixed header:

| Field | Size | Rule |
|---|---:|---|
| magic | 4 bytes | ASCII `ADVC` |
| version | 1 byte | `1` |
| direction/kind | 1 byte | mic `1`, assistant `2` |
| flags | 2 bytes | reserved bits must be zero |
| sequence | 8 bytes | unsigned big-endian, strictly increasing per direction |
| media timestamp | 8 bytes | unsigned microseconds from local stream start |
| payload length | 2 bytes | exact remaining PCM length |
| payload | bounded | mic: 16 kHz/640 bytes; assistant: 24 kHz/960 bytes |

The watch uses `AVAudioConverter` to normalize hardware capture to 16 kHz mono PCM and playback
from exact 24 kHz mono PCM. The relay rejects gaps beyond its bounded reorder/recovery policy,
duplicates, invalid flags/length/rate, text-as-audio, oversized frames, excessive duration, or
binary frames before authorization. No binary frame is written to disk.

### JSON control/data envelopes

Bounded JSON messages carry only:

- ready/reconnecting/error/ended state and reason codes;
- the same `voice_transcript` envelopes used on LiveKit data;
- the same `voice_announcement_media` manifest before each declared assistant PCM sequence range;
- speech lifecycle IDs/timing, ping/pong, and explicit client interruption;
- no grants, secrets, raw provider error/body, tool data, or hidden reasoning.

The relay feeds mic PCM into the same direct-RTC worker's Silero/ASR state machine and sends exact
Kokoro frames back. This is a last-mile transport adapter, not platform speech, a second voice
profile, or a second agentic path.

## 9. Endpointing and Turn Identity

- Silero VAD plus configured endpointing determines a natural turn end. The launch path does not
  use an LLM turn detector.
- Recognition start echoes the server-confirmed visible `chat_id`/context revision; the coordinator
  allocates the remaining immutable binding before transcript publication. Navigation after that
  cannot rebind the turn.
- Final recognition is emitted once. Provider retries preserve the same IDs and sequence lineage.
- Final recognition always carries a normalized detected BCP-47-compatible language. `en`/`en-*`
  selects `full_recap`; every other language selects `english_lifecycle_only` plus reason
  `output_language_unsupported`. Visible transcript/full text remains unchanged; speech is limited
  to a safe English lifecycle/result-ready notice and never pretends to translate the result.
- Starting a newer recognition turn is allowed while old accepted work continues. The server
  transaction selects the newer foreground turn; media does not cancel the older task.
- The final transcript always follows normal authenticated text acceptance. Inside that authority
  seam, a strict server-side allowlist may route an exact phrase to a currently valid normal
  control (sensitive-result read, speech stop/mute, or existing foreground-task cancellation).
  The worker/client never decides intent and no model infers it; ambiguous text dispatches as the
  ordinary query.

## 10. Readiness and Failure Contract

Capability is `ready` only when bounded probes confirm:

1. LiveKit signaling/room operation and a ready worker;
2. exact ASR model inventory plus authenticated bounded batch transcription after worker VAD;
3. exact TTS model, `af_heart`, WAV, and parsed 24 kHz output;
4. transport-specific client support (direct LiveKit or watch bridge);
5. capacity for the authenticated user/deployment.

Readiness is cached briefly with checked/expiry timestamps but never inferred from environment
presence. Missing/rejected credentials, model/voice drift, malformed audio, timeout, redirect,
oversize response, worker crash, or media failure changes voice to a typed-fallback state without
taking AstralDeep health or typed chat down. Error payloads contain stable reason codes and safe
messages only.

## 11. Network and Deployment Requirements

- Production: trusted HTTPS/WSS, certificate verification, public/reachable LiveKit signaling,
  ICE UDP/TCP and TURN per deployment topology, no loopback advertisement, no open ingress to
  worker-internal control routes, and explicit capacity/resource limits.
- Local Docker: expose required ports and generate a host-reachable advertised node/public URL for
  browser, PySide, Android emulator/device, and Apple simulators/devices. Container `localhost`
  must not be returned blindly. Exact local address selection is an operator/dev harness concern,
  not an end-user setting.
- Speech egress: only the configured fixed origin through `streaming_egress`; TLS required except
  an explicit local-development profile; DNS addresses pinned/validated; redirects/proxies off;
  time/frame/body/session bounds and redacted errors.
- No LiveKit E2EE claim is made through the server-side ASR/TTS processor: audio necessarily exists
  as plaintext in the trusted worker and configured speech processor. TLS/access isolation,
  ephemeral buffers, zero recording, and processor-boundary documentation are mandatory.

## 12. Observable Non-Content Metrics

Allowed labels/values include bounded state/reason, client kind, transport, timing histograms,
session/turn counts, reconnect/dedup/takeover counts, queue depth, cadence gaps, interruption
latency, readiness, and cleanup outcome. User/chat/session/turn identifiers are hashed or omitted
according to the existing telemetry policy. Transcript, recap, phrase text, audio, endpoint/key,
room token/ticket, provider body, and PHI never appear as metric labels or trace/log fields.
