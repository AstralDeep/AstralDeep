# Feature Specification: FHIR Clinical Data agent

**Feature Branch**: `fhir-agent`
**Created**: 2026-10-05
**Status**: Implemented; verification is recorded in [verification.md](verification.md).
**Input**: Create an agent that connects AstralDeep to an HL7 FHIR stream of demo eICU patient data, visually appealing through astralprims, built and tested on the sandbox server. The stream must not be exposed to anything but the agent.

## Clarifications

### Session 2026-10-05

- Q: What does "stream" mean? → A: A live replay served over FHIR REST. Stays play out against the wall clock on a loop, and the agent reads the latest values or history with standard FHIR search.
- Q: Which FHIR release and server? → A: FHIR R5 (5.0.0), served by a purpose-built lightweight service validated against the official HL7 definitions. `HL7/fhir` is the specification source, not a server.
- Q: How does the agent land on `main`? → A: The owner authorized a direct push with the organization-admin bypass once the work is finished.
- The FHIR service is a separate project outside this repository. This feature is the agent only, and it works against any FHIR R5 server that offers the resources it reads.

## User Scenarios & Testing

### User Story 1 - See who is in intensive care (Priority: P1)

A clinician or researcher asks what the ICU looks like right now and gets the census, its breakdown by unit type, the recent admission rate and the latest vital signs of the most recently admitted patients.

**Independent Test**: With a FHIR server holding one active ICU encounter and recent vitals, `icu_census` returns one card and a summary naming that patient, their latest values and a flag.

**Acceptance Scenarios**:

1. **Given** active ICU encounters, **When** the census is requested, **Then** one card shows the total, the breakdown by unit type and a table whose flag column reflects each patient's worst listed vital.
2. **Given** a unit filter that matches nothing, **When** the census is requested, **Then** the card reports zero patients instead of failing.

### User Story 2 - Review one patient (Priority: P1)

The user opens a patient and sees demographics, the current stay, latest vitals with alert colouring, risk predictions, problems, allergies, key laboratory results and active orders, then drills into vital trends, laboratory history, medication and a timeline.

**Independent Test**: Each patient tool returns one card with a stable id and a compact summary for a patient with data, and a clear empty state for a patient without.

**Acceptance Scenarios**:

1. **Given** a patient id from the census, **When** an overview, trend, laboratory, medication or timeline view is requested, **Then** the matching card is returned and a repeat request replaces it in place.
2. **Given** an identifier that is empty, malformed or unknown, **When** a patient tool runs, **Then** nothing is requested from the server for malformed input and the user sees a fixed message.

### User Story 3 - Watch activity arrive (Priority: P2)

The user asks to watch the ICU and a card updates as admissions, discharges and results arrive through FHIR topic subscriptions.

**Independent Test**: With scripted subscription events, the stream yields a first card, updated cards with correct counters and a terminal card, and deletes its subscriptions however it ends.

**Acceptance Scenarios**:

1. **Given** a card whose data can be streamed, **When** the platform can show live streams, **Then** the card carries a button that starts the stream without a model call, and the button is absent otherwise.
2. **Given** a running stream of one patient's vital signs, **When** a new reading arrives, **Then** the same card shows it and counts it, and the stream ends by itself within the requested time.

### User Story 4 - Operate it safely (Priority: P1)

An operator turns the agent on for one deployment by configuration and off again without leaving anything behind.

**Independent Test**: With `FF_FHIR` off the agent is absent from in-process registration, subprocess start-up and the safe seed; with it on, all three include it.

## Requirements

- **FR-001**: The agent MUST be read-only. It MUST NOT expose a tool that changes clinical data, and its only non-read requests are creating and deleting its own subscriptions for the live feed.
- **FR-002**: The agent MUST be absent unless `FF_FHIR` is on: not registered in-process, not spawned as a subprocess, not seeded with permissions.
- **FR-003**: The FHIR address and token MUST be operator configuration. No tool argument may supply a server address.
- **FR-004**: All requests MUST use `shared/external_http.py`, allow-listing only the configured host and only for the agent's own calls. Paging links to another origin MUST be refused.
- **FR-005**: Every tool MUST return exactly one top-level astralprims card with a stable id, plus a compact summary for the model that contains no raw resources.
- **FR-006**: Failures MUST surface as fixed public messages; server text MUST NOT reach the user.
- **FR-007**: Data from the feed MUST be classified as untrusted for taint tracking.
- **FR-008**: Tool names and argument names MUST NOT match sink, threat or protected-field patterns.
- **FR-009**: The live feed MUST end within its requested duration and MUST delete its subscriptions on normal completion, failure and abandonment.
- **FR-010**: Display thresholds and reference intervals MUST be labelled as presentation aids on every card that uses them.
- **FR-012**: Cards whose data can be streamed MUST offer the stream through an astralprims button only while `tool_streaming` and `stream_progress` are both on. A streamed card MUST carry no author id so the stream and its saved state share one canvas component.
- **FR-013**: With `FF_STREAM_PROGRESS` on, the orchestrator MUST save a bridged push stream's latest content to the canvas while it runs, only for tools that declare `persist_progress_s`, no more often than that interval and only when the content changed. With the flag off, stream behavior MUST be unchanged.
- **FR-011**: When the server holds more readings or charted doses than a view reads, the view MUST keep the most recent ones and state on the card that it shows a subset. Thinning a series for a chart MUST keep each interval's highest and lowest value.

## Success Criteria

- **SC-001**: Every tool, error path and gating branch is covered by deterministic tests with no network access; changed-line coverage is at least 90%.
- **SC-002**: On the sandbox, with the flag on, an example prompt from the agent's intro dialog produces a rendered card from live data.
- **SC-003**: With the flag off, existing registration, start-up and seed tests pass unchanged apart from naming the new id in their exclusion lists.
