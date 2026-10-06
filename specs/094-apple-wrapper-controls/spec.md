# Feature Specification: Apple Controls and Web/Mobile Consistency

**Feature Branch**: `codex/094-apple-wrapper-controls`

**Created**: 2026-10-05

**Status**: Implemented candidate; component CI and protected adoption/release remain pending. Projection draft PR54 is published. The owner approved implementation, paired merges, Sandbox deployment and Apple upload, fixing every uploaded Apple product to version 1.8 / build 67.

**Input**: "Open up the simulator and click on every single button that you can find in the app wrapper. You don't need to send a query in. Most of the issues stem from buttons not working in the settings modal. Find any of these issues and create a spec, then stop so I can review. Do this quickly." Follow-up: "The dashboard interface is inconsistent with the web client. That needs to be fixed, along with all other consistency issues between web and mobile. Mobile/Apple clients should follow web design and function."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Configure the same account correctly on Apple (Priority: P1)

A signed-in user sees the account's actual saved settings and can identify and use each operation available in the web client, receiving accurate success or actionable failure feedback.

**Why this priority**: The native provider form currently shows four indistinguishable Submit buttons and the wrong selected provider.

**Independent Test**: With a disposable configured account, compare web and Apple provider settings, load models, test connection, save unchanged configuration, and reopen. Do not send a chat query.

**Acceptance Scenarios**:

1. **Given** a saved custom provider, endpoint and model, **When** settings opens on web and Apple, **Then** both display the same selected provider and model with understandable labels.
2. **Given** the provider form, **When** Load models, Test connection, Save or Save TypeSafe key is selected, **Then** only the named operation runs, once, with its own progress and result.
3. **Given** untouched saved fields and a blank replacement key, **When** the user saves at the existing endpoint, **Then** the account retains its provider, model and existing key under the same rules as web.
4. **Given** invalid input, missing acknowledgment, insufficient permission or a storage failure, **When** Save is attempted, **Then** the failure is visible, entered values remain available for correction, and no persistence success is reported.
5. **Given** structurally valid, authorized provider settings, **When** Save is selected, **Then** encrypted account persistence succeeds before the connection test begins, the user receives an accurate saved acknowledgment, and an external connection/authentication/model failure produces a separate warning while retaining the saved credentials for correction and retry.
6. **Given** a loaded provider form retained through normal reconnection, **When** a current authorized Save completes, **Then** its matching ordinary panel closes and the saved acknowledgment and advisory warning remain available; completion cannot dismiss a newer form.

### User Story 2 - Use a consistent dashboard and wrapper (Priority: P1)

A user moving between web, mobile and Apple recognizes the dashboard, directory, history, composer and settings. Controls retain the same meaning and reach the same workflows in layouts suited to the device.

**Why this priority**: The owner requires web to be the reference for design and function. The audit also found an unavailable Drafts workflow and an Advanced settings load failure.

**Independent Test**: Compare identical account, role, theme and content at phone, tablet and desktop sizes. Exercise navigation, example filtering and prompt loading; clear the composer without submitting.

**Acceptance Scenarios**:

1. **Given** the same account and content, **When** the dashboard opens, **Then** mobile and Apple follow the web reference's hierarchy, labels, theme, example categories and action meanings.
2. **Given** a phone or tablet, **When** the directory, history or settings opens, **Then** every authorized entry is reachable by touch and returns without losing the active chat or unsent draft.
3. **Given** an example, **When** Load prompt is selected, **Then** its text appears in the composer without sending; New chat clears that draft as on web.
4. **Given** an authorized user, **When** Drafts or Advanced settings is selected, **Then** the ordinary workflow offered on web opens and functions, without a placeholder or persistent load failure.
5. **Given** a capability requiring another device, **When** the user encounters it, **Then** the approved explanation and handoff provide a clear next step.
6. **Given** Advanced settings fails to load, **When** Retry is selected, **Then** the original authorized Advanced read is retried or its stale authorization is explained; it never silently opens Private notes instead.
7. **Given** the small Watch screen and its negotiated stack navigation, **When** Home or an empty chat opens, **Then** one compact native heading identifies the destination, redundant console title/subtitle text does not displace the primary actions, and the shared server-authorized navigation remains available.

### User Story 3 - Apply and customize appearance (Priority: P1)

A user selects a theme or custom color and sees the chosen appearance throughout the app, retained when the screen or app reopens and consistent across the user's devices.

**Why this priority**: Native theme settings reported Daylight and Ocean as saved/active while the interface retained Midnight styling; preset color previews were absent.

**Independent Test**: Apply every preset on a disposable account, reopen settings and the app, and compare with web. Save a custom color outside the existing native six-color menu.

**Acceptance Scenarios**:

