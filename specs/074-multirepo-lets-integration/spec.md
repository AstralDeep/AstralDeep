# Feature Specification: Astral Multi-Repository Decomposition and LETS Integration

**Feature Branch**: `074-multirepo-lets-integration`

**Created**: 2026-08-13

**Status**: Draft

**Input**: User description: "Keep LETS and AstralDeep independent while integrating LETS into AstralDeep; decompose AstralDeep so orchestration remains in AstralDeep, rendering and clients move to AstralProjection, the data plane moves to AstralPlane, primitives remain in AstralPrimitives, and all four independent repositories are pinned as AstralDeep submodules. Preserve legacy history, update renamed repository identities, use AstralDeep as a LETS case study, cite the published AMIA 2026 Astral paper, checkpoint and push regularly, keep the active paper local, and defer broad CI until the migration is nearly complete."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Compose the Independent Astral System (Priority: P1)

An operator or developer can obtain AstralDeep together with its four independently maintained components, verify their exact revisions, and run the composed system without relying on duplicate source trees or undocumented manual copying.

**Why this priority**: The repository split has no value unless the independently owned projects still compose into one reliable Astral system.

**Independent Test**: Starting from a new checkout, retrieve the declared component revisions, build the composed system, start it with representative configuration and data, and complete an authenticated user interaction without copying files between repositories.

**Acceptance Scenarios**:

1. **Given** a new AstralDeep checkout, **When** a developer retrieves the declared component revisions, **Then** every required component is present at one exact, reviewable revision and the composed product can be built.
2. **Given** the composed product and representative existing data, **When** the operator starts the system, **Then** existing authenticated orchestration, data access, and client flows remain available.
3. **Given** a component revision that is missing, uninitialized, or incompatible, **When** a build or startup is attempted, **Then** the process fails with a precise corrective message rather than silently using stale or duplicated code.

---

### User Story 2 - Enforce Finite Agent Authority with LETS (Priority: P1)

An Astral user can run a dynamically created or user-authored agent whose lifecycle and protected tool effects remain subject to Astral's existing identity and policy gates and additionally consume finite, lineage-bound authority issued by an independently deployed LETS system.

**Why this priority**: This is the primary functional integration requested by the owner and the real-system use case for LETS.

**Independent Test**: Enable enforcement for a test principal, provision a dynamic agent, execute an allowed tool effect, reject an exhausted or unmapped effect, revoke or expire the agent, and verify both Astral and LETS evidence without changing either project's standalone behavior.

**Acceptance Scenarios**:

1. **Given** a user and agent that pass every existing Astral gate and hold sufficient LETS authority, **When** the agent invokes a protected tool, **Then** the effect executes once and both systems record correlated, attributable evidence.
2. **Given** a request that fails an Astral identity, permission, policy, confirmation, PHI, owner-isolation, or egress gate, **When** LETS integration is enabled, **Then** the request remains denied before LETS can authorize an effect.
3. **Given** an agent with missing, expired, revoked, exhausted, or incorrectly mapped LETS authority, **When** it attempts a protected effect, **Then** the effect is denied and no alternate execution path bypasses the denial.
4. **Given** LETS is unavailable or returns an invalid, stale, replayed, or wrong-audience authorization, **When** enforcement is enabled, **Then** the protected effect fails closed with a visible, diagnosable result.
5. **Given** LETS enforcement is disabled for a population, **When** that population uses Astral, **Then** its current behavior is unchanged and no false LETS success is recorded.

---

### User Story 3 - Maintain Every Client from AstralProjection (Priority: P2)

A client developer can change shared rendering behavior or any supported client in AstralProjection, publish a reviewed component revision, and update AstralDeep to consume it without maintaining a second mutable copy of the same renderer, protocol, or client source.

**Why this priority**: Rendering and client ownership must be unambiguous or the split will create drift and release failures.

**Independent Test**: Make a representative shared presentation change in AstralProjection, exercise it through web, Windows, Android, iOS, macOS, and watchOS targets, update AstralDeep's pinned revision, and verify that all targets consume the same declared behavior.

