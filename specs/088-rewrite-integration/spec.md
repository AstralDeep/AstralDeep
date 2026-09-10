# Feature Specification: Astral Rewrite Integration

**Feature Branch**: `codex/088-rewrite-integration`

**Created**: 2026-09-10

**Status**: Draft — implementation authorized; qualification remains required

**Input**: Integrate everything implemented in the separate Astral rewrite into the AstralDeep ecosystem, including the reviewed UI improvements and defect corrections, using the existing Keycloak at `iam.ai.uky.edu`.

## Scope and source authority

This feature adopts the rewrite's user capabilities and useful runtime/contract patterns into the existing Astral ecosystem. It preserves existing capabilities, user records, identity, governed execution, and component ownership. It is not a deployment of a second application or a migration of standalone rewrite data.

The source baselines are AstralDeep `2de5d867413ce2aec5b7448dca8eeaa42674a7e3`, its pinned components, rewrite `daeea32b6e99b9f7cf0ff32abe372731744c639e`, and the seven draft specifications 081–087 at `51f6ee22703fe11e5feb99fe26a9f556825219f9`. The older specification branches are preserved as source history. Their requirements are reconciled explicitly; their existence or donor test results are not evidence of integrated completion.

“Everything” includes operations and recovery, research/results, monitoring, declarative agents, skills, explicit encrypted memory notes, settings/grants/framework credentials, interoperability, shared UI behavior, and observability. Existing Astral capabilities remain accessible. A capability already supplied by Astral may satisfy the adoption through its existing implementation when observable behavior and security requirements are verified. A donor limitation must not become a regression in an existing capability.

## Clarifications

### Session 2026-09-10

- Q: Must an ordinary chat/public-research request receive a separate preflight confirmation? → A: No. Send starts ordinary requested work immediately; consequential actions, saving/publishing results, and new permissions retain approval and all existing limits/security checks.
- Q: Must records from the standalone rewrite installation be imported? → A: No. Integrate all features, preserve existing AstralDeep data, and leave rewrite data untouched.
- Q: Must all redesigned native clients ship with the first integrated release? → A: No. Deliver the redesigned web experience first; native redesign follows. All clients remain in the overall scope.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Start useful work without learning the system (Priority: P1)

A signed-in user sees one primary composer, recent work and a clear place for results. They describe a task and select Send. Ordinary conversation or public reading starts without another assignment-review dialog. Advanced agent, skill, memory, permission and execution controls remain available when relevant.

**Why this priority**: The owner identified visual/text overload as the principal usability problem. The experience should “just work.”

**Independent Test**: A fresh-account browser journey starts conversation and public-source research without opening settings or reading execution terminology.

**Acceptance Scenarios**:

1. **Given** an authenticated user with an available configured provider, **When** the user submits a normal prompt, **Then** one logical task starts from that submission and shows a concise progress state.
2. **Given** a task with a useful result, **When** the result arrives, **Then** its content and sources take precedence over activity, usage, limits and implementation metadata.
3. **Given** a consequential proposed effect or publication, **When** review is required, **Then** the user sees the exact effect/result, destination and material scope before approving; Send has not approved that effect.
4. **Given** advanced capabilities, **When** a user opens the relevant disclosure or settings destination, **Then** the capability remains reachable without adding unrelated controls to the default composer.

### User Story 2 - Keep existing identity, work and capabilities (Priority: P1)

An existing Astral user signs in through the established institutional identity provider and retains their account, conversations, files, tools, providers, agents and permissions.

**Why this priority**: Adoption must improve the existing ecosystem rather than replace it with the rewrite's narrower capability set.

**Independent Test**: Representative existing accounts and populated data complete the same authorized workflows before and after the additive upgrade, including denials.

**Acceptance Scenarios**:

1. **Given** an existing Astral account, **When** the integrated experience is used, **Then** ownership remains bound to the same verified identity from realm `Astral` at `iam.ai.uky.edu`.
2. **Given** existing conversations, attachments, skills, memory, provider choices and schedules, **When** the upgrade runs, **Then** those records remain owned and usable without resetting permissions or budgets.
3. **Given** separate user and system provider configuration, **When** each workload runs, **Then** the existing credential boundary remains enforced and provider breadth is retained.
4. **Given** the standalone rewrite installation, **When** integration or qualification runs, **Then** its users, records and services remain unchanged.

### User Story 3 - Rely on tasks across interruptions (Priority: P1)

