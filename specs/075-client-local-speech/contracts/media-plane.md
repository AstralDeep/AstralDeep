# Speech and Media Plane Contract

**Feature**: `075-client-local-speech`
**Authority**: AstralDeep selects one speech backend for the process. Neither media nor local
recognition authorizes an Astral action; only the ordinary authenticated conversation dispatcher
may accept a user turn.

## 1. Backend-discriminated topology

```text
VOICE_SPEECH_BACKEND=llm_factory

web / Windows / Android / iOS / macOS -- LiveKit audio --> voice worker
watchOS ------------------------------ bounded PCM WSS --> voice worker
voice worker -- bounded fixed-origin HTTPS --> LLM Factory ASR/TTS
voice worker -- proof-bound final/control --> AstralDeep ordinary dispatcher

VOICE_SPEECH_BACKEND=client_local

device microphone --> OS/browser local-only ASR --> strict local-final adapter
strict local-final adapter --> AstralDeep ordinary dispatcher
AstralDeep speech policy --> authorized text announcement --> OS/browser local TTS
```

The selector is parsed once at startup. Missing preserves the legacy `llm_factory` default. An
explicit empty, unknown, or malformed value makes voice unavailable while typed chat stays usable.
Clients cannot override or automatically fall back between backends. A live session records its
immutable backend and is ended or drained across a deployment change.

The existing Feature-065 remote media, worker-control, transcript HMAC, grant, and playout bytes
remain v1. Local mode is a separate v2 session and `client_local/v1` frame contract. A new client
probes v2 discovery; a v2 404 means an older server and permits only the existing v1 remote flow.
An older client on a local deployment must fail voice closed.

## 2. Client-local eligibility

All observations are categorical, ephemeral, and bound to the current authenticated
user/device/connection. The server accepts local activation only when the client reports and the
client runtime enforces all of the following:

- current foreground explicit activation, microphone, audio output, and both permissions;
- configured locale `en-US` available for both recognition and synthesis;
- a recognition API that explicitly guarantees local-only processing;
- a synthesis voice/runtime that does not require a network connection;
- half-duplex capture (`full_duplex=false`) and immediate local stop support;
- exact `client_local/v1` handling for every required frame and terminal state.

Optional language asset installation starts only from a separate user gesture. `downloadable` or
`downloading` is not session-ready. No engine name, installed voice list, model path, model bytes,
or provider setting is sent to Astral.

Platform qualification is exact:

| Client | Local ASR gate | Local TTS gate | Unsupported disposition |
| --- | --- | --- | --- |
| Web | unprefixed recognition API; `processLocally=true`; positive `available(en-US)` | matching `speechSynthesis` voice with `localService=true` | typed-only; never prefixed/cloud fallback |
| Windows | signed first-party bounded-pipe System.Speech helper | packaged `QTextToSpeech` local engine | typed-only if helper/language/plugin absent |
| Android | API 33+ on-device recognizer plus installed `en-US` from `checkRecognitionSupport` | initialized matching non-network `TextToSpeech` voice | typed-only on API 26–32 or missing asset |
| iOS/macOS | `supportsOnDeviceRecognition` and request `requiresOnDeviceRecognition=true` | retained delegate-backed `AVSpeechSynthesizer` | typed-only when locale/runtime unavailable |
| watchOS | same explicit Speech runtime guarantees on the foreground watch app | retained `AVSpeechSynthesizer` | typed-only; never reuse remote PCM in local mode |

## 3. Local recognition and dispatch

1. The client stops synthesis, observes the 500 ms post-terminal echo fence, then starts local
   recognition only while the current session is active, foreground, unmuted, context-synced, and
   generation-fresh.
2. After the first speech/partial event, the client sends
   `voice_local_recognition_started`. Astral validates authentication, owner device, live socket,
   the server-held current unexpired control binding, session/generation/speech revision, current
   chat/context, state, sequence, and UUID4 before creating the ordinary durable `recognizing`
   voice turn. The bearer control value is not present in the WebSocket frame.
3. Astral returns `voice_local_turn_bound` with server-minted turn, submission, and request IDs.
   The binding expires within two minutes. Interim text never leaves the device.
4. The client canonicalizes one final by converting CRLF to LF, applying Unicode NFC, trimming
   outer whitespace, and rejecting NUL or controls other than tab/newline. It sends at most 8,000
   Unicode scalar values and a lowercase SHA-256 of the canonical UTF-8 bytes in
   `voice_local_final` with the complete binding.
