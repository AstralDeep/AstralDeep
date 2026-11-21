# SQLite composition and recovery

AstralPlane 0.1.12 adds a SQLite backend for personal nodes. PostgreSQL remains the institutional
backend. The public repository catalog is shared; callers continue using Plane-owned transactions
and repositories rather than obtaining a driver connection.

## Composition

Use `create_sqlite_runtime(database_url, identity=..., reconcilers=...)`, or use
`create_runtime(...)` to select SQLite for a `sqlite:` URL and PostgreSQL for a PostgreSQL DSN.
Call `initialize()` before admitting durable work and call `close()` after draining callers.
Required host reconciliation hooks retain the same durable, fail-closed completion contract.

A SQLite URL names an absolute persistent file, such as
`sqlite:///absolute/private-directory/plane.db`. Provision the directory first. In-memory databases,
URL authorities, query parameters and fragments are refused. SQLite 3.39 or newer is required;
no database server or extra Python SQLite dependency is needed. The driver enables foreign keys,
WAL journaling and `synchronous=FULL`, bounds pool checkout and write waits, and creates database,
WAL, shared-memory and reconciliation-lock files with owner-only permissions.

The SQLite registry begins from an empty database at schema `089.022`. It does not import a
PostgreSQL database or accept PostgreSQL's historical migration lineage. Use
`SQLITE_DATA_PLANE_REVISION`, `SQLITE_MIGRATION_DIGEST` and `SQLITE_MIGRATION_REGISTRY` for its pin;
the existing unprefixed registry and digest continue describing PostgreSQL. A SQLite runtime's
`inspect_compatibility()` reports its own digest and readable revision set. Pin both the exact
package/component artifact and the selected backend's registry evidence in the host composition.

The baseline installs every repository table, index and trigger, declared scalar-type guards,
and the same initial admission classes and slots as PostgreSQL. Registry evidence binds that
DDL, deterministic seed data and installed Python guard-function source. Reopening verifies
the exact registry digest and schema structure, including quoted literals. Unexpected objects,
foreign databases, unrecognized revisions, altered guard code and missing evidence are refused.

## Transactions and contention

Ordinary reads use SQLite WAL snapshots and do not reserve the writer. Writes, advisory guards
and locking reads reserve the real database writer until the caller commits or rolls back.
Missing or foreign-owner locked lookups do not reserve a writer for an unrelated source.
Try-lock functions return the result of an actual reservation attempt; session reconciliation
uses a separate bounded cross-process file lock that survives durable marker commits.

SQLite has one writer for the entire file. PostgreSQL can concurrently lock or update independent
owners and rows; SQLite may refuse or delay the second transaction instead. A snapshot that became
stale before a write cannot be upgraded successfully: the caller receives a bounded lock failure
and must retry the whole authorized operation in a new transaction. A failed transaction must never
be reused to approve work from its old snapshot. PostgreSQL-only physical catalog and row-lock
scheduling assertions are identified separately from shared repository behavior checks.

`is_lock_contention(error)` is the public classifier for genuine PostgreSQL lock-not-available
errors and SQLite's `LockContentionError`, including unsuppressed repository wrappers. A redacted repository wrapper preserves known contention in the public error type before hiding
native details. A code string or an unrelated repository conflict is insufficient evidence. Classification does not authorize a
retry: the host must retain its own attempt/deadline bound and ensure the authorized callback has
not started external effects. Deep's adoption of this portable classifier remains unqualified.

Lock/statement timeout settings, transaction and statement clocks, savepoint rollback, and
transaction-local settings retain their respective lifetimes. Unsupported PostgreSQL constructs
raise an explicit SQL contract error; raw PostgreSQL SQL is not a backend-independent API.
Rolling back a savepoint undoes its changes and settings; SQLite retains its file-level writer
reservation until the outer transaction ends.
Statement deadlines remain attached to a cursor through every result step and internal locking-read
probe. Expiry before or after a short step raises `TimeoutError` without fabricated native metadata;
SQLite VM cancellation retains its genuine interrupt error. Idle cursors leave no connection-wide
progress handler installed.

## Backup and restore

Close admission, drain transactions and close every runtime attached to the file. Back up the
database and all paired durable blob roots as one recovery unit while writers remain quiesced.
Do not copy a live WAL database's main file alone. A cold copy must come from fully closed SQLite
connections; a live database backup requires SQLite's consistent backup API.

Restore the verified database and paired blob roots together under closed admission, retain the
same encryption/audit keys, and reconcile post-backup revocations, forgotten/expired data and
external effects before reopening. PostgreSQL endpoint-readiness and joint-restore fence evidence
remain PostgreSQL contracts; SQLite does not manufacture PostgreSQL server or timeline identities.

For restored-session retirement, call `retire_restored_sessions(...)` with the SQLite URL, the
canonical absolute file path as `expected_database`, `main` as `expected_schema`, schema `089.022`,
and the exact `SQLITE_MIGRATION_DIGEST`. The file must already exist. This path validates the
target, schema structure and metadata without initialization or migration, retires every owner's
restored session in one writer transaction, and runs again for every restore rather than trusting
a historical completion marker. Failure returns no completion result and rolls back the deletion.

## Qualification

Run the shared contract and caller-rollback suites against each backend, followed by repository
tests and changed-line coverage. The same fixture harness selects SQLite when
`ASTRALPLANE_TEST_POSTGRES_DSN` contains a SQLite URL; the harness creates disposable migrated file
copies. For example:

```sh
ASTRALPLANE_TEST_POSTGRES_DSN=sqlite:///fixture-template python -m pytest -q \
  tests/contract tests/integration/test_catalog_caller_rollback.py tests/repositories \
  tests/test_sqlite.py tests/test_sqlite_functions.py
```

Repeat with an isolated PostgreSQL administrator DSN to qualify the preserved backend. The fixture
environment variable retains its historical name; it never selects an application database.
