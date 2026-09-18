# Research: TypeSafe Routing and a8p UI Integration

Date: 2026-09-17. Baselines: AstralDeep `e92db75d`, AstralProjection `447ac077`, AstralPlane `1aee0db3` (`088.008`), AstralPrimitives `8dadde18` (0.3.0), LETS `v1.0.11`, a8p `bcdc014`, `typesafe-sdk` 0.6.0.

Each decision lists what was chosen, why, and what was rejected. File references are to AstralDeep unless prefixed `P:` (AstralProjection at `components/AstralProjection`), `PL:` (AstralPlane), `PR:` (AstralPrimitives) or `a8p:`.

## Findings that shape the design

1. **The turn loop is still in `backend/orchestrator/orchestrator.py`.** 088 did not extract it.
   - Path: `handle_chat_message` → `_handle_chat_message_with_guidance` → `_handle_chat_message_impl` (~L15470).
   - Order: slash/onboarding intercepts, LLM preflight, `chat_status: thinking`, eligible tools via `tool_visibility.eligible_tool_pairs` (~L15816–16020), system prompt, `admit_task`.
   - ReAct loop `while turn_count < MAX_TURNS` (~L16342, `MAX_TURNS=10`), then `_call_llm` inside `perf_span("turn.route")` (~L16435).
   - `_call_llm` hard-codes `tool_choice="auto"` whenever tools are present (~L17591).
   - REST `POST /api/chats/{chat_id}/messages` (`api.py` ~L813) and background `async_mode` (`_dispatch_async_chat`, `VirtualWebSocket`) enter the same `handle_chat_message`.
2. **No classifier screens user input.** `supervisor.scan_ingress` (`orchestrator/supervisor.py:130`) has no production caller. All blocking security runs per tool call after the model responds, in `_run_gate_stack` (~L18968):
   system block → machine scope → identity claims → `is_tool_allowed` → destructive confirmation → policy engine → taint/egress → supervisor intent + HITL → credentials → disabled tool → RFC 8693 delegation → `PRE_TOOL_USE` hook → concurrency cap → `effect_refusal` → LETS governed dispatch.
3. **Current UI generation** has two layers:
   - Agents return astralprims objects (`create_ui_response`), which are delivered per round by `_deliver_round_components` (~L23572).
   - That path is upsert-first: flat components go out immediately. For web rounds with ≥2 components, `ui_designer` (`FF_UI_DESIGNER`, default ON) runs an LLM layout pass that lands later as an in-place refinement. Designer failure leaves the flat delivery, with no user-visible error.
   - The designer is skipped for native clients.
   - This is the "current UI generation method" that 089 falls back to.
4. **a8p's `presentation_style` is metadata only** (`a8p:astral/orchestrator.py` stores it in `routing_meta` and never uses it in rendering). a8p's actual UI generation is deterministic per-tool component construction plus server HTML. So "adopt a8p UI generation" means:
   - (a) the richer primitive vocabulary, and
   - (b) making the style decision actually drive a deterministic arrangement.
5. **Per-user LLM credentials** are encrypted with Fernet in Deep (`llm_config/user_store.py`, `CREDENTIAL_ENCRYPTION_KEY`) and stored as ciphertext in Plane (`PL:repositories/secrets.py`, `EncryptedLLMConfigRepository`, table `user_llm_config`).
   - The settings surface is `orchestrator/projection_surfaces/llm.py`, which renders both web `render()` and native `components()`.
   - Saves run as probe-gated durable operations.
   - The first-run gate predicate `llm_configured_for` (~L17276) is "a user LLM record exists".
6. **Secret hygiene is name-bound.**
   - `log_scrub.redact_llm_config` redacts only the dict key `api_key`.
   - `audit_events._assert_no_api_key` rejects only the literal `api_key` key.
   - `backend/tests/test_llm_env_inert.py` scans for LLM env reads.
