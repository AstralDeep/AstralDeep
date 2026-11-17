# Persistent assignment contracts (`079.001`)

`create_repository_catalog().assignments` and `create_assignment_repository()` expose
`AssignmentRepository`. Immutable public dataclasses live in
`astralplane.repositories.assignment_models` and are also imported by `assignments`.
Every operation uses the caller-owned Plane transaction; Deep supplies policy, authorization,
offline credentials, trusted resource quotes, ordinary dispatcher and work-admission gates.
No repository method performs external I/O or grants permission to a model.

## Additive one-shot admission (`088.001`)

`create_operation` shares the assignment initializer, action ledger and execution fences.
It creates only the `one_shot` profile; `create_assignment` remains `persistent` with
its original source/tool/offline-grant requirements. Existing row JSON and initial
definition digests are preserved by the additive migration. Legacy list and due-claim
methods explicitly select the persistent profile. The former untyped
`claim_operations_for_administration` entry point now refuses; a one-shot host uses
read-only discovery followed by an exact observation-bound claim as described below.

New `AssignmentOperationSpec` records use outer version 2 and require the matching
issued-session storage contract (`088.002`). This assignment change allocates no new
schema or migration. Versions 1 and 2 remain decodable with understood control and
checkpoint version 1. Only v2 interactive `session_incarnation` currently has a
qualified execution observation. Version 1 permits safe inspection, cancellation,
receipt replay and authentic liability settlement, but never new continuation.

The host supplies a typed `AssignmentOperationSpec` and `AssignmentOperationAuthority`.
The latter is only an opaque, owner-bound reference to an issued session incarnation,
delegation, framework credential or offline grant. It contains no token, role claims or permission
decision. Deep authenticates the caller and revalidates current lineage, tool/PHI/egress
permissions and the separate work-admission fence before dispatch. Plane checks closed
reference kinds, owner equality, owner retirement and database time after the owner lock.
Scheduled admission additionally requires the same current owner offline-grant reference;
that stored reference alone does not enable execution, resume or claims.

New interactive admission also requires `authority=SessionExecutionObservation(...)`.
The operation stores `reference_kind="session_incarnation"` and the original canonical
UUID4 as `reference_id`, never the observation or credential bytes. Plane validates the
exact owner/incarnation and current encrypted generation under owner then session locks.
The immutable authority expiry cannot exceed that session's hard expiry. Both the
original observation and operation time bounds are checked again after assignment and
receipt writes. A savepoint rolls back those writes on late refusal even when the
caller catches the error and commits unrelated work. The host must include its required
allowance/audit writes and final authority checks in the enclosing transaction.

One-shot definitions allow no source or external tools for ordinary model work, but
research requires a nonempty source plan. They declare no recurrence, have at most three
retries and a deadline no more than one day after acceptance. Lifetime resource ceilings
remain authoritative; an omitted daily ceiling uses the lifetime ceiling. Limits and
reference/profile fields are closed, and working authority is never reconstructed from
an API body's claims. The legacy cadence-based `request_check` refuses this profile.

Admission receives the original owner/namespace/caller key and canonical command digest.
An existing matching receipt is resolved before new definition/guidance expansion or
new observation checks; framework replay also binds the issuing credential reference.
An accepted v1 receipt returns its original version and binding without adopting a
current session or spending again. Genuinely new v1 admission is refused. Receipt, new assignment,
and the caller's audit/allowance changes belong in one caller-owned transaction. Separate
one-shot capacity defaults to 25 active/paused and 256 retained tasks. At most 4096
original-key receipts per owner are retained; callers may select lower ceilings.
Reaching a ceiling refuses new admission without evicting replay or unresolved effects.

Terminal task deletion nulls the receipt's live foreign key but retains its original
logical identity: the same key returns `assignment_operation_deleted` and cannot start
another effect. A completed account retirement removes the private receipts only after
unresolved effects are settled; the owner retirement fence still prevents new admission.
This storage admission contract does not itself install a host execution handler, add
new effect authority or replace the existing ordinary chat dispatcher.

### One-shot wait, wake and controller projection

