# Current authority observations

These additive read guards added no schema change when they were introduced on
schema `088.005`. They neither renew a lease nor select an execution, exchange a
credential, or grant permission. Callers retain the original authority snapshot
and compare the returned record to that snapshot; a new record is not permission
to adopt replacement authority. No host wall-clock argument is accepted.

`work_admission.assert_current_execution_lease(transaction, fence)` returns the
current `OperationRecord`. The exact copied `ExecutionFence` must still name a
running, uncancelled execution. The repository locks its operation, then all
admission slots in `(class_name, slot_number)` order. There must be exactly one
slot for each class in the current configured chain, one coherent slot token,
valid slot generations, and every lease must extend beyond `clock_timestamp()`
sampled after the locks. A changed configured chain or incomplete lease refuses.
No configuration-row locks are acquired after operation locks. Existing
`assert_current_execution` retains its broader settlement semantics.

`offline_grants.assert_current_grant(transaction, *, owner_id, grant_id)` returns
the exact `OfflineGrantRecord`, including only the existing opaque encrypted
credential field. It requires a current active owner, an unrevoked grant, and
`issued_at <= database_time_ms < expires_at`. Missing owner lifecycle metadata
retains the existing active-owner convention. It never decrypts the record.

The host takes owner 79 and any original session locks, then a scheduled
occurrence when applicable, then operation and sorted slots, then the grant,
then guidance heads and policy/audit. The grant guard checks owner 79 with a
nonblocking advisory acquisition and owner lifecycle with `FOR UPDATE NOWAIT`.
This permits reentry in the canonical order and refuses upstream contention
instead of introducing a blocking inversion. NOWAIT refusal rolls back its
savepoint and raises a data-free repository conflict; the caller transaction
remains usable. The grant row lock can wait within the caller's SQL bounds;
database time is sampled after that wait.

These are bounded transaction observations, not perpetual capabilities. Hosts
must establish current human or delegated authorization separately, bound SQL
waits, keep later operations in the declared lock order, and repeat the guards
after later waits before using private guidance. No network runs under these
locks. Authentic terminal settlement remains available under its existing
contract after authority loss.

## Voice guidance observation

`voice.assert_current_guidance_turn(transaction, *, owner_id, session_id,
turn_id, expected_session_generation, expected_media_grant_revision,
operation_id)` returns a frozen `VoiceGuidanceObservation` containing detached
existing session/turn metadata and the database observation time. Session,
turn and operation identities are canonical UUID4 strings; generations are
exact positive integers. No transcript or media is returned or retained.

After its normal owner and operation/slot guards, the host calls this assertion
before guidance heads. It locks the exact owner session then turn using
`FOR UPDATE NOWAIT` in one savepoint. Existing voice writers use both lock
orders; any contention refuses immediately and releases partial read locks,
so a turn-first writer can proceed. Existing writers and their leases are
unchanged. Cancellation propagates after savepoint rollback.

The session must be active, unended, at its original generation and media-grant
revision, and within its lease according to database time sampled after the
reads. The accepted, nonterminal turn must retain its exact owner, session,
generation, media revision and operation, with no unavailable-chat marker.
Backend metadata uses the existing strict local/remote validator. The host
continues to compare the original full turn/connection identity and to guard
human permission and operation authority; this voice observation does not
replace those checks or authorize a later voice write. A later read must
repeat these checks; a retained observation is not a capability.