5. Astral repeats canonicalization and digest calculation, constant-time compares the digest,
   rejects stale/altered/cross-binding/replayed input, and constructs a call-stack-only
   `client_local` attestation. A sibling `admit_local_transcript` shares canonicalization, durable
   turn transition, and `TranscriptAdmission` output with remote admission but validates the
   authenticated client/session binding instead of a worker HMAC. This adapter cannot write a
   conversation or call an LLM, tool, or agent. It hands the admission to the same
   `handle_chat_message` path as remote voice/typed chat, including owner, LLM-selection,
   permission, PHI, confirmation, audit, execution, commit, cancellation, and publication gates.
6. Empty/error/interrupted recognition sends `voice_local_recognition_failed` for the exact bound
   turn. An unbound or stale failure has no durable side effect. Exact valid-final replay is
   idempotent; changed reuse fails.

Local transcript text is untrusted user content. It receives no worker, system, tool, or agent
authority. Local frames contain no remote transcript proof and the client never receives a proof
key or `VOICE_CONTROL_SECRET`.

## 4. Authorized local synthesis

The existing VoiceCoordinator owns greeting, acknowledgement, progress, waiting, result,
sensitive notice, failure, refusal, and cancellation policy. In local mode it emits only a bounded
`voice_local_announcement` to the current owner socket. The frame binds session/generation/speech
revision, current connection, announcement/turn/sequence, kind, `en-US`, canonical text digest, and
an expiry no more than ten seconds after issue. Text is at most 600 UTF-8 bytes and remains within
the existing aggregate result/consent/cadence limits. Only greeting may have a null turn.

Clients reject duplicate, out-of-order, stale, expired, wrong-session, hidden, muted, or
unauthorized announcements. They speak only the frame text—never arbitrary DOM, chat, notification,
or model output. They synchronously stop recognition before synthesis, use one serialized playout
owner, and hold recognition closed until `finished|interrupted|failed` plus a 500 ms echo fence.

`voice_local_playout_event` contains no text or audio. It reports `started`, `finished`,
`interrupted`, or `failed`, is rate/sequence/binding fenced, and cannot prove that sound was audible
or that a task was accepted. A TTS failure after durable text acceptance does not roll back or
repeat the accepted turn.

## 5. Lifecycle and privacy

Backgrounding, route/interruption, logout, takeover, session end, permission loss, or explicit stop
first stops capture and synthesis synchronously on-device and clears bounded buffers. The network
update is best effort. Reconnect rotates the UI control binding and must revalidate capability,
session generation, speech revision, chat context, foreground, and mute state before capture.

In local mode, no microphone audio may reach Astral, LLM Factory, a browser cloud recognizer, a
network TTS engine, files, caches, analytics, or crash attachments. Audio and interim/final working
text live only in bounded memory. Logs, metrics, audits, exceptions, and durable voice records omit
audio, unrestricted transcript/announcement text, digests, engine identifiers/paths, endpoints,
credentials, and hidden reasoning. Low-cardinality backend/phase/duration/outcome values are
permitted.

## 6. Remote reliability retained by this feature

`llm_factory` preserves the exact Feature-065 topology and profile. Feature 075 changes only these
reliability seams:

- every ASR/TTS operation has one total deadline shared by all attempts; retries cannot multiply
  the configured deadline;
- successful exact inference preflight is followed by synchronous greeting and earliest-
  acknowledgement cache warm-up before worker registration; remaining phrases warm asynchronously;
- readiness represents real inference, not model inventory alone;
- Windows and Android recover an owner-valid current grant within the lease using current
  generation/revision and reject stale identities/grants/turns;
- content-free phase timers distinguish configuration, connection/inference preflight, fixed-phrase
  warm-up, worker registration, recognition, synthesis, and playout.

The observed OOM incident produced remote ASR 500 responses and the former two 15-second attempts
explain the roughly 30-second symptom. After recovery, exact preflight completed in 1.393 seconds;
warm TTS took 0.142–0.153 seconds and ASR 0.355–0.418 seconds for the comparison phrase. This is
diagnostic evidence, not candidate release qualification, and does not remove local resilience.

## 7. Qualification boundary

Deterministic unit/contract tests fake platform engines. Integration tests block every remote
speech endpoint in local mode and assert zero calls, ordinary dispatch/auth/PHI parity, bounded
memory, strict frame rejection, retry idempotence, and typed fallback. Candidate qualification then
uses real PostgreSQL/Keycloak/dispatcher dependencies, the configured remote service, a supported
browser, Windows host, Android device, and iOS/macOS/watchOS devices. Callback completion is not
audibility proof; physical/acoustic evidence is required for audible claims.
