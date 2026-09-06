# Feature Specification: Persistent Agents

**Feature Branch**: `codex/079-persistent-agents`

**Created**: 2026-09-05

**Status**: Clarified; implementation planning in progress

**Input**: Implement persistent agents that follow ongoing user instructions, remember their work, respond to relevant external events, wait when idle, decompose and delegate work, recover without repeating completed actions, respect permissions and resource limits, request approval for sensitive changes, report activity, and support revision, pause, and stop. Examples include inbox monitoring and investigating newly reported bugs.

## Clarifications

### Session 2026-09-05

- Q: Which initial event sources should the feature exercise? → A: Initially existing connected inbox and bug-tracker tools with scheduled checks; superseded below for the live example.
- Q: May agents operate while the user is offline? → A: Yes, under revocable grants with explicit tool and spending limits, and approval for sensitive actions.
- Q: Which live sources are available? → A: Use a different example requiring no extra credentials or configuration. The initial live example is a public release-page change monitor through the bundled web-research agent. Inbox/bug monitoring remains a supported assignment pattern through registered tools when available.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Assign ongoing work and receive useful results (Priority: P1)

An owner gives an agent standing instructions such as “watch this public release page and summarize changes that need my attention.” The owner reviews the source, cadence, permitted tools, resource limits, and unattended authorization. Once enabled, the assignment continues across sessions. It waits between checks and only calls attention to meaningful findings or required intervention. The same workflow can watch an inbox when a suitable reader tool is connected.

**Why this priority**: Continued work without repeated prompts is the central benefit.

**Independent Test**: Create a public-page assignment, observe an initial check and a later unchanged check, introduce a relevant page revision in the controlled recovery test, and observe one retained finding without another owner prompt. Exercise real public-page reads separately in the live environment.

**Acceptance Scenarios**:

1. **Given** an enabled assignment and valid authorization, **When** its check becomes due, **Then** it checks the selected source and saves progress under the owner's permissions.
2. **Given** an unchanged source, **When** checks find nothing requiring work, **Then** the assignment waits without issuing repeated notifications or running between its bounded wake times.
3. **Given** a relevant new item, **When** the assignment processes it, **Then** it records the source identity, work outcome, and next step and supplies an activity update.
4. **Given** an owner who is signed out, **When** a check becomes due, **Then** only a current, explicitly consented unattended grant permits execution.

### User Story 2 - Recover ongoing work after interruption (Priority: P1)

An owner expects the same assignment to retain its instructions, checked-item cursor, findings, remaining tasks, and completed actions when a worker or server restarts.

**Why this priority**: Recovery without duplicate effects is essential to trusting unattended work.

**Independent Test**: Interrupt work before and after durable completion, restart two competing workers, and verify one accepted result and no replay of completed effects.

**Acceptance Scenarios**:

1. **Given** completed work and an unfinished subsequent step, **When** service recovers, **Then** the completed work remains recorded and only eligible unfinished work resumes.
2. **Given** duplicate or reordered source items, **When** workers attempt to claim them, **Then** completed items are not processed again unless their source revision represents a new event.
3. **Given** a worker whose lease or assignment revision has expired, **When** it attempts to publish or start another action, **Then** the attempt is refused.
4. **Given** an external action with an uncertain outcome, **When** recovery cannot prove completion or safe retry, **Then** the assignment requests reconciliation and does not blindly repeat it.

### User Story 3 - Stay in control of authority and cost (Priority: P1)

An owner can inspect and change instructions, pause or resume checks, stop an assignment, revoke authority, and review sensitive action proposals. Controls remain available while work is running.

**Why this priority**: An unattended agent must remain subordinate to its owner.

**Independent Test**: Exercise each control during an active run, exhaust its budget, and attempt a sensitive action before and after an exact approval.

**Acceptance Scenarios**:

1. **Given** running work, **When** the owner revises instructions, pauses, or stops the assignment, **Then** the control is acknowledged immediately and old work loses authority to start additional actions or publish stale results.
2. **Given** a paused assignment, **When** events arrive, **Then** no execution occurs; resume checks the current source under the latest instructions without a catch-up storm.
3. **Given** a stopped assignment, **When** later events or old approvals arrive, **Then** the assignment remains stopped and performs no further work.
4. **Given** a sensitive proposed action, **When** no valid approval exists, **Then** it waits and shows the exact action, target, relevant parameters, and consequences for review.
5. **Given** approval, **When** instructions, permissions, proposal parameters, or source preconditions change, **Then** the old approval cannot authorize the changed action.
6. **Given** exhausted limits or revoked/expired authority, **When** another step is attempted, **Then** it is refused and the owner receives an actionable explanation.