`set_event_wait` is an episode completion, not an unfenced owner-ID write. It takes
the current `AssignmentFence`, observed `state_version`, checkpoint and completion
digest plus a bounded event key and strict integer source observation revision.
The source revision is a monotonic watermark, never an ordered opaque revision string.
The existing completion transaction rejects in-flight or unresolved effects, releases
unused reservations, saves the checkpoint and clears the claim. The phase becomes
`awaiting_event` with no due time; neither worker profile treats it as cadence work.

`accept_wake` takes the owner/assignment, observed state/instruction/control vector,
original event ID, event key, source revision and host-computed event-content binding.
The host still authenticates the caller and validates current authority and the source;
these strings and digests are not permission. Owner retirement locks precede the
assignment lock. First acceptance requires the current active wait, matching key and
a strictly greater watermark, then advances the existing wake generation once and
sets database-time eligibility. Wrong-owner and missing assignment both return absent.

`operation.control` version 1 stores the current wait, at most 64 source-key watermarks
and at most 128 wake receipts keyed by the original event ID (128 UTF-8 bytes each).
A receipt binds key, source revision and content binding. Exact replay acknowledges
the prior acceptance before stale version/phase/expiry checks, without another state
change; mismatched event-ID reuse conflicts. Receipts are never evicted during the
retained operation lifetime. Capacity refuses new events while retaining inspection
and cancellation. The control envelope is at most 64 KiB and the complete assignment
remains at most 256 KiB. This uses existing private JSON; no migration bytes change.

### Durable completion subscriptions and wake receipts (089.004)

`completion_subscription` is a neutral bounded waiter-to-source binding: an owner, a waiter
operation, a source operation, a terminal condition (`completed`, `failed`, `cancelled`, or
`any_terminal`), and a source-revision fence. `wake_receipt` records exactly one acceptance per
`(subscription_id, idempotency_key)` with the observed terminal and revision; a replay references
the original receipt and never creates a new delivery. Receipts are accepted only for live
(non-revoked) subscriptions whose condition covers the observed terminal and whose fence admits
the observed revision. Acceptance locks the owner-scoped subscription until the caller's
transaction ends, so concurrent revocation or deletion serializes with receipt creation.
Registration-versus-completion races resolve to one row, restarts keep
receipts, and deletion cascades. A subscription never schedules work, never encodes IAM policy,
and never substitutes for original session authority; nothing is written into wait-state JSON.

One-shot `apply_control` requires strict `expected_state_version` in addition to the
instruction/control vector. Persistent callers and their receipt signatures retain
their existing contract. Replays of foundation one-shot receipts also remain valid.
Pause/resume preserves event waits, approval/reconciliation/authority holds and an
already scheduled retry. Source-less interactive resume validates its operation
authority and references without inventing an offline grant. Revocation cannot be
undone by pausing and resuming. Cancellation preserves started/uncertain liabilities.

Owner-directed waiting does not borrow a worker's `set_event_wait` completion
fence. Use `prepare_owner_event_wait` followed by `set_owner_event_wait` with the
same arguments in one bounded caller transaction: owner/assignment, observed
instruction/control/state vector, canonical UUID4 `submission_id`, command digest,
`event_key` (128-byte bound), nonnegative `source_revision`, and `control_version=1`.
Preparation locks owner then assignment then all action rows in stable ID order,
writes nothing, and returns `AssignmentOwnerWaitPreparation(assignment, replayed,
invalidated_action_ids, begun_action_ids)`. The host authenticates the current
owner and appends its required same-transaction audit only for a new receipt.
Final mutation independently repeats validation and inventory after any waits.

A new wait supports known interactive-v2 one-shot records that are active or
paused. It refuses terminal/unsupported records, stale counters and old source
watermarks. Original execution-session expiry or retirement does not prevent a
safe hold. Waiting stores the existing event-wait structure, advances the control
epoch, clears the claim and fences pending/running child generations. It preserves
the checkpoint, original authority/deadline, spent usage and issued/unknown
liabilities. Only unstarted work is invalidated and its unused reservations
released. Existing reconciliation/unknown holds remain in reconciliation; otherwise
the phase becomes awaiting-event. Paused lifecycle remains paused. Neither path
schedules work or grants resume/wake authority. Later continuation still requires
the qualified host's current original-session authority and all existing holds.

