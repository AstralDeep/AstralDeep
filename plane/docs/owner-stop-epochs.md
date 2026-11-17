# Owner stop epochs

Schema `089.003` adds `owner_stop_epoch`, `peer_stop_acknowledgment` and
`owner_stop_operation_epoch`. The public
`create_stop_epoch_repository()` factory and `RepositoryCatalog.stop_epochs`
return `StopEpochRepository`; frozen `OwnerStopRecord` and
`PeerStopAcknowledgment` values are exported through both stable facades.

All methods accept a caller-owned transaction and a neutral owner identity. The
host verifies IAM, confirmation, mesh identity and peer receipt authenticity,
then appends its authenticated audit event in that same transaction. Plane
implements persistence and fencing; it grants no permission or transport effect.

`get(tx, owner_id=..., for_update=False)` returns the current record or `None`.
`get(..., for_update=True)` and `assert_running(...)` provision and lock an
inactive owner anchor so a first engagement cannot race absence. The anchor has
epoch/revision zero and is hidden by reads. Locks remain held through caller
commit/rollback; different owners use different rows. Host code must preserve a
consistent cross-repository lock order when combining stop and mesh admission.

`engage` requires the current revision, bounded reason (an empty reason is
permitted), actor and aware UTC timestamp. A new engagement increments both
epoch and revision. Only an exact currently engaged replay returns the existing
record. `resume` requires the current revision and engaged epoch; it increments
revision while retaining epoch and original engagement metadata. Stale epochs,
revision mismatches, regressive timestamps and exhausted signed-64-bit counters
raise typed conflicts. `assert_running` retains the owner lock and refuses an
engaged stop or a mismatched optional expected epoch.

`acknowledge` requires the current engaged epoch/revision and an opaque lowercase
SHA-256 receipt digest bound by the host to exact mesh/peer identity. A new
receipt increments owner revision. Exact current replay preserves the first
receipt and timestamp; altered or stale receipts conflict. Receipts remain after
resume and later engagements. Each epoch permits at most 64 receipts;
`list_acknowledgments` returns an owner-scoped tuple ordered by mesh and peer and
fails closed if stored inventory exceeds that bound. Acknowledgments are
evidence of peer receipt only; remote completion remains a host decision.

`bind_operation(tx, owner_id=..., operation_id=UUID(...))` captures the current
running epoch once for an operation. Exact replay returns the binding; a later
epoch never rewrites it. `assert_operation(...)` holds the same owner lock and
requires a running owner with the bound epoch. An unbound legacy operation is
allowed only before the first stop (epoch zero); it is never silently upgraded.
The host calls binding only for newly accepted work. `AcceptedAdmission.created`
is true only on a new operation insert and false on submission/idempotency
replays. `work_admission.peek_next(tx, class_name)` is a detached, unlocked hint:
the host acquires the stop lock before `claim_operation`, which independently
rechecks the candidate under its ordinary admission locks.

The guarded predecessor edge preserves all existing rows and adds only the three
tables. Catalog verification refuses damage and same-name foreign objects.
Migration/recovery uses the normal registry and coordinated PostgreSQL/blob
procedure in [migration and recovery](migration-and-recovery.md). Never reset a
stop epoch or relabel an old receipt to simulate a resume or schema downgrade.
