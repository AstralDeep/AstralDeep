# Conversation extended-state repositories

This schema-neutral slice exposes the remaining conversation-adjacent tables from the extracted
`066.001` baseline. It introduces no migration and keeps every transaction caller-owned.

## Public composition

- `create_chat_step_repository()` returns persistent, already-redacted progress-step lifecycle
  storage over `chat_steps` and its `messages.step_count` cache.
- `create_conversation_file_repository()` returns conversation file-link metadata storage over
  `chat_files`.
- `create_saved_component_repository()` exposes the existing publication-aware
  `CanvasRepository` implementation under the stable saved-component name. It does not create a
  second implementation of `saved_components`.

The same repositories are available from `RepositoryCatalog` as `chat_steps`,
`conversation_files`, and `saved_components`.

## Owner isolation and ordering

Creating a step proves the conversation belongs to `owner_id`. When a turn message is supplied,
the insert additionally proves that message belongs to the same owner and conversation. Its
`step_count` increments in the same caller transaction and only after a genuinely new step insert;
an idempotent replay never increments it twice. Reads carry the stored owner predicate and list
steps by `started_at ASC, id ASC`.

File-link creation uses `INSERT ... SELECT` from an owner-matched `chats` row. Reads and deletion
also carry both owner and conversation identity where applicable. Lists preserve legacy upload
order and add the row ID as a deterministic tie breaker: `uploaded_at ASC NULLS LAST, id ASC`.
The original name and `backend_path` are excluded from record representations; Plane treats the
path as opaque metadata and never joins it to a filesystem root or opens the referenced file.

Saved-component methods inherit the publication-aware selection already used by Plane workspaces:
revision-zero rows are visible only in the legacy scope, while revisioned reads select exactly the
commit and render revision named by the conversation authority row. Component ordering remains
`position`, then creation time and row identity.

## Replay and compare-and-set behavior

`ChatStepRepository.create_step` uses the stable step ID as an idempotency identity. Replays may
observe a later terminal lifecycle state, but the immutable conversation, turn, kind, name,
redacted arguments, truncation flag, and start time must match. `finish_step` accepts only a live to
terminal transition and predicates the update on owner, expected status, missing end time, and a
non-regressing timestamp. A late completion therefore cannot overwrite cancellation or another
terminal result.

The legacy `chat_files` table is an append-only mapping with a database-generated serial ID and no
natural uniqueness constraint. `add_mapping` deliberately preserves those append semantics rather
than claiming concurrency-safe replay that the schema cannot enforce.

Saved-component creation, immutable replay, publication scoping, ordered current reads, and
timestamp compare-and-set replacement are the existing `CanvasRepository` contract. The stable
saved-component repository subclasses that implementation so fixes remain single-sourced.

## Product-owned behavior

PHI redaction, step orphan healing, WebSocket emission, task cancellation policy, upload and parser
policy, blob storage, filesystem path resolution, canvas identity generation, layout behavior,
artifact version cleanup, and UI response shaping remain outside Plane. Potential step text is
expected to be redacted before persistence and is excluded from detached-record representations as
defense in depth.

Rollback requires only restoring the prior Plane/Deep composition. Do not alter conversation rows,
file links, snapshots, or component publication state during code rollback.
