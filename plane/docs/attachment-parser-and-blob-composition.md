# Attachment parser and blob composition

This slice exposes parser state already present in the extracted `066.001` baseline and the
revision-`074.004` attachment-materialization and durable-purge contract:

- `create_attachment_parser_repository()` returns the stateless typed repository cataloged as
  `attachment_parsers`.
- `create_streaming_blob_store(root=..., io_chunk_bytes=..., create_root=True)` returns the
  production resource used by fenced staging, readers, trusted path-only parsers, and purge
  execution without buffering a multi-gigabyte attachment in memory.
- `create_attachment_materialization_coordinator(...)` binds that one configured store to Plane's
  transaction authority and materialization repository.
- `create_durable_purge_executor(...)` binds the same store and database runtime to typed purge
  scheduling and recovery.

Neither factory borrows a database connection, authorizes an administrator, generates or executes
parser code, chooses an upload root, or decides retention policy.

## Parser claims and coverage

The `attachment_parser` table remains a global registry because one administratively approved
parser covers that file-type gap for every user. That does not make its provenance global:

- `get_coverage(...)` selects only extension/category, lifecycle status, live agent/tool binding,
  and the opaque parser/gap identities. It never returns requester, draft, attachment, chat, or
  approver fields.
- owner claim reads and lists include `requested_by = owner_id` in SQL. A driver returning foreign
  provenance fails closed.
- the explicitly named administrative reads expose provenance only after AstralDeep has applied
  its normal administrator authorization and audit policy.

`claim_pending(...)` is one PostgreSQL statement using the legacy unique gap index. A missing gap is
inserted. A `failed` or `discarded` gap is atomically reclaimed with new owner/source provenance and
cleared live/approval fields. A `pending` or `live` gap is unchanged and returns only safe global
coverage when another owner already holds the claim. This fixes the legacy retry path where a
later upload could create a new draft but leave the registry tied to the first failed draft.

Owner failure/discard changes require owner, gap, expected `pending` status, and exact
`updated_at`. Global promotion is an explicitly administrative method and requires the expected
pending status, exact timestamp fence, live agent/tool identities, and approver identity. New
timestamps must advance the prior fence. Callers own the surrounding transaction and audit event.

The table has no separate integer revision in `066.001`; the existing `updated_at` column is the
compatible compare-and-set fence. No schema or migration change is required.

## Physical blobs, parser leases, and purge

The streaming factory requires an absolute root. With `create_root=True`, it may create the
configured root's missing suffix below the nearest existing ancestry that has been proved free of
symlinks, junctions, and Windows reparse points; `create_root=False` requires the operator to
provision that same safe root. Every operation nests a normalized, total-length/segment-bounded key
below a separately validated opaque owner directory. Ordinary owner identifiers remain
alphanumeric-first. The sole leading-underscore exception is the qualification namespace
`__verif__` followed by an alphanumeric-first `[A-Za-z0-9._-]` suffix, with the same 255-character
total bound; near-miss prefixes and arbitrary leading underscores remain invalid. Absolute keys,
separators in identity components, traversal or dot segments, drive syntax, platform-reserved
names, case-fold aliases, links/reparse points, and non-regular targets fail closed.

There is no public direct-write method. A publisher first commits a hidden pending metadata row with
`begin_pending_materialization(...)`. The canonical physical identity is exact: storage key
`{attachment_id}/{filename}` and root-relative locator
`{owner_id}/{attachment_id}/{filename}`. The coordinator then acquires filesystem owner exclusion
before opening its short database transaction, locks the active owner plus exact unexpired
lease/version, and creates one deterministic exclusive staging sentinel and temporary file. Bytes
stream through the returned `BlobStagingSession.write_chunks(...)` or `awrite_chunks(...)` with no
database transaction held. The session enforces the declared byte bound while hashing, flushes and
`fsync`s the unpublished descriptor, and returns only opaque `BlobStagedWrite` evidence.

Publication is a second short coordinator transaction. It re-locks the pending row, revalidates the
owner, attachment, filename, key, locator, lease, version, size, digest, and content type before any
rename, then atomically publishes the staged descriptor and finalizes READY metadata in that same
transaction. Expired-intent recovery locks the same row before soft deletion and deterministic
prefix scheduling, so recovery cannot certify absence ahead of a late publisher. A commit failure
after rename remains a pending intent that bounded recovery can converge; deterministic validation
failure never renames. Pending rows are hidden from every ordinary attachment, blob-metadata, and
message-link read.

The owner reservation is cross-process and retained through staging. A dedicated bounded Plane I/O
lane guarantees async stage progress even if a host's default executor is saturated; asynchronous
owner-lock acquisition polls without parking blocking waiters in that pool. Cancellation observes
the worker and aborts the sentinel/temp on the owning lane. `close()` is idempotent only after all
readers, reservations, and staging capabilities have settled; otherwise it fails with
`blob_store_busy` rather than racing cleanup.

