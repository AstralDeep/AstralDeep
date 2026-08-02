# Data Model: Runtime Reliability and Release Readiness

This document defines the durable, in-memory, file-backed, and wire state required by feature 060.
It is a design contract for implementation, not deployed SQL. PostgreSQL remains the only durable
coordination authority. Existing Keycloak identity, authorization, credential storage, audit, and
other security behavior are unchanged.

## Conventions

- New durable identities are full UUIDs. A UUID sent by a client is validated and is never trusted
  as proof of ownership; the authenticated OIDC subject remains the owner key.
- Existing `user_agent.agent_id`, `chats.id`, and other legacy `TEXT` identifiers remain `TEXT` to
  avoid a destructive identity migration. Newly created personal-agent identifiers use canonical
  UUID text, while existing identifiers remain stable aliases.
- Existing epoch-millisecond columns remain epoch milliseconds until a separately approved cleanup
  migration. Every **new durable coordination timestamp** in feature 060 is PostgreSQL
  `TIMESTAMPTZ`, is written/compared using database UTC time, and is serialized on the wire as an
  RFC 3339 UTC timestamp. Integer monotonic times remain process-local diagnostics only; they are
  never persisted or compared across replicas.
- State vocabularies use `TEXT` plus `CHECK` constraints rather than PostgreSQL enum types. This
  keeps guarded startup migrations additive and repeat-safe.
- JSONB is reserved for bounded, schema-validated metadata. Operation records, release evidence,
  profiles, and observability rows never contain message bodies, credentials, generated code, raw
  child output, or provider responses.
- Mutable rows carry a monotonic `state_revision BIGINT`. Every transition is one conditional
  transaction (`... WHERE state_revision = expected`) and increments it. A zero-row update is a
  conflict, not permission to retry an unconditional write.
- Lease-bearing rows carry both a UUID `lease_token` and a monotonic claim/execution generation.
  The token fences two claimants in one generation; the generation fences a late holder after lease
  recovery. Tokens are internal and are never included in user-facing wire projections or evidence.
- Digests are lowercase SHA-256 hex unless a field explicitly names another format.

### Identity allocation

- The server allocates every full UUID4 `operation_id` only after admission succeeds. A client
  submission ID is an idempotency key, not an operation ID; a refusal creates no synthetic accepted
  operation. An operation ID is never shortened, recycled, or derived from display text.
- `occurrence_id`, every attempt's `operation_id`, `job_run.id`, delivery/revision/runtime/request
  IDs, publication IDs, transition IDs, snapshot IDs, evidence IDs, and exception IDs are
  independently allocated UUID4 identities at the authority that first creates the corresponding
  record. Stable subsystem work IDs survive retries; attempt operation IDs do not.
- A desktop installation allocates and persists its random `host_id`; the server allocates
  `host_session_id` after authenticating each connection. The server allocates `delivery_id`,
  `revision_id`, `runtime_instance_id`, `request_id`, `request_generation`, and every lifecycle
  generation. The selected host allocates a fresh logical UUID4 `process_id` immediately before
  each concrete child launch; the server accepts it only through the one-time fenced `starting`
  compare-and-set, and it is never ownership proof.
- The server allocates a draft's immutable `draft_uuid` and `target_agent_id` at creation. A name or
  slug is never an identity and never participates in identity allocation.

## Relationship Overview

```text
operation_record
  |-- operation_admission_slot (temporary capacity ownership)
  |-- scheduled_occurrence --< effect_ledger
  |                          \--< job_run
  |-- agent_runtime_request --> agent_runtime_instance
  |                              |--> agent_host_session
  |                              \--> user_agent_revision
  |-- draft_artifact_publication --> user_agent_revision
  \-- maintenance_unit --< maintenance_unit_input

user_agent
  |--> active_revision_id / last_known_good_revision_id
  |--> selected_host_session_id / authoritative_instance_id
  \--< user_agent_revision / agent_runtime_instance

chats + messages + saved_components --build--> ConversationSnapshot
ClientConversationLocator --requests-------> owned chat snapshot

WindowsDeploymentProfile --identifies--> packaged Windows artifact
ReleaseEvidenceSet --collects------------> same-SHA platform evidence
```

Database foreign keys are used where the referenced row already has a compatible durable identity.
Owner equality is additionally checked in repositories because legacy tables do not all expose a
composite owner key.

## 1. Accepted Operations and Admission

### `operation_record`

One row represents exactly one accepted **attempt** of foreground, background, scheduled,
generation, or maintenance work. A capacity refusal is not an accepted operation and therefore does
not create a fake operation row. `retryable` is terminal: retrying stable subsystem work allocates a
new operation and links it through `parent_operation_id`; it never puts the old operation back in a
queue.

| Field | Type | Rules |
|---|---|---|
| `operation_id` | UUID | Primary key; server-allocated UUID4 after acceptance. |
| `operation_kind` | TEXT | Bounded snake-case subsystem code such as `connection_frame`, `background_chat`, `scheduled_occurrence`, `agent_generation`, `maintenance`, or `llm_credential_save`. |
| `admission_class` | TEXT | FK to `operation_admission_class.class_name`; exactly `interactive`, `background`, `scheduled`, `maintenance`, or `system`. |
| `owner_scope` | TEXT | Exactly `connection`, `user`, `schedule`, `maintenance`, or `system`. |
| `owner_user_id` | TEXT nullable | Authenticated user identifier for user/schedule ownership; null otherwise and never trusted from payload. |
| `connection_scope_id` | UUID nullable | One server-issued socket lifetime; required for connection-owned operations. |
| `idempotency_namespace` | TEXT nullable | Bounded caller/subsystem namespace; null only together with `idempotency_key`. |
| `idempotency_key` | TEXT nullable | Bounded retry key; null only together with `idempotency_namespace`. |
| `normalized_input_digest` | CHAR(64) nullable | Digest of normalized non-secret work identity; required with an idempotency pair and null otherwise. The input itself is not stored here. |
| `chat_id` | TEXT nullable | Owner-validated chat context needed to replay a safe status projection. |
| `parent_operation_id` | UUID nullable | Self-FK for a child operation with `ON DELETE SET NULL`. |
| `connection_generation` | UUID nullable | Wire generation that admitted the work. |
| `request_generation` | UUID nullable | Logical request generation that owns resulting frames. |
| `state` | TEXT | `queued`, `running`, `completed`, `failed`, `cancelled`, or `retryable`. |
| `phase_code` | TEXT nullable | Safe bounded progress code such as `validating_credentials`; not free-form model output. |
| `terminal_code` | TEXT nullable | Safe result/error code; required in every terminal state except `completed`, where it is optional. |
| `safe_summary` | TEXT nullable | Bounded non-sensitive terminal summary; never raw input/output. |
| `retry_after_ms` | INTEGER nullable | Non-negative and present only for `retryable` when a delay is known. |
| `execution_generation` | BIGINT | Non-negative, default 0; 0 means never selected, first worker selection sets 1, every permitted reselection increments it, and terminal rows retain the final value. |
| `execution_lease_token` | UUID nullable | Internal token required while running. |
| `state_revision` | BIGINT | Starts at 0 and increments on every transition. |
| `accepted_at`, `updated_at` | TIMESTAMPTZ | Required database acceptance/update times; acceptance is the FIFO ordering authority. |
| `queue_deadline_at` | TIMESTAMPTZ nullable | Required for queued work and finite for every configured queue. |
| `started_at`, `terminal_at` | TIMESTAMPTZ nullable | Start/terminal times follow lifecycle. |
| `cancel_requested_at` | TIMESTAMPTZ nullable | Makes cancellation observable before a worker reaches its terminal transition. |
| `purge_after` | TIMESTAMPTZ nullable | Required on terminal rows; default is `terminal_at + 24h`. |

Constraints and indexes:

- `owner_user_id IS NOT NULL` for `owner_scope IN ('user','schedule')` and null otherwise;
  `connection_scope_id IS NOT NULL` for `owner_scope='connection'`, and is optional as an origin/
  subscriber scope for work that survives a viewer disconnect.
- Authenticated REST lookup is restricted to the `user` and `schedule` owner partitions and compares
  the persisted `owner_user_id` with the authenticated principal. Connection-owned rows terminate
  with their socket and are never made visible by presenting their UUID; reconnectable client work
  is admitted as user-owned before execution and may carry the connection scope only as origin
  metadata.
- A check requires both idempotency fields to be null or both non-null. A partial unique index on
  `(owner_scope, CASE WHEN owner_scope='connection' THEN connection_scope_id::text WHEN owner_scope
  IN ('user','schedule') THEN owner_user_id ELSE '' END, idempotency_namespace, idempotency_key)`
  when non-null returns the original operation within its owner scope. Reuse with a different
  `operation_kind`, owner tuple, or
  `normalized_input_digest` is `idempotency_conflict`; those comparison fields are deliberately not
  all part of the unique key.
- `terminal_at IS NOT NULL` and `purge_after IS NOT NULL` only for terminal states
  (`completed`, `failed`, `cancelled`, `retryable`). `queued` and `running` are non-terminal.
- `running` requires a non-null execution lease token and a positive `execution_generation`.
  `queued` has generation 0/token null. A terminal operation clears the token atomically and retains
  generation 0 if it never started or its final positive generation if it did.
- Indexes cover `(state, accepted_at, operation_id)`, `(owner_scope, owner_user_id, accepted_at DESC)`,
  `(connection_scope_id, state)`, and `(purge_after)` for
  terminal rows.
- The retention sweeper runs at least hourly. It removes only terminal rows whose `purge_after` is
  in the past, so the default 24-hour records disappear no later than hour 25.

State transitions:

```text
queued -> running -> completed | failed | cancelled | retryable
queued ------------> cancelled | retryable
```

There is no transition out of a terminal state. Repeating the same non-null idempotency pair returns
that terminal result idempotently. A caller acting on a `retryable` result starts a new operation
with a new attempt-scoped idempotency key and the prior operation as its parent; subsystem identities
such as a scheduled occurrence or maintenance unit remain stable separately.
Disconnect marks queued connection-owned operations cancelled and requests cancellation of running
ones. Every worker must settle them within the five-second drain bound.

