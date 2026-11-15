# Identity, agent, draft, and tool-policy state

This repository slice exposes the durable mechanics required to remove identity/agent SQL from
AstralDeep without moving identity or authorization policy into AstralPlane. It uses tables already
present in the schema-only `066.001` compatibility baseline, so this slice itself adds no migration
edge. The current Plane schema is `089.022`; its registry digest includes later independent durable
changes.

## Public composition

`create_identity_repository()`, `create_agent_repository()`,
`create_draft_agent_repository()`, and `create_tool_policy_state_repository()` return stateless
repositories. The same instances are available as `runtime.repositories.identity`, `.agents`,
`.draft_agents`, and `.tool_policy_state`.

Every method accepts a caller-owned Plane transaction. Repository methods never borrow a
connection, commit, or run product callbacks. Deep can therefore compose ownership, lifecycle,
audit, outbox, and authority writes into one transaction.

## Isolation and concurrency

- Ordinary identity, user-agent, draft, host, runtime, request, selection, and permission methods
  require the opaque OIDC subject and include it in their SQL predicate.
- Deliberately global administrative inventories and cleanup sweeps are named explicitly; Deep must
  authorize those operations before calling them.
- User-agent, revision, runtime-instance, runtime-request, draft, and publication transitions use
  durable revision/state compare-and-set predicates. Stale writes raise a repository conflict.
- Agent IDs, revision IDs, runtime generations, request generations, draft UUID aliases,
  transition IDs, publication IDs, and claims cannot silently acquire different semantics on
  replay.
- Preference-document changes first materialize the row, then lock it with `FOR UPDATE`, so a tool
  selection or agent opt-out preserves unrelated theme and product settings.
- `AgentRepository.lock_owner()` supplies the existing owner advisory-lock identity for workflows
  that atomically touch several lifecycle tables.
- `AgentRepository.reconcile_validation_policy_for_administration(...)` serializes one product
  policy revision with a stable transaction advisory lock. An exact marker replay performs no
  writes; a changed marker atomically flags only live, mismatched, not-already-flagged agents and
  records the opaque product revision in `schema_meta`. Plane does not interpret that revision.

## Compatibility behavior

The repositories preserve all existing representations during cutover:

- `agent_ownership` remains the first-party visibility record and cannot transfer ownership through
  an upsert.
- `agent_trust` remains a storage-only marker; Deep decides who may mark or reset it.
- `agent_scopes`, legacy `tool_permissions`, legacy NULL-kind `tool_overrides`, and per-kind
  `tool_overrides` remain readable until Deep completes policy reconciliation.
- `draft_agents` retains legacy text/JSON columns while adding owner and revision fencing to every
  new write path.
- `user_agent`, `user_agent_revision`, `agent_host_session`, `agent_runtime_instance`, and
  `agent_runtime_request` retain their feature-060 foreign keys and generation constraints.

Rollback is code-only for this slice: restore the prior Plane/Deep composition while leaving the
unchanged schema and rows in place. No destructive DDL or data rewrite is required.

The administrative policy reconciliation has live PostgreSQL evidence for concurrent starters,
idempotent replay, exact affected-row reporting, owner-neutral selection, and caller-owned rollback.


## Web-session incarnation (`088.002`)

`runtime.repositories.history.sessions` is the public `SessionRepository`. New
`put(transaction, SessionRecord(..., incarnation_id=None))` inputs receive a PostgreSQL-generated
canonical UUID4. Callers must retain the returned record. An identical no-ID retry returns the
original incarnation; a supplied ID may replay only the exact existing record, never insert or
resurrect it. Rotation and resume retain this identity. Deleting and recreating the same SID,
including identical encrypted bytes and timestamps, issues a different identity.

`get_by_incarnation(query, *, owner_id, incarnation_id)` resolves exactly one owner and incarnation.
A caller also holding a SID must compare it with the returned row; no latest-owner fallback is an
execution reference. `compare_and_set_refresh` requires the observed incarnation on its input record;
`mark_resumed`, `delete` and `delete_and_return` require `expected_incarnation_id`. Refresh
cannot change the original creation time, interactive anchor or hard expiry, even when the optional
exact ciphertext fence is omitted. Conditional stale
cleanup never returns or deletes a replacement credential. Deliberate `delete_owner` and named
expired-session administration retain their existing owner-wide/time-wide scope.

