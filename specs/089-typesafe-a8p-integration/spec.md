# Feature Specification: TypeSafe Routing and a8p UI Integration

**Feature Branch**: `089-typesafe-a8p-integration`

**Created**: 2026-09-17

**Status**: Draft — specified and planned; implementation not started

**Input**: Integrate the a8p proof of concept into the Astral ecosystem. The main focus is a8p's UI generation and TypeSafe AI (System One / Jev) routing for agents, UI and security. By the end of the migration the Astral system must keep working without interrupting the user's flow. Add a TypeSafe API key field to the LLM options so the live system never uses the owner's personal key. When a user has no TypeSafe key, Astral reverts to the current routing and UI generation.

## Scope and source authority

Source baselines:

- AstralDeep `main` at `e92db75d` (feature 088 merged), with the component pins in `config/astral-composition.json`: AstralProjection `447ac077`, AstralPlane `1aee0db3` (schema `088.008`), AstralPrimitives `8dadde18` (0.3.0), LETS `v1.0.11`.
- a8p at `bcdc014` (`c:\Users\Sam\Desktop\a8p`): `astral/orchestrator.py` (TypeSafe fan-out), `astral/primitives.py` (23 primitive types plus HTML renderer) and `astral/app.py` (web console design).
- kos-wiki `project-a8p` (ingested 2026-09-17). It records that live Jev routed 33 of 33 console example prompts to the intended agent, against 24 of 33 for the keyword fallback. It also records known a8p defects at `bcdc014`: an `eval` code-execution path in `calculate_math`, double tool execution per turn, blocking I/O in the async chat endpoint, unenforced HITL verdicts and a stale Playwright test. These defects are further reasons a8p code is not ported verbatim.
- `typesafe-sdk` 0.6.0 (`AsyncTypeSafeClient`, `RetryPolicy`, typed errors, `models.list`).

This feature adopts a8p's **ideas and design**, not its code verbatim:

- TypeSafe single-call fan-out.
- Deterministic presentation-driven layout.
- The six primitive types astralprims lacks.
- The visual design language.

a8p's 11 simulated agents, in-memory session store, in-memory audit ledger, keyword argument extraction and f-string HTML renderer are **not** migrated. Astral's existing agents, durable work, hash-chained audit, sanitizing renderer and gate stack remain authoritative.

**Supersession**: This feature supersedes the unimplemented draft specifications 081–087. Their content was already reconciled into 088. Their unpushed local branches were deleted on 2026-09-17; final tips were 081 `e44f6ddc`, 082 `ee0f7f4d`, 083 `9b42fa7a`, 084 `a50b74f9`, 085 `c3573fa7`, 086 `928d6c2f`, 087 `51f6ee22`. No requirement from them is implied by this feature beyond what 088 already carries.

## Clarifications

### Session 2026-09-17

- Q: Who owns the TypeSafe API key? → A: **Per-user only.** Each user supplies their own key in LLM options, encrypted at rest like the existing LLM key. There is no deployment-wide or operator default. The owner's personal key is never used by the live system.
- Q: What does the a8p visual design apply to? → A: **The look replaces the current web UI, with no toggle.** It applies to everyone regardless of TypeSafe key. Only TypeSafe-driven decisions depend on the key.
- Q: How do TypeSafe security judgments relate to existing defenses? → A: **An additive layer.** Every existing gate still runs; TypeSafe can only add a refusal or a confirmation requirement. This holds only if the security screen adds no substantial lag.
- Q: What happens when TypeSafe fails mid-turn? → A: Inform the user with a UI message, then retry with exponential backoff up to 3 tries. On complete failure, fall back to the current routing and rendering. If that also fails, inform the user and stop.
- Q: What is the maximum extra wait from TypeSafe on one turn? → A: **About 1.5 seconds total**, including all retries, before fallback begins.
- Q: How much of the per-turn decision does TypeSafe own? → A: **Agent, tool and presentation style.** The LLM fills arguments for the chosen tool.
- Q: Is a staged rollout or shadow mode needed? → A: **No.** No users are on the live site. A kill-switch flag remains for operations.
- Q: Where does the plan live? → A: A Spec Kit feature `089` on its own branch in AstralDeep, touching Projection, Primitives and Plane as needed.
- Q: Should `typesafe-sdk` be adopted or raw HTTP used? → A: **Adopt `typesafe-sdk`, pinned.** The owner approved it on 2026-09-17 and amended the AstralDeep constitution to v3.0.0, under which any dependency may be installed without lead-developer approval (Principle V).
- Q: Which TypeSafe key prefix should redaction use? → A: The implementer's choice. It is derived from the real TypeSafe key format during implementation, and every `*api_key` field is redacted regardless of format.
- Q: Are the native and Windows drift-guard failures acceptable? → A: **Yes.** Only the web client matters for this feature.
- Q: May the implementer use the owner's credentials for testing? → A: **Yes.** The owner permits configuring their own LLM and TypeSafe keys in the local candidate stack for testing and measurement, under FR-044.
- Directive (owner, 2026-09-17): **The LLM credential page shows a data-sharing warning with a checkbox below the credential inputs.** The warning says that using a third-party model or a TypeSafe model may share the user's data with that provider through requests.
- Q: What about draft specs 081–087? → A: Old local branches before 088 are deleted, and 089 supersedes them.
- Directive (owner, 2026-09-17): **Open Sans is non-negotiable** as the web typeface.
- Directive (owner, 2026-09-17): **Colors are managed by Astral's existing `ThemeView`** (server semantic palette). a8p's hard-coded hex palette is not ported.
- Directive (owner, 2026-09-17): **On desktop screens, the web layout must be at least 90% similar to the a8p console**, measured by the layout-parity inventory in `contracts/web-layout-parity.md`. Parity applies to desktop viewports only. Tablet and phone-sized browsers need their own responsive layout and ROTE adaptation so the UI looks good on every screen type; they are not scored against a8p.
- Directive (owner, 2026-09-17): **Only the web client is updated.** The Windows, Android, iOS, macOS and watchOS clients are left alone, with no source, asset, test or manifest changes in their client directories. Server-owned SDUI that already reaches them unchanged (for example the new settings fields) is not a client change. New component types are down-converted server-side by ROTE before reaching them.
- Directive (owner, 2026-09-17): **kos-wiki (`Y:\WORK\kos-wiki`) is referenced and updated at regular intervals** throughout 089. Read the relevant pages before each phase. Record progress, decisions, measurements and divergences at every phase checkpoint and at defined milestones, following the wiki's own schema (`CLAUDE.md`: provenance tags, dated `log.md` entries, `index.md` upkeep, sensitivity rules).
- Directive (owner, 2026-09-17): **CI is ignored for this feature on all Astral and LETS repositories.** CI results do not gate implementation, review or merge of 089 work, and CI workflow files are not modified. Tests are still written and run locally (Constitution III), and local results are the qualification evidence. This is a recorded owner exception to Constitution XI and to the CI portions of X and XII enforcement.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Bring my own TypeSafe key (Priority: P1)

