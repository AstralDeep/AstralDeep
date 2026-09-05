# Research decisions: Persistent Agents

Source: refreshed Deep `34609998` and exact composed components, inspected 2026-09-05. Registered external tools were not assumed to exist.

## Durable execution

**Decision**: Compact Plane assignment repository and bounded Deep execution episodes.

**Rationale**: Scheduler runner already derives fresh authority and fences occurrences. `async_tasks.py::BackgroundTaskManager.submit` retains executable factories in process-local `_pending_executions`; schedule pause cancels only unstarted occurrences. Neither provides standing instruction revisions or reconstructable task graphs.

**Alternatives**: Repeated scheduled chat loses continuity and resets budgets; temporary closures cannot recover; a third-party workflow service duplicates authority/admission and adds an unapproved dependency.

## Public source without new credentials

**Decision**: Start with `web-research-1:fetch_page`, stable bounded content digests and a generic explicit reader-tool contract.

**Rationale**: Bundled fetch_page uses approved egress and size/time limits without a provider key. Outlook currently sends mail/probes credentials; no bundled inbox or bug reader exists. The owner changed the live example to avoid extra setup.

**Alternatives**: New provider connectors exceed the revised scope; raw HTTP in the controller bypasses tool authorization; disconnected sources must report honestly.

## Authority and controls

**Decision**: Explicit grant plus MachineTurnAuthority and normal gate-stack dispatch, with persistent revision/lease/resource checks at each effect and ordered dispatch-start/control acknowledgment.

**Rationale**: Authority derivation already intersects consent/current grants. Turn permission memoization cannot alone enforce immediate revocation; in-process cancellation cannot fence another worker.

**Alternatives**: Direct agent functions, stored access-token replay and status-only checks bypass or weaken existing security.

## Approval and uncertain effects

**Decision**: Durable exact proposals, owner/revision/preconditions/expiry binding, single-use foreground dispatch and durable result return. Uncertain outcomes require reconciliation.

**Rationale**: Remote confirmation deliberately refuses unattended mutations before consuming approval markers; scheduler excludes unreviewed write/execute scopes. Optional generic HITL alerts do not durably resume work.

**Alternatives**: Removing restrictions would violate existing policy; treating uncertain actions as failed creates duplicate effects.

## Task results and budgets

**Decision**: Bounded durable task graph, immutable completed results and incorporation markers, shared cumulative resource reservations.

**Rationale**: Existing subtasks provide isolation, attenuation, digest scanning and parent-debiting ChainBudget, but discard results on parent cancellation and retain budgets only in memory.

**Alternatives**: Personalization memory is user knowledge, not an execution ledger; unbounded JSON breaks resource controls; unverified prices cannot guarantee a currency ceiling.

## Shared controls

**Decision**: Projection-owned Schedule assignment views using existing primitives; Deep authenticated handlers and one action manifest; explicit wrist status/chat control disposition.

**Rationale**: Existing primitives suffice. Deep does not currently call Projection's reusable scheduler view. Apple tests enumerate the action count; watch buttons are not automatically supported.

**Alternatives**: New primitives/SPA are unnecessary; web-only test results cannot establish all-client parity.

## Initial environment

The first clean composition check found Primitives base.py checked out with CRLF bytes. Restoring the exact HEAD blob, disabling component-local autocrlf and refreshing the unchanged index restored the pinned digest and passed composition verification. No tracked Primitives change or pin change was made. Host .venv supplies Python 3.11.15 but initially lacks runtime/test packages; Docker Engine is available and baseline images are building.
