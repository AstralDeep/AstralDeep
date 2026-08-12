# Contract: Atomic Conversation Resume and Generation Fencing

**Scope**: FR-026–FR-032; SC-006, SC-022
**Targets**: web, Windows, Android, iOS, macOS, and existing watch-compatible chat paths.

## 1. Account-scoped resume locator

Each client persists exactly the intentionally active chat identity before it opens/registers a new
UI connection. The storage key is:

```text
astraldeep.active_chat.v1.<lowercase-hex SHA-256(UTF8(issuer) || 0x00 || UTF8(subject))>
```

The value is JSON
`{ "schema_version": 1, "chat_id": "<uuid>", "updated_at": "<RFC3339 UTC>" }`.
It contains no token, credential, transcript, canvas, provider value, deployment URL, or display
name. Browser storage, QSettings, Android preferences, and UserDefaults implement the same logical
contract. A value with an unknown `schema_version` is retained but not used until it can be migrated;
it is not silently interpreted as version 1. Account switch naturally selects another digest key.
The Watch target owns `AstralWatch/ConversationResumeStore.swift`; endpoint override synchronization
is a separate concern and MUST NOT be used as conversation persistence.

The locator is written synchronously before `load_chat`, before a newly-created chat is presented as
active, and before reconnect registration. It is removed only after:

- an explicit new-chat action has durably selected/replaced it;
- definitive sign-out/account removal (not token refresh or transient auth/network failure); or
- a server-confirmed deletion/not-found result for that same owner and chat.

Process recreation, app backgrounding, socket close, service restart, timeout, hydration failure,
and provider failure do not clear it.

## 2. Registration and request generations

The client generates a new UUID4 `connection_generation` for every WebSocket connection attempt.
Its UI registration carries:

```json
{
  "connection_generation": "c38a9565-60bb-4277-a6d5-0eac9f4cf1ef",
  "resume": {
    "schema_version": 1,
    "active_chat_id": "dbe5e456-d9d8-45b9-aebf-ac90925e9c47",
    "request_generation": "ad36ca65-28a3-4b69-b415-474f7a8ae386"
  }
}
```

For client-originated work, `request_generation` is a UUID4 created whenever the client
intentionally loads/resumes a chat or submits a new turn. Its locally tracked/server-echoed purpose
is `hydration` for load/resume and `commit` for a submitted turn; purpose cannot change within a
generation. It is an equality fence, not an ordered counter. The server binds both values to the
authenticated connection, validates ownership of `active_chat_id`, and echoes them on scoped
frames. A client never reuses a request generation for different normalized work.

Detached server work has no client-created turn fence to reuse. A scheduled turn, a detached/REST
mutation, a persisted stream terminal, or a long-running-job result therefore receives a fresh
server-generated UUID4 `request_generation`. A client may open that server-generated generation
only through the exact `conversation_commit_ready` prelude in section 3; an unannounced snapshot
never creates or changes request authority.

For a valid locator, the server builds the snapshot before sending any welcome canvas. Invalid
ownership uses existing non-disclosing not-found behavior; a transient read/render failure emits a
retryable operation result and leaves the locator/current committed UI untouched.

## 3. `conversation_commit_ready` prelude

This server→client push opens one commit-purpose fence for a server-originated logical update:

```json
{
  "type": "conversation_commit_ready",
  "schema_version": 1,
  "chat_id": "dbe5e456-d9d8-45b9-aebf-ac90925e9c47",
  "connection_generation": "c38a9565-60bb-4277-a6d5-0eac9f4cf1ef",
  "request_generation": "e0ba50da-8344-48b1-b86a-80abf7c272d0",
  "render_revision": 28
}
```

Those six top-level fields are exact: no field may be missing and no additional field is accepted.
`schema_version` is the integer `1`; all three identities are canonical UUID4 strings; and
`render_revision` is the positive target revision of the already durable logical commit. The
prelude is valid only for the intentionally active `chat_id`, the current registered
`connection_generation`, a fresh `request_generation`, and a revision greater than the client's
last committed revision. Malformed, unknown-version, foreign-chat, old-connection, non-fresh, or
stale/equal-revision preludes are logged no-ops and do not change the locator, request fence,
transcript, canvas, or revision.