A signed-in user opens **Settings → LLM settings**, finds an optional **TypeSafe routing** section, pastes their own TypeSafe API key and saves. Astral verifies the key, stores it encrypted, and shows that smart routing is active. The user can replace or remove the key at any time. Removing it returns the user to standard routing and never locks them out of chat.

**Why this priority**: Every TypeSafe capability is keyed per user. Without this surface, no live user can use TypeSafe, and the owner's personal key would be the only way to exercise it.

**Independent Test**: With a stubbed TypeSafe endpoint, save a valid key, a rejected key and an empty key on web and on one native client. Then remove the key. Confirm the stored ciphertext, write-only display, status line, audit events without key material, and that the first-run LLM gate is unaffected in every case.

**Acceptance Scenarios**:

1. **Given** a user with a working LLM configuration and no TypeSafe key, **When** they open LLM settings, **Then** they see an optional TypeSafe section whose status reads that standard routing is in use, and chat works unchanged.
2. **Given** a valid key, **When** the user saves it, **Then** Astral verifies it with a bounded request, stores it encrypted, never echoes it back, and shows smart routing as active.
3. **Given** a key TypeSafe rejects, **When** the user saves it, **Then** nothing is stored and the user sees an actionable message. Their existing LLM configuration and any previously saved TypeSafe key are unchanged.
4. **Given** a saved key, **When** the user removes it, **Then** the record is deleted, the next turn uses standard routing, and the first-run LLM gate is **not** re-triggered.
5. **Given** a deployment where a `TYPESAFE_API_KEY` (or any `TYPESAFE_*`) environment variable is present, **When** the application starts in production posture, **Then** startup refuses with an explicit message. No user's turn can ever authenticate with an environment-supplied key.
6. **Given** the first-run mandatory LLM dialog, **When** it is shown, **Then** it does not include the TypeSafe section. TypeSafe is never a precondition for using Astral.

---

### User Story 2 - Faster, better-targeted routing with my key (Priority: P1)

A user with a saved TypeSafe key sends a normal request, such as "what's the 7-day forecast for Lexington". In one TypeSafe call, Astral judges which agent and tool fit the request. It then asks the LLM only to fill that tool's arguments rather than choosing among every tool. The result arrives through the same tools, permissions and audit as today.

**Why this priority**: This is the central capability of the migration. It shrinks the model's tool-selection prompt and uses a calibrated, typed decision for routing.

**Independent Test**: With a deterministic TypeSafe fake and a scripted LLM, run a fixture set of single-tool, multi-tool, conversational and ambiguous prompts. Confirm that high-confidence decisions narrow the first round, medium confidence supplies a shortlist, and low confidence or no match leaves current behavior unchanged. All gates must still run.

**Acceptance Scenarios**:

1. **Given** a keyed user and a request that clearly matches one eligible tool, **When** they Send, **Then** the first model round receives only that tool and a directive to use it where the provider supports it. The tool executes through the unchanged gate stack.
2. **Given** a request where TypeSafe's top choices are close, **When** the turn runs, **Then** the first round receives a bounded shortlist with automatic tool choice rather than a single forced tool.
3. **Given** a conversational follow-up, a low-confidence decision, or a choice outside the user's currently eligible tools, **When** the turn runs, **Then** the turn proceeds exactly as it would without a key.
4. **Given** a multi-step task, **When** later rounds of the reasoning loop run, **Then** they use the full eligible tool list exactly as today. Routing only shapes the first round.
5. **Given** a user who disabled an agent, lacks a permission, or deselected a tool, **When** TypeSafe names that agent or tool, **Then** it is not offered, because TypeSafe candidates are drawn only from the user's already-filtered eligible tools.
6. **Given** a user without a key, **When** they Send, **Then** no TypeSafe request is made and behavior is identical to the pre-089 baseline.

