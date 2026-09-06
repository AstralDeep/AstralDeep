# AstralPlane persistent-assignment repository API

Target schema: `079.001`. Proposed module:
`astralplane.repositories.assignments`. This document describes the new contract
to implement, not methods already present in the pinned Plane package.

Expose `create_assignment_repository() -> AssignmentRepository` in `api.py` and
`__init__.py`, add `assignments: AssignmentRepository` to `RepositoryCatalog`, its
`as_mapping()` and `create_repository_catalog()`. Use the live Plane patterns in
`repositories/maintenance.py`, `scheduler.py`, `remote_proposals.py` and shared
repository validators. Keep construction stateless and dependency-free.

Every mutation takes the caller's existing `Transaction`; reads take
`QueryExecutor`. No method borrows a connection, commits independently or performs
network/model/notification work. Deep composes related mutations with existing
work-admission, audit and conversation repositories in the same transaction where
required. Run complete transactions off the event loop through the composition's
bounded `AsyncPlaneRuntime.run_in_transaction` adapter.

## Public immutable records

Use frozen, slotted dataclasses and string enums. Structured fields are detached,
deeply frozen canonical JSON values. Validation bounds are defined in
[`../data-model.md`](../data-model.md); invalid persisted records raise
`RepositoryDataError`, not fallback defaults.

| Type | Fields/meaning |
| --- | --- |
| `AssignmentDefinition` | `name`, `instructions`, `source`, `allowed_tools`, `consented_scopes`, `offline_grant_id`, `limits`, `completion_condition`, `conversation_id`; complete validated owner definition |
| `AssignmentRecord` | Assignment identity/owner/submission identity, definition, instruction revision, control epoch, state version, lifecycle, phase, wake fields, checkpoint, tasks, usage, safe error/timestamps; excludes private claim token |
| `AssignmentControl` | `revise`, `pause`, `resume`, `stop`, `revoke`; `revise` requires a complete replacement definition |
| `AssignmentFence` | `assignment_id`, `owner_id`, `instruction_revision`, `control_epoch`, `claim_generation`, `claim_token`; secret execution capability, never a UI value |
| `AssignmentClaim` | `assignment: AssignmentRecord`, `fence: AssignmentFence`, lease expiry and previous operation binding requiring reconciliation; optional exact `approved_action_id` for a foreground-only claim |
| `AssignmentOperationBinding` | Exact existing `operation_id`, `execution_generation`, `execution_lease_token`; internally verified against work admission by Deep |
| `AssignmentControlResult` | New record, `applied`, invalidated action IDs and begun action IDs; callers can distinguish newly applied vs replayed control |
| `AssignmentSourceEvent` | Event ID/source-item-revision identity, canonical identity/context digests, bounded context, disposition/result/timestamps |
| `AssignmentSourceBatch` | Immutable batch key/digest, source key/configuration digest, expected cursor digest, next cursor and at most 100 events |
| `AssignmentTask` | Bounded task graph entry, including stable plan/task identity, dependencies, authority subset, status/generation, durable result/provenance and incorporation marker |
| `AssignmentTaskClaim` | Assignment fence, task ID/generation, attempt index and task record |
| `AssignmentTaskResult` | `completed`, `failed`, `cancelled` or `reconciliation`, bounded result/digest/provenance, safe error and actual usage references |
| `AssignmentResourceAmount` | Mandatory non-negative integer model calls, tool calls, tokens and elapsed milliseconds; optional spend micro-units/currency, null means unknown monetary usage rather than zero |
| `AssignmentActionIntent` | Stable action key, task/event references, immutable canonical request/digest, sensitivity/interactive-only disposition, target/precondition/permission digests, retry boundary and downstream key, proposed maximum resources, quote digest/expiry, approval expiry |
| `AssignmentActionRecord` | Intent, action/assignment/owner/revision/epoch identities, state, reservation/decision/result metadata; excludes secret dispatch token |
| `AssignmentActionReservation` | Action record, exact attempt/reservation identity, maximum reserved amount and `created` replay indicator |
| `AssignmentDispatchPermit` | Action/attempt identities, random dispatch token, immutable request digest, assigned operation binding; internal capability only |
| `AssignmentActionOutcome` | Outcome (`succeeded`, `failed`, `uncertain`, `failed_not_started`), result/digest, evidence reference, verified actual usage or maximum-charge disposition |
| `AssignmentActionDecision` | Exact proposal digest, approve/decline, immutable decision submission ID/digest, current permission/target precondition digests and approval expiry |
| `AssignmentActionReconciliation` | Exact prior uncertain action/result digest, explicit confirmed-applied/confirmed-not-applied decision, trusted evidence reference and immutable decision ID |
| `AssignmentActivityRecord` | Stable activity key, sequence, state/type, safe title/summary, result/task/action references, created time and notification state |
| `AssignmentEpisodeCompletion` | Expected state version, checkpoint/result digest, accepted task incorporation references, completed/irrelevant event receipts, phase, next wake/reason, safe error and optional activity |
| `AssignmentRecoveryResult` | Bounded reclaimed claim/operation IDs, safe retry counts, uncertain action IDs and assignments requiring intervention; no source text |
| `AssignmentRetentionResult` | Payload redactions, safe identity removals, activity removals, capacity holds and continuation cursor |