#### Durable attempt-execution fence

The coordinator selects queued rows in FIFO order with `FOR UPDATE SKIP LOCKED` and atomically sets
`state='running'`, `execution_generation + 1`, and a fresh execution lease token. Every
side-effecting repository entry point receives `(operation_id, execution_generation,
execution_lease_token)` and, in the **same transaction as its durable effect**, locks the operation
and verifies that it is still the selected `running` execution with the current generation and
token. The durable effect row
records `operation_id` (nullable after retention purge) and the non-null selected
`operation_execution_generation`.

A permitted coordinator reselection before terminal rotates the token and increments the generation
before replacement code runs; the prior worker immediately loses commit authority. This is worker
ownership recovery within one attempt, not a subsystem retry. Once an operation terminalizes
`retryable`, recovery allocates a child attempt operation with a fresh idempotency key. A stale
executor therefore cannot publish, acknowledge, promote, delete, or terminalize effects even if it
still holds process-local state. Database-owned effects and the fence check commit atomically;
supported external effects additionally use their subsystem's stable downstream idempotency key.

### `operation_admission_class`

One row is the effective non-sensitive capacity configuration for an admission class.

| Field | Type | Rules |
|---|---|---|
| `class_name` | TEXT | Primary key (`global`, `interactive`, `background`, `scheduled`, `maintenance`, `system`). |
| `parent_class_name` | TEXT nullable | Self-FK; allows an operation to consume both a class slot and its global/parent slot. |
| `active_limit` | INTEGER | Greater than zero. |
| `queue_limit` | INTEGER | At least zero; zero means explicit refusal at capacity. |
| `max_wait_ms` | INTEGER | Finite and greater than zero when `queue_limit > 0`. |
| `config_revision` | TEXT | Effective deployment-configuration revision/digest. |
| `updated_at` | TIMESTAMPTZ | Database UTC time. |

The default background policy is five active slots and 100 queued items. Exact production values
remain configurable and are exposed through the existing non-sensitive `system_config` response.
The class row is locked while checking queue depth so two replicas cannot both accept the last queue
position. Lowering a limit does not evict running work; excess slots retire as their owners finish.
For scheduled work, `max_wait_ms` may exceed the occurrence claim lease only because a dedicated
lease-keeper starts when the claim transaction commits and renews independently throughout the
queued and running intervals; admission selection is never the first renewal point.

### `operation_admission_slot`

Rows materialize capacity and make cross-replica active limits structural.

| Field | Type | Rules |
|---|---|---|
| `class_name`, `slot_number` | TEXT, INTEGER | Composite primary key; slot number is positive. |
| `operation_id` | UUID nullable | FK to `operation_record(operation_id) ON DELETE SET NULL`; an operation may own one slot in each class in its parent chain. |
| `lease_token` | UUID nullable | Fresh on every claim/renewal epoch. |
| `claim_generation` | BIGINT | Starts at 0, increments whenever ownership changes. |
| `lease_expires_at` | TIMESTAMPTZ nullable | Required while occupied. |

Admission locks required class rows in stable parent-to-child order, claims one free or expired slot
per class with `FOR UPDATE SKIP LOCKED`, and commits all or none. Workers renew leases. Recovery may
free an expired slot only after terminalizing or reclaiming its operation; it never permits two
live holders with the same lease tuple.

### `operation_submission_result`

This bounded reconciliation table records the immutable result of a client submission without
turning a refusal into a fake operation.

| Field | Type | Rules |
|---|---|---|
| `submission_result_id` | UUID | Server-allocated primary key. |
| `submission_id` | UUID | Client retry/reconciliation identity. |
| `owner_scope`, `owner_user_id`, `connection_scope_id` | TEXT/TEXT/UUID nullable | Same authenticated owner partition rules as operation admission; never payload-derived. |
| `accepted` | BOOLEAN | Immutable admission outcome. |
| `operation_id` | UUID nullable | Required only when accepted; FK to `operation_record(operation_id) ON DELETE SET NULL`. |
| `refusal_code` | TEXT nullable | Required only when refused; stable safe admission code. |
| `retryable` | BOOLEAN | False for accepted; canonical refusal retryability otherwise. |
| `retry_after_ms` | INTEGER nullable | Non-negative only when a retryable refusal has a known delay. |
| `observed_at`, `purge_after` | TIMESTAMPTZ | Database UTC time and 24-hour reconciliation expiry. |

The owner partition expression plus `submission_id` is unique, so a repeat/query returns the
original accepted operation projection or original safe refusal and can never change outcome.
Checks enforce the accepted/refused field alternatives. Cleanup removes this row no earlier than
its linked terminal operation's reconciliation window and orders accepted submission cleanup before
operation cleanup in one transaction. `GET /api/operation-submissions/{submission_id}` applies the
same non-disclosing owner lookup and returns no payload/digest. Refusals without a client submission
identity increment bounded non-sensitive counters but need no durable fake identity.

### `background_task` additions

- `operation_id UUID NULL REFERENCES operation_record(operation_id) ON DELETE SET NULL`
- `operation_execution_generation BIGINT NULL`

Feature-060 task creation requires `operation_id`; a task that reaches `running` also requires the
selected positive execution generation. A queued operation that expires before selection truthfully
leaves this compatibility generation null while its operation retains generation 0. A started
task's copied generation remains after operation retention cleanup, so a stale worker cannot reuse
the task row as current authority.

Connection registration state itself remains an in-memory `ConnectionContext`: UUID scope,
connection generation, five-second deadline, finite pre-registration deque, ordered reader/writer
data lane, tracked tasks, and closing flag. Consecutive reads share a reader generation, while every
mutation waits for earlier reads/mutations and later reads wait for that mutation; transport controls
bypass the data lane. Durable operation ownership is written before dequeued work starts.

## 2. Scheduled Occurrences and Effect Deduplication

### `scheduled_occurrence`

One row materializes one intended firing independently of its schedule definition.

| Field | Type | Rules |
|---|---|---|
| `occurrence_id` | UUID | Primary key; stable through every retry. |
| `job_id` | UUID | FK to `scheduled_job(id) ON DELETE RESTRICT`; job deletion is a soft/fenced transition and never erases occurrence history. |
| `owner_user_id` | TEXT | Immutable denormalized owner, verified equal to the job owner on insert. |
| `scheduled_for` | TIMESTAMPTZ | Intended UTC firing time at the scheduler's normalized precision. |
| `run_now_submission_id` | UUID nullable | Client retry identity for an explicit Run-now action; null for automatic occurrences and immutable once set. |
| `state` | TEXT | Exactly `pending`, `claimed`, `running`, `completed`, `failed`, `retryable`, or `cancelled`. Ineligible handlers are refused before occurrence creation. |
| `lease_token` | UUID nullable | Required for claimed/running work. |
| `claim_generation` | BIGINT | Starts at 0 and increments on every claim/reclaim. |
| `lease_owner` | TEXT nullable | Non-sensitive service-instance identity. |
| `lease_expires_at` | TIMESTAMPTZ nullable | Required while claimed/running. |
| `attempt_count` | INTEGER | Starts at 0; increments exactly when a new execution attempt is allocated. |
| `current_operation_id` | UUID nullable | Current/most recent attempt FK to `operation_record(operation_id) ON DELETE SET NULL`. |
| `operation_execution_generation` | BIGINT nullable | Selected execution generation for the current/most recent attempt; required once that attempt starts. |
| `first_eligible_at`, `started_at`, `terminal_at`, `next_attempt_at` | TIMESTAMPTZ nullable | First eligibility is required; other values follow state. |
| `result_code`, `last_error_code` | TEXT nullable | Safe bounded result/failure codes; no instruction, output, or credential data. |
| `created_at`, `updated_at` | TIMESTAMPTZ | Database UTC times. |

`UNIQUE(job_id, scheduled_for)` is the durable occurrence identity boundary. A partial unique index
on `(owner_user_id, run_now_submission_id)` where the submission is non-null makes explicit Run-now
retries resolve one occurrence without deriving its UUID from client input. Indexes also cover
`(state, scheduled_for)` and `(state, lease_expires_at)`. Polling performs, in one transaction:

1. lock eligible `scheduled_job` rows with `FOR UPDATE SKIP LOCKED`;
2. insert the due occurrence with `ON CONFLICT (job_id, scheduled_for) DO NOTHING`;
3. compute and persist `scheduled_job.next_run_at`/completion state; and
4. claim eligible occurrences with a fresh token/generation.

Run-now locks the owner-scoped active job, replays or rejects the owner/submission identity, inserts
one pending occurrence at normalized PostgreSQL database time, and leaves the recurring
`next_run_at` unchanged. Pause/delete lock and recheck the job, accepted operation, and occurrence in
the same canonical order; they cancel pending/retryable/claimed-but-not-running attempts and release
their slots, but never cancel an occurrence whose fenced transition to `running` already won.

An expired `claimed` or `running` lease transitions to `retryable` and is reclaimed with the
same `occurrence_id` but a new attempt operation. State transitions are:

```text
pending | retryable -> claimed -> running -> completed | failed | retryable | cancelled
claimed -------------------------------> retryable | cancelled
pending --------------------------------> cancelled
```

`completed`, `failed`, and `cancelled` are terminal occurrence states. `retryable` is non-terminal
occurrence state, but the operation that produced it has already terminalized `retryable`. At each
new attempt allocation, `attempt_count` increments and the
scheduler creates a new operation with:

```text
idempotency_namespace = scheduled_occurrence_attempt
idempotency_key       = <occurrence_id>:<attempt_count>
```

The occurrence identity remains stable; an operation identity and its terminal result never do
double duty for a later scheduler attempt. Reclaiming an expired claim first terminalizes any old
running operation as `retryable`, then allocates the next attempt. A dedicated claim lease-keeper
starts immediately after the claim transaction commits and renews throughout operation queueing and
execution at least once per one third of the configured lease. Before start, the runner renews and
rechecks the claim in the same transaction that records the run. Claim/start/renew/result updates
compare-and-set both `lease_token` and `claim_generation`; a lost renewal terminalizes the queued
attempt operation as retryable, prevents start, and removes all effect authority.