---

### User Story 3 - Uninterrupted flow when TypeSafe misbehaves (Priority: P1)

A keyed user sends a request while TypeSafe is slow, rate-limited, down, or rejecting their key. The user sees a brief non-blocking notice, Astral retries within a strict time budget, and then answers the request the standard way. If the standard path also fails, the user is told clearly and the turn stops, with no hang, silent loss or duplicate effect.

**Why this priority**: The owner requires that the migration never interrupt the user's flow. Failure behavior is part of the core contract, not polish.

**Independent Test**: Inject timeouts, 429, 529, 5xx, connection errors, 401 and malformed responses into the TypeSafe fake. Measure added wall time per turn, the notices sent, fallback activation, circuit behavior across consecutive turns, and the terminal-failure message when the scripted LLM also fails.

**Acceptance Scenarios**:

1. **Given** a transient TypeSafe failure (timeout, connection error, 408/429/5xx/529), **When** the first attempt fails, **Then** the user receives a non-blocking status notice and Astral retries with exponential backoff, up to 3 attempts in total.
2. **Given** all attempts fail, **When** the budget is exhausted, **Then** the turn continues on the standard routing and UI generation path. Total TypeSafe-attributable delay is at most 1.5 seconds and the user is told standard routing was used.
3. **Given** a non-transient failure (401/403 rejected key, 422 malformed request), **When** it occurs, **Then** Astral does not retry. It falls back immediately and marks the key status in LLM settings (for example "Key rejected — update or remove it").
4. **Given** repeated TypeSafe failures on consecutive turns, **When** the failure threshold is reached, **Then** Astral stops attempting TypeSafe for that user for a cool-down period, so later turns add no retry delay. It resumes automatically afterwards.
5. **Given** the standard path also fails after fallback, **When** that failure occurs, **Then** the user sees an explicit failure message through the existing error surface and the turn stops, with no partial effect, duplicate tool call or indefinite progress state.
6. **Given** a turn that was cancelled or disconnected during the TypeSafe attempt, **When** cancellation is observed, **Then** the in-flight attempt is abandoned and no retry or fallback continues on the user's behalf.

---

### User Story 4 - An extra, fast safety screen (Priority: P1)

For a keyed user, the same TypeSafe call that routes the request also judges jailbreak likelihood, harm and threat category. A clearly malicious request is refused before any model or tool runs. A risky one requires the user's explicit confirmation before any tool executes. A benign request passes to the existing gates with no extra wait.

**Why this priority**: Astral currently runs no classifier on user input. `supervisor.scan_ingress` exists but is never called. The owner accepted this layer only if it adds no substantial lag.

**Independent Test**: Run the existing security benchmark corpus and a benign reference corpus through a recorded TypeSafe fixture. Verify refusal, confirmation and pass-through tiers, audit records, and that no verdict removes an existing denial. Measure added latency against the routing call alone.

**Acceptance Scenarios**:

1. **Given** a request judged above the calibrated refusal threshold, **When** the user sends it, **Then** the turn is refused before any LLM or tool call. The refusal is recorded in the hash-chained audit without prompt text, and the user sees a clear refusal.
2. **Given** a request in the elevated tier or a sensitive threat category, **When** the model proposes a tool call, **Then** that call requires explicit human confirmation through the existing approval path, even if the tool would not normally require it.
3. **Given** a request TypeSafe judges benign, **When** an existing gate would deny or require approval, **Then** the existing outcome stands. TypeSafe never relaxes, skips or pre-approves any gate.
4. **Given** the security screen, **When** measured on the success path, **Then** it adds no request beyond the single routing call. Its evaluation cost is local policy only.
5. **Given** TypeSafe is unavailable or the user has no key, **When** the turn runs, **Then** exactly the existing gates apply, unchanged from baseline.

---

### User Story 5 - Richer, instantly arranged results (Priority: P2)

When a turn produces several result components, a keyed user's canvas is arranged using TypeSafe's chosen presentation style (dashboard, detailed table, alert-focused or conversational). A deterministic layout is applied immediately rather than waiting for a second LLM design pass. All users gain the new gauge, donut, radar, stat group, pipeline stepper and action group components. Users without a key, or when TypeSafe fails, get today's adaptive designer arrangement.

**Why this priority**: This is a8p's UI generation contribution. It depends on routing (US2) for the presentation decision and on new primitives for richer output.

**Independent Test**: Deliver fixture rounds of 2–8 components for each presentation style. Confirm the arrangement references only delivered components, arrives without an LLM design call, degrades through ROTE on every client profile, and falls back to the existing designer when no style is available.

**Acceptance Scenarios**:

