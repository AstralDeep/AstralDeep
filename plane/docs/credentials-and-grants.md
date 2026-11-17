# Credentials and grants repository slice

This schema-neutral slice exposes the `066.001` credential and grant tables through three
typed, caller-transaction-owned repositories. It does not move rows or introduce a migration.

## User provider configuration selection

`EncryptedLLMConfigRepository.get_user_for_update(transaction, *, owner_id)`
returns the same opaque `EncryptedLLMConfigRecord | None` as the existing user
getter, while holding an existing row with `SELECT ... FOR UPDATE` until the
caller transaction commits or rolls back. Ordinary user upserts and deletions
must wait for that row lock, including delete-and-recreate operations; another
owner's row remains independently writable. The existing unlocked getters,
setters, and deletion behavior are unchanged.

The trusted host must set bounded SQL waits, retain the same transaction through
its dependent permit write, and compare the returned record to its original
opaque selection. A writer that committed before lock acquisition may cause the
getter to return changed state; the getter does not authorize that replacement.
Missing or wrong-owner rows return `None` without a gap lock, so the host must
refuse that selection rather than adopt a later insertion. No provider request,
decryption, new credential field, schema change, or admission policy is added.

## Public composition

- `create_credential_repository()` returns opaque user-agent and remote-machine credential
  storage over `user_credentials` and `machine_credential`.
- `create_offline_grant_repository()` returns encrypted refresh-token lifecycle storage over
  `user_offline_grant`.
- `create_share_grant_repository()` returns digest-bound immutable snapshot storage over
  `share_grant`.
- `create_framework_credential_repository()` returns hash-only, owner-issued bearer-credential
  storage over the additive `088.008` `framework_credential` table (see below).

All four are also available as `credentials`, `offline_grants`, `share_grants`, and
`framework_credentials` in `RepositoryCatalog`.

## Security and transaction boundary

Plane never receives plaintext credentials, raw share tokens, Keycloak access tokens, or an
encryption key. The embedding product encrypts or hashes before calling a repository and retains
authorization, credential-key policy, token exchange, PHI disclosure policy, rendering, and audit
event decisions. Opaque ciphertext and immutable share snapshots are marked non-representable on
detached records so routine logs do not disclose them.

Ordinary credential and offline-grant methods require `owner_id`. Creating a machine credential
also proves the machine belongs to that owner through `remote_machine`; a conflict cannot transfer
a credential between owners. The deliberately cross-owner user-credential page is named
`list_agent_credentials_for_reencryption` and is bounded and cursor-paged for an already-authorized
administrative migration worker.

Offline token bytes are returned only by `get_active_for_exchange`, which requires both owner and
grant identity and applies the live/expiry predicate. `find_latest_valid` returns token-free
metadata and preserves the legacy preference for an agent-specific grant before the owner's newest
valid fallback.

Public share resolution is intentionally capability-scoped by the caller-supplied SHA-256 digest;
unknown, revoked, and expired grants all produce `None`. `record_open` repeats the digest,
revocation, and expiry predicates in the increment itself, preventing a resolve/revoke race from
counting or auditing a stale open. Owner listing omits both digest and snapshot content.

## Replay, CAS, and revocation

- Offline grant IDs, share digests, and initial machine credentials accept only exact immutable
  replay. Reuse with changed semantics raises `RepositoryConflictError`.
- User ciphertext re-encryption and machine credential replacement use explicit persisted
  timestamp compare-and-set fences. A legacy nullable user-credential timestamp can be advanced
  through `IS NOT DISTINCT FROM NULL` once.
- Single-grant revocations return `revoked`, `already_revoked`, or `missing`; owner-wide offline
  revocation changes only live rows and returns the transition count.
- `replace_refresh_token_if_current(transaction, owner_id, grant_id,
  expected_encrypted_refresh_token, encrypted_refresh_token, as_of)` replaces
  opaque credential state only when owner, exact prior ciphertext, unrevoked
  state and expiry predicates all match. It returns the detached new record or
  `None`; it cannot reactivate a revoked grant. This also supports a product-owned
  encrypted reference to a canonical credential without copying refresh tokens.
  The caller owns encryption, exact reference validation and the external exchange.
- `history.sessions.delete_and_return(transaction, owner_id, session_id)`
  atomically returns the encrypted session record that was actually deleted.
  Product logout uses those final credential bytes for best-effort IdP revocation;
  a prior read or process cache cannot safely supply them during rotation.
- Deletes are owner-scoped and idempotent where the legacy caller treats absence as success.

The repositories never borrow or commit a connection. Callers that combine a durable mutation
with Deep-owned audit or authority work must pass the same Plane transaction to every repository
operation.

## Exact interactive execution observation

`history.sessions.get_execution_state(query, owner_id=..., session_id=...)` reads
only the specified owner/session and samples PostgreSQL `clock_timestamp()`. It
returns `None` for absence, otherwise an ephemeral `SessionExecutionState` with
`credential` and `observed_at`; this unlocked read grants no execution authority.
It has no cache, administrative or latest-owner fallback.