All externally supplied replay identities are bounded UUID4 or canonical safe keys;
digests are lowercase SHA-256. Owner IDs follow the existing 512-character bound.
Timestamps are aware UTC. Lease and expiry comparisons use the transaction's
database time, not caller wall-clock claims. Monetary currency is immutable while
nonzero reservations or spending exist.

## Definitions and owner controls

```python
create_assignment(transaction, *, owner_id: str, assignment_id: str,
                  submission_id: str, submission_digest: str,
                  definition: AssignmentDefinition,
                  max_owned_assignments: int = 25,
                  max_retained_assignments: int = 256) -> AssignmentRecord
get_assignment(query, *, owner_id: str,
               assignment_id: str) -> AssignmentRecord | None
list_assignments(query, *, owner_id: str, limit: int = 50,
                 after_id: str | None = None) -> tuple[AssignmentRecord, ...]
apply_control(transaction, *, owner_id: str, assignment_id: str,
              expected_instruction_revision: int, expected_control_epoch: int,
              submission_id: str, submission_digest: str,
              control: AssignmentControl,
              replacement: AssignmentDefinition | None = None
              ) -> AssignmentControlResult
request_check(transaction, *, owner_id: str, assignment_id: str,
              expected_instruction_revision: int, expected_control_epoch: int,
              submission_id: str, submission_digest: str) -> AssignmentRecord
```

Creation replay compares immutable submission semantics. Validate owned
conversation/offline-grant references and serialize owner cap enforcement in the
same transaction: at most 25 active/paused and 256 retained assignments across all
lifecycle states, allowing lower policy bounds only. Initial lifecycle is active,
phase waiting, first wake due now.
The caller has already obtained explicit unattended consent; Plane does not
invent or capture it. Definition activation/revision with a selected currency cap
requires a trusted finite quote coverage declaration for allowed model/tool calls;
missing coverage is refused. Usage-only definitions require no pricing setup.

`apply_control` locks the assignment; missing and wrong-owner lookups are
indistinguishable. Stale expected revision/epoch conflicts. Exact submission
replay returns the retained control receipt without reapplying it. The bounded
control receipt history is retained in activity identities; a pruned old decision
must never be re-applied against a newer epoch. Revision increments the
instruction revision; every successful control increments the epoch and revokes
the old claim/approval authority. It invalidates unstarted old work/proposals,
retains completed receipts, and returns known begun actions honestly. Stop is
terminal and idempotent; resume is valid only from paused. Revoke clears the grant
reference and waits for authorization without granting a new root. Grant-wide
revocation remains the existing offline-grant path.

`request_check` is the UI run-now seam. It requires active lifecycle and current
revision/epoch, records an immutable owner submission receipt, and coalesces one
wake without changing the epoch or resetting counters. It cannot run earlier than
the existing cadence/rate guard permits, bypass backoff or capacity, or create an
extra parallel episode. Exact replay returns the existing wake receipt. A paused,
stopped or completed assignment is refused; request-check is never resume.

## Bounded episode scheduling and fencing