`open_reader(...)`, `iter_chunks(...)`, and `aiter_chunks(...)` expose only bounded owner/key reads;
readers never emit past the open-file snapshot and verify a supplied digest even when a caller
closes immediately after the final-sized read. Synchronous `close()` deliberately drains unread
bytes to finish requested verification. Cancellation or explicit early abandonment of an async
iterator instead closes its descriptor and releases owner exclusion promptly without claiming
digest verification; it never consumes a bounded control worker for an unbounded drain. No root or
generic filesystem path is public. Stable errors distinguish absence, size-limit, integrity,
unsafe-path, cleanup, and deletion failures.

`open_parser_lease(owner_id=..., key=..., max_bytes=..., expected_size_bytes=...,
expected_sha256=...)` is the one narrow capability for trusted product parsers that only accept a
local path. It accepts no arbitrary path, optionally verifies expected size and SHA-256 against the
held descriptor before yielding, holds the owner exclusion lock plus descriptor/directory anchors,
and revalidates containment, regular-file identity, timestamps, and size on entry and exit. POSIX
leases use a descriptor-bound `/proc/self/fd` or `/dev/fd` capability; Windows leases hold
non-delete-sharing handles across every ancestor and deny write sharing on the validated file
descriptor, preventing same-size in-place mutation while the real-path capability is active. The
revocable `BlobParserPath` exists only while the context is active, has no write methods, and must
not be retained. Replacement, deletion, in-place writing, reparse substitution, or exit-validation
failure is visible and fail closed.

Physical deletion is not a public store method. Capability-bound executor mechanics delete an
attachment prefix or retired owner namespace while holding the same cross-process owner exclusion,
lazily traversing anchored post-order without materializing an owner-wide inventory. Unsafe entries
fail closed. The executor performs the matching absence proof before a version-fenced terminal
transition; raw roots and locators remain absent from results and error metadata. Legacy generic
exact-key tombstones are never automatically certified because their publishers lacked this durable
fence. Migration places them in `manual_review`, and the explicit administration method requires an
exact persisted owner/locator digest plus a retained operator-evidence digest. Exact replay succeeds;
different evidence conflicts.

`PostgresPurgeStore.schedule_attachment_prefix(...)` and `schedule_owner_namespace(...)` compose
the pending tombstone with owner-scoped attachment soft deletion in the caller's one transaction.
Plane derives a deterministic scope/owner/object tombstone identity, so a later HTTP or background
retry with a new timestamp returns the first accepted intent instead of forking work. Attachment
intent removes only that attachment's storage-key prefix. Owner intent remains schedulable with
zero metadata rows so account deletion can still remove orphaned bytes. The owner form accepts the
same narrowly reserved canonical `__verif__` namespace as the streaming store.

`DurablePurgeExecutor` accepts that one configured `StreamingBlobStore`; it never constructs or
opens another root. Each attempt uses a short tombstone-load transaction, closes it before physical
I/O, verifies prefix/owner absence, and uses a second short version-fenced transaction to record
completion or a bounded retry. Concurrent executors reconcile the winning terminal transition as
an idempotent replay. `list_ready_for_administration(...)` and executor discovery/reconciliation
return bounded available work. The single-statement
`has_incomplete_for_administration(...)` aggregate remains true for every non-purged tombstone,
including delayed failures and manual review, or any DB-clock-expired live pending materialization.
Readiness therefore cannot miss work while recovery converts an expired row into a tombstone.

Whole-owner scheduling atomically establishes a durable retired-owner admission fence before it
soft-deletes existing attachment metadata. Begin, renewal, staging-open, and publication all reject
that retired owner, so stale processes cannot recreate bytes after terminal owner purge. Plane
exposes the service boundary, but AstralDeep must separately authorize and mount any account-level
retirement trigger; the existence of the Plane method is not product authorization.

The configured blob resource is deliberately outside `RepositoryCatalog`: repositories are
stateless, while the durable root is host configuration and part of the PostgreSQL/blob recovery
unit. AstralDeep should create it once during closed-admission composition, inject the same instance
into attachment materialization and purge execution, and never derive the root from a package or
source path.

## Compatibility and composition

Revision `074.004` adds the pending/READY materialization lifecycle, exact typed purge scope,
retired-owner state, case-fold attachment/owner isolation, recovery indexes, and the guarded
constraints that make these mechanics durable. A clean `074.003` predecessor is preflighted before
any migration write. Canonical legacy live rows become READY; every legacy soft-deleted attachment
gets a deterministic attachment-prefix tombstone; legacy exact-key work becomes manual review.
Noncanonical READY locators or partial namesake lifecycle objects fail before mutation rather than
being silently rewritten.

AstralDeep constructs one streaming store and one materialization coordinator during
closed-admission composition, uses the pending/staged/publish lifecycle for every production
publisher, runs expired-pending scheduling before purge reconciliation, and closes the coordinator
before the store. Upload/content policy, parser execution, administrator gates, account-retirement
authorization, admission control, and audit delivery remain in Deep. A future blob-root relocation
is a separately authorized, quiesced operator procedure; Plane exposes no DB-only locator mutation.