7. **`typesafe-sdk` 0.6.0 facts** (installed package source):
   - `AsyncTypeSafeClient(api_key, model, retry, timeout, base_url, http_client, …)`, with a per-call `timeout` and `retry` on `system_one`.
   - Default timeout 10 s.
   - `RetryPolicy` defaults to `max_retries=2`, backoff 0.5 s→5 s, retrying 408/429/5xx plus connection and timeout errors; `RetryPolicy(max_retries=0)` disables it.
   - Typed errors: `TypeSafeAuthenticationError`, `TypeSafePermissionDeniedError`, `TypeSafeUnprocessableEntityError`, `TypeSafeRateLimitError`, `TypeSafeInternalServerError`, `TypeSafeAPIConnectionError`, `TypeSafeAPITimeoutError`, `TypeSafeAPIResponseValidationError`.
   - `client.models.list()` exists.
   - **It silently reads `TYPESAFE_API_KEY`, `TYPESAFE_BASE_URL` and `TYPESAFE_DEFAULT_MODEL` from the environment when arguments are omitted.**
   - Requires `httpx2`, `msgspec`, `tenacity`, `typing-extensions`. Python ≥3.10.
   - API: `POST /v1/systemone`, bearer auth, model `jev-latest`, errors 401/422/429/529; docs recommend exponential backoff on 429/529.
   - The docs publish no Choice option limit, state-size limit or latency figures.
8. **The ROTE fallback ladder** (`P:backend/rote/fallback.py`) substitutes unsupported types per client `supported_types`, bottoming out at `text`. Native clients declare their supported types at registration. New types that are never declared by a native client will only ever reach it as a fallback, with no client change required at runtime.
9. **The theme is server-owned.** `ThemeView` is in `P:src/astralprojection/models.py`, and `ui_protocol.json` declares `"theme_source": "server_semantic_palette"`. Fonts are shared via `contracts/assets/fonts`, which native clients consume. The web static fonts are in `P:backend/webrender/static/fonts/`.
10. **a8p console security issues** (do not port code verbatim):
    - inline `onclick`/`oninput` handlers, blocked by Astral's nonce CSP;
    - `sendPrompt('{html.escape(label)}')` JS-context injection;
    - unescaped `css` and color values in `style`/SVG attributes;
    - unescaped `innerHTML` of agent names and descriptions;
    - external Google Fonts, blocked by `font-src 'self'`;
    - `to_dict` fails on nested non-Primitive models.

## Decisions

### R1 — One routing seam, placed before the reasoning loop

**Decision**: Add a routing stage in `_handle_chat_message_impl`.
- Start it as an `asyncio.Task` immediately after `eligible_tool_pairs` returns (~L15881), so it runs concurrently with prompt, history, file-map and workspace preparation.
- Await it with the remaining budget just before the first `_call_llm` in round 1 (~L16435).
- Pass the decision into round 1 only: narrowed `tools_desc` plus a `tool_choice` override through a new keyword argument on `_call_llm`. This is not a context variable, so the override cannot leak across concurrent turns.

**Rationale**: At this point the exact prefixed tool names the LLM will see are known. The existing gates are untouched downstream, and concurrency turns most of the TypeSafe latency into overlap with work that already happens.

**Rejected**:
- Inside `_call_llm` only: it runs every round and knows nothing about turn identity.
- Before eligibility filtering: this would route to tools the user cannot use.
- Extracting a new turn service first: this would couple 089 to a large refactor of a 26k-line file. The seam is a small, well-bounded module call.

### R2 — Adapter module and SDK usage

**Decision**: New package `backend/orchestrator/typesafe_routing/` with:
- `client.py` — per-user client factory;
- `questions.py` — question and state builder;
- `decision.py` — typed decision and tiering;
- `security_policy.py` — additive verdict;
- `budget.py` — attempts, deadline, circuit;
- `layout.py` — style templates;
- `store.py` — encrypted credential store facade.