### `job_run` additions

- `occurrence_id UUID NULL REFERENCES scheduled_occurrence(occurrence_id)`
- `attempt_number INTEGER NULL`
- `operation_id UUID NULL REFERENCES operation_record(operation_id) ON DELETE SET NULL`
- `operation_execution_generation BIGINT NULL`
- `occurrence_claim_generation BIGINT NULL`
- unique partial index `(occurrence_id, attempt_number)` where `occurrence_id IS NOT NULL`

Historical runs remain nullable. Every feature-060 run requires all five additions, with the
operation generation and occurrence claim generation copied from the selected fences when the run
starts. Multiple attempts therefore remain truthful without inventing multiple occurrences or
reusing a terminal operation.

### `effect_ledger`

One row reserves one AstralDeep-controlled visible effect.

| Field | Type | Rules |
|---|---|---|
| `occurrence_id` | UUID | FK to `scheduled_occurrence(occurrence_id) ON DELETE RESTRICT`. |
| `effect_kind` | TEXT | Bounded kind such as `chat_message`, `notification`, `history_publish`. |
| `effect_key` | TEXT | Stable target-local key. |
| `payload_digest` | TEXT | SHA-256 of normalized effect data. |
| `state` | TEXT | Exactly `reserved`, `published`, or `failed`. |
| `operation_id` | UUID nullable | Producing attempt FK to `operation_record(operation_id) ON DELETE SET NULL`. |
| `operation_execution_generation` | BIGINT | Selected producing-operation generation. |
| `occurrence_claim_generation` | BIGINT | Selected occurrence claim generation. |
| `reserved_at`, `published_at`, `failed_at` | TIMESTAMPTZ nullable | Reservation required; the matching terminal time follows state. |
| `failure_code`, `downstream_receipt_digest` | TEXT nullable | Safe failure code or digest of a stable downstream receipt; never a raw receipt. |

The composite primary key is `(occurrence_id, effect_kind, effect_key)`. A repeated reservation with
a different digest is terminal `effect_idempotency_conflict`. Every reservation/publication/failure
transaction verifies both the current occurrence `(lease_token, claim_generation)` and the current
operation `(execution_lease_token, execution_generation)`; the ledger persists both generations.
Database-backed effects insert the target row and mark the ledger `published` in that same
transaction. Supported downstream effects use `occurrence_id` or a deterministic derivative as the
downstream idempotency key and persist the stable receipt digest before success. Recovery reads the
ledger/receipt and completes without re-emitting. A handler without an atomic or downstream
idempotency boundary is ineligible for unattended scheduling.

For a scheduled chat effect, the target/effect transaction is also the conversation publication
transaction: it validates the occurrence claim and operation fence, commits the staged ordered
messages plus complete canvas/layout generation, advances the chat revision once, commits the
`conversation_commit`, and marks the effect `published` atomically. An explicit owner-validated
`target_chat_id` is used when present. Otherwise `scheduled_job.id` itself (a UUID4) is the stable
fallback `chat_id`; no per-user or per-attempt synthetic chat identity is allocated, so retries
hydrate and deduplicate against the same conversation.

## 3. Personal-Agent Revision, Host, Runtime, and Request Fences

### `user_agent` additions

| Field | Type | Rules |
|---|---|---|
| `active_revision_id` | UUID nullable | FK to `user_agent_revision ON DELETE SET NULL`; nullable during staged legacy reconciliation, before first publication, or after deletion. |
| `last_known_good_revision_id` | UUID nullable | FK `ON DELETE SET NULL`; recoverable prior working revision, never cleared by candidate preparation. |
| `selected_host_session_id` | UUID nullable | FK `ON DELETE SET NULL`; current sticky host session. |
| `authoritative_instance_id` | UUID nullable | FK `ON DELETE SET NULL`; current invocable runtime. |
| `lifecycle_generation` | BIGINT | Generation of authoritative pointers, default 0. |
| `generation_counter` | BIGINT | Monotonic allocator; never decremented or reused after failed candidates. |
| `state_revision` | BIGINT | CAS revision for lifecycle/delete/promotion. |
| `validated_policy_revision` | TEXT nullable | Exact combined user-agent policy revision that passed Analyze. |

Pointer foreign keys are added after the new tables exist. `UNIQUE(agent_id, owner_user_id)` supplies
the referenced owner pair for child tables. Repository transitions lock the `user_agent` row,
allocate `generation_counter + 1` in that same update, and verify all referenced rows have the same
agent and owner. A generation is consumed even when its candidate fails and is never reused.
`deleted_at` remains the durable tombstone.

### `user_agent_revision`

| Field | Type | Rules |
|---|---|---|
| `revision_id` | UUID | Primary key. |
| `agent_id`, `owner_user_id` | TEXT | Composite FK to `user_agent(agent_id, owner_user_id) ON DELETE RESTRICT`. |
| `revision_number` | BIGINT | Monotonic per agent; unique with `agent_id`. |
| `parent_revision_id` | UUID nullable | Same-agent prior source revision; composite FK `ON DELETE SET NULL`. |
| `previous_good_revision_id` | UUID nullable | Same-agent recovery relationship captured when preparing promotion; composite FK `ON DELETE SET NULL`. |
| `artifact_digest` | TEXT nullable | SHA-256; nullable only for explicit `legacy_pending` backfill rows. |
| `manifest_json` | JSONB nullable | Bounded normalized bundle manifest without code or credentials; nullable only for `legacy_pending`. |
| `artifact_relative_path` | TEXT nullable | Validated path beneath the configured immutable revision root; nullable only for `legacy_pending`. |
| `runtime_contract_version` | INTEGER nullable | Positive for reconciled revisions; nullable only for `legacy_pending`. |
| `release_lock_digest` | TEXT nullable | Packaged dependency-lock digest; nullable only for `legacy_pending`. |
| `compatibility_state` | TEXT | `compatible`, `incompatible`, or `legacy_pending`. |
| `state` | TEXT | `legacy_pending`, `prepared`, `starting`, `ready`, `active`, `retired`, or `failed`. |
| `promotion_token` | UUID nullable | Correlates candidate acknowledgements; nullable only for `legacy_pending`. |
| `state_revision` | BIGINT | CAS revision; artifact fields are otherwise immutable. |
| `created_at` | TIMESTAMPTZ | Database creation time. |
| `confirmed_at`, `promoted_at`, `failed_at` | TIMESTAMPTZ nullable | State-dependent database UTC times. |
| `failure_code` | TEXT nullable | Safe bounded code. |

Artifact identity/manifest/path/digests are immutable after insert. Only the promotion state,
revision counter, and terminal metadata may change. `UNIQUE(agent_id, revision_number)`,
`UNIQUE(agent_id, artifact_digest) WHERE artifact_digest IS NOT NULL`, and
`UNIQUE(revision_id, agent_id, owner_user_id)` support truthful dedupe and composite child FKs. A
check requires artifact digest, manifest, artifact path, runtime-contract version, and lock digest
for every row whose `compatibility_state != 'legacy_pending'`; all may remain null only on the
explicit staged legacy row, whose state is also `legacy_pending` and which is never routable. The
promotion token is subject to the same rule. The FK/check constraints are installed
`NOT VALID` where representative legacy data cannot yet satisfy them, enforced for every new/changed
row immediately, and validated after inventory reconciliation proves no invalid legacy row remains.

### `agent_host_session`

| Field | Type | Rules |
|---|---|---|
| `host_session_id` | UUID | Primary key; one authenticated UI connection lifetime. |
| `host_id` | UUID | Stable non-credential installation identity. |
| `owner_user_id` | TEXT | Bound from authenticated principal, not trusted from host payload. |
| `connection_scope_id` | UUID | Owning connection scope. |
| `platform` | TEXT | Validated `windows` or `macos`; must agree with the authenticated registration device/profile. |
| `client_version` | TEXT | Strict SemVer from the accepted host registration. |
| `host_generation` | BIGINT | Monotonic for a stable host ID. |
| `supersedes_session_id` | UUID nullable | Prior session for a same-host reconnect; FK `ON DELETE SET NULL`. |
| `supported_runtime_contract_versions` | INTEGER[] | Non-empty, unique positive host-advertised versions. |
| `runtime_contract_version` | INTEGER | Server-selected common version, currently 2. |
| `release_lock_digest` | TEXT | Host packaged-runtime digest. |
| `state` | TEXT | `connected`, `disconnected`, or `incompatible`. |
| `inventory_state` | TEXT | `pending`, `reconciled`, or `failed`. |
| `eligible_since` | TIMESTAMPTZ | Durable deterministic standby ordering time. |
| `accepted_at`, `last_seen_at`, `disconnected_at`, `inventory_reconciled_at` | TIMESTAMPTZ nullable | Accepted and last-seen required while connected. |
| `failure_code` | TEXT nullable | Safe incompatibility/reconciliation reason. |

`UNIQUE(owner_user_id, host_id, host_generation)` prevents generation reuse. A connected host may
be selected for some agents and standby for others. No retained bundle may launch until inventory
reconciliation removes deleted/obsolete revisions and marks this session `reconciled`.

### `agent_runtime_instance`

