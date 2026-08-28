# Data Model: Client-Local Conversational Speech

**Feature**: `075-client-local-speech`  
**Date**: 2026-08-28

## Design boundary

Feature 075 extends the existing Feature-065 voice session and turn lifecycle. It does not create a second conversation record, speech transcript store, or client-authority store. PostgreSQL retains only bounded ownership, lifecycle, correlation, and content-free outcome metadata. Audio, interim/final text copies, text digests, proofs, local engine information, voice inventories, endpoints, and credentials remain ephemeral.

AstralPlane owns all schema/repository changes. AstralDeep consumes the qualified Plane facade and pins its exact schema revision/digest in `config/astral-composition.json`.

## Enumerations

### `VoiceSpeechBackend`

| Value | Meaning |
| --- | --- |
| `llm_factory` | Existing LiveKit/watch media plus isolated remote Whisper/Kokoro worker. |
| `client_local` | Client performs local-only ASR/TTS; Astral retains session/dispatch authority and sends no audio to speech services. |

The deployment selector and every new v2 frame use these exact values. `server_media`, `remote`, `browser_local`, `platform_local`, and `on_device` are not accepted aliases.

### `VoiceTransport`

| Value | Backend | Meaning |
| --- | --- | --- |
| `livekit` | `llm_factory` | Existing primary-client RTC media. |
| `watch_pcm_websocket` | `llm_factory` | Existing watch foreground PCM bridge. |
| `client_local` | `client_local` | No server media room, worker, bearer grant, or PCM transport. |

### Local outcome reasons

Public reason strings are a closed, low-cardinality vocabulary:

- Eligibility: `client_contract_upgrade_required`, `client_readiness_required`, `microphone_permission_not_determined`, `microphone_permission_denied`, `speech_recognition_permission_not_determined`, `speech_recognition_permission_denied`, `no_microphone`, `no_audio_output`, `local_processing_not_guaranteed`.
- Asset/locale: `local_recognition_unavailable`, `local_synthesis_unavailable`, `local_recognition_locale_unavailable`, `local_synthesis_locale_unavailable`, `local_language_download_required`, `local_language_installing`, `local_language_install_failed`.
- Runtime: `local_capture_not_ready`, `local_session_not_ready`, `local_recognition_failed`, `local_recognition_cancelled`, `local_synthesis_failed`, `local_audio_interrupted`, `local_engine_lost`, `local_announcement_expired`.
- Validation: `stale_connection`, `stale_session`, `stale_speech_revision`, `stale_chat_context`, `stale_local_turn`, `duplicate_local_final`, `altered_local_final`, `local_final_empty`, `local_final_oversized`, `local_final_malformed`, `local_language_mismatch`.
- Announcement: `announcement_stale_sequence`, `announcement_suppressed_muted`, `announcement_suppressed_background`, `announcement_consent_invalid`, `announcement_invalid`.

Owner/device/session mismatches retain the existing non-enumerating `invalid_binding` or `stale_session` disposition rather than exposing which identity existed.

## Durable entities

### `voice_session` changes

Existing columns remain unless conditionality is noted.

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `speech_backend` | text | yes | New. `llm_factory|client_local`; existing rows backfill to `llm_factory`. Immutable after insert. |
| `transport` | text | yes | Existing vocabulary plus `client_local`; must match `speech_backend`. |
| `room_name` | text | remote only | Existing nonempty value for `llm_factory`; null for `client_local`. |
| `participant_identity` | text | remote only | Existing nonempty value for `llm_factory`; null for `client_local`. |
| `worker_identity` | text | assigned remote only | Existing behavior; always null for `client_local`. |
| `media_grant_nonce_hash` | bytea | remote only | Existing 32-byte hash; null for `client_local`. |
| `media_grant_issued_at` | timestamptz | remote only | Existing ordering rules; null for `client_local`. |
| `media_grant_expires_at` | timestamptz | remote only | Existing ordering rules; null for `client_local`. |
| `worker_assignment_id` and worker RTC grant fields | existing types | remote only when assigned | Always null for `client_local`. |
| `generation` | bigint | yes | Existing positive owner/session fence for both backends. |
| `media_grant_revision` | bigint | yes | Retained schema name for compatibility; server v2 projects it as `speech_revision`. It monotonically fences local activation/announcement/final frames but never represents a bearer grant in local mode. |
| all owner/device/chat/control/lease/state fields | existing | yes/conditional as today | Semantics unchanged for both backends. |

Named exhaustive constraint `voice_session_speech_backend_075_check` enforces:

```text
llm_factory:
  transport in (livekit, watch_pcm_websocket)
  room_name, participant_identity, media nonce/issued/expiry are non-null
  all existing grant ordering/length rules remain true

client_local:
  transport = client_local
  room_name, participant_identity, worker identity/assignment,
  worker grant metadata, media nonce/issued/expiry are null
```

No update operation may change `speech_backend` or `transport`. Operator configuration changes affect only newly created sessions after restart; existing sessions are ended/drained.

### `voice_turn`

No new column is required. A turn's backend is derived from its immutable parent session. Existing uniqueness on `(user_id, client_turn_id)` and `(user_id, submission_id)` provides replay/idempotency protection for both modes.

The following existing fields retain their meaning:

- `session_generation` fences takeover/session replacement.
- `media_grant_revision` stores the session's remote media revision or local `speech_revision` compatibility value.
- `chat_id`, `chat_context_revision`, and execution-base metadata bind ordinary durable conversation publication.
- `state`, accepted/processing/waiting/terminal timestamps, and terminal/rejection fields remain one lifecycle.
- Announcement sequence/claim/playout timing fields are reused. Announcement text is never stored.

Local recognition follows the existing transitions:

```text
recognizing
  ├─ voice_local_final valid ─> submitting ─> accepted ─> processing/waiting/terminal
  ├─ explicit local failure ─> abandoned
  ├─ stop/mute/takeover/chat loss/expiry ─> abandoned
  └─ stale/replay before a valid bound turn ─> no durable side effect
```

### Conversation messages, commits, and audit

No schema change. After `LocalTranscriptAdmission` validates the bound final, the existing ordinary chat acceptance transaction writes the user message/commit and associated content-free voice correlation. The local adapter cannot write a message, run an LLM/tool, or publish a result independently.

Existing audit redaction remains mandatory. A content-free `speech_backend=client_local` and allowlisted outcome may be recorded; text, digest, proof/attestation, local engine, language inventory, and device path may not.

## Ephemeral entities

### `SpeechBackendSelection`

| Field | Type | Rule |
| --- | --- | --- |
| `value` | `VoiceSpeechBackend` | Parsed once from process environment. |
| `valid` | bool | Explicit malformed/empty selection is false. |
| `source` | enum | `legacy_default|explicit`; no raw invalid value in logs/status. |

This is configuration, not database state.

### `ClientLocalSpeechCapability`

Bound to authenticated `(user_id, device_id, connection_generation)` and supplied fresh for v2 activation/takeover. It is validated, used, then discarded.

| Group | Fields | Bounds/rules |
| --- | --- | --- |
| Contract | `schema_version`, `contract`, `transport`, `configured_locale`, `full_duplex`, `requirement_revision` | Exact `2`, `client_local/v1`, `client_local`, server-required `en-US`, `false`, and current revision. The client does not choose locale. |
| Input/output | `has_microphone`, `has_audio_output` | Both true. |
| Permissions | microphone and recognition permission | Exact allowlisted states; both `authorized` to activate. |
| ASR | `recognition_processing`, `recognition_locale`, `recognition_installation` | Exact `guaranteed_local`, `ready`, `ready`; categorical only. |
| TTS | `synthesis_processing`, `synthesis_locale` | Exact `guaranteed_local`, `ready`; no voice names/inventory. |

The server treats it as untrusted eligibility information. It grants no tool, LLM, conversation, or identity authority.

### `LocalRecognitionBinding`

Server-owned in-memory projection over the durable `voice_turn` row:

- authenticated user/device/current socket plus its server-held unexpired control binding (the bearer is absent from local WebSocket frames);
- session ID, generation, speech revision;
- immutable chat ID/context revision;
- client turn ID plus server turn/submission/request IDs;
- strictly increasing local recognition sequence;
- at most two-minute binding expiry.

### `LocalTranscriptAttestation`

Internal-only immutable value created after final canonicalization/digest validation. It contains only the verified binding, canonical text for the current call stack, detected language, and server-derived `client_local` source. It is never serialized to the client, persisted, or logged. The ordinary dispatcher consumes it immediately.

### `AuthorizedTextAnnouncement`

| Field | Bound |
| --- | --- |
| Text | Server-authored, NFC, no forbidden controls, at most 600 UTF-8 bytes per frame and within existing aggregate recap policy. |
| Identity | Device, connection generation, session, generation, speech revision, announcement UUID, sequence, optional turn UUID. |
| Policy | Closed kind/output-policy enums, `en-US`, foreground requirement, and current monotonic mute and consent revisions. |
| Lifetime | Canonical UTC expiry no more than 10 seconds after issue. |

Text exists only in the outbound frame/current call stack. The client must not persist it outside ordinary visible conversation content already authorized by the server.

### `LocalPlayoutObservation`

Content-free authenticated event with the exact announcement binding, monotonic client sequence, `started|finished|interrupted|failed`, and optional allowlisted safe reason. It is an operational observation, never an audibility claim or authorization input.

## Session state transitions by backend

### `llm_factory`

Unchanged:

```text
starting -> worker/media allocation -> active
active <-> suspended/reconnecting
active|suspended|reconnecting -> ending -> ended
failure -> error/ended
```

### `client_local`

```text
starting
  -> v2 capability accepted / durable session created
  -> voice_local_ready validated against current capability/control state
  -> voice_local_session_ready returned
  -> active + greeting authorization

active
  <-> suspended (background, interruption, mute/capture loss)
  <-> reconnecting (socket replacement within lease)
  -> ending -> ended (end, logout, takeover, lease/chat loss)
  -> error/ended (local capability irrecoverable)
```

An ASR or TTS failure does not roll back an already accepted conversation result. It may suspend/end the voice session and expose typed fallback.

## Migration and recovery

### AstralPlane `075.001`

1. Require exact predecessor `074.004` and current expected migration digest.
2. Add nullable `speech_backend`, backfill every existing row to `llm_factory`, then set `NOT NULL`.
3. Drop only the specifically named old transport/remote-field constraints after verifying their exact definitions.
4. Permit `client_local`, relax remote-only columns to nullable, and install the exhaustive conditional constraint plus unchanged lifecycle constraints.
5. Update repository record/create validation and tests for remote, local, malformed mixed modes, idempotency, and legacy reads.
6. Bump `SCHEMA_REVISION`, recompute the guarded migration digest, and verify repeat execution plus representative pre-075 data.

### Deployment procedure

- Disable voice admission and drain/end active sessions before migration.
- Back up/verify representative PostgreSQL data through the normal Plane procedure.
- Apply the exact pinned Plane migration; verify schema revision/digest and all existing remote rows as `llm_factory`.
- Start AstralDeep in `llm_factory`, run remote smoke, then test `client_local` in a separate configured candidate environment.

There is no automatic down migration. Recovery is either restore the verified pre-migration backup with the old application or deploy a new guarded forward migration. Merely switching the selector back to `llm_factory` requires no data rewrite.