The final method returns the existing `AssignmentControlResult`. Its `wait`
receipt uses the existing control namespace and an exact command/counter/event
signature; conflicting reuse refuses, and exact replay returns without another
mutation or audit even after later cancellation. A new wait refuses at 256 control
receipts rather than evicting history. Other existing safe controls retain their
bounded-history policy, so a receipt may eventually be evicted by those controls;
the original epoch then prevents reapplication. No schema or stored version changes.
The final savepoint rolls back all wait/action/activity writes if a caller catches
a failure. Required audit before that savepoint remains provisional: the host must
abort the enclosing transaction on final failure, and only a committed result may
be delivered. The preparation is not a capability outside that transaction.

One-shot `finish_episode` requires explicit due time for a nonterminal queued yield;
it never reads persistent cadence. Transient failures use 5/15/45-second bounded
retries inside the original deadline/authority lifetime and configured retry ceiling.
Exhaustion becomes an explicit terminal failure only when unresolved work permits it.
Explicit terminal completion may carry `terminal_outcome` (`completed` or `failed`)
and a bounded canonical result reference. The host remains responsible for result
ownership and approved publication. Cancellation records `cancelled`. New optional
completion fields are omitted at default when computing old receipt signatures.

`get_operation` and bounded UUID-keyset `list_operations` return an
`AssignmentOperationRead` with the existing assignment revision vector, disposition,
continuation support and explicit terminal/result metadata. They filter the profile
before pagination. Known v1 records and unsupported v2 origins report no continuation
support while preserving their understood historical terminal/result metadata.
Unknown positive operation/control/checkpoint versions remain
readable but non-dispatchable; claim selection excludes them before applying its
limit, so they cannot starve later supported rows. A safe stop preserves unknown
nested metadata and retires the known outer fence. Invalid identities, malformed
versions or inconsistent indexed fields remain data errors, not compatibility cases.

`get_operation(..., lock=True)` takes the owner guard before locking the owner-scoped
operation row until the caller's transaction ends, matching the owner-wait lock order.
Callers own that transaction and must establish a consistent order for any credential,
authorization, or other domain locks before using this option. The default remains a
nonlocking read; `lock` accepts only a boolean. Inspection remains available for retired
owners; the getter does not apply an admission decision or qualify a host integration.

These are repository/controller contracts. The Deep operation service, external
owner-command adapters and current-authority stream delivery still require their
separate integration work; they are not installed
by adding these methods.

## Owner commands and receipts

Create takes an owner, UUID4 assignment/submission IDs, a semantic submission SHA-256 and an
`AssignmentDefinition`. Definition replacement requires both `instruction_revision` and
`control_epoch`; every accepted pause/resume/revise/stop/revoke changes the control epoch.
Worker writes use `state_version` independently. `get_submission_receipt` accepts the exact
command names `create`, `revise`, `pause`, `resume`, `stop`, `revoke`, and `run-now`; it resolves
accepted client semantics before the caller recaptures server-owned grants. Conflicting reuse
fails. A bounded recent receipt history cannot reapply an evicted command under a stale epoch.

`request_check` coalesces owner intent within cadence and durable retry deadlines. It does not
override lifecycle, missing authorization, approval, reconciliation, or resource limits. At most
25 active/paused and 256 total retained assignments belong to an owner. Capacity never prevents
pause/stop/revocation. Terminal stopped or completed deletion requires the exact control epoch
and no unresolved effect. Account `retire_owner` returns stopped/deleted assignment IDs and any
unresolved action IDs; commit the stop before reporting pending reconciliation and defer purge.