### User Story 4 - Investigate new bugs using bounded delegated work (Priority: P2)

An assignment turns a relevant new report or public release into a small work plan, delegates independent analysis when useful, and incorporates completed results into its investigation and later work. A connected bug tracker can supply reports; the live example uses public release information and linked public documentation.

**Why this priority**: Persistent assignments must support complex work beyond repeated summaries.

**Independent Test**: Present a new report that requires two independent investigations, interrupt the parent after one child finishes, and verify that the resumed parent reuses that result and combines both findings.

**Acceptance Scenarios**:

1. **Given** complex work, **When** the agent proposes a plan, **Then** accepted tasks have bounded dependencies, owner-visible status, and resource limits.
2. **Given** delegated work, **When** a child runs, **Then** it receives no broader authority and consumes the parent's shared limits.
3. **Given** child completion or failure, **When** the parent continues, **Then** it incorporates the durable result once and explains remaining work or failure.
4. **Given** parent pause, stop, or revocation, **When** a child attempts another action, **Then** the same restriction applies to the child.

### Edge Cases

- Missing connector, expired provider authorization, source throttling, timeouts, malformed output, and source cursor invalidation produce bounded retries or an actionable waiting state.
- Source content is untrusted data; an email or bug report cannot change standing instructions, add permissions, approve actions, or override limits.
- Duplicate triggers, simultaneous workers, duplicate controls, concurrent edits, stale approvals, instruction changes during delegation, and crashes around publication are covered.
- Empty sources, irrelevant updates, oversized histories, repeated failures, and overdue checks cannot create unbounded work or notification storms.
- Model/tool/token/time limits always apply. If the owner selects a currency ceiling, missing trusted cost information prevents activation or execution under that ceiling; unpriced usage is never shown as free or zero.
- Memory and activity must obey existing owner isolation, sensitive-data gates, retention, and deletion rules; credentials and raw authorization tokens are never included.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Owners MUST be able to create named persistent assignments with instructions, selected connected source/tools, check cadence, explicit limits, and consented unattended authority.
- **FR-002**: Assignments MUST retain their current instruction revision, lifecycle state, bounded work history, progress, source cursors/event identities, findings, next wake reason/time, and remaining tasks across worker and server restarts.
- **FR-003**: The system MUST trigger eligible work from due checks and completed delegated work without a new owner prompt, and wait without model activity while nothing is due.
- **FR-004**: Source checks MUST use existing authorized tools, initially the bundled public web reader without new provider credentials. Inbox/bug-reader tools MAY be selected when registered and authorized; unsupported or disconnected sources MUST be reported honestly.
- **FR-005**: Source selection and relevance decisions MUST stay within the assignment's configured scope. Source text MUST be treated as untrusted input rather than owner authority.
- **FR-006**: Claims, completion records, and publication MUST reject stale workers and duplicate completion. Completed event revisions and actions MUST not be replayed after recovery.
- **FR-007**: Actions without a provable safe retry boundary MUST enter a reconciliation state after uncertain outcomes instead of being repeated automatically.
- **FR-008**: Complex work MUST support a bounded plan of tasks and dependencies, optional delegation, durable child outcomes, and a parent continuation that incorporates each result once.
- **FR-009**: Every action and delegated step MUST pass the ordinary current permission, policy, sensitive-data, outbound-access, confirmation, and provenance checks.
- **FR-010**: Unattended execution MUST require a current revocable owner grant, intersect its consent with current authority, and refuse work after expiry or revocation.
- **FR-011**: Assignment limits MUST cover check frequency, retries, concurrent work, task/delegation depth, model/tool/token usage and time. Owners MAY additionally select a currency ceiling only where trusted finite cost bounds exist; unknown monetary cost MUST be shown explicitly. Children and retries MUST consume shared durable limits rather than resetting them.
- **FR-012**: Sensitive changes MUST wait for explicit approval of an immutable, bounded proposal. Approval MUST be owner-bound, revision-bound, expiring, single-use, and revalidated against current permissions and target conditions.
- **FR-013**: Existing interactive-only sensitive operations MUST remain interactive; their approved result may wake the waiting assignment without granting general unattended mutation authority.
- **FR-014**: Owners MUST be able to revise instructions with conflict detection, pause/resume, stop, and revoke an assignment at any time. These controls MUST invalidate stale work and approvals and propagate to child tasks.
- **FR-015**: Stopping MUST be terminal. Resuming paused work MUST revalidate authority and limits and coalesce missed checks into bounded current work.
- **FR-016**: Owners MUST see current state, current instructions, next wake time/reason, limits/usage, work results, pending approvals, and errors through the shared user interface on all supported clients.
- **FR-017**: Activity MUST distinguish waiting, checking, investigating/delegating, waiting for approval, waiting for authorization, budget exhaustion, reconciliation, paused, stopped, completed, and failed work. Notifications MUST be reserved for meaningful results, failures, completion, and required owner action.
- **FR-018**: All retained assignment data and controls MUST be owner-isolated, bounded, and subject to existing privacy/retention rules. Logs MUST expose safe diagnostic codes and identifiers without source secrets.
- **FR-019**: Existing chat, scheduled tasks, first-party agents, external agents, and user-authored agents MUST preserve their trust boundaries and existing behavior when persistent assignments are disabled.
- **FR-020**: The shipped implementation MUST include migration/recovery procedures, automated golden/denial/failure tests, measured changed-code coverage, and live verification of the affected controls and worker recovery before it is declared complete.

