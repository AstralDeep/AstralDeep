# Feature Specification: General Cresco Bridge Contract — Reconciliation and Bound

**Feature Branch**: `095-cresco-bridge-contract`

**Created**: 2026-10-13

**Status**: Contract ratified; bounded topology runtime prepared behind default-off gates. Generic dispatch, bounded executor completion and real-fabric acceptance remain pending.

**Current authority**: [`contracts/bridge-contract.md`](contracts/bridge-contract.md) and its executable conformance cases supersede the historical absence assumptions below. The following overview, decisions and requirements preserve the original document-only child scope; their references to no runtime, old wire pins and implementation not started describe that earlier snapshot. Current runtime disposition is reconciled at the end of this record.

**Input**: Issue #279: "Feature 050 planned a general Cresco bridge, but that agent/runtime is absent. The merged GaiaKeep integration is a specialized Cresco-gated archival path, not a generic topology/discovery/dispatch bridge between approved Astral deployments. This child is limited to: reconcile and bound the general cresco bridge contract."

## Overview

Three Cresco-related records exist and disagree in the details:

1. **Spec 050** ([`specs/050-cresco-integration-decision/`](../050-cresco-integration-decision/spec.md)) approved a **general** first-party Python bridge agent over the Cresco `wsapi` seam (`backend/agents/cresco/`). It was never implemented: no agent directory exists, `cresco` is absent from `BUILT_IN_AGENT_DIRS`, and `cresco-1` is absent from `FIRST_PARTY_PUBLIC_AGENT_IDS` in `backend/orchestrator/local_agents.py`.
2. **GaiaKeep (spec 092)** shipped as the merged integration: a specialized, Cresco-gated **archival** path. It requires `FF_GAIAKEEP` **and** `FF_CRESCO` together, presents the `cresco_service_key` header over a caller-owned DGX SSH tunnel to a GaiaKeep gateway, and exposes GaiaKeep collection operations — not regions/agents topology, not discovery, not generic dispatch.
3. **Constitution v6.2.0** (Principle VII, amended 2026-10-05) sanctions Cresco as approved external infrastructure reached only through a first-party Python bridge agent over `wsapi`, behind fail-closed `FF_CRESCO` (default off), with the JVM/OSGi/ActiveMQ fabric never embedded, never an internal bus, and never replacing the A2A/WebSocket protocol.

This feature reconciles the three into **one bounded contract** and delivers it with executable acceptance cases. It does **not** implement the bridge: no agent directory is added, nothing is registered, no flag default changes, no dependency is added, and no new transport enters the image. The contract bounds any future implementation and is enforced deterministically by a conformance suite that fails closed if the repository drifts from the bounded posture.

## Reconciliation decisions