For one-shot `decide_action`, `reconcile_action`, and `delete_for_owner`, pass the
observed strict integer `expected_state_version`. The persistent signatures remain
compatible. An exact prior action decision/reconciliation acknowledges its receipt
without applying a second transition; a genuinely new command must match the current
state version and instruction/control vector. Reconciliation can settle an issued
liability after stop or expiry, but cannot schedule new work under expired, revoked,
or retired authority. Once the immutable task deadline expires, a fully settled
reconciliation becomes terminal failed; any remaining liability/task reconciliation
keeps the operation held. Renewable authority loss with a live deadline remains an
authority hold. No result is incorporated or published by settlement.

For one-shot reconciliation, pass the optional original
`authority=SessionExecutionObservation(...)` only when the host has freshly qualified
it. Omission, wrong incarnation, deletion, rotation, expiry or revocation never
turns a genuine uncertain liability into a refund or an authorization exception:
the exact decision settles once with no result payload, but cannot schedule a wake.
Continuation additionally requires no remaining reservations, issued/uncertain
attempts, pending approvals, reconciliation tasks, event wait or budget hold.
Successful one-shot continuation clears obsolete retry/error metadata. Persistent
reconciliation retains its existing continuation behavior. No stored
operation, control, action or schema version changes.

When the host requires atomic audit, use this sequence in one bounded transaction:

1. `prepare_action_reconciliation` with the same owner, assignment/action IDs,
   observed state/instruction/control vector, exact typed
   `AssignmentActionReconciliation`, and optional original observation. It acquires
   owner/original-session locks before the assignment and all one-shot action rows
   in sorted ID order. It performs no writes and returns
   `AssignmentActionReconciliationPreparation(assignment, action, replayed)`.
2. Append the required audit through the caller's ordinary same-transaction audit
   facade only for a new decision. Remote evidence/authentication must already have
   completed; no network or second transaction belongs inside this sequence.
3. Call `reconcile_action` with the identical arguments. It independently validates
   the decision and resamples database time after audit and action-inventory waits.
   Expired execution authority suppresses only continuation; the factual charge and
   audit commit together. A replay never creates a new wake or charge.

Preparation is neither a dispatch permit nor a durable capability, and cannot be
used after its transaction. The host remains responsible for current owner command
permission and for committing the entire audit/settlement sequence together.
Infrastructure failures roll back; the exact receipt resolves a lost commit
acknowledgment. Final settlement uses a savepoint so a caught storage failure cannot
leave a partial action/accounting update. The preceding required audit is outside
that internal savepoint: if final settlement raises, the host must abort the whole
transaction rather than catch the error and commit an audit-only success. SQL timeout/cancellation is not proof that
settlement committed and is not a reason to refund the original issued liability.


One-shot and mixed-profile account cleanup must explicitly adopt
`retire_operations_for_owner`. It fences the owner and stops every owned profile in
one transaction, returning `retained_assignment_ids` as well as actual
`unresolved_action_ids`. Commit that result and defer physical account/blob purge
while **either** collection is nonempty. Outstanding usage or reconciliation tasks
can retain an assignment even without an action ID. The legacy `retire_owner`
refuses one-shot profiles and orphan holds, so older hosts that inspect only action
IDs cannot silently purge them. Deep must update its cleanup adapter when adopting
this Plane revision; the repository change alone is not that integration.

Unknown operation/control/checkpoint versions allow safe stop receipts and owner
inspection. Stop changes only understood outer fences and preserves unknown nested
bytes. Cancellation and deletion conservatively decode the current action envelope;
future/malformed actions remain unresolved even if their indexed state says they
settled. Their attempt, reservation, binding and proposal metadata is never followed
or rewritten. Physical deletion requires every action to be understood and all
issued/reserved/uncertain liabilities, outstanding usage and reconciliation tasks
to be settled. Supported neighboring assignments can still retire without waiting
for those unknown records.

## Claims, durable memory and bounded tasks

`claim_due_for_administration` uses PostgreSQL time and `FOR UPDATE SKIP LOCKED`. Claims carry
owner, instruction revision, control epoch, generation and an opaque token. Bind the ordinary
`AssignmentOperationBinding` before any dispatch; validate its separate work-admission fence at
each use. `renew_claim` and `assert_current_claim` never revive invalidated authority.