A valid prelude immediately precedes exactly one `conversation_snapshot` with
`snapshot_purpose="commit"` and the same chat, connection, request generation, and target render
revision. It never opens a hydration generation and never authorizes transient frames from another
generation. If a client-originated commit generation is still unfinished, the client logs
`commit_request_busy` and refuses to let the prelude steal that fence; the unaccepted paired
snapshot is consequently a scoped no-op, and the already durable server update is recovered by the
next normal hydration. A transport loss between the pair has the same recovery behavior.

## 4. `conversation_snapshot` frame

This server→client push is the only authoritative hydration or commit publication:

```json
{
  "type": "conversation_snapshot",
  "schema_version": 1,
  "snapshot_id": "fb6b25f3-ae88-4010-846e-a8fc86257162",
  "chat_id": "dbe5e456-d9d8-45b9-aebf-ac90925e9c47",
  "connection_generation": "c38a9565-60bb-4277-a6d5-0eac9f4cf1ef",
  "request_generation": "ad36ca65-28a3-4b69-b415-474f7a8ae386",
  "snapshot_purpose": "hydration",
  "render_revision": 27,
  "committed_at": "2026-07-15T18:41:00Z",
  "transcript": [
    {
      "message_id": "1842",
      "role": "assistant",
      "created_at": "2026-07-15T18:40:59Z",
      "parts": [{"type": "text", "text": "The result is 21."}],
      "attachments": []
    }
  ],
  "canvas": {
    "target": "canvas",
    "components": []
  }
}
```

Required top-level fields are exactly those shown; there is one canonical snapshot frame, not a
transcript frame followed by a canvas frame. `schema_version` is the integer `1`. `snapshot_id`,
both generations, and `chat_id` are UUID strings; `render_revision` is a non-negative 64-bit integer
that identifies one complete logical conversation commit. `snapshot_purpose` is exactly `hydration`
or `commit` and must agree with the registered generation purpose. `canvas` is always an object with exactly
`target: "canvas"` and `components`; it is never the bare component array. `canvas.components` is
the complete ROTE-adapted component array for this socket. An explicit empty array means the
committed canvas is empty; absence/null is not a valid snapshot.

The browser deliberately has no independent primitive renderer. Therefore, after ROTE adaptation
and only for a web-profile socket, the server augments every top-level component with this exact
reserved presentation member:

```json
"_presentation": {
  "target": "web",
  "html": "<server-rendered top-level component fragment>",
  "workspace": {"export": false, "share": false}
}
```

`html` is the canonical escape-by-default server renderer's complete top-level fragment, including
its component-identity wrapper. `workspace` repeats the effective full-workspace export/share flags
and must agree on every component. The browser validates all semantic components and all reserved
presentation members before changing either transcript or DOM, then concatenates those trusted
fragments inside the standard workspace root and applies the flags in the same atomic reducer
action. Missing, malformed, mixed-target, or inconsistent presentation retains the prior committed
view. The explicit empty component array needs no presentation member and clears the canvas.
Non-web sockets receive no `_presentation` member and continue rendering the adapted semantic
components natively. Arbitrary component `html` or `rendered_html` fields are never presentation
authority. This reserved envelope is transport presentation only: it is not stored in the durable
canonical workspace, included in semantic component comparisons, or accepted from a client.

Each logical update publishes through a durable `conversation_commit` boundary. This includes
direct chat turns, component mutations, scheduled turns, persisted stream terminals, detached/REST
updates, and long-running-job results. The message changes, complete canonical canvas changes,
incremented per-chat `render_revision`, and commit timestamp are written in one database
transaction. If legacy processing must prepare transcript and canvas in separate steps, those steps
remain staged/non-authoritative until that transaction marks their shared revision committed.
Snapshot construction reads only the last complete `conversation_commit` under one repeatable view;
it never combines newer messages with an older canvas or exposes a partially prepared revision.
A scheduled turn uses its explicit owner-validated target chat when present; otherwise its scheduled
job UUID4 is the stable fallback `chat_id` across attempts and recovery.

After a logical-update transaction commits, the server emits exactly one complete
`conversation_snapshot` with `snapshot_purpose="commit"` for the new revision. Initial/reconnect hydration instead emits the
current complete committed revision with `snapshot_purpose="hydration"`; it does not invent or
increment a commit. No other frame may advance committed transcript/canvas or the client's last
committed render revision.

