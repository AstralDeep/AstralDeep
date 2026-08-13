# Data Model: Astral Multi-Repository Decomposition and LETS Integration

**Feature**: `074-multirepo-lets-integration`
**Date**: 2026-08-13

The model has two layers:

1. **Composition/provenance records** committed with source and used by build, startup, rollback, and research tooling.
2. **Runtime authority records** persisted by AstralPlane and interpreted by AstralDeep policy.

All identifiers are canonical strings with explicit namespaces. All timestamps stored in runtime tables are timezone-aware UTC or integer nanoseconds where required by the LETS wire contract. All owner-scoped queries include the owner/tenant key in the predicate, not only in post-query filtering.

## 1. ComponentRevision

An immutable component selected by one AstralDeep composition.

| Field | Type | Rules |
|---|---|---|
| `component` | enum | `astral-projection`, `astral-plane`, `astral-primitives`, or `lets` |
| `repository` | string | Exact canonical `https://github.com/AstralDeep/<Repo>.git` URL |
| `path` | string | Exact canonical relative submodule path under `components/`; no `..`, absolute path, or case ambiguity |
| `commit` | SHA-1 hex | Exactly the Gitlink commit in the parent tree |
| `ref` | string/null | Informational signed tag or release, e.g. `v1.0.10`; never used as the mutable resolver |
| `contract_version` | semver-like string | Public component contract consumed by Deep |
| `artifact_digest` | SHA-256/null | Wheel, manifest, or release artifact digest when applicable |
| `qualified` | boolean | True only after the declared local qualification set passed |
| `evidence` | array of paths/digests | Candidate-bound evidence references; never raw secrets or user data |

**Validation**:

- Gitlink commit, submodule HEAD, and manifest `commit` must be identical.
- Repository URL must match `.gitmodules` after canonical normalization.
- A dirty or uninitialized component is not deployable.
- `lets.ref` is `v1.0.10` until a successor release is deliberately adopted.

## 2. SystemComposition

The exact join between one AstralDeep revision and its four components.

| Field | Type | Rules |
|---|---|---|
| `format` | string | `astral.composition/v1` |
| `astraldeep_contract_version` | string | Composition/host contract version; the containing AstralDeep commit is derived from Git or injected into the built artifact, never self-recorded |
| `components` | map | Exactly one `ComponentRevision` for each required component |
| `ui_protocol` | object | Projection protocol version and SHA-256 |
| `data_plane` | object | Plane contract version, schema revision/range, and migration-set digest |
| `primitives` | object | Package version and primitive-contract digest |
| `lets` | object | Release, API/receipt version, and scope-profile version; deployment policy/machine digests live in authenticated runtime configuration |
| `qualification` | external record | Required check IDs, status, evidence digest, and candidate identity; stored outside the self-describing manifest and joined by its digest |

The immutable composition identity is `(AstralDeep commit, canonical manifest SHA-256)`. The commit cannot be embedded in the committed manifest because doing so would create an impossible self-reference. Clean-build tooling injects the commit into OCI/application diagnostics, while qualification evidence records both values.

**State**:

`draft` -> `locally_qualified` -> `remote_candidate` -> `reviewable`

- A component change returns the composition to `draft` and invalidates prior candidate evidence.
- This feature never transitions a composition to `deployed` or `released`; those require later authorization.

## 3. LegacyBaseline

The refreshed default-branch state from which an ordinary replacement commit descends. It is provenance metadata, not a new Git ref.

| Field | Type | Rules |
|---|---|---|
| `repository` | string | Canonical repository identity |
| `source_ref` | string | Ref observed from the live remote, normally `refs/heads/master` |
| `commit` | SHA-1 hex | Resolved default tip before `main` creation/replacement |
| `observed_at` | RFC 3339 | Time of the read-only remote query |
| `replacement_commit` | SHA-1 hex/null | Must be a normal descendant when created |
| `ancestor_verified` | boolean | True only after `merge-base --is-ancestor` succeeds |

No archive tag or archive branch is created. Observed deleted/unmerged Plane tips may be logged as a risk but are not promised remote reachability.

## 4. ComponentCompatibility

Machine-readable compatibility assertions exported by each project.