**Acceptance Scenarios**:

1. **Given** the decomposed repositories, **When** a maintainer locates renderer, device-adaptation, protocol-manifest, or client source, **Then** AstralProjection is the only mutable owner.
2. **Given** a shared presentation or protocol change, **When** the new Projection revision is integrated, **Then** every affected client either renders the behavior consistently or reports an explicit supported degradation.
3. **Given** an already installed client using the existing AstralDeep release trust identity, **When** release ownership transitions, **Then** the client follows a documented compatibility path rather than losing update trust.

---

### User Story 4 - Maintain Durable State from AstralPlane (Priority: P2)

A backend developer can evolve Astral's durable data model and storage behavior in AstralPlane while AstralDeep retains orchestration and policy decisions, with existing user data migrating safely and rollback remaining possible.

**Why this priority**: A repository boundary through persistence code is unsafe unless ownership, transactions, migration state, and recovery are explicit.

**Independent Test**: Start from representative pre-split data, upgrade through the composed system, exercise conversation, workspace, attachment, artifact, audit, scheduler, and asynchronous-operation persistence, then perform the documented recovery path and verify no committed data is lost or misattributed.

**Acceptance Scenarios**:

1. **Given** a pre-split database with representative records, **When** the decomposed system starts, **Then** the normal migration path reaches the declared schema state without manual database edits.
2. **Given** concurrent durable operations, **When** one operation fails, **Then** its transaction rolls back without corrupting unrelated state or emitting false success.
3. **Given** a failed deployment or incompatible Plane revision, **When** the operator follows the recovery procedure, **Then** the prior compatible composition can be restored with its committed data intact.
4. **Given** a data-plane failure, **When** AstralDeep reports it, **Then** the error remains attributable to the affected operation and does not weaken authorization or owner isolation.

---

### User Story 5 - Preserve Repository and Release Provenance (Priority: P2)

A maintainer can inspect the legacy histories, renamed repository identities, migration checkpoints, current default branches, and exact cross-repository composition without relying on redirects or destructive history rewrites.

**Why this priority**: The destination repositories contain meaningful prior work, and existing clients and releases depend on verifiable repository identities.

**Independent Test**: Verify archive references at every pre-replacement tip, canonical repository URLs, `main` as the default branch, component checkpoint branches, and a trace from each extracted path to its source revision.

**Acceptance Scenarios**:

1. **Given** the pre-migration repository tips and unique branches, **When** replacement content is published, **Then** the prior histories remain reachable through immutable named archive references.
2. **Given** a local checkout or maintained document containing a pre-rename URL, **When** the migration finishes, **Then** current operational references use the canonical repository name and historical citations remain understandable.
3. **Given** a cross-repository checkpoint, **When** a maintainer audits it, **Then** each component revision and its corresponding AstralDeep composition revision are identifiable.

---

### User Story 6 - Evaluate and Report the Astral LETS Case Study (Priority: P3)

A researcher can reproduce an AstralDeep-based LETS evaluation, distinguish prior v1.0.10 evidence from new case-study evidence, and update the local paper with an accurate citation to Astral's published AMIA 2026 paper.

**Why this priority**: The integration is intended to become a defensible research use case, but product behavior and measured evidence must exist before manuscript claims change.

**Independent Test**: Reproduce the declared integration scenarios from clean component revisions, validate all recorded inputs and results, and confirm the paper attributes baseline and new evidence correctly while its anonymous form does not expose prohibited identifying material.

**Acceptance Scenarios**:

1. **Given** unchanged LETS runtime behavior, **When** the Astral case study is added, **Then** the v1.0.10 baseline remains immutable and new measurements are labeled as integration evidence rather than release evidence.
2. **Given** integration requires a LETS runtime change, **When** the change is validated, **Then** it receives a successor release and the paper no longer claims that changed result is bound to v1.0.10.
3. **Given** named and anonymous paper variants, **When** the Astral citation and case study are incorporated, **Then** the named form identifies the system accurately and the anonymous form uses neutral self-citation and preserves double-blind constraints.

