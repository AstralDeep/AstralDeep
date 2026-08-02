# Phase 1 Data Model: Conversational Voice

**Feature**: 065-conversational-voice
**Database**: PostgreSQL through `backend/shared/database.py::_init_db()` only
**Schema revision**: integrated final predecessor `064.001` → target `065.001`

## Design Rules

1. The ordinary chat message and committed assistant result remain the only durable content.
2. Voice tables store ownership, correlation, fencing, lifecycle, and bounded timing metadata—not
   audio, partial/final transcript copies, recap text, credentials, or grants.
3. Every mutation is scoped by authenticated `user_id`, `session_id`, and `generation`.
4. Takeover and duplicate final-transcript races are resolved in PostgreSQL, not process memory.
5. All timestamps used for persistence are UTC. In-process cadence scheduling uses monotonic time;
   persisted UTC times support reconstruction only and are never compared as cross-host monotonic
   clocks.
6. Additive DDL is guarded and repeat-safe. T024 integrated the authorized feature-064 handoff and
   recorded `064.001` as the sole predecessor. The 065 migration advances only that predecessor to
   `065.001` without overwriting 064's DDL or tests and refuses every other source revision.
7. Client timestamps are never lease or cadence authority. Authenticated server receipt time is
   stored for interaction/playout observations; client wall clocks remain diagnostic only.

## Durable Entity: `voice_session`

One row represents one authenticated user's client-owned foreground media relationship. Historical
ended rows provide bounded operational/audit correlation without media content.

| Column | Type | Constraints / meaning |
|---|---|---|
| `session_id` | UUID | Primary key; server-generated. |
| `user_id` | TEXT | Non-empty authenticated subject; indexed. |
| `activation_id` | UUID | Explicit start/takeover UUID4 idempotency key; unique per user and stable only for exact request retry. |
| `device_id` | UUID | Client-generated stable installation UUID4, bound by authenticated `register_ui`; identification only, never authorization by itself. |
| `device_kind` | TEXT | Check: `web`, `windows`, `android`, `ios`, `macos`, `watchos`. |
| `transport` | TEXT | Check: `livekit`, `watch_pcm_websocket`. |
| `room_name` | TEXT | Server-generated opaque LiveKit room name; unique while active. |
| `participant_identity` | TEXT | Server-generated client/relay participant identity. |
| `worker_identity` | TEXT | Expected server-designated worker participant; nullable until ready. |
| `visible_chat_id` | TEXT | Non-null retained chat UUID string for every unended session; owner-validated against a live authorized chat on every bind/update, but deliberately **not** an FK so hard deletion cannot erase/fail voice fencing history. |
| `chat_context_revision` | BIGINT | Starts at 1 and increments on each server-accepted visible-chat change. |
| `applied_visible_chat_id` | TEXT | Last destination acknowledged by the ordered worker-control channel; nullable until initial bind acknowledgement. |
| `applied_chat_context_revision` | BIGINT | Nullable until initial acknowledgement; thereafter never exceeds desired revision. |
| `state` | TEXT | Check: `starting`, `active`, `suspended`, `reconnecting`, `ending`, `ended`, `error`. |
| `speech_muted` | BOOLEAN | Default false; orthogonal to state. |
| `microphone_enabled` | BOOLEAN | Default true after permission; false stops capture without ending work. |
| `foreground_active` | BOOLEAN | Default true; false is automatic app/OS suspension and fences capture/playout independently of user mute. |
| `foreground_reason` | TEXT | Bounded lifecycle reason: foreground, backgrounded, locked, audio_interrupted, route_unavailable, or connection_lost. |
| `generation` | BIGINT | Starts at 1; increments only on takeover/session replacement fencing. |
| `media_grant_revision` | BIGINT | Starts at 1; increments on reconnect grant rotation without invalidating accepted turns. |
| `owner_connection_generation` | UUID | Current authenticated UI connection UUID4 allowed to control this owner session. |
| `control_binding_id` | UUID | Current short-lived signed voice-control binding identifier; bearer value is never stored. |
| `control_binding_expires_at` | TIMESTAMPTZ | Expiry fence for the current control binding. |
| `lease_expires_at` | TIMESTAMPTZ | Server lease for crash/reconnect cleanup. |
| `control_owner_id` | TEXT | Bounded orchestrator replica ID currently holding the worker-control socket. |
| `control_lease_expires_at` | TIMESTAMPTZ | Short renewable lease; another replica may recover only after expiry. |
| `last_interaction_at` | TIMESTAMPTZ | Server receipt time for speech, explicit control, or navigation; never a supplied client clock. |
| `idle_started_at` | TIMESTAMPTZ | Set only when listening with no turn/gate; drives five-minute expiry. |
| `started_at` | TIMESTAMPTZ | Server creation time. |
| `updated_at` | TIMESTAMPTZ | Last state mutation. |
| `ended_at` | TIMESTAMPTZ | Null while owned; terminal otherwise. |
| `end_reason` | TEXT | Bounded enum: user, idle, takeover, logout, auth_expired, chat_deleted, chat_unauthorized, lease_expired, media_error, shutdown. |
| `chat_unavailable_at` | TIMESTAMPTZ | Server time a current retained chat was deleted or authorization was revoked; null otherwise. |
| `takeover_of_session_id` | UUID | Optional self-FK to the prior ended session with `ON DELETE SET NULL`, so bounded retirement can prune old owners without breaking the replacement row. |
| `media_grant_nonce_hash` | BYTEA | Current client media/watch-ticket nonce hash; never the bearer value. |
| `media_grant_expires_at` | TIMESTAMPTZ | Expiry of the current client grant generation. |
| `media_grant_consumed_at` | TIMESTAMPTZ | One-time watch-ticket consumption; nullable for direct LiveKit grants. |
| `last_media_refresh_id` | UUID | Stable UUID4 idempotency key for the current refresh revision. |
| `media_grant_issued_at` | TIMESTAMPTZ | Fixed claim time used to deterministically remint the same current short-lived grant after a lost response. |
| `worker_assignment_id` | UUID | Coordinator-generated idempotency key for the current worker-pool `session_bind`; rotates with generation/worker reassignment, not an authorization bearer. |
| `worker_rtc_grant_revision` | BIGINT | Starts at 1 and advances only when the orchestrator remints the direct-RTC worker room grant for a fenced reconnect. |
| `worker_rtc_grant_issued_at` | TIMESTAMPTZ | Fixed claim time for deterministic current-grant remint; the bearer itself is never stored. |
| `worker_rtc_grant_expires_at` | TIMESTAMPTZ | Expiry fence for the worker's current room-scoped join grant. |