| Field | Type | Rules |
|---|---|---|
| `component` | enum | Owning component |
| `contract_version` | string | Exact interface version |
| `requires` | map | Allowed ranges/digests of dependent first-party contracts |
| `provides` | map | Protocol, schema, package, assets, or receipt wire versions |
| `source_commit` | SHA-1 hex | Build source identity |
| `content_digest` | SHA-256 | Canonical digest over compatibility-relevant bytes |

Dependency direction is acyclic:

```text
AstralDeep -> AstralPlane
AstralDeep -> AstralProjection -> AstralPrimitives
AstralDeep -> AstralPrimitives
AstralDeep -> LETS public client/executor contracts -> external LETS warden
```

Plane has no first-party runtime dependency on the other four projects. LETS has no AstralDeep dependency. Projection has no AstralDeep dependency.

## 5. DataPlaneRevision

The Plane-owned durable compatibility point.

| Field | Type | Rules |
|---|---|---|
| `contract_version` | string | Library/repository contract version |
| `schema_revision` | string | Single current schema revision |
| `read_compatible_from` | string | Oldest schema revision safe for admission/startup migration |
| `migration_digest` | SHA-256 | Canonical digest of ordered migration definitions |
| `advisory_lock_ids` | array of integer pairs | Includes preserved migration/reconciliation identities |
| `blob_layout_version` | string | Logical storage layout, independent of host root path |
| `recovery_version` | string | Recovery procedure contract |

**State transition**:

`uninitialized` -> `migration_locked` -> `migrating` -> `reconciling` -> `ready`

Failure leads to `migration_failed` or `reconciliation_failed`; traffic remains closed. A rollback may select an older composition only if its Plane contract declares the resulting schema readable. Destructive downgrade is never inferred.

## 6. CommandResult

A detached return value for a Plane command after its transaction/connection scope ends.

| Field | Type | Rules |
|---|---|---|
| `row_count` | integer/null | Copied before connection release |
| `status_message` | string/null | Driver status copied before release |
| `returned` | immutable records | Optional materialized `RETURNING` rows, size-bounded by caller contract |

It never exposes a driver cursor, connection, or pool object.

## 7. DurableOutboxEntry

A committed request for a Deep-owned side effect following a Plane transaction.

| Field | Type | Rules |
|---|---|---|
| `entry_id` | UUID/string | Globally unique |
| `owner_id` | canonical owner key | Required for owner-scoped work; system entries use an explicit system namespace |
| `topic` | constrained string | Registered Deep handler name |
| `operation_id` | string | Stable idempotency key |
| `payload` | canonical JSON | No credentials or unbounded user content |
| `payload_digest` | SHA-256 | Verified before handler execution |
| `created_at` | UTC | Set in the creating transaction |
| `available_at` | UTC | Backoff scheduling |
| `attempt_count` | integer | Non-negative and bounded |
| `lease_owner` | string/null | Worker claim identity |
| `lease_expires_at` | UTC/null | Expired claims are reclaimable |
| `status` | enum | `pending`, `claimed`, `succeeded`, `retry`, `dead_letter` |
| `last_error_code` | string/null | Redacted structured diagnostic |

**Transitions**:

`pending` -> `claimed` -> `succeeded`

`claimed` -> `retry` -> `claimed`
`claimed` -> `dead_letter`

Ack and state change use compare-and-set fencing. A handler success for the same `operation_id` is idempotent.

## 8. PurgeTombstone

A durable record that database deletion and physical blob removal have not yet converged.

| Field | Type | Rules |
|---|---|---|
| `tombstone_id` | UUID/string | Unique |
| `owner_id` | canonical owner key | Required |
| `object_kind` | enum | attachment, artifact, knowledge object, generated agent artifact |
| `object_id` | string | Logical object identity |
| `storage_locator_digest` | SHA-256 | Hash of normalized locator; raw sensitive path not exposed in logs |
| `requested_at` | UTC | Database deletion time |
| `status` | enum | `pending`, `purged`, `failed`, `manual_review` |
| `attempt_count` | integer | Non-negative |
| `verified_absent_at` | UTC/null | Set only after safe absence verification |

A user-visible/account-deletion result cannot claim physical purge complete while any associated tombstone is not `purged`.

## 9. AuditRetentionAnchor

An authenticated bridge between a retained audit chain and a pruned prefix.

| Field | Type | Rules |
|---|---|---|
| `anchor_id` | UUID/string | Unique |
| `owner_or_chain` | string | Audit chain namespace |
| `first_retained_sequence` | integer | Positive |
| `previous_entry_digest` | SHA-256 | Digest immediately before first retained row |
| `retention_policy_digest` | SHA-256 | Policy authorizing cutoff |
| `created_at` | UTC | Before prefix deletion commits |
| `signature_or_mac` | bytes/string | Verified by the configured audit trust mechanism |