```python
claim_due_for_administration(transaction, *, worker_id: str,
                             limit: int = 20, lease_seconds: int = 30
                             ) -> tuple[AssignmentClaim, ...]
claim_for_approved_action(transaction, *, owner_id: str, assignment_id: str,
                          action_id: str, expected_request_digest: str,
                          expected_instruction_revision: int,
                          expected_control_epoch: int,
                          interactive_receipt_id: str,
                          submission_id: str, submission_digest: str,
                          worker_id: str, lease_seconds: int = 30
                          ) -> AssignmentClaim
bind_operation(transaction, *, fence: AssignmentFence,
               binding: AssignmentOperationBinding) -> AssignmentRecord
renew_claim(transaction, *, fence: AssignmentFence,
            lease_seconds: int = 30) -> AssignmentClaim
assert_current_claim(query, *, fence: AssignmentFence) -> AssignmentRecord
recover_expired_for_administration(transaction, *, limit: int = 100
                                   ) -> AssignmentRecoveryResult
finish_episode(transaction, *, fence: AssignmentFence,
               completion: AssignmentEpisodeCompletion) -> AssignmentRecord
```

Due selection and recovery use bounded row locks with `SKIP LOCKED`; no active
lease may be stolen. Lease bounds are 5-60 seconds; selection hard limit is 100.
Claims require active lifecycle, eligible phase/time and available retry budget.
An episode without the exact admitted work operation cannot begin actions.
`bind_operation` is idempotent for the identical binding; a different generation
conflicts until the prior execution is explicitly reconciled. Deep verifies both
the assignment fence and `WorkAdmissionCoordinator.assert_current_execution`
before action execution and publication.

`claim_for_approved_action` obtains a new foreground claim while an active
assignment waits for approval without a background lease. It locks assignment
then action; requires an unexpired approved exact action, matching owner/revision/
epoch and trusted current attended interaction, refuses an existing live claim,
and binds the claim to this action only. Its single-use admission submission
cannot resurrect an expired claim: replay can inspect the existing result but
cannot execute again. It does not revive prior workers or admit unrelated tasks.
Bind a freshly admitted interactive work-admission operation and run the full
ordinary dispatch gate stack before `start_action`; the receipt is not a tool
permission bypass. Ending this foreground action releases the claim and may
coalesce a continuation for the current assignment.

`finish_episode` atomically checks current claim/version, incorporates the named
child results once, updates checkpoint/event dispositions, appends an optional
stable activity and releases the claim. A durable event/checkpoint/activity is
published in that transaction. The caller cannot declare an event complete while
referenced work is unfinished/uncertain. Next wake cannot override paused/stopped
controls. Completion replay uses the immutable completion digest retained in the
activity or episode receipt; a same-fence stale write with different content
conflicts. If a child/action wake arrived during the episode, its greater wake
generation survives and cannot be overwritten by a later cadence wake.

Expired claims invalidate every old task generation, preserve completed results
and counters, and return work-admission operation bindings for coordinated
recovery. Known begun unreplayable effects become reconciliation holds. Safe
read/downstream-key retry candidates retain their action key and charged attempts;
recovery is not authority to invoke them until Deep verifies the adapter and grant.

## Source ingestion and bounded task graph

```python
record_source_batch(transaction, *, fence: AssignmentFence,
                    expected_state_version: int,
                    batch: AssignmentSourceBatch
                    ) -> tuple[AssignmentRecord, tuple[AssignmentSourceEvent, ...]]
list_events(query, *, owner_id: str, assignment_id: str,
            disposition: str | None = None, limit: int = 100,
            after_id: str | None = None) -> tuple[AssignmentSourceEvent, ...]
put_task_plan(transaction, *, fence: AssignmentFence,
              expected_state_version: int, plan_key: str, plan_digest: str,
              tasks: tuple[AssignmentTask, ...]) -> AssignmentRecord
claim_task(transaction, *, fence: AssignmentFence, task_id: str,
           expected_task_generation: int) -> AssignmentTaskClaim
complete_task(transaction, *, claim: AssignmentTaskClaim,
              result: AssignmentTaskResult) -> AssignmentRecord
```

Batch replay compares identity, context and cursor digests; cursor advancement
cannot skip an event that failed insertion. Capacity denial rolls back the whole
batch. Source configuration/revision must match the definition. Plane validates
the records, while Deep determines relevance and safe source extraction.