### Constraints and indexes

- Partial unique index on `(user_id) WHERE ended_at IS NULL` enforces one live media owner.
- Unique `(user_id, activation_id)` makes a lost start/takeover response replay-safe without creating another session/generation.
- Unique active room identity and bounded unique participant identity.
- Check `generation >= 1`, `media_grant_revision >= 1`, `chat_context_revision >= 1`, applied
  revision is null or at least 1 and does not exceed desired revision,
  `foreground_active` agrees with `foreground_reason`,
  `lease_expires_at > started_at`, and terminal fields agree with `ended_at`.
- Chat IDs are retained, indexed correlation values rather than chat FKs. Every live transition
  owner-validates them against the chat store; the value conveys no authority after deletion or
  revocation. Deletion or authorization loss of the session's current chat ends the media session;
  an active row never transitions to a null desired chat, and the ended row retains the former ID for
  fencing/audit until bounded voice retention expires.
- Retention follows bounded operational metadata policy; ended rows may be pruned only after all
  linked turns and required audit correlations expire.

### Session transitions

```text
starting -> active <-> suspended -> reconnecting -> active
    |          |          |             |          |
    +----------+----------+-------------+----------+-> ending -> ended
    +-------------------------------------------------------> error -> ended
```

- `start`: lock user ownership, insert when none exists, or return `takeover_required`.
- `takeover`: lock/CAS current generation, end old session, increment/fence generation, create the
  replacement, and best-effort disconnect the old LiveKit participant.
- `refresh`: same session/user/device plus expected generation/grant revision only; increment the
  grant revision, rotate participant identity/nonce, update the worker's accepted publisher through
  ordered rotate/applied frames before returning the credential,
  best-effort remove the old participant, and extend the lease without changing submitted turns.
  The old short-lived JWT cannot be cryptographically revoked, so the worker and coordinator ignore
  its old identity/revision for every new recognition until it expires; only a pre-rotation
  immutable bound turn may finalize from its existing bounded buffer. The first `refresh_id` fixes the new revision,
  identity, issued/expiry claims, and deterministic watch nonce. Retrying that ID within the short
  volatile recovery window remints the same current no-store grant without another increment;
  another ID with stale CAS receives credential-free current state. No bearer is stored.
- `suspend/resume`: background/lock/audio interruption first stops local media, then updates
  `foreground_active`. Suspension sets capture false and cancels queued speech without ending
  accepted tasks. Resume requires current UI control binding, capability recheck, grant refresh,
  and applied chat-context synchronization before capture.
- `visible-chat update`: the client pauses new capture before PATCH. The server accepts at most one
  pending desired context, increments the desired revision, and sends `session_context_update`.
  Ordered `session_context_applied` advances the applied fields and re-enables capture. A recognition
  already started before the update remains bound to the prior applied context; a new recognition
  cannot start while desired and applied revisions differ.
- `current-chat deletion/revocation`: atomically move the session to ending/ended with
  `chat_deleted` or `chat_unauthorized`, fence capture/playout, and clear unaccepted media buffers.
  Accepted execution/audit follows ordinary deletion policy, but result publication to the
  unavailable chat and every later voice announcement are suppressed. If an older turn's different
  bound chat disappears after navigation, reject only an unaccepted final, cancel/suppress all voice
  announcements for any already accepted turn from that chat, and keep the current session active.
