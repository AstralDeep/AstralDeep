# Persistent assignment data model

Feature: `079-persistent-agents`. Target Plane schema: **079.001**, additive over
the pinned `075.001` schema. This is a design contract, not a claim that the schema
or methods below already exist.

## Ownership and composition

AstralPlane owns storage, transactions, constraints, bounded selection, comparison
and exchange, leases, and recovery. Deep owns authenticated principals, source
adapters, relevance, model planning, permission/PHI/egress checks, trusted cost
quotes, approval policy, dispatch, and rendering. The four new tables live only in
Plane's guarded migration registry; Deep receives a stateless
`AssignmentRepository` through `RepositoryCatalog.assignments` on its existing
application runtime. No new pool, task broker, raw SQL in Deep, dependency, or
generic workflow framework is introduced.

All owner reads and writes require `owner_id`. Composite assignment/owner foreign
keys prevent cross-owner event/action/activity links. Cross-owner due/recovery and
retention operations are explicitly named `for_administration`. Returned records
are frozen detached values; token/claim/argument-bearing fields are excluded from
`repr`. Query/public UI projections never contain a lease token, access token,
refresh token, raw credentials, or a dispatch permit.

## Relational core: four tables

| Table | Durable purpose | Main keys and indexes |
| --- | --- | --- |
| `persistent_assignment` | Definition, control authority, bounded progress/task graph, lease, limits and counters | UUID primary key; unique `(id, owner_user_id)`; unique `(owner_user_id, submission_id)`; owner/update/id list index; partial active/due index |
| `persistent_assignment_event` | Source identity dedupe and bounded pending source context | UUID primary key; composite assignment/owner FK; unique `(assignment_id, source_key, item_key, source_revision)`; assignment/state/discovery/id pending index |
| `persistent_assignment_action` | Immutable action proposal, resource reservation, approval, dispatch receipt and effect dedupe | UUID primary key; composite assignment/owner FK; unique `(assignment_id, action_key)`; assignment/state/update/id and pending-expiry indexes |
| `persistent_assignment_activity` | Immutable, owner-visible transitions/findings/intervention requests | UUID primary key; composite assignment/owner FK; unique `(assignment_id, activity_key)`; assignment/sequence index; unique `(assignment_id, sequence)` |

Each child table stores `owner_user_id`; joins and mutations use it explicitly.
Assignment deletion cascades only after the authorized account/assignment purge
path has established a stopped/completed terminal state and fenced execution.
Normal stop never deletes recovery or
deduplication evidence.

### Assignment fields

- Identity: `id`, `owner_user_id`, immutable `submission_id` and
  `submission_digest`, creation/update/terminal UTC timestamps, owned
  `conversation_id` (optional).
- Owner definition: bounded `name`, `instructions`, `source` JSON, `allowed_tools`
  JSON, `consented_scopes` JSON, `offline_grant_id`, `limits` JSON, optional explicit
  completion condition. A source binds a server-registered reader tool and
  normalized source selection; it never carries provider credential bytes.
- Versions: `instruction_revision` starts at 1 and advances on a definition or
  authority/limit revision; `control_epoch` starts at 1 and advances on every
  revise/pause/resume/stop/revoke transition; `state_version` starts at 1 and
  advances on every assignment write. Worker writes use the current claim and
  state-version CAS. Owner controls compare the owner-visible instruction revision
  and control epoch, so a harmless worker heartbeat cannot prevent a pause.
- Lifecycle: `active`, `paused`, `stopped`, `completed`. `stopped` and `completed`
  are terminal. Runtime `phase`: `waiting`, `checking`, `investigating`,
  `delegating`, `waiting_approval`, `waiting_authorization`, `budget_exhausted`,
  `reconciliation`, `failed`. The shared UI derives paused/stopped/completed from
  lifecycle before showing phase.
- Wake state: `next_wake_at`, bounded `wake_reason`, `pending_wake` boolean,
  `wake_generation`, `last_check_at`, `consecutive_failures`, `next_retry_at`.
  Overdue cadence coalesces into one current check; missed intervals do not become
  one row per interval. An earlier eligible child/action completion coalesces into
  this same wake state. Paused assignments do not execute due work.
- Lease: `claim_generation`, `claim_token`, `claimed_by`, `lease_expires_at`,
  current `operation_id` and `operation_execution_generation`. The claim is
  assignment-local scheduling authority; the existing work-admission operation
  remains the sole operation-state and capacity authority.
