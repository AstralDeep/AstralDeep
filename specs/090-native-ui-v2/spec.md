# Feature Specification: Native UI v2 parity

**Feature Branch**: `codex/090-native-ui-v2`

**Created**: 2026-09-24

**Status**: Draft

**Input**: Bring the completed web UI v2 to iPhone, iPad and macOS first, then Android; use live web screenshots at multiple dimensions as ground truth, use ROTE for adaptation and capability aggregation, save reference screenshots and regular progress notes in kos-wiki, and leave Windows implementation to the owner's Windows machine.

## User Scenarios & Testing

### User Story 1 - Recognizable Apple workspace (Priority: P1)

A user moving from the web console to iPhone, iPad or Mac finds the same navigation, examples, conversation, result controls and settings at comparable window sizes.

**Independent Test**: Compare each Apple form factor against the captured web reference and complete the same signed-in walkthrough.

**Acceptance Scenarios**:

1. Given the landing page, when the user searches agents, filters examples, loads a prompt or runs an example, then the content and actions match the web console and execution uses the ordinary authenticated path.
2. Given a desktop-width window, when the user opens History or Agent Directory, then navigation remains in the persistent left sidebar; below the web drawer breakpoint, it opens in a dismissible drawer.
3. Given a conversation, when a result arrives, then user messages, assistant summaries, compact result previews, reasoning disclosures, turn labels and full-screen controls follow the web arrangement and retain component identity.
4. Given settings, when the user selects a server-authorized item, then it displays the same settings surface in a rail-and-content dialog on larger screens and a full-screen sheet with horizontal navigation on phone widths.
5. Given the composer, when the user opens More options, then background execution, Advanced settings, Workspace timeline and Pulse digest remain reachable as on the web.

### User Story 2 - Equivalent Android workspace (Priority: P2)

After Apple implementation and verification, Android phone and tablet users receive the same experience for comparable dimensions and declared capabilities.

**Independent Test**: Run the web/Apple walkthrough on Android phone and tablet configurations and compare recorded results.

**Acceptance Scenarios**:

1. Given identical authorized content, viewport class and capabilities, when Apple, Android or web displays the workspace, then navigation, content order, actions, theme roles and major layout regions agree.
2. Given rotation, resizing, split view or a capability change, when the client reports its updated state, then ROTE selects the appropriate adaptation without losing the active conversation, focused input or result identity.

### User Story 3 - Recoverable cross-machine implementation (Priority: P3)

The owner and Windows implementer can inspect reference screenshots and dated progress without repeating this research.

**Independent Test**: Open the vault reference index on another machine and trace each screenshot to its viewport, state, source version and capture date.

**Acceptance Scenarios**:

1. Given the reference archive, when a reader selects desktop, tablet or phone dimensions, then landing, navigation, conversation/results and settings are documented with reproducible state descriptions.
2. Given an implementation checkpoint, when a reader opens the progress record, then it distinguishes implementation, automated checks, live verification and remaining work, with repository and branch identities.
3. Given Windows, when this feature is developed, then its source remains outside the redesign scope and shared changes preserve its existing contract dispositions. WatchOS is included by the owner's subsequent clarification and uses watch-appropriate ROTE adaptations.

### Edge Cases

- Narrow windows, 320-pixel web reference, enlarged text, keyboard-visible composer, orientation and split-view changes.
- Long titles, many agents/chats, empty history, empty search/filter results, delayed loading and disconnected state.
- Streaming results and updates while expanded, restored chats, new chat, reconnect, stale actions and signed-out/account-switched state.
- Mandatory first-login surfaces, role-restricted settings, unavailable agent/tools and denied actions.
- Unsupported content preserves the existing labeled ROTE fallback; supported content does not lose values, citations or actions.
- Screenshot capture excludes private user content and secrets; synthetic examples are clearly identified.

## Requirements

### Functional Requirements