1. **Given** a keyed user whose round yields two or more components, **When** results are delivered, **Then** flat components still arrive first, as today, and the style-driven arrangement follows as the in-place refinement without an LLM design call.
2. **Given** no key, TypeSafe failure, or the style "as delivered", **When** results are delivered, **Then** the existing adaptive designer path runs unchanged. If it fails, the flat components remain, exactly as today.
3. **Given** an agent that returns a new component type (for example `gauge`), **When** delivered to a client whose declared `supported_types` lack it (every current native and Windows client), **Then** ROTE substitutes a defined fallback (for example `progress`, then `metric`, then `text`) server-side and no client shows a broken or blank component.
4. **Given** the UI protocol manifest, **When** new types are added, **Then** the manifest, the Deep manifest test and Projection's server-side disposition matrix are updated. Native and Windows client drift-guard tests are **not** modified, and their resulting local failures are recorded as an accepted known divergence.

---

### User Story 6 - The a8p layout on the Astral web client (Priority: P2)

Every user of the web experience sees a8p's layout and typography. The layout includes the sidebar with brand, agent search and agent directory, the profile widget with settings entry, the landing area with scenario cards and filter tabs, the chat feed with response cards, the bottom composer bar, the full-screen result view and the tabbed settings dialog. All text is set in Open Sans. Colors come from Astral's existing `ThemeView`. Everything 088 delivered remains reachable: one primary composer, recent work, progressive disclosure, accessibility and all existing capabilities. Other clients are untouched.

**Why this priority**: The owner prefers a8p's look and is separately rewriting the native clients. The web redesign is independent of TypeSafe and can land in parallel.

**Independent Test**: Score the redesigned web shell against the layout-parity inventory (`contracts/web-layout-parity.md`) using a8p reference screenshots at desktop viewports 1920×1080, 1440×900 and 1280×800. Confirm at least 90% of weighted items match at each. Separately, run the responsive checklist at tablet (1024×768, 768×1024) and phone (390×844, 320×640) viewports. Verify Open Sans is the computed font family of every text element. Run 088's accessibility, CSP, keyboard and 200% text checks, and confirm no file under any native or Windows client directory changed.

**Acceptance Scenarios**:

1. **Given** any signed-in user on a desktop viewport (≥1280 CSS px wide), **When** they open the web experience, **Then** it renders the a8p layout scoring at least 90% on the layout-parity inventory, with no toggle to the previous look.
2. **Given** a tablet or phone-sized browser, **When** the web experience loads, **Then** it uses a responsive arrangement: sidebar as a drawer, composer pinned and full width, response cards single-column. ROTE adapts result components to the web device profile. Every capability remains reachable with no horizontal scrolling, overlapping controls or clipped content, as defined by the responsive checklist.
3. **Given** any rendered text element in the web shell, including code blocks, **When** its computed style is inspected, **Then** its font family resolves to self-hosted Open Sans.
4. **Given** the user's theme preference, **When** the shell renders, **Then** all colors come from the existing `ThemeView` roles, and no a8p hex literal is hard-coded in shell CSS or JS.
5. **Given** the strict CSP (nonce scripts, `font-src 'self'`), **When** the redesigned shell loads, **Then** there are no inline event handlers, no external font or script loads, and no CSP violations.
6. **Given** the 088 journeys (Send, recent work, results, approvals, attachments, voice, settings), **When** exercised on the redesigned shell at desktop, tablet and phone viewports, **Then** each remains reachable and passes its existing local regression tests.
7. **Given** keyboard-only use, screen readers, reduced motion, 320px width and 200% text, **When** the redesigned shell is used, **Then** 088's FR-028 accessibility behavior holds.
8. **Given** the Windows, Android and Apple clients, **When** 089 is complete, **Then** no file under `windows-client/`, `android-client/` or `apple-clients/` has changed. Those clients continue to work through the existing contract, with new component types down-converted server-side.

---

### User Story 7 - Acknowledge data sharing before saving credentials (Priority: P1)

On **Settings → LLM settings**, and in the mandatory first-run LLM dialog, a user sees a warning directly below the credential inputs with an unchecked checkbox:

> **Your data may be shared.** If you use a third-party model or a TypeSafe model, the content of your requests — which can include your messages and conversation context — is sent to that provider and may be shared with them.
>
> ☐ I understand that my data may be shared with third-party model providers and TypeSafe through requests.

The user must check the box before Astral saves an LLM configuration or a TypeSafe key. Once acknowledged, the box stays checked with the acknowledgment date. It is only required again if the notice wording changes.

**Why this priority**: 089 introduces a second external processor (TypeSafe) alongside the user's chosen LLM provider. The owner requires users to be told explicitly, at the point of entering credentials, that their data leaves Astral through those requests.

**Independent Test**: On the web client, try to save an LLM configuration and a TypeSafe key with the box unchecked (rejected server-side with a field message), then checked (saved and acknowledgment recorded). Reload and see the box checked with its date. Bump the notice version in a test and see it unchecked and required again. Confirm existing users with saved credentials can still chat without acknowledging.

**Acceptance Scenarios**:

