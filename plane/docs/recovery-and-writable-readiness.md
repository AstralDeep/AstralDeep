# Neutral recovery and writable-readiness preconditions

## Status and scope

This document defines the neutral, implementable preconditions for treating a restored
PostgreSQL database and its configured blob roots as a **writable** AstralPlane recovery state.
It specifies the recovery boundary associated with `astralplane.recovery/v1`; this document
does not add a service port, a second database, an election protocol, or an admission API.

The specification status is **defined and reviewable**. The runtime status is **partial**:
Plane already provides guarded migration, exact catalog verification, durable boot
reconciliation, explicit joint PostgreSQL/blob restore qualification, explicit restored-session
retirement, explicit blob-root safety, and digest-checked blob reads. Plane does not provide
automatic node election, automatic replica promotion, backup selection, a readiness certificate,
or reconciliation of external LETS/authority state. Those remain host or operator
responsibilities. A successful restore, a compatible row set, or `PlaneHealth.ready` alone is not
a claim that a state is writable.

The contract applies when a personal-mesh node is lost and a host is deciding whether another
node or a restored deployment may serve the owner's durable state. Personal-mesh membership and
device identity are neutral records in Plane; they do not grant the right to lead a mesh, fence
an old node, or open admission.

## Terms

- **Durable recovery unit**: one PostgreSQL database plus every configured attachment, artifact,
  workspace, generated-knowledge, and other durable root that belongs to the same Plane
  composition. The roots are host configuration, never package or source directories.
- **Restore candidate**: a database copy and blob-root set loaded into recovery destinations but
  not yet admitted for writes.
- **Writable readiness**: the host's recorded decision that the restore candidate may accept
  ordinary durable-state writes. Read compatibility, a successful backup restore, a successful
  migration, and local process health are necessary inputs at most; none is synonymous with
  writable readiness.
- **Commit ambiguity**: the caller cannot determine whether a database commit took effect, for
  example because the connection was lost after the server committed but before the client saw
  the acknowledgement. It is an uncertain outcome, not a reported failure and not a reported
  success.
- **Host**: the embedding AstralDeep composition and its operator, including its election,
  admission, identity, key-custody, and external-authority mechanisms.

## Ownership boundary

| Concern | Plane mechanics | Host-owned decision or evidence |
| --- | --- | --- |
| Leadership, quorum, node fencing, single writer | None. Advisory locks coordinate clients connected to the same PostgreSQL database only. | Elect or fence the serving node; prove the old node and every other writer are quiescent before restore or promotion. |
| Admission | No admission service. Recovery and repository calls run only when the host invokes them. | Keep admission and every product writer closed until every precondition below is satisfied. |
| Schema and catalog | Guarded migration registry, exact revision/digest checks, full current-catalog verification, repeat-safe forward repair. | Select the exact qualified Plane revision and decide whether a failed state is repairable forward. |
| Backup and restore | Supplies the procedures and local verification mechanics; it never chooses or validates a backup. | Choose the PostgreSQL and blob snapshots, prove they belong to one maintenance window, restore them, and retain the evidence. |
| Durable blobs | Explicit absolute root, link/reparse refusal, bounded path identities, staged publish and purge capabilities, read-time size/digest verification. | Record the complete root set, verify its inventory and digests, and decide whether a root is the same logical root. |
| Sessions | Explicit complete restored-session retirement on the exact current schema/digest. | Prove that no old process can create sessions, discard caches, and require fresh authentication and consent. |
| External authority, effects, and delivery | Durable outbox, receipt, grant, and uncertain-effect storage where the schema already defines it. | Reconcile external LETS state, effects, deliveries, revocations, and liabilities after a restore; a database snapshot cannot roll those back. |
| Readiness evidence | Supplies compatibility, migration, reconciliation, and retirement reports. | Assemble, retain, and approve the complete evidence record and make the final admission decision. |

