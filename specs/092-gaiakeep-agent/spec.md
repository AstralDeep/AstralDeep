# Feature Specification: GaiaKeep agent

**Feature Branch**: `codex/092-gaiakeep-production` (continuation of merged `codex/092-gaiakeep-agent`)
**Created**: 2026-10-02
**Status**: Production continuation in progress; checks and exact-candidate staging/client qualification are recorded separately in verification.md. Legacy fetch remains unsupported.
**Input**: Create an AstralDeep agent for all public GaiaKeep actions through the existing sandbox-to-DGX SSH tunnel. Do not provision Cresco or create a GaiaKeep instance. The owner now reports the enrolled DGX client is available and requests checks, a production-ready PR, qualified merge and live sandbox update.

## Clarifications

### Session 2026-10-02

- Q: Is infrastructure provisioning required? → A: Forget Cresco setup; use the existing SSH listener on sandbox port 40000.
- Q: May the agent create its own GaiaKeep instance? → A: No; discover and connect only to the existing deployment.
- Q: Should agent development wait for the interface? → A: Focus on the Astral agent now; the owner will report when the interface is up.

### Session 2026-10-03

- The owner supplied Cody's existing client kit/account instructions and authorized checks, PR, merge and sandbox deployment. SSH credentials must suffice; the enrolled profile and private signing key remain on DGX. The previous staging waiver covered only the disabled initial merge.
- Use a fixed first-party adapter over owner-scoped pinned SSH, an exact remotely installed SDK artifact and operator-supplied certificate/core pins. Do not forward upstream MCP tools' arbitrary host-file paths.
- Astral must automatically update an operator-readable issue log when the Gaia tool handler reports a failure or its governed outer dispatch ends without a confirmed result. A timed-out or cancelled write remains unconfirmed; the worker may still finish. Manual qualification notes alone do not satisfy this requirement.

## User Scenarios & Testing

### User Story 1 - Operate GaiaKeep from Astral (Priority: P1)

An authorized user asks Astral to inspect and manage their GaiaKeep collections, versions, storage, jobs, retention, and permissions. Astral exposes the public operations, reports native refusals honestly, and keeps privileged and destructive work behind the normal permission and confirmation controls.

**Independent Test**: Execute each advertised operation against a deterministic protocol fixture, including failures and denied permissions; prove that internal operations cannot be requested.

**Acceptance Scenarios**:

1. **Given** an available configured deployment and an authorized Gaia identity, **When** a supported action runs, **Then** its validated arguments reach that deployment and its bounded result appears in Astral.
2. **Given** insufficient permission or an unconfirmed mutation, **When** an operation is requested, **Then** nothing is dispatched to GaiaKeep.
3. **Given** a request whose effect becomes uncertain after disconnect, **When** Astral reports the outcome, **Then** it says the effect is unconfirmed and does not replay the mutation.

### User Story 2 - Connect with isolated credentials (Priority: P1)

Users register their DGX SSH connection in Astral; the enrolled Gaia profile and signing key stay in that account. Connections verify the host, gateway, and core identities and use only the caller's machine and local enrolled identity.

**Independent Test**: Attempt cross-owner access, changed host keys, invalid gateway certificates, wrong core identity, forged caller identity, and missing credentials; every attempt fails before an unauthorized effect.

**Acceptance Scenarios**:

1. **Given** a caller-owned DGX machine and Gaia identity, **When** a connection opens, **Then** it uses the pinned machine through the existing tunnel and authenticates Gaia requests separately.
2. **Given** missing endpoint or credentials, **When** an operation is requested, **Then** Astral explains what is missing without creating infrastructure or pretending it succeeded.
3. **Given** disabled integration flags, **When** Astral starts, **Then** no Gaia tools or connection are registered.

### User Story 3 - Transfer bounded files (Priority: P2)

An authorized user publishes a bounded file to a collection or retrieves a bounded file from a version. Bytes pass through the authenticated data channel and temporary content is cleaned up.

**Independent Test**: Round-trip bytes through a deterministic transfer fixture and reject excessive, malformed, or unauthenticated content.

**Acceptance Scenarios**:

1. **Given** a permitted file publication, **When** the user approves it, **Then** authenticated bytes are ingested and the result names the actual committed version.
2. **Given** a permitted file read, **When** Gaia returns authenticated bytes within the bound, **Then** Astral returns the exact bounded content and integrity information.

### Edge Cases

- The deployment is not yet running, exposes a different public catalog, or uses an older protocol.
- A service key or identity is revoked, a key is changed, or a tenant denies a signed operation.
- A leader redirect names an unapproved core peer.
- A reply contains credentials, malformed compressed content, excessive bytes, terminal control characters, or unsupported fields.
- An action mixes read and mutation modes; classify conservatively as a mutation.
- The upstream legal-order `action` parameter collides with the routing field; fail explicitly for unsupported modes rather than silently choosing another effect.
- A job is accepted but remains unfinished; return its actual identifier and state.