| # | Question | Decision |
|---|---|---|
| D1 | Is spec 050 superseded or amended? | **Amended and bounded.** Spec 050 remains the evaluation record and general intent; the normative wire/gate/target details are superseded by [`contracts/bridge-contract.md`](contracts/bridge-contract.md). Where 050 and this contract disagree, this contract wins. |
| D2 | What happens to spec 050's stale assumptions? | **Retired in 050's artifacts**: T003a's `_FIRST_PARTY_PUBLIC_AGENT_IDS` target `backend/shared/database.py` no longer exists (AstralPlane composition removed it; the registry is `backend/orchestrator/local_agents.py::FIRST_PARTY_PUBLIC_AGENT_IDS`); FR-002's absolute "zero new dependency / never `pycrescolib`" posture is superseded by Constitution v6.2.0 Principle V (any declared dependency is permitted) and Principle VII ("any supporting library, including `pycrescolib`, is permitted under Principle V"). `pycrescolib` remains disqualified as the **default** client solely by its global TLS-verification-off default (verified 2026-07-06, see [research](../050-cresco-integration-decision/research.md) R4), which no constitution relaxes. |
| D3 | Does the merged GaiaKeep path constitute "the Cresco bridge"? | **No.** GaiaKeep is a specialized Cresco-gated archival integration: its `FF_CRESCO` use is an external-fabric **boundary** gate, its `cresco_service_key` authenticates a caller-owned SSH tunnel to a GaiaKeep gateway, and its surface is GaiaKeep collections — not Cresco topology, discovery, or dispatch. GaiaKeep's existence does **not** activate, satisfy, or substitute for the general bridge. |
| D4 | What is the current sole consumer of `FF_CRESCO`? | The GaiaKeep boundary gate (`backend/start.py::_gaiakeep_enabled`, `backend/orchestrator/local_agents.py`, `backend/agents/gaiakeep/gaiakeep_agent.py`, `backend/agents/gaiakeep/mcp_server.py`). The general bridge shares the flag's fabric-boundary semantics but MUST treat GaiaKeep's flag posture as independent of bridge activation. |
| D5 | Which wsapi profile is authoritative? | The profile pinned in [`contracts/bridge-contract.md`](contracts/bridge-contract.md) §2, shape-pinned from the 2026-07-06 evaluation (`wsapi` @ `d5b4bcd`, cross-read from `pycrescolib` 1.3.0). It is **not re-verified against live upstream in this snapshot**; byte-level re-verification against the then-current upstream is a hard precondition for any future implementation task. |
| D6 | How are targets bounded? | Member targets are admitted, operator-configured `region_agent[_plugin]` tuples matching the contract's bounded grammar (§5). Wildcards, empty components, over-depth addressing, and non-admitted targets are unsupported and fail closed. A member target exists only for a fabric the operator configured; the service key grants **no mesh admission**. |
| D7 | Which risk tiers apply? | Read (`tools:read`) for bounded topology/telemetry; write (`tools:write`) for file operations; system (`tools:system`) only for the executor wrapper — default-deny, hard security-flagged, never enabled by the safe-agent baseline, explicit per-user override required (Constitution VII). |
| D8 | What fault states are supported? | Five, closed set: `unavailable`, `unreachable`, `auth`, `protocol`, `tls` — each with a fixed user-legible result and a fixed retryable disposition (§6). Anything else maps to `protocol` (fail-safe). |
| D9 | Does this change activate anything at runtime? | **No.** No bridge agent, no wsapi client, no registration, no route, no dependency, no schema, no UI surface. `FF_CRESCO` default stays off. The only executable change is the conformance test suite. |
| D10 | What is the relationship to tracker #242 and PR #248? | This child closes only issue #279. Its relationship to the non-bounty tracker (#242) is retained; PR #248's linked work is not overwritten or re-scoped. This PR must not close the tracker or any other bounty child. |

## Requirements

- **FR-001**: The general bridge contract MUST be a single reviewable document ([`contracts/bridge-contract.md`](contracts/bridge-contract.md)) covering: the wsapi profile, the bounded topology surface, risk tiers, member-target mapping and grammar, fault states, credential and admission boundaries, and an explicit supported/unsupported matrix.
- **FR-002**: The contract MUST carry deterministic, machine-readable conformance cases ([`contracts/conformance-cases.json`](contracts/conformance-cases.json)); every case MUST have a unique id, a declared kind, a binding to an executable test, and the contract document MUST reference every case id.
- **FR-003**: The conformance suite MUST execute every case deterministically (no network, no live fabric, no clock-derived bounds) and MUST fail closed when the repository drifts from the bounded posture.
- **FR-004**: The suite MUST verify: `FF_CRESCO` default off; no `backend/agents/cresco` runtime importable or registered; `cresco-1` absent from the public first-party agent registry; GaiaKeep's dual-flag (`FF_GAIAKEEP` AND `FF_CRESCO`) boundary intact; no Cresco/JVM/broker/OSGi artifact in the image manifests or runtime requirements; and no committed Cresco credential material.
- **FR-005**: The contract MUST pin authoritative external sources with identifier, status, and retrieval date, and MUST state explicitly which pins have **not** been re-verified in this snapshot and that re-verification is a precondition for implementation.
- **FR-006**: The contract MUST state that `CRESCO_SERVICE_KEY` is an operator-local runtime secret, that possession grants no Astral role/scope/mesh admission, and that the fabric admits nothing into AstralDeep (dial-out only, egress-validated, verified TLS).
- **FR-007**: Spec 050 MUST be amended to record the reconciliation: the retired `backend/shared/database.py` task reference, the superseded dependency posture, and the bounding of its tasks by this contract — without rewriting its evaluation history.
- **FR-008**: No product runtime code, flag default, manifest, schema, workflow, or UI surface MAY change in this feature. The feature's only executable artifact is its conformance suite.