| Field | Type | Rules |
|---|---|---|
| `runtime_instance_id` | UUID | Primary key. |
| `agent_id`, `owner_user_id` | TEXT | Durable owner pair. |
| `host_id`, `host_session_id` | UUID | Stable host identity plus FK to the exact host session `ON DELETE RESTRICT`. |
| `delivery_id` | UUID | Unique delivery attempt. |
| `revision_id` | UUID | Composite same-agent/owner FK to immutable revision `ON DELETE RESTRICT`. |
| `process_id` | UUID nullable | Null while the durable instance is pre-launch. The selected host binds it exactly once during the first valid `starting` transition; it is immutable thereafter and distinct from an OS PID. |
| `lifecycle_generation` | BIGINT | Reserved from `user_agent.generation_counter`. |
| `runtime_contract_version` | INTEGER | Must match an accepted host/bundle pairing. |
| `operation_id` | UUID nullable | Delivery/start/promotion attempt FK to `operation_record(operation_id) ON DELETE SET NULL`. |
| `operation_execution_generation` | BIGINT | Selected operation generation that authorized creation and promotion; retained after operation purge. |
| `state` | TEXT | `delivering`, `starting`, `ready`, `online`, `updating`, `stopping`, `stopped`, `failed`, `offline`, or `superseded`. `ready` is the fenced candidate-registration state before promotion. |
| `is_authoritative` | BOOLEAN | True only after durable promotion/selection. |
| `state_revision` | BIGINT | CAS transition revision. |
| `created_at` | TIMESTAMPTZ | Database creation time. |
| `registered_at` | TIMESTAMPTZ nullable | Null before the exact child registration is accepted; stamped with database receipt time once for the bound process. |
| `last_heartbeat_sequence` | BIGINT nullable | Null before the first valid post-registration heartbeat; thereafter positive and strictly increasing for this runtime/process instance. |
| `started_at`, `ready_at`, `last_liveness_at`, `terminal_at` | TIMESTAMPTZ nullable | State-dependent database receipt times. The first valid heartbeat sets `last_heartbeat_sequence` and `last_liveness_at`; later liveness advances both only under a larger sequence. |
| `failure_code` | TEXT nullable | Required for failed/offline terminalization caused by a fault. |

`delivering` requires `process_id IS NULL`. Every state reached after a concrete launch retains a
non-null `process_id`; a pre-launch `failed` instance may remain null. The bind update requires the
complete pre-launch fence, current host session, expected state revision, and `process_id IS NULL`.
`registered_at`, `last_heartbeat_sequence`, and `last_liveness_at` are null before child
registration. Accepting the exact registration stamps only `registered_at`; the first valid
heartbeat stores a positive sequence and database receipt-time liveness. A repeat/lower sequence
does not update either field. Sequence reset is possible only by creating a new runtime instance and
binding a fresh process generation; server restart never resets it.

Unique indexes cover `delivery_id`, `(agent_id, lifecycle_generation)`,
`(runtime_instance_id, agent_id, owner_user_id)`, and one authoritative row per agent
(`agent_id WHERE is_authoritative`), plus `(host_id, process_id) WHERE process_id IS NOT NULL`.
Candidate and last-good processes may overlap. `ready` proves
the candidate registered and passed its fence/liveness check, but only an `online` row referenced by
`user_agent.authoritative_instance_id` is invocable.

### `agent_runtime_request`

| Field | Type | Rules |
|---|---|---|
| `request_id` | UUID | Primary key and stable call identity. |
| `request_generation` | UUID | Fresh logical generation; unique with runtime instance. |
| `operation_id` | UUID nullable | Attempt FK to `operation_record(operation_id) ON DELETE SET NULL`; nullable later only because operation records expire. |
| `operation_execution_generation` | BIGINT | Selected operation execution generation retained after operation purge. |
| `runtime_instance_id`, `agent_id`, `owner_user_id` | UUID/TEXT/TEXT | Composite FK to the exact same-agent/owner runtime instance. |
| `state` | TEXT | `assigned`, `running`, `completed`, `failed`, `cancelled`, or `retryable`. |
| `state_revision` | BIGINT | CAS revision. |
| `assigned_at` | TIMESTAMPTZ | Database assignment time. |
| `terminal_at` | TIMESTAMPTZ nullable | Required for terminal states. |
| `terminal_code`, `result_digest` | TEXT nullable | Safe terminal code and optional normalized-result digest; no raw result body. |

All host frames carry this complete logical fence:

```text
host_id + host_session_id + delivery_id + revision_id +
runtime_instance_id + process_id + lifecycle_generation +
request_id + request_generation
```

The server accepts a frame only when every field matches current durable request/instance/pointer
state **and** the request's operation still has the recorded selected execution generation. Result
publication and operation terminalization occur in one transaction under that complete fence. Host
loss, child exit, stop, or hang terminalizes all non-terminal requests for that instance and their
attempt operations exactly once. A retry creates a new request, request generation, and child
operation; it never revives either terminal row. A liveness gap of five seconds moves the instance
to failed/offline; requests settle within the following two seconds.

The canonical `agent_lifecycle` projection carries both `lifecycle_generation` and the authoritative
instance's `state_revision`. Clients compare the pair lexicographically: a higher generation replaces
all states from an older authority, while a higher state revision permits `starting -> online` (and
other transitions) within one generation. Equal pairs are idempotent; lower pairs are stale.

### Revision promotion and deletion transitions

1. Lock the `user_agent`, increment `generation_counter`, insert an immutable `prepared` revision
   and non-authoritative candidate instance using the reserved generation. The current pointers do
   not move.
2. Stage/validate/place the candidate bundle, start it, and accept `starting -> ready` only with the
   full fence and promotion token. Failure stops only the candidate and marks the revision failed.
3. In one transaction, recheck `deleted_at`, expected agent state revision, candidate revision,
   selected host, and full runtime fence; switch authoritative/active pointers, set
   `lifecycle_generation`, preserve the old revision as `last_known_good_revision_id`, and mark the
   candidate active. Only after commit may the old process stop.
4. A crash after commit is reconciled from database pointers; a crash before commit leaves the old
   pointers authoritative. Immutable prior bundles remain until retention/recovery policy permits
   cleanup.
5. Delete first commits `deleted_at`, `status='disabled'`, a newly allocated lifecycle generation,
   and cleared selected/authoritative pointers. Only then are requests settled, routes removed, and
   host cleanup sent. Any delayed frame is fenced by tombstone and generation.

Host selection retains a healthy incumbent. A new session for the same `host_id` may rebind after
inventory reconciliation; otherwise eligible sessions remain standby and the earliest
server-accepted standby is selected only after incumbent loss.

## 4. Draft CAS and Atomic Artifact Publication

The existing `draft_agents.id TEXT` remains the stable legacy key; it is not assumed to be a valid
UUID and is never used as a new composite FK target. The server allocates `draft_uuid` and
`target_agent_id` once. Migration copies a canonical UUID `id` where safe and allocates a separate
UUID4 alias for every other row before that row can enter a 060 authoring transition. Display name
and `agent_slug` are never identities.

### `draft_agents` additions

| Field | Type | Rules |
|---|---|---|
| `draft_uuid` | UUID nullable | Unique canonical alias; required for every 060 transition. |
| `target_agent_id` | TEXT nullable | Assigned once at draft creation; new values are canonical UUID text, revisions preserve the existing agent ID. |
| `state_revision` | BIGINT | Defaults to 0. |
| `generation_claim_id` | UUID nullable | Current generation claim. |
| `generation_claim_expires_at` | TIMESTAMPTZ nullable | Finite database-time recovery bound. |
| `published_revision_id` | UUID nullable | Part of the composite FK to the same target-agent/owner revision; nullable before publication. |

`UNIQUE(draft_uuid)` and the actual FK target `UNIQUE(draft_uuid, user_id)` are constraints (not
merely indexes). Children use `FOREIGN KEY (draft_uuid, owner_user_id) REFERENCES
draft_agents(draft_uuid, user_id) ON DELETE RESTRICT`. Once publication is present,
`(published_revision_id, target_agent_id, user_id)` references
`user_agent_revision(revision_id, agent_id, owner_user_id) ON DELETE RESTRICT`. Immutable published
revisions are retained; deleting one behind the pointer is not an allowed retention action.
`target_agent_id` is never recomputed after a rename. Same-name drafts therefore have different
draft, target-agent, staging, and revision identities.

### `draft_transition`

Each mutating authoring request supplies a client UUID `transition_id`.

| Field | Type | Rules |
|---|---|---|
| `transition_id` | UUID | Primary key/idempotency identity. |
| `draft_uuid`, `owner_user_id` | UUID, TEXT | Composite FK to the owner-scoped draft. |
| `operation_id` | UUID nullable | Attempt FK to `operation_record(operation_id) ON DELETE SET NULL`. |
| `operation_execution_generation` | BIGINT | Selected operation generation that authorized this transition. |
| `transition_kind` | TEXT | `save`, `advance`, `analyze`, `claim_generation`, `publish`, or lifecycle action. |
| `expected_revision`, `result_revision` | BIGINT | Expected CAS input and resulting revision. |
| `outcome` | TEXT | `applied`, `conflict`, `replayed`, or `failed`. |
| `safe_code` | TEXT nullable | Safe conflict/failure reason. |
| `created_at` | TIMESTAMPTZ | Database UTC time. |

The transaction first checks this identity. An existing applied transition returns its recorded
result. Otherwise it verifies the selected operation execution fence, updates the draft only where
owner and expected revision match, inserts the transition with that execution generation, and
commits. Conflict leaves draft state unchanged and returns the current revision and the `refresh`
action within one second. Retrying a terminal operation creates a new attempt operation but may
replay the same transition ID only when its normalized input digest is identical.

### `draft_artifact_publication`

| Field | Type | Rules |
|---|---|---|
| `publication_id` | UUID | Primary key. |
| `draft_uuid`, `owner_user_id` | UUID, TEXT | Composite FK to the owner-scoped draft. |
| `source_state_revision` | BIGINT | Exact draft version being generated. |
| `generation_claim_id` | UUID | Must match the live claim. |
| `target_agent_id` | TEXT | Immutable target. |
| `target_revision_id` | UUID | Together with target/owner, composite FK to `user_agent_revision`. |
| `operation_id` | UUID nullable | Current/most recent attempt FK to `operation_record(operation_id) ON DELETE SET NULL`. |
| `operation_execution_generation` | BIGINT nullable | Selected generation for the current/most recent attempt; required once staging begins. |
| `staging_relative_path`, `revision_relative_path` | TEXT | Validated descendants of configured roots, never client paths. |
| `artifact_digest`, `manifest_digest` | TEXT nullable | Required from `validated` onward. |
| `state` | TEXT | `claimed`, `staged`, `validated`, `published`, or `failed`. |
| `state_revision` | BIGINT | CAS transition revision. |
| `created_at` | TIMESTAMPTZ | Database creation time. |
| `published_at`, `failed_at` | TIMESTAMPTZ nullable | State-dependent database UTC times. |
| `failure_code` | TEXT nullable | Safe bounded code. |