- `idle expiry`: only when `idle_started_at` remains uninterrupted for 300 seconds and no turn or
  user-input gate is active. A client sends only `interaction: true`; the server stamps receipt time.
- Delayed frames/timers whose generation no longer matches are ignored and audited as stale.

### Hard chat deletion / authorization-loss transaction

`HistoryManager.delete_chat()` and equivalent owner-authorized deletion paths must call one shared
voice-aware transaction before the physical `DELETE FROM chats`; raw chat FKs are intentionally not
used as this lifecycle mechanism. Under owner/chat and affected voice-row locks it:

1. marks every current `voice_session.visible_chat_id` match ending/ended, stamps
   `chat_unavailable_at`/reason, fences capture/playout, and clears worker/client unaccepted buffers;
2. marks every unaccepted matching `voice_turn` terminally `abandoned` with
   `chat_unavailable`/`explicit_user_retry`. An accepted/processing/waiting turn is also terminalized
   as voice state `abandoned`, receives `origin_chat_unavailable_*`, and has all queued/playing/future
   announcements suppressed; this ends only the voice/publication correlation and does not cancel
   the already admitted operation;
3. for each affected staged `assistant_result`, clears its stage-scoped execution-base FK, aborts and
   removes only that private stage, and prevents task completion from recreating or publishing to the
   deleted chat; the already admitted operation may finish side effects/audit only under ordinary
   deletion policy;
4. physically deletes the chat and its normal messages/commits/components/layouts. `SET NULL` clears
   only the now-gone content/commit references on retained voice rows; their immutable user/session/
   turn/submission/chat ID correlations remain until bounded voice retention expires.

An access-revocation hook applies the same fencing without requiring physical deletion. A delayed or
replayed unaccepted final is looked up by the complete retained owner/session/generation/turn/
submission tuple and returns the same correlated `chat_unavailable` terminal disposition. A replay
of an already accepted tuple also receives a terminal chat-unavailable disposition and can never
dispatch a second operation. Neither path treats the retained chat UUID as authorization or
auto-creates the chat.

## Durable Entity: `voice_turn`

One row correlates one recognized final utterance with exactly one ordinary chat submission and
its normal durable execution/result. Transcript and result content remain in their existing stores.