1. **Given** the LLM settings page or the first-run LLM dialog, **When** it renders, **Then** the warning text and checkbox appear directly below the credential inputs and above the Save actions.
2. **Given** a user who has not acknowledged the current notice, **When** they select Save for an LLM configuration or a TypeSafe key with the box unchecked, **Then** nothing is saved, no probe request is sent to the provider, and the checkbox shows an inline message: "Check this box to confirm you understand how your data is shared."
3. **Given** the box is checked, **When** the user saves, **Then** the acknowledgment (notice version and time) is recorded and audited, and the save proceeds through its normal verification.
4. **Given** a user who acknowledged the current notice version, **When** they return to the page, **Then** the box is pre-checked and shows "Acknowledged on {date}". Unchecking it and saving is rejected like scenario 2.
5. **Given** the notice wording changes (new notice version), **When** the user next saves credentials, **Then** they must acknowledge the new version.
6. **Given** an existing user whose LLM configuration was saved before 089 and who has never acknowledged, **When** they chat, **Then** nothing is interrupted. Acknowledgment is only requested at their next credential save.
7. **Given** Test connection, Load models or Remove key actions, **When** used with the box unchecked, **Then** they work as before, because they do not save credentials.
8. **Given** a native client rendering the same server-owned surface, **When** it opens LLM settings, **Then** the checkbox arrives as an existing SDUI `boolean` field, with no native client code change.

---

### Edge Cases

- A key is saved while a turn for that user is mid-routing. The in-flight turn keeps the client it resolved, and the next turn uses the new key.
- A key is removed or rejected mid-turn. The attempt fails as non-transient, the turn falls back, and no retry uses a stale key.
- The eligible tool set is empty (all agents disabled). TypeSafe is not called, and the turn proceeds as today.
- The eligible tool set is very large (many agents and BYO tools). Question construction stays within the measured request bound, and beyond it routing degrades to a shortlist or skips TypeSafe rather than exceeding the time budget.
- Tool names collide across agents and receive the `{agent}__` prefix. TypeSafe options use the exact prefixed names the LLM sees.
- The provider does not support a forced `tool_choice` object (some OpenAI-compatible or local runtimes). The narrowed tool list is sent with automatic choice instead.
- A TypeSafe response parses but references an option not in the request. It is treated as low confidence, and current behavior applies.
- A slash command, onboarding intercept or LLM preflight failure short-circuits the turn. TypeSafe is not called.
- A REST-originated chat (`POST /api/chats/{chat_id}/messages`) or background (`async_mode`) turn uses the same routing seam and the same budget. Background turns deliver notices to the virtual socket.
- HTTP Work `kind="chat"` (one-shot, tool-less) receives only the security screen. It has no routing decision to make.
- A keyed user's request includes PHI. The content sent to TypeSafe is exactly the datamarked content already sent to that user's own LLM provider in the same turn, and never attachment bodies.
- The TypeSafe SDK or its transitive dependencies are unavailable at import. The adapter reports TypeSafe unavailable, and every turn uses standard routing with no startup failure.
- A native client older than the new component types connects. ROTE substitutes fallbacks based on its declared `supported_types`.
- A client saves an LLM configuration through the legacy WebSocket `llm_config_set` message instead of the settings surface. The same acknowledgment rule applies: the message must carry `data_sharing_acknowledged: true` unless the user has already acknowledged the current notice version. Otherwise it fails with an explicit error that directs the user to Settings → LLM settings.
- A user checks the box, but the save fails verification (rejected key or unreachable provider). The acknowledgment is still recorded, because the user saw and accepted the notice, and the credential is not saved.
- The admin-only System LLM surface configures a deployment credential, not a user's own. It is out of scope for the checkbox.

## Requirements *(mandatory)*

### Functional Requirements

**Key ownership and settings**

- **FR-001**: Users MUST be able to save, replace and remove their own TypeSafe API key from the server-owned LLM settings surface. The same fields and actions render on web and native clients.
- **FR-002**: The TypeSafe key MUST be stored encrypted at rest with the same credential encryption key and owner scoping as the per-user LLM key. It MUST be write-only (never echoed in markup, components, logs, audit or errors).
- **FR-003**: Saving a key MUST verify it with a bounded, rate-limited request before persisting. A failed verification MUST NOT alter any stored configuration.
- **FR-004**: The TypeSafe key MUST NOT be a condition of the first-run LLM gate. Saving or removing it MUST NOT unlock, gate or re-gate any client.
- **FR-005**: The system MUST NOT authenticate any user's TypeSafe request with a deployment, operator or environment-supplied key. Production posture MUST refuse to start when any `TYPESAFE_API_KEY`, `TYPESAFE_BASE_URL` or `TYPESAFE_DEFAULT_MODEL` environment variable is set. The adapter MUST pass key, base URL and model explicitly.
- **FR-006**: The settings surface MUST show a truthful key status: not set, active, rejected (with time), or temporarily unavailable.

**Routing**

- **FR-007**: For a keyed user, the system MUST make at most one TypeSafe System One request per turn. The request judges security, target agent, target tool and presentation style over the user's currently eligible tools.
- **FR-008**: TypeSafe candidates MUST be derived only from the per-turn eligible tool set after all existing visibility, permission, identity, security-flag and selection filters. Option identifiers MUST match the exact tool names sent to the LLM.
- **FR-009**: Routing decisions MUST be consumed by confidence tier:
  - **High**: the first model round receives only the chosen tool, forced where the provider supports it.
  - **Medium**: the first round receives a bounded shortlist with automatic choice.
  - **Low, no match or invalid**: current behavior applies.
- **FR-010**: Routing MUST affect only the first model round of a turn. Subsequent rounds MUST use the full eligible tool list and existing behavior.
- **FR-011**: The LLM MUST remain responsible for tool arguments. TypeSafe MUST NOT supply tool arguments in this feature.
- **FR-012**: Users without a key MUST experience behavior identical to the pre-089 baseline, and no TypeSafe request MUST be made for them.
- **FR-013**: The routing request MUST start as soon as the eligible tool set is known and run concurrently with the remaining turn preparation.

