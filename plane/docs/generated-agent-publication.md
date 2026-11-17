# Generated-Agent Publication Journal

AstralPlane owns the durable PostgreSQL authority for generated-agent publication. The product
composition owns authorization, bounded startup scheduling, local cancellation, and the later
decision to activate a prepared revision. Immutable filesystem publication remains a separate
commit domain: a database transaction and a filesystem rename are never described as atomic.

The factory is `create_generated_agent_publication_repository()`, also available as
`create_repository_catalog().generated_agent_publications`. The supported import surface is
exported from both `astralplane` and `astralplane.api`:

- `GENERATED_AGENT_BUNDLE_CONTRACT`, `ImmutableBundleStore`, `FinalizedBundle`,
  `StagedBundleReceipt`, `BundlePublicationKey`, `BundlePublicationPaths`,
  `BundlePublicationReceipt`, `BundleRecoveryDisposition`, `BundleRecoveryResult`,
  `PublishedBundle`, the storage error types, `canonical_bundle_digest()`,
  `runtime_metadata_for_manifest()`, and `paths_for()`;
- `GeneratedAgentPublicationRepository`, `GeneratedAgentPublicationIntent`,
  `GeneratedAgentPublicationOperationBinding`, `GeneratedAgentPublicationResultMetadata`,
  `AgentRevisionRecord`, `DraftAgentRecord`, and `DraftPublicationRecord`;
- `ExecutionFence`, `OperationRequest`, `OperationRecord`, `OperationOwner`, `AdmissionClass`,
  `OwnerScope`, and `OperationState`; and
- the original/recovery operation constants plus
  `generated_agent_publication_operation_binding()`,
  `generated_agent_publication_recovery_operation_binding()`, and
  `generated_agent_publication_paths()`.

Callers copy a returned operation binding into an `OperationRequest` exactly. A recovery operation
is a deterministic child of the journal's retained prior operation and its exact state revision;
arbitrary operations, missing parents, sibling attempts, and changed idempotency identities are
not recovery authority.

## Exact bundle and path contract

`begin_intent()` accepts only a `FinalizedBundle` using `GENERATED_AGENT_BUNDLE_CONTRACT`. That
typed value has already checked the complete v2 manifest shape, canonical JSON plus one LF, exact
ordered four-file inventory, each file's UTF-8 digest and byte size, bundle digest, runtime version,
runtime lock, and safe agent/revision identities. The coordinator then fences those values against
the target revision and operation digest; generic `AgentRepository.create_revision()` remains
manifest-agnostic.

The only accepted paths are derived rather than caller-designed:

```text
staging/<draft_uuid>/<source_state_revision>/<publication_id>
revisions/<target_agent_id>/<target_revision_id>
```

Absolute paths, drive prefixes, backslashes, empty/dot/dot-dot components, leading-dot aliases,
duplicate separators, changed source revisions, and changed target components fail closed. The raw
draft publication insert/transition primitives are private capability-scoped internals, so catalog
consumers cannot bypass these coordinator checks.

## Transaction and filesystem sequence

All repository calls accept a caller-owned Plane `Transaction`. Use the storage engine's split
stage/promote API so no database call occurs while its global filesystem lock is held:

1. Claim generation. During long pre-publication model or validation work, call
   `DraftAgentRepository.renew_generation_claim()` with the exact owner, draft ID, lifecycle
   revision, and claim UUID. PostgreSQL time decides liveness and expiry; renewal never shortens a
   lease or increments the lifecycle revision.
2. Create the target `user_agent` when needed, create the exact designated operation, then call
   `begin_intent()`. One DB transaction verifies the owner, live claim, source revision, operation,
   bundle, compatibility state, and exact paths, and creates or exactly replays both the journal
   row and non-routable `prepared` revision. It never changes `user_agent.active_revision_id`.
3. Call `assert_current_attempt()` in a DB transaction immediately before storage work. Then call
   `ImmutableBundleStore.stage()` without a database-backed callback. Its optional in-lock callback
   is only for local cancellation/revocation. After it returns, call `mark_staged()` in a new DB
   transaction.