`discover_due_operations_for_administration(query, limit=20, after_due_at=None,
after_id=None)` returns a bounded read-only page of due v2 interactive incarnation
records. Both cursor members are required together; order is `(next_wake_at,id)`.
The query filters execution profile, phase, known outer/nested versions and supported
origin before `LIMIT`, and acquires no lease. A host advances after a refused candidate
and wraps boundedly to avoid repeatedly selecting an unavailable first record.

`claim_operation_for_administration(transaction, owner_id=..., assignment_id=...,
expected_state_version=..., worker_id=..., authority=..., lease_seconds=30)` grants
one exact due claim. It locks owner, the original selected session, then assignment;
validates the observed strict state version, due time, phase and absence of a lease;
and rechecks the same observation after the write inside a savepoint. Foreign,
replaced, expired, unsupported or competing claims refuse without partial changes.
The host obtains fresh remote authority before opening this transaction. Discovery is
not an authority grant and cannot select the owner's latest session as a substitute.
One-shot `claim_for_approved_action` likewise requires `authority` and observed
`expected_state_version`, and rechecks after action waits and the restricted-claim
write. Persistent approved-action signatures and behavior remain compatible.

Before a host transaction performs a write requiring both execution fences, call
`assert_current_assignment_execution(transaction, fence=AssignmentFence(...),
binding=AssignmentOperationBinding(...), action_id=None, authority=None)`. This strict
public guard locks the owner retirement domain, the exact selected interactive
session when applicable, the selected owner-scoped offline grant when present,
the logical assignment, then the work-admission execution. It discovers the
authority references without an assignment row lock and refuses any changed
selection when it reloads the assignment under lock. Session validation binds
opaque encrypted credential state without decrypting it. Session deletion and
rotation contend on that session row; grant revocation contends on its grant row. After the
admission lock wait it rechecks the current assignment lease, revision/control
vector, local authority/deadline, grant expiry/revocation and admission ownership.
An approved-action claim requires its exact `action_id`; omission cannot widen it.

The returned detached `AssignmentRecord` does not renew either lease or authorize
a later transaction. Perform the subsequent bounded repository mutation in this
same transaction and still satisfy its action/state/request checks. No network,
provider callback or second pool belongs inside the transaction. The host must
refresh institutional session/delegation authority before opening it and perform
any required audit/outbox writes before commit. A missing, stale or unsupported
local context refuses the guarded write. Interactive one-shot requires an exact
`SessionExecutionObservation` in `authority`, bound to the operation's stored
incarnation reference. Delegation, scheduled one-shot and framework references have
no qualified execution observation adapter here and are refused, even if a grant
row or credential identifier exists. Persistent granted profiles retain their
existing checks and signatures. Version 1 is settlement-only; a live historical SID
cannot re-enable it. Reference metadata is insufficient without the required adapter. These storage primitives do not enable
source-less chat, interactive ingress or a one-shot runner.

Use the named `put_action_for_execution`, `reserve_action_for_execution` and
`start_action_for_execution` wrappers for guarded action transitions. They take
the existing action arguments plus the exact `binding` and optional `authority`,
and check current authority/both fences before and after action/resource lock
waits. Each uses the caller transaction's existing savepoint: an exception rolls
back that wrapper's prepare, reservation, permit and approval-consumption writes,
even if the caller catches it and commits independent work. Failure to restore a
savepoint marks the enclosing transaction failed under the existing transaction
contract. Do not catch an authority failure and dispatch. Returned records and
permits are usable only after the enclosing transaction commits successfully;
the caller remains responsible for same-transaction required audit/outbox writes.
Raw `put_action`, `reserve_action` and `start_action` signatures remain compatible
for established callers; they do not replace these stronger execution checks.

The guard shares admission-fence validation with result settlement without changing
the latter's contract: authentic old permits can still settle usage when execution
authority has ended. Do not put that settlement-only path behind a strict current
execution guard. Existing `assert_current_claim` and persistent signatures remain
compatible. Deep must adopt this guard through its bounded transaction adapter
before enabling one-shot runner dispatch; this Plane addition does not wire or
enable those callers, and introduces no migration.