### Edge Cases

- A component revision exists remotely but its repository is unavailable during checkout or build.
- An old repository redirect resolves, but embedded metadata, release verification, or updater trust still names the old identity.
- A developer updates one component without updating the declared compatible revisions of the others.
- Legacy Projection or Plane branches contain commits absent from the old default branch.
- A partially completed repository replacement leaves both legacy and extracted code present.
- Existing dynamic agents have no LETS lease when enforcement is enabled.
- A request is retried after Astral succeeds in obtaining authorization but before the final effect outcome is known.
- A receipt is validly signed but bound to the wrong user, tenant, agent, tool, operation, audience, policy, or configuration epoch.
- LETS becomes unavailable after agent provisioning but before authorization, verification, or lifecycle closure.
- Database migration succeeds while file-backed artifact or attachment migration fails, or vice versa.
- A rollback crosses a component compatibility boundary or would require an unsafe schema downgrade.
- An installed Windows client trusts only the former release workflow identity.
- A new primitive appears necessary during extraction but is not yet available from AstralPrimitives.
- Local manuscript sources or generated evidence are accidentally selected by a broad cleanup or repository replacement operation.

## Requirements *(mandatory)*

### Functional Requirements

#### Independent Repository Ownership

- **FR-001**: AstralDeep MUST remain the independent owner of orchestration, agent lifecycle coordination, identity and authorization policy, model planning, tool dispatch, and product composition.
- **FR-002**: AstralProjection MUST become the independent owner of the complete supported client products, shared rendering behavior, per-device presentation adaptation, shared UI protocol definition, client-specific protocol mirrors, presentation assets, and their verification and release processes.
- **FR-003**: AstralPlane MUST become the independent owner of database connectivity, schema state and migrations, durable repositories and stores, workspace and canvas persistence, conversation and session history, attachments and artifacts, audit persistence, scheduler and asynchronous-operation state, durable event/outbox state, and data lifecycle or recovery behavior.
- **FR-004**: AstralPlane MUST NOT own orchestration decisions, identity or authorization policy, model or agent logic, UI rendering, client behavior, A2A or MCP agent transport, remote-compute execution, or real-time voice/media processing.
- **FR-005**: AstralPrimitives MUST remain the independent owner of primitive definitions, serialization, documentation, and package releases.
- **FR-006**: LETS MUST remain an independently deployable, protocol-neutral authorization project that does not import AstralDeep internals.
- **FR-007**: AstralDeep MUST contain pinned submodule references to AstralProjection, AstralPlane, AstralPrimitives, and LETS using their canonical repository identities.
- **FR-008**: A mutable implementation file MUST have exactly one owning repository after decomposition; generated, packaged, or compatibility copies MUST be identifiable and mechanically checked against their owner.
- **FR-009**: A new checkout MUST detect and explain missing, stale, dirty, or incompatible component revisions before producing a deployable artifact.

#### Repository Preservation and Identity

- **FR-010**: The pre-replacement default tips, tags, and unique branches of AstralProjection and AstralPlane MUST remain reachable through clearly named archive references.
- **FR-011**: Repository replacement MUST preserve existing Git history rather than create orphan histories or force-rewrite published commits.
- **FR-012**: AstralProjection and AstralPlane MUST use `main` as their default branch after migration while retaining their current visibility unless the owner directs otherwise.
- **FR-013**: Current operational remotes, package metadata, documentation, submodule declarations, release policies, and repository links MUST use `AstralDeep/AstralProjection`, `AstralDeep/AstralPlane`, and `AstralDeep/AstralPrimitives` with canonical casing.
- **FR-014**: Historical citations MAY retain prior names only when they are clearly identified as historical and still resolve to the intended artifact.
- **FR-015**: Each pushed checkpoint MUST record the exact source and destination revisions for all affected repositories in kos-wiki.

#### Composition and Compatibility