## Success criteria

- **SC-001**: The contract document and conformance cases exist, are internally consistent, and every case executes green.
- **SC-002**: The repository's current state demonstrably conforms to the bounded posture (flag off, no bridge runtime, GaiaKeep boundary intact, no new bus, no committed credentials).
- **SC-003**: Spec 050's stale assumptions are retired with a dated reconciliation note; its evaluation record is preserved.
- **SC-004**: The delivered documents claim specification status only: "contract ratified and bounded; bridge implementation not started". No phrase in the delivered artifacts may assert bridge runtime completion, live-fabric verification, or activation.

## Source traceability

Authoritative external sources (full table in the contract; pin discipline per Constitution XIII):

- `CrescoEdge/agent` @ `4093d7d`, `CrescoEdge/controller` @ `1b3cc3c`, `CrescoEdge/library` docs @ `826df9c`, `wsapi` @ `d5b4bcd`, `executor` @ `8e9020e` — retrieved/verified **2026-07-06** in [`specs/050-cresco-integration-decision/research.md`](../050-cresco-integration-decision/research.md); **not re-verified in this snapshot**; re-verification is an implementation precondition.
- `pycrescolib` 1.3.0 — evaluated 2026-07-06 (wire-shape cross-read; TLS-verify-off default observation); not adopted at runtime.
- `GaiaKeep/gfs` @ `9f30ebaeebdfdf1f44a2980d62ab0908d809e7a9` — the merged specialized integration's pinned SDK, verifiable in `backend/requirements-gaiakeep.txt` in this snapshot.

Internal authorities: `.specify/memory/constitution.md` v6.2.0 Principle VII (Cresco clause) and Principle V; `backend/shared/feature_flags.py` (`FF_CRESCO` default off); `backend/orchestrator/local_agents.py` (registries and dual-flag guard); `backend/start.py::_gaiakeep_enabled`; `backend/agents/gaiakeep/` (transport, credential handling); `backend/shared/external_http.py::validate_egress_url` (egress posture).

Open external dependency: AstralPlane issue #67 (outside this environment; status unverified here; recorded as pending and not required for this contract deliverable).

## Dependencies & sequencing

- **Builds on**: spec 050 (evaluation record), spec 092 (merged GaiaKeep boundary), Constitution v6.2.0.
- **Depends on (external)**: AstralPlane #67 — open; no change to AstralDeep's composition pin in this feature.
- **Out of scope / deferred**: implementing the bridge agent or wsapi client (any future implementation MUST adopt this contract and re-verify the upstream pins first); dataplane/log streaming; fabric-initiated inbound connections; a Cresco-side Java plugin.

## Current runtime reconciliation (2026-10-13)

The document-only feature's original snapshot predates the runtime child. Its absence checks are superseded by current conformance in `contracts/bridge-contract.md`: a present default-off bounded-topology adapter, source-verified controller/agent requests and flat responses, and unavailable generic dispatch/executor result transports. Synthetic TLS tests do not establish real-fabric acceptance or completed work. Historical evaluation and original document-only scope remain preserved.