- State: bounded `checkpoint` JSON, `tasks` JSON, `usage` JSON; current safe error
  code and bounded owner-visible explanation. No raw model reasoning is retained.

Assignment creation and owner active-count admission share an owner-keyed
transaction advisory lock, preventing parallel creates from exceeding the cap.
The initial upper bounds are 25 active/paused assignments and 256 total retained
assignments per owner across **all** lifecycle states. Create/stop cycles cannot
bypass the total quota. Capacity refusal persists until authorized terminal
deletion frees a slot. Lower policy limits may be supplied; a client cannot
increase these storage hard limits.

An owner `request_check` coalesces one eligible check under the current revision
and epoch. It does not change authority, invalidate the claim, override cadence,
reset usage, or create catch-up occurrences. An identical submission returns the
same wake receipt. Requests during an active episode set one pending wake; their
earliest execution remains bounded by the last check plus configured cadence and
the owner's request rate limit. Paused/stopped/completed assignments reject it.

### Bounded JSON progress and task graph

JSON is appropriate for the small graph: one episode owns a claim, each graph
transition locks its assignment row briefly, and no worker transaction includes a
model call, tool invocation, network operation, or long wait. Relational task rows
are unnecessary for the required bounded fan-out. JSON is **not** used as a
replacement for indexed event/action dedupe, action approval state, or leases.

`checkpoint` contains `schema_version: 1`, source cursor and comparison digest,
last accepted source revision, a bounded progress summary, retained findings with
stable identities, next step, and references to event/action/task results. Each
cursor is opaque to Plane and scoped to its source configuration digest. Deep
must clear/revalidate it after a source change. Total canonical UTF-8 checkpoint
size is at most 65,536 bytes; complete assignment state including graph is at
most 262,144 bytes. Inputs exceeding bounds are refused, never truncated into a
different instruction or approval.

Each task contains:

```text
task_id, plan_key, instruction_revision, event_id, parent_task_id,
depends_on[], title, instruction, depth, allowed_tools[],
state, attempt_count, task_generation, claim_generation,
operation_id, started_at, completed_at,
result_digest, bounded_result, provenance, incorporated_by
```

Task states are `pending`, `running`, `completed`, `failed`, `cancelled`,
`reconciliation`. `incorporated_by` maps an authorized parent task identity to its
immutable result digest; the normal tree has one parent. Results are summaries
and evidence references, not copied transcripts. Deep scans untrusted delegated
results before incorporation and records an explicit quarantined failure when
necessary.

Hard bounds: 32 tasks per current graph, at most 5 direct children, depth at most
4, at most 8 dependencies per task, instruction at most 4,096 bytes and retained
result at most 8,192 bytes. Lower consented limits apply. Plane validates all
references, unique identities, acyclicity, dependency completion and subset tool
constraints on accepted transitions. Completed results cannot be overwritten.
`plan_key` replay must match the complete canonical plan digest; callers cannot
replace a plan under the same identity.

Task completion is persisted immediately, independently of the parent's final
checkpoint. Incorporation and the resulting parent checkpoint update are one
transaction. A repeat with the same child/result/parent/checkpoint identity
returns the existing receipt; changed semantics conflict. Recovery retains
completed tasks, requeues only safely retryable unfinished tasks and never resets
usage or action identities. Compacting a finished graph requires every retained
child result to have been incorporated or explicitly disposed of and all action
references to remain in the event/action ledgers. The graph is not the sole proof
that an external effect occurred.

### Source event

Fields: `event_id`, assignment/owner, `source_key`, `item_key`, `source_revision`,
canonical `identity_digest`, `context_digest`, bounded context JSON, discovery
time, disposition (`pending`, `processing`, `completed`, `irrelevant`, `failed`,
`reconciliation`), accepted instruction revision, related task identities,
completion time and result digest.

Deep derives identities from the registered source adapter's stable item/revision
contract. A public-page monitor can bind its normalized page selection and a
content digest plus a persisted observation sequence; generic readers can supply
provider item/revision identifiers. Snapshot sources compare with the last
accepted digest before assigning that sequence. An unchanged snapshot emits no
event, while A-to-B-to-A is a new observation rather than replay of the original
A. Sequence allocation and cursor advancement share the batch transaction;
replaying the same check cannot allocate another sequence.
Repeated or reordered identical identities are replay, even across instruction
revisions. A deliberate owner-requested reprocessing operation needs a distinct
explicit processing identity; changing an instruction must not accidentally
replay completed effects. The same event identity with a changed context digest
is a conflict, not an update.

