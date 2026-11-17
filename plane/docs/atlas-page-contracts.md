# Atlas page contracts

Schema `089.006` adds the bounded Atlas page store behind `create_atlas_page_repository()` and the
`atlas_pages` catalog member. Schema `089.007` adds immutable page revisions and fenced edits on the
same repository, and schema `089.008` adds same-owner link and backlink storage described in
[Atlas link contracts](atlas-link-contracts.md). Plane stores opaque owner- and project-scoped page
heads, exact create/edit/delete command receipts, bounded revision history, and terminal tombstones.
It does not parse or encrypt content, authorize a caller, audit a command, or expose a network
route.

## Page identity and content

A page is identified by `(owner_id, page_id)`. `page_id` is a host-supplied canonical UUID and
remains stable for the lifetime of that page row. `project_id` is an optional exact scope; `None`
is the global project and is distinct from every non-empty project. A live slug is unique within
one owner and one project. The same slug, page identity, and content may exist independently under
another owner or project.

The live row carries a host-sealed `content` byte string of 1 through 16,384 bytes, its SHA-256
digest, up to eight unique source references, the current `revision` (initially `1`), and
caller-supplied millisecond timestamps from 0 through 9,007,199,254,740,991 (the safe 53-bit integer
maximum). All commands, detached records, receipts, revisions, and cursors enforce the same
timestamp range before SQL.

Each source reference contains 1 through 512 Unicode characters, with no edge whitespace, surrogate
code points, or control characters other than interior tab/newline. The eight references are unique.
PostgreSQL bounds the UTF-8 bytes of `sources::text` at 16,416: eight strings of 512 four-byte
characters, 16 quote bytes, 14 comma/space bytes, and two bracket bytes. JSON escaping of quotes,
backslashes, tabs, and newlines uses two bytes per character, so it cannot exceed that bound. Real
PostgreSQL tests reach the exact maximum with distinct supplementary-plane strings and exercise all
escaped forms.

Plane never receives a plaintext page body or an encryption key and never interprets the ciphertext.
Reads verify that the stored digest still matches the stored bytes and fail with
`RepositoryDataError` instead of returning damaged content. `titles`, rendering, source validation
policy, PHI checks, encryption, authorization, and audit remain with the embedding host.

## Lifecycle, revisions, and replay

`AtlasPageCreateCommand` binds the owner, canonical command UUID, page identity, project, slug,
sources, ciphertext, and creation time. Its request digest covers every immutable input. Creation
writes revision `1` and the live head in the caller transaction. Replaying the same owner/command
with the same digest returns the original page and receipt with `replayed=True` and performs no new
write. Reusing a command UUID with changed semantics, an existing page identity, or a live slug
conflict fails with `RepositoryConflictError`.

`AtlasPageEditCommand` binds the owner, command UUID, page identity, `expected_revision`, sources,
replacement ciphertext, and edit time. `edit_page()` and its `append_revision()` alias insert the
exact next row into `atlas_page_revision` before advancing the live head under the owner lock and
the `expected_revision` predicate in one transaction. The head update is accepted only when its
revision, sources, ciphertext, digest, and edit time exactly match that immutable row. The command
receipt stores the expected and result revisions. Replaying the exact command later returns the
original revision record with `replayed=True` even when the head has advanced, without changing the
head. A changed digest, reused command identity, stale expected revision, or regressive edit clock
fails with `RepositoryConflictError` before a new write.

`AtlasPageDeleteCommand` carries the owner, command UUID, page identity, `expected_revision`, and
deletion time. The update validates the owner, live state, revision fence, and monotonic deletion
time in one statement. It advances the head revision by one, nulls head ciphertext and its digest,
clears source metadata, and writes the tombstone time. Immutable content revisions remain available
only through explicit `include_deleted=True` recovery reads; deletion never rewrites them. Repeating
the same delete command returns the same tombstone without restoring content. A different command
cannot delete an existing tombstone, and a stale revision cannot delete a changed page. PostgreSQL
triggers refuse any attempt to update a tombstone, restore head content, mutate a revision or
command receipt, or move a live head without an exact backing revision.

