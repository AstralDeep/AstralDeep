# Feature Specification: Bounded Personal-Mesh Availability Topology

**Feature Branch**: `100-mesh-availability-topology`

**Created**: 2026-10-21

**Status**: Topology decision ratified at specification level with executable conformance cases; no deployment, release, failover activation, or runtime-availability claim is made.

**Input**: Issue #283: "Current durable work/lease recovery is a useful single-deployment foundation, but it does not qualify a personal mesh surviving loss of an orchestrator, executor or durable-service node. Rescheduling uncertain effects without proof risks duplication or false success. This child is limited to: select a bounded personal-mesh availability topology."

## Overview

AstralDeep's durable work runtime already provides the single-deployment foundation: durable admission with slot leases and execution fencing ([`backend/orchestrator/work_admission.py`](../../backend/orchestrator/work_admission.py)), owner wait-for-event and uncertain-attempt reconciliation bound to prior-result digests ([`backend/orchestrator/work_continuations.py`](../../backend/orchestrator/work_continuations.py)), and fail-closed binding to the one embedded AstralPlane runtime over PostgreSQL ([`backend/orchestrator/plane_composition.py`](../../backend/orchestrator/plane_composition.py)). The personal mesh ([`backend/orchestrator/mesh_api.py`](../../backend/orchestrator/mesh_api.py) and its enrollment/admission/presence modules) connects approved members and devices, but nothing yet *decides* what availability the mesh promises when an orchestrator, an executor, or a durable-service node is lost — and rescheduling uncertain effects without proof would risk duplicated or falsely-successful work.

This feature selects that topology and delivers it as a complete, reviewable decision with a failure matrix: [`contracts/availability-contract.md`](contracts/availability-contract.md). The decision is bounded to Plane's current embedded-PostgreSQL boundary (Plane Constitution I: an embedded library with PostgreSQL as its only database; no service port, no second database). A distributed database is recorded as an open design question, not an imposed backend. The feature changes no product runtime code, no flag default, no schema, no workflow, and no UI surface: its only executable artifact is the conformance suite ([`backend/tests/test_mesh_availability_topology.py`](../../backend/tests/test_mesh_availability_topology.py)) executing the machine-readable cases in [`contracts/availability-cases.json`](contracts/availability-cases.json).

## Decisions