| Column | Type | Constraints / meaning |
|---|---|---|
| `turn_id` | UUID | Primary key; server-generated correlation ID. |
| `client_turn_id` | UUID | Worker-generated UUID4 at VAD start; unique per user and stable across resend. |
| `session_id` | UUID | Foreign key to `voice_session`. |
| `session_generation` | BIGINT | Immutable generation at recognition start. |
| `media_grant_revision` | BIGINT | Immutable accepted publisher revision at recognition start. |
| `user_id` | TEXT | Repeated owner key for efficient owner-scoped uniqueness/checks. |
| `chat_id` | TEXT | Immutable retained chat UUID string taken when speech begins; owner-validated then and again at acceptance, but deliberately not an FK so a hard delete cannot erase the rejection/idempotency tombstone. |
| `chat_context_revision` | BIGINT | Server-issued session context revision echoed at VAD start. |
| `detected_language` | TEXT | Null only before final recognition; normalized BCP-47-compatible ASR language thereafter. |
| `spoken_output_policy` | TEXT | `pending`, `full_recap`, or `english_lifecycle_only`, derived deterministically from detected language. |
| `output_reason` | TEXT | `language_pending`, `ready`, or `output_language_unsupported`. |
| `execution_base_render_revision` | BIGINT | Immutable committed chat/canvas revision used by this turn's agent context. |
| `submission_id` | UUID | Stable normal `chat_message` UUID4; unique per user. |
| `request_generation` | UUID | Normal UUID4 request-generation value used by dispatch ordering. |
| `result_request_generation` | UUID | Server-generated UUID4 for the linked private assistant-result commit; unique per chat and stable across terminal retry. |
| `accepted_connection_generation` | UUID | Authenticated UI connection generation that accepted the message; audit only. |
| `message_id` | INTEGER | Nullable FK to existing `messages.id` (`SERIAL`) with `ON DELETE SET NULL` after normal acceptance. |
| `acceptance_commit_id` | UUID | Nullable until acceptance, then FK `ON DELETE SET NULL` to the committed `user_acceptance` conversation commit containing exactly this user bubble. |
| `result_commit_id` | UUID | Nullable until acceptance, then FK `ON DELETE SET NULL` to the linked private `assistant_result` conversation commit; unique per voice turn while present. |
| `operation_id` | UUID | Nullable FK to `operation_record.operation_id` with `ON DELETE SET NULL`; voice retention cannot pin expired operation metadata. |
| `background_task_id` | TEXT | Nullable FK to `background_task.task_id` with `ON DELETE SET NULL` (the current operation UUID string projection). |
| `state` | TEXT | Check: `recognizing`, `submitting`, `accepted`, `processing`, `waiting_on_user`, `succeeded`, `failed`, `refused`, `cancelled`, `abandoned`. |
| `is_foreground` | BOOLEAN | At most one nonterminal foreground turn per session; older work remains durable. |
| `terminal_kind` | TEXT | Null until terminal; check matches terminal state. |
| `rejection_reason` | TEXT | Nullable bounded pre-acceptance disposition: capacity exhausted, chat unavailable, invalid binding/proof, proof expired, permission denied, stale session, or malformed final. |
| `rejection_retry_policy` | TEXT | Nullable; `explicit_user_retry` or `none`, populated only for a pre-acceptance rejected/abandoned submission. Post-acceptance destination abandonment uses the origin-unavailable fields instead. |
| `origin_chat_unavailable_at` | TIMESTAMPTZ | Null unless the retained originating chat is hard-deleted or access-revoked. |
| `origin_chat_unavailable_reason` | TEXT | Null, `deleted`, or `access_revoked`; fences acceptance and every later announcement. |
| `result_id` | TEXT | Existing committed result/turn ID; unique when non-null. |
| `recap_source` | TEXT | `none`, `authoritative_summary`, `committed_visible_fallback`, `sensitive_notice`, `terminal_status`. |
| `sensitivity` | TEXT | `unknown`, `sensitive`, `non_sensitive`; unknown is treated as sensitive. |
| `sensitive_consent_at` | TIMESTAMPTZ | Fresh result-bound consent; null by default. |
| `sensitive_consent_method` | TEXT | Null, `tap`, or `strict_spoken_control`. |
| `sensitive_consent_consumed_at` | TIMESTAMPTZ | Set atomically when the one-result detailed recap is queued. |
| `announcement_sequence` | BIGINT | Last coordinator sequence, starts 0. |
| `result_reserved_samples` | BIGINT | Conservative fixed-24-kHz result budget reserved so far; default 0, check 0..720000. Each new deterministic announcement claim CAS-adds its `max_duration_samples` once before send; exact retry reuses the reservation, and failure/interruption never refunds it. |
| `result_quantum_count` | INTEGER | Number of result quanta reserved so far; default 0, check 0..32. The emitted `quantum_index` equals the pre-increment value. |
| `last_announcement_kind` | TEXT | Metadata only; no text. |
| `last_phrase_key` | TEXT | Allowlisted key only, used to prevent immediate repetition. |
| `next_announcement_due_at` | TIMESTAMPTZ | Reconstructable conservative due time; null while terminal/waiting/muted. |
| `announcement_claim_id` | UUID | Atomic scheduler claim; produces a deterministic announcement ID. |
| `announcement_claim_expires_at` | TIMESTAMPTZ | Short crash-recovery lease for the current claim. |
| `last_announcement_started_at` | TIMESTAMPTZ | Worker-confirmed publication/playout start metadata. |
| `last_speech_finished_at` | TIMESTAMPTZ | Worker-confirmed UTC observation for recovery/metrics. |
| `last_client_playout_started_at` | TIMESTAMPTZ | Server receipt time of the latest valid local-render start event. |
| `last_client_playout_finished_at` | TIMESTAMPTZ | Server receipt time of the latest valid local-render finish event. |
| `last_client_playout_sequence` | BIGINT | Strict per-client observation fence; starts 0. |
| `accepted_at` | TIMESTAMPTZ | Durable normal-chat acceptance time. |
| `processing_started_at` | TIMESTAMPTZ | Actual dispatch start; set before the acknowledgement becomes eligible. |
| `waiting_started_at` | TIMESTAMPTZ | User-input gate start. |
| `terminal_at` | TIMESTAMPTZ | Atomic terminal transition time: committed-result time for ordinary outcomes, or deletion/revocation fencing time for destination abandonment. |
| `created_at` / `updated_at` | TIMESTAMPTZ | Lifecycle bookkeeping. |

### Constraints and indexes

- Unique `(user_id, client_turn_id)` deduplicates media retries and reconnects.
- Unique `(user_id, submission_id)` ties retry to one ordinary message.
- Partial unique `(user_id, result_request_generation)` plus unique non-null
  `acceptance_commit_id` and `result_commit_id` prevent a retry from allocating a second commit pair
  while the chat exists; `ON DELETE SET NULL` lets hard chat deletion remove normal content/commits
  without erasing this retained voice-turn tombstone.
- Unique non-null `result_id` prevents two recaps for one committed result.
- Partial unique `(session_id) WHERE is_foreground AND state NOT IN (terminal states)` enforces one
  voice foreground while allowing concurrent durable background turns.
- Owner consistency is checked when correlating session/chat/message/task/result; no lookup may use
  an ID without `user_id`.
- Transcript hashes, transcript text, recap text, raw lifecycle messages, tokens, or tool data are
  intentionally absent.

### Recognition-time binding