A tombstone permanently occupies its page identity; recreating that page UUID is refused and no
edit command, including replay of an earlier edit, can run against the deleted page. A new page UUID
may reuse a slug after the prior page is deleted, while the old identity remains a content-free
head. Reads and listings exclude tombstones unless `get_page(include_deleted=True)` or a revision
read explicitly requests them. Ordinary reads, listings, and writes put the owner predicate in the
SQL statement; there is no cross-owner administrative page surface.

## Bounded reads and pagination

`get_page()` reads one owner-scoped identity. `get_page_by_slug()` reads one live owner/project/slug
identity. `list_pages()` reads at most 100 live pages ordered by `(created_at DESC, page_id DESC)`
and returns `AtlasPageBatch`. The cursor carries the exact owner, project, creation time, and page
identity; a cursor from another owner or project is refused before SQL. The query fetches one extra
row to decide whether a continuation exists, so an exact-capacity page has no false continuation.
Keyset iteration is not a snapshot: a later insert may appear on a fresh first page, and a deleted
page disappears.

`get_revision()` reads one exact immutable revision for a live page, or for a deleted page only with
`include_deleted=True`. `list_revisions()` reads at most 100 revisions ordered by revision descending
and returns `AtlasPageRevisionBatch`. Its `AtlasPageRevisionCursor` carries the exact owner, page,
and revision; a cursor from another scope is refused before SQL. A deleted page yields no ordinary
history. `get_page_revision()` and `list_page_revisions()` are equivalent explicit aliases.

All methods accept an explicit caller-owned transaction or query executor. They never commit,
roll back, borrow a connection, retain a cursor, or mutate a returned record. The owner-scoped
advisory lock for a mutation is transaction-local, so replay, edit fences, and identity races
serialize without creating a second database or service.

## Migration and recovery

The `089.005 -> 089.006` edge attests the exact predecessor registry digest
`98a9082bb3317192cd6e48df3b63c76dbb9404814cb4a0180eb997f8737e37b0` and complete catalog before
creating only `atlas_page` and `atlas_page_command`. It does not rewrite any existing table or row.
A repeat run verifies the complete current catalog and is a no-op. Partial DDL or a stamp failure
rolls the edge back with its registry metadata; a same-name foreign object or damaged predecessor
fails before adoption.

The `089.006 -> 089.007` edge attests the exact predecessor registry digest
`1176ee5a65f74e0447abe2e63bb90808acc40289acd1a6a09ec6a10ae1a967b1` and the `089.006` catalog digest
`88896cd0498562ecd0b4d610c4453f7ed01f527e0845cacf7300d88374578e48`. It creates
`atlas_page_revision`, its history index and immutable trigger, the exact-head verification trigger,
and the edit command columns and constraints. It backfills every live page head into an exact
revision `1` row and does not invent content for deleted pages whose ciphertext was already erased.
It removes only the predecessor's one-command-per-page uniqueness so several edits may append
separate receipts. A repeat run verifies the complete current catalog and is a no-op. Interrupted
DDL, a same-name object, or a damaged predecessor rolls back or fails before adoption.

Follow the closed-admission backup, guarded upgrade, populated repeat, corruption, and rollback
checks in [migration and recovery](migration-and-recovery.md). Once the edge commits, keep every
Atlas table and the exact `089.007` revision/digest intact. Prefer a reviewed forward repair. If the
prior composition must be restored, restore the verified PostgreSQL backup and every paired durable
root together under closed admission, then retire restored sessions and reconcile uncertain
effects before reopening. Never restore a tombstone to a live page, rewrite revision/digest markers,
or execute ad-hoc downgrade SQL. Restoring an older database snapshot does not restore content that
was never placed in Plane or that was erased from a deleted head.