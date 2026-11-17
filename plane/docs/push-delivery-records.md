# Push destination and claimed delivery records

Schema `089.011` adds opaque device destinations and a bounded push-delivery outbox behind
`create_push_delivery_repository()` and the `push_deliveries` catalog member. Plane stores only
host-encrypted tokens and host-sealed notification payload snapshots. It never decrypts a token,
calls a push provider, chooses a device or route, evaluates IAM, or decides whether a notification
should be sent.

## Opaque device destinations

`notification_device_destination` stores one owner-scoped destination key, a bounded token
format, opaque token ciphertext, the ciphertext SHA-256, a monotonic `token_version`, a current
`revision`, caller timestamps, and optional revocation/deletion times. `(owner_id,
destination_key)` is the idempotency identity. Registration accepts no plaintext token and verifies
that the supplied digest matches the supplied ciphertext. An exact replay returns the original
row; changing the token, format, digest, or creation time under the same key conflicts.

`rotate_destination_token()` requires both the current destination revision and the current token
version in addition to the owner predicate. A successful rotation replaces the encrypted token,
increments `token_version` and `revision`, and updates the host-supplied rotation time in one
statement. An exact retry returns the same record without another write. A stale revision or token
version, or a rotation attempt against a revoked or deleted destination, raises
`RepositoryConflictError`.

`revoke_destination()` is a terminal live-state transition fenced by owner, destination identity,
and revision. Repeating the same revocation time and state is idempotent; a different time or a
later rotation conflicts. `delete_destination()` advances the revision and writes a tombstone with
no token format, ciphertext, or digest. Repeating the same deletion time returns the same tombstone.
Ordinary reads and listings exclude deleted destinations; `include_deleted=True` is the explicit
inspection path. No method physically erases MVCC versions, WAL, replicas, or backups.

## Delivery outbox and token fences

`notification_push_delivery` binds one owner-scoped delivery key to a live inbox entry and a live
destination. It snapshots the inbox event kind, content format, ciphertext and digest, plus the
destination's `token_version` and `revision` at enqueue time. Enqueue requires the caller's expected
destination revision and token version; a rotation between the caller's read and the enqueue
conflicts rather than silently binding newer token state. An exact replay returns the original
delivery; changing the entry, destination, payload identity, token snapshot, retry bound, or
availability time conflicts.

The delivery state machine is:

- `pending` and `retry`: available at or after `available_at`;
- `claimed`: one current lease owner, claim identity, claim time, and bounded lease expiry;
- `delivered`: an exact-claim acknowledgment time and no error;
- `dead_letter`: a terminal bounded-retry or expired-lease outcome with an error code;
- `superseded`: pending/retry work whose destination was revoked, deleted, rotated, or otherwise
  advanced beyond the delivery token snapshot;

`claim_deliveries()` is owner-scoped and bounded. It selects pending/retry work that is due and
expired claims that still have attempts remaining, locks deliveries with `FOR UPDATE SKIP LOCKED`,
and locks their destinations for share. It claims a delivery only when the destination is live and
both the destination token version and current revision still equal the delivery snapshot. The
returned `ClaimedPushDelivery` carries the encrypted destination token so the host can perform its
own provider call outside Plane. An already-current claim cannot be claimed concurrently by a
second worker.

Each claim increments `attempt_count` and writes one claim identity. Expired claims are reclaimed
on a later claim call until `max_attempts`; an expired claim with no attempts remaining becomes
`dead_letter`. `fail_delivery()` records a bounded error and either schedules a retry or, when the
attempt bound is exhausted, dead-letters the delivery. The caller owns retry timing and provider
failure classification.

`acknowledge_delivery()` accepts only the exact current claim identity and the owner/delivery
identity in the mutation statement. A late acknowledgment after lease expiry is accepted while the
claim identity is still current, which avoids a duplicate send when the provider succeeded before
the worker stopped. Once another claim replaces that identity, the old acknowledgment conflicts and
cannot mark the newer claim delivered. Exact acknowledgment replay returns the original record.
Acknowledging never updates the destination token, so a late result for an older token cannot
restore or mutate a rotated, revoked, or deleted destination.

`expire_stale_deliveries()` is a bounded owner-scoped sweep that marks pending/retry work
`superseded` when its destination is no longer live or its token version/revision no longer matches.
It deliberately leaves in-flight claims alone so their exact late acknowledgment remains possible.
Restart recovery needs no process-local state: a new process reads committed claims, their leases,
attempt counts, and token snapshots from PostgreSQL.

## Public composition

Use `create_push_delivery_repository()` or `runtime.repositories.push_deliveries`. Detached records
are `PushDestinationRecord`, `PushDeliveryRecord`, and `ClaimedPushDelivery`. All methods take a
caller-owned transaction and never commit, roll back, borrow a connection, or invoke a product
callback. A caller should append its own audit event in the same transaction when policy requires
one.

## Migration and recovery

The `089.010 -> 089.011` edge requires the exact `089.010` registry digest
`facf476fbde30deb1952b352e2f80d56742956c368b9f97e3e04478934cc2d9b` and complete predecessor
catalog. It performs no backfill, infers no destination or delivery, and alters no existing table.
A repeat run verifies both tables' constraints and indexes as part of the current catalog and
performs no write. Partial DDL or a failed stamp rolls back with the registry metadata; a same-name
foreign object or damaged predecessor fails before adoption.

Follow the closed-admission backup, guarded upgrade, populated repeat, interruption, corruption,
and rollback checks in [migration and recovery](migration-and-recovery.md). Once the edge commits,
keep both tables and the exact `089.011` revision/digest intact. Prefer a reviewed forward repair.
If the prior composition must be restored, close admission and restore the verified PostgreSQL
backup and every paired durable root together with the exact prior application. Reconcile deliveries
that may have reached a provider before the backup boundary, and never re-deliver a restored claimed
delivery merely because its row came back. Never drop the tables, restore a deleted token, reset
attempt counters, rewrite revision markers, or execute ad-hoc downgrade SQL. Schema qualification
does not establish Deep's encryption, provider, routing, IAM, audit, or UI behavior.