- **FR-001**: Capture the current live web UI before native implementation at desktop, tablet and phone widths, recording exact dimensions and source identities.
- **FR-002**: Save screenshots, an index and regular evidence-backed implementation notes in kos-wiki for cross-machine use.
- **FR-003**: Complete Apple implementation and its available verification before beginning Android implementation.
- **FR-004**: Match web v2 sidebar, landing, conversation/result, full-screen, composer and settings behavior at comparable dimensions.
- **FR-005**: Keep navigation, example content, settings authorization and design roles server-owned; native clients remain thin renderers of shared definitions.
- **FR-006**: Use ROTE for layout adaptation and aggregate the client's actual viewport, supported types and interaction capabilities; updates must preserve the current session.
- **FR-007**: Retain Keycloak sign-in, ordinary dispatch, owner isolation, permission/confirmation gates, PHI protection and audit behavior.
- **FR-008**: Preserve accessible labels, keyboard operation, text scaling and reachable controls; phone layouts must not require horizontal page scrolling.
- **FR-009**: Retain existing functional actions including file attachment, voice, sharing/export, component controls, history restore and settings surface events. The bottom web voice availability warning is web-specific and must not be reproduced in native layouts. Verify native voice against the running voice worker, including connection, recording, playback and failure recovery; preserve native actionable errors.
- **FR-010**: Leave Windows redesign/source changes out of scope, with its existing protocol behavior retained. Update watchOS to work with the new system using its actual small-screen and interaction capabilities. Windows parity is explicitly deferred by the owner to a Windows machine; this feature makes no all-client or release qualification claim until that separate work is complete.
- **FR-011**: Add no alternate frontend framework, authentication bypass or locally forked product menu.
- **FR-012**: Record exact checks and visual walkthrough results for iPhone, iPad, macOS, Android phone and Android tablet; do not equate compilation with live parity.
- **FR-014**: Include watchOS in the Apple phase: qualify its new-system contracts, adapted presentation, owner-scoped settings/examples, continuity and voice behavior; do not force a desktop sidebar onto the watch.
- **FR-013**: Bring up the Docker backend for references and live verification and pass the required CI checks before declaring completion; keep baseline failures and toolchain constraints visible until resolved.

### Key Entities

- **Reference capture**: viewport, UI state, capture date, server/client source versions and image identity.
- **Workspace presentation**: shared navigation, examples, theme roles, regions and capability-dependent layout rules.
- **Device capability snapshot**: current viewport and supported input, rendering and accessibility capabilities, aggregated for ROTE.
- **Verification checkpoint**: source identities, changed behavior, checks/results, remaining work and reference links.

## Success Criteria

### Measurable Outcomes

- **SC-001**: The vault contains indexed captures at 1440×900, 1280×800, 1024×768, 834×1194, 768×1024, 390×844 and 320×740, with representative landing/navigation/result/settings states and breakpoint observations.
- **SC-002**: Every in-scope client completes the same signed-in landing-to-result, history restore, full-screen return and settings walkthrough with all authorized actions reachable.
- **SC-003**: All major layout regions and action locations agree with the corresponding web reference; any difference is tied to an explicit platform capability or accessible native control behavior and documented.
- **SC-004**: Rotation/resize/reconnect tests retain the active chat, pending input and result identity, and no tested phone view overflows its viewport.
- **SC-005**: The Windows implementer can reproduce the reference states using only the vault record and source anchors.

## Assumptions

- This is a new native follow-up to the completed, explicitly web-scoped feature 089; it does not mutate that feature's accepted scope.
- Existing user authentication and backend/provider configuration are reused; any interactive sign-in is completed by the user.
- Native platform idioms may differ only where needed for actual input, accessibility or OS capability behavior; they do not authorize a separate navigation model or palette.
- Existing explicitly web-only administrative/tour surfaces retain their server-enforced scope.
- Product changes remain local unless the owner authorizes pushing. Vault checkpoint commits and pushes follow the owner's standing authorization.
- The owner's explicit screenshot request authorizes storing these UI reference images in the vault despite its usual text-only source-ingestion convention.

## Clarifications

### Session 2026-09-24

- The owner expanded scope to include the watch client working with the new system. WatchOS compatibility and appropriate presentation are now required in the Apple phase; Windows remains excluded.
- The owner reported that GLM 5.3 Flash was unavailable and selected a working alternative. Credential-save behavior must be checked; the owner authorized a local credential file if needed, whose contents must never enter product history or evidence.

- The owner confirmed Xcode/Docker updates are ready. The bottom web voice warning is web-specific and excluded from native layouts; native voice-worker functionality remains an explicit live-verification requirement.
- The owner's follow-up requires starting Docker and passing CI at completion; Xcode and Docker are being updated by the owner. Source research continues while those updates finish.
- The formal ambiguity scan found no critical unanswered product questions: functional scope, entities/lifecycle, interaction, security/privacy, integration, failures, constraints, terminology and completion criteria are clear. Exact presentation/contract details and build commands belong in the implementation plan and live-reference record.
