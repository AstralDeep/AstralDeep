# Feature Specification: Cresco Target-Bound Work Dispatch

**Feature Branch**: `096-cresco-target-dispatch`
**Created**: 2026-10-14
**Status**: Approved — implementation in progress
**Input**: Issue #282: "Dispatch authorized work to an approved Cresco target."

## Overview

Spec 050 approved a general first-party Cresco bridge; this child delivers one bounded
slice of it: **dispatching authorized work to an approved Cresco target**. The bridge
agent is a first-party in-process agent (`backend/agents/cresco/`, `agent_id="cresco-1"`)
that reaches an operator-configured Cresco fabric over its `wsapi` WebSocket seam. Every
effect flows through the existing ordinary authority seams — per-user tool permissions,
tool-security hard flags, RFC 8693 delegation at the orchestrator's dispatch stack,
remote-operation confirmation cards, taint, egress validation, and the hash-chained
audit. Discovery and reconnect never bypass admission: only operator-configured approved
targets are actionable, and discovery results never widen that set.

## Scope

- **In scope**: the bridge agent package; read-only topology tools; the
  `cresco_dispatch_work` effect tool; the `cresco_run_command` executor wrapper
  (system scope, hard security flag, default-deny); target grammar and approved-target
  admission; wsapi client (verified TLS, egress validation, bounded timeouts, fault
  states, bounded reconnect); registration, confirmation policy, taint and error-code
  wiring; deterministic multi-node staging tests with authorized/denied exchanges,
  revoked/stale targets, malformed/late results and connection failures.
- **Out of scope**: dataplane/log streaming, fabric-initiated inbound connections, a
  Cresco-side Java plugin, PHI-affecting UI protocol changes (results render through the
  existing astralprims path; no client disposition changes).

## Requirements

- **FR-001**: Dispatch must be target-bound: every effect names one approved
  `region_agent[_plugin]` target; targets match the bounded grammar (two or three
  lowercase components, `[a-z0-9][a-z0-9-]{0,62}` each, total ≤ 192) and must appear in
  the operator-configured approved set (`CRESCO_APPROVED_TARGETS`, default empty ⇒
  default-deny). Discovery never auto-approves.
- **FR-002**: The wsapi client dials only after `shared/external_http.validate_egress_url`
  accepts the https form of the configured host; private hosts require the host-scoped
  `CRESCO_ALLOW_PRIVATE_HOST` opt-in naming the exact configured host. TLS is verified
  (system trust, `CRESCO_CA_BUNDLE`, or `CRESCO_TLS_FINGERPRINT` SHA-256 pin); a
  self-signed certificate is refused without explicit trust.
- **FR-003**: Fault states are a closed set — `unavailable`, `unreachable`, `auth`,
  `protocol`, `tls` — each with a fixed user-legible message (`tool_feedback.PUBLIC_ERRORS`)
  and a fixed retryable disposition; unmapped failures fail safe into `protocol`.
- **FR-004**: A dispatched work order carries a correlation id; the result window is
  bounded. A result that never arrives, arrives late, or arrives malformed leaves the
  dispatch **unconfirmed** (the effect may have taken effect) with the work id retained
  for reconciliation — never reported as success, never replayed automatically.
- **FR-005**: The executor wrapper is registered at `tools:system` scope and is
  hard-security-flagged (CODE_EXECUTION, blocked) by the registration analyzer; it is
  default-deny, never enabled by the safe-agent baseline, additionally requires an
  operator env switch plus an explicit per-user override in its own handler, and every
  attempt is audited.
- **FR-006**: Every dispatched effect and every local refusal writes audit records
  carrying the fabric identifiers (full target tuple, work id) via the existing
  `agent_tool_call` audit class.
- **FR-007**: `FF_CRESCO` (default off, fail-closed) gates registration, standalone
  start, the agent's own tools, and safe-seed exclusion (the agent is never auto-marked
  safe).
- **FR-008**: Fabric topology and results are untrusted content: `cresco-1` is a taint
  untrusted source and its effect tools are taint sinks; results render only through
  astralprims components.

## Acceptance scenarios

1. Authorized dispatch: an approved target on a staged two-region fabric receives the
   work order and the correlated result renders; audit rows carry the target and work id.
2. Denied dispatch: a grammar-valid but non-approved target is refused locally before
   any dial; the refusal is audited.
3. Revoked/stale target: the fabric reports the agent gone → typed stale-target failure;
   an operator-revoked (de-approved) target is refused at admission.
4. Malformed/late result: frame drift or a result after the bounded window → unconfirmed
   verdict with diagnostic; no misattribution of success.
5. Connection failure and recovery: refused dial → `unreachable` (retryable); the next
   call succeeds without stale state.
6. Executor denial: the wrapper is denied by the security-flag gate for every caller,
   the safe-agent baseline never enables it, and denials are audited.

## Dependencies

- Builds on spec 050 (evaluation record and bridge intent), the merged GaiaKeep
  integration (specialized archival path — independent flag posture, not a substitute),
  tracker #242, and dependencies #281/#277 as recorded on the issue.