`UNIQUE(draft_uuid, source_state_revision)` prevents duplicate generation and
`UNIQUE(target_revision_id)` prevents duplicate publication. The publication's composite FKs are
`(draft_uuid, owner_user_id) -> draft_agents(draft_uuid, user_id)` and
`(target_revision_id, target_agent_id, owner_user_id) ->
user_agent_revision(revision_id, agent_id, owner_user_id)`. Files are written beneath
`staging/<draft_uuid>/<source_state_revision>/<publication_id>`, flushed and fsynced, validated,
then moved with same-filesystem `os.replace` to
`revisions/<target_agent_id>/<target_revision_id>`. The immutable revision directory is durable
before database promotion. The database pointer is authoritative; a small filesystem current-pointer
cache is atomically replaced after commit and rebuilt from the database after a crash. Before each
staging, replace, revision insert, pointer promotion, or failure transition, the repository checks
both the generation claim and the selected operation execution fence; durable rows retain that
execution generation after the operation FK is purged. A failed generation never writes into a live
revision directory and never removes a prior revision. A retry preserves publication/draft/target
identities but receives a new child attempt operation and may continue only after reconciliation of
the digest-scoped staging path.

## 5. Startup Revision and Policy Coordination

The existing `schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)` remains the marker store.
Feature 060 uses two independent keys:

- `revision = '060.004'` (the hardened schema revision; implementation bumps
  `SCHEMA_REVISION` from `057.001`), and
- `user_agent_policy_revision = USER_AGENT_POLICY_REVISION`, an exact combined value for the
  user-agent constitution plus Analyze policy. `backend/orchestrator/agent_analyze.py` owns
  `ANALYZE_POLICY_REVISION = "1"`; `backend/orchestrator/agent_constitution.py` validates the baked
  constitution SemVer and owns construction of the canonical combined value
  `constitution=<semver>;analyze=<positive-integer>`. The feature-060 initial value is exactly
  `constitution=0.1.0;analyze=1`. Missing or malformed inputs fail startup closed; the database layer
  consumes the exported value and never fabricates one. Equality is exact; this is not a major-only
  SemVer comparison.

Startup algorithm:

1. Create `schema_meta` if absent and commit that minimal bootstrap.
2. Begin a transaction and acquire `pg_advisory_xact_lock(1095980114, 60001)`, where
   `1095980114` is the fixed project class `0x41535452` (`ASTR`) and `60001` is the schema-update
   key. `astraldeep:schema-update` is a diagnostic label only. Re-read `revision` after acquiring it.
   Apply only repeat-safe 060 DDL,
   backfills, constraints, and indexes, then update the marker and commit together. A crash rolls
   back both schema transaction and marker and releases the lock.
3. In a separate transaction acquire `pg_advisory_xact_lock(1095980114, 60002)` for the independent
   policy update. `astraldeep:user-agent-policy-update` is diagnostic only. Re-read the exact policy
   marker on every boot even when schema is current, and compare it to the code constant.
4. On mismatch, mark each non-deleted agent whose `validated_policy_revision` differs
   `revalidation_required=TRUE`, preserving the old value as evidence of what last passed. Analyze
   success later writes the current exact value and clears the flag through the existing fail-closed
   policy boundary. Update the global policy marker only in the same successful sweep transaction.
   A crash leaves the old marker so the next instance repeats the sweep.

Every instance that loses either lock waits, then re-reads the marker and observes the winner's
committed revision. No file lock, process-local flag, or schema-revision shortcut is authoritative.
The exact two signed-32-bit key pairs are constants shared by every starter; Python `hash()`, process
IDs, database connection IDs, and runtime-dependent string hashes are prohibited lock identities.

## 6. Durable Maintenance Claims

### `maintenance_unit`

| Field | Type | Rules |
|---|---|---|
| `unit_id` | UUID | Primary key; retained across retry. |
| `unit_kind` | TEXT | Stable kind such as `agent_synthesis`, `cross_agent_synthesis`, or bounded cleanup. |
| `owner_user_id` | TEXT nullable | Required for user-scoped work. |
| `scope_key`, `idempotency_key` | TEXT | Bounded normalized scope and durable dedupe key. |
| `state` | TEXT | `pending`, `claimed`, `running`, `succeeded`, `failed_retryable`, `failed_terminal`, or `cancelled`. |
| `lease_token` | UUID nullable | Required in claimed/running. |
| `claim_generation` | BIGINT | Monotonic claim fence. |
| `claimed_by`, `lease_expires_at` | TEXT/TIMESTAMPTZ nullable | Required in claimed/running. |
| `attempt_count`, `max_attempts` | INTEGER | Non-negative; max is finite and positive. |
| `operation_id` | UUID nullable | Current/most recent attempt FK to `operation_record(operation_id) ON DELETE SET NULL`. |
| `operation_execution_generation` | BIGINT nullable | Selected generation for the current/most recent attempt; required once running. |
| `output_generation` | UUID nullable | Stable filesystem publication generation. |
| `output_relative_path`, `output_digest` | TEXT nullable | Both required when complete. |
| `last_error_code` | TEXT nullable | Safe code only. |
| `state_revision` | BIGINT | CAS revision. |
| `created_at`, `updated_at` | TIMESTAMPTZ | Database UTC times. |
| `terminal_at`, `next_attempt_at` | TIMESTAMPTZ nullable | State-dependent terminal/retry times. |

`UNIQUE(unit_kind, idempotency_key)` makes crash retry idempotent. Claimers use
`FOR UPDATE SKIP LOCKED`; an expired lease becomes `failed_retryable` and preserves `unit_id`.
Every claim that will execute increments `attempt_count` and creates a fresh operation with
`idempotency_namespace='maintenance_unit_attempt'` and
`idempotency_key='<unit_id>:<attempt_count>'`. The prior attempt operation is already terminal
`retryable`; it is never re-run.

### `maintenance_unit_input`

| Field | Type | Rules |
|---|---|---|
| `unit_id` | UUID | FK to maintenance unit. |
| `input_kind`, `input_id` | TEXT | Source type and durable source identity. |
| `input_digest` | TEXT nullable | Detects changed source data. |
| `state` | TEXT | `pending` or `completed`. |
| `operation_id` | UUID nullable | Completing attempt FK to `operation_record(operation_id) ON DELETE SET NULL`. |
| `operation_execution_generation` | BIGINT nullable | Completing attempt generation, required when completed. |
| `completed_at` | TIMESTAMPTZ nullable | Present only when completed. |

The primary key is `(unit_id, input_kind, input_id)`. Selected `interaction_log` rows are locked and
inserted as membership before work starts; they are not marked `synthesized` then. On success, the
same transaction marks only that unit's inputs complete and updates their source rows. On failure,
the unit becomes `failed_retryable` and all pending sources remain unsynthesized.

Maintenance output is written to a same-directory temporary file, flushed/fsynced, atomically
replaced, and followed by a directory fsync. A crash between file replacement and database commit is
reconciled by `output_generation` and digest; matching output completes the same unit rather than
publishing again. Every input-completion, output-publication, source-row update, and index-pointer
change verifies the unit claim plus the selected operation execution fence and persists both
generations. Index rebuild follows committed output. Blocking process/database/filesystem work runs
in the dedicated bounded maintenance executor, outside the interactive executor.

## 7. Runtime Registry and Process Supervision Records

These records are process-local projections; durable personal-agent truth remains in the tables
above.

The supervision limits form one conformance contract, not one cross-package runtime import. Server-
hosted draft/test children use `backend/shared/process_supervision.py`; the packaged desktop host
uses `windows-client/win_agent/process_supervision.py`. Both consume the same constants/test vectors
and pass equivalent stress assertions. The frozen Windows artifact contains its local module, and
neither product runtime imports the other application tree.

### `RuntimeRegistrySnapshot`

- `registry_version: UInt64`
- immutable tuples of runtime, host-session, lifecycle, and card records
- each record includes its durable identity and `state_revision`
- `captured_at_monotonic` for diagnostics only

Writers hold one registry lock, build a replacement immutable record/map, increment the version, and
publish the new snapshot atomically. Readers receive the immutable snapshot or tuple; they never
iterate a concurrently mutated dictionary or assemble a view from separately versioned maps.

### `SupervisedProcessRecord`

- logical UUID `process_id`, owning runtime/draft/publication identity, OS PID and process-group/job
  identity
- state `starting`, `running`, `stopping`, `exited`, `failed`, or `killed`
- start/last-liveness/termination monotonic times, exit code, and safe failure code
- one continuous reader per stdout/stderr stream using 16 KiB reads
- maximum logical line size: 64 KiB; overlong lines are truncated with a counter
- ring capacity: 256 KiB per stream (512 KiB per child total); oldest complete data is discarded
  with dropped-byte/line counters
- explicit reader completion and pipe-closed events

Output buffers are diagnostic only and are never an operation result authority. Stop, quit, failure,
and cancellation first terminate the complete POSIX process group or Windows process tree, escalate
within the shared five-second deadline, join both readers, and close both pipes. Terminal runtime
state is not published until tree and pipe cleanup has completed or a cleanup failure has been
recorded explicitly.

## 8. Conversation Snapshot and Client Locator

No duplicate transcript database is introduced. Existing owner-scoped `chats`, `messages`, and
`saved_components` remain authoritative. Feature 060 adds to `chats`:

- `render_revision BIGINT NOT NULL DEFAULT 0`
- `snapshot_committed_at TIMESTAMPTZ NULL`
- `conversation_commit_id UUID NULL`

### `conversation_commit` boundary

This metadata table makes a logical update—not an individual message/component write—the snapshot
visibility boundary. Direct turns, component mutations, scheduled turns, persisted stream
terminals, detached/REST updates, and long-running-job results all use this boundary. It stores no
transcript or component body.