1. On VAD start the worker creates `client_turn_id` and emits `recognition_started` with the
   session/generation/grant revision and its last acknowledged applied `chat_id`/
   `chat_context_revision`. New VAD is disabled while a context update is pending.
2. The coordinator validates control ordering and generation, creates `voice_turn` in one
   transaction, snapshots that chat, and allocates server `turn_id`, `submission_id`, and
   `request_generation` UUID4 values.
3. `turn_bound` returns those immutable fields to the worker before a transcript is published. The
   worker may keep only a bounded in-memory audio/partial buffer while binding is in flight.
4. Partial/final transcript envelopes carry the complete binding. The client adds its current
   authenticated `connection_generation` and submits the existing `chat_message` action with an
   owner-validated `voice_origin`; navigation cannot alter the bound chat. The verified voice branch
   invokes the normal authenticated dispatcher in bound-destination mode: it never auto-creates a
   missing chat, mutates `_ws_active_chat`, changes visible socket scope, or forces the client back to
   the originating chat. A delayed accepted turn publishes only to its original chat subscribers and
   ordinary background-status surfaces.
5. Normal acceptance verifies the short-lived HMAC proof over the immutable binding and normalized
   final-text digest, strips proof/digest before storage, records `message_id`, connection generation,
   and operation/task correlations, commits the ordinary user bubble, and sends a fully correlated
   `user_message_acked` plus `transcript_accepted.accepted_message_id`. Until then, the worker keeps
   only the bounded final/proof in memory and resends after media reconnect using the same IDs.
6. Any permanent pre-acceptance denial, including a missing/deleted/unauthorized bound chat, records
   only the bounded rejection metadata and emits fully correlated `transcript_rejected` and
   `voice_submission_rejected`. Both buffers clear immediately. `explicit_user_retry` requires fresh
   turn/submission/request IDs and a new user action; rejected IDs are never automatically replayed.

### Turn transitions

```text
recognizing -> submitting -> accepted -> processing -> succeeded
                    |                      |----> failed
                    |                      |----> refused
                    |                      |----> cancelled
                    |                      +----> waiting_on_user -> processing
                    +-> abandoned (empty/cancelled/refused before normal acceptance)

accepted | processing | waiting_on_user -> abandoned
    (origin unavailable; voice/publication correlation only)
```

- Partial transcripts do not create a durable turn row until a stable correlated recognition turn
  exists; they are never durable content.
- `submitting` retries reuse `client_turn_id` and `submission_id` only while no terminal accepted or
  rejected disposition exists.
- Session end, lease expiry, or takeover atomically changes any `recognizing`/`submitting` row for
  that generation to `abandoned` with `stale_session`/`explicit_user_retry`; accepted and later
  execution states are not cancelled. A bounded maintenance reconciliation applies the same
  transition to legacy or crash-window rows already attached to an ended session.
- Normal chat acceptance occurs only after a no-queue `voice_interactive` execution lease is active;
  it atomically establishes `message_id`/operation/task correlations and then enables exactly one
  acknowledgement. Capacity exhaustion is an explicit-user-retry refusal before normal acceptance;
  it is never a queued operation.
- A newer accepted turn flips the old row's `is_foreground` false and the new row true in one
  transaction. It does not cancel old work.
- Success/failure/refusal/cancellation terminal state and committed result are observed only after
  the ordinary conversation commit. The terminal hook fences progress, selects recap source, and
  queues at most one result speech. The sole post-acceptance no-commit terminal is `abandoned` after
  origin deletion/revocation; it fences publication/speech but does not imply operation cancellation.
- Multiple coordinator replicas may observe the same task event, but only the control-lease owner
  can send and only one atomic announcement claim/sequence can win. A crash expires the claim; a
  retry reuses the deterministic announcement ID and the worker deduplicates it.

### Concurrent same-chat execution and terminal publication

Feature 065 also evolves the existing `conversation_commit` coordination contract; it does not add
a second conversation-content store. The existing `base_render_revision` remains the mutable final
publication CAS base required by its lifecycle constraint. The repeat-safe 065 migration adds these
exact nullable/defaulted columns so legacy commits remain valid while every new
`voice_interactive` commit supplies an immutable execution base:

| Existing table | Additive column | Type and rule |
|---|---|---|
| `conversation_commit` | `publication_role` | `TEXT NOT NULL DEFAULT 'atomic'` checked to `atomic`, `user_acceptance`, or `assistant_result`; legacy rows remain `atomic`. |
| `conversation_commit` | `parent_commit_id` | `UUID NULL` self-FK `ON DELETE SET NULL`; required while a new `assistant_result` is staged and points to its committed `user_acceptance`. |
| `conversation_commit` | `execution_base_commit_id` | `UUID NULL` self-FK to `conversation_commit(commit_id) ON DELETE RESTRICT`; immutable current-chat anchor while `state='staged'`, null for revision-zero base, and cleared atomically on committed/aborted transition. |
| `conversation_commit` | `execution_base_render_revision` | `BIGINT NULL CHECK >= 0`; non-null and immutable for every new voice commit. |
| `conversation_commit` | `execution_base_components_sha256` | `CHAR(64) NULL`; lowercase digest of the canonical components selected through the base anchor/revision. |
| `conversation_commit` | `execution_base_layouts_sha256` | `CHAR(64) NULL`; lowercase digest of the canonical layouts selected through the base anchor/revision. |
| `conversation_commit` | `publication_rebase_count` | `INTEGER NOT NULL DEFAULT 0 CHECK >= 0`; incremented only when terminal publication rebases over a newer committed revision. |
| `workspace_layout` | `conversation_commit_id` | `UUID NULL` FK to `conversation_commit(commit_id)`; null only for legacy revision-zero rows. |
| `workspace_layout` | `committed_render_revision` | `BIGINT NULL CHECK > 0`; present with `conversation_commit_id`. |

The migration drops/recreates `ux_workspace_layout_chat_key` as a unique index on
`(chat_id, layout_key, COALESCE(conversation_commit_id,
'00000000-0000-0000-0000-000000000000'::uuid))`, adds the same null-together metadata check used by
`saved_components`, and adds a lookup index on
`(chat_id, conversation_commit_id, committed_render_revision, position)`. Authoritative component
and layout reads for a nonzero revision select only rows matching both
`chats.conversation_commit_id` and `chats.render_revision`; only revision-zero legacy reads select
null commit metadata. New voice commits enforce the application/constraint invariant that base
revision zero has a null base commit, while a positive base revision has a non-null committed base
anchor with matching chat/owner/revision. The self-FK and targeted cleanup retain that base while a
stage references it; the terminal transition releases the FK while retaining only the non-content
base revision/digests, so later cleanup does not form a permanent ancestor chain. This provides
immutable base content through the normal versioned rows without duplicating conversation/canvas
content into `conversation_commit`. A new `assistant_result` also
requires a same-chat/same-owner committed `user_acceptance` parent, its separate stored request
generation, all execution-base fields/digests, and a running matching operation fence. The existing
unique `(chat_id, request_generation)` therefore remains intact.

The same repeat-safe migration inserts `operation_admission_class('voice_interactive',
'interactive', active_limit=10, queue_limit=0, max_wait_ms=0, config_revision='065-defaults')` with
conflict-safe verification rather than overwriting operator policy. The coordinator also limits one
user to two simultaneously running voice-originated turns by default. This supports the five-user
target with a second in-flight turn apiece while refusing excess work before acceptance; operators
may narrow capacity through the existing policy path but cannot silently turn this class into a
queue and still claim immediate-start conformance.

1. Under one short chat transaction/async lock, admission validates owner/idempotency and obtains a
   running no-queue `voice_interactive` operation lease. It creates and atomically publishes a
   `user_acceptance` commit under the client `request_generation` containing exactly the ordinary
   new user bubble **and a normal versioned copy-forward of the complete currently authoritative
   component and layout rows**, but no assistant candidate output; it deliberately does **not**
   terminalize the operation. In that same transaction it
   allocates `result_request_generation`, inserts the linked private staged `assistant_result`
   commit, anchors its immutable execution base to the just-published acceptance commit/revision,
   records canonical component/layout digests, and stores both commit IDs on `voice_turn`. Only after
   commit does it broadcast the user bubble and fully correlated acknowledgement. A capacity refusal
   happens before either commit or any message.
2. Each accepted turn executes immediately in its own task-local publication stage. The chat-wide
   lock is not held during LLM, agent, confirmation, delegation, or tool work. Resource-specific
   authorization, operation fences, idempotency, and audit controls remain in force. Existing
   confirmation/user-input gate controls may publish through their normal owner-bound operation
   surface while the result stage stays private; they are not assistant/canvas candidate content and
   resume this same operation/commit rather than allocating a new turn.
3. Under a short terminal lock, publication materializes three immutable views for both components
   and layouts: the stored execution base, this commit's private candidate rows, and the latest
   committed rows selected through the chat anchor. Component merge keys are stable `component_id`;
   layout merge keys are `layout_key`, and equality uses canonical JSON digests. Non-conflicting
   additions/updates/deletes rebase onto latest. If both latest and candidate changed the same key
   from base, latest wins and this turn appends a safe conflict notice without overwriting it.
4. After component merge, every candidate layout is schema/canonical-form validated and every leaf
   reference must resolve in the merged component set. A valid non-conflicting layout rebases by
   `layout_key`; a same-key conflict preserves latest and records the safe notice. An invalid
   candidate layout is dropped or rendered flat for its surviving components and may not replace a
   valid latest layout.
