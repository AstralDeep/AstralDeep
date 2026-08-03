# Data Model: Canvas-First Adaptive UI/UX (066)

## Schema delta

**None.** `SCHEMA_REVISION` is untouched. The one durable mutation this
feature adds is an UPDATE on an existing column:

- `operation_record.chat_id` — `bind_chat` performs the single legitimate
  `NULL → <created chat>` transition for an operation admitted before its
  conversation existed (fence-checked: `state='running'`, current
  `execution_generation` + `execution_lease_token`, `chat_id IS NULL`).
  `state_revision` increments; a non-null mismatch refuses; re-binding the
  same chat is an idempotent no-op. Rollback: none needed — rows created by
  the old code simply keep `chat_id NULL` and fail publication exactly as
  before the fix.

## Client-local state (web, not persisted server-side)

- **Layout preference** — `localStorage["astral-chat-pref"]`:
  `"open" | "closed"` (absent = auto). Read at layout computation; wins over
  the width default at ≥700px.
- **Layout mode** — `body[data-astral-layout]`: `stacked | collapsed | split`
  (derived, never stored).
- **Drawer / unread state** — `body.astral-chat-open`, unread counter (memory
  only; badge caps at "9+"; cleared on reveal or split mode).
- **Send queue** — bounded array (5) of `{label, dispatch, onRefused, at,
  timer}`; 45s TTL; memory-only by design (R5).
- **Connection state** — `#astral-conn[data-conn]`:
  `connecting | offline` (pill hidden when healthy); `socketReady` flips true
  on the post-registration `rote_config` frame.
- **Failed-turn retry** — the local operation submission record retains
  `message` (chat_message only) so the inline Retry re-sends exact content.

## Wire (existing frames only — Constitution XII intact)

- **`ui_event action="capability_update"`** (C→S, new ACTION, read-only
  admission class): `payload.device` = the same envelope dict as
  `register_ui.device`, now including additive `reduced_motion: bool` and
  `pointer_type: "fine"|"coarse"`.
- **`rote_config`** (S→C, unchanged shape) — now also re-emitted after a
  capability refresh.
- **`composer_state`** (S→C, unchanged) — the client renders a local default
  voice-start control before/without it; frames refine but never remove.

## Server-side model additions

- `rote.capabilities.DeviceCapabilities` + `reduced_motion`, `pointer_type`
  (additive dataclass fields, safe defaults; `from_dict` already filters
  unknown keys so old/new clients interoperate).
- `work_admission.WorkAdmissionRepository.bind_chat(fence, chat_id, *, now)`
  — protocol + InMemory + Postgres implementations, mirroring
  `update_phase` semantics.
- Voice readiness snapshot (P3, planned): admitted workers, last admission
  refusal + reason, last preflight verdict — served by an authed
  `GET /api/voice/status`; no persistence (in-memory pool observation).
