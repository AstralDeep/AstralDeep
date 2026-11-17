# Observation recall contracts

Schema `089.012` adds the bounded tool-observation recall store behind
`create_observation_repository()` and the `observations` catalog member. The repository stores
opaque owner- and conversation-scoped observation heads, their bounded ciphertext pages, absolute
expiry, and terminal purge tombstones. It does not encrypt or decrypt content, authorize a caller,
resolve an audience, audit a recall, pack observations, summarize or compact context, choose a
provider, or interpret any stored byte.

## Observation identity and content

An observation is identified by `(owner_id, conversation_id, observation_id)`. `observation_id` is
a host-supplied canonical UUID and remains stable for the lifetime of that row. `conversation_id`
scopes recall inside one owner; the same content may exist independently under another owner,
another conversation, or another observation identity, and repeated content never merges
identities. `capture_seq` is a dense per-conversation capture order assigned under the owner
advisory lock, so listings and evidence ordering are deterministic.

The live head carries a host-sealed `seal` of 1 through 512 characters, up to eight unique source
references, the originating `operation_id`, the page extent (`page_count` and
`observation_octets`), the whole-observation SHA-256 digest, and the `capture_digest` that fences
capture replay. Times are caller-supplied integer seconds from 0 through 9,007,199,254,740,991
(the safe 53-bit integer maximum); every command, detached head, page, cursor, and usage record
enforces the same range before SQL.

Ciphertext is split into pages of at most 16,384 bytes, each stored with its own SHA-256 digest in
`tool_observation_page`. One observation holds at most 512 pages and 8,388,608 octets. Each source
reference contains 1 through 512 Unicode characters with no edge whitespace, surrogate code points,
or control characters. PostgreSQL bounds the UTF-8 bytes of `source_refs::text` at 16,416 using the
same accounting as the Atlas page store. Plane never receives plaintext observation text or an
encryption key and never interprets the ciphertext; `seal` is opaque host metadata for its
envelope. Redaction, normalization, retention policy, PHI and outbound-content checks, delegated
authority, revocation, and audit remain with the embedding host.

## Absolute retention and bounded capacity

`ObservationCaptureCommand` requires `expires_at` with
`created_at < expires_at <= created_at + 86400`, so a captured observation carries an absolute
expiry no later than 24 hours after capture; earlier source, conversation, or policy deadlines are
the host's obligation and are expressed by passing an earlier `expires_at`. Expiry is immutable:
no recall, restart, recovery, or capture replay extends it, and the durable retention window is
enforced by a table constraint. Recall, verification, and listing treat an observation with
`expires_at <= now` as unavailable and return `None` without disclosing content, extent, or
existence. The same closed outcome covers another owner's reference, a purged observation, and an
unknown identity.

One conversation may retain at most 67,108,864 octets across unpurged observations. A capture that
would exceed the capacity fails with `RepositoryConflictError` and evicts nothing; the host
surfaces the limit and may purge or expire observations through the same repository.
`conversation_usage()` reports the live count, retained octets, expired count, and capacity for
host-side policy.

## Paged recall and integrity

`read_page()` returns one bounded `ObservationPage` for a live, unexpired observation. Page
numbers are 1-based; a valid page carries its location, the whole-observation integrity identity,
its bytes, the total extent, and a `continuation` flag. Requesting page `page_count + 1` returns
the explicit end-of-source state (empty content, no continuation); anything beyond that is a typed
`RepositoryValidationError`, and zero or negative page numbers are refused before SQL. Reassembling
every returned page reproduces the captured ciphertext exactly, without omission or duplication.
The per-page digest is verified against the stored bytes before any page is returned, so damaged
pages fail closed with `RepositoryDataError`.

`verify_observation()` materializes every page, checks each page digest, the page count, the total
extent, and the whole-observation digest, and returns the detached head only when all agree. It is
the deterministic corruption oracle for hosts that need a full integrity check before trusting a
recalled source; `read_page` alone is bounded and verifies only the page it returns.

## Lifecycle, replay, and purge

Capture replays exactly: repeating the same command returns the stored head with `replayed=True`
and performs no new write, and a purged observation replays as its tombstone without restoring
content. Reusing an observation identity with a different request digest fails with
`RepositoryConflictError`. Capture order is assigned inside the caller's transaction under an
owner-scoped advisory lock, so concurrent captures of the same identity admit exactly one writer
and concurrent captures of distinct identities receive distinct dense sequence numbers.

`purge_observation()` tombstones one observation for a bounded reason (`deleted`, `revoked`, or
`expired`) at a caller time that never regresses before capture. The update carries the
`purged_at IS NULL` fence in one statement, nulls the seal, digest, and extent, clears source
references, and deletes every content page. Repeating the same purge returns the same tombstone
with `replayed=True`; a different reason is refused. PostgreSQL triggers refuse any attempt to
update a tombstone, restore content, mutate a page, delete a page outside a purge, or delete an
observation row, so tombstones are durable evidence and a failed purge leaves the live row visible
and retryable. `purge_expired()` sweeps at most 100 expired observations of one conversation in
capture order and returns their receipts.

`get_observation()` reads one owner-scoped head, optionally including tombstones;
`list_observations()` reads at most 100 live, unexpired observations ordered by `capture_seq` with
a keyset cursor bound to its owner and conversation. All methods accept an explicit caller-owned
transaction or query executor. They never commit, roll back, borrow a connection, retain a cursor,
or mutate a returned record.

## Migration and recovery

The `089.011 -> 089.012` edge attests the exact predecessor registry digest
`8677b4e37ea98a7471ca97a82bc0182aed0f68002cace976ecfaacf8487d04fc` and complete catalog before
creating only `tool_observation` and `tool_observation_page`. It does not rewrite any existing
table or row. A repeat run verifies the complete current catalog and is a no-op. Partial DDL or a
stamp failure rolls the edge back with its registry metadata; a same-name foreign object or damaged
predecessor fails before adoption.

Follow the closed-admission backup, guarded upgrade, populated repeat, corruption, and rollback
checks in [migration and recovery](migration-and-recovery.md). Once the edge commits, keep both
tables and the exact `089.012` revision/digest intact. Prefer a reviewed forward repair. If the
prior composition must be restored, restore the verified PostgreSQL backup and every paired durable
root together under closed admission, then retire restored sessions and reconcile uncertain
effects before reopening. Never drop the tables, rewrite revision markers, or execute ad-hoc
downgrade SQL to simulate a rollback. Restoring an older database snapshot does not restore content
that was never placed in Plane, and an expired or purged observation stays unavailable after
recovery.