| Field | Type | Rules |
|---|---|---|
| `commit_id` | UUID | Primary key; server allocated for one logical conversation-update attempt. |
| `chat_id`, `owner_user_id` | TEXT | Owner-validated chat identity; FK to `chats(id) ON DELETE CASCADE` plus repository owner check. |
| `request_generation` | UUID | Logical update generation; unique with `chat_id`. Client turns use their client-generated UUID4; scheduled/detached/stream/long-job work uses a fresh server UUID4 advertised only through `conversation_commit_ready`. |
| `operation_id` | UUID nullable | Turn attempt FK to `operation_record(operation_id) ON DELETE SET NULL`. |
| `operation_execution_generation` | BIGINT nullable | Selected attempt generation; required before side effects begin. |
| `base_render_revision` | BIGINT | Revision from which the turn began. |
| `committed_render_revision` | BIGINT nullable | Exactly `base + 1` when committed. |
| `state` | TEXT | `staged`, `committed`, or `aborted`. |
| `started_at` | TIMESTAMPTZ | Database start time. |
| `committed_at`, `aborted_at` | TIMESTAMPTZ nullable | Exactly one is set for a terminal turn. |

Feature 060 adds nullable `conversation_commit_id`, `commit_position`, and
`committed_render_revision` columns to `messages`; it adds nullable `conversation_commit_id` and
`committed_render_revision` columns to
`saved_components`. New commit-owned rows require those values. Legacy rows remain null and are
treated as the revision-zero committed view rather than receiving fabricated commit identities.

A logical update's authoritative commit is one transaction that:

1. locks the chat and verifies its owner, active request generation, base revision, and selected
   operation execution fence;
2. makes the complete ordered message set and complete canonical workspace generation visible;
3. increments `chats.render_revision` exactly once, writes `snapshot_committed_at` and
   `conversation_commit_id`, stamps all commit-owned rows with that revision, and marks the commit
   committed;
4. terminalizes the attempt operation consistently with the committed result.

Pending/aborted update rows and transient stream fragments are not snapshot-visible. A retry is a
new update attempt/operation/request generation and may commit only from the still-current base
revision.
Snapshot construction reads committed messages, the complete canonical workspace (including an
explicit empty component list), and the chat revision under one repeatable database view. It retries
if a concurrent commit changes the revision; it never combines a transcript from one committed turn
with a canvas from another.

### `ConversationCommitReady` wire value

This exact prelude is the only way a client opens a fresh server-generated commit fence for a
detached, scheduled, persisted-stream, or long-job update.

| Field | Type | Rules |
|---|---|---|
| `type` | STRING | Exactly `conversation_commit_ready`. |
| `schema_version` | INTEGER | Exactly 1; booleans and unknown versions are rejected. |
| `chat_id` | UUID4 string | Must equal the intentionally active owner-validated chat. |
| `connection_generation` | UUID4 | Must equal the current registered socket generation. |
| `request_generation` | UUID4 | Fresh server-allocated commit generation; it cannot reuse or relabel a client load/turn generation. |
| `render_revision` | BIGINT | Positive target committed revision and strictly greater than the client's last committed revision. |

Those six fields are the whole value: missing and additional fields fail closed. A valid prelude
immediately precedes exactly one `ConversationSnapshot` with `snapshot_purpose='commit'` and the same
four scope/revision values. It changes no durable client state itself. Foreign/stale/duplicate
preludes are no-ops, and a prelude cannot replace an unfinished client-created commit fence; if it
is refused or its paired snapshot is lost, later hydration observes the already durable update.

### `ConversationSnapshot` wire value

| Field | Type | Rules |
|---|---|---|
| `type` | STRING | Exactly `conversation_snapshot`. |
| `schema_version` | INTEGER | Exactly 1; unknown versions are rejected without changing committed client state. |
| `snapshot_id` | UUID | Fresh server-allocated snapshot identity. |
| `chat_id` | UUID string | Owner-validated active chat. |
| `connection_generation` | UUID | Current registered socket generation. |
| `request_generation` | UUID | Snapshot/hydration request generation. |
| `snapshot_purpose` | STRING | Exactly `hydration` or `commit`; equal-revision replacement is possible only for a generation explicitly opened for hydration. |
| `render_revision` | BIGINT | Monotonic per chat. |
| `committed_at` | RFC3339 STRING | UTC timestamp of the source logical-update commit. |
| `transcript` | ARRAY | Ordered canonical messages; each has string `message_id`, role, RFC3339 `created_at`, non-empty `parts`, and `attachments` (possibly `[]`). Invalid stored content becomes an explicit recovery part. |
| `canvas` | OBJECT | Exactly `{ "target": "canvas", "components": [...] }`; an explicit empty `components` array is the committed empty canvas. After ROTE adaptation for a web socket only, every non-empty top-level component also carries the exact server-produced `_presentation={target:'web', html, workspace:{export,share}}` transport member; it is never durable semantic state or client input. |

Those eleven top-level fields are exactly the canonical frame. Transcript `parts` use only the
canonical `text`, `components`, `structured`, and `recovery` shapes, preserving order and semantic
fallback. Transcript and canvas are one frame
and one reducer action. Clients retain the old committed state until this entire value validates,
then atomically replace both. Every durable logical commit emits exactly one complete
`snapshot_purpose='commit'` frame. A server-originated commit first emits the exact
`ConversationCommitReady` value above; a client-originated turn already has its client-created
commit fence and receives no such prelude. Initial or reconnect hydration emits exactly one complete
`snapshot_purpose='hydration'` frame of the current committed revision; it never invents or advances
that durable revision. Only a complete snapshot may advance `last_committed_render_revision` or
replace committed transcript/canvas. A greater revision atomically replaces both. The first complete
snapshot with `snapshot_purpose='hydration'` accepted for a new `(connection_generation,
request_generation)` explicitly opened for hydration may also replace both at an equal revision and
marks that generation hydrated; this supports reconnect and fresh ROTE adaptation even though
`snapshot_id` is freshly allocated. An equal `commit` snapshot or equal snapshot for a normal new-
turn generation is rejected. Once hydrated, the same revision and same accepted `snapshot_id` is an
idempotent no-op, while a different identity or content is `revision_conflict`; a lower revision is
stale.

The browser validates the exact reserved `_presentation` member on every non-empty web-adapted
top-level component, requires one target and identical workspace flags, and swaps the server-rendered
fragments with the semantic state in that same reducer action. Empty canvas clears without a
presentation member. Native targets receive only semantic components. Presentation members are
added after adaptation, excluded from durable storage and semantic equality, and can never be
supplied by a client; ordinary `html`/`rendered_html` attributes do not satisfy this boundary.

Existing `ui_render`, `ui_update`, `ui_upsert`, `ui_append`, and `ui_stream_data` values carry
`chat_id`, connection/request generations, `base_render_revision`, and a strictly increasing
`frame_sequence` per generation. They may update only a disposable request-scoped preview overlay
when the base equals the current committed revision; they never mutate committed transcript/canvas
or advance its revision. A committed snapshot or terminal failure clears that overlay. Status,
progress, and acknowledgement frames affect only pending/status overlays. A mismatched chat or
generation is ignored, and a valid resume is hydrated before any welcome content.

### `ClientConversationLocator`

This small native/browser-store value is not server data. Account identity is in the storage key,
not duplicated into the value:

```text
astraldeep.active_chat.v1.<sha256(issuer || NUL || subject)>
```

| Field | Type | Rules |
|---|---|---|
| `schema_version` | INTEGER | Exactly 1; an unknown version is retained but not interpreted. |
| `chat_id` | UUID string | Intentionally active chat, written synchronously before registration/load. |
| `updated_at` | RFC3339 STRING | UTC update time. |

The record contains no token, credential, transcript, canvas, message excerpt, or display identity.
Writes use each platform's atomic preference transaction. It is cleared only by explicit new chat,
definitive sign-out/account switch, or confirmed deletion. Process recreation, app backgrounding,
socket loss, hydration failure, and service restart do not clear it. If the server returns an
owner-validated not-found/deleted result, the client shows an explicit recovery state and clears the
locator only after that definitive response.
Watch owns the same store contract in `AstralWatch/ConversationResumeStore.swift`; endpoint override
synchronization is not reused as conversation persistence.

## 9. Windows Deployment Profile

`WindowsDeploymentProfile` is a reviewed, non-secret JSON file bundled into the 0.4.0 executable.
Its authoritative JSON Schema is `contracts/windows-deployment-profile.schema.json`.

| Field | Type | Rules |
|---|---|---|
| `schema_version` | INTEGER | Exactly the supported profile schema. |
| `profile_id` | STRING | Immutable deployment-profile identity. |
| `release_id` | STRING | Immutable release identity bound to the artifact. |
| `client_version` | STRING | Strict SemVer; 060 production value is `0.4.0`. |
| `distribution` | STRING | Exactly `production` or `generic_developer`. |
| `local_only` | BOOLEAN | Must be false for production and otherwise agree with endpoint/authority policy. |
| `authority` | STRING | Complete approved OIDC authority URI with no userinfo, query, or fragment. |
| `websocket_endpoint` | STRING | Complete approved service WebSocket URI with no userinfo, query, or fragment. |
| `client_id` | STRING | Existing deployment client identity/mode. |
| `auth_mode` | STRING | Existing supported Keycloak/OIDC mode only. |
| `override_policy` | OBJECT | Whole-profile managed/CLI/persisted permissions; no per-field overlay. |
| `agent_connection.byo_host` | OBJECT | Required `disposition`: `authenticated_ui_tunnel` or `disabled`. |
| `agent_connection.legacy_tools` | OBJECT | Required `disposition`: `disabled` or `managed_api_key`, plus `credential_source`: `none` or `managed_environment_agent_api_key` with the schema-enforced matching pair. |

The production 0.4.0 profile uses `byo_host.disposition='authenticated_ui_tunnel'`,
`legacy_tools.disposition='disabled'`, and `legacy_tools.credential_source='none'`; it contains no
shared agent key.
Placeholder values, partial profiles, production-local profiles, URI/profile inconsistency, URI
userinfo/query/fragment components, undeclared fields, and secret-like values fail validation before
signing. The whole-profile precedence is:

1. explicit managed or `--deployment-profile` profile;
2. permitted atomically persisted QSettings profile;
3. bundled release profile; then
4. development-only local defaults.

