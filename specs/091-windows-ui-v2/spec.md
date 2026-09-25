# Feature Specification: Windows UI v2 parity

**Feature Branch**: `codex/091-windows-ui-v2`
**Created**: 2026-09-25
**Status**: Specified; implementation and live qualification pending
**Input**: Implement web UI v2 appearance and behavior in AstralDeep's native Windows client; preserve security, state and server ownership. Product commits remain local.

## User Scenarios & Testing

### User Story 1 — Navigate and start work (Priority: P1)

A signed-in user sees the same dashboard categories, scenarios, agent directory, History and account controls as on the web at matching logical dimensions.

**Why this priority**: The entry experience must make the same work discoverable.
**Independent Test**: Resize the signed-in dashboard, search agents, collapse History and activate a scenario.
**Acceptance Scenarios**:
1. Given a scenario, Load prompt populates an editable composer without dispatch, while Run submits through the ordinary authorized path exactly once.
2. Given a narrow window, navigation becomes a dismissible drawer and every control remains reachable by keyboard and supported touch input.
3. Given denied or missing catalog access, display an actionable error without inventing catalog entries or exposing another owner's data.

### User Story 2 — Converse and interact with results (Priority: P1)

A user submits text, attachments and voice, receives an ordered feed with bounded result previews, and opens a result full screen without losing conversation or component state.

**Why this priority**: Results and continuity are core work behavior.
**Independent Test**: Complete a synthetic dice request against the live backend; collapse, expand, enter and exit full screen, export/share and continue the same conversation.
**Acceptance Scenarios**:
1. Given an interactive result, collapse/expand and full-screen transitions preserve values and callbacks and restore focus on exit.
2. Given attachments, More and Advanced selection, the existing authorized upload, selection, export/share and dispatch semantics remain available.
3. Given an available voice worker, recording, response playback and interruption work; worker loss offers recovery without clearing the draft. Windows never reproduces the web-only passive voice warning.

### User Story 3 — Consistent settings and accessible presentation (Priority: P1)

A user navigates the server-provided settings surfaces, changes theme and uses controls at different Windows display scales.

**Why this priority**: Parity includes settings and accessibility, not only a landing screenshot.
**Independent Test**: Compare settings, keyboard focus and themes at the reference sizes and 100%, 125%, 150% and 200% display scaling.
**Acceptance Scenarios**:
1. Given a settings definition, show its authorized labels/order and rendered surfaces with responsive navigation.
2. Given a theme preference, all equivalent controls consume the same palette roles and use Open Sans typography.
3. Given each existing v2 primitive, show equivalent information/actions with accessible names and a truthful empty/invalid-data response.

### Edge Cases

- Expired authentication, permission denial, disconnected backend/voice worker, unavailable shared contract revision, empty history/catalog, long text and large result payloads.
- Resize while editing, streaming, viewing full screen or navigating settings; high DPI and mixed input devices; keyboard-only use; no touch hardware.
- Invalid/non-finite chart values, zero totals, missing labels, unknown action IDs, duplicate component IDs and disabled actions.
- Cross-owner conversation switch and logout clear owner-bound views; attachment/export failures retain actionable feedback without leaking credentials.

## Requirements

### Functional Requirements

- **FR-001**: Windows MUST match the web sidebar/drawer, collapsible History, agent search and account controls at equivalent logical sizes.
- **FR-002**: Landing MUST consume shared categories/scenarios with distinct Run and Load prompt semantics.
- **FR-003**: Conversation feed MUST preserve order, continuity, result previews, collapse/expand, full-screen state and keyboard focus.
- **FR-004**: Bottom composer MUST retain attachments, native voice, More and Advanced selection while resizing.
- **FR-005**: Settings MUST use shared navigation/surfaces, palette roles and Open Sans, with visible focus and actionable errors.
- **FR-006**: Existing action_group, stat_group, gauge, pipeline_stepper, donut_chart and radar_chart MUST render natively and retain their declared actions/data semantics.
- **FR-007**: Adaptation MUST follow ROTE using aggregated actual capabilities, logical dimensions and input availability; Windows MUST NOT claim unsupported capabilities.
- **FR-008**: Chrome, catalogs and actions MUST remain server-owned. Windows MUST consume the coordinated shared revision, without a parallel product catalog/protocol or web wrapper.
- **FR-009**: Keycloak authentication, owner isolation, permission/confirmation gates, component state, export/share and attachments MUST remain enforced.
- **FR-010**: Native voice MUST be verified against the real worker. The web-only bottom availability warning MUST be absent from Windows.
- **FR-011**: Apple, watchOS, Android and feature 090 artifacts MUST remain untouched. Independent Windows work MUST continue while the shared dependency is unavailable.
- **FR-012**: Qualification MUST record exact automated checks, changed-code coverage of at least 90%, source-bound screenshots and live verification gaps without weakening gates.

### Key Entities

- Shared console definition: authoritative navigation, catalog and action identities with revision negotiation.
- Capability snapshot: aggregate input, output and display capabilities of this running client.
- Conversation/result: owner-scoped ordered content and persistent component state across presentation changes.
- Verification evidence: exact source revisions, logical/pixel size, scaling, state, hashes and observed limitations.

## Success Criteria

### Measurable Outcomes

- **SC-001**: At all seven reference sizes (1440×900, 1280×800, 1024×768, 834×1194, 768×1024, 390×844, 320×740), navigation, landing, composer, settings and results remain reachable without unintended horizontal clipping.
- **SC-002**: Every named acceptance flow succeeds with keyboard operation and available native inputs, and all four scaling settings retain legible controls and correct hit targets.
- **SC-003**: A successful live request survives collapse/expand and full-screen round trips with identical result values, owner and conversation identity.
- **SC-004**: All six existing v2 primitive types preserve their data and action semantics; invalid data fails safely and visibly.
- **SC-005**: Automated gates and required changed-code coverage pass; every unperformed live/native/CI check is explicitly recorded as a gap, never a pass.

## Assumptions

- The user authorizes a separate Windows feature 091 after remote-tree collision checks; feature 090 is reserved by the Mac even though not published.
- The vault's 54-image reference at 92e4ed0 supersedes its earlier incomplete 470a390 archive; both source anchors are preserved. Live web behavior remains authoritative.
- Shared console/v2/ROTE changes are owned by the Mac feature. Integration waits for exact published Deep/Projection revisions and confirmed Windows negotiation support; existing-contract renderer work is independent.
- Existing Keycloak sign-in is performed by the user; no credentials or authentication bypass will be introduced.
- No new primitive, schema migration, product release, remote product push or cross-client implementation is authorized by this Windows task.

## Clarifications

### Session 2026-09-25

- Scope, security, ownership, reference dimensions and voice-warning exclusion are explicit in the owner's request; no additional product decision is needed.
- Shared revision identity is an external integration dependency, not permission to invent a replacement contract. Coordination was requested while independent work proceeds.
