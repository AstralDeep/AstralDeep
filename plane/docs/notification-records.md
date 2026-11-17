# Notification records

Schema `089.005` adds the durable records AstralDeep needs to apply owner notification policy and
maintain an offline inbox without moving routing, provider, IAM, rendering, or audit decisions into
AstralPlane. The `notifications` catalog member exposes all three tables through one stateless
`NotificationRepository`; every method takes a caller-owned transaction and never commits, rolls
back, borrows a connection, or invokes a product callback.

## Owner preferences

`notification_preference` has one row per owner with explicit booleans for each channel and event
class:

- channels: `in_app_*`, `inbox_*`, and `push_*`;
- events: `*_completion`, `*_failure`, and `*_required_action`.

These are policy inputs, not delivery decisions. The host decides how a preference, quiet period,
device state, authority result, provider capability, or retry policy combines. `revision` is the
compare-and-set fence and `updated_at` is a host-supplied bounded integer. Creation uses
`expected_revision=0`; updates require the current revision. Exact creation replay is a no-op,
stale updates conflict, regressing update times conflict, and a missing owner row is not invented.
No quiet-hours, timezone, device, endpoint, or provider field is stored because those are host or
provider policy.

## Opaque inbox entries

`notification_inbox_entry` stores:

- a UUID entry identity and owner;
- an event kind (`completion`, `failure`, or `required_action`);
- a bounded opaque source reference and owner-scoped deduplication key;
- an opaque content format, ciphertext, and host-computed SHA-256;
- host-supplied creation and retention boundaries;
- optional acknowledgment and deletion timestamps;
- a monotonic revision.

Plane accepts only bounded ciphertext bytes and verifies that the supplied digest matches those
bytes. It never receives plaintext notification content and has no rendering or decryption path.
`(owner_id, deduplication_key)` is unique. An exact replay returns the original entry, including a
deleted entry, so a retry cannot resurrect or duplicate a notification the owner removed. Reusing
the key with changed immutable content conflicts. Every UUID conflict uses an owner-scoped replay
lookup or raises `RepositoryConflictError` without exposing another owner's row or aborting the
caller transaction.

`acknowledge_entry` and `delete_entry` require the owner, entry identity, and expected revision in
the mutation statement. Acknowledging an already acknowledged entry is idempotent only for the same
acknowledgment timestamp; deletion likewise replays only the same tombstone time. Deleted entries
cannot be acknowledged or receive new delivery identities. Reads default to live entries, with an
explicit `include_deleted` inspection path for recovery and support. At the maximum revision
(`9007199254740991`), writes that would increment it return a typed conflict before issuing an
update; exact acknowledgment and deletion replays still return the original record.

`purge_expired_entries` is a bounded, owner-scoped retention sweep. It selects only live rows whose
`retention_expires_at` has passed, takes row locks with `SKIP LOCKED`, and records a deletion
tombstone with a revision increment. It does not physically erase rows or claim to erase MVCC,
WAL, replicas, or backups; physical erasure remains an explicit host purge and operator procedure.

## Delivery identities

`notification_delivery_identity` binds one owner-scoped immutable delivery key to one live inbox
entry. The composite `(entry_id, owner_id)` foreign key makes a cross-owner binding
unrepresentable. Exact registration replay returns the original identity; a changed entry or
creation time conflicts. Every delivery UUID conflict uses an owner-scoped replay lookup or a typed
conflict without exposing another owner's identity or aborting the caller transaction. The key is
opaque and does not encode a channel, endpoint, token, route, provider, or IAM subject. Retries, provider selection, and whether a delivery ever occurs remain
in Deep.

## Push destinations and claimed deliveries

Schema `089.007` adds the separate `push_deliveries` catalog member for opaque device destinations
and bounded claimed-delivery outbox records. Notification policy still decides whether push applies;
that store carries the encrypted destination token, token/revision fences, retry state, and exact
claim acknowledgments. See [push delivery records](push-delivery-records.md).

## Public composition

Use `create_notification_repository()` or `runtime.repositories.notifications`. The same records are
available from `astralplane.NotificationRepository`, `NotificationPreferenceRecord`,
`NotificationInboxEntry`, `NotificationDeliveryIdentity`, and `NotificationEventKind`. A caller
must append its own audit event in the same transaction when its policy requires one.

## Verification and recovery

`tests/test_notifications_089_005.py` exercises preference, deduplication, acknowledgment,
retention, delivery, stale, cross-owner, and facade fences.
`tests/repositories/test_notifications_postgres.py` exercises the same behavior on real PostgreSQL,
including restart persistence and tombstones.
`tests/integration/test_notification_records_upgrade.py` proves populated `089.004` upgrade,
repeat, injected-failure rollback, catalog damage refusal, and namesake refusal, while preserving
representative session, assignment, and completion records. The catalog rollback and repository
contract matrices include the new member.

Recovery follows [migration and recovery](migration-and-recovery.md#089005-notification-records-and-recovery):
keep the new tables and the exact revision/digest after a committed upgrade, prefer guarded forward
repair, and restore the verified database plus paired durable roots together under closed admission
if the prior composition must be reselected. Never drop the tables or rewrite revision markers as an
inferred downgrade.