A source batch has at most 100 items and 65,536 aggregate context bytes (8,192
per item). Batch insertion, cursor advancement, and the appropriate wake state
commit together. If capacity is exhausted, the cursor is not advanced past
unaccepted items. Only an authenticated adapter can establish a monotonic replay
floor permitting old dedupe tombstones to be removed; arbitrary page/provider
ordering does not establish such a floor.

### Action, reservation and approval ledger

The action row covers model calls, source/tool reads, delegated work admission,
internal publication, and external mutations. A stable server-derived
`action_key` includes the logical event/task/step identity; it is not replaced by
a fresh UUID on every retry. The immutable request digest covers action kind,
agent/tool, bounded arguments, target, source preconditions, allowed tool/scopes,
instruction revision, sensitivity disposition, idempotency boundary, and maximum
resource cost. Reusing a key with different bytes conflicts.

Fields include action/task/event identities; instruction revision and approval
control epoch; immutable canonical request JSON/digest; effect boundary
(`internal_transaction`, `downstream_key`, `read_only`, `unreplayable`); exact
downstream idempotency key when applicable; state; reservation values; trusted
quote digest/expiry; approved/declined/consumed times; approval expiry and decision
submission identity; dispatch token and operation fence identity; result
digest/bounded result; safe error code; timestamps. Bounded provenance references
identify the source and human approver. Credentials are injected only at dispatch
and never included in this row.

States: `ready`, `proposed`, `approved`, `reserved`, `started`, `succeeded`,
`failed_not_started`, `failed`, `declined`, `invalidated`, `uncertain`.

- Sensitive work begins at `proposed`; approval preserves immutable request
  bytes. Approval alone does not execute or reserve unlimited resources.
- A normal or valid approved action obtains a finite reservation before start.
  Starting atomically consumes approval, charges admission counters and issues
  one dispatch token. Identical replay retrieves its existing state/receipt.
- Rechecking the assignment control epoch, instruction revision, claim and
  current authority/preconditions is mandatory before this transition. Plane
  checks persisted bindings; Deep supplies freshly verified permission and
  target precondition digests. Plane does not authenticate those claims itself.
- An interactive-only action can start only through Deep's existing attended
  dispatch path with an owner interaction receipt. Approval never changes an
  agent's unattended policy. The assignment observes its durable outcome later.
- An approved foreground action obtains a **new**, action-bound claim through
  `claim_for_approved_action` when the assignment waits without a background
  lease. The claim binds the exact approval, current epoch/revision and current
  authenticated owner interaction. It cannot resurrect the old worker or claim
  unrelated background work. Existing work admission supplies the foreground
  operation and the complete ordinary gate stack still applies.
- `started` records the dispatch-start linearization point, not proof that a
  provider performed an action. Pause/stop cannot issue a later start permit.
  The transaction ordering of start permits and controls is authoritative:
  controls refuse permits after their committed epoch change. Previously begun
  actions may finish and remain explicitly reported as in-flight/uncertain,
  including an authorized dispatch whose network response is still pending.
  This does not promise a distributed network-byte barrier or recall of an
  already-authorized external request. An old lease cannot obtain a new permit
  or publish a stale continuation.
- A completed action receipt is immutable and can be returned without execution.
  A late external outcome may be recorded by its exact dispatch token after
  pause/revision/lease loss, because losing authority must not erase knowledge of
  an issued effect. This **does not** authorize stale task completion, checkpoint
  mutation or publication. The current worker may incorporate the observation
  only after revalidation.
- Crash recovery does not retry `unreplayable` started actions. It holds them
  `uncertain` for explicit reconciliation. A declared downstream key is eligible
  for the same-key replay only when Deep's reviewed adapter guarantees that
  boundary. Read-only retries still consume new finite resource reservations;
  a reservation is never an infinite retry permit.

Approval is invalidated by instruction/source/permission/limit revision, pause,
stop, revocation, expiry or changed parameters/preconditions. Approve/decline are
owner/revision/epoch CAS operations, idempotent under an immutable decision ID.
An expired approval cannot be renewed in place; a new reviewed proposal is
required. An explicit reconciliation decision records evidence and cannot
silently classify uncertainty as success or reset spent resources.

### Resource limits

