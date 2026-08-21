# Contract: AstralPlane Embedded Durable-State Library

**Contract version**: `astralplane.contract/v1`

**Runtime**: Python 3.11, embedded in the AstralDeep application process
**Database**: the existing PostgreSQL service and database

## Ownership

AstralPlane owns:

- PostgreSQL pool, connection, transaction, and explicit boot initialization;
- ordered guarded migrations, schema revision, indexes, constraints, and deterministic database-only backfills;
- neutral immutable persistence records and repository implementations;
- conversation/session/history/workspace/canvas persistence;
- attachment, artifact, generated-knowledge, and generated-agent-artifact storage mechanics;
- audit persistence and retention anchors;
- scheduler, work-admission, effect-claim, voice-session, remote-metadata, and asynchronous-operation persistence;
- durable outbox, purge tombstones, claim/lease/retry/dead-letter mechanics;
- data-plane compatibility, integrity, backup, and recovery tooling.

AstralPlane does not own:

- user/agent authorization or product policy;
- API, WebSocket, A2A, MCP, or in-memory stream transport;
- orchestrator handlers, agent/model logic, confirmation, PHI, taint, or egress decisions;
- SSH/remote execution, LiveKit/media, or the voice worker;
- rendering, UI seed definitions, clients, primitives, or LETS policy;
- a separately deployed service or new service port in contract v1.

## Dependency rule

`astralplane` must not import AstralDeep, AstralProjection, AstralPrimitives, or LETS. Neutral records and protocols live inside `astralplane.contracts`. AstralDeep maps its domain objects to those records at the boundary.

An architecture test scans runtime imports and fails on forbidden roots. The installed wheel is built with `--no-deps`; only dependencies already present in the composed runtime may be declared, initially the existing psycopg/cryptography stack.

## Initial package layout

```text
src/astralplane/
├── compatibility.py
├── contracts/
├── database/
│   ├── pool.py
│   ├── transaction.py
│   ├── migrations.py
│   └── revision.py
├── repositories/
│   ├── conversations.py
│   ├── workspace.py
│   ├── attachments.py
│   ├── artifacts.py
│   ├── audit.py
│   ├── scheduling.py
│   ├── admission.py
│   ├── voice.py
│   ├── remote.py
│   ├── agents.py
│   └── sessions.py
├── blobs/
│   ├── attachments.py
│   ├── agent_artifacts.py
│   └── knowledge.py
└── outbox/
    ├── records.py
    ├── repository.py
    └── worker_contract.py
```

## Compatibility metadata

The package exports immutable metadata:

```python
CONTRACT_VERSION: str
SCHEMA_REVISION: str
READ_COMPATIBLE_FROM: str
MIGRATION_DIGEST: str
BLOB_LAYOUT_VERSION: str
RECOVERY_CONTRACT_VERSION: str
ADVISORY_LOCK_IDS: tuple[tuple[int, int], ...]
```

The first cutover preserves the current schema lineage and advisory identities, including `(1095980114, 60001)` and `(1095980114, 60002)`. AstralDeep checks this metadata against `config/astral-composition.json` before admitting traffic.

## Boot and migration API

Conceptual public API:

```python
class PlaneRuntime(Protocol):
    def inspect_compatibility(self) -> CompatibilityReport: ...
    def initialize(self, *, expected_revision: str) -> InitializationReport: ...
    def reconcile(self, reconciler: ProductReconciler) -> ReconciliationReport: ...
    def health(self) -> PlaneHealth: ...
    def close(self) -> None: ...
```

Rules:

1. One explicit application-boot initializer obtains the migration advisory lock.
2. Schema changes and deterministic database-only backfills run in declared order and are repeat-safe.
3. Product reconciliation is invoked separately by Deep; Projection owns any UI/tutorial seed source.
4. Ordinary repository or connection construction never runs migrations after the compatibility facade is removed.
5. Startup remains closed until schema and required reconciliation reach a compatible state.
6. Existing representative data is used in migration tests; an empty database is insufficient.

During the first compatibility slice, `backend/shared/database.py` may re-export/wrap the Plane implementation so call sites can move in controlled clusters. The facade is temporary, contains no independent SQL truth, and is deleted before final ownership qualification.

## Transaction API

```python
class Transaction(Protocol):
    def execute(self, statement: Statement, parameters: Parameters = ()) -> CommandResult: ...
    def fetch_one(self, statement: Statement, parameters: Parameters = ()) -> Record | None: ...
    def fetch_all(self, statement: Statement, parameters: Parameters = ()) -> tuple[Record, ...]: ...
    def savepoint(self, name: str) -> Savepoint: ...

class PlaneDatabase(Protocol):
    def transaction(self, *, isolation: IsolationLevel | None = None) -> ContextManager[Transaction]: ...
```