**Resilience and latency**

- **FR-014**: The TypeSafe attempt budget MUST be at most 3 attempts with exponential backoff. The total TypeSafe-attributable delay before fallback MUST NOT exceed 1.5 seconds, enforced by a hard deadline.
- **FR-015**: Transient failures (timeout, connection, 408, 429, 5xx, 529) MUST be retried within the budget. Non-transient failures (401, 403, 422, response validation) MUST NOT be retried.
- **FR-016**: On the first failed attempt the user MUST receive a non-blocking status notice. On fallback the user MUST be told standard routing was used for that request.
- **FR-017**: After exhausting the budget, the turn MUST continue on the current routing and UI generation path. If that path fails, the user MUST receive an explicit failure through the existing error surface and the turn MUST stop.
- **FR-018**: A per-user circuit MUST suspend TypeSafe attempts for a cool-down period after repeated consecutive turn-level failures, and MUST resume automatically.
- **FR-019**: SDK-internal retries MUST be disabled. All retry, timeout and backoff policy MUST be owned by the adapter.
- **FR-020**: Cancellation or disconnection of a turn MUST abandon any in-flight TypeSafe attempt and its retries.

**Security layer**

- **FR-021**: The TypeSafe security judgments MUST be additive. They MAY refuse a turn or require confirmation for tool calls in that turn. They MUST NOT remove, relax, skip or pre-satisfy any existing gate, approval, budget, PHI, egress, delegation or LETS check.
- **FR-022**: Refusal and confirmation thresholds MUST be calibrated against the existing security benchmark corpus and a benign reference corpus before the refusal tier is enabled. The calibration results MUST be recorded.
- **FR-023**: A TypeSafe refusal or escalation MUST be recorded as an audit event containing tier, category, probabilities, chosen identifiers and latency, and no prompt text or key material.
- **FR-024**: The security screen MUST NOT add a network request beyond the single routing request.

**UI generation and primitives**

- **FR-025**: AstralPrimitives MUST define and document `action_group`, `stat_group`, `gauge`, `pipeline_stepper`, `donut_chart` and `radar_chart` before use, following astralprims naming and serialization conventions.
- **FR-026**: AstralProjection's web renderer MUST render the new types with escaped, attribute-allowlisted output. ROTE MUST provide a fallback ladder for each, ending at a type every existing client already supports. The UI protocol manifest and server-side disposition matrix MUST list them. Native and Windows client code, assets and tests MUST NOT be changed; clients that do not declare a new type MUST only ever receive its fallback.
- **FR-027**: For a keyed user with a presentation style, multi-component rounds MUST be arranged by a deterministic, style-specific layout composer delivered as the existing in-place refinement. The upsert-first ordering MUST be preserved.
- **FR-028**: Without a style (no key, failure or "as delivered"), the existing adaptive designer path MUST run unchanged, including its existing flat-delivery fallback.
- **FR-029**: The layout composer MUST reference only components delivered in that round and MUST NOT invent content.

**Visual design**

- **FR-030**: The web experience MUST adopt the a8p console layout for all users with no toggle. On desktop viewports (≥1280 CSS px wide) it MUST score at least 90% on the weighted layout-parity inventory (`contracts/web-layout-parity.md`) while preserving 088 FR-003, FR-023 and FR-028. Where a8p and 088 conflict, the 088 capability is kept and placed in the a8p region that best fits it.
- **FR-030a**: On tablet- and phone-sized web viewports (<1280 CSS px), the web shell MUST use a responsive arrangement derived from the a8p design and MUST pass the responsive checklist in `contracts/web-layout-parity.md`. ROTE's web device-profile adaptation (level of detail, per-type rules, chart and table sizing) MUST be tuned so result components, including the six new types, render legibly at those widths. This work is confined to server-side ROTE and web shell assets; no native client changes.
- **FR-031**: The redesigned web assets MUST comply with the existing CSP: no inline handlers, no external fonts or scripts, and no `innerHTML` of untrusted strings. They MUST pass the tracked ESLint configuration when run locally.
- **FR-032**: Open Sans (SIL OFL) MUST be self-hosted as WOFF2 under the web client's static assets and be the only text typeface in the web shell, including monospace-styled code. It MUST NOT replace fonts in the shared `contracts/assets/fonts` used by native clients.
- **FR-033**: All web shell colors MUST come from the existing `ThemeView` semantic roles and the user's theme preference. No a8p hex palette MUST be hard-coded, and no new palette definition is introduced by this feature.
- **FR-034**: Only the web client MAY change. No file under `windows-client/`, `android-client/` or `apple-clients/` in AstralProjection MAY be modified by this feature.

**Observability and dependencies**

- **FR-035**: Each TypeSafe attempt and turn outcome MUST be measured: latency, attempt count, outcome class, tier and fallback. Measurements use existing performance spans and low-cardinality runtime metrics, with no content.
- **FR-036**: `typesafe-sdk` MUST be pinned to an exact version in `backend/requirements.txt`, with its transitive packages (`httpx2`, `msgspec`, `tenacity`, `typing-extensions`) resolved and recorded. No approval gate applies (Constitution V v3.0.0, owner approval recorded 2026-09-17).
- **FR-037**: TypeSafe egress MUST be restricted to the configured TypeSafe API host through the existing egress validation.
- **FR-038**: The content sent to TypeSafe MUST be bounded: current message, a bounded recent-history window, active agent, and eligible agent and tool names and descriptions. It MUST be no broader than what the same turn sends to the user's own LLM provider after datamarking, and MUST never include attachment bodies, credentials or tool outputs.