Plane MUST remain PostgreSQL-only. A recovery design MUST NOT introduce a second durable store,
a mesh consensus database, a service port, or an implicit root discovery mechanism. The
dependency-direction guard in
`tests/architecture/test_dependency_direction.py::test_astralplane_runtime_has_no_forbidden_imports`
keeps product, UI, IAM, and transport policy outside Plane.

## Normative preconditions

The identifiers below are stable references for review and acceptance. A writable readiness
decision MUST satisfy every applicable precondition.

### R1 — Restore one quiescent recovery unit

The PostgreSQL database and every paired durable root MUST come from the same verified recovery
point or maintenance window. All writers MUST be stopped and the old serving node MUST be fenced
before the restore or promotion is considered. Plane cannot prove quiescence from a successful
transaction, a local advisory lock, an open pool, or a row copy. A database-only restore, a
blob-only restore, a mixture of snapshots, or a restore taken while writers were active is not a
writable candidate.

### R2 — Bind the exact composition identity

The host MUST record and select the exact Plane commit, package version, contract version,
schema revision, migration registry digest, blob-layout version, and recovery-contract version.
The selected consumer MUST declare that exact composition compatible. A copied row set, an
available replica, or a newer/older binary MUST NOT be treated as compatible merely because the
rows are present. Deep adopts only the exact qualified Plane revision.

### R3 — Require a recognized writable schema

For writable readiness, the database MUST be at the exact schema revision expected by the
selected composition, or MUST first be forward-migrated from a recognized predecessor to that
exact revision. `inspect_compatibility(...)` deliberately accepts the declared read-compatible
floor, so its `compatible: true` result is not by itself permission to write. It also evaluates
the observed revision supplied by the caller rather than querying the database. The host MUST
read the actual schema metadata and run the guarded registry before relying on it.

`CURRENT_DATA_PLANE_REVISION.accepts_reader_at(...)` describes read compatibility only. An
older composition may be selected only when its own contract explicitly declares the observed
state readable and writable; otherwise the state must reach the selected exact target first.

### R4 — Require exact digest and complete catalog agreement

`schema_meta.revision` and `schema_meta.astralplane_migration_digest` MUST agree with each other
and with the selected registry. The full current catalog MUST pass Plane's structural verifier,
including owned tables, columns, constraints, indexes, functions, triggers, policies, rules,
inheritance, and schema privileges. A current marker MUST NOT be used to excuse a damaged
catalog. Missing metadata, duplicate metadata, a wrong digest, a partial schema, unknown
revisions, same-name replacements, or unexpected owned objects MUST be refused without
rewriting the marker or adopting the objects. A restored predecessor is acceptable only with its
exact pinned historical digest and attested catalog, and it remains unwritable until the guarded
forward migration and current-catalog verification complete.

### R5 — Preserve the complete blob-root set and layout

The host MUST restore every configured root that belongs to the composition, not only the root
named by a current process. Root paths MUST be explicit and absolute, outside source, package,
and submodule trees. The blob-layout version MUST match the selected contract. A renamed,
relocated, partially copied, or newly created root is not evidence of the restored root's
identity. Moving a root after recovery is a separate quiesced relocation with its own copy and
verification procedure; it MUST NOT be presented as part of a database restore. No DB-only
locator mutation or implicit root fallback is authorized.

### R6 — Verify blob membership and content

Before writable admission, the host MUST verify the restored root set against the durable
inventory: expected files are present, unsafe entries are refused, file counts and byte counts
match, and SHA-256 values match the recorded durable metadata for the same recovery point. Plane
validates the explicit root, refuses traversal and symlink/reparse crossings, and verifies
expected size and digest on supported reads. `qualify_joint_restore(...)` additionally compares
one caller-supplied manifest with the exact membership and bytes of every configured root, then
binds the result to the database fence. Plane still does not choose or validate the backup, or
prove that the supplied manifest is complete for the original recovery point. It MUST NOT create
missing bytes from database rows, delete extra bytes to make the inventory agree, or silently
replace mismatched content. A missing or mismatched blob is a failed recovery precondition.

