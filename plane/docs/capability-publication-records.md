# Capability publication records

Schema `089.005` adds `capability_publication` and the public `capability_publications`
catalog member. `create_capability_publication_repository()` returns
`CapabilityPublicationRepository`; frozen `CapabilityPublicationRecord` values and the
`CapabilityPublicationState` enumeration are exported through both stable facades.

A publication binds one neutral `owner_id`, one opaque `capability_id`, one opaque
`audience_id`, one exact `capability_revision`, and a finite `published_at`/`expires_at`
lifetime. The composite key is `(owner_id, capability_id, audience_id)`, so an owner may
publish a capability independently to several audiences without a deployment-wide setting.
`state_revision` is the compare-and-set fence for every transition.

`metadata_ciphertext` and `metadata_digest` carry host-encrypted, opaque capability metadata
and the host's SHA-256 digest of that metadata. Plane never parses, decrypts, or interprets
either value, and stores no provider endpoint, token, protocol selection, discovery
document, or transport behavior. Audience membership, capability approval, IAM, invocation,
and any A2A or MCP exposure decision remain entirely with the host.

## Reads

- `get(transaction, owner_id=..., capability_id=..., audience_id=..., for_update=False)`
  returns the current record in any state, or `None`. `for_update=True` takes a row lock that
  lasts through the caller's commit or rollback.
- `get_active(...)` adds `publication_state = 'published'`, `published_at <= at`, and
  `expires_at > at` to the same owner-scoped SQL statement. It returns `None` for withdrawn,
  explicitly expired, not-yet-published, or time-expired rows. `for_update=True` locks a live
  row so a concurrent withdrawal or expiry cannot commit before an invocation decision does.
- `list_active(transaction, owner_id=..., at=..., limit=...)` returns a detached tuple ordered
  by capability and audience, bounded to at most 200 rows.

Times are timezone-aware UTC values supplied by the host in the same caller-owned
transaction. Plane does not consult a wall clock for publication validity, so tests and hosts
can reason about exact lifetimes without clock-derived nondeterminism.

## Transitions

`publish` inserts a new row at `state_revision = 0 -> 1`, or republishes a withdrawn/expired
row with the expected current revision. A new record requires `expected_revision=0`. An exact
publish replay with the same capability revision, metadata, and lifetime returns the original
row and performs no write. A stale revision, a changed replay of a currently published row,
or a request that tries to change a published row without first withdrawing it raises
`CapabilityPublicationConflictError`.

`withdraw` requires the current revision and a live published row. It records `ended_at` and
an optional bounded reason, increments `state_revision`, and never changes the capability or
audience identity. An exact withdrawal replay returns the original row; a stale revision or
different terminal semantics conflicts. Withdrawal before `published_at` is refused.

`expire` requires the current revision, a live published row, and a host time at or after
`expires_at`. It records the terminal expiry and increments `state_revision`. Calling it
before the lifetime elapses raises `CapabilityPublicationNotExpiredError`; a stale revision,
a withdrawn/expired row with different terminal semantics, or an exhausted signed-64-bit
revision conflicts. Active reads already exclude a time-expired row even before this explicit
terminal transition is recorded.

All transitions lock the exact owner-scoped row with `SELECT ... FOR UPDATE`, then issue one
`UPDATE ... WHERE owner_id = ... AND state_revision = ...` fence. Conflicting first publishes
use `INSERT ... ON CONFLICT DO NOTHING`: one transaction inserts, the other re-locks the
winner and returns it only when the request is an exact replay. No repository method commits,
rolls back, or retains a connection.

## Host responsibilities and recovery

The host validates owner identity, capability approval, audience entitlement, metadata
encryption, and any audit event before or in the same transaction. A host that reads an active
publication and then invokes a capability must recheck its own IAM and capability-revision
policy; this table is publication state, not an authorization decision.

The guarded `089.004 -> 089.005` edge requires the exact predecessor registry digest and
catalog. It adds only this table and its live-owner index, preserves every existing row, and
is repeat-safe by complete current-catalog verification. A failed DDL transaction rolls back
with its registry marker. A same-name foreign object or damaged current catalog fails closed.
Recovery is the normal coordinated PostgreSQL/blob restore under closed admission; never drop
the table or rewrite a revision marker to simulate a downgrade. Restored publication records
remain historical until the host refreshes IAM and capability approval and explicitly
republishes or withdraws them.