**Knowledge base (kos-wiki)**

- **FR-040**: Before each implementation phase begins, and before any design decision that touches an Astral subsystem, implementers MUST read kos-wiki `index.md` and the relevant pages:
  - `project-a8p`, `project-astral`, `synthesis-astral-rewrite-review`;
  - `astral-orchestrator`, `astral-llm-credential-resolution`, `astral-security-defense-layers`, `astral-security-benchmark-harness`;
  - `astral-adaptive-ui-designer`, `astral-primitives`, `astral-rote`, `astral-web-client`, `astral-cross-client-contracts`;
  - `astral-feature-flags`, `astral-feature-timeline`, `astral-open-follow-ups`, `astral-ci-gates`.

  Where the wiki and current code disagree, the current code on the 089 base wins, and the disagreement MUST be recorded back to the wiki as a contradiction per its schema.
- **FR-041**: kos-wiki MUST be updated at every phase checkpoint in `tasks.md` and at each of these milestones, whichever comes first, and at least once per working day on which 089 work lands:
  - spec and plan committed;
  - dependency approval recorded;
  - each component PR opened or merged;
  - migration `089.001` rehearsed;
  - latency and calibration results recorded;
  - known divergences recorded;
  - composition repin;
  - final qualification.
- **FR-042**: Each wiki update MUST follow kos-wiki `CLAUDE.md`:
  - dated, provenance-tagged facts (`extrepo:AstralDeep/<repo>@<sha>`, `doc:089-typesafe-a8p-integration` with date, `conv:<date>` for owner directives);
  - a `## [YYYY-MM-DD] checkpoint | <summary>` entry in `log.md`;
  - `index.md` upkeep for any new or renamed page;
  - two-way cross-links;
  - contradiction callouts instead of silent overwrites;
  - distinction between local and qualified evidence.
- **FR-043**: Wiki updates MUST NOT contain TypeSafe or LLM key values or key prefixes, credential material, raw logs or evidence dumps, PHI, or prompt content. They MUST NOT credit an AI assistant as a contributor. Pushing the vault follows its pre-authorized commit-and-push rule; wiki work never modifies product repositories.

**Owner test credentials**

- **FR-044**: The implementer MAY configure the owner's own LLM and TypeSafe keys in the **local** candidate stack for testing, measurement and calibration (owner permission, 2026-09-17), subject to these rules:
  - Keys MUST be entered through the normal settings save path (encrypted at rest) or passed to the local bench script over standard input.
  - Keys MUST NOT be exported as `TYPESAFE_*` or LLM environment variables for Astral processes.
  - Keys MUST NOT be committed, written to spec, verification or wiki files, logged, or pasted into test fixtures.
  - They MUST NOT be used against any deployed or shared environment.
  - Once qualification ends, the owner's saved credentials MUST be removed from the local stack unless the owner asks to keep them.

**Data-sharing acknowledgment**

- **FR-045**: The per-user LLM credential surface (`projection_surfaces/llm.py`, both `render()` and `components()`, including the first-run mode) MUST show the data-sharing warning and an acknowledgment checkbox directly below the credential inputs and above the save actions. The wording is fixed by `contracts/typesafe-settings.md` and identified by a notice version.
- **FR-046**: Saving an LLM configuration (`chrome_llm_save` and the legacy `llm_config_set`) and saving a TypeSafe key (`chrome_typesafe_save`) MUST be rejected server-side, before any provider probe, unless the request acknowledges the notice or the user has already acknowledged the current notice version. Test connection, Load models and Remove actions MUST NOT require acknowledgment.
- **FR-047**: Acknowledgment MUST be persisted per user with notice version and timestamp in AstralPlane, audited without content, and shown as a pre-checked box with its date when the current version is acknowledged. A new notice version MUST require a fresh acknowledgment at the next save.
- **FR-048**: The acknowledgment requirement MUST NOT interrupt existing sessions or chat for users whose credentials predate it. It MUST be requested only at a credential save.
- **FR-049**: The checkbox MUST use the existing SDUI `boolean` field kind, so non-web clients receive it without client code changes. The web rendering MUST be keyboard-operable, labelled and associated with its warning text for screen readers (088 FR-028).

**Qualification**

- **FR-039**: CI results on AstralDeep, AstralProjection, AstralPlane, AstralPrimitives and LETS MUST NOT gate 089 work, and 089 MUST NOT modify CI workflow files. Qualification evidence MUST come from locally run tests, lint and measurements recorded in `verification.md`.

### Key Entities *(include if feature involves data)*