5. The rebase transaction deletes/rewrites only rows belonging to this `assistant_result`
   `conversation_commit_id`,
   updates their revision pointers to `latest + 1`, updates this commit's mutable
   `base_render_revision` to latest, increments `publication_rebase_count` when applicable, and uses
   the normal atomic commit/CAS to advance the chat anchor. This result publication—not the earlier
   acceptance commit—terminalizes the matching operation fence and clears the now-unneeded
   `execution_base_commit_id` in the same transaction. It never deletes, overwrites, or exposes
   another still-staged commit and never reruns an LLM, agent, tool, confirmation, or consequential
   side effect. User bubbles retain acceptance order; attributed assistant results publish in honest
   terminal order when concurrent turns finish in reverse.
6. Abort and crash recovery clear the stage-scoped base FK and delete only the aborted commit's
   private message/component/layout stage.
   Old committed version rows remain addressable for normal snapshot/retention semantics and are
   pruned only by a targeted cleanup after no retained snapshot references them—never by chat-wide
   delete during publication.
7. A newer accepted voice turn atomically becomes foreground while the older operation continues
   running. “Background” is a voice/UI relationship, not reassignment to a queue or cancellation.

## Ephemeral Entity: Voice Service Profile

Configuration/readiness held in the isolated worker and capability cache:

- fixed ASR model, TTS model, `af_heart`, WAV/24 kHz;
- redacted endpoint origin identity/fingerprint (never returned to users);
- LiveKit/worker/ASR/TTS/voice readiness booleans, checked time, expiry, and reason code;
- capacity and operational kill-switch status.

It is not a user setting and has no database row. End users never provide or see its URL/key.

## Ephemeral Entity: Media Access Grant

A short-lived signed bearer returned once from the authenticated session API:

- `session_id`, `user_id` subject hash/reference, `device_id`, session `generation`,
  `media_grant_revision`, audience, expiry, nonce, and allowed transport;
- LiveKit: URL, opaque room, participant identity, token permitting join + microphone publication
  + audio/data subscription only (no room admin, API secret, arbitrary data publication);
- watch: WSS URL and one-time opaque ticket, fixed codec/size/rate limits.

Refresh increments only `media_grant_revision`, rotates participant identity/nonce, and returns the
authoritative session with the grant. An old LiveKit JWT remains cryptographically usable until its
short expiry, so the worker rejects the old publisher identity/revision and the server best-effort
removes it; only session takeover increments `generation`. Only purpose-specific nonce hashes/
expiry/consumption/idempotency markers may be stored. A retry of the same `refresh_id` remints the
same current short-lived claims rather than storing a bearer. Token/ticket values are excluded from logs, traces,
metrics, crash reports, UI snapshots, and audit metadata.

### Worker RTC grant

The worker first establishes one service-level pool WebSocket using bounded HMAC challenge-response
with `VOICE_CONTROL_SECRET`; no per-session bearer or polling endpoint exists. After that channel is
authenticated and a coordinator has claimed the session's control lease, the orchestrator uses its private
`livekit-api` dependency and API secret to mint a separate short-lived worker room grant. The grant
is carried only inside `session_bind`, permits the designated worker identity to join the assigned
room, subscribe to the designated client microphone, and publish assistant audio/data, and grants no
room administration, recording, egress, SIP, or API-secret access. It is held only in worker memory.
A direct-RTC reconnect receives a higher `worker_rtc_grant_revision` bind; stale revisions and
cross-session/room/identity claims are rejected. Only revision/issued/expiry metadata is persisted,
never the JWT, pool secret, API secret, or speech credential. Frames remain independently fenced by
session/generation/sequence when multiple assignments share one worker-pool connection.

## Ephemeral Entity: UI Control Binding

After authenticated `register_ui`, the server mints a short-lived signed bearer containing the
authenticated user, stable UUID4 device, fresh UUID4 UI connection generation, UUID4 binding ID,
audience, and expiry no more than ten minutes or the remaining Keycloak/socket lifetime. It is delivered once in `voice_control_binding`, retained only in client
memory, and required alongside the normal user bearer on every mutating voice REST request. The
server validates its signature and equality with the session's current device/connection/binding
fields. UI reconnect rotates it; explicit takeover is the only cross-device ownership transition.
Only binding ID/expiry may persist, and protocol capture/logging/crash tooling must redact the bearer.

## Ephemeral Entity: Spoken Announcement

In-memory/control-channel object:

- server-generated `announcement_id` plus required session, generation, grant revision, and
  announcement sequence; `turn_id` is null only for the pre-turn greeting;
- kind: greeting, acknowledgement, progress, waiting, result, sensitive_notice, failure,
  refusal, cancellation;
- strict quantum role/index and fixed-24-kHz ceiling: `single`/continuation <=96,000 samples,
  `result_opening` <=36,000 samples, and per-result accumulated output <=720,000 samples;
- allowlisted phrase key or bounded recap text;
- requested/started/finished/interrupted timestamps and status;
- sensitivity consent reference for detailed result speech.

The object is destroyed after playback/interrupt/error. Only bounded kind/key/timing metadata may
update `voice_turn`; content/audio is not a chat message and is never persisted.

## Migration Plan