- **FR-016**: AstralDeep MUST define one authoritative compatible composition of the four component revisions.
- **FR-017**: The composed system MUST preserve existing external user-facing behavior unless a change is explicitly documented and verified as part of this feature.
- **FR-018**: Inter-repository contracts MUST be versioned, validated at their producing boundary, and tested at every consuming boundary.
- **FR-019**: The UI protocol, schema compatibility, data-plane contract, and LETS integration contract MUST each have a machine-readable compatibility check.
- **FR-020**: Production MUST continue using the current monorepo-derived deployment until the decomposed composition passes the final local qualification and a separate deployment decision is made.
- **FR-021**: The migration MUST include a documented recovery procedure for every repository replacement and for the composed product.

#### LETS Enforcement

- **FR-022**: AstralDeep MUST consume the immutable LETS v1.0.10 release as the initial integration baseline.
- **FR-023**: AstralDeep MUST connect to LETS through an Astral-owned boundary using public LETS contracts; neither project may depend on the other's private implementation details.
- **FR-024**: LETS enforcement MUST initially be feature-flagged and scoped to dynamic and user-authored agents before expanding to other populations.
- **FR-025**: The integration MUST provide explicit mappings for Astral's six declared tool scopes: read, write, search, system, files, and execute.
- **FR-026**: Unknown, missing, ambiguous, or incomplete scope, capability, transition, cost, policy, or audience mappings MUST fail closed.
- **FR-027**: Astral's existing authentication, delegated identity, owner isolation, permission, policy, security-flag, taint, confirmation, PHI, egress, and audit gates MUST remain authoritative and execute before LETS can authorize a protected effect.
- **FR-028**: AstralDeep MUST maintain a durable, owner-isolated binding between each governed agent and its LETS tenant, envelope, lease, policy, configuration epoch, and lifecycle state.
- **FR-029**: Agent provision, recursive creation, renewal, quiescence, resumption, revocation, closure, and expiry MUST update the corresponding LETS lifecycle or fail without reporting a false success.
- **FR-030**: Every protected effect MUST use a durable operation identifier that remains stable across safe retries.
- **FR-031**: A LETS authorization MUST be verified at the final effect boundary for issuer, signature, tenant, envelope, lease, policy, epoch, audience, operation, sequence, nonce, time, and replay before the effect executes.
- **FR-032**: No REST, WebSocket, A2A, MCP, background, scheduled, chained, remote, first-party, external, or user-authored execution path may bypass enforcement for an enabled population.
- **FR-033**: When enforcement is enabled, LETS unavailability, timeout, malformed responses, trust failure, stale state, or replay MUST deny the protected effect and produce diagnosable user and operator evidence.
- **FR-034**: When enforcement is disabled for a population, current Astral behavior MUST remain unchanged and the system MUST NOT fabricate LETS authorization evidence.
- **FR-035**: Rollout MUST progress from dynamic and user-authored agents to broader populations only after golden, denial, failure, concurrency, retry, and recovery evidence passes for the prior stage.
- **FR-036**: A required LETS runtime change MUST produce a successor release; the immutable v1.0.10 tag and its published evidence MUST NOT be altered or relabeled.

#### Data Plane

- **FR-037**: AstralPlane MUST expose explicit contracts for transactional data access rather than allowing AstralDeep modules to depend on private storage internals.
- **FR-038**: Schema evolution MUST remain guarded, repeat-safe, attributable to one declared revision, and exercised against representative existing data.
- **FR-039**: Database state and file-backed attachment, artifact, workspace, and generated-data state MUST migrate or recover as one documented operational unit.
- **FR-040**: Data-plane contracts MUST preserve owner isolation, audit attribution, transaction boundaries, idempotency, ordering, and failure visibility.
- **FR-041**: The split MUST not copy production data, credentials, user uploads, generated user code, manuscript sources, or generated evidence into any repository.
- **FR-042**: A data-plane revision incompatible with the current AstralDeep composition MUST be rejected before normal traffic is admitted.