- **TypeSafe credential**: Per-owner encrypted API key with creation, update, last verification time and last verification outcome. Independent of the LLM configuration record.
- **Routing request**: The per-turn bounded state and question set built from the eligible tool set. Ephemeral and never persisted.
- **Routing decision**: The typed result: jailbreak probability, harm score, threat category, agent and tool choice with distributions, presentation style and derived tier. Ephemeral; summarized in audit and metrics.
- **Security verdict (additive)**: pass, confirm-tools or refuse, derived from the decision by calibrated local policy.
- **Routing circuit**: Per-owner, in-process failure counter and cool-down state. Not durable.
- **Presentation style and layout template**: Server-owned mapping from style to a deterministic arrangement of delivered component references.
- **New primitive types**: Six astralprims definitions with renderers, ROTE ladders and protocol dispositions.
- **Data-sharing acknowledgment**: Per-owner record of the acknowledged notice version and time. Independent of credential records, so it is not removed when a key or LLM config is cleared.
- **Web layout-parity inventory**: A weighted checklist of a8p console layout elements (regions, dimensions, placement, behaviors) used to score the web shell.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For users without a key, the p50 and p95 time from Send to first progress state and to first model token are within 5% of the pre-089 baseline on the same fixture set and environment.
- **SC-002**: For keyed users on the TypeSafe success path, the p95 added wait before the first model call is at most 150 ms over baseline. The median time from Send to first tool dispatch is no worse than baseline.
- **SC-003**: Under every injected TypeSafe failure mode, the maximum TypeSafe-attributable delay per turn is at most 1.5 s. 100% of turns either complete via fallback or end with an explicit failure message, with zero hung turns.
- **SC-004**: After the circuit opens, subsequent turns for that user add at most 5 ms of TypeSafe-attributable delay until the cool-down ends.
- **SC-005**: On the routing fixture set, high-tier decisions select a tool the reference labeling accepts in at least 95% of cases. Turns routed through TypeSafe complete the task at least as often as baseline.
- **SC-006**: On the benign reference corpus, the refusal tier's false-positive rate is at most 0.5% and the confirmation tier's is at most 3%. On the security benchmark corpus, refusal plus confirmation catches at least the proportion recorded as the calibration target.
- **SC-007**: Zero occurrences of TypeSafe key material in logs, audit events, rendered markup, components, error messages or test artifacts, verified by automated scans.
- **SC-008**: Run locally on the integrated candidate, the following pass:
  - the Deep, Projection (web and server portions), Plane and Primitives test suites, with ≥90% changed-code coverage;
  - Ruff;
  - the web ESLint configuration;
  - the CSP checks.
  Failures in native or Windows client drift guards caused solely by the manifest's new component types are recorded as the accepted known divergence and are not counted. CI outcomes are not considered (FR-039).
- **SC-009**: The redesigned web shell passes 088's keyboard, screen-reader, reduced-motion, 320px and 200%-text checks with no required control hidden.
- **SC-010**: The redesigned web shell scores at least 90% on the weighted layout-parity inventory at 1920×1080, 1440×900 and 1280×800. 100% of text elements compute to Open Sans at every tested viewport.
- **SC-012**: At 1024×768, 768×1024, 390×844 and 320×640, the web shell passes 100% of the responsive checklist items: no horizontal page scroll, no overlapping or clipped controls, all capabilities reachable, and every result component type legible.
- **SC-013**: kos-wiki `log.md` contains a dated 089 `checkpoint` entry for each of the nine task phases and each FR-041 milestone. At completion, `project-a8p`, `project-astral`, `astral-orchestrator`, `astral-llm-credential-resolution`, `astral-security-defense-layers`, `astral-adaptive-ui-designer`, `astral-primitives`, `astral-rote`, `astral-web-client`, `astral-feature-timeline` and `astral-open-follow-ups` reflect the final 089 state with provenance. A wiki lint pass reports no unresolved links or unprovenanced facts introduced by 089.
- **SC-014**: 100% of credential save attempts without a current acknowledgment are rejected before any provider request; 100% of acknowledged saves record the notice version and time; zero existing users are prompted during chat. Verified by tests and a local web walkthrough.
- **SC-011**: `git diff` from the 089 base shows zero changed files under `windows-client/`, `android-client/` and `apple-clients/`, and zero changed files under any `.github/workflows/` directory in all five repositories.

## Assumptions

- TypeSafe is treated like the user's own LLM provider: a processor the user chose by supplying a key. Content sent is limited by FR-038. If a deployment policy later forbids sending chat content to non-approved processors for some chats, TypeSafe is skipped for those turns.
- TypeSafe publishes no Choice option-count, state-size or latency limits. The feasible option bound and per-attempt timeout are measured in research before routing tiers are enabled.
- The TypeSafe API key prefix is not documented. The implementer derives the redaction token pattern from the owner's real key format during implementation, without recording the key. Dictionary-key redaction covers the field regardless.
- Constitution V (v3.0.0, amended 2026-09-17) permits any dependency without approval. `typesafe-sdk` is additionally owner-approved.
- The data-sharing checkbox is shown and required for every provider preset, including local runtimes, for one consistent rule. Its wording names third-party and TypeSafe models specifically.
- Native Android, Apple and Windows clients are out of scope and untouched. The owner's separate native rewrite may later adopt the new component types by declaring them in `supported_types`.
- Updating `contracts/ui_protocol.json` `component_types` makes the unmodified native and Windows drift-guard tests fail locally. The owner accepts this as a known divergence under the web-only directive, and runtime safety is provided by server-side ROTE down-conversion.
- a8p remains a standalone reference demo and is not deployed.
