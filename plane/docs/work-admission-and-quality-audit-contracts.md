# Work-admission and qualification-audit contracts

The `074.002` data-plane composition exposes both surfaces through the ordinary in-process
repository catalog. AstralPlane owns durable PostgreSQL mechanics; AstralDeep retains admission
policy, qualification policy, authorization, redaction, and application transaction composition.

## Work admission

`create_work_admission_repository()` returns the same `WorkAdmissionRepository` available at
`catalog.work_admission`. Every durable operation accepts a caller-owned Plane `Transaction`.
There is no connection, cursor, commit, rollback, or generic SQL escape hatch.

The repository owns persisted class configuration, hierarchical finite slots, immutable
submission replay, owner-partitioned operation lookup, queue/claim/reselection leases, phase/chat
and request-generation binding, terminalization, expiry recovery, and bounded retention. Full
operation reads that intentionally cross owners are explicitly named
`get_operation_for_administration`; ordinary status reads require an `OperationOwner`.

Configuration publication is two-phase because Plane does not own the caller's commit:

1. write or read a detached snapshot with `configure(transaction, configs)` or
   `load_existing_configs(transaction)`;
2. exit the transaction successfully; and
3. call `bind_configs(configs)` to publish that committed graph to the repository instance.

Every public boundary validates record types, UUID objects, timezone-aware timestamps, positive
retention and lease durations, bounded snake-case codes, safe summaries, terminal-state payloads,
and purge limits before issuing SQL. `bind_request_generation` is NULL-only and execution-fenced;
an exact replay returns the existing record, while a changed generation or stale fence is visible.

## Retryable executor inventory

`list_retryable_for_administration` reads retained retryable operations through a bounded,
validated FIFO keyset cursor. The database rows remain the inventory; a caller may keep only
an optional continuation hint and restart scanning from the beginning after restart. Held
work and temporary read/admission failures are never consumed by a read. The optional handled
namespace filter suppresses only an existing child with matching owner partition, parent,
namespace, key, kind, admission class, chat, connection/request generation and present
original digest. An admission refusal does not create such a child. Historical missing-digest
rows stay visible for host verification.

`get_idempotent_operation` is an owner-scoped, read-only lookup of the existing operation for a
bounded namespace and key. It creates no submission or admission record. Hosts can verify
legacy derived identities and report a conflict without repeatedly appending replay receipts.
A host must retain its stop, cancellation, uncertainty, retention and recovery-depth policies;
Plane does not decide whether an operation may execute. A reserved recovery namespace with a
missing parent remains a host reconciliation condition, including after retention clears a
parent link.

These additive APIs and detached assignment recovery results ship in package `0.1.1` without
changing schema `089.008`, its migration digest or the existing compatibility contract.

## Qualification audit

Revision `074.002` owns `test_runs`, `test_case_results`, `test_evidence`, `audit_entries`, and
`latex_artifacts`. `QualityAuditRepository` returns immutable typed records and keeps ordinary
reads/writes owner-partitioned. The review operation locks both the target case and the current
owner audit head, validates the expected verification status, appends the review entry, and changes
case status in one caller-owned transaction.

Legacy audit entries retain `hash_version=1` and their historical digest. New review entries use
the version-2 canonical full-record digest, which authenticates owner, run/case, action, reviewer,
rationale, timestamp, previous link, and entry identity. Migration and replay never silently
rewrite an existing history. A current revision marker and registry digest are accepted only when
the current-schema verifier also matches canonical PostgreSQL catalog structure.

## Verification and recovery

Focused unit/schema tests and the serial isolated-PostgreSQL fresh/upgrade suites cover
exact-repeat upgrade, injected transactional failure and recovery, same-name structural tampering,
concurrent qualification reviews, real work-admission replay/owner denial/claim/terminalization,
and caller-owned rollback.

Revision `074.002` introduced the qualification-audit tables. Its registry digest
`1bb948074ec378d2a74e2b74eff29e72a6f9a6be03d3ae24ec6439fcf70f1e02` stays pinned as the accepted
digest of a `074.002` predecessor; the current schema is `089.022`. The digest-only migration
evidence in `provenance/checks.json` is the historical `074.004` record, not current evidence (see
`provenance/README.md`). Recovery is forward-only under closed admission as described in
`migration-and-recovery.md`; never down-migrate or rewrite audit hashes by inference.
