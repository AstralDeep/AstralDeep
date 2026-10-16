# Feature Specification: Cresco Integration (Bridge Agent)

**Feature Branch**: `050-cresco-integration-decision`
**Created**: 2026-07-06
**Status**: Approved — bounded and reconciled by `specs/095-cresco-bridge-contract/` (2026-10-13); bridge implementation remains pending
**Input**: User description: "create a new specification for attempting to integrate Cresco into the AstralDeep system … provide a final decision as to whether or not to implement Cresco. https://github.com/CrescoEdge/agent https://github.com/CrescoEdge/controller https://github.com/CrescoEdge/library"

## Overview

Cresco (CrescoEdge: `agent`, `controller`, `library`) is a hierarchical, secure, multi-tenant distributed **edge-computing agent mesh** — Java/OSGi agents under regional and global controllers, communicating over embedded ActiveMQ brokers, exposing an external WebSocket gateway (`wsapi`). AstralDeep is an LLM-agent orchestration platform. The two are **complementary layers**: Cresco manages distributed compute/devices; AstralDeep decides, consents, and audits LLM-driven tool calls.

This feature evaluated whether and how to integrate Cresco, and the evaluation is **decided (approved)**:

- **NO-GO on platform-level adoption** — no JVM in the product image, no ActiveMQ as an internal bus, no replacement of the A2A/WebSocket agent protocol, no `pycrescolib` runtime dependency. This is barred by Constitution Principle I (Python-only backend) and Principle V (no unapproved dependencies), and the layers do not substitute for one another.
- **GO on a narrow, first-party Python bridge agent** that reaches a Cresco fabric through its `wsapi` seam, treating the fabric as **external infrastructure** (the same posture as Keycloak). This is what this spec now specifies for implementation.
- **DEFER a Cresco-side bridge plugin** (Java, presenting an AgentCard to AstralDeep) unless the fabric must *initiate* toward AstralDeep.

The evaluation findings and the implementation plan are folded into this spec (below and in [plan.md](plan.md)); tasks are in [tasks.md](tasks.md). Supporting design artifacts: [research.md](research.md) (long-form evaluation + SHA-pinned facts), [data-model.md](data-model.md) (config/wire/tool entities; no schema change), [quickstart.md](quickstart.md) (fabric bring-up + verification runbook), and [contracts/](contracts/) (wsapi-client, tool, and audit contracts). Unlike the evaluation phase, the bridge is **product code**: Constitution Principles III (coverage), X (production readiness), and XI (CI) apply in full.

## Evaluation findings (verified 2026-07-06)

Findings are pinned to sources retrieved 2026-07-06; Cresco is under active development (whole org pushed 2026-07-05/06), so repository facts are pinned by commit SHA.