| # | Question | Decision |
|---|---|---|
| D1 | What is the selected availability topology? | **T1 — single durable authority per deployment with bounded in-place recovery.** One active orchestrator process group, one embedded PostgreSQL (AstralPlane) as the sole durable authority, one configured blob root. Loss of any node fails closed and recovers in place: lease expiry and re-claim for interrupted work, fenced at-least-once outbox redelivery, and owner-decided reconciliation for uncertain external effects. Full rationale and the alternatives in the contract ([§Decision](contracts/availability-contract.md#decision)). |
| D2 | Is orchestrator replica pairing (active/passive sharing one PostgreSQL) selected? | **Not selected; recorded as a bounded future option.** Admission fencing is already database-authoritative, so work-claim handover is mechanically available, but the blob root, personal-agent artifacts, evidence archive, and per-node ephemeral archives are host-bound today. Supporting replicas first requires shared/replicated blob mechanics — a Plane design question, not imposed here. |
| D3 | Is a distributed/consensus database selected? | **No.** It would add a second database or replace the embedded PostgreSQL boundary, which Plane's constitution forbids and this issue explicitly leaves as a design question. Recorded as deferred with its preconditions; nothing is imposed. |
| D4 | What disaster-recovery path is supported? | **T4 — operator-managed backup/restore.** RPO equals the operator's backup cadence (the only non-zero RPO in the matrix). Restore requires explicit operator attestation of the restored database; Plane's post-restore session retirement ([`components/AstralPlane/src/astralplane/recovery.py`](../../components/AstralPlane/src/astralplane/recovery.py)) refuses anything but the current schema revision and migration digest, and clients re-authenticate afterwards. |
| D5 | What happens to uncertain external effects after executor or orchestrator loss? | **Never auto-resolved.** Uncertain attempts surface honestly (retryable/unconfirmed/orphaned) and are settled only by the owner's explicit reconcile decision bound to the prior-result digest ([`work_continuations.py::WorkReconcileRequest`](../../backend/orchestrator/work_continuations.py)). No effect is reported applied without that decision. |
| D6 | What does the mesh promise about member/device availability? | **Honest presence only.** Enrollment and membership are durable in Plane; presence is a TTL-bounded advertisement (default 300 s, clamped 30–3600 s) that expires to `offline`/`unavailable` with a stated reason. No member is advertised live beyond its published observation. |
| D7 | Does this feature change runtime behavior? | **No.** No product code, flag, manifest pin, schema, or UI surface changes. The conformance suite is the only executable artifact, and it asserts the *current* snapshot matches the decided posture, failing closed on drift. |
| D8 | What is the relationship to tracker #243 and sibling issues? | This child closes only issue #283. The parent tracker (#243) is a non-bounty tracker and is not closed by this work; dependency #279 is already closed by spec 095; AstralPlane #58 remains an open external dependency recorded honestly as unverified in this snapshot. |
| D9 | What status may the delivered documents claim? | **Specification status only**: decision ratified, conformance cases executable, qualification evidence classes named. No phrase may assert deployment, release, live failover, or measured availability. |

## Requirements

- **FR-001**: The availability decision MUST be a single reviewable contract ([`contracts/availability-contract.md`](contracts/availability-contract.md)) covering: the selected topology and its rationale, the compared options within Plane's embedded-PostgreSQL boundary, the failure matrix (orchestrator, executor, PostgreSQL, blob, mesh member, delivery, post-restore), consistency semantics, RPO/RTO per failure class, honest degraded states, recovery promises with required evidence, and explicit unsupported states.
- **FR-002**: The contract MUST carry deterministic, machine-readable acceptance cases ([`contracts/availability-cases.json`](contracts/availability-cases.json)); every case MUST have a unique id, a declared kind, a binding to an executable test, and the contract document MUST reference every case id.
- **FR-003**: The conformance suite MUST execute every case deterministically (no network, no live database, no clock-derived bounds, no wall-clock performance bounds) and MUST fail closed when the repository drifts from the decided posture.
- **FR-004**: The suite MUST verify at minimum: the single-durable-authority boundary (no second database module or distributed-DB driver); the lease/fence recovery mechanics the decision relies on; owner-decision-only resolution of uncertain effects; honest executor-loss outcomes; fenced at-least-once outbox redelivery; fail-closed composition on contract drift; operator-attested restore hygiene; fail-closed blob-root resolution; and honest TTL-bounded presence.
- **FR-005**: The failure matrix MUST state, for every failure class: detection, consistency behavior, RPO, RTO disposition, the honest degraded state, and the recovery action — plus the states the topology explicitly does not support.
- **FR-006**: Every recovery promise MUST name its required evidence classes and be marked `specified`; none MAY be marked delivered, deployed, or released.
- **FR-007**: Source claims MUST trace to files and symbols verified in the current snapshot (anchor commit recorded in the contract); external/unverified references MUST be marked as such with retrieval discipline per Constitution XIII.
- **FR-008**: No product runtime code, flag default, composition pin, schema, workflow, or UI surface MAY change in this feature. The feature's only executable artifact is its conformance suite.

## Success criteria

- **SC-001**: The contract document and acceptance cases exist, are internally consistent, and every case executes green in the default backend suite.
- **SC-002**: The repository's current state demonstrably matches the decided posture (single durable authority, lease/fence recovery, owner-decided reconciliation, fail-closed composition, operator-gated restore, honest presence), and the suite fails closed on drift.
- **SC-003**: The failure matrix covers orchestrator, executor, PostgreSQL and blob loss, consistency, RPO/RTO, and honest degraded states, with explicit unsupported states.
- **SC-004**: The delivered documents claim specification status only. No phrase asserts runtime availability completion, live failover, deployment, or release.

## Acceptance scenarios

1. **Given** the delivered contract and cases, **When** the conformance suite runs in CI, **Then** every case passes deterministically and any drift from the decided posture (e.g. a second database path, a weakened reconciliation gate, an unattested restore path) fails the suite.
2. **Given** the failure matrix, **When** a reviewer checks any failure class (orchestrator, executor, PostgreSQL, blob, member, delivery, restore), **Then** they find detection, consistency, RPO/RTO disposition, honest degraded state, recovery action, and unsupported boundaries stated, each traceable to a verified source symbol.
3. **Given** the recovery promises, **When** an implementer of a future availability child reads them, **Then** each promise states what work/client recovery is promised, what evidence is still required, and that nothing is asserted as deployed or released.

## Source traceability

Internal sources are verified in this snapshot at anchor commit `3627625edb6ca461c0a847dc160b978496c39f72` (file/symbol table in the contract). The issue's baseline links (`work_admission.py`, `work_continuations.py`, `plane_composition.py` at `e9a47d8b826dc506dd509b6110a5199ad1cddc42`) were not re-fetched from the network in this environment; the same paths and symbols were verified directly in the current snapshot instead, and the contract records this honestly.

External dependencies: AstralPlane issue #58 — outside this environment; status unverified here; recorded as pending and not required for this specification deliverable. Tracker #243 and bounty framing are task metadata, not technical sources.

## Dependencies & sequencing

- **Builds on**: the durable work runtime (work admission, continuations, plane composition), the personal-mesh enrollment/presence modules, AstralPlane's outbox/recovery mechanics, and Constitution v6.2.0 Principles IX and XIII.
- **Depends on (external)**: AstralPlane #58 — open; no change to AstralDeep's composition pin in this feature.
- **Out of scope / deferred**: implementing orchestrator replicas, shared/replicated blob mechanics, any standby or distributed database, mesh-wide work migration, and cross-deployment transactions. Any future implementation child MUST adopt this contract and deliver its named evidence classes first.