Use `AsyncTypeSafeClient` constructed per call with `api_key=<decrypted user key>`, `base_url=TYPESAFE_API_BASE` (a code constant, `https://api.typesafe.ai`), `model="jev-latest"` (a code constant), `retry=RetryPolicy(max_retries=0)` and `timeout=<per-attempt>`. The shared `httpx2.AsyncClient` is owned by the adapter for connection reuse, with the per-call timeout overriding it. The base URL is validated once at import against `shared.external_http.validate_egress_url`.

**Rationale**: Explicit arguments remove the SDK's environment fallback. Disabling SDK retries keeps the 1.5 s budget in one place.

**Rejected**:
- Raw HTTP via `shared.external_http`: the owner chose the SDK, and typed errors and schemas reduce custom code.
- A module-level singleton client with a key: it would cross users.

### R3 — Environment-key exclusion (owner's key never used live)

**Decision**: Four layers of protection:
1. **Production-posture boot check**: startup refuses when `TYPESAFE_API_KEY`, `TYPESAFE_BASE_URL` or `TYPESAFE_DEFAULT_MODEL` is set. The check sits next to the existing posture checks in `orchestrator/session_store.py`.
2. **Non-production**: the same variables log a warning and are **still ignored**, because the adapter always passes explicit values.
3. **Regression test**: extend `backend/tests/test_llm_env_inert.py` with a behavioral test (env set, user without key ⇒ no TypeSafe request) and a source scan for `TYPESAFE_` env reads.
4. **Sandbox**: add the variables to the sandbox child-process env denylist (`orchestrator/sandbox.py`).

**Rationale**: FR-005. Defense in depth against the SDK's implicit environment reads and against a developer `.env` leaking into a deployed image.

### R4 — Question design over dynamic, per-user tool sets

**Decision**: Use a single `system_one` request that combines a security core, a target-agent Choice, tool fan-out and a presentation Choice.

**Security core** (always asked):
- `is_jailbreak` — Noul;
- `harm_score` — Score, 4 levels, adapted from `a8p:_build_fanout_questions`;
- `threat_category` — Choice: none, data_egress, code_execution, credential_access, destructive, privilege_escalation, network_manipulation.

**Target agent**: `target_agent` — Choice over the distinct agents present in the eligible set, plus `no_tool_needed` ("conversational reply, clarification or greeting without running a tool"). Criteria text comes from each agent card's name and description, truncated to a bounded length.

**Tool fan-out** (speculative, per the TypeSafe fan-out pattern): one `tool_for_<agent>` Choice per eligible agent, over that agent's eligible tools with their descriptions, plus `none_fit`. Only the chosen agent's answer is consumed; the others are ignored.

**Presentation**: `presentation_style` — Choice: dashboard, detailed_table, alert_focused, conversational, as_delivered.

**State**:
```json
{
  "current_request": "...",
  "recent_conversation": [...],
  "active_agent": "...",
  "user_selected_tools": [...]
}
```
Question instructions reference `current_request` by backticked path.

**Size bound**: `MAX_ROUTING_AGENTS` and `MAX_TOOLS_PER_AGENT` start as provisional values and are set from the T015 measurement spike.
- If the eligible set exceeds the bound, the agent Choice is kept and tool questions are asked only for the agents that remain within the bound. Agents are kept in priority order: the active agent first, then user-selected tools' agents, then the rest in stable catalog order.
- If the chosen agent's tool question was not asked, the decision tier is at most medium, with the shortlist being that agent's tools (capped at `SHORTLIST_MAX`).

**Rationale**: One request (FR-007). Fan-out avoids a second round trip. A per-agent Choice keeps each option list small and uses TypeSafe's hierarchical classification guidance without a sequential second call.

**Rejected**:
- Flat Choice over all tools: option counts are unbounded and unmeasured.
- Two-stage agent-then-tool requests: they double latency against a 1.5 s budget.

### R5 — Consumption tiers

**Decision**: Define tiers from the agent and tool distributions.

Let `pa` = probability of the chosen agent, `pt` = probability of the chosen tool within that agent's question, and `margin` = gap to the second-best tool.

