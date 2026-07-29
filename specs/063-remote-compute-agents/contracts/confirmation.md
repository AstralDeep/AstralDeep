# Contract: Durable Destructive-Operation Confirmation

**Feature**: `063-remote-compute-agents` | **Spec**: [../spec.md](../spec.md)

This is the **net-new mechanism** (spec Dependencies): a durable, single-use, expiring,
user-bound, argument-bound approval for destructive operations. It mirrors the **proven
cross-client** `scheduling_chat` pattern (`Button(action=…)` → `ui_event` → server-side
proposal), and explicitly does **not** revive the dormant `authorize_action` path (no client
emits it; every HITL refusal today is a buttonless `Alert`). Enforcement is at the **dispatch
gate**, not in the tool.

## Why the gate, not the tool (FR-030)

A tool that "forgets" to ask, a differently-named verb, a parallel tool batch, or a chained
hop from another agent must all be unable to reach a destructive effect. So the check lives at
the single authorization path both dispatch routes share — feature 056's `_authorize_and_prepare`
(the seam `execute_parallel_tools` was routed through so it stops skipping policy/taint/etc.).
Placing the destructive-confirmation check there means:
- the **single-tool** path, the **parallel batch** path, and a **chained hop** all hit it (US3-6);
- it keys on the verb's **declared destructive classification** ([verbs.md](verbs.md)) evaluated
  for the actual arguments — never the verb's name (FR-007), never the tool's cooperation.

It sits beside the existing hard-block gate (`orchestrator.py:12478-12489`), which is already an
independent upstream check (`tool_permissions.py:332-334`).

## Lifecycle

```
1. model calls a remote-control-1 destructive verb (first call)
2. gate computes classification for the ACTUAL args:
      always            -> destructive
      never             -> pass straight through (e.g. submit_job, make_directory)
      if_exists         -> read-only stat(remote_path); exists => destructive (upload_file)
      by_action:{...}   -> destructive iff action in the declared set (control_service/manage_package)
3. if destructive AND no valid approval on this call:
      - INSERT remote_operation_proposal {owner, chat, machine, verb, args_json,
        args_fingerprint=sha256(canonical args), summary, status='pending',
        expires_at=now+TTL}
      - return a Card: summary + Button(action="remote_op_decision",
        payload={proposal_id, decision:"approve"}) and a decline Button
      - verdict = confirmation_required; the verb function NEVER runs on first call (FR-029, SC-005)
4. user presses Approve (or types an approval) -> ui_event remote_op_decision
5. decision handler (server): load row; verify
      owner_user_id == session sub          (user-bound; else refuse+audit, US3-4)
      status == 'pending' and now < expires_at (else 'already used' / 'expired', US3-3)
   -> status='approved', decided_at=now
   -> RE-ENTER execute_single_tool for {verb, machine_id, args_json} carrying proposal_id
6. gate sees an approved, unconsumed proposal matching (owner, verb, args_fingerprint):
      - atomic UPDATE ... SET status='consumed', consumed_at=now WHERE status='approved'
        (single-use; a racing second execution sees non-'approved' and is refused)
      - proceeds through the REST of the gate stack (permissions, security flags, taint,
        concurrency, audit) — not a direct dispatch (FR-033)
      - executes exactly args_json, once (FR-031)
```

## The decision frame (Constitution XII)

- New accepted `ui_event` action **`remote_op_decision`** added to
  `backend/shared/ui_protocol.json` `accept_actions` (beside `schedule_decision`), routed in the
  WS accept loop like `orchestrator.py:7332-7337`. Payload: `{proposal_id, decision}` only —
  **never** the operation arguments (a client cannot redirect the target, FR-031).
- The consent `Card` + its `Button`s are ordinary astralprims rendered by the orchestrator and
  adapted by ROTE, so **web, Windows, Android, and Apple** all present and complete the flow with
  no per-client code (clients already dispatch button actions generically). This is the parity
  the dormant `authorize_action` path never had.

## Binding invariants (FR-031, US3)

| Property | Mechanism | Scenario |
|---|---|---|
| Single-use | atomic `status: approved → consumed`; second use refused | US3-3 |
| Expiring | `expires_at` absolute server time; approval past it → `confirmation_expired` | US3-3 |
| User-bound | decision `sub` must equal `owner_user_id`; else refuse + audit | US3-4 |
| Argument-bound | execute uses `args_json`; re-check `args_fingerprint`; payload can't carry args | US3-2, US3-6 |
| Restart-durable | it's a table row, not memory; pending survives restart; restart never auto-approves | US3-5, SC-006 |
| Gate-enforced | check in `_authorize_and_prepare`, on single/parallel/hop paths | US3-6, SC-004 |

## No-live-human refusal (FR-033)

Before the confirmation logic, the gate checks the turn's principal. If the turn is a
**machine turn** — `turn_class ∈ MACHINE_TURN_CLASSES` (`chain_authority.py:43`:
`scheduled_job`, `parser_replay`, `draft_self_test`), i.e. authority derived from stored
offline-grant consent rather than a live session — any `remote-control-1` verb is refused
**outright** with `unattended_refused`, regardless of granted scope (there is nobody present to
approve). Only the read-only poller runs unattended ([job-tracking.md](job-tracking.md)). This
refusal does not depend on a scheduler flag staying off (FR-044).

## Watch-client degradation (FR-033, US3-7)

The watch form-factor cannot present an actionable approve control. Driven from the shared
definition (Constitution XII web-only/degradation carve-out), a destructive proposal on a watch
renders a "continue on your phone or desktop" message; **no approval can be registered from the
watch** — the `remote_op_decision` handler still requires the owner's session, and the watch
simply never shows the button.

## Audit (FR-047, FR-048)

Every proposal (creation, approval, decline, expiry, consume-and-execute) and every executed
consequential operation with its target is appended to the hash-chained audit under the
existing `agent_lifecycle`/`conversation` classes, identifying the acting user and machine and
correlated by `proposal_id` — sufficient to reconstruct, after the fact, what was done to which
machine, by whom, under which approval. Secrets never appear (FR-049).

## Adversarial coverage (SC-004) — the US3 suite asserts none of these execute a destructive effect

differently-named verb · repeated call · parallel tool batch · chained hop from another agent ·
expired approval · reused approval · another user's approval · approval redirected to different
arguments · a machine-initiated turn. Target: **zero** destructive executions without a fresh,
matching, user-issued, unconsumed approval across ≥ 20 attempts.
