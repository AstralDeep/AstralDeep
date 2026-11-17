# Operation harness profile bindings

Schema `089.010` adds `operation_harness_binding`, one immutable profile/version/artifact identity
for an admitted operation. AstralPlane persists only the neutral identity; harness deployment
selection, provider routing, rollout qualification, artifact availability, IAM, and audit policy
remain with the embedding host.

## Typed contract

`HarnessProfileIdentity(profile, version, artifact_digest)` is the only accepted identity shape:

- `profile` is bounded lowercase snake case, `^[a-z][a-z0-9_]{0,63}$`.
- `version` is a bounded numeric semantic version, `^[0-9]+\.[0-9]+\.[0-9]+$`.
- `artifact_digest` is a lowercase SHA-256 digest supplied by the host.

`OperationHarnessBinding` returns the operation identity, the typed identity, the execution
generation observed at binding time, and the database transaction timestamp. It exposes
`profile`, `version`, and `artifact_digest` aliases for callers that project the flat fields.

## Bind and read

`WorkAdmissionRepository.bind_harness_profile(transaction, owner, fence, identity, now=None)`
requires a current `ExecutionFence` and an `OperationOwner`. The operation row is locked under the
owner predicate, the fence is compared, and the insert selects the same owner- and fence-qualified
operation row. The write carries the owner and execution generation/lease in one statement; it does
not write a JSON metadata bag or alter the strict operation record.

An exact replay with the same identity returns the original binding. A different identity raises
`RepositoryConflictError`, a stale or replaced fence raises `StaleWorkExecutionFenceError`, and a
missing or foreign operation raises `WorkAdmissionNotFoundError`. A direct update or delete of the
binding row is rejected by `operation_harness_binding_immutable`; operation retention purge removes
the binding through its foreign-key cascade.

`WorkAdmissionRepository.get_harness_profile_binding(transaction, owner, operation_id)` is a
single-row, owner-joined read. An operation created before this edge, or one that has not yet been
bound, returns `None`; migration never invents a profile identity for existing work. Reads remain
available after the operation leaves `running`, so terminal evidence can be inspected.

## Upgrade and recovery

The guarded `089.009 -> 089.010` edge requires the exact `089.009` registry digest
`c3e2b3d2d2a2f88a7b995eb6525f6f01df463bad5129a989f7ed70be5e95ed4b` and its catalog. It adds one
table, one index, one immutability function, and one trigger without changing existing operation,
assignment, notification, guidance, or audit rows. Current catalog digest is
`1e1fb4ca7f6ec63e790a65c3ebbaf028702f0ddefb084152fba6cb19cb161c68`.

Quiesce writers and apply the edge through normal host initialization. Verify repeat startup,
legacy operations read as unbound, and an owner/fenced bind followed by a new repository instance
reading the persisted identity. `tests/integration/test_harness_bindings_upgrade.py` covers the
populated upgrade, injected rollback, namesake refusal, current-catalog damage, and repeat run;
`tests/repositories/test_harness_bindings_postgres.py` covers legacy absence, replay, owner/stale
denial, immutability, restart persistence, and purge cascade.

Prefer guarded forward repair. If restoration is necessary, keep admission closed and restore the
verified PostgreSQL backup and every paired durable root together with the exact prior composition.
Do not rewrite operation harness bindings, drop the table to simulate downgrade, or infer that a
restored operation's durable profile identity is still available in the host's deployment catalog.