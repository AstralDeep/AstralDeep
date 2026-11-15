# Mesh membership, enrollment, and revocation records

This repository slice exposes the durable identity/challenge state required by owner-confirmed
personal-mesh enrollment. It stores only neutral records: possession proof, IAM, QR/network
handling, and admission policy stay with the host, and private device keys never reach
AstralPlane. The current Plane schema is `089.022`; the slice's tables arrive in the `089.002`
edge over six additive tables.

## Public composition

`create_mesh_enrollment_repository()` returns a stateless `MeshEnrollmentRepository`, also
available as `runtime.repositories.mesh_enrollment`. Every method takes a caller-owned Plane
transaction and never commits, borrows a connection, or runs product callbacks, so Deep can
compose ownership, audit, outbox, and authority writes into one atomic unit.

## Records

- `mesh_record`: one owner's mesh with a bounded display name, monotonic `membership_epoch` and
  `revocation_epoch` counters, and a `record_version` compare-and-set fence.
- `mesh_member`: a `device`, `agent`, or `companion` member with the membership epoch it was
  activated at, an `active`/`revoked`/`retired` status, and its own `record_version` fence.
- `mesh_public_identity`: public key material, algorithm, and a 64-hex fingerprint for one
  member. Binding requires a locked, active member row. Private keys stay at their device.
- `mesh_enrollment_challenge`: an opaque 64-hex possession-challenge digest with host-supplied
  BIGINT issue/expiry windows and a `pending`/`proven`/`expired`/`cancelled` state machine.
- `mesh_enrollment_invitation`: an opaque 64-hex invitation digest with a
  `pending`/`consumed`/`confirmed`/`expired`/`revoked` state machine and the same windows.
- `mesh_member_revocation`: one revocation per member per revocation epoch with a bounded reason.

## Isolation and concurrency

- Every ordinary read and write is scoped to the calling owner in the SQL predicate itself.
- Activation and revocation require the caller's `expected_mesh_version` and
  `expected_member_version`; activation uses member version `0` only for create-only intent.
  Existing members need their exact current version, including intentional re-enrollment after
  retirement or revocation. Confirmation also requires `expected_invitation_version` and the
  host's `as_of` within the invitation window. Stale authority cannot reactivate a member.
- Compound transitions take the mesh lock first and use a local savepoint, so caught conflicts
  preserve the caller's other writes without consuming an epoch or partially confirming.
- Epoch counters are allocated with `UPDATE mesh_record ... RETURNING` inside the caller's
  transaction. Concurrent writes observing the same revision admit one winner; losers refresh
  current records before a new host decision. Accepted writes receive distinct monotonic epochs.
- Invitation consumption and expiry use fenced state transitions; confirmation additionally
  validates current mesh/member/invitation versions and the issue/expiry window; exactly one concurrent consumer or confirmer wins and every loser receives
  a typed conflict (`mesh_invitation_expired`, `mesh_invitation_digest_mismatch`,
  `repository_conflict`) instead of silent success.
- `bootstrap_mesh` creates the mesh and its first member atomically and replays idempotently;
  concurrent exact replays return the first member without advancing epochs. An existing mesh
  cannot gain another bootstrap member; later enrollment uses explicit current-revision fences.
- Transition fences (`record_version`, state predicates) raise typed conflicts on stale writes.

`assert_current_member(tx, owner_id=..., mesh_id=..., member_id=...,
expected_membership_epoch=None, expected_revocation_epoch=None,
expected_member_version=None)` locks the mesh first, then the member, and returns
a detached `(MeshRecord, MeshMemberRecord)` tuple. It always requires active
member status; optional expectations compare the global mesh membership and
revocation epochs and the member record version. Rejected assertions change no
record or epoch. The caller retains both locks through credential CAS, audit or
dispatch; concurrent revocation must wait for that caller transaction.

## Compatibility behavior

The slice is purely additive over the `089.001` catalog: no existing table, column, or function
changes, and no edge in this slice uses `IF NOT EXISTS`. The current-member assertion changes only the repository API; its mesh DDL and
historical `089.002` edge remain unchanged. The separate owner-stop slice advances
the current catalog to `089.007`. An older Plane binary cannot run against the newer schema. Recovery uses
a qualified forward repair or the coordinated PostgreSQL/blob restore in the `089.002` section
of [migration and recovery](migration-and-recovery.md), with the matching prior composition.
Epoch counters are monotonic by design and are never rewritten.