| Tier | Condition (provisional; calibrated in T028) | Round-1 effect |
|---|---|---|
| high | `pa ≥ 0.80`, `pt ≥ 0.75`, `margin ≥ 0.30`, tool ∈ eligible | `tools_desc = [chosen]`; `tool_choice` forced if provider supports it, else `"auto"` |
| medium | `pa ≥ 0.55` and top-k tools with cumulative p ≥ 0.80 exist in eligible | `tools_desc = shortlist (≤ SHORTLIST_MAX=6)`; `"auto"` |
| low | anything else, `no_tool_needed`, `none_fit`, invalid option, or missing answer | unchanged |

Rounds ≥2 are always unchanged.

**Provider support**: Forced `tool_choice` support is decided from the resolved provider preset (`llm_config/providers.py`). Unknown or custom providers use `"auto"`. If a forced call errors with a provider rejection, `_call_llm` retries once with `"auto"` on the same narrowed list, before its own retry logic counts the attempt.

**Rationale**: FR-009 and FR-010. Degrading to unchanged behavior on doubt protects the user's flow, and multi-step tasks keep full capability after round 1.

### R6 — Latency budget, retries, notices and circuit

**Decision**:
- **Hard deadline**: 1,500 ms from routing-task start. `asyncio.timeout` wraps the whole attempt sequence.
- **Attempts**: 3. Per-attempt timeout is `min(400 ms, remaining)`. Backoff before attempts 2 and 3 is 100 ms and 200 ms, ±20% jitter, clipped to the remaining budget.
  - Worst case: 400 + 100 + 400 + 200 + 400 = 1,500 ms.
  - The per-attempt timeout is provisional and set from the T015 p99 measurement. It never exceeds `(1500 − 300) / 3`.
- **Retryable errors**: `TypeSafeAPITimeoutError`, `TypeSafeAPIConnectionError`, `TypeSafeRateLimitError`, `TypeSafeInternalServerError`, and API errors with status 408, 5xx or 529.
  - A `Retry-After` value is honored only if it fits the remaining budget; otherwise the sequence stops.
- **Non-retryable errors**: authentication (401), permission (403), unprocessable (422), response validation, and SDK import or configuration error.
  - These fall back immediately.
  - 401 and 403 also persist `last_verification_outcome = rejected` on the credential record (off the hot path, via a background task) for FR-006.
- **Notices** (non-blocking, through `_send_chat_status`, which also reaches `VirtualWebSocket`):
  - after the first failed attempt: `status="retrying"`, message "Smart routing is slow to respond — retrying…";
  - on fallback: `status="info"`, message "Using standard routing for this request."
  - Notices never create chat messages or persist to history.
- **Circuit**: an in-process per-user record.
  - After 3 consecutive turn-level fallbacks, it opens for 5 minutes. While open, TypeSafe is skipped with zero network calls, and one `info` notice is shown on the first skipped turn.
  - After the cool-down, the next turn is a half-open trial. Success closes the circuit; failure re-opens it.
  - A non-retryable authentication failure opens the circuit until the key is changed or removed.
- **Cancellation**: the routing task is cancelled when the turn is cancelled or the socket drops, using the existing `cancelled_sessions` check and task cancellation.
- **Total failure**: if the standard path then fails, the existing `_call_llm` failure handling and error surfaces apply unchanged, and the turn ends with an explicit message.

**Rationale**: FR-014 to FR-020, and the owner's explicit sequence: notice, 3 tries with backoff, fallback, then inform and stop. The circuit is what makes "no hiccups" true during a sustained outage.

**Rejected**: SDK retries, which have a 0.5 s initial backoff and default 10 s timeout and would blow the budget. An unbounded `Retry-After` wait.

### R7 — Additive security policy

**Decision**: `security_policy.evaluate(decision) → pass | confirm_tools | refuse`.

- **refuse**: `jailbreak ≥ T_refuse_j` **and** `harm ≥ T_refuse_h`, or `threat ∈ {credential_access, network_manipulation}` with `jailbreak ≥ T_refuse_cat`.
  - The turn short-circuits before round 1 with the existing refusal rendering (`Alert`, `variant="error"`) and a `chat_status` done.