#### Projection and Release Continuity

- **FR-043**: AstralProjection MUST provide all artifacts and contracts required to render the server-owned experience across web, Windows, Android, iOS, macOS, and watchOS.
- **FR-044**: A shared UI behavior change MUST be verified across every affected client before its Projection revision is declared compatible.
- **FR-045**: Existing unknown-component, accessibility, sanitization, theme, layout, and graceful-degradation behavior MUST remain intact after extraction.
- **FR-046**: Existing installed clients MUST retain a valid update path during repository and workflow identity transition.
- **FR-047**: Release identity migration MUST be staged and documented; an updater or verifier MUST never silently trust a newly named repository or workflow without an authenticated compatibility transition.
- **FR-048**: Client build, signing, distribution, and release evidence MUST remain attributable to an exact Projection revision and its compatible AstralDeep composition.

#### Primitives

- **FR-049**: The split MUST first use existing primitives and MUST add or modify a primitive only when a verified product requirement cannot be represented by the current vocabulary.
- **FR-050**: Any required primitive change MUST be implemented, documented, tested, released, and consumed from AstralPrimitives before dependent component revisions claim compatibility.

#### Research and Documentation

- **FR-051**: The active LETS manuscript and its generated submission artifacts MUST remain local and excluded from public repository history until the owner changes that decision.
- **FR-052**: Repository cleanup, replacement, and migration tooling MUST preserve the ignored local manuscript and evidence trees.
- **FR-053**: The named LETS paper MUST cite the published AMIA 2026 Astral paper accurately and distinguish the paper's historical design from the current system used in the case study.
- **FR-054**: Anonymous paper text MUST use neutral self-citation and omit identities or public fingerprints prohibited by the target venue's double-blind rules.
- **FR-055**: New paper results MUST be reproduced from exact component revisions and retained evidence, with baseline v1.0.10 evidence clearly separated from integration-specific measurements.
- **FR-056**: Every incidental bug fix, architectural decision, repository checkpoint, result change, and unresolved risk discovered during this feature MUST be captured in curated kos-wiki pages and pushed before the corresponding checkpoint is declared complete.

#### Verification and Delivery

- **FR-057**: Narrow checks MUST run throughout migration, while broad cross-repository and client qualification MUST run locally near completion before relying on hosted CI.
- **FR-058**: Final local qualification MUST cover component contract checks, representative data migration and recovery, authenticated orchestration, LETS authorization and denial paths, every affected client, release/updater compatibility, and composed clean-checkout build and startup.
- **FR-059**: Checkpoint work MUST be pushed to named non-default branches; draft pull requests MAY be opened for review, but no component default branch may receive replacement content before its compatibility checkpoint is reviewable.
- **FR-060**: No production deployment, store submission, public paper submission, or public release is authorized by this feature alone.
- **FR-061**: The existing project constitutions other than kos-wiki are non-binding for this owner-authorized migration; kos-wiki provenance, sensitivity, capture, commit, and push rules remain mandatory.

### Key Entities