`SessionCredentialFence` is version 1 and binds `owner_id`, `session_id`,
`created_at`, `interactive_anchor`, `hard_expires_at`, `refresh_generation`, and
`encrypted_state_binding`. The last field binds both already encrypted credential
values, so deleting/recreating a session with reused timestamps and different
ciphertext cannot revive an old observation. A new observation alone cannot prove
an assignment's original session incarnation: its existing authority metadata
stores only the selected session ID. The host must retain and validate the issued
incarnation before minting such a new observation. `resumed` is excluded because the
reconnect marker does not rotate credentials. Generation is a monotonic counter,
not a wall-clock expiry. `execution_fence(SessionRecord)` derives this typed fence;
no plaintext credential or key enters Plane.

The host captures the database `observed_at` before remote validation/refresh,
performs the existing external exchange without a transaction, verifies current
IAM/roles and persists rotated credentials, then supplies a version 1
`SessionExecutionObservation(credential, started_at, valid_until)`. The observation
uses the exact persisted post-refresh fence and the original pre-exchange database
time. Its aware timestamps must define a positive window no longer than 15 seconds;
future or expired observations refuse execution. It must not be persisted, logged,
sent to clients, synthesized from JWT `sid`, or treated as evidence of remote IAM
validation by itself. Deep owns issuance and the exact signed-cookie reference.

`assert_current_execution(transaction, observation=...)` locks owner retirement,
then the exact session row, and samples database time after the wait. It checks
owner activity, exact credential identity, interactive anchor, hard expiry and the
observation window. Hold the same transaction for guarded writes and recheck after
subsequent lock waits. `compare_and_set_refresh(..., expected_credential=...)`
optionally applies the same exact credential/owner/lifetime guard before advancing
the existing refresh generation. It refuses changes to immutable session identity;
the legacy signature remains available to existing callers.

All three observation/fence DTOs are exported by `astralplane.repositories.history`.
Their credential fields are excluded from routine representations. Error messages
are closed and contain neither ciphertext nor caller-supplied credential fields.
This schema-neutral primitive provides local serialization and bounded freshness;
it does not perform Keycloak calls, prove remote freshness itself, enable an ingress
or runner, or close the host's remaining current-authority integration.

## Framework credentials (088.008)

`framework_credential` is an additive `088.008` table: an owner-issued, hash-only
bearer credential for an external caller — an SDK, MCP, or A2A client — that
outlives the single request that minted it. Plane never receives or persists
the plaintext token; the caller hashes the generated secret with SHA-256 before
calling `issue`, and only that hex digest plus a short non-secret display
prefix (`token_prefix`) are ever stored.

A framework credential is **independently issued, never delegated**: its scope
set, allowance and owner-lifetime expiry are fixed at mint time from the
owner's OWN currently-live authority, and it carries no parent reference. This
is the opposite shape from an attenuated authority binding or a delegation
chain, where every capability traces back through a parent's lineage and can
be narrowed but never independently re-issued. `tests/authority/
test_issuance_vs_delegation.py` pins the two shapes apart at the model level so
they can never be structurally confused.

### Closing the pre-lock-authority defect

A prior reference implementation read "is the issuer still valid" and computed
the expiry BEFORE acquiring any lock, so a concurrent revoke or owner
retirement racing the mint could lose the race and still see a credential
appear. `FrameworkCredentialRepository.issue` closes this: it takes the SAME
owner advisory-lock domain (`pg_advisory_xact_lock(hashtextextended(owner_id,
79))`) that `AssignmentRepository.create_operation` uses, re-reads the owner's
retirement state and the named issuer (a live `web_session` incarnation today,
for either issuer kind) INSIDE that lock, and computes `expires_at` from
`clock_timestamp() + ttl_seconds` only after both checks pass. A revoke or
owner-retirement committed while `issue` waits on the lock is always visible
to it once the lock releases; `tests/repositories/
test_framework_credentials_postgres.py` proves this with real contending
transactions using `pg_blocking_pids`, not a timing guess.

### Lifecycle

- `issue(transaction, *, owner_id, credential_id, name, scopes, token_hash,
  token_prefix, issuer_kind, issuer_reference, max_admissions, ttl_seconds)` —
  `scopes` must be a non-empty subset of the closed
  `FRAMEWORK_CREDENTIAL_SCOPES` vocabulary (`operations.submit`,
  `operations.read`, `operations.control`, `artifacts.read`, `agents.read`);
  `issuer_kind` is `session_incarnation` or `native_credential`, both
  currently verified through the same live `web_session` incarnation
  mechanism (the kind only records, for audit, which kind of caller Deep
  observed); `max_admissions` is 1..10000; `ttl_seconds` is 1..7,776,000 (90
  days). Returns a `FrameworkCredentialRecord` that never carries the hash.