4. Call `mark_validated()` with the staged receipt's exact artifact/manifest digests and one bounded
   `GeneratedAgentPublicationResultMetadata`. In one DB statement it advances the journal and
   persists `error_message`, `security_report`, `validation_report`, and `required_credentials` on
   the exact still-claimed draft without changing its lifecycle revision. These values are durable
   before promotion, so a crash after the native move cannot lose validation evidence.
5. Call `assert_current_attempt()` again, then `ImmutableBundleStore.promote_staged()` without a
   database-backed in-lock callback. Re-open and verify its exact receipt. Finally call
   `commit_published()`. Its single PostgreSQL statement consumes/fences the already-persisted
   result values, marks the journal `published`, clears the exact claim, sets the draft status to
   `generated`, and records `published_revision_id`. The revision remains `prepared` for the
   product-owned activation flow.
6. On terminal storage/validation error, call `fail(failure_code=...,
   safe_error_message=...)`. One statement fails the journal and prepared revision, clears only the
   exact current claim, and stores the separate user-safe message rather than exposing a snake-case
   machine code as UI text.

There is intentionally a race between the last DB fence and the separate filesystem commit. The
durable journal and startup recovery close crash windows; they do not manufacture cross-domain
atomicity. Every state change uses `state_revision` plus owner/source/claim/target/path/digest and
operation-generation predicates. `renew_generation_claim()` is available on the journal after
intent creation and additionally authenticates the bound current attempt and prepared revision.

## Startup and replay

`list_reconcilable_for_administration(limit=...)` returns a deterministic bounded inventory of
`claimed`, `staged`, and `validated` rows. Startup code obtains an exact recovery child binding,
submits/claims it through work admission, and calls `rebind_recovery_attempt()`. The CAS requires a
terminal retained prior operation (or a higher reselection generation of the same operation), exact
parent/idempotency/digest lineage, the unchanged prepared revision, and the unchanged draft claim.
Multiple replicas using the same deterministic child fence converge on one journal revision.

After rebind, recovery calls `assert_current_attempt()` outside the filesystem lock, invokes
`ImmutableBundleStore.recover()` with only a local cancellation callback, and applies the appropriate
journal transition afterward. A `validated` row can call `commit_published()` without reconstructing
result metadata; Plane consumes the values persisted by `mark_validated()`.

Nonterminal transition replays require the currently bound live attempt and DB-time live claim.
Terminal replay authenticates the journal's stored operation ID, execution generation, owner, exact
operation identity, and retained terminal/running token state; it does not require an unrelated live
execution. An unrelated operation cannot replay a terminal result. Supplying result metadata on
`commit_published()` is optional but, when supplied, must exactly match the durable values.

Lookup APIs are `get_by_source()` for `(owner_id, draft_uuid, source_state_revision)` and
`get_by_target_revision()` for `(owner_id, target_agent_id, target_revision_id)`.

## Existing schema

No migration or digest update is required. The contract uses the existing 074.004 relations and
columns:

- `draft_artifact_publication`: source/claim/target/operation/path identities, validated
  `artifact_digest` and `manifest_digest`, lifecycle timestamps/state/failure code, and
  `state_revision`;
- `draft_agents`: `state_revision`, `generation_claim_id`, `generation_claim_expires_at`,
  `target_agent_id`, `status`, `error_message`, `security_report`, `validation_report`,
  `required_credentials`, and `published_revision_id`;
- `user_agent_revision`: prepared state, manifest JSON, artifact path/digest, runtime and lock
  identities, compatibility state, promotion token, and failure state; and
- `operation_record`: owner, operation kind, parent, idempotency namespace/key/input digest,
  execution generation/token, terminal state, and retention.

The manifest digest needs no additional column on `user_agent_revision`: it is reconstructed from
the stored JSON object using canonical UTF-8 JSON followed by exactly one LF, matching the generated
`manifest.json` byte contract.