Verification begins from this anchor when a retained chain no longer begins at genesis.

## 10. AgentAuthorityBinding

Plane-persisted, Deep-interpreted association between an Astral agent and LETS authority.

| Field | Type | Rules |
|---|---|---|
| `binding_id` | UUID/string | Unique |
| `owner_id` | canonical owner key | Required in every lookup/update |
| `agent_id` | string | Unique with owner and rollout profile |
| `population` | enum | `dynamic`, `user_authored`, later explicitly approved values |
| `tenant_id` | string | Exact LETS tenant |
| `envelope_id` | string | Exact LETS envelope |
| `lease_id` | string | Current lease |
| `lineage_id` | string | LETS lineage |
| `subject_id` | string | Must equal the governed agent identity mapping |
| `policy_digest` | SHA-256-like LETS digest | Exact configured policy |
| `machine_digest` | SHA-256-like LETS digest | Exact state-machine definition |
| `config_epoch` | positive integer | Receipt fence |
| `capabilities` | sorted set | Only capabilities mapped from declared Astral scopes |
| `lease_sequence` | non-negative integer | Latest reconciled LETS sequence |
| `lease_expires_at_ns` | positive integer | LETS time domain |
| `state` | enum | See below |
| `created_at`, `updated_at` | UTC | Auditable |
| `version` | integer | Optimistic concurrency fence |

**States**:

`provisioning` -> `active` <-> `quiescent` -> `closing` -> `closed`

Any nonterminal state -> `revoking` -> `revoked`

Remote uncertainty -> `reconciling` -> prior confirmed state or terminal denial
Time expiry -> `expired`

There is never a transition from `closed`, `revoked`, or `expired` back to `active`. A successor lease creates a new binding generation.

## 11. AuthorityLifecycleOperation

A durable idempotent request to change one binding.

| Field | Type | Rules |
|---|---|---|
| `operation_id` | string | Stable LETS `request_id`; unique |
| `binding_id` | foreign key | Owner-scoped binding |
| `kind` | enum | `provision`, `spawn`, `renew`, `quiesce`, `resume`, `close`, `revoke`, `reconcile` |
| `expected_binding_version` | integer | Compare-and-set fence |
| `expected_lease_sequence` | integer/null | LETS fence where supported |
| `request_digest` | SHA-256 | Canonical non-secret request |
| `status` | enum | `pending`, `in_flight`, `succeeded`, `failed`, `uncertain`, `reconciled` |
| `remote_request_id` | string | Same as `operation_id` |
| `result_digest` | SHA-256/null | Canonical LETS result digest |
| `error_code` | string/null | Typed and redacted |
| `attempt_count` | integer | Bounded |

An `uncertain` mutation is reconciled using the same request ID or a read operation; it is not blindly replayed with a new ID.

## 12. ProtectedEffectOperation

The local intent and final outcome for a LETS-governed effect.

| Field | Type | Rules |
|---|---|---|
| `operation_id` | string | Stable across safe retry and equal to receipt `request_id` |
| `owner_id` | canonical owner key | Required |
| `agent_id`, `binding_id` | identifiers | Must match one active owner-scoped binding |
| `tool_id` | string | Canonical Astral tool |
| `astral_scope` | enum | One of six declared `tools:*` values |
| `lets_capability` | string | Exact configured mapping |
| `lets_transition` | string | Exact configured mapping |
| `executor_audience` | string | Exact final gateway |
| `nonce` | string | Unique per attempted effect |
| `effect_digest` | SHA-256 | Canonical authorized arguments/context; excludes secrets while binding security-relevant values |
| `expected_sequence` | integer | LETS concurrency fence |
| `status` | enum | See below |
| `receipt_id` | string/null | Set after validated response |
| `receipt_digest` | SHA-256/null | Canonical receipt digest |
| `effect_result_digest` | SHA-256/null | Redacted/canonical outcome evidence |
| `error_code` | string/null | Typed diagnostic |

**Transitions**:

`created` -> `astral_authorized` -> `lets_pending` -> `receipt_received` -> `receipt_claimed` -> `executing` -> `succeeded`