Use integers: counts, tokens, elapsed milliseconds and, when selected by the
owner, spending micro-units in one currency. No floating-point money. The
persisted **mandatory** limits include
cadence, retries per task/check (default 3, hard maximum 10), concurrent tasks,
task/delegation depth, lifetime
and daily model/tool/token/time ceilings. An owner-selected monetary ceiling is
**optional**. All children and retries debit
the same assignment row; parent concurrency is checked under the same lock.

A reservation always names its exact logical attempt and finite maximum
model/tool/token/time charge. When a monetary ceiling is selected, it also needs
a trusted finite spend upper bound, quote digest and expiry for every covered
call. Missing quotes then refuse activation, affected definition revisions and
dispatch with `cost_bound_unavailable`; the owner must either supply an available
trusted quote or explicitly choose usage-only limits. The maximum accounts for
bounded model input/output and downstream costs. An explicit zero-spend quote
must be trusted; it cannot be inferred from the absence of pricing metadata.

With no monetary ceiling, `currency`, `spend_micro_units`, `quote_digest` and
`quote_expires_at` may be null. Monetary usage is explicitly `unknown` unless a
verified report exists; null never means zero or free. Mandatory finite
call/token/time reservations remain enforced. The baseline public-page monitor
works with these usage-only limits and requires no new operator price
configuration. Adding a monetary cap is a reviewed definition revision and must
account conservatively for prior unknown usage; it cannot silently reset history.

Reserve under lock only if `spent + outstanding + requested` fits every ceiling.
Starting prevents automatic release; unsettled started calls conservatively retain
their maximum charge across restart. A proven never-started cancellation may
release its reservation. Settlement can replace the maximum with verified actual
usage at or below it; a missing usage report retains the maximum. Actual overrun
is retained as actual usage and blocks subsequent work; it is not clamped into
fictional compliance. Daily rollover changes the day bucket only, preserves
lifetime counters and outstanding liabilities, and cannot refund old started
work. Owner increases to limits require a reviewed definition revision.

### Activity and retention

Activities carry sequence, stable activity key, type/status, bounded safe title
and summary, source/task/action references, creation time, and notification
disposition. State mutation and its activity record are one transaction.
Owner-visible findings in this table are the durable publication; live frames
notify clients to render that state and cannot create another finding. Unchanged
checks only update `last_check_at`/waiting state; they do not append a notification
for every empty poll.

Hard per-assignment defaults: 1,000 retained activity rows, 10,000 event identities,
10,000 action identities, and at most 100 pending approval rows. More restrictive
deployment limits apply. Full event/action payloads may be redacted according to
existing sensitive-data retention while preserving bounded identity/digest/state
tombstones. Event and action tombstones are not silently dropped merely because
they are old: that would make completed effects replayable. At the hard ledger
cap, wait with `history_capacity_exhausted` until authorized retention establishes
a safe replay floor or the owner retires the assignment. An infinite history with
arbitrary reordering cannot provide both finite storage and lossless dedupe.

Activity pruning removes only old non-pending rows and never the sole retained
description of an unresolved approval, failure or reconciliation hold. Payload
redaction and identity pruning are separately counted in a bounded retention
receipt. Authorized owner deletion permits either fenced terminal lifecycle,
`stopped` or `completed`, with no unresolved started/uncertain effects. It reuses
the existing account-retirement/audit-retention path and cannot leave an executable
orphan. Both terminal deletion paths and account retirement require tests.

## Migration and recovery

Implement `079.001` through `astralplane.database.migrations.MIGRATION_REGISTRY`,
bump `database.revision.SCHEMA_REVISION` and predecessor metadata, and regenerate
the registry digest. Migrations add constraints/indexes idempotently and validate
the resulting shape. Current tables and scheduled-job behavior remain unchanged.
Test against representative `075.001` data and a repeated application, as well as
a fresh database. Update Deep's composition schema/digest only after qualification.

Recovery selects a bounded page under `FOR UPDATE SKIP LOCKED`, expires claims
using database UTC time, invalidates old task execution generations, preserves
completed results/action receipts and counters, and requeues only work whose
action boundary is proven safe. Retry counts are persisted. Claim or admission
loss cannot produce a notification storm or reset those counts. The embedding
controller binds/reconciles the existing work-admission operation before execution.

Rollback disables `FF_PERSISTENT_AGENTS`, stops/fences workers, retains the
additive schema and journals, and rolls back code only within declared schema
read compatibility. Do not down-migrate by deleting progress or effect history.
Restore from backup only through the existing operator recovery procedure.

Public types, exact method names and transaction semantics are specified in
[`contracts/plane-assignment-api.md`](contracts/plane-assignment-api.md).
