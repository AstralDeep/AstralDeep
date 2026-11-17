# External-host integration example

`examples/external_host_example.py` is a standalone, executable example for teams that embed
AstralPlane in an independently implemented service, such as an MCP or A2A server. It imports only
the public facade (the `astralplane` package with its `api`, `errors`, and `repositories` modules)
plus the standard library, and it never imports another Astral product. The example is
documentation in executable form: every scenario below is one function in that file, and
`tests/integration/test_external_host_example.py` runs the example against a real, test-owned
PostgreSQL instance so the documented boundary cannot drift from the code.

## Running the example

The example requires operator-provided inputs and provisions nothing by itself:

- `--database-url`: an empty PostgreSQL database that the caller owns. The first boot installs the
  schema-only baseline and applies every required registry edge under the migration advisory lock,
  then runs the required reconciliation hooks under the reconciliation advisory lock. Admission
  stays closed until both finish.
- `--blob-root`: an absolute filesystem path outside every source, package, and submodule tree.
  The factory refuses relative roots; the operator owns the root and its contents.
- `--refusal-database-url` (optional): a second database containing a non-Plane schema, used to
  demonstrate the fail-closed boot refusal.

```
python examples/external_host_example.py \
  --database-url "$EXTERNAL_HOST_DATABASE_URL" \
  --blob-root /srv/external-host/blobs \
  --refusal-database-url "$FOREIGN_DATABASE_URL"
```

The example prints one JSON report per scenario and a final summary line, and it exits nonzero on
any unexpected typed failure. The qualification test creates both databases and the blob root
outside the repository tree, removes them afterwards, and asserts the exact scenario outcomes.
It also invokes the CLI entry point in-process to measure its successful output, relative-root
refusal, typed boot failure, and resource cleanup through the required Python coverage gate;
the separate standalone subprocess remains an executable smoke check.

## Public API stability boundary

The example exercises exactly the facade surface an external host may rely on across a pinned
contract revision. Plane's compatibility contract is its contract version, schema revision,
read-compatible floor, migration registry digest, blob layout version, and recovery contract
version; a host re-pins deliberately when any element moves.

Each example runtime uses a fresh bounded boot identity because Plane shares readiness by identity
within the host process. A new runtime must not inherit readiness or a prior failed boot from a
different database. The reconciliation hook's durable name and version stay stable, so repeated
boots still verify the same idempotent reconciliation plan.

| Scenario | Facade entry points | Boundary demonstrated |
| --- | --- | --- |
| `boot` | `create_postgres_runtime`, `runtime.initialize`, `runtime.health`, `create_repository_catalog` | Guarded baseline install plus registry migration in one transaction, then reconciliation; ready health publishes only after both finish. |
| `boot_refusal` | `runtime.initialize` | A non-empty, unrecognized application schema is refused with `SchemaRevisionError` code `schema_revision_incompatible` (reason `schema_metadata_missing`); Plane never adopts or repairs an unknown schema. |
| `explicit_durable_root` | `create_streaming_blob_store` | Durable roots are explicit, absolute operator configuration; a relative root is refused with `SQLContractError` code `sql_contract`, and the factory never selects a root. |
| `owner_scoped_transactions` | `runtime.transaction`, `catalog.draft_agents` | Repositories run inside caller-supplied transactions and never commit or roll back themselves; the owner predicate lives in the SQL, so a foreign owner's read returns nothing. |
| `idempotent_replay` | `catalog.draft_agents.create_draft` | Re-submitting an identical operation returns the original record; a replay that changes an immutable identity is refused with `RepositoryConflictError` code `repository_conflict`. The host supplies stable identities (`draft_uuid`, `target_agent_id`). |
| `compare_and_set_fence` | `catalog.draft_agents.compare_and_set_draft` | Optimistic concurrency by expected revision: a stale fence is refused with code `repository_conflict`, and a foreign owner's fence is refused with `RepositoryNotFoundError` code `repository_not_found`. |
| `caller_rollback` | `runtime.transaction` | The host aborts its own transaction with its own exception; Plane rolls the transaction back and re-raises, and nothing is committed. |
| `explicit_reconciliation` | `runtime.reconcile` | Re-running reconciliation is idempotent under the reconciliation advisory lock and reports the hook as already durably complete. |
| `shutdown` | `runtime.close`, `runtime.health` | Closing the runtime closes the bounded pool, subsequent work is refused with `InitializationError` code `initialization_failed`, and repeated `close` calls are safe. |

Typed failures throughout are `PlaneError` subclasses carrying a bounded `code` and non-sensitive
`metadata`; hosts branch on the code, never on message text.

## Mapping MCP/A2A identities to neutral owners

Plane never sees a protocol. The host terminates the protocol itself — an MCP session's
authenticated principal or an A2A request's verified agent identity — and maps it, before any
Plane call, to one neutral `owner_id` string that is stable for that external identity. The
mapping is host policy: it decides which protocol subjects share an owner, and it can be rewritten
without touching Plane because no `owner_id`-keyed row encodes protocol semantics.

- Plane receives only bounded, non-sensitive `owner_id` strings such as the example's
  `ext-host-owner-…` values; credentials, tokens, and protocol metadata never reach Plane, which
  stores ciphertext and digests only.
- Authentication, authorization, confirmation, audit decisions, and rendering remain in the host.
  Plane persists what the host's already-authorized calls record, inside transactions the host
  owns.
- Cross-owner reads are impossible by accident: ordinary statements carry the owner predicate, and
  the only cross-owner paths are the explicit `..._for_administration` methods.
- The host adds no protocol library to Plane, Plane opens no service port, and Plane grows no
  second schema framework: the host process imports Plane in-process, points it at one PostgreSQL
  database, and maps identities at its own edge.

## Limits

The example intentionally does not exercise blob publication flows, purge recovery, audit-chain
appends, or product policy; those are covered by the repository's own suites. The example is a
consumer-oriented onboarding artifact, not a claim that independent use is impossible to misuse,
and it runs no service listener of its own.
