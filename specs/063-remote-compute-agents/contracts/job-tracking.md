# Contract: Durable Job Tracking, Boot Reconciliation & Unattended Polling

**Feature**: `063-remote-compute-agents` | **Spec**: [../spec.md](../spec.md)

Covers US4 (FR-042–FR-046): a submitted job outlives the tab, the process, and a restart, and is
truthfully reportable from any device; unattended polling is read-only.

## Submission (FR-042, FR-036, FR-037)

`submit_job` (mutating, non-destructive, **non-retryable**):
1. Resolve the cluster `remote_machine` (role must be `cluster`).
2. Compute a **submit marker** (`--comment=<nonce>` and/or a deterministic job-name tag) so a
   duplicate is detectable cluster-side (FR-037).
3. Run `sbatch` via the transport (login-shell argv wrapper).
4. On a confirmed job id → INSERT `tracked_job` {owner, machine, chat, scheduler_job_id,
   submit_marker, state='submitted'} and return the id (FR-042).
5. On a slow/lost response → the transport returns `unconfirmed` (non-retryable); the user is told
   to check the queue. A retry never creates a second job (FR-036, SC-010); if a job did land, the
   submit marker lets the next `list_queue` reconcile it.

## Boot reconciliation (FR-043, SC-009)

At startup, a reconciliation pass selects non-`terminal` `tracked_job` rows and polls each
(read-only, under machine-turn authority, below). A job that reached a terminal state during the
outage is resolved to its terminal `state` + `exit_code` — never left "running". Idempotent and
bounded; failures to reach a host leave the row untouched (retried next sweep), except a
missing machine/credential which orphans it.

## Unattended polling — read-only only (FR-044)

- The poller derives **read-only authority** via the existing machine-turn path
  (`derive_machine_authority` → `MachineTurnAuthority.derive`, `chain_authority.py:159-246`),
  narrowed to `remote-observe-1` status verbs. It updates `state`/`exit_code`/`last_polled_at`.
- It can **never** submit or cancel: consequential verbs are refused on a machine turn by the
  confirmation gate ([confirmation.md](confirmation.md)) with `unattended_refused`, independent
  of any scheduler flag (FR-044). This is defence in depth with the poller only ever calling read
  verbs.

## Finish notification opt-in (FR-045)

- `notify_on_finish` is a per-job opt-in. When a tracked job reaches a terminal state and the
  flag is set, the user is notified **through their AstralDeep clients** (the same channel
  scheduled-job notices use) — the transcript is the durable record. The opt-in copy states
  plainly that there is **no email/SMS**; the notice reaches signed-in clients, not an inbox
  (Assumptions). The discovering poll used read-only authority only (US4-4).

## Orphaning (FR-046, US1-6)

If, at poll time (or on machine deletion), the job's `machine_id` row or its
`machine_credential` is gone, tracking stops and the row is closed `state='orphaned'`,
`terminal=TRUE`, with an honest user-visible status. No further polling occurs.

## Cross-device truth (US4)

Because the record is a durable row keyed by owner, `job_status`/`list_queue` answer truthfully
from any of the user's devices, before or after a restart, without the originating tab. The
`uq_tracked_job (machine_id, scheduler_job_id)` index prevents duplicate tracking rows for one
cluster job.