A user can inspect, cancel, pause, resume and review durable work across navigation, disconnects and restarts. Scheduling and recovery remain fair, bounded and truthful about uncertain outcomes.

**Why this priority**: Reliable execution and current authority are foundational to the adopted workflows.

**Independent Test**: Controlled interruption scenarios exercise admission, recovery, effect review and publication through the normal authenticated dispatch path.

**Acceptance Scenarios**:

1. **Given** an accepted task, **When** the client retries or reconnects, **Then** the logical task and consumed budget are preserved without unintended duplicate effects.
2. **Given** blocked schedules belonging to one owner, **When** another owner's schedule is due, **Then** bounded scheduling continues considering eligible work from other owners.
3. **Given** research that does not retain source text, **When** generation temporarily fails and is retried, **Then** required evidence is fetched again within the remaining budget rather than falsely reported unavailable.
4. **Given** a changed or revoked grant, selected revision, credential or approval, **When** another dispatch or publication is attempted, **Then** current authority is checked and stale work cannot continue with previous permission.

### User Story 4 - Manage agents, reusable guidance and private notes (Priority: P2)

A user creates and revises declarative agents, manages skills and explicitly selects private notes where appropriate. Changes and expiry take effect predictably without removing existing skill and memory features.

**Why this priority**: Reusable behavior and personal context are central donor features and must share the existing ownership controls.

**Independent Test**: Two owners exercise create/revise/select/disable/expire/forget histories and interrupted tasks, including cross-owner and stale-reference denial.

**Acceptance Scenarios**:

1. **Given** a selected agent or skill revision, **When** its definition changes or is disabled, **Then** stale unfinished work is refused at the required current-state boundary and revision history remains available.
2. **Given** an encrypted expiring note, **When** it is corrected, expires or is forgotten, **Then** its current value and usability change under the documented retention policy, and unfinished references cannot silently use the prior value.
3. **Given** existing procedural skills and personalization tools, **When** the new guidance UI is adopted, **Then** existing authorized behavior remains available alongside explicit selection.

### User Story 5 - Monitor sources and keep reviewed results (Priority: P2)

A user schedules bounded observations, distinguishes an initial observation from a comparison, and saves a reviewed result without authorizing unrelated future publication.

**Why this priority**: Monitoring and reviewed results are complete user workflows that depend on the preceding authority and lifecycle foundations.

**Independent Test**: A real public-source monitoring journey produces an initial grounded observation, detects an unchanged source without unnecessary generation, and stops further work after Stop.

**Acceptance Scenarios**:

1. **Given** no previous observation, **When** monitoring starts, **Then** the result does not invent historical comparison or stability.
2. **Given** unchanged complete source observations, **When** the next observation runs, **Then** the system may avoid generation and truthfully records the outcome.
3. **Given** an exact reviewed proposal, **When** the owner approves it, **Then** only that result is saved once; expired or superseded proposals cannot be approved.

### User Story 6 - Use external frameworks and understand outcomes (Priority: P2)

An authorized external client can submit and observe work through supported integrations. Owners can inspect their task outcomes and usage; administrators can inspect bounded operational diagnostics.

**Why this priority**: External access and outcome visibility must share the established security and execution behavior rather than add competing paths.

**Independent Test**: Supported framework adapters enter the same authority/lifecycle boundary and are tested for allowed operations, denials, revocation and retries.

**Acceptance Scenarios**:

1. **Given** a scoped framework credential, **When** an adapter submits or observes a task, **Then** its permissions remain attenuated and it cannot approve effects or gain settings authority by changing transport.
2. **Given** credential revocation before issuance commits, **When** new framework credentials are requested, **Then** no valid credential is issued from stale authority.
3. **Given** missing or uncertain usage information, **When** outcomes are displayed, **Then** missing information is distinguished from zero and no false completion or cost is reported.

### User Story 7 - Continue using native clients during the web-first rollout (Priority: P2)

Existing native clients retain compatible access during the first web release. Their redesigned experience follows as a distinct qualification milestone without being represented as completed by web evidence.

**Why this priority**: The explicitly phased rollout must remain usable for existing clients and keep the remaining redesign scope visible.

**Independent Test**: Existing supported native versions negotiate compatible behavior during the web milestone; each redesigned native client later completes the same core journeys in its form factor.

**Acceptance Scenarios**:

1. **Given** an existing supported native client, **When** the web milestone is installed, **Then** authentication and established workflows continue, or an explicit server-declared version/handoff disposition explains an unsupported new interaction.
2. **Given** a redesigned native client, **When** qualified for its milestone, **Then** shared capabilities, meaning, theme roles and authorization match the integrated server contract.