The transcript and canvas are therefore read from one coherent committed server view. If either
cannot be produced, the server sends no partial snapshot. Clients validate `schema_version`, every
field, the full transcript, and the full canvas candidate off the render thread while keeping the
prior committed transcript and canvas visible (optionally with a non-destructive loading overlay),
then commit both plus `render_revision` in one reducer/main-actor action. No intermediate
transcript-only, blank-canvas, or welcome state is observable.

## 5. Canonical semantic transcript

Each transcript message has `message_id` (string), `role` (`user`, `assistant`, `system`, or `tool`),
RFC3339 `created_at`, non-empty `parts`, and `attachments` (possibly empty). Parts are:

```json
{"type":"text","text":"visible text"}
```

A `text` part MAY additionally carry `"variant"` drawn from the closed set `{"caption"}` (feature 066
T023 contract extension, 2026-08-05): a text primitive lifted into the rail keeps caption weight
through commit and hydration. Any other value — or any other extra key — remains invalid. Validators:
backend `shared/protocol.py` (`CANONICAL_TEXT_PART_VARIANTS`), web `client.js`
`validateSnapshotShape`, Windows `astral_client/protocol.py`, Android `Wire.kt`, Apple
`ConversationContinuity.swift`. Clients without a caption style render it as plain text.

```json
{"type":"components","components":[{"type":"text","content":"visible component"}]}
```

```json
{"type":"structured","value":{"total":21,"rolls":[6,6,4,3,2]},"plain_text":"total: 21; rolls: 6, 6, 4, 3, 2"}
```

```json
{"type":"recovery","code":"saved_content_unrenderable","message":"A saved response could not be displayed."}
```

A `structured` part's `plain_text` MUST be non-blank — a string with at least one non-whitespace
character. It is the part's only visible rendition, and the Apple validator
(`ConversationContinuity.swift`, `continuityNonBlank`, which trims first) rejects a blank one; because
snapshot decoding is all-or-nothing, a single blank part discards the whole `conversation_snapshot`
and that chat's rail never hydrates. The server therefore holds the strictest client's rule at both
ends: `shared/protocol.py` refuses a blank rendition, and `orchestrator/history.py` never emits one
(see the normalization rules below). Web, Windows and Android accept a blank `plain_text` as shipped;
that asymmetry is deliberate — emission is strict, acceptance stays as released, so no client needs a
change.

Server normalization is deterministic:

- stored string → one `text` part, preserving Unicode and ordering;
- stored list of strings/primitives → ordered `text`/`structured` parts;
- stored astralprims component dictionaries/list → one ordered `components` part, validated through
  the existing renderer/ROTE vocabulary;
- other valid JSON object/array → `structured` with the value plus deterministic human-readable
  `plain_text` (stable key/order rules shared by clients);
- null, malformed stored JSON, a component that cannot be safely normalized, or a value whose
  `plain_text` rendition would be blank (an empty/whitespace-only string, or a nested array that
  reduces to one) → a visible `recovery` part and a structured diagnostic, never an empty/omitted
  turn.

Clients render `text`; render `components` through their existing shared primitive renderer; render
`structured.plain_text` while retaining its semantic value for tests; and visibly render `recovery`.
They must not serialize dictionaries with language-specific debug syntax. Message and part order,
roles, text, labels, values, attachments, and supported interactions must remain semantically
equivalent across clients.

## 6. Scoped live-frame requirements

`conversation_snapshot` is the sole committed-state publication. A client accepts it only when the
chat/connection/request generations match a fence that the client opened for its own load/turn or
opened from a valid exact `conversation_commit_ready` prelude, and applies revision rules as
follows:

- greater than `last_committed_render_revision`: validate the entire frame and atomically replace
  transcript plus canvas;
- equal revision on the first complete `snapshot_purpose="hydration"` frame for a new
  `(connection_generation, request_generation)` explicitly opened for hydration: validate and
  atomically replace transcript plus canvas, retain the fresh `snapshot_id`, and mark that generation
  hydrated;
- equal revision with `snapshot_purpose="commit"`, or for a normal new-turn generation: log
  `unexpected_equal_commit` and no-op;