Any pre-execution state -> `denied` or `failed_closed`
`executing` -> `succeeded`, `effect_failed`, or `outcome_uncertain`

No effect executes before `receipt_claimed`. Safe retry of `outcome_uncertain` uses the tool's idempotency contract; it does not consume a fresh authority grant without reconciliation.

## 13. ReceiptClaim

The final gateway's replay and binding evidence.

| Field | Type | Rules |
|---|---|---|
| `receipt_id` | string | Unique globally within trusted wardens |
| `operation_id` | string | Equals protected effect and receipt request IDs |
| `tenant_id`, `envelope_id`, `warden_id`, `lease_id` | strings | Exact binding match |
| `subject_id`, `lineage_id` | strings | Exact governed-agent match |
| `policy_digest`, `machine_digest`, `config_epoch` | values | Exact configured fences |
| `audience`, `transition`, `nonce` | strings | Exact operation match |
| `resulting_sequence` | positive integer | Strictly advances `(warden, lease, audience)` watermark |
| `evidence_digest` | string/null | Equals operation effect digest when the profile requires evidence |
| `issued_at_ns`, `expires_at_ns` | integers | Valid under bounded clock uncertainty |
| `claimed_at` | UTC/ns | Set atomically with uniqueness/watermark checks |
| `canonical_digest` | SHA-256 | Full receipt bytes after strict parsing |

Uniqueness covers receipt ID and `(tenant, envelope, audience, nonce)`; sequence watermark prevents reuse of an older valid receipt.

## 14. ReleaseTrustTransition

The bounded migration from AstralDeep-owned release identity to Projection-owned release identity.

| Field | Type | Rules |
|---|---|---|
| `client` | enum | Initially `windows`; additional stores use their native identity records |
| `state` | enum | `legacy_only`, `bridge_ready`, `dual_pinned`, `projection_primary`, `legacy_retired` |
| `legacy_repository` | string | Exact old release source |
| `legacy_workflow_identity` | string | Exact Sigstore certificate identity pattern |
| `legacy_max_version` | version | Bounds old trust to the bridge lineage |
| `projection_repository` | string | Canonical Projection release source |
| `projection_workflow_identity` | string | Exact Projection workflow identity pattern |
| `bridge_version` | version | One immutable transition artifact |
| `artifact_sha256` | SHA-256 | Same executable bytes in both channels |
| `legacy_bundle_digest`, `projection_bundle_digest` | SHA-256 | Both verifiable bundles |
| `verified_at` | UTC | Candidate-bound evidence |

Repository redirect or organization membership alone never satisfies trust.

## 15. MigrationCheckpoint

A cross-repository, wiki-recorded handoff point.

| Field | Type | Rules |
|---|---|---|
| `checkpoint_id` | string | Stable phase name/date |
| `repositories` | map | Exact branch and commit for every affected repo |
| `composition_digest` | SHA-256/null | Present once composition exists |
| `legacy_baselines` | list | Default-tip records plus ordinary-ancestry verification |
| `verification` | list | Exact commands, outcomes, and evidence paths/digests |
| `paper_impact` | string | `none`, `citation`, `new_results`, or `requires_successor_release` |
| `risks` | list | Open, accepted, or resolved status |
| `wiki_commit` | SHA-1 hex | Separate pushed kos-wiki commit |

A checkpoint is not complete until product branches and the matching wiki commit are remotely verified.

## 16. CaseStudyEvidenceBundle

Local research evidence for Astral as a LETS use case.

| Field | Type | Rules |
|---|---|---|
| `format` | string | `lets.case-study-evidence/v1` |
| `baseline_release` | string | `v1.0.10` unless successor clearly declared |
| `repository_commits` | map | Exact LETS, Deep, Plane, Projection, Primitives commits |
| `composition_digest` | SHA-256 | Exact system composition |
| `policy_digest`, `machine_digest` | LETS digests | Exact authority semantics |
| `environment` | object | Non-secret software/hardware/runtime facts |
| `commands` | ordered list | Reproduction commands and exit status |
| `raw_evidence` | path/digest list | Local retained artifacts; never fabricated or relabeled |
| `measurements` | object | Named units, sample counts, uncertainty, exclusions |
| `baseline_or_integration` | enum | Prevents v1.0.10 release claims from absorbing new integration evidence |
| `reproduced_at` | UTC | Required |

The paper may cite only measurements whose bundle passes schema, digest, and exact-revision checks.