1. **Given** Midnight is active, **When** Daylight, Ocean, Sunset or Forest is applied, **Then** canvas, chrome, settings, text and controls reflect that preset and agree with the active indicator.
2. **Given** a saved preset, **When** settings or the app reopens, **Then** the palette remains visible and the preview matches the web reference.
3. **Given** any valid custom color supported by web, **When** it is applied on mobile or Apple, **Then** that color is displayed and retained for the selected role.
4. **Given** an appearance update is rejected or fails, **When** its result is shown, **Then** the active indicator and appearance agree with the last accepted account state and explain the failure.

### User Story 4 - Complete settings journeys and recover from failure (Priority: P2)

A user completes ordinary web settings journeys for permissions, personalization, audit, notes, authoring, remote machines and help, and can distinguish loading, empty and failed states.

**Why this priority**: Several settings destinations work, but complete action coverage is needed. An unreachable initial server currently leaves a misleading empty wrapper.

**Independent Test**: Exercise each control with safe fixtures and representative role/connection states, including initial failure, recovery, cancellation and denied operations, without a chat query.

**Acceptance Scenarios**:

1. **Given** an ordinary settings section, **When** its controls are exercised, **Then** results, saved values and validation agree with the web journey.
2. **Given** the server is unreachable before any successful connection, **When** the app opens, **Then** it shows connecting/failure status and recovery guidance without claiming unavailable history or settings are empty.
3. **Given** an open form, **When** a response arrives, the keyboard opens or the device resizes, **Then** controls remain reachable and edits remain attached to the correct form and active work.
4. **Given** an attachment, permission, credential or destructive operation is canceled, **When** its dialog closes, **Then** no upload, access expansion, deletion or chat query occurs.
5. **Given** an idle socket whose registered identity expires or whose access token rotates, **When** the user reopens private settings, **Then** normal authentication renewal restores an authorized read without restarting the app; expired requests remain rejected and writes or queries are never automatically replayed.

### Edge Cases

- Initial connection failure, interruption mid-operation, expired session, delayed response, repeated taps, or a response arriving after navigation.
- Saved selection is not the first option; options have distinct labels and values; saved model is absent from a refreshed catalog.
- Conditional fields, duplicate field names across forms, refreshed defaults during editing and invalid/empty input.
- Long labels, large lists, increased text size, landscape, narrow windows and an open keyboard.
- Unknown actions, rejected permissions, failed theme saves, blank replacement credentials and endpoint changes.
- A delayed provider Save completes after navigation to another settings form or a new draft in the same form; it must not dismiss the newer draft.
- A normal OS Keychain read waits during startup; the Apple interface remains responsive and a later sign-out or new sign-in retires that restoration.
- A loaded form survives physical socket replacement, but its server modal ownership must be re-established only by a current authorized action; old or foreign requests cannot claim it.
- A Watch credential refresh or authorized recent-chat response returns after owner, server, session or connection replacement; retired results must not persist credentials, expose another server's rows or alter a newer request's loading state.
- Watch capability handoffs and explicitly bounded role-restricted or web-only entries.
- Watch stack navigation collapses repeated introductory copy at 162-, 198- and 216-point widths. Accepted content palettes retain their shared role colors; native Watch navigation uses a dark background so the system clock remains legible in light presets without adding another header row.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Maintain a complete parity inventory of dashboard, wrapper and settings controls: web reference, expected outcome, permitted adaptation, and pass/fail evidence for every in-scope client and role. Distinguish confirmed defects from unverified gaps.
- **FR-002**: Mobile and Apple MUST follow the approved web reference for hierarchy, typography roles, spacing, card/dialog structure, colors, icons, labels and action placement. Record device adaptations and preserve task completion.
- **FR-003**: Keep every offered control reachable and legible at phone, tablet and desktop sizes, with increased text size, keyboard, rotation and resizing, without clipped or overlapping actions.
- **FR-004**: Present the same server-authorized content, account state and action meanings for the same user and role. Client omissions MUST follow a documented product capability rule.
- **FR-005**: Match web dashboard examples, filters, discovery, history, navigation and composer behavior. Loading a prompt MUST not submit it.
- **FR-006**: Opening, closing or switching surfaces MUST preserve the active conversation, composer draft and operation ownership according to the web workflow.
- **FR-007**: Each offered action MUST retain its distinct readable/accessible label and execute its intended operation. Missing action information MUST produce an explicit unavailable/error state instead of an enabled generic Submit button or silent no-op.
- **FR-008**: Forms MUST display saved defaults, selections, understandable option labels, required states, conditional fields and validation consistently with web. Displayed and submitted values MUST agree, including untouched selects and checklists.
- **FR-009**: Load models, Test connection, Save and Save TypeSafe key MUST be independently usable, show their own progress/result, and prevent duplicate pending submissions. Provider Save MUST persist validated, authorized settings before running the connection test; external probe failure MUST keep the persisted configuration and show a separate connection warning. Persistence/validation failure MUST not report a saved configuration. Unchanged configuration saves MUST retain existing credentials under established endpoint rules.
- **FR-010**: Ordinary Drafts and Advanced settings journeys offered on web MUST function on phone, tablet and macOS, retaining existing authorization, approval and device-execution restrictions.
- **FR-011**: Theme selection MUST update all visible surfaces, active indication and saved account state consistently. Preset previews MUST show the actual colors.
- **FR-012**: Support every valid custom theme color supported by web for all existing roles, display the current value, and report failed/rejected saves without false success.
- **FR-013**: Agents/permissions, provider settings, personalization, audit, theme, private notes, agents/skills, remote machines and user guide MUST support ordinary web operations: navigation, filtering, validation, save/reopen and cancellation. Help MUST describe available device controls and capabilities.
- **FR-014**: Distinguish connecting, connected, stale, failed, loading and confirmed-empty states from first launch. Surface failures MUST offer recovery without concealing the failure or losing entered work. Idle registered-token expiry and token rotation MUST recover through normal authentication, preserving current owner/socket fences and never automatically replaying writes or queries. Read recovery MUST retain the requested destination and its existing authorization, with an explicit failure when that authority is stale.
- **FR-015**: Credential, permission, upload, destructive and execution actions MUST retain web authentication, owner isolation, acknowledgment, approval, auditing and cancellation protections. Denials MUST produce no unauthorized effect.
- **FR-016**: Qualify web, iOS/iPadOS, macOS and Android at relevant form factors, plus watchOS's approved capabilities/handoffs. Shared changes MUST preserve existing Windows functionality and contracts.
- **FR-017**: Verify successes, invalid input, denials, service failures, recovery, repeated taps, cancellation and saved-state reopening. Use disposable fixtures for credential/destructive operations; the owner's live secrets and data MUST not become test fixtures.