### Edge Cases

- An unsent draft exists when an owner signs out, another owner signs in, or a session temporarily fails to refresh.
- Public configuration and identity responses complete in either order; successful public configuration must not become a misleading failure.
- A credential expires or is revoked while issuance waits for an owner lock.
- Multiple oldest schedules are held, stopped, revoked or waiting for reconciliation; other owners still receive service.
- Source content is incomplete, changed, malicious, redirected, unavailable or discarded under retention policy before a retry.
- A timeout leaves approval/publication uncertain; a retry cannot become a second approval or effect.
- Guidance changes during review, dispatch, lease heartbeat, note expiry or account switch.
- A custom model provider lacks a qualified accounting bound, while existing provider integrations remain available under their existing policy.
- An older client receives an unknown component, action, revision or lifecycle state.
- Upgrading representative populated data fails or repeats; rollback/recovery preserves existing work and audit provenance.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST preserve existing Astral identity, ownership, records, providers, specialist tools, attachments, voice and remote-execution capabilities while adopting donor features.
- **FR-002**: Users MUST authenticate through the existing Keycloak realm `Astral` at `https://iam.ai.uky.edu/realms/Astral`; existing authorized client registrations and verified subject identities MUST remain compatible.
- **FR-003**: The normal experience MUST present one primary task composer, recent work and a result area; technical execution details MUST be available through progressive disclosure rather than mandatory introductory text.
- **FR-004**: Ordinary explicitly requested chat and public research MUST begin on Send without a separate assignment-preflight confirmation. Applicable scopes, permissions, PHI, egress and budget checks MUST still run.
- **FR-005**: Consequential effects, result saving/publication and new permissions MUST retain exact, current, explicit approval. A normal submission MUST NOT imply those approvals.
- **FR-006**: The system MUST preserve logical task identity, current lifecycle state, event history, attempts, checkpoints, reservations and accounting through retry, disconnect and recovery.
- **FR-007**: Authorized users MUST have coherent task cancel, pause/resume, wait/wake, reconciliation, approve/reject and delete controls with current-state validation and duplicate-safe outcomes.
- **FR-008**: The scheduler MUST consider eligible work fairly across owners when older schedules are held, and MUST preserve existing interval, cron and one-shot functionality.
- **FR-009**: Recovery MUST reacquire ephemeral evidence needed for research when retention policy prohibits durable source text, within remaining limits.
- **FR-010**: Credential issuance and later execution MUST revalidate current authority at the committing/dispatch boundary, including revocation or expiry during contention.
- **FR-011**: Browser drafts MUST be scoped to a verified owner and erased on explicit logout or verified owner change; temporary same-owner connection recovery MUST NOT expose them to another owner.
- **FR-012**: Bootstrap MUST correctly load public configuration regardless of identity-response ordering and give actionable, truthful failure messages.
- **FR-013**: Research MUST retain source attribution, extraction-completeness and observation-scope information and MUST NOT claim unsupported conclusions merely because a citation exists.
- **FR-014**: Scheduled monitoring MUST distinguish initial observations, unchanged complete observations, meaningful changes and insufficient evidence, with bounded grant/run allowances and effective Stop behavior.
- **FR-015**: Declarative agents MUST support authoring, revision, activation, archival, cloning, history and bounded capability/trigger policy while preserving existing bundled and user-hosted agents.
- **FR-016**: Skills MUST support owner libraries, applicability, aliases, revision history, enable/disable/delete and exact selection binding while retaining existing skill packs and recipes.
- **FR-017**: Private notes MUST support encrypted current values, supported categories, expiry, correction, enable/disable, search, explicit selection and Forget, with current-value invalidation of unfinished references.
- **FR-018**: Guidance composition MUST have bounded, deterministic expansion and current-revision bindings. Guidance MUST NOT itself grant authority or count as verified research evidence.
- **FR-019**: Provider configuration MUST remain encrypted/redacted and preserve existing user/system credential separation and working provider breadth. Donor local-model support and accounting MUST be adopted as compatible additions, not a global single-model restriction.
- **FR-020**: The system MUST support explicit bounded unattended grants and revocable scoped framework credentials through the existing authoritative security boundary.
- **FR-021**: Supported REST, MCP, A2A, synchronous/asynchronous SDK and framework adapter workflows MUST share the same admission, owner, lifecycle and effect authorization semantics.
- **FR-022**: Owners MUST have task outcome/timing/usage views; administrators MUST have bounded privacy-preserving diagnostics. Unknown cost, incomplete measurement and uncertain effects MUST remain explicit.
- **FR-023**: Shared UI content, form semantics and actions MUST be validated, versioned and rendered through the ecosystem's authoritative components. Unsafe/unknown content MUST have a defined refusal or handoff.
- **FR-024**: The first integrated release MAY introduce the redesigned web experience before native redesign, as explicitly authorized on 2026-09-10. Existing native access MUST remain compatible, new unsupported interactions MUST receive server-owned dispositions, and native redesign/qualification MUST remain open tracked work.
- **FR-025**: Every adopted donor capability and every retained old capability in the acceptance inventory MUST have a traceable disposition and verification evidence; no donor limitation, source file or passing fixture test may substitute for integrated functionality.
- **FR-026**: Upgrade and recovery MUST preserve representative existing data, ownership, task fences and audit provenance. Standalone rewrite data and services MUST remain untouched and MUST NOT be imported.
- **FR-027**: Readiness, backup/restore, audit integrity, contract generation and operational qualification MUST cover the integrated application. Deployment/release authority remains subject to the existing release workflow.
- **FR-028**: UI MUST support keyboard operation, visible focus, screen-reader semantics, reduced motion, narrow layouts and 200% text without hiding required controls or losing drafts/current task state.