### R7 — Complete migration and reconciliation before readiness

The selected composition MUST initialize through the guarded migration registry and the required
reconciliation plan under closed admission. The migration report MUST target the exact expected
revision, carry the exact registry digest, and have passed the current-catalog verifier. The
reconciliation report MUST belong to the same revision and plan digest, use the declared
reconciliation advisory lock, include every required hook, and prove `durably_complete: true`.
A boot failure, a running or failed reconciliation, a partial migration, or an unverified no-op
MUST leave admission closed. Retry is explicit; an interrupted or failed boot MUST NOT be
reported as ready.

### R8 — Retire every restored session

After the restore and under closed admission, the host MUST call
`recover_joint_restore(...)` or first `qualify_joint_restore(...)` followed by
`retire_restored_sessions(database_url=..., expected_database=..., expected_schema=...,
expected_schema_revision=..., expected_migration_digest=...)` from the matching Plane facade.
The qualified path MUST verify the exact database, schema, current revision, digest, complete
catalog, root identities, and supplied blob inventory before retirement. The retirement call
MUST retire every restored `web_session` row in one transaction. The host MUST run it again
after every restore, even when the snapshot contains evidence of an earlier retirement.

A successful call proves only that a committed retirement deleted the sessions observed by that
transaction. The host MUST repeat it until the repeated call returns zero after writer
quiescence has been independently established. A retirement call does not fence new writers,
change admission, invoke IAM, clear another process's memory, or prove blob consistency.
Grants, assignments, issued permits, uncertain effects, receipts, audit, and liabilities remain
historical and are not silently revived or deleted. After retirement, old session and consent
bindings MUST be refused and the host MUST require fresh authentication and new consent before
reopening IAM-backed work.

### R9 — Keep election and admission host-owned

Plane MUST NOT be used as an election authority, quorum service, distributed lease, or
split-brain resolver. PostgreSQL advisory locks and row locks in Plane coordinate callers using
the same database; they do not fence another node, another database copy, or an old process.
The host MUST prove leadership/fencing, stop all writers, reconcile external authority and
effect state, and decide when admission reopens. This remains true when a personal-mesh node has
been lost: mesh identity rows and membership epochs describe durable records, not permission to
serve.

### R10 — Record sufficient, current readiness evidence

Writable readiness MUST have an evidence record made while admission is closed. The record MUST
be bound to the selected composition and the exact restore candidate, and MUST contain at least:

| Evidence element | Required content and source |
| --- | --- |
| Composition | Contract version, package version, schema revision, migration digest, blob-layout version, recovery-contract version, and the exact selected commit. |
| Compatibility | The public `CompatibilityReport` fields, with no compatibility reasons, plus the actual observed database revision rather than only the caller's assumption. |
| Migration | `MigrationReport.source_revision`, `target_revision`, `applied_steps`, `already_current`, and `migration_digest`; the target and digest MUST be exact and the catalog verifier MUST have passed. |
| Reconciliation | `ReconciliationReport.schema_revision`, `plan_digest`, `advisory_lock`, every hook identity/attempt/result digest, and `durably_complete: true`. |
| Restore | Backup snapshot identity, maintenance window, database and every root restored, writer-quiescence/fencing reference, and successful restore verification references. |
| Blobs | Layout version, complete absolute root set with the host's root-identity evidence, file/byte counts, and SHA-256 manifest or equivalent verification references. |
| Sessions | First retirement count, repeat retirement result (zero after quiescence), the target database/schema/revision/digest checked by the facade, and the opaque joint-restore evidence digest. |
| Host gate | Election/fencing evidence, admission-closed reference, external authority/effect reconciliation references, approver identity, and decision time. |

