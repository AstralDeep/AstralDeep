# Research: Canvas-First Adaptive UI/UX (066)

Decisions from the 2026-08-03 live audit (main@253378d, local stack, Chrome).
Each entry: Decision / Rationale / Alternatives considered.

## R1 — Layout modes are client-owned; ROTE stays component-owned
**Decision**: Three shell arrangements (`stacked` <700px, `collapsed` 700–1023
default, `split` ≥1024 default) stamped on `body[data-astral-layout]` by
client.js, with a persisted per-device user override; ROTE keeps owning
COMPONENT adaptation via the (now live) capability envelope.
**Rationale**: Mirrors the Android division of labor (windowSizeClass owns
arrangement, ROTE owns content) already documented in client.js; keeps the
server render path unchanged per Constitution II.
**Alternatives**: server-driven arrangement frames (rejected: new wire
vocabulary, Constitution XII friction, slower resize response); CSS-only
container queries (rejected: user persistence + unread logic need JS anyway).

## R2 — Collapsed mode reuses the aside as a floating composer
**Decision**: In collapsed mode the existing `<aside>` becomes the floating
bottom composer (fixed, centered, max 760px) and the transcript opens inside
it as a drawer; no DOM re-parenting.
**Rationale**: Zero JS layout surgery — pure CSS repositioning keeps every
existing element id/behavior (voice host, attachments, slash menu) intact.
**Alternatives**: moving the form node between containers (rejected: breaks
listeners and focus, high regression risk).

## R3 — Live envelope rides a ui_event action, not a new frame type
**Decision**: `capability_update` is a C→S `ui_event` action carrying the same
`device` dict as `register_ui`; server refreshes the socket's ROTE profile via
the existing `rote.register_device` and re-pushes `rote_config`.
**Rationale**: `ui_protocol.json` freezes FRAME types (Constitution XII);
ui_event actions are the sanctioned extension point (038/040 precedent).
Reuses the registration profile path, so no second adaptation code path.
**Alternatives**: reconnect-on-resize (rejected: drops streams, races
turns); new `viewport_update` frame (rejected: manifest + native drift).

## R4 — Registration ack = the `rote_config` verdict
**Decision**: The client treats the post-registration `rote_config` frame as
"socket healthy": flips the connection pill off and flushes the queued sends.
**Rationale**: It is the first server frame guaranteed on every successful
registration path (verified in logs), needs no new wire shape.
**Alternatives**: a dedicated register_ack frame (manifest change).

## R5 — Send queue is bounded, memory-only, per-tab
**Decision**: Max 5 queued actions, 45s TTL, visible queued bubble for chat
sends, loud refusal restoring the text on expiry; chrome actions queue through
the same gate (FR-015).
**Rationale**: Matches the audit failures (3 silently dropped saves; a
pre-registration send eaten). Durable queues invite duplicate-turn semantics
the operation-admission layer would then need to arbitrate — out of scope.

## R6 — Failed turns materialize the transient overlay
**Decision**: On a terminal failed/retryable operation, the transient
overlay's chat bubbles move into the canonical rail, an inline error card with
Retry (exact text retained on the local submission record) is appended, and
only then is the overlay cleared. `cancelled` keeps today's quiet behavior.
**Rationale**: The 060 snapshot machinery clears the overlay wholesale on
terminal — correct for canvas staging, catastrophic for the user's own words.
The fix respects the overlay lifecycle (no partial-frame leakage) while
keeping human-visible content.

## R7 — Voice affordance is client-default-first
**Decision**: The shell pre-renders a disabled voice-start control; client.js
re-renders the same default on socket teardown; `composer_state` frames refine
but can never leave the host empty. Real SVG icons keyed by the existing
`data-icon` contract (CSS text stubs removed).
**Rationale**: `publish_voice_composer_state` returns None on any projection
error → empty host → `:empty{display:none}` hid voice entirely (the
owner-reported bug). The server model already renders start-disabled when
unavailable; the default only covers frame absence.

## R8 — Keyless endpoints strip Authorization via a shared httpx hook
**Decision**: `openai_auth_kwargs()` returns a real key pass-through or the
sentinel api_key + a shared httpx client whose request hook removes the
Authorization header. `custom` preset becomes key-optional; the probe-gated
save remains the honesty gate.
**Rationale**: Verified live: the factory 403s any wrong bearer but accepts a
missing one; SDK 2.52 cannot omit the header via api_key="" (illegal
trailing-space header) or client-level Omit (per-request-only validation).
**Alternatives**: per-request extra_headers omit at every call site
(rejected: N call sites, unenforceable); SDK fork/monkeypatch (rejected).

## R9 — bind_chat at the single None→chat transition
**Decision**: `WorkAdmission.bind_chat(fence, chat_id)` (coordinator +
protocol + both repositories, fence-checked, refuses non-null mismatch,
idempotent re-bind) called from the chat_message branch right after chat
creation; every downstream fence keeps strict equality.
**Rationale**: Operations for a first message admit before the conversation
exists (chat_id NULL); five downstream fences compare strictly; loosening
each would erode the cross-conversation guarantee. One durable adoption at
the only legitimate transition preserves strictness everywhere.
**Alternatives**: fence tolerance for None (rejected: weakens history.py
stage_commit which re-reads the DB row inside the transaction); pre-creating
chats client-side (rejected: native parity + offline welcome flows).

## R10 — Voice readiness is a first-class surface
**Decision**: An authed `GET /api/voice/status` snapshot (admitted workers,
last admission refusal + reason, last preflight verdict) plus worker-side
bounded preflight re-checks and composer-visible refusal reasons.
**Rationale**: The sandbox 503 required correlating two containers' raw logs;
the worker's own log was empty pre-admission. Every acceptance drill in US9
needs one place to look.

## R11 — Markdown-in-rail root cause is the words-only snapshot path
**Decision (deferred sub-item)**: The narrative STREAM frames already render
`variant="markdown"`; the literal asterisks come from the committed words-only
rail snapshot (062) used for the terminal replacement and transcript
rehydration. Fix belongs in that snapshot's rail rendering; client-side
re-parsing of server HTML was rejected (double-render risk). Tracked as the
open portion of T022.