- **Architecture.** Global → regional controllers (each embedding an ActiveMQ broker) → agents; addressing `region_agent[_plugin]`; a MsgEvent control plane over ActiveMQ plus a pub/sub dataplane; JDK 21, Apache Felix OSGi, single executable JAR; the controller embeds ActiveMQ + Derby. Identity is mutual-TLS X.509 (`CN=agent, OU=region, O=tenant`) with `SUPERUSER`/`TENANT` broker roles and tenant isolation. (agent @ `4093d7d`, controller @ `1b3cc3c`, docs @ `826df9c`.)
- **External seam (`wsapi`).** A secure WebSocket gateway on `wss://host:8282` with paths `/api/apisocket` (control), `/api/dataplane` (streams), `/api/logstreamer` (logs); clients send a `cresco_service_key` HTTP header. The wire format is **implementation-defined** (no protocol spec; read from the `pycrescolib` 1.3.0 source): JSON envelopes `{"message_info": {...}, "message_payload": {"action": ...}}`, with bulk params gzip+base64-encoded. (wsapi @ `d5b4bcd`.)
- **Executor is the risk center.** The `executor` plugin "runs arbitrary shell commands by design — a remote execution surface," with authorization delegated to the broker/tenant layer only; the `cresco_service_key` is a single all-or-nothing fabric credential. Any LLM-facing wrapper concentrates the entire per-user safety case in AstralDeep's gates. (executor @ `8e9020e`.) It is enabled by default on a global node.
- **Zero-new-dependency feasibility (proven live).** A single-node fabric was brought up from the released `agent-1.3-SNAPSHOT.jar` (JDK 21+) and driven end-to-end from Python twice: once with `pycrescolib` 1.3.0, and once with a raw client using **only the `websockets` library already in `backend/requirements.txt`** (`websockets>=12.0`) plus stdlib (`json`/`gzip`/`base64`/`ssl`), round-tripping `listregions`/`listagents`. Therefore the bridge needs **no new third-party libraries**. `pycrescolib` is deliberately not adopted: it would add `backoff` (absent from requirements) and disables TLS verification globally by default.
- **What the bridge must supply on the AstralDeep side.** Cresco has no LLM layer, no per-user consent or per-tool permission model (principals are machines/tenants), no human-anchored delegation, no per-user tamper-evident audit chain. The bridge therefore inherits AstralDeep's existing gates — none of which need to be built: fail-closed `AGENT_API_KEY` registration (`orchestrator/auth.py::validate_agent_api_key`), per-tool scopes + `tool_permissions.is_tool_allowed`, `tool_security` hard flags, `agent_trust` safe-marking, RFC 8693 delegation (`orchestrator/delegation.py`), and the per-user hash-chained audit (`audit/repository.py::verify_chain`).

## Reconciliation (2026-10-13, by spec 095)

Issue #279 reconciled this spec against the merged GaiaKeep integration (spec 092) and Constitution v6.2.0. The evaluation record above is preserved; the following are retired or superseded, and the normative details are now bounded by [`specs/095-cresco-bridge-contract/contracts/bridge-contract.md`](../095-cresco-bridge-contract/contracts/bridge-contract.md):

- **T003a's registry target is stale**: `backend/shared/database.py` (and its `_FIRST_PARTY_PUBLIC_AGENT_IDS`) no longer exists — AstralPlane composition removed it. The current registry is `backend/orchestrator/local_agents.py::FIRST_PARTY_PUBLIC_AGENT_IDS`.
- **FR-002's absolute dependency posture is superseded** by Constitution v6.2.0 Principles V and VII: any supporting library (including `pycrescolib`) is permitted under Principle V when declared in the manifest. `pycrescolib` remains disqualified as the default client solely by its global TLS-verification-off default (research R4), which no constitution relaxes.
- **`FF_CRESCO`'s only live consumer today is the GaiaKeep boundary gate** (`FF_GAIAKEEP` AND `FF_CRESCO`), a specialized Cresco-gated archival path over a caller-owned SSH tunnel — not this general bridge. GaiaKeep's existence does not satisfy or activate this spec.
- **All implementation tasks (T001–T019) remain unchecked and are bounded** by the spec 095 contract; any future implementation MUST adopt that contract and re-verify the 2026-07-06 upstream source pins first.

## Clarifications

### Session 2026-07-06

- Q: Does this feature adopt Cresco at the platform level? → A: **No** — NO-GO on platform adoption (Constitution I + V). The integration is a first-party Python bridge agent only.
- Q: New dependencies? → A: **None.** The bridge uses the already-present `websockets` library and stdlib; `pycrescolib` is not adopted.
- Q: Is the Cresco fabric bundled/operated by AstralDeep? → A: **No** — the fabric is external infrastructure (Keycloak posture), configured via environment (`CRESCO_WSAPI_URL`, `CRESCO_SERVICE_KEY`). Absent config ⇒ the agent's tools report unavailable; no boot failure.
- Q: How is the arbitrary-shell `executor` surface handled? → A: Wrapped tools are **system-scoped, default-deny, hard security-flagged**, never enabled by the safe-agent baseline; exposure requires an explicit per-user override.
- Q: Rollout safety? → A: `FF_CRESCO` feature flag, **default off, fail-closed**; with the flag off, behavior is byte-identical to today.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Read Cresco fabric state from a chat (Priority: P1)