The record MUST NOT contain the database URL, credentials, tokens, keys, raw user content, or
other secrets. It MUST be retained for the recovery review window. Evidence from another
revision, root set, snapshot, or candidate is stale and MUST NOT authorize this restore. The
public reports provide neutral fields; the host owns the record format, storage, and approval.

`PlaneHealth.ready` is only a local runtime observation: compatibility of the supplied
composition, the in-process boot status, and an open pool. It does not carry restore, blob,
session-retirement, fencing, or external-authority evidence, and it MUST NOT be used as the sole
writable-readiness record.

### R11 — Treat commit ambiguity as an uncertain outcome

A Plane recovery entry point that owns its transaction, such as session retirement, returns a
result only after its commit completes successfully. A repository call inside a caller-owned
transaction returns a provisional result before the caller commits. If the caller loses the
acknowledgement, receives a timeout, or cannot otherwise distinguish a pre-commit failure from a
post-commit failure, the outcome is ambiguous. Plane MUST NOT claim that the operation failed,
that it rolled back, or that it committed. A recovery failure wrapper may be data-free because
the durable outcome is unknown.

The host MUST keep admission closed and determine the actual durable state by re-reading it or
by repeating an explicitly idempotent/fenced operation. Exact session retirement is repeat
safe: repeating after an ambiguous call is permitted, and only a later zero result after
quiescence can complete retirement evidence. A migration MUST be re-read through its exact
metadata and catalog before a forward retry. Repository operations MUST use their declared
idempotency keys, receipts, or compare-and-set fences; an exception MUST NOT be treated as proof
that a write rolled back. Uncertain external effects and outstanding liabilities MUST remain
visible and be reconciled by the host. Deleting rows, rewriting revision markers, or inventing a
successful outcome to normalize an ambiguous call is prohibited.

A database snapshot cannot roll back work that happened outside PostgreSQL after the snapshot,
including external grants, deliveries, revocations, and remote effects. Those are reconciled by
the host, not inferred from restored rows.

### R12 — Limit forward repair and use joint restore beyond it

Forward repair is preferred only within the following limits:

- Retry the guarded migration registry from an exactly recognized predecessor or the exact
  current revision, with its pinned digest and attested catalog.
- Repeat reconciliation from durable markers, and repeat session retirement or idempotent purge
  work, without changing historical evidence.
- Re-read and verify the same configured root set; retry safe reads and absent/purge checks.
- Use the separate quiesced blob-root relocation procedure when the root itself must move; do
  not disguise relocation as restore.

Forward repair MUST NOT run inferred down-SQL, rewrite `schema_meta`, drop new tables or
columns, adopt same-name foreign objects, infer a downgrade, reconstruct missing blobs from rows,
delete extra blobs without evidence, revive an old session/grant binding, or replay an external
effect merely because a snapshot contains a row. If the state is unknown, partial, corrupt,
blob-inconsistent, or externally irreconcilable, the only supported path is to preserve the
failed copies, keep admission closed, and restore the verified joint PostgreSQL/blob backup with
the exact matching composition; then apply R1 through R11 again. Rehearse the paired restore and
re-upgrade on an isolated copy when possible.

## Unsupported states

The following states are explicitly unsupported as writable recovery states. They require the
listed host recovery action, not an inference by Plane.