### Key Entities

- **Assignment**: Owner, name, instructions/revision, lifecycle, source selection, consent reference, resource limits/usage, next wake, progress and safe status.
- **Source event**: Assignment, source/item/revision identity, discovery time, bounded relevance context, and processing disposition.
- **Work task**: Assignment/revision, parent/dependencies, bounded instructions, status, lease/fence, usage, durable result, and incorporation marker.
- **Action proposal**: Exact target and operation, approval preconditions and expiry, owner/revision binding, execution identity, result or reconciliation status.
- **Activity record**: Bounded owner-visible state transition, work result or request for intervention, timestamp, correlation identity and deduplicated notification status.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A public-page monitor and a delegated investigation of a discovered release complete relevant work after the owner leaves, without another prompt, and display the resulting activity. Registered reader tools can supply inbox/bug events through the same source contract.
- **SC-002**: Across interruption tests at every durable transition, competing workers, and duplicate source deliveries, zero completed actions or findings are published twice.
- **SC-003**: A waiting assignment makes zero model calls between eligible wakeups; repeated unchanged checks produce zero repeated attention notifications.
- **SC-004**: Pause, stop, and revision controls are acknowledged within two seconds under the verification workload; no new dispatch permit is issued under invalidated authority after acknowledgment. Actions whose durable start permit preceded the control are reported as in flight and may finish; their outcomes cannot authorize stale continuation or publication.
- **SC-005**: All tested attempts to cross owner boundaries, widen delegated authority, reuse stale approvals, bypass sensitive-action confirmation, or exceed configured limits are denied.
- **SC-006**: Completed delegated results survive parent interruption, are incorporated exactly once, and remain visible with their provenance.
- **SC-007**: With 25 idle assignments per owner, due work becomes eligible within one configured scheduler tick, bounded to 30 seconds in the verification environment, without a model-driven busy loop.
- **SC-008**: Every supported client exposes equivalent assignment controls and activity from the shared server-owned definition, with recorded live verification and automated parity checks.

## Assumptions

- Existing product authentication, connected tools, consent, privacy controls, and normal dispatch are reused. Connecting new provider accounts or introducing provider-specific credentials is outside this feature.
- An initial check cadence is at least 60 seconds; active assignments default to a maximum of 25 per owner, subject to lower operator/owner limits.
- Scheduled polling is the initial external-event acquisition mechanism. New unauthenticated provider webhook endpoints are outside scope.
- An assignment is ongoing until stopped unless its owner explicitly supplies a completion condition. It does not imply an always-running model process.
- All current client targets are in scope. Existing primitives and shared surfaces are preferred; no new primitive or third-party runtime dependency is assumed.
- On wrist-sized clients, the server supplies bounded status/results and chat-based lifecycle controls; detailed creation and sensitive-action review use the existing handoff to a full client. This is an explicit form-factor disposition, not a private client menu fork.
- Publication, production deployment, app-store submission, and product Git pushes are not requested by this implementation task.
