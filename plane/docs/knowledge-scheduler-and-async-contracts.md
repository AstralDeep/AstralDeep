# Knowledge, scheduler-extended, and async contracts

This schema-neutral feature-074 slice completes the Plane public contracts over tables already
present in the extracted `066.001` baseline. It adds no migration. Every repository method accepts
an explicit caller-owned transaction or query executor, and every cross-owner/system operation is
named `for_administration` so AstralDeep must make the authorization and audit decision first.

## Knowledge and personalization

`create_knowledge_repository()` returns a grouped facade with four stateless stores:

- `interactions` records an owner-bound conversation interaction or an explicitly administrative
  system interaction, lists unsynthesized rows deterministically, reloads an exact bounded ID set
  in caller order after restart, and verifies the entire ID set after marking synthesis complete;
- `quality_signals` stores bounded window aggregates, uses `(agent_id, tool_name, window_end)` as
  its replay identity, and requires an exact `computed_at` fence before replacing changed
  semantics;
- `quarantine` can hold only feedback proven to be active and owned by the supplied user, then
  requires an administrator-only detected-time lifecycle CAS to release or dismiss it; and
- `proposals` serializes proposal creation per agent/tool with a transaction advisory lock,
  supersedes an older pending proposal, and fences review/application transitions by status.

`create_personalization_graph_repository()` is independently cataloged as
`personalization_graph`. It creates both directions of a memory link only after proving both live
memory endpoints share the supplied owner, rejects partial persisted link pairs, stores
owner-isolated short-term signals under immutable replay identities, and records bounded
consolidation results. Model synthesis, review policy, artifact publishing, and selection of which
memories should be linked remain in AstralDeep.

`preferences.personalization.list_memory_envelope_page` is a read-only, schema-neutral
query over existing memory rows. Callers provide an owner, one exact project (or global
`None`), and bounded format/kind identifiers. A named envelope header contract requires
JSON object fields `format`, `kind`, `owner_id`, and `project_id`; format/kind must be
strings, owner must exactly match the row owner, and project must exactly match the row
project, including explicit JSON `null` for global rows. Missing, malformed, scalar,
wrongly typed, superseded, or mismatched headers do not consume result slots. The row
owner/project predicates remain authoritative and apply independently of the JSON.

The existing owner/project and live-memory indexes narrow the authorized row scope.
Within that scope PostgreSQL validates JSON before casting and filters headers before
the limit. The query returns at most 1..100 detached records, reads at most one additional
matching row, and returns a typed owner/project/format/kind-bound continuation ordered by
`(created_at DESC, id DESC)`. It performs one query and no writes, offsets, or automatic
overfetch loops. PostgreSQL may inspect additional owner-scoped rows to evaluate the
filter; this does not promise constant physical scan cost or a new envelope index.

Keyset iteration is not a snapshot: inserts newer than the cursor appear on a fresh
listing, and deleted rows disappear. Creation keys supplied through the existing memory
write contract are non-null and non-negative. The host validates its complete envelope,
ciphertext binding and product policy after this query. The separate first-class Atlas
page/tombstone store is exposed by `atlas_pages` through schema `089.007` (revisions) and `089.008` (links); this envelope query
still does not parse private bodies, adopt new namespaces, or migrate legacy content.

## Scheduler-extended state

`create_scheduler_repository()` is the complete typed scheduled-job/occurrence/run/effect-ledger
boundary used by Deep's public scheduler store. It owns definition replay, next-occurrence CAS,
bounded due selection, occurrence claiming/recovery, operation binding, run/effect idempotency, and
the atomic staged-chat publication seam. Deep keeps recurrence, command, notification, and chat
policy and now injects the one application Plane runtime/catalog; the scheduler store does not hold
or reconstruct a legacy database pool.

Three focused factories complement it:

- `create_background_task_repository()` persists the legacy task projection under owner, expected
  status, and expected operation-generation predicates. Its operation projection is monotonic and
  idempotent, terminal timestamps are mandatory, notification is a one-row owner CAS, and bounded
  administrative retention methods use row locks for legacy operation-FK-null rows.