Source batches atomically insert stable provider/item/revision identities and advance the
checkpoint cursor under compare-and-set. Reserved checkpoint keys are `cursor`,
`source_configuration_digest`, and `last_batch_key`. Source context is bounded and untrusted.
Recent batch receipts are bounded to 32; older source identities remain in the relational ledger.
Task plans enforce depth, fanout, dependencies, child tool attenuation and shared budgets.
Completion persists the exact task generation/result/provenance; incorporation references must
match those retained bytes. An episode cannot finish with a started action or complete an event
whose direct effect is unresolved. Unstarted reservations release when the episode yields.

Revision supersedes old unfinished events and task graphs, clears current source/finding
checkpoint fields, and archives each old task's result, digest and provenance in inspectable
`task_superseded` activity before permitting a replacement graph. These archives have references
and are not pruned as transient activity. Bounded event/action/plan history refuses new work at
capacity, requiring owner retirement or revision as applicable; it never discards replay identities.

## Effects, approval and resource admission

`put_action` records an immutable request and complete intent digest under an assignment-scoped
action key. `get_action_by_key` retrieves it without an unbounded scan. `reserve_action` durably
reserves the exact finite call/token/time maximum; parallel child work shares both lifetime and
daily totals. Currency is optional. With no currency cap, spending remains explicitly unknown;
selected caps require trusted finite unexpired quotes, including an explicit zero-cost quote for
zero caps. Actual usage overrun is recorded honestly and prevents further admission.

`start_action` commits the single-use dispatch permit, with request, permission, precondition,
operation and current epoch checks. That commit is the durable action-start boundary. A later
control cannot recall the external request; it fences every later permit and stale publication.
`record_action_outcome` accepts an exact previously issued dispatch token even after stop or
lease loss, updating only the ledger/accounting; it cannot resurrect continuation authority.
`AssignmentActionRecord.ever_started` reports whether any durable attempt received a permit
without exposing dispatch tokens. A safe replacement after control invalidation requires an
invalidated action that has never started; succeeded action identities remain reusable receipts.

Sensitive intent approval binds the complete reviewed request and immutable expiry. Interactive
only tools require a fresh `claim_for_approved_action` admission, exact action binding and the
existing attended confirmation flow. `link_interactive_proposal` verifies the actual durable remote
proposal owner, tool, agent and argument fingerprint in the same transaction that creates it.
`get_action_for_interactive_proposal` resolves the inverse association; `observe_interactive_proposal`
only reflects an actual remote decline/expiry. Control and retirement expire unstarted linked
pending/approved remote proposals before removing associations. A consumed or begun approval is
never replayed under a new worker.

Owner reconciliation of an uncertain effect records `reconciled_applied` or
`reconciled_not_applied` with the evidence reference and prior result digest. It does not recover
the external response: the public result has `result_available: false` and an empty `result`.
Even when the effect ledger is classified as succeeded, a consumer requiring that response must
hold for reconciliation until usable evidence or revised owner instructions permit continuation.
Never substitute an empty result for a completed investigation or manufacture a finding.

### Transient model input and unavailable results

`AssignmentActionIntent.transient_input=None` preserves the existing durable request
contract and canonical receipt bytes. One-shot ordinary model actions may instead
provide `AssignmentTransientInput` version 1 with `binding_key_id`, `payload_binding`,
`source_retention`, `references`, and `reconstruction_kind="model_messages"`.
`payload_binding` is a host-produced keyed attestation over the exact transient model
request; `request_digest` must equal that attestation. Plane receives neither its key
nor private messages. Never substitute an unkeyed digest of private text. The bounded
key ID is diagnostic metadata, not a key or authority token.

In this mode `request` contains only `kind="model"`, required finite
`max_output_tokens`, and optional bounded model/provider identifiers, a closed
`reasoning_effort`, or `response_format` of `{"type":"text"}` or
`{"type":"json_object"}`. Arbitrary messages, arguments, schema text and custom
fields are refused. The operation's retention policy must match the disposition;
consequential or interactive approval requests retain the existing durable reviewed
intent contract. A reconstruction reference contains only `kind` (`source`, `note`,
or `skill`), `resource_id`, and positive `revision`. Note IDs are canonical UUID4;
other IDs are bounded identifiers. References are unique by kind/ID and bounded to
32 sources, 8 notes and 20 skills, with an 8 KiB serialized ceiling. No reference
contains expanded guidance or source text.