## Requirements

### Functional Requirements

- **FR-001**: Expose the complete pinned public GaiaKeep capability catalog with validated named control tools and complete bounded SDK file workflows; do not expose unadvertised internal node or consensus verbs. Explicitly refuse unsupported upstream/incomplete transfer modes.
- **FR-002**: Send core operations as the configured Gaia principal with a fresh signed request; prohibit model-supplied signatures, audiences, transport addresses, or caller identity overrides.
- **FR-003**: Preserve Astral's authenticated dispatch, delegated scopes, per-tool permission, policy, PHI, taint, and hash-chained audit controls.
- **FR-004**: Require durable, single-use, caller- and exact-argument-bound human approval for every mutation; unattended mutations fail closed.
- **FR-005**: Keep the integration disabled by default and require its external-fabric boundary flag as well as its own feature flag.
- **FR-006**: Resolve SSH credentials only from a caller-owned registered machine with an existing host-key pin; refuse changed or untrusted host identity and blocked egress.
- **FR-007**: Use only the existing tunnel and operator-configured gateway/core peers; verify gateway TLS on both control and data channels and pin the core identity out of band.
- **FR-008**: Default SSH mode loads the private Gaia profile/key only inside the caller's DGX account and keeps them there. Native compatibility mode decrypts user Gaia credentials only within the agent boundary. Never expose secret material in results, logs, errors, process arguments, or specifications.
- **FR-009**: Enforce bounded connection, request, response, decompression, and file-transfer resources; close sockets and temporary files when the bounded worker finishes. An earlier caller timeout or cancellation must not claim that the remote operation was cancelled.
- **FR-010**: Report native denials, deployment unavailability, unsupported upstream operations, and uncertain effects distinctly; do not fabricate outcomes or automatically retry mutations.
- **FR-011**: Support bounded authenticated file publication/retrieval in addition to native control operations, without accepting arbitrary local filesystem paths.
- **FR-012**: Deliver results using approved server-owned components shared by all clients; introduce no new primitive, protocol frame, database schema, or client-specific interface.
- **FR-013**: Keep private upstream implementation code outside the product repository and declare/pin any optional client dependency reproducibly.
- **FR-014**: Qualify changed code with at least 90% coverage, meaningful negative/integration tests, and lint. Record staging and live-client verification as pending until the owner's deployment is available.
- **FR-015**: Automatically append bounded, redacted Markdown operational diagnostics for Gaia tool failures, with timestamp, closed tool/verdict and dispatch/mutation state only. Preserve the primary result and existing authorization/audit path if the configured logging sink fails; report that sink failure through normal logs. Do not log credentials, identities, arguments, file content, paths or raw exception messages.

### Key Entities

- **Connection**: A caller-owned registered SSH machine, pinned host identity, and operator-configured gateway and allowed core peers.
- **Gaia identity**: A per-user principal and private signing key, separate from DGX SSH credentials; Gaia owns its bindings and roles.
- **Operation**: One pinned public capability, parameter schema, risk classification, native result, and possible native job identifier.
- **Approval**: Existing Astral durable proposal tied to the caller, operation, exact arguments, expiry, and one consumption.
- **File transfer**: Bounded input/output bytes and ephemeral temporary content; Gaia owns durable collection data.

## Success Criteria

### Measurable Outcomes

- **SC-001**: Every pinned public operation has an explicit tool/workflow disposition and all unadvertised operations are rejected. Four encrypted transfer primitives belong to complete SDK file workflows; legacy fetch and legal-order shortening have explicit unsupported dispositions until qualified.
- **SC-002**: Every tested unauthorized, cross-owner, untrusted-identity, and unconfirmed mutation attempt performs zero remote actions.
- **SC-003**: Bounded file round-trips preserve exact bytes; malformed and excessive content is refused.
- **SC-004**: Missing deployment configuration produces an actionable unavailable result, with no new service created.
- **SC-005**: Tests cover at least 90% of changed executable code; actual deployment verification remains explicitly pending until available.

## Assumptions

- The owner supplies the existing Gaia deployment address, trusted identities, and enrolled user credentials when ready.
- Astral's remote-machine credential surface supplies SSH registration. Default SSH mode uses the account's qualified Gaia installation without backend signing credentials; native compatibility retains the encrypted agent credential surface.
- Privileged public capabilities remain available to authorized users; their availability does not imply permission to run them.
- Protocol defects in the pinned upstream release are surfaced and recorded, rather than repaired by deploying an independent Gaia instance.