Task plan acceptance is immutable and bounded; no cycles, absent dependencies,
duplicate IDs or enlarged child tool sets. `claim_task` starts only a dependency-
ready task under an existing assignment lease and the concurrency/retry/depth
ceilings. Its attempt counter is durable. Model/tool actions remain independently
reserved; claiming a task is not permission to spend. Completed task replay with
the identical result digest is idempotent; a different digest conflicts. Every
successful child completion sets `pending_wake` and advances `wake_generation`
without allocating an unbounded queue. `finish_episode` handles durable child
incorporation and checkpoint update; existing completed entries are immutable.

## Metered actions and immutable approvals

```python
put_action(transaction, *, fence: AssignmentFence,
           intent: AssignmentActionIntent) -> AssignmentActionRecord
get_action(query, *, owner_id: str, assignment_id: str,
           action_id: str) -> AssignmentActionRecord | None
list_actions(query, *, owner_id: str, assignment_id: str,
             states: tuple[str, ...] = (), limit: int = 100,
             after_id: str | None = None) -> tuple[AssignmentActionRecord, ...]
decide_action(transaction, *, owner_id: str, assignment_id: str,
               action_id: str, expected_instruction_revision: int,
               expected_control_epoch: int, decision: AssignmentActionDecision
               ) -> AssignmentActionRecord
reserve_action(transaction, *, fence: AssignmentFence, action_id: str,
                attempt_id: str, expected_request_digest: str,
                maximum: AssignmentResourceAmount,
                quote_digest: str | None = None,
                quote_expires_at: datetime | None = None
                ) -> AssignmentActionReservation
start_action(transaction, *, fence: AssignmentFence, action_id: str,
              attempt_id: str, expected_request_digest: str,
              current_permission_digest: str, current_precondition_digest: str,
              binding: AssignmentOperationBinding,
              interactive_receipt_id: str | None = None
              ) -> AssignmentDispatchPermit
record_action_outcome(transaction, *, owner_id: str, assignment_id: str,
                       action_id: str, attempt_id: str, dispatch_token: str,
                       expected_request_digest: str,
                       outcome: AssignmentActionOutcome
                       ) -> AssignmentActionRecord
release_unstarted_action(transaction, *, owner_id: str, assignment_id: str,
                          action_id: str, attempt_id: str,
                          expected_request_digest: str,
                          reason_code: str) -> AssignmentActionRecord
reconcile_action(transaction, *, owner_id: str, assignment_id: str,
                   action_id: str, expected_instruction_revision: int,
                   expected_control_epoch: int,
                   decision: AssignmentActionReconciliation
                   ) -> AssignmentActionRecord
```

The stable action identity is independent of a worker claim generation. A new
claim loads it; it cannot replace an uncertain/completed action with a fresh ID.
`put_action` returns `proposed` when approval is required, otherwise `ready`, a
non-started action eligible for reservation. It records a deduplicated approval activity when
necessary. New attempts under an existing logical action are bounded, retain
prior immutable attempt outcomes and consume additional resources. The bounded
attempt history lives inside the action row (maximum matching retry hard cap);
it is never overwritten to pretend that an earlier request was not sent.

`decide_action` requires a still-pending, matching, unexpired, same-epoch proposal.
It records the authenticated owner's decision once. Approval does not extend the
immutable expiry or substitute another tool/target/argument set.
`reserve_action` atomically reserves against every current limit, and binds the
exact request. Refusal leaves no partial debit. Finite model/tool/token/time
reservations are always mandatory. With an owner-selected currency cap, an expired
or missing trusted quote cannot authorize activation, revision or `start_action`;
a trusted explicit zero-cost declaration still has an identity/digest. With no
currency cap, quote/spend/currency fields may be null and monetary usage remains
explicitly unknown; this permits the baseline usage-capped public monitor without
new operator price configuration. Null must never be converted into zero/free.

`start_action` locks the parent assignment before the action. It checks active
lifecycle/current claim/epoch/revision, operation binding, exact approved
parameters/current permission/current precondition digests, valid quote when a
currency cap is selected, and finite usage reservation. An action-bound foreground
claim can authorize only its bound action. It consumes approval at most once and issues one exact
dispatch permit. Interactive-only intents require a trusted attended receipt;
Deep obtains it through existing remote confirmation and gate-stack execution,
never from model arguments or a machine socket. No method sets tool permissions.
The committed permit is the durable action-start boundary: row-lock ordering
prevents permits after a control's epoch change. Previously permitted work may
finish and is reported as in flight; no distributed network-byte barrier or recall
of an already-authorized request is promised. An invalid old claim cannot acquire
a later permit, start a new continuation or publish stale work.