- **Component Revision**: An immutable revision of AstralProjection, AstralPlane, AstralPrimitives, or LETS, including its canonical repository identity and compatibility metadata.
- **System Composition**: The exact set of component revisions consumed by one AstralDeep revision, together with their contract versions and verification status.
- **Archive Reference**: A durable named reference preserving a repository's pre-replacement default tip, tags, or unique historical branch.
- **Agent Authority Binding**: The owner-isolated association between an Astral agent and its LETS tenant, envelope, lease, policy, configuration epoch, lifecycle state, and latest confirmed operation.
- **Protected Effect Authorization**: A short-lived, audience- and operation-bound LETS result that must be independently verified at the final Astral effect boundary.
- **Data Plane Revision**: The declared schema, durable-store contract, file-state expectations, and recovery compatibility owned by AstralPlane.
- **Release Trust Transition**: Evidence and compatibility state allowing existing installed clients to move from AstralDeep-owned release identities to AstralProjection-owned identities without silent trust expansion.
- **Migration Checkpoint**: A pushed, cross-referenced set of repository revisions, verification results, wiki records, and unresolved risks.
- **Case-Study Evidence Bundle**: Reproducible Astral/LETS integration inputs and measurements bound to exact component revisions and clearly separated from LETS v1.0.10 release evidence.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A developer starting from a new checkout can retrieve the four exact component revisions and produce a runnable Astral system without copying source files or consulting undocumented steps.
- **SC-002**: One hundred percent of migrated renderer, client, primitive, and data-plane implementation files have one declared mutable owner, with zero unmanaged duplicate source trees in AstralDeep.
- **SC-003**: All representative pre-split user records, workspaces, conversations, attachments, artifacts, audit records, scheduler state, and asynchronous-operation state remain present, owner-isolated, and usable after upgrade and documented recovery testing.
- **SC-004**: Every supported client completes the same authenticated golden flow and renders the same shared protocol fixture from the decomposed composition with no unexplained capability, theme, layout, accessibility, or sanitization drift.
- **SC-005**: One hundred percent of enabled protected effect paths pass enforcement tests proving valid authorization, exhaustion, missing mapping, revocation, expiry, wrong audience, stale state, replay, timeout, unavailable-service, retry, and recovery behavior.
- **SC-006**: Existing Astral authorization denials remain denials in every LETS posture, and no test or live verification finds a path where LETS widens an Astral permission.
- **SC-007**: The pre-migration default tips and every unique legacy branch remain reachable through documented archive references, while all current operational repository links resolve directly to canonical names without redirect dependence.
- **SC-008**: Existing installed client update verification continues to succeed through a documented trust transition, with zero unauthenticated repository or workflow identity substitutions.
- **SC-009**: Final local qualification completes with no failing required component, migration, security, client, compatibility, clean-build, or startup check before any hosted-CI or default-branch integration request.
- **SC-010**: Each major checkpoint has a pushed kos-wiki record naming exact revisions, behavior changes, verification performed, paper impact, and unresolved risk.
- **SC-011**: The updated named manuscript cites the AMIA Astral paper, distinguishes historical and current Astral architecture, and reports only results reproducible from retained exact-revision evidence; the anonymous build contains none of the prohibited identifying tokens or public fingerprints.
- **SC-012**: Production remains on its currently deployed monorepo-derived artifact throughout migration, with no user-visible outage caused by development or repository restructuring.

## Assumptions

- No other developer or machine is currently performing overlapping work in the five repositories.
- Existing repository histories will be preserved with archive references; orphan-history replacement and destructive force-pushes are out of scope.
- AstralProjection and AstralPlane will retain their current private visibility and move their default branch from `master` to `main`.
- Canonical repository identities are `AstralDeep/AstralDeep`, `AstralDeep/AstralProjection`, `AstralDeep/AstralPlane`, `AstralDeep/AstralPrimitives`, and `AstralDeep/LETS`.
- The current production deployment remains unchanged until a later, separately authorized cutover.
- The initial LETS consumer baseline is the immutable v1.0.10 release; current rewritten LETS `main` is not treated as the release anchor.
- LETS runs as an independent service boundary; its submodule provides exact source, contracts, tests, and reproducibility rather than collapsing it into AstralDeep internals.
- AstralPlane is defined as the durable persistence and storage plane, including transactional repositories and durable event/outbox state, but excluding orchestration, agent transport, remote execution, and real-time media processing.
- Whole client products move to AstralProjection because their rendering, transport, authentication, release, and platform build concerns cannot be safely split into separate repositories.
- Existing primitives are expected to be sufficient for the migration itself; any contrary finding will trigger the explicit AstralPrimitives release path.
- The active LETS manuscript remains local and ignored until the owner authorizes publication or a different preservation location.
- Broad hosted CI is intentionally deferred until near completion; narrow local verification remains expected throughout.
- The owner has explicitly waived all project constitutions for this task except the kos-wiki operating rules and has authorized incidental fixes when they are recorded in the wiki.