With `FF_CRESCO` on and a fabric configured, a user asks about the connected Cresco fabric and the bridge agent's read tools return live topology and telemetry (regions, agents, per-agent plugin/health, host sysinfo) rendered as normal SDUI.

**Why this priority**: Read-only topology/telemetry is the safe, useful core of the integration and exercises the whole path (connect, authenticate, RPC, render, audit) without the executor risk surface.

**Independent Test**: Point `CRESCO_WSAPI_URL`/`CRESCO_SERVICE_KEY` at a local single-node fabric, enable `FF_CRESCO`, ask for the region/agent list; the bridge returns the live values and an `agent_tool_call` audit row is written.

**Acceptance Scenarios**:

1. **Given** `FF_CRESCO` on and a reachable fabric, **When** the read tools run, **Then** they return live `listregions`/`listagents`/per-agent info/sysinfo over `wss://…:8282` using only `websockets` + stdlib, and the result renders as SDUI.
2. **Given** the same, **When** any tool executes, **Then** a paired start/finish `agent_tool_call` audit row is written carrying the fabric identifiers (`region_agent[_plugin]`).
3. **Given** `FF_CRESCO` on but no fabric configured (env unset), **When** a tool is invoked, **Then** it reports "Cresco fabric not configured" and the orchestrator boots and runs normally.
4. **Given** a self-signed/untrusted TLS certificate, **When** the bridge dials, **Then** the connection is refused unless the operator has configured a trusted CA or pinned fingerprint (no global verification bypass).

---

### User Story 2 - Gated write and process tools (Priority: P2)

A privileged user can, when explicitly permitted, invoke higher-risk Cresco tools (file operations; and — only behind a hard flag and explicit opt-in — the `executor` process tool), each gated by the existing permission and security machinery.

**Why this priority**: These unlock the fabric's real capability but carry real risk; they must be strictly gated, so they follow the safe read core.

**Independent Test**: With the executor tool present, confirm it is denied by default for a normal user and permitted only after an explicit per-user override; confirm a file-put/get round-trips under write scope.

**Acceptance Scenarios**:

1. **Given** the file tools at write scope, **When** a permitted user runs a file put/get, **Then** it round-trips through the fabric and is audited.
2. **Given** the `executor` wrapper tool, **When** a user without an explicit override invokes it, **Then** it is denied by the permission gate (default-deny, hard security flag), and the denial is audited.
3. **Given** the safe-agent baseline, **When** it is evaluated for the executor tool, **Then** the baseline never flips the executor tool to allow (hard flag wins).

### Edge Cases

- Fabric unreachable or wsapi down → bounded connect timeout, a clear "fabric unreachable" tool result, no orchestrator impact; the agent reconnects on the next call (stdlib backoff, no `backoff` dependency).
- `cresco_service_key` rotated/invalid → authentication failure surfaced as a tool error, never a crash; the key is a runtime-only secret (never logged, never committed).
- wsapi wire format drifts (implementation-defined, active development) → the client pins to the evaluated shape and fails safe on unexpected frames with a diagnostic, rather than mis-parsing.
- Cresco node performs its own outbound network I/O → out of AstralDeep's egress control; documented residual risk owned by the fabric operator (AstralDeep validates only its own dial-out to the wsapi host).
- `FF_CRESCO` off → the agent is not registered and no Cresco code path is reachable (kill-switch).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The integration MUST be a first-party in-process agent under `backend/agents/cresco/`, registered via the existing in-process path (`orchestrator/local_agents.py`), seeded **not** safe (owner approval required).
- **FR-002**: The bridge MUST speak the `wsapi` protocol using only the already-present `websockets` dependency and the Python standard library; it MUST NOT add any third-party dependency, and MUST NOT adopt `pycrescolib` at runtime.
- **FR-003**: Fabric connection MUST be configured by environment only (`CRESCO_WSAPI_URL`, `CRESCO_SERVICE_KEY`), both runtime-only secrets; if unset, the agent's tools MUST report unavailable and MUST NOT prevent orchestrator boot.
- **FR-004**: The feature MUST be behind `FF_CRESCO`, default **off** and fail-closed; with the flag off, system behavior MUST be byte-identical to before this feature (the agent is not registered).
- **FR-005**: Tools MUST be tiered by risk and scope — read scope for topology/telemetry (regions, agents, agent info, sysinfo, bounded log/metric reads); write scope for file operations (filerepo); **system scope for any `executor` wrapper**.
- **FR-006**: Any `executor`-wrapping tool MUST be default-deny with a hard `tool_security` flag, MUST require an explicit per-user permission override to run, and MUST never be enabled by the safe-agent baseline flip.
- **FR-007**: Before dialing, the bridge MUST validate the wsapi host via `shared/external_http.py::validate_egress_url` (with a documented private-host override for on-prem fabrics) and MUST require verified TLS (trusted CA or pinned fingerprint); it MUST NOT disable certificate verification globally.
- **FR-008**: Every tool call MUST flow through the existing `agent_tool_call` audit path and MUST record the fabric identifiers (`region_agent[_plugin]`) so that a delegation chain terminating in a fabric action is fully attributable end-to-end.
- **FR-009**: The feature MUST NOT introduce any JVM component into the product image, any message broker, or any change to AstralDeep's A2A/WebSocket agent protocol or in-process transport. Operating the Cresco fabric is external infrastructure, not part of this deliverable.
- **FR-010**: The feature MUST ship with unit + integration tests meeting the ≥90% changed-code coverage gate, and MUST be verified end-to-end against a local single-node Cresco fabric (flag-off no-op; flag-on read round-trip; executor default-deny).