- equal revision after that generation is hydrated and the same accepted `snapshot_id`: idempotent
  replay/no-op;
- equal revision after hydration with a different snapshot identity or content: log
  `revision_conflict` and no-op;
- lower revision: log `stale_frame_ignored` and no-op.

Existing `ui_render`, `ui_update`, `ui_upsert`, `ui_append`, and `ui_stream_data` are disposable
request-scoped preview overlays after 060. They carry:

```json
{
  "chat_id": "<uuid>",
  "connection_generation": "<uuid>",
  "request_generation": "<uuid>",
  "base_render_revision": 27,
  "frame_sequence": 4
}
```

The base must equal the current committed revision and `frame_sequence` must strictly increase for
that chat/connection/request tuple. These frames may update only the transient overlay; they never
mutate committed transcript/canvas or advance the committed revision. `chat_status`, `chat_step`,
`user_message_acked`, `task_started`, `task_completed`, `tool_progress`, and `operation_status` may
update pending/status overlays only. A committed snapshot or terminal failure clears the transient
overlay. Global bootstrap/chrome/theme frames are not chat scoped.

Every scoped frame still requires the intentionally active chat and current connection/request
generations. Failure of any identity/base/sequence check is a logged no-op. Generations are never
inferred from arrival order. Switching chats first persists the new locator and generation, so
delayed old-chat output is fenced before it arrives.

## 7. Welcome, deletion, and recovery semantics

- Welcome is valid only when registration has no locator, explicit new chat selected an empty chat,
  or the server definitively confirms the located chat was deleted/not found for its owner.
- A snapshot with an empty transcript/canvas is still a successful resume and is not replaced by a
  generic welcome unless that empty chat's server-owned experience calls for it.
- On confirmed deletion, the client clears that locator, cancels the matching request generation,
  and then shows the shared server-owned no-active-chat experience.
- On snapshot timeout/failure, keep old committed content, show retryable status, and retry the same
  chat with a new request generation. Never silently start a different chat.
- Snapshot commit must complete within five seconds in the release continuity trials.

## 8. Compatibility and manifest

`conversation_commit_ready` and `conversation_snapshot` are added to
`backend/shared/ui_protocol.json` and every client disposition table in the same change.
Connection/request generations plus transient `base_render_revision` and `frame_sequence` are
recorded as additive fields for the applicable live frames. Legacy
`chat_loaded` plus trailing `ui_render` may remain during a bounded compatibility
period, but 060 clients do not treat that pair as resume completion and the server emits the atomic
snapshot to them.

## 9. Required contract tests

- Twenty consecutive network-loss, backend-restart, browser reload, Windows/macOS restart, and
  Android/iOS process-recreation trials per supported client: correct coherent state within five
  seconds and no unintended welcome.
- Reordered, duplicated, missing, different-chat, old-connection, old-request, and old-render frames
  cannot alter committed content.
- Multiple reordered/equal-revision transient frames may affect only their overlay; exactly one full
  snapshot advances each committed revision, and a conflicting same-revision snapshot is rejected.
- Exact, missing-field, extra-field, malformed, foreign-chat, old-connection, stale-revision, and
  busy-client-commit `conversation_commit_ready` cases prove that only one valid fresh prelude opens
  its paired server-generated commit fence; the paired scheduled/detached/stream/long-job snapshot
  is the only committed-state update.
- Direct turns, component mutations, scheduled turns, persisted stream terminals, detached/REST
  mutations, and long-running-job results each publish one complete logical commit or leave the
  prior revision authoritative; no path exposes a message-only or canvas-only update.
- Every stored content form above, including empty arrays, nested structured content, malformed
  content, Unicode, attachments, and component-only assistant turns, is visibly nonblank and
  semantically equivalent across clients.
- Snapshot transcript failure and canvas failure each retain both old surfaces; a later full retry
  replaces both atomically.
- Fault injection between message preparation, canvas preparation, and `conversation_commit`
  publication exposes only the prior complete revision or the next complete revision, never a
  mixed/partial turn.
- Locator persists before registration, survives transient failure/process death, separates two
  accounts, rejects unknown schema versions safely, and clears only for the three explicit
  conditions.