- `create_maintenance_repository()` owns unit and input membership, idempotent unit creation,
  `FOR UPDATE SKIP LOCKED` selection, exact lease-token/claim-generation/state-revision updates,
  operation-generation binding, input completion, and terminal output-generation/digest fencing.
  Expired claimed/running units are recovered under bounded row locks before selection, applying
  the persisted maximum-attempt policy. System-wide selection methods are explicitly administrative.
- `create_tracked_job_repository()` owns external scheduler-job metadata. Ordinary reads are owner
  scoped; the cross-owner open-job page is explicitly administrative. Poll writes require the
  owner, expected failure count, and exact prior poll timestamp so two pollers cannot silently
  overwrite one another. Notification is terminal-only and compare-and-set; owner-wide deletion
  exists only for an already-authorized account-retirement transaction.

`create_work_admission_repository()` owns the related operation-record and hierarchical slot
authority. It is documented separately in `work-admission-and-quality-audit-contracts.md`; the
three extended-state repositories above do not duplicate admission or scheduled-occurrence rows.

Plane does not execute coroutines, jobs, models, SSH commands, notifications, or maintenance
outputs. AstralDeep keeps those policies and supplies only validated records and state transitions.

## Optional job policy, episode admission and Stop (088.007)

Revision `088.007` adds two additive tables behind `SchedulerRepository`:
`scheduled_job_policy` (one optional versioned row per definition) and
`scheduled_occurrence_assignment` (a durable occurrence-to-assignment binding, unique by
occurrence and owner, with a foreign key to the owner's `persistent_assignment` row). A definition
without a policy row keeps every pre-088.007 semantic byte for byte: the due scan, run-now,
`update_job_after_run_for_administration` (last-run / next-run projection), pause, delete and
`cancel_unstarted_occurrence` never consult these tables, and `admit_assignment_episode` on such a
definition returns the typed refusal `policy_missing` after writing nothing. Old jobs gain no run
limit.

`ScheduledJobPolicy` (`astralplane.repositories.scheduler_models`) carries `max_runs` (nullable,
1..1,000,000), `admitted_runs`, `per_episode_limits` (bounded snake_case name to non-negative
integer pairs, at most 32), `max_outstanding_episodes` (default 1, at most 64), `monitor_changes`,
`definition_revision`, `terminal_stop`, `last_assignment_id` (the last logical task reference) and
`updated_at`. Every bound is validated before SQL runs, so a negative or oversized limit never
reaches the database. `admitted_runs`, `terminal_stop` and `last_assignment_id` are scheduler
owned: `put_job_policy(transaction, policy=, expected_version=)` creates with expected version 0
(those fields must start unset) or replaces under version CAS and refuses
`scheduled_job_policy_charge_rewrite` when a caller tries to lower a charge or clear a Stop. A
policy whose `max_runs` is below `admitted_runs` is refused as a `ValueError` by the record itself.
`get_job_policy` is owner scoped and returns `None` for legacy definitions.

`admit_assignment_episode(transaction, *, owner_id, job_id, occurrence_id, claim_generation,
lease_token, lease_owner, assignment_id, admitted_at, spend=1)` binds one current claim to one
episode and charges the allowance in the caller's transaction. Lock order is definition
(`scheduled_job FOR UPDATE`, which also requires an executable definition), policy, occurrence
(`assert_current_claim`), binding. The caller creates or locks the episode's persistent assignment
before calling admission, which keeps the assignment-before-admission order documented in
`persistent-assignment-contracts.md`. The typed `EpisodeAdmission` reports `admitted`, `created`
and a reason: `admitted` (binding inserted, `admitted_runs += spend`, `last_assignment_id`
updated, policy version advanced), `replayed` (the identical binding already existed; nothing is
charged), or a refusal that writes nothing: `policy_missing`, `terminal_stop`,
`episode_outstanding` (bindings whose assignment is still `active`/`paused` already reach
`max_outstanding_episodes`, so an outstanding episode consumes nothing more) and
`allowance_exhausted` (`admitted_runs + spend > max_runs`). A stale claim, a definition that is
paused/disabled, an occurrence of another definition, a missing/foreign/resolved assignment, or an
occurrence already bound to a different assignment fails closed with `PlaneError`.

`stop_assignment_job(transaction, *, owner_id, job_id, expected_version, stopped_at)` is the
terminal Stop for a policy job. Atomically it sets `terminal_stop` under version CAS, moves the
definition to `completed` with `next_run_at = NULL` so the due scan never materializes it again,
cancels every unstarted occurrence (`pending`/`retryable`/`claimed`) through the existing
`cancel_unstarted_occurrence` path with terminal code `cancelled_job_stopped`, and returns a
`JobStopOutcome` with the cancelled occurrence ids, their operation ids (so the host cancels the
matching work-admission records in the same transaction) and the still outstanding assignment ids
so Deep stops each episode family. Bindings, charges, runs and started occurrences are retained; a
repeat Stop returns `stopped=False` with the same outstanding families. Pausing a policy job is the
ordinary `paused` transition and never touches the policy row. `list_outstanding_episodes` is the
delete precondition: a policy job may be deleted only when it is empty.

## Bounded event-loop adapter

`AsyncPlaneRuntime` is the only async composition adapter. `run_in_transaction(callback)` admits a
bounded number of operations, then runs the complete synchronous Plane transaction and callback on
one worker thread. It deliberately does not expose `afetch_one`, `afetch_all`, `aexecute`, raw SQL,
connections, or commits. Admission has a bounded wait and raises
`async_plane_capacity_unavailable` rather than growing an unbounded executor queue.

The callback must complete synchronously inside its one caller-owned transaction. Coroutine
functions, `functools.partial` wrappers around them, and callable objects with an async
`__call__` are rejected with the typed `async_plane_callback_type` error before admission, so no
event loop is bound and no transaction starts. A synchronous callable that returns an awaitable
closes any coroutine it produced and fails with the same typed error inside the transaction, which
rolls back instead of committing an empty result; the capacity slot is released exactly as for
other worker failures, and cancellation behavior is unchanged. Regular synchronous results remain
supported.

Python cannot cancel a worker thread already inside PostgreSQL. If an awaiting coroutine is
cancelled, the adapter retains that capacity slot until the transaction finishes and consumes any
worker failure. Retryable product callbacks must therefore use the repositories' idempotency and
CAS identities. `close()` rejects new admissions but does not close the composition-owned
`PlaneRuntime`; the host still owns runtime shutdown.

`await adapter.drain(timeout=5.0)` closes admission and waits for all admitted workers to finish.
The timeout is a finite number from 0.01 through 300 seconds; `None`, booleans, non-numeric values,
infinities, NaN, and values outside that range are rejected before loop binding or admission changes.
Drain uses the adapter's owning event loop and refuses a different loop without closing admission.
It returns `True` when no admitted worker remains and `False` when the wait times out. Repeated or
concurrent drain calls are safe, including after an earlier timeout.

Timeout and cancellation of a drain waiter do not cancel a database transaction or release its
capacity slot early. Admission stays closed; worker errors still reach their original waiter or are
consumed if that waiter was cancelled. A successful drain confirms completion rather than success
of every callback. After `True`, the host may call `runtime.close()` when its other callers have also
finished. After `False` or waiter cancellation, keep the event loop and runtime alive and retry the
drain; `PlaneRuntime.close()` continues to refuse closure while a connection is borrowed.

## Verification and rollback

Focused contract verification covers successful writes, replay, owner mismatch, stale fences,
missing rows, corrupt persisted shapes, bounded inputs, async cancellation, and capacity refusal.
The changed repository units retain focused branch-aware coverage above the feature's 90% floor.
The complete suite runs on real PostgreSQL in CI; the digest-only migration evidence in
`provenance/checks.json` is the historical `074.004` record, not current evidence (see
`provenance/README.md`).

Rollback is code-only: restore the prior Plane/Deep composition. Do not delete or rewrite
interaction, quality, quarantine, proposal, memory, scheduler, maintenance, or tracked-job rows.