Resolution produces one frozen in-memory `EffectiveDeploymentProfile` with `source` and canonical
profile digest. Qt surfaces, authentication, transport, and hosted agents consume that same object
and do not re-read environment/settings. Connection failure retains it. `--validate-deployment`
reports only profile/release IDs, source, digest, versions, and disposition—not values or credentials.

## 10. Release Evidence

> **Owner override (2026-07-16)**: Spec 060 prepares and parses canonical evidence locally before
> push, then requires independent protected-CI validation. The bounded exception/debt data model
> remains active, but approval, append-only registration, resolution, and publication use protected,
> environment-approved GitHub Actions jobs with the built-in short-lived job token. References below
> to repository-scoped GitHub Apps, installation tokens, or a custom token broker are superseded.

Release evidence is tracked/artifact JSON validated by `contracts/release-evidence.schema.json`, not
a database table and not an assertion inferred from source-only tests.

### `PlatformEvidence`

- `document_type='platform_evidence'`, `schema_version=1`, UUID `evidence_id`, full
  `candidate_sha`, `release_id`, and strict-SemVer `release_version`
- `platform` exactly `backend`, `web`, `windows`, `android`, `macos`, `ios`, `watchos`, or `docs`, plus
  `target_description`
- a non-null exact candidate `artifact` for passed, failed, and exception-eligible unavailable
  reports; a `runner` object with OS/architecture/image/name and GitHub-hosted versus self-hosted
  environment identifies the actual report producer, and a `workflow` object carries its
  run/attempt/job identity; each report matches one independently attested producer manifest
- RFC3339 `started_at`/`completed_at`, `outcome` exactly `passed`, `failed`, or `unavailable`, the
  paired nullable `unavailable_reason`, and a nullable `unavailability_observation`; unavailable
  reports require a protected immutable observation with failure class, attempted target workflow,
  target runner requirement, timestamp, immutable reference, and re-hashed bytes
- `checks[]` with canonical check ID, `outcome` exactly `passed`, `failed`, `not_applicable`, or
  `not_run`, duration, detail code, applicability reason, `measurements[]`, and
  `evidence_artifacts[]`
- each quantitative measurement names its metric, aggregation, non-negative observed value, unit,
  sample count, canonical comparator, and threshold; each raw report/JUnit/log/screenshot/video
  reference carries an immutable reference and SHA-256 digest
- `staging_environment` identifies the externally reachable request namespace, topology
  `shared_reachable_ephemeral`, commit-derived candidate image reference/digest, sanitized synthetic
  representative-data fingerprint, pre/post migration revisions, real Keycloak posture, real
  PostgreSQL posture, both required `background` and `scheduler` worker paths, candidate TLS
  endpoint, deployment workflow run, and candidate-owned macOS-host capability signal; only the docs
  report may use null for a passing candidate, and an unavailable shipping-client report still
  requires this verified staging identity
- Windows evidence additionally identifies clean-profile deployment validation, packaged worker
  round trip, no-dialog GUI launch, and runtime-lock digest
- Apple evidence additionally identifies first-login LLM valid/invalid/slow/unavailable trials and
  measured acknowledgement, responsiveness, loading, success, and ten-second terminal bounds

Evidence details contain only bounded non-sensitive codes, timings, counts, and artifact references.
A platform document may contain each canonical check ID at most once; JSON Schema constrains the
shape while the policy aggregator rejects duplicate IDs and requires the exact platform check set.
A passing document requires `passed` for every mandatory platform check. `not_applicable` is legal
only for a policy-enumerated capability gap: current Watch `personal_agent`, and
`macos_personal_agent_host` exactly when the candidate capability says unsupported. A supported,
missing, malformed, refused, or unacknowledged host cannot use N/A. The policy
validator requires canonical named metrics for thresholded checks, including 10,000 scheduler
interleavings, 50 migration trials, 100 process/BYO trials, 20 continuity trials, 30 Apple trials per
platform, latency percentiles/maxima, success rates, and zero duplicate/residual/unresolved counts.
The macOS-hosting applicability record is the sanitized canonical value returned identically by
authenticated `/api/dashboard` and `system_config.config` at
`capabilities.personal_agent_host.macos`. Its `supported=false`, empty-version, null-feature form
makes only `macos_personal_agent_host` not applicable. `supported=true` requires runtime contract v2,
`source_feature='059'`, the direct-download artifact's structured registration, a server-issued
`agent_host_registered` acknowledgement, and a passing host check. Missing/malformed capability
data or a refused/unacknowledged attempted registration blocks the candidate.

### `ReleaseEvidenceSet`

- `document_type='release_evidence_set'`, `schema_version=1`, `policy_revision='060-v1'`, UUID
  `evidence_set_id`, exact `candidate_sha`, `release_id`, and strict-SemVer `release_version`
- RFC3339 `generated_at` and canonical `required_targets[]`
- complete nested `evidence[]` platform documents and complete nested `exceptions[]` documents (not
  a differently shaped list of references)
- aggregate `decision` exactly `passed`, `failed`, or `blocked`

This embedded decision is the protected policy engine's evidence-set result, but it is not by itself
a release authorization; only an independently attested `TrustedReleaseDecision` can qualify it.

Aggregation rejects evidence from a different SHA, mutable/missing artifact, mismatched release
identity, duplicate platform target, duplicate check ID, or incomplete check set.
The protected verifier receives one workflow-provenance manifest per producer job plus stage-deploy
and protected exception-approval manifests governed by `contracts/release-trust.schema.json`. Shape
validation alone does not make them trusted. Repository rules require the reusable verifier pinned
independently of the candidate; its signer digest and certificate identity come from an immutable
protected environment/ruleset, never the candidate or manifest. It reconstructs exact current-run
jobs/artifacts through GitHub API state, verifies each attestation and downloaded subject bytes, and
requires each report's run/attempt/job and complete runner identity to match exactly one producer
manifest. The caller aggregate is not a trust boundary.
`bundle://` references resolve only beneath the attested evidence root without symlinks/traversal;
canonical same-repository `gh://` references identify either a run/attempt/artifact/member or an
official release/asset by numeric IDs; canonical `oci://` references are digest-qualified. The
validator derives each URI from verified manifest fields and recomputes downloaded/bundled bytes.
HTTP(S), unknown/mutable references, caller-selected staging URLs, credential-bearing archived
endpoints, and supplied hashes that do not match recomputed bytes are rejected. Candidate endpoint,
deployment, artifact, and runner identity are compared to the attestation-verified manifests before
a reachability check.

### `TrustedReleaseDecision`

After all inputs exist, the protected verifier runs the policy and changed-code coverage
implementations from its independently pinned signer revision, not the candidate checkout. Its
attested `document_type='trusted_release_decision'` manifest binds UUID decision/evidence-set IDs,
repository, immutable base and candidate SHAs, release ID, exact evidence-set and coverage artifact
members/digests, every consumed manifest ID plus its canonical run/artifact/member identity and
re-hashed bytes, protected policy SHA-256, passing decision, measured
coverage at or above 90%, required-check name, workflow/job/runner, builder identity, and generation
time. It always binds the protected `release-evidence-debt` repository/ref, exact current commit and
tree, immutable commit reference, canonical path-to-byte-digest snapshot SHA-256, and verification
time—even when the current release uses no exception—plus a protected `valid_until` no later than
the earliest used approval expiry. The verifier reads the head both before and after evaluation and
rejects stale or concurrent movement. Before use, a first protected landing installs this exact verifier/policy/all-three-schema
revision plus publication roots on the protected default branch, records its immutable identities,
and leaves the automatic caller/required check disabled. The candidate rebases onto and verifies
that root; only a second checkpoint enables `release-readiness / protected-decision`. Execution
extracts the exact pinned archive and rejects same-HEAD dirty substitution or a bootstrap run that
installs the root it claims to trust. Repository rules require the installed protected workflow
identity, not a name-only status; candidate workflow output, a same-name job, candidate policy
execution, or a self-declared passing evidence-set decision cannot substitute.

### `EvidenceExceptionRequest` and protected debt ledger

A current-run immutable artifact contains exactly
`document_type='evidence_exception_request'`, `schema_version=1`, UUID `exception_id`, full
`candidate_sha`, `release_id`, canonical shipping-client `platform`, non-empty `missing_checks[]`,
bounded `reason`, GitHub `requester_login`, RFC3339 `requested_at`, `maximum_valid_days=7`, and
`blocks_next_release=true`. It contains no reviewer, approval state/time, or expiry because those
facts do not exist before review. Backend, docs, staging, trust, policy-integrity, failed-product,
and Apple first-login gaps are not exception-eligible. A request may cover only checks marked
`not_run` by an `outcome='unavailable'` report whose reason is independent of product behavior.

The protected `release-evidence-exception` deployment exposes the exact request artifact ID,
canonical reference, digest, exception/candidate/release IDs, and requester before review. After an
allowlisted release owner other than the requester approves, a separately pinned protected registrar
re-queries that same-repository API state, resolves and re-hashes the request, chooses `approved_at`
and an `expires_at` no more than seven days later, and creates a canonical
`document_type='release_evidence_debt'` entry. It appends that entry create-only beneath
`debts/<exception_id>.json` on protected non-force-push
`refs/heads/release-evidence-debt`; current debt is never committed into the candidate tree, avoiding
a candidate-SHA self-reference.

The registrar emits an attested `trusted_exception_approval` binding actual reviewer/requester and
API deployment state; approval/expiry; the exact request artifact; canonical approval payload; the
ledger repository/ref, parent and new commits, unique path, entry bytes/digest, and immutable
`ghgit://.../commits/<sha>/paths/<path>` reference; and its protected workflow/builder identity. The
protected verifier proves the new ledger commit descends from the expected parent, every prior entry
is byte-identical, the new path did not exist, exactly one canonical entry was added, the protected
ref rejects force pushes/candidate writes, and all embedded values match re-hashed request and API
facts. A guessed approver, candidate-authored receipt, duplicate path, or mutation is insufficient.