### Key Entities *(include if feature involves data)*

- **Console reference**: Approved web presentation and workflows for a defined account, role, content, theme and viewport.
- **Control parity record**: Label, purpose, input state, result, client disposition and evidence for one control.
- **Account settings**: Existing provider, acknowledgment, theme, profile, guidance, permissions and machine registrations whose presentation/interactions need correction.
- **Operation state**: The initiating screen, input, progress/result and recovery guidance belonging to one action.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: All four provider operations are uniquely named and complete their intended journeys on applicable clients; zero enabled generic replacements or silent no-ops remain.
- **SC-002**: Every saved-value fixture, including non-first provider/model selections, displays and submits correctly on applicable clients.
- **SC-003**: All five presets and custom colors outside the former six choices produce the expected appearance, active indication and reopening behavior on applicable clients.
- **SC-004**: Every inventory control has passing evidence or an explicit approved capability disposition; all confirmed audit defects are resolved and no undocumented missing mobile/Apple workflow remains.
- **SC-005**: Matched phone, tablet, desktop and narrow-window comparisons show consistent product content, hierarchy and design roles; each recorded adaptation preserves the user outcome and every control is reachable.
- **SC-006**: All representative failure, interruption, denial, cancellation and duplicate-tap scenarios show accurate outcomes, preserve appropriate work and produce zero unauthorized/duplicate effects.
- **SC-007**: Dashboard/navigation/settings qualification runs without a chat query, modifying the owner's credentials or deleting the owner's data.

## Assumptions

- The existing server-owned web console is the design/function reference. Shared product-definition repairs preserve parity across affected clients.
- The follow-up expands scope to full ordinary web/mobile console and settings consistency, including Android. This initial audit live-inspected iPhone and web only.
- iOS/iPadOS require ordinary mobile workflows; macOS their desktop equivalents. WatchOS retains documented capability adaptations/handoffs. Existing explicit web-only tour/administrator dispositions remain bounded exceptions; offered ordinary Drafts/Advanced actions are not such exceptions.
- Existing identity, account state, ownership, storage and credential policies are reused. No new product feature, schema, provider or release is requested.
- The owner approved implementation of the full specification on 2026-10-05. The existing no-chat-query audit constraint remains effective; product pushing, merging, deployment and publication require their own authorization.
- During implementation on 2026-10-05, the owner explicitly requested automatic entry of supplied credentials into provider settings and changed provider Save to persist first, then test and warn. Credentials remain excluded from source, logs, specifications and disposable test fixtures; IAM sign-in still belongs to the owner.
- The dated [audit](audit.md) distinguishes observations, source leads, setup changes and incomplete coverage. It does not claim that every control/client was tested.