### Key Entities *(include if feature involves data)*

- **Owner**: Existing verified institutional identity; defines all record and permission boundaries.
- **Task and attempt**: Logical accepted intent and individual physical execution tries; distinguish requested authority, current authority, lifecycle, limits and outcomes.
- **Proposal and decision**: Exact effect/result and its current owner decision, destination, expiry and consumption status.
- **Agent revision and skill revision**: Immutable behavior/guidance definitions with current availability and applicability.
- **Private note revision**: Encrypted current owner statement with category, expiry and explicit selection references; distinct from verified evidence.
- **Grant and framework credential**: Bounded permission/continuation references that do not replace downstream authorization.
- **Schedule and observation**: Recurrence and bounded episode history with source completeness and previous-observation bindings.
- **Saved result**: Owner-approved artifact with source and task provenance.
- **Client contract/disposition**: Shared presentation and interaction semantics, negotiated support and explicit handoffs.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In the prespecified new-user evaluation, at least four of five participants can submit a normal task within 60 seconds without opening settings or receiving coaching; human evidence is recorded separately from automated tests.
- **SC-002**: Automated ordinary-chat and public-research journeys require exactly one submission action after valid input, with zero mandatory preflight dialogs; effect/publication approval remains separately verified.
- **SC-003**: All five reproduced review defects have regression coverage exercising their original failure conditions, plus integrated verification of the corresponding user/security boundary.
- **SC-004**: Every capability in the frozen donor inventory and retained existing-capability inventory has a passing integrated acceptance result or an explicit still-open milestone; the full feature cannot be declared complete while any required disposition remains open.
- **SC-005**: A representative populated-data upgrade, repeat upgrade and recovery exercise preserves expected owner associations and record counts with no unintended task resumption, renewed authority or duplicate effect.
- **SC-006**: Two-owner denial, credential/grant revocation, stale guidance, duplicate submission, cancellation, restart recovery and uncertain-effect scenarios pass through real authenticated dispatch.
- **SC-007**: The web milestone works at 320 CSS pixels and 200% text with all required controls operable and no cross-account draft exposure. Every affected supported native contract passes compatibility checks; redesigned native clients require their own later live evidence.
- **SC-008**: A real monitoring journey verifies grounded initial observation, unchanged-source generation avoidance, exact owner-approved result and no new work after Stop.

## Assumptions

- Existing Astral component ownership and production security/release rules apply. The standalone rewrite's architecture waiver does not authorize a competing production identity or storage system inside AstralDeep.
- The owner explicitly authorizes the web-first rollout above; it is recorded as a bounded scope decision, not a claim of native redesign parity or a generic exemption from security/contract verification.
- Existing configured providers are reused. Local inference is an optional additional capability; no external model bill, download or infrastructure replacement is implied by the default experience.
- No standalone rewrite user data, credentials, sessions, schedules, approvals or runtime state are imported or reset.
- Existing real institutional identities and configured endpoints are discovered safely; administrative IAM changes, product pushes and live deployment are not implied by local implementation authorization.
- If an added runtime dependency, new primitive or external administrative change proves necessary, a concrete proposal and evidence will precede the specific approval required by existing policy.
