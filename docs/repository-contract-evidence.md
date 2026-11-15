# Public repository contract evidence

AstralPlane's stable catalog currently contains 59 repository members. The contract matrix in
`tests/contract/test_repository_contract_matrix.py` is ordered against `RepositoryCatalog.as_mapping()`
and fails whenever a public member is added, removed, or reordered without evidence.

For every member, the gate now executes concrete behavioral tests for its applicable scope,
replay/idempotency, compare-and-set or concurrency, and typed failure behavior. It does not accept
source-token matching as behavioral proof. Read-only projections and deliberately distinct-event
queues carry explicit reasons where replay or mutable CAS does not apply. Global tutorial content,
fixed test-harness cleanup, worker queues, and administrative inventories remain explicitly named
and bounded rather than being mislabeled as owner CRUD.

The same matrix executes a real repository query with an attributed sentinel through the supplied
transaction, proves the persistence failure remains visible, and proves no repository attempts to
commit or roll back the caller's transaction. The static no-commit/no-rollback scan is retained only
as an additional packaging guard.

`tests/integration/test_catalog_caller_rollback.py` supplies the corresponding live PostgreSQL
proof. It exactly classifies all 59 catalog members, performs one successful write for each of the
58 write-capable members, confirms the mutation is visible inside the caller's transaction, forces
the caller to abort, then re-reads persistence in a new transaction. Insert/update writes disappear,
and the fixed-manifest cleanup deletion is restored. The one non-write member,
`agent_management`, exposes only the bounded `get_list_context()` and `get_detail_context()` read
projections, so caller rollback is genuinely inapplicable rather than silently omitted.

Serial isolated-PostgreSQL evidence covers the full rollback matrix plus locking-sensitive
seams that a scripted transaction cannot prove: audit JSON detachment, WorkAdmission
replay/fence/rollback, concurrent qualification review serialization, concurrent policy
reconciliation, active authority-binding uniqueness, and claim/outbox savepoint rollback.
Scheduler, maintenance, voice, workspace publication, and history composition retain their focused
product-level PostgreSQL suites in AstralDeep; those callers use the same public Plane repositories
and application runtime.

Push deliveries additionally exercise opaque destination rotation/revocation/deletion, stale-token
claim denial, concurrent claim exclusion, expired-lease recovery, and exact-claim late
acknowledgment fencing. Assignments additionally exercise real concurrent claim, shared-budget reservation, control/permit
ordering, remote-proposal creation/link rollback, and account retirement. Its behavioral matrix entry
executes the same isolated PostgreSQL owner/replay/claim cases used by the dedicated assignment suite.

This evidence uses repositories against the current `089.022` schema. A future catalog addition
must add both a failure/attribution probe and executable behavioral evidence before the
exact-catalog assertion can pass.

Harness cleanup accepts the optional `owner_issuer_hash` keyword for canonical synthetic OIDC
owners. It must be exactly 16 lowercase hexadecimal digits and scopes the existing verification
or security-benchmark prefix as `oidc.<hash>.__verif__<run>_` or
`oidc.<hash>.__bench__<run>__`. Omit it to retain legacy cleanup. The run identifier rules, literal
LIKE escaping and fixed table allowlists remain unchanged; arbitrary owner prefixes are not an
API. Real backend tests cover another issuer, neighboring run names, ordinary owners, legacy
namespaces, caller rollback and a failure after a deletion.

The `projects` matrix entry exercises the public factory against real PostgreSQL for owner
isolation, semantic create replay, caller rollback, and independent-connection create races.
The dedicated project suite also proves attach/delete serialization and complete chat inventory.