`SessionCredentialFence` version 2 includes the incarnation and exact encrypted generation.
`get_execution_state` captures the row and database time. `SessionExecutionObservation` still
requires host forced-refresh/current-IAM verification outside Plane. The distinct
`SessionConsentObservation(credential, started_at, valid_until, version=1)` represents ordinary
host-authenticated consent, and is refused by execution guards. Its public
`assert_current_consent(transaction, *, observation)` must run before grant locks/writes and again
after any waits, before commit. Both guards lock owner then session, reject retirement/deletion or
rotation, and sample database time after locking. Observations must retain the original start time,
expire within 15 seconds, and remain within the exact session hard lifetime. They contain no tokens
or durable IAM claim. Exceptions must propagate out of the enclosing transaction so all grant
writes roll back; the method itself does not create a grant or own a transaction.

The host must preserve the originally issued incarnation across every await, continuation and
consent operation. This storage change alone does not upgrade old SID-only assignment authority,
legacy offline grants, ingress or runners. Matching host/operation-version integration and denial
qualification remain required before those execution paths can use incarnation authority.


## Session issuing identity (`088.003`)

`SessionRecord` appends nullable `issuing_issuer` and `issuing_client_id` fields. Both
null means legacy unknown binding; both present means an exact host-supplied pair.
Plane accepts nonempty strings of at most 2048 and 256 Unicode code points,
respectively, without leading/trailing whitespace, control characters or surrogate
code points. Strings are neither trimmed nor normalized. Plane does not interpret
an issuer URL, discover endpoints, authenticate a client, validate a JWT or grant
execution permission. The trusted host must establish all those facts before
supplying a bound issuance. Ciphertext is never decoded or inspected for metadata.

Only a genuinely new database-issued incarnation can select a new pair. Same-SID
issuance replay must match the complete original record. Refresh cannot add,
remove or change either field, including when the optional credential fence is
absent: the update matches both fields with null-safe equality and never assigns
them. Resume and reads preserve them. A stale reference cannot adopt a replacement
incarnation, even if the owner, SID, timestamps and ciphertext are otherwise equal.

`SessionCredentialFence` retains version 2 and appends the same nullable pair. For
null/null, its encrypted-state digest remains the exact legacy canonical JSON
array of the two opaque ciphertext strings. A bound pair extends that array with
one canonical object containing exactly `issuing_issuer` and `issuing_client_id`;
no plaintext token is hashed. The digest and explicit pair both participate in
execution and consent equality checks. Positional callers retain their old field
positions and defaults. Neither the new pair nor its digest is bearer authority.

Legacy null/null sessions remain eligible under the existing Plane execution
policy. This prerequisite does not silently tighten host authentication, enable a
native broker or convert stored Work/grant authority. The later coherent host
integration must decide where a verified bound issuer/client is required, while
preserving ordinary session compatibility. The additive schema still requires a
matching exact Plane/host composition; old binaries are not declared compatible.

Deferred revocation records append optional `issuing_issuer` after their existing
fields. The trusted host can enqueue the exact original issuer and client for a
bound session. A present issuer requires a nonempty exact client with the same
structural bounds as session metadata. Legacy records keep their null issuer,
with or without an existing client ID. Reading, retrying, and incrementing an
attempt preserve the pair; there is no relabel operation. The host must select
only a trusted configured issuer before network revocation and retain unavailable
work without guessing a realm.

The additive, schema-neutral
`RevocationQueueRepository.page_for_administration(query, *, limit=20, after=None,
ceiling=None)` supports finite retry cycles without changing the existing
`pending_for_administration` peek. Its immutable `RevocationQueuePage` contains
`records`, `next_cursor`, and `ceiling`. The cursor is an immutable
`RevocationQueueCursor(enqueued_at, queue_id)`, ordered by that exact pair.
These types are public in `astralplane.repositories.revocations`.

An initial request captures the maximum queue ID and its first page in one SQL
snapshot. Continuations retain that ID ceiling and use an exclusive cursor;
later IDs are excluded even if their enqueue timestamps are equal or backdated.
A null next cursor ends the cycle, including when other workers deleted its
remainder. Reset both cursor and ceiling to begin another cycle. An initially
empty queue also returns a null ceiling. Limit is an exact integer from 1 through
200; timestamp is an exact nonnegative signed-bigint value, and IDs/ceilings are
positive signed-bigint values. Boolean, string, fractional, malformed, or
unenclosed continuation inputs are refused before SQL.

The host can retain one cursor and ceiling per drainer and process one bounded
page per pass, wrapping at cycle end. Retained unavailable work therefore cannot
permanently occupy the first page, and continuous new enqueues cannot extend an
already captured cycle. These traversal fields grant no authority and are not a
queue claim or a transaction spanning passes. Concurrent workers may inspect
the same row; ordinary owner/attempt fences still govern mutation. Reads never
remove work, increment attempts, or alter issuer metadata. This successor adds
no schema, migration, dependency, or automatic retry policy.