- `CommandResult` is detached before the connection returns to the pool; no cursor or live connection escapes.
- Plane SQL uses native psycopg placeholders. No lexical `?`/`%` translation runs over SQL text.
- Repository methods declare transaction ownership. A caller-provided transaction is never silently committed by a nested repository.
- Owner, expected version, fencing token, and idempotency predicates are part of the write statement where applicable.
- Failure raises typed Plane errors with safe metadata; it never returns fake success or swallows a commit failure.

## Repository contract

Each repository exposes neutral records and methods rather than Deep domain types. Required invariants:

- every owner-scoped read/write includes `owner_id` (or an explicit system namespace) in its database predicate;
- versioned writes use compare-and-set and report conflict distinctly;
- durable operation IDs are unique and safe to replay with identical semantics;
- atomic publication/admission/effect-ledger operations remain within one transaction;
- ordering/sequence columns are monotonic under the existing locking/fencing model;
- returned records are immutable values, not driver rows tied to a connection;
- raw SQL and psycopg imports do not remain in AstralDeep production modules after final cutover.

## Durable outbox

Plane owns storage mechanics:

```python
enqueue(transaction, entry)
claim(worker_id, topics, now, lease_duration, limit) -> tuple[ClaimedEntry, ...]
ack(entry_id, worker_id, expected_version)
retry(entry_id, worker_id, expected_version, available_at, error_code)
dead_letter(entry_id, worker_id, expected_version, error_code)
reclaim_expired(now, limit)
```

Deep registers and executes topic handlers after all relevant policy checks. Enqueue occurs in the same transaction as the authoritative state change. Payloads are canonical, bounded, digest-checked, and contain no credentials or unnecessary user content.

Initial migrations include:

- audit publication retries from the lossy JSONL path;
- durable attachment/artifact purge tombstones;
- LETS binding/lifecycle/effect operation follow-up work where an external call is required.

## Audit retention

Before pruning a hash-chain prefix, Plane persists an authenticated `AuditRetentionAnchor` that contains the first retained sequence and the preceding digest. Verification starts from genesis only when genesis is retained; otherwise it verifies the anchor then the surviving suffix. A prune without a valid anchor fails.

## Blob and purge behavior

- Runtime roots are explicit host configuration outside source/submodule trees.
- First cutover preserves existing roots and mount paths; package location never determines durable storage.
- Path operations reject traversal and reparse/symbolic-link boundary crossings.
- Database soft deletion and purge-tombstone enqueue commit atomically.
- Physical deletion failure remains visible and retryable; account deletion cannot report physical purge complete prematurely.
- Logs redact raw locators where they could disclose user or host data.

## Recovery contract

The first repository cutover does not transfer data. Recovery selects a prior compatible AstralDeep/Plane composition against the same database and blob roots.

Before a schema migration:

1. Record exact composition and schema revision.
2. Take and verify the normal PostgreSQL/blob backup appropriate to the environment.
3. Confirm the target declares `read_compatible_from` for the current state.
4. Run migration and reconciliation under admission closure.
5. Verify representative records and blob referential integrity before opening traffic.

Rollback rules:

- Prefer forward repair when schema has advanced beyond an older reader's declared compatibility.
- Never infer or run destructive down-SQL.
- Restore from the verified backup only under the documented operator procedure.
- A later blob-root relocation quiesces writers, copies without traversing links, verifies count/size/SHA-256, switches atomically, and retains the old copy until rollback expiry.

## Extraction order

1. Pool/transaction/migration kernel and compatibility metadata.
2. Temporary `shared.database` facade and AstralDeep composition install.
3. Leaf repositories: attachments/artifacts, sessions/revocations, remote metadata, preferences/feedback/onboarding/personalization, encrypted LLM configuration.
4. Coupled cluster: work admission, conversations/history/workspace, scheduler occurrence/effect ledger, voice session/turn metadata, personal-agent runtime/publication.
5. Outbox, purge tombstones, audit anchor, and LETS durable records.
6. Explicit boot initialization and product reconciliation separation.
7. Remove facade; enforce no Deep raw SQL/psycopg.

## Verification

Plane owns unit/integration tests for:

- pool lifecycle, detached results, transactions/savepoints, and error typing;
- repeat-safe migrations and representative existing-data upgrades;
- indexes/constraints/advisory locks/schema metadata;
- every moved repository and owner-isolation denial;
- attachment/artifact storage and purge failure/recovery;
- audit append-only/retention-anchor verification;
- work-admission/scheduler/effect atomicity;
- workspace/history atomic publication;
- voice-session persistence without importing voice runtime;
- outbox concurrency, fencing, retry, reclaim, and dead-letter behavior;
- import/dependency and SQL-ownership guards;
- backup/recovery and compatibility rejection.

Deep retains cross-component orchestration, policy, authorization, transport, and end-to-end tests.