`AssignmentActionOutcome.result_disposition` is optional for durable legacy calls.
Version 1 carries `available`, optional `reason`, `references`, and `binding_key_id`.
Unavailable results require an empty `result` and one of `retention_discarded`,
`stale_execution`, or `reconstruction_required`; available results carry neither a
reacquisition reason nor references. Transient model outcomes require a diagnostic
key ID and a host-produced keyed `result_digest`, so retained receipts cannot expose
an unkeyed hash of private output. Plane validates the envelope, not the secret-key
attestation itself. The public result projects `result_available` and, when false,
`reacquisition_reason` beside the typed disposition.
For transient outcomes, non-null `evidence_reference` is an opaque identifier of at
most 256 UTF-8 bytes, starting with an ASCII letter/digit and using only letters,
digits, `.`, `_`, `:`, `/`, `@`, or `-`. It must identify retained evidence, never
contain private excerpts or prose. Both writes and known-envelope decoding enforce
this bound; legacy durable evidence references keep their existing contract.

A successful read whose source bytes were discarded remains `succeeded` and charged.
It cannot be reserved again under the same action identity. A consumer that still
needs those bytes must resolve current owner-authorized references and perform a new
budgeted read with a new action key. The old receipt is preserved; empty content is
never a successful substitute. Deep owns retention enforcement at extraction, private
guidance expansion, attestation generation/verification, current revision resolution,
and reconstructing each new request after retry or restart. Those caller changes and
guidance storage are separate integration work.

For one-shot `record_action_outcome`, pass both `result_fence: AssignmentFence` and
`result_binding: AssignmentOperationBinding`, plus the current
`result_authority: SessionExecutionObservation` for interactive session origin.
Result usability requires the exact
issued assignment fence and admission binding, the current assignment lease/control
and instruction revisions, a current owner-scoped work-admission execution, live
local authority, and an active owner. Lock order is owner, selected session/grant,
assignment, admission, action, with database time sampled after the admission and
action lock waits. The host must refresh
external authority before this transaction and fence subsequent result incorporation
and publication. A valid old dispatch token can still settle factual usage after
either lease or owner authority is lost, even when these optional result arguments
are absent. That path discards returned payload bytes, projects `stale_execution`,
and cannot advance phase, wake generation, checkpoints or continuation. Exact replay
does not charge twice; a replay made after authority loss returns an unavailable
projection without rewriting previously accepted result bytes. Inspection of an old
receipt is not permission to incorporate it.

A missing, revoked, rotated, expired, unknown or malformed session observation
does not prevent an authentic issued permit from settling consumption. The fresh
authority check becomes false rather than raising before mandatory accounting;
success/failure charge once and uncertainty retains the reservation until explicit
settlement. This does not relax permit identity, outcome validation, settlement
replay or unresolved-liability checks. The operation's original incarnation must
match the fresh observation; a new same-SID row cannot substitute even when every
timestamp and encrypted credential repeats. Exact current row and generation checks
remain additional conditions. Version 1 always has a false current-result context:
its authentic issued and uncertain attempts remain chargeable once, with no new
result bytes, checkpoint advancement or wake. Explicit reconciliation and reserved
no-permit release remain available; issued attempts cannot be refunded. An exact
historical decision/completion receipt does not authorize another execution.
This does not enable Deep ingress or runners; caller adoption and institutional
staging remain separate prerequisites.