| Unsupported state | Required action |
| --- | --- |
| A live node, replica, or old process may still write while the restore is promoted. | Fence and quiesce it first; keep admission closed. Plane cannot elect or fence it. |
| Only PostgreSQL rows were copied, or only a blob root was copied. | Repeat the joint restore with every paired root from the same recovery point. |
| A blob root was renamed, newly created, partially copied, or silently remapped. | Use the explicit relocation/restore procedure and record root identity; do not infer it from a path string. |
| The database is at a read-compatible predecessor, or `inspect_compatibility` alone says compatible. | Run the exact guarded forward migration and current-catalog verification before writes, or use an older composition only when its contract explicitly authorizes that state. |
| The current marker is present but metadata, catalog, constraints, indexes, functions, or privileges differ. | Refuse writes; preserve the failed state for diagnosis; forward-repair only from an exact recognized state. |
| A migration or session-retirement call has ambiguous commit outcome. | Re-read the durable state or repeat the idempotent operation; do not claim success or failure from the exception. |
| Restored sessions remain, retirement was skipped, or its result was not repeated after quiescence. | Keep admission closed and complete the explicit retirement, then discard process caches and require fresh authentication. |
| Some blobs are missing, extra, truncated, or digest-mismatched. | Restore/verify the complete paired root; do not synthesize or delete bytes to match the database. |
| External LETS authority, effects, revocations, deliveries, or consent state cannot be reconciled. | Keep admission closed and complete host-owned reconciliation or restore a compatible joint point. |
| Encryption/audit keys or other host-held material are lost or changed. | Plane cannot recover plaintext or authenticate the chain; use the exact matching host key custody and qualified composition. |
| `PlaneHealth.ready` is the only evidence available. | Build the complete R10 record; local health is not writable readiness. |
| A second database, mesh consensus store, or service port is proposed as the recovery mechanism. | Reject it; Plane and its recovery unit are PostgreSQL-only. |

## Why common shortcuts are insufficient

- **Copied rows are not a recovery unit.** They do not prove the blob roots, exact catalog,
  retired sessions, current composition, or writer quiescence.
- **An available replica is not an elected writer.** Availability says nothing about fencing,
  recovery-point consistency, or external effects.
- **Renamed blobs are not verified blobs.** A path change does not preserve recorded root
  identity, membership, byte counts, or digests.
- **A successful migration is not admission.** It proves the schema path reached its target; it
  does not prove the restore, blobs, sessions, or host authority.
- **A retirement count is not proof of quiescence.** It proves the sessions observed by that
  committed transaction are gone.

## Source traceability

The promises above are implemented by these public and internal surfaces; the test matrix below
binds their observable behavior to deterministic cases.

| Contract area | Source surface |
| --- | --- |
| Contract, schema, digest, blob-layout, and recovery identities | `src/astralplane/compatibility.py` and `src/astralplane/database/revision.py` |
| Guarded migration, repeat-safe edges, exact digest, and full catalog verification | `src/astralplane/database/migrations.py` |
| Migration-then-reconciliation readiness publication | `src/astralplane/database/bootstrap.py` and `src/astralplane/reconciliation.py` |
| Explicit restored-session retirement and its data-free failure boundary | `src/astralplane/recovery.py` and `src/astralplane/repositories/history.py` |
| Coordinated database/blob restore qualification and evidence | `src/astralplane/joint_restore.py` |
| Explicit roots, safe paths, digest-checked reads, staged publish, and purge capabilities | `src/astralplane/api.py` and `src/astralplane/blob_store.py` |
| Joint database/blob restore procedure and recovery-unit definition | `docs/migration-and-recovery.md` |

## Traceability to current behavior and executable cases

Every precondition is tied to a current public-facade behavior and at least one deterministic
acceptance case. The acceptance cases execute real behavior or real PostgreSQL; none relies on
source-token matching alone.

