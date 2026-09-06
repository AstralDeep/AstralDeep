# Feature Specification: Operational metrics access and background latency

**Feature Branch**: `codex/080-runtime-metrics`
**Created**: 2026-09-06
**Status**: Implemented and locally verified; review publication authorized, merge/deployment not authorized
**Input**: The owner asked to begin practical priority improvements from the growth investigation using the current setup, retaining primitive UI and security and using Claude.

## User Scenarios & Testing

### User Story 1 - Protect operational diagnostics (Priority: P1)

As an operator, I need aggregate service diagnostics restricted to verified administrators so ordinary accounts cannot inspect deployment-wide activity.

**Independent Test**: Exercise the existing metrics route with unauthenticated, invalid, ordinary-user and administrator principals; only the administrator obtains data.

**Acceptance Scenarios**:

1. Given an unauthenticated or invalid principal, requesting diagnostics fails before any metrics or admission inspection occurs.
2. Given a valid non-admin account, requesting diagnostics returns a permission denial without data or collector work.
3. Given a verified admin account, requesting diagnostics returns the existing payload-free snapshot and no-store response.
4. Given an ordinary user querying their own operation, owner-scoped reconciliation continues to work; admin restrictions apply only to deployment diagnostics.

### User Story 2 - Diagnose slow background tasks (Priority: P2)

As an operator, I need aggregate realized queue, execution and total elapsed times so I can distinguish waiting for admission from time spent running and understand latency distributions.

**Independent Test**: Complete, fail and cancel controlled background operations and verify exact aggregate distributions without exposing operation identities or content.

**Acceptance Scenarios**:

1. Given accepted, started and terminal timestamps, a terminal background operation contributes one queue-wait, execution and end-to-end observation.
2. Given an operation that terminates before starting, elapsed waiting and end-to-end time are measured; no execution observation is fabricated.
3. Given repeated terminal observation of the same manager task, latency is counted once, independently of delivery/reconnection count.
4. Given malformed or missing required timestamps, diagnostics record a bounded omission reason and lifecycle handling still succeeds.
5. Given concurrent observations and snapshot reads, aggregate bucket/count/sum snapshots remain internally consistent.

### Edge Cases

- Zero duration and exact bucket boundaries; very long durations and the overflow bucket.
- Missing required timestamps, naive timestamps, invalid ordering, and unsupported states.
- Exporter/collector failure must not block admission, terminal delivery or cleanup.
- Process restart resets these counters; they are operational observations, not durable billing, audit or population-complete research evidence.
- Background operations that terminate without being observed by this manager are not represented; no global exactly-once telemetry claim is made.

## Requirements

### Functional Requirements

- **FR-001**: Deployment metrics MUST require verified admin authority through the existing IAM role gate, with denial before collector work.
- **FR-002**: Successful diagnostic reads MUST retain the existing response envelope and no-store behavior; ordinary owner-scoped operation access MUST remain available.
- **FR-003**: Background terminal observations MUST aggregate realized queue, execution and end-to-end latency using existing authoritative timestamps and terminal-observation ownership.
- **FR-004**: Never-started terminal work MUST have no execution sample; missing/invalid required timestamps MUST produce a bounded omission signal rather than invented latency.
- **FR-005**: Aggregation MUST use fixed duration buckets and closed phase/outcome vocabularies, with no identifiers, task kinds supplied by users, raw terminal codes, prompts, credentials or payload dimensions.
- **FR-006**: Recording and snapshotting a latency observation MUST be atomic with respect to concurrent updates; repeated local terminal observation MUST not duplicate latency.
- **FR-007**: Diagnostic failure MUST not alter authorization, admission, task terminality, cancellation or delivery behavior.
- **FR-008**: Implementation MUST use existing dependencies and infrastructure, retain existing metrics, and introduce no database/schema, primitive, frame or client change.
- **FR-009**: Unit and integration tests MUST cover success, denials, timestamp failures, boundaries, concurrency, deduplication and telemetry failure; changed Python coverage MUST be at least 90%.
- **FR-010**: Operator documentation MUST explain units, aggregation, omission signals, scope/reset limitations and the metrics access change without asserting production deployment or complete capacity evidence.

### Key Entities

- **Operator principal**: existing verified IAM identity and admin role.
- **Terminal background observation**: existing authoritative lifecycle timestamps and coarse terminal state; no new durable record.
- **Latency aggregate**: fixed bucket counts, observation count and sum by closed phase/outcome; process-local and content-free.

## Success Criteria

- **SC-001**: All unauthenticated/invalid/non-admin diagnostic requests in the denial matrix perform zero metrics/admission inspections.
- **SC-002**: Controlled timing sequences yield exact expected counts and sums within floating-point tolerance; each local terminal observation is represented once.
- **SC-003**: All emitted latency dimensions belong to the documented closed vocabulary, including adversarial metadata cases.
- **SC-004**: Task outcomes and owner-scoped reconciliation remain unchanged when telemetry fails.
- **SC-005**: Focused tests and changed-code coverage pass with recorded commands/results; release readiness is reported separately.

## Assumptions

- This session owns a new feature, 080, allocated after refreshing origin and inspecting every remote spec tree; it does not mutate merged feature 079.
- Admin is the existing Keycloak admin role; no new IAM role or identity provider is needed.
- Timing scope is background tasks observed by the existing BackgroundTaskManager. MCP and other existing metrics remain compatible; first-render/client latency and distributed telemetry are separate work.
- Population-wide diagnostic access tightening is intentional and documented. The UI contract remains unchanged.
- The initial implementation step did not request publication. The owner subsequently requested a PR for review, authorizing branch push and review publication; purchase, merge and production deployment remain outside this request.

## Clarifications

### Session 2026-09-06

No critical ambiguity requires user input. Existing role enforcement, fixed aggregate-only telemetry and a background-manager scope are conservative implementation choices within the owner's authorization. Security/privacy, lifecycle semantics, failure handling, units, concurrency and release boundaries are explicit above.

The owner subsequently requested notification when the PR is ready for review. Only Deep changed, so one PR covers this feature. It may be published for code review with the remaining pre-merge qualification listed explicitly; no release-evidence bootstrap or deployment is requested.