Request-local forced-refresh adapters may call
`SessionRepository.bound_request_execution_waits(transaction)` immediately after
opening each transaction, before initial reads, refresh claims, settlement reads
and compare-and-set, or final observation checks. It sets PostgreSQL transaction-local
`lock_timeout` to at most 100 milliseconds and `statement_timeout` to at most 1000
milliseconds, preserving stricter existing nonzero settings. These caps cover
ordinary server-side lock waits and query execution. A timeout must leave the
transaction through its failure path so rollback releases every acquired lock and
the pooled connection; commit and rollback both restore the prior settings.
Ordinary session operations do not opt in and retain their existing behavior.
`create_operation`, exact operation claims and approved-action claims do not install
these caps themselves: a request host must call the helper before its first repository
call, including the initial owner lock. Real PostgreSQL request-host tests hold owner,
session or assignment locks while the capped worker fails, rolls back, releases its
locks and successfully reuses its pooled connection before the blocker is released.

These are per-lock and per-statement limits, not a universal fifteen-second
physical deadline. They do not replace the original database-clock authority
sample or cancel a worker thread. Pool checkout retains its configured bound
(30 seconds by default), and connection establishment retains its default ten-second
bound. Neither is shortened by this API; a thread waiting for pool checkout has
not acquired a connection or database locks. Server/OS/network failure can also
outlive a server-side SQL timeout. An outer async timeout returns no authority;
an already-running successful refresh settlement may still finish and preserve
the exact rotated credential, but ordinary SQL contention cannot leave it holding
locks indefinitely. This does not authorize replay of an uncertain refresh or
enable session-backed one-shot ingress/continuation.

The timeout semantics follow PostgreSQL's
[client connection settings](https://www.postgresql.org/docs/15/runtime-config-client.html)
and [transaction-local configuration](https://www.postgresql.org/docs/15/sql-set.html).

Unknown positive input/result disposition versions remain inspectable and are refused
for execution. Safe cancellation and owner retirement retain their opaque actions and
liabilities. One-shot lease recovery holds these rows without interpreting nested
bindings, releasing reservations or rewriting action bytes, while supported neighbors
continue recovery. Resolve the hold through a qualified decoder upgrade or established
reconciliation procedure; never clear it with manual payload edits. Persistent recovery
retains its existing behavior. This addition uses the existing private JSON envelopes
and does not allocate a schema revision or change migration bytes.

Lease recovery returns stale operation bindings, preserves completed results, releases unstarted
reservations, conservatively charges interrupted read-only calls and holds uncertain effects.
`recover_expired_for_administration` selects only the persistent profile;
`recover_expired_operations_for_administration` selects only one-shot work. Both filter before
the batch limit so expired work in one profile cannot block the other's recovery.
One-shot recovery also expires active, unclaimed work whose original deadline has
elapsed, including work that never obtained a worker slot. This administrative
settlement obtains no execution authority, creates no claim or action, and consumes
no retry attempt. A current unexpired lease is left to its holder. Unresolved
liability or approval remains in reconciliation; unsupported versions remain held.
Terminal and held rows are not repeatedly rewritten by subsequent recovery ticks.
Recovery acquires available owner locks before assignment row locks and rechecks eligibility
after those locks. It skips busy or retired owner prefixes while collecting at most the
requested batch and trying at most 100 distinct owners per pass. A skipped owner is left
untouched; reaching the owner budget returns bounded partial progress. The detached
`terminal_assignments` records in `AssignmentRecoveryResult` identify the exact saved one-shot
failures, including their owner and state version. The caller may deliver completion events
and append its audit records in the same transaction; any delivery or audit failure must roll
back recovery. Plane never decides notification or authorization policy.
The one-shot recovery domain includes known outer versions 1 and 2, unlike new claim
eligibility. Legacy v1 liabilities are not excluded before settlement. Recovery clears
their old executable lease, retains uncertain/opaque effects and never schedules a
new due time or retry. Unsupported v2 origins are likewise held without execution.
Persistent episodes retain their bounded exponential retry deadlines. Executable
one-shot recovery uses 5/15/45-second
delays, at most its original retry allowance, and never schedules beyond the original authority
expiry or task deadline.
Supported one-shot recovery exhaustion or a deadline without unresolved liabilities
records terminal `failed`; expired authorization remains an explicit authority hold,
and uncertain effects remain in reconciliation. Resume,
restart and owner check do not reset lifetime spending or turn an uncertain effect into retryable
work. Refer to [migration and recovery](migration-and-recovery.md) for deployment/restore policy.