1. Preserve the integrated feature-064 handoff and its final `SCHEMA_REVISION = '064.001'` as the
   sole predecessor without editing its branch/spec or discarding local owner changes.
2. Retain target `SCHEMA_REVISION = '065.001'` and the existing schema guard; fail rather than
   guessing if the checked-out predecessor differs from `064.001`.
3. Add `CREATE TABLE IF NOT EXISTS`, the exact additive `conversation_commit`/`workspace_layout`
   columns and versioned-layout indexes above, check constraints, and repeat-safe constraint/index
   guards inside `_init_db()` following current patterns.
4. Wire the single owner-authorized chat delete/revocation hook before enabling voice. Prove retained
   voice chat IDs are not `chats` FKs, content/operation references use the specified delete actions,
   staged base anchors are cleared before cascade, and current/older-chat deletion is repeat-safe.
5. Do not backfill historical chat/task rows; voice tables begin empty. Representative migration
   tests start with the immediately preceding schema plus real-format user/chat/background-task
   rows, run initialization twice, and prove those rows/content remain unchanged.
6. Boot with `FF_CONVERSATIONAL_VOICE` disabled first, verify schema and typed chat, then enable
   capability only after LiveKit/worker/exact-speech readiness.

## Rollback, Recovery, and Retirement

- **Runtime rollback**: disable voice capability, stop admission, end/revoke media sessions, drain
  worker buffers, and leave ordinary accepted tasks/results untouched. Typed chat stays online.
- **Application rollback**: older code ignores additive tables after voice is disabled. Do not lower
  the stored schema revision or run ad-hoc destructive SQL.
- **Migration failure**: startup remains fail-closed; restore the pre-migration PostgreSQL backup or
  repair with a reviewed repeat-safe migration, then restart. Never mark the revision manually.
- **Retirement**: after the retention window and verified zero live sessions/turn correlations, a
  separately reviewed guarded cleanup may archive/delete metadata and drop tables/indexes. It must
  have its own revision, backup, dry-run counts, and recovery procedure.

## Invariants to Test

1. A user can own at most one unended voice session across all replicas/devices.
2. A stale generation cannot publish/submit/control after takeover.
3. The same `(user_id, client_turn_id)` or submission ID can create at most one ordinary message,
   operation, task, and recap.
4. Chat navigation changes only future recognition snapshots.
5. Ending/muting/transferring media never cancels an accepted task.
6. At most one foreground turn exists per session; all older accepted turns stay durable.
7. Terminal commit fences all later progress sequence numbers.
8. At most one live control-connection lease and one announcement claim can act for a generation.
9. Unknown or failed sensitivity classification cannot authorize detailed speech.
10. Consent is one-result, fresh, owner-bound, consumed once, and non-replayable.
11. Storage inspection finds no audio, transcript duplicate, recap content, key, token, or ticket.
12. Two accepted turns in one chat hold simultaneous running execution leases; neither waits on the
    whole-task chat lock. Reverse-order terminal publication preserves both private stages, rebases
    components and layouts from their immutable bases, never reruns a tool, and never erases another
    staged or prior committed version.
13. A rotated media identity cannot start a new recognition binding; a bound pre-refresh turn may
    complete only with its immutable binding, while takeover abandons unaccepted old-generation turns.
14. Future/skewed client wall clocks cannot extend idle expiry or satisfy playout cadence.
15. A same-user client with a different device or UI connection cannot mutate/end/refresh the
    session without explicit takeover; stale control bindings fail closed.
16. A final transcript whose normalized text, digest, HMAC proof, expiry, worker identity, or bound
    IDs differ creates no user message; proof/digest never appear in durable storage.
17. With two active turns, bounded speech quanta and deadline arbitration preserve each turn's
    20-second next-start deadline with an injected positive 250 ms handoff; coincident terminal
    openings are serialized and attributable.
18. A result quantum reserves its exact command ceiling by row-locked CAS before send; reservation,
    command, manifest, lifecycle, and playout role/index/cumulative values match, no quantum exceeds
    96,000 samples, no opening exceeds 36,000, and cumulative reservation never exceeds 720,000.
19. A pre-acceptance rejection is terminal for its exact IDs, clears worker/client buffers, and
    cannot become automatic replay or a hidden capacity queue; explicit retry uses fresh IDs.
20. Deleting the current session chat ends media, while deleting only an older bound chat rejects
    an unaccepted turn and suppresses an accepted turn's later announcements without switching,
    recreating, or ending the newer current-chat session.
21. Publishing a `user_acceptance` over a non-empty canvas/layout carries both forward under that
    acceptance commit/revision before advancing the chat anchor; current reads remain unchanged and
    the linked result's execution-base component/layout digests match those carried-forward rows.
22. Physical deletion succeeds for a current or older bound chat without cascading/restricting the
    retained voice tombstones; current media ends, accepted voice correlations become abandoned
    without cancelling their operations, old accepted speech/publication is suppressed, and every
    replay is rejected without a second dispatch or chat recreation.