- **confirm_tools**: `jailbreak ≥ T_confirm_j` **or** `harm ≥ T_confirm_h` **or** `threat ∈ {destructive, data_egress, code_execution, privilege_escalation}`.
  - The verdict is carried on the turn context, and `_run_gate_stack` consults it at the supervisor + HITL step (~L19239).
  - It forces the existing human-approval path for every tool call in the turn. It can only turn "allowed" into "requires approval", and never the reverse.
- **pass**: nothing changes.

**Provisional thresholds** (a8p's values): `T_refuse_j=0.70`, `T_refuse_h=2.5`. Final values are set by calibration T036 against `backend/security_benchmark/` plus a benign reference corpus (routing fixtures + 088 reference journeys), meeting SC-006.

**Before calibration is recorded**, the refuse tier is disabled (`TYPESAFE_REFUSE_TIER_ENABLED` constant false) and refuse-level verdicts are downgraded to confirm_tools. This is not a rollout mechanism. It keeps an uncalibrated model from blocking legitimate users.

**Audit**: each non-pass verdict appends an audit event `typesafe.security_verdict` with tier, threat category, rounded probabilities, chosen agent and tool IDs, latency and turn ID. There is no text. The event is recorded via the existing hash-chained audit repository and checked by `_assert_no_api_key`.

**Rationale**: FR-021 to FR-024. The verdict comes from the same request, so it adds no network cost. Confirmation reuses the existing approval UX, so there is no new interruption pattern.

### R8 — Credential storage in Plane

**Decision**: Add a new Plane table `user_typesafe_credential` in guarded revision `089.001`, with repository `EncryptedTypeSafeCredentialRepository` alongside `EncryptedLLMConfigRepository` in `PL:repositories/secrets.py`. The Deep store `llm_config/typesafe_store.py` uses the same Fernet resolver.

**Why a separate table rather than a column on `user_llm_config`**:
- clearing the LLM config deletes that row and re-gates the user;
- the TypeSafe key must survive LLM provider changes;
- a TypeSafe key must never create a `user_llm_config` row, which would falsely satisfy the first-run gate.

**Rationale**: FR-002 and FR-004, following Plane's migration process (see data-model.md).

### R9 — Settings surface integration

**Decision**: Extend `projection_surfaces/llm.py`:
- a "TypeSafe routing (optional)" section in `render()` and `components()`, hidden when `params.first_run` is true;
- new chrome actions `chrome_typesafe_save` and `chrome_typesafe_clear`;
- handlers in new `llm_config/typesafe_handlers.py`, registered as durable credential operations like `_LLM_CREDENTIAL_SAVE_ACTIONS`.

**Verification on save**: `client.models.list()` with a 5 s timeout and no retries, behind `_check_probe_rate` (this also closes the existing gap where the LLM save probe is not rate-limited, for TypeSafe saves).

**Not gate-relevant**: neither action appears in `llm_gate` unlock or re-gate paths, and neither is added to the gated-state allowed actions.

**Rationale**: FR-001 to FR-006. The server-owned surface reaches native clients through existing SDUI with no native client change.

### R9a — Data-sharing acknowledgment on the LLM credential page

**Decision**: Add a warning block and a required acknowledgment checkbox directly below the credential inputs in `projection_surfaces/llm.py`, in `render()`, `components()` and first-run mode.
- **Rendering**: web uses a labelled `<input type="checkbox">` tied to the warning with `aria-describedby`. Native clients get the existing SDUI `field(kind="boolean")`, so no client change is needed.
- **Enforcement**: a single `llm_config/data_sharing.require_acknowledgment(user_id, submitted_flag)` runs first in `chrome_llm_save`, `llm_config_set` and `chrome_typesafe_save`, before any provider probe. It returns a field error when the flag is not set and the user has no acknowledgment of `NOTICE_VERSION`.
- **Storage**: when the flag is set, the acknowledgment is upserted in Plane table `user_data_sharing_acknowledgment` (same `089.001` migration) and audited as `llm_data_sharing.acknowledged`.
- **Scope of prompting**: chat, routing and existing sessions never consult it (FR-048).
- **Notice versioning**: `NOTICE_VERSION` is a code constant (for example `"2026-09-17.1"`). Changing the wording requires bumping it, and a test pins the text to the version.

**Rationale**: owner directive; FR-045 to FR-049. Server-side enforcement makes the rule hold for every client. A separate table keeps the acknowledgment when a credential is cleared.

**Rejected**:
- A client-only required checkbox: bypassable, and it diverges across clients.
- Storing the acknowledgment on `user_llm_config`: it would be deleted on clear, and it cannot cover a TypeSafe-only save.
- Prompting existing users mid-chat: it violates the no-interruption requirement.

### R10 — Secret hygiene extensions

**Decision**:
- `log_scrub.redact_llm_config` redacts dict keys matching `api_key` **or ending in `_api_key`**.
- `_KEY_TOKEN_PATTERNS` adds a TypeSafe key token pattern that the implementer derives from the owner's real key format during T007. Only the pattern is recorded, never the key.
- `audit_events._assert_no_api_key` rejects any key ending in `api_key`.
- `.gitleaks.toml` gains a TypeSafe rule.
- `verification/config.py` `OTHER_SECRET_ENV_NAMES` adds the `TYPESAFE_*` names.
- The credential dataclass `__repr__` hides the key.

**Rationale**: SC-007.

### R11 — UI generation: style-driven deterministic layout

**Decision**: In `_deliver_round_components`, after the existing upsert-first flat delivery:
- if the turn has a TypeSafe `presentation_style` other than `as_delivered`, and the socket is web (same device policy as the designer), call `typesafe_routing.layout.compose(style, components)`. It returns a designer-compatible layout (the same `ref` node format `ui_designer` produces) using only delivered component references;
- validate it with the designer's existing layout validator and lint, and apply it through the same refinement send.

**Fallback**: if `compose` returns `None` or validation fails, run `_run_designer` exactly as today. If the designer fails, the flat delivery stands, as today. Only if flat delivery itself fails is the existing error surfaced and the turn stopped.

**Templates** (a8p-derived):

| Style | Arrangement |
|---|---|
| `dashboard` | hero/alert first; `metric`/`stat_group`/`gauge` in a grid row; charts in a 2-column grid; tables and lists full width |
| `detailed_table` | tables and keyvalue first, full width; charts collapsed below |
| `alert_focused` | alerts and badges first; everything else in a collapsible |
| `conversational` | text/card first; supporting components stacked |

Components are classified by type, never by content.

**Rationale**: FR-027 to FR-029. It removes the LLM designer wait for keyed users and preserves the refinement contract.

**Rejected**: sending a8p server HTML, which violates Principle II and the sanitizer; and replacing the designer for unkeyed users, which the owner's fallback directive forbids.

### R12 — Six new primitives, web-only rendering, server-side down-conversion

**Decision**: Add the types to AstralPrimitives 0.4.0 using astralprims conventions (`content` children, `variant`, value ranges consistent with existing types):

| Type | Shape (summary) | ROTE ladder |
|---|---|---|
| `action_group` | `buttons: [Button]`, `align` | `container`, `text` |
| `stat_group` | `items: [{label, value, delta?, trend?, hint?}]`, `columns` | `grid`, `keyvalue`, `table`, `text` |
| `gauge` | `value` 0–1, `label`, `thresholds?`, `unit?` | `progress`, `metric`, `text` |
| `pipeline_stepper` | `steps: [{label, status, detail?}]` status ∈ done/active/pending/error | `timeline`, `list`, `text` |
| `donut_chart` | `labels`, `data`, `center_label?` | `pie_chart`, `table`, `list`, `text` |
| `radar_chart` | `axes`, `datasets: [ChartDataset]`, `max?` | `table`, `list`, `text` |

Web renderers in `P:backend/webrender/renderer.py` produce escaped inline SVG, attribute-allowlisted, with no `style` emission from component data and colors taken from theme CSS variables.

`ui_protocol.json` `component_types` gains the six names, and `disposition_matrix_088`'s server-side counterpart records them as `supported` on web and `omitted_by_server` with fallback on all native and Windows targets.

**Web-only directive**: no files under `windows-client/`, `android-client/` or `apple-clients/` change.
- ROTE down-conversion is guaranteed because none of those clients declares the new types in `supported_types`.
- Their manifest drift-guard tests will fail locally against the new `component_types` list. This is an accepted known divergence (spec Assumptions), recorded in verification.md.

**Also fixed**: the web `bar_chart` currently draws only the first dataset (`P:renderer.py` ~L678). Multi-dataset rendering is added because a8p's charts rely on it. This is a web-only renderer change.

**Rationale**: Constitution VIII (define in astralprims first), FR-025, FR-026 and FR-034.

### R13 — Web shell: a8p layout, Open Sans, ThemeView colors

**Decision**: Rebuild `P:backend/webrender/templates/shell.html`, `static/astral.css` and the shell portions of `static/client.js` to the a8p layout, scored against `contracts/web-layout-parity.md`.

**Region mapping** (a8p → 088 capability):

| a8p region | Web shell content |
|---|---|
| sidebar brand | home/new chat |
| Agent Directory + filter | server-owned agent list from the existing agents chrome, rendered with `textContent` |
| profile widget + cog | the existing server-owned Settings menu (`menu_model`) opens from the cog |
| landing header/overview/scenario cards + filter tabs | empty state; example scenarios come from the existing welcome content (`orchestrator/welcome.py`), not a8p's hard-coded 12 |
| chat feed + response cards with expand chip | conversation + result components |
| full-screen overlay | full-screen result view |
| composer bar | the single 088 primary composer, including attach/background/Advanced/voice controls placed inside the bar |
| settings modal (tabbed) | the existing `#astral-modal` chrome surface host restyled to a8p's modal (header icon badge, tabs where the surface declares sections) |
| recent work | sidebar section below the agent directory |

**Typography**: Open Sans WOFF2 (weights 400, 500, 600, 700, 800; latin subset, SIL OFL with license file) under `P:backend/webrender/static/fonts/`, with `@font-face` in `astral.css`. `--astral-font` is Open Sans for all text including code. Inter and JetBrains Mono references are removed from the web shell. `contracts/assets/fonts` is unchanged.

**Colors**: a8p hex values are not ported. a8p roles map to `ThemeView` roles via the existing `--astral-*` CSS variables:

| a8p role | Theme variable |
|---|---|
| base | `--astral-bg` |
| surface | `--astral-surface` |
| accents | `--astral-primary` / `--astral-accent` |
| text | `--astral-text` / `--astral-muted` |

Status colors use the existing state/severity roles. Gradients (radial canvas background, send-button gradient) are expressed with `color-mix()` over theme variables.

**Script hygiene**: every a8p inline handler becomes an `addEventListener` binding in `client.js`. Untrusted strings use `textContent`. Global functions are removed. The file follows the existing ES5 IIFE convention and passes `P:tooling/web-ci` ESLint locally.

**Parity scope**: The 90% parity target applies **only to desktop viewports ≥1280 CSS px**, scored at 1920×1080, 1440×900 and 1280×800. The a8p reference is desktop-only; it has no rule that collapses its fixed 350px sidebar.

**Responsive web layout (<1280px)**: This is new design work derived from the a8p language rather than copied from it, with three breakpoints:

| Viewport | Sidebar | Main area |
|---|---|---|
| 1024–1279px | narrows to 288px | canvas padding 24px; scenario grid 2 columns |
| 768–1023px | off-canvas drawer opened from a top-bar toggle, with focus trap and Esc to close | composer full width; response cards single column; full-screen overlay becomes a full-viewport sheet |
| <768px | drawer | composer pinned to the bottom above the safe-area inset; composer secondary controls collapse into an overflow menu; settings modal becomes a full-screen sheet with tabs as a horizontal scroller; landing overview collapses to a single column and scenario filters become a scrollable chip row |

Every breakpoint preserves 088 FR-028 (keyboard, focus, 200% text, reduced motion) and is verified by the responsive checklist in `contracts/web-layout-parity.md`.

**ROTE web tuning** (server-side, `P:backend/rote/`): Tune only the web profiles `browser`, `tablet` and `mobile`.

- **Guardrail**: `capabilities.py` shares the viewport-based grid cap with the native `android`/`ios`/`macos` profiles (088). Every 089 ROTE change is therefore keyed to those three web device types, and golden tests assert byte-identical adapter output for `windows`, `android`, `ios`, `macos`, `watch`, `tv` and `voice` profiles before and after. This satisfies the web-only directive at the server layer too.
- **New-type adaptation rules** in `adapter.py`:
  - `stat_group` caps columns at `max_grid_columns`;
  - `gauge` renders a compact arc below 480px;
  - `donut_chart` and `radar_chart` below 700px fall back to the legend-plus-values table form, consistent with `_adapt_chart`'s existing viewport rule;
  - `pipeline_stepper` switches to vertical orientation below 768px;
  - `action_group` wraps buttons and caps visible actions on `mobile`, with overflow as a menu.
- **Existing-type tuning for the a8p look on tablet and mobile browsers**: response-card grid density, table column caps (`tablet` `max_table_cols=6`, `mobile` 4 retained), chart height classes, and hero metric-strip wrapping. Values are adjusted only where the responsive checklist fails.
- **Profile selection**: confirm the web client reports viewport width and device type on register and resize, as 088 already does for grid density, so ROTE re-adapts on orientation change. The existing re-render-on-device-change path is `orchestrator.py` `render_for_target`/`target_for_profile`.

**Rationale**: owner directives (desktop parity only; responsive and ROTE work for other screens; web client only), FR-030, FR-030a, FR-031 to FR-034, Constitution II (server-driven shell assets) and XII (colors from the shared definition).

### R14 — Dependency pin and transitive impact

**Decision**: Pin `typesafe-sdk==0.6.0` in `backend/requirements.txt`. Transitive additions: `httpx2`, `msgspec`, `tenacity`, `typing-extensions` (already present). The owner approved the SDK on 2026-09-17 and amended the constitution to v3.0.0, so any dependency may be installed without an approval gate (Principle V). The PR description names the dependency and its purpose.

Import is lazy inside `typesafe_routing/client.py`. An `ImportError` marks TypeSafe unavailable, and turns use the standard path with no boot failure (spec edge case).

### R15 — CI excluded, local qualification

**Decision**: Per owner directive, no CI workflow file is modified in any of the five repositories, and CI outcomes do not gate 089. Evidence is recorded in `verification.md` from local runs:
- `docker exec astraldeep … pytest` with coverage;
- Plane `uv run pytest`;
- Primitives `pytest`;
- Projection `pytest` (server/web tests) and `tooling/web-ci` ESLint + Playwright;
- `ruff check`.

### R16 — Measurement spike (prerequisite to enabling tiers)

**Decision**: Before routing tiers are wired (T022+), run `scripts/verification/typesafe_routing_bench.py`, a new local-only script that uses the owner's own TypeSafe key, which the owner permitted for local testing (FR-044), passed over stdin (never env, never committed, never logged) over:
- the routing fixture corpus (T014);
- the real bundled agent catalog;
- a synthetic large catalog (60 agents × 8 tools).

It records p50/p95/p99 latency per question count, option-count sensitivity, and tier accuracy. The outputs set `ATTEMPT_TIMEOUT_MS`, `MAX_ROUTING_AGENTS`, `MAX_TOOLS_PER_AGENT` and the tier thresholds, recorded in `verification.md`.

**Rationale**: TypeSafe publishes no limits or latency (Assumptions). The owner's lag requirement needs measured values, not guesses.