Each validation reads historical debt from one exact protected ledger commit via
`--exception-ledger-repository`, `--exception-ledger-ref`, and `--exception-ledger-commit`. For every
prior debt, each `(platform, missing_check)` must be `passed` in the later candidate with no
replacement exception. The protected registrar then appends a canonical
`release_evidence_debt_resolution` beneath `resolutions/<resolution_id>.json` and emits an attested
`trusted_debt_resolution` receipt binding the parent/new commits, unique path, exact bytes/digest,
original debt, and later passing evidence plus producer provenance. The verifier consumes that
receipt before its final decision. A resolution satisfies only its named debt; a later outage for the
same check creates a new debt and cannot reuse the old receipt. Missing, duplicate, forged, partially
resolved, rewritten, malformed, or unresolved history blocks. The current
exception can qualify only after its approval-and-registration manifest is verified and included in
the protected final decision.

### `WindowsDraftVerificationProvenance`

The passing `ReleaseEvidenceSet` is a pre-sign input. An owner-approved protected publisher emits one
separately schema-validated `document_type='windows_draft_verification_provenance'` record with
`schema_version=1`, UUID `publication_id`, exact candidate/release/SemVer identity, and an exact
attestation-verified `trusted_release_decision` reference: decision/evidence-set IDs, canonical
manifest artifact ID/member/digest, required-check name, protected builder/policy identity, and
verification time. It also binds readiness/publication workflow identities; the installed full
controller/publisher commits and certificate identity; the legacy schema field carrying the exact
native publisher-policy digest (not a token broker);
protected deployment/reviewer/requester/API/payload approval; legacy bridge blob hash; generated
timestamp; the matrix-tested executable plus post-upload re-downloaded `AstralDeep.exe`,
`SHA256SUMS`, and `cosign.bundle`; and official repository/tag/release ID, three distinct asset IDs,
constant asset names/count, `draft_state=true`, `prerelease=false`, target SHA, and verification time.
The draft release also records `release_name` equal to its exact `v${release_version}` tag and
`latest_disposition='make_latest_on_publish'`; official-mode success additionally requires the
published API-shaped `/releases/latest` response consumed by v0.3.0 to identify that same release.

To remain accepted by the shipped v0.3.0 updater, detached signing runs only in the byte-pinned
`release-windows.yml` compatibility bridge on the protected publisher-created `v0.4.0` tag. The
bridge has contents/actions/attestations-read plus OIDC authority and no release mutation; it may retrieve only
the exact originating run/attempt/artifact identity and re-hashes those bytes. Its exact legacy SAN,
template hash, and actual v0.3.0 verifier pass are recorded. Detached Sigstore MUST NOT alter executable bytes,
so tested and draft EXE digests are equal. The signing object fixes its legacy identity, GitHub OIDC
issuer, v0.3.0 policy verifier, `verification_outcome='passed'`,
`executable_bytes_modified=false`, and `rebuild_performed=false`.

Policy uploads the three files only to a draft/quarantined release, resolves and re-downloads each
through its canonical same-repository numeric release/asset identity, hashes all bytes, parses
`SHA256SUMS` to the tested EXE digest, and verifies the downloaded bundle against the downloaded EXE
and exact legacy Sigstore identity/issuer using the shipped verifier. It requires tag exactly
`v${release_version}`, equal build identities, target-SHA equality, exact protected decision and
approval/publisher API state, current time before the decision `valid_until` and every applicable
approval expiry, no existing collision, and no build/PyInstaller step. The complete
record is schema-validated while the release is still a three-asset draft; only then may the protected
publisher make it public as latest and verify `/releases/latest`. Failure removes only the just-created tag/draft before publication. This
record proves that exact draft is ready; it does not claim a public transition occurred.

### Standard-library schema-validator profile

The release validator supports every validation keyword used by all three tracked schemas and fails
closed if a new unsupported assertion keyword appears. Its declared profile covers `$schema`, `$id`,
`$defs`, local `$ref`, `title`, `description`, `$comment`, `type` (including unions and `number`),
`const`, `enum`, `required`, `properties`, recursive `additionalProperties`, `pattern`, active
`format` checks for UUID/date-time/URI, `minLength`, `maxLength`, `minimum`, `minItems`, `maxItems`,
`uniqueItems`, `items`, `contains`, `allOf`, exact-one `oneOf`, `not`, and `if`/`then`/`else`.

Only local `#/$defs/...` references are allowed; no remote retrieval occurs. `uniqueItems` uses deep
JSON equality, `contains` requires a match, Python booleans are not numbers/integers, duplicate keys
and non-finite numbers fail JSON decoding, and input size/nesting are bounded. A schema-walk test and
mutation corpus exercise every keyword/conditional branch and reject any unrecognized assertion.
Strict-SemVer patterns reject every whitespace character, including trailing CR/LF, in addition to
v-prefixes and leading-zero numeric identifiers.

## Migration, Backfill, and Recovery

All database work lives in `backend/shared/database.py::_init_db()` under the advisory-lock startup
algorithm above. There is no second migration framework and no ad-hoc deployed SQL.

### Forward migration (`057.001` -> `060.004`)

1. Under exact advisory key pair `(1095980114, 60001)`, create operation policy/slot/record/submission-result
   tables and indexes. Seed slot rows from validated effective configuration. Add nullable
   `background_task.operation_id` and operation execution generation; new writes require the
   operation ID and started writes require the generation. Historical rows remain readable, while a
   deterministic backfill may populate it only when an exact historical accepted-operation identity,
   normalized input digest, and terminal state already exist. All other legacy rows retain null;
   migration never fabricates an accepted operation or execution generation.
2. Create occurrence and `effect_ledger` tables, including the nullable owner-scoped Run-now
   submission identity and its partial unique index; add nullable `job_run.occurrence_id`,
   `attempt_number`, `operation_id`, operation execution generation, and occurrence claim generation.
   Historical runs stay nullable because their exact scheduled firing cannot be reconstructed
   honestly. New repository writes require the complete attempt and fence tuple.
3. Add user-agent pointer/generation/policy columns, then create revision, host, runtime, and request
   tables and pointer foreign keys. Runtime `process_id` starts nullable; only the selected host's
   first valid `starting` CAS binds it, and the partial uniqueness rule applies once non-null.
   Existing active agents receive a `legacy_pending` revision row
   and retain their prior live path. Server-local artifacts are hashed during the guarded backfill;
   host-only bundles become fully versioned during mandatory host inventory reconciliation. A
   `legacy_pending` host-only revision keeps digest/manifest/path/runtime/lock fields nullable, is
   shown as updating/offline, and is not invocable under the 060 router until inventory supplies an
   accepted complete tuple and the staged validity constraints pass. No fabricated artifact digest,
   runtime version, or ready state is recorded and the last working bundle is not deleted.
4. Add draft CAS columns/tables. Current UUID draft IDs are copied to `draft_uuid`; a guarded
   application backfill assigns UUID4 aliases only to exceptional legacy non-UUID rows. Existing
   unpublished drafts receive one immutable UUID target-agent identity; revising drafts preserve
   their existing target identity. Display names/slugs are unchanged.
5. Create maintenance tables. Existing `interaction_log.synthesized` values are preserved; no
   historical item is reset or falsely claimed. New synthesis selection uses membership rows.
6. Add chat render-revision/commit fields, `conversation_commit`, and nullable commit/revision metadata
   on messages/components. Existing message and workspace data is untouched and treated as the
   legacy revision-zero committed view; it receives no fabricated turn identity. The first 060
   snapshot emits revision zero, and every later logical conversation update increments once at its
   atomic commit.
7. Run every `CREATE`, `ALTER ... ADD COLUMN IF NOT EXISTS`, backfill, constraint validation, and
   index operation repeat-safely. Write `revision='060.004'` only after the complete transaction.
8. Independently acquire exact advisory key pair `(1095980114, 60002)` and run the exact policy-
   revision sweep even if the schema marker was already current.

All operation references held by longer-lived rows are nullable foreign keys with
`ON DELETE SET NULL`; their non-null execution-generation columns preserve the historical fence
after the operation's 24-hour retention purge. Temporary nullable compatibility columns and staged
`NOT VALID` constraints are enforced by 060 repository writes immediately. They remain nullable or
unvalidated in this revision only where honest historical reconstruction is impossible. Inventory/
reconciliation metrics and an explicit validation pass—not migration guesses—are prerequisites for
a later `NOT NULL` or `VALIDATE CONSTRAINT` cleanup.

### Representative migration tests

- empty database, current 057 database, and representative populated data containing active jobs,
  historical runs, terminal/running background tasks, same-name drafts, deleted/live agents,
  server/host-only bundles, chats with structured transcript content, and synthesized/unsynthesized
  interactions;
- two and more concurrent starters, one killed before commit, repeated boot at current revision, and
  a policy-only constant change with unchanged schema marker;
- all backfills retain owner and legacy identities, no active revision is deleted, historical run
  truth is not fabricated, and repeated execution produces identical rows/indexes;
- downgrade/read compatibility smoke plus forward re-upgrade on the same representative snapshot.

### Rollback and incident recovery

- This revision is additive. The preferred incident rollback is to stop all 060 writers, preserve a
  database and artifact-root snapshot, disable affected scheduler/agent execution through existing
  controls, and redeploy the prior image. Prior code ignores extra tables/columns and existing
  legacy columns remain populated by compatibility writes.
- Do not drop 060 tables or lower markers manually during an incident. A prior image may run its
  guarded schema path after all newer instances are stopped; a later 060 redeploy safely reapplies
  its idempotent revision.
- Before rolling back personal-agent hosting, drain runtime requests and atomically restore the
  legacy live-path pointer from `active_revision_id`; never delete immutable revision directories.
  If that compatibility projection cannot be proven, keep hosting disabled and redeploy 060 rather
  than risking stale routing.
- Before rolling back scheduling, stop new claims and let claimed occurrences terminalize or leases
  expire. The occurrence/effect ledger is retained so a later forward deploy never repeats a
  published visible effect.
- A failed migration transaction rolls back and releases the advisory lock. A failed file
  publication leaves only UUID-scoped staging/immutable data; startup reconciliation compares
  digests and database pointers, removes abandoned staging after its retention bound, and never
  guesses a live revision.
- Destructive removal of 060 structures is a separate, lead-approved future migration after its
  data is retired; it is not part of feature rollback.