| Precondition | Current behavior and source | Executable acceptance case |
| --- | --- | --- |
| R1 | Joint restore is documented in `docs/migration-and-recovery.md`; blob staging/publish failures are visible and retryable. | `tests/integration/test_pre_split_upgrade.py::test_documented_joint_restore_returns_to_066_then_reapplies_upgrade` |
| R2 | `src/astralplane/compatibility.py` exposes exact contract, schema, digest, blob-layout, and recovery identities; `inspect_compatibility` rejects mismatched contract/consumer/schema. | `tests/test_compatibility.py::test_incompatible_compositions_report_stable_reason_codes` |
| R3 | `CURRENT_DATA_PLANE_REVISION.accepts_reader_at` declares read compatibility; `MigrationRunner.run` requires the selected exact target and forward-migrates recognized predecessors. | `tests/test_recovery_writable_readiness_contract.py::test_current_revision_is_the_only_declared_writable_target` |
| R4 | `MigrationRunner.run` and `MigrationRegistry.verify_current` refuse wrong digests, missing markers, and structural drift without relabeling. | `tests/test_schema_migrations.py::test_current_metadata_cannot_hide_structural_corruption` |
| R5 | `create_streaming_blob_store` requires one explicit absolute root and `blob_store.py` rejects unsafe roots and path crossings. | `tests/test_blob_store.py::test_constructor_rejects_missing_relative_file_and_link_roots` |
| R6 | `qualify_joint_restore` verifies exact manifest membership, byte counts, SHA-256 values, root identities, and the database fence; blob readers and parser leases verify expected size/digest on reads. | `tests/test_joint_restore.py::test_incomplete_or_changed_blob_inventory_is_refused` and `tests/test_blob_store.py::test_reader_reports_missing_oversize_and_persisted_integrity_mismatch` |
| R7 | `BootInitializer` publishes ready only after the migration commits and every reconciliation hook is durably complete. | `tests/test_bootstrap.py::test_reconciliation_failure_requires_explicit_retry_before_ready` |
| R8 | `recover_joint_restore` qualifies the exact database/root unit and then `retire_restored_sessions` retires all restored sessions on the exact current schema/digest; both are repeat-safe. | `tests/repositories/test_session_restore_postgres.py::test_restore_retirement_preserves_liabilities_and_authentic_settlement` |
| R9 | Plane has no election/host-policy dependency, and recovery takes an explicit target without implicit environment fallback. | `tests/architecture/test_dependency_direction.py::test_astralplane_runtime_has_no_forbidden_imports` and `tests/test_session_recovery.py::test_invalid_explicit_target_never_constructs_a_pool` |
| R10 | `PlaneHealth`, `MigrationReport`, and `ReconciliationReport` expose local evidence, while readiness remains a host-owned decision. | `tests/test_recovery_writable_readiness_contract.py::test_health_ready_is_local_and_not_restore_readiness` and `tests/integration/test_recovery_writable_readiness.py::test_current_catalog_repeat_is_a_verified_noop` |
| R11 | Session retirement commits or reports a data-free failure; a lost commit acknowledgement cannot become a completion claim. | `tests/repositories/test_session_restore_postgres.py::test_lost_commit_acknowledgement_returns_no_completion_and_retry_checks_actual_rows` |
| R12 | Damaged predecessor metadata and catalogs are refused before repair or relabeling; forward retries require the exact recognized state. | `tests/repositories/test_session_restore_postgres.py::test_restored_metadata_or_catalog_damage_is_not_migrated_or_repaired` |

The document itself is checked for status, required sections, live contract identities, source
paths, and resolvable test references by
`tests/test_recovery_writable_readiness_contract.py::test_contract_document_declares_status_and_traceability`.
The exact current digest/catalog acceptance cases also run against real PostgreSQL in
`tests/integration/test_recovery_writable_readiness.py::test_current_catalog_repeat_is_a_verified_noop`,
`tests/integration/test_recovery_writable_readiness.py::test_wrong_current_digest_is_refused_without_relabelling`,
and `tests/integration/test_recovery_writable_readiness.py::test_damaged_catalog_is_refused_without_relabelling`.

## Acceptance decision

A host may open admission only when it can show, under closed admission, that R1 through R10 are
satisfied, R11 has no unresolved ambiguous outcome, and R12 permits the chosen repair path. The
evidence must be complete, current, and bound to the same composition, restore point, and root
set. Session retirement is a separate gate from migration and readiness; passing one never
implies the others. If any requirement cannot be proved, the state remains closed and the host
uses a verified joint restore rather than inventing a writable recovery state.