- `revoke(transaction, *, owner_id, credential_id)` — owner-scoped, idempotent
  (revoking an already-revoked credential returns it unchanged); raises
  `RepositoryNotFoundError` for a foreign or missing id.
- `list_for_owner(query, *, owner_id)` — bounded, owner-scoped, hash-free.
- `capture_execution_observation(transaction, *, owner_id, credential_id,
  token_hash, scopes=None)` — samples PostgreSQL time before acquiring the exact
  credential row and returns the existing frozen `FrameworkCredentialObservation`.
  Its unchanged 15-second window includes lookup and lock waits; a long capture,
  future observation, revoked/expired key, foreign owner or wrong hash is refused.
  Optional scopes intersect the current grant with the caller's attenuation and
  cannot widen it. No caller timestamp, host-clock tolerance or retry is accepted.
  This read-only capture creates no credential or admission and does not replace
  the same-transaction execution check after later waits. Keep observations
  ephemeral and out of logs, durable data and client responses.
- `consume_admission(transaction, *, owner_id, credential_id)` — a
  compare-and-set that charges exactly one admission, refusing
  (`credential_allowance_exhausted`) a revoked, expired, or exhausted
  credential without ever double-charging a race's loser.
- `assert_current_execution(transaction, *, observation)` — never persisted.
  Locks the exact credential row `FOR UPDATE`, re-validates a fresh
  `FrameworkCredentialObservation` (database-captured, valid at most 15 seconds,
  the same freshness discipline as `SessionExecutionObservation`), and refuses
  a revoked, expired, owner-mismatched, or hash-mismatched (replaced)
  credential. `FrameworkCredentialFence`/`FrameworkCredentialObservation` are
  exported from `astralplane.repositories.history`, mirroring the session
  execution-observation shape; their `token_hash` field is excluded from
  routine representations.

### Execution adapter

A persistent one-shot assignment's `operation.authority` may declare
`origin="framework"` + `reference_kind="credential"` (previously refused
outright by `AssignmentRepository._executable`). `_lock_execution_authority`
now branches on the selected authority's origin: `interactive` continues to
require a matching `SessionExecutionObservation`; `framework` requires a
matching `FrameworkCredentialObservation` whose fence names the SAME owner and
credential id as the persisted operation, re-verified via
`FrameworkCredentialRepository.assert_current_execution` inside the caller's
lock. `create_operation` binds the receipt's `credential_id` to the exact
issuing credential (`_operation_spec`'s `framework` → `credential` reference
kind, already receipt-tested by `test_one_shot_framework_receipt_is_bound_to_
issuing_reference`) and, for a framework-origin operation, `_assert_creation_
authority` verifies that same observation before any row commits — an
unverified or foreign-typed authority (for example a session observation
presented for a framework-origin operation) is refused exactly like an
unverified interactive one. `scheduled`/interactive `delegation` reference
kinds remain refused exactly as before.

### Finite offline-grant allowance

`user_offline_grant` gains two additive nullable columns, `max_admissions` and
`consumed_admissions` (both `NULL` on every pre-`088.008` row and on any new
grant created without an explicit limit — unlimited-admissions legacy
semantics, unchanged). `OfflineGrantRepository.create_grant(..., 
max_admissions=None)` accepts the new optional keyword (defaulting
`consumed_admissions` to `0` only when a limit is given), and
`consume_admission(transaction, *, owner_id, grant_id, as_of)` is the matching
compare-and-set: a grant with `max_admissions IS NULL` always succeeds
(unlimited), otherwise it charges exactly one admission or raises
`RepositoryConflictError("offline grant allowance exhausted")` without
touching a revoked or expired grant.

### Usage charge basis (additive, FR-022)

`AssignmentResourceAmount` gains an optional `basis: Mapping[str, str] | None`
field: a per-dimension charge-provenance annotation (`observed`, `estimated`,
`uncertain`, or `none`) over the same dimension keys as the amount itself
(`model_calls`, `tool_calls`, `tokens`, `elapsed_ms`, `spend_micro_units`).
It never introduces a duplicate counter and never changes how "unknown ≠
zero" is represented — that remains `None` on the amount field itself; `basis`
only annotates HOW a *populated* value was determined. Absent on every legacy
row (`basis=None`), and `AssignmentRepository._amount` validates it only when
present, so old persisted amounts decode unchanged.

## Rollback and compatibility

The `066.001`-era slice above uses columns already present in the extracted baseline, so its own
rollback is a code composition rollback only. `088.008` (`framework_credential` and the two additive
`user_offline_grant` columns) is a forward-only additive migration. A composition that predates
`088.008` refuses the upgraded schema rather than reading it, so recovery is a guarded forward
repair or the joint database/durable-root restore in the `088.008` section of
`migration-and-recovery.md`; never drop the table or columns by hand. Existing nullable
user-credential timestamps and nullable offline-grant bookkeeping timestamps remain readable. No
ciphertext, snapshot, or framework-credential hash should be copied into diagnostic evidence.