`record_action_outcome` is intentionally permitted for the exact previously issued
dispatch token after a control or lease invalidation. Its only authority is to
retain the real outcome and settle the already-reserved resources; it cannot
change instructions, publish stale content, claim a task, consume another approval
or initiate work. An identical outcome is replay; changed result bytes conflict.
It may coalesce a wake only while the current assignment is active, leaving
paused/stopped assignments inert. All stale task/result publication remains
fenced. `release_unstarted_action` proves from persisted state that no permit was
issued before releasing resources; it cannot erase uncertain spend.

`reconcile_action` records an explicit owner decision plus trusted evidence. A
confirmed-applied outcome preserves its completed receipt; confirmed-not-applied
permits later reconsideration under current authority and a new bounded attempt,
never automatic broad re-execution. A model cannot self-attest that an uncertain
external action failed.

## Activity, notification and retention

```python
append_activity(transaction, *, fence: AssignmentFence,
                 activity: AssignmentActivityRecord) -> AssignmentActivityRecord
list_activity(query, *, owner_id: str, assignment_id: str,
               after_sequence: int = 0, limit: int = 100
               ) -> tuple[AssignmentActivityRecord, ...]
mark_activity_notified(transaction, *, owner_id: str, assignment_id: str,
                        activity_id: str,
                        expected_state: str = "pending") -> bool
retain_for_administration(transaction, *, limit: int = 100
                           ) -> AssignmentRetentionResult
delete_for_owner(transaction, *, owner_id: str, assignment_id: str,
                   expected_control_epoch: int) -> bool
```

Activity replay checks immutable key/digest semantics. Appending worker activity
requires its current claim; owner controls and late-action observations append
their restricted activity internally, without pretending to hold a worker lease.
Finding/checkpoint completion uses the atomic `finish_episode` seam rather than
a separate publish call. In-app notifications refer to a durable activity ID;
clients can deduplicate that ID, while reconnect always recovers the activity.
The notification CAS prevents workers claiming repeated delivery as distinct
results. It is not a guarantee that a disconnected socket received a frame.

Retention has bounded locked pages, removes no live claims/pending approvals or
unresolved effects, and preserves dedupe tombstones until a validated source
replay floor or authorized terminal deletion allows removal. At capacity, the
assignment fails closed with an actionable retained status. `delete_for_owner`
requires a terminal stopped **or completed** assignment with the exact epoch, no
executable claim and no unresolved started/uncertain effects; existing audit
retention remains independent. Account retirement invokes
the same stop/fence/purge policy and cannot directly delete active rows.

## Failure vocabulary and verification

Reuse `RepositoryValidationError`, `RepositoryConflictError`,
`RepositoryNotFoundError` and `RepositoryDataError`. Attach bounded stable codes:
`assignment_not_found`, `assignment_revision_conflict`, `assignment_not_active`,
`assignment_claim_stale`, `assignment_idempotency_conflict`,
`assignment_operation_conflict`, `assignment_budget_exhausted`,
`assignment_history_capacity_exhausted`, `assignment_approval_invalid`,
`assignment_precondition_changed`, `assignment_action_uncertain`,
`assignment_task_dependency_invalid` and `assignment_result_conflict`. Deep maps
these to safe UI messages; exceptions never echo source data or tokens.

Plane tests must exercise owner mismatch, replay conflicts, malformed/corrupt
persisted JSON, graph bounds/cycles, two-worker row claiming, stale epochs and
leases, stop versus start permit, completion replay, retained child results,
reservation races/rollover/uncertain charges, immutable approval/expiry/consumption,
late outcome observation without stale publication, source cursor batch rollback,
hard retention caps, repeat migration, and representative predecessor data.
Tests cover both stopped/completed deletion, account retirement, total-retained
quota refusal after create/stop cycles, coalesced request-check replay/cadence,
foreground approved-action claim versus expired/replayed admission, usage-only
reservations with explicitly unknown money, and fail-closed selected currency
caps with missing/expired quotes.
Critical concurrency/migration cases run on real PostgreSQL. Mock repositories
are explicitly test-only; no in-memory production fallback is permitted.