### Key Entities

- **Cresco bridge agent**: the first-party in-process agent (`backend/agents/cresco/`) exposing the tiered tool set.
- **wsapi client**: the hand-rolled JSON-over-WSS client (existing `websockets` + stdlib) implementing the evaluated envelope format.
- **Fabric configuration**: `CRESCO_WSAPI_URL` + `CRESCO_SERVICE_KEY` (env, runtime-only) and the `FF_CRESCO` flag.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With `FF_CRESCO` off, the full existing test suite passes unchanged and no Cresco code path is reachable.
- **SC-002**: With `FF_CRESCO` on and no fabric configured, tools report unavailable and the orchestrator boots and serves normally.
- **SC-003**: With `FF_CRESCO` on and a local single-node fabric, the read tools return live topology/telemetry over `wss://…:8282`.
- **SC-004**: The `executor` tool is denied by default and runs only after an explicit per-user override; every attempt is audited.
- **SC-005**: `git diff` of `backend/requirements.txt` is empty — zero new runtime dependencies.
- **SC-006**: TLS verification is enforced; a self-signed certificate is rejected unless a CA/fingerprint is configured.
- **SC-007**: Every Cresco tool call writes an `agent_tool_call` audit row carrying the fabric identifiers.
- **SC-008**: Changed-code coverage ≥ 90%; end-to-end verification evidence (flag-off no-op, flag-on read round-trip, executor default-deny) recorded.

## Assumptions

- A Cresco fabric is available to integrate against — for development, a local single-node bring-up from the released JAR (JDK 21+, `-Dis_global=true -Denable_wsapi=true`); in production, an operator-run fabric reachable at `CRESCO_WSAPI_URL`.
- The evaluated `wsapi` wire format (pinned 2026-07-06) is stable enough to pin against; drift is handled by failing safe (edge cases).
- Operating, securing, and upgrading the Cresco fabric is the fabric operator's responsibility, not this feature's.

## Dependencies & Sequencing

- **Builds on**: the feature-029 plug-and-play agent pattern and the feature-040 in-process agent path; `shared/external_http.py` (egress), `tool_permissions`/`tool_security` (gates), `orchestrator/delegation.py` (RFC 8693 chains, incl. the recursive `act` chains behind `FF_RECURSIVE_DELEGATION`), and the `audit` repository.
- **Constitution**: sanctioned by the Cresco external-infrastructure clause added to the constitution (Principle VII + Technology Stack) alongside this feature.
- **Out of scope / deferred**: a Cresco-side Java bridge plugin (revisit only if the fabric must initiate toward AstralDeep); dataplane streaming beyond bounded log/telemetry reads.
