# Tasks: TypeSafe Routing and a8p UI Integration

**Input**: `specs/089-typesafe-a8p-integration/` — spec, plan, research, data-model, contracts.

**Tests**: Required by the spec and Constitution III. They are run **locally**; CI is excluded by owner directive (FR-039).

**Format**: `[ID] [P?] [Story] Description`. `[P]` means the task touches disjoint files within its phase after prerequisites.

**Path prefixes**:
- `D:` AstralDeep root;
- `P:` `components/AstralProjection` (developed in `Y:\WORK\MCP\AstralProjection`);
- `PL:` AstralPlane;
- `PR:` AstralPrimitives.

**Knowledge base (all phases)**: `K` tasks read and update kos-wiki (`Y:\WORK\kos-wiki`) per the plan's "Knowledge base cadence" and spec FR-040 to FR-043.
- A phase does not start until its `K` reference task is done, and is not closed until its `K` checkpoint task is done.
- Milestone updates (K19) happen whenever a milestone occurs, and at least once per working day on which 089 work lands.
- All wiki writes follow the vault's `CLAUDE.md` schema: provenance tags, a dated `log.md` `checkpoint` entry, `index.md` upkeep, contradiction callouts.
- Wiki writes never include key values or prefixes, credentials, raw evidence, PHI or AI contributor credit.
- The vault is committed and pushed under its pre-authorized rule.

**Scope guards that apply to every task**:
- No changes under `P:windows-client/`, `P:android-client/`, `P:apple-clients/`.
- No changes to any `.github/workflows/` in the five repositories.
- No environment-supplied TypeSafe configuration.
- No commit credits Claude.

---

## Phase 1: Setup

- [X] K01 Reference kos-wiki before Setup: `index.md`, `project-a8p`, `project-astral`, `synthesis-astral-rewrite-review`, `astral-open-follow-ups`, `astral-ci-gates`, `astral-feature-timeline`; note any wiki/code disagreements for K02.
- [X] T001 Create `specs/089-typesafe-a8p-integration/verification.md` with the following sections, populated at the start:
  - exact baseline SHAs of all five repositories and a8p;
  - the 081–087 supersession record;
  - the owner exceptions (CI ignored, web-only, desktop-only parity);
  - the known-divergence register (native and Windows drift guards);
  - a local evidence log template.
- [X] T002 Add `D:scripts/check_089_scope.py`. It reports changed files between the 089 base SHAs and HEAD for all five repositories, fails on any path under the client directories or `.github/workflows/`, and records its command in verification.md. Also confirm and record the local toolchains:
  - Deep: `docker compose up -d`, `docker exec astraldeep … pytest`;
  - Plane: `uv run pytest`;
  - Primitives: `pytest`;
  - Projection: `pytest`, `tooling/web-ci` ESLint and Playwright;
  - `ruff check`.
- [X] T003 Pin `typesafe-sdk==0.6.0` in `D:backend/requirements.txt` (owner-approved 2026-09-17; no approval gate under Constitution V v3.0.0). Rebuild the container, confirm that `httpx2`, `msgspec` and `tenacity` install, and record the resolved versions in verification.md.
- [X] T003a Record the owner-test-credential procedure in verification.md without any key values (FR-044). At test time, the owner's LLM and TypeSafe keys are read from the owner-designated local source (their a8p `.env`, or as the owner directs). They are entered only through the local web LLM settings save path, or piped over stdin to the bench script. They are never exported as `TYPESAFE_*` or LLM env vars for Astral processes, never written to repository, verification or wiki files, never logged, used only against the local candidate stack, and removed by T070.
- [ ] T004 Add `perf_span` markers `turn.first_llm_call_start` (just before round-1 `_call_llm`) and `turn.first_tool_dispatch` in `D:backend/orchestrator/orchestrator.py` (the latter is a no-op marker). Capture baseline p50/p95 over 200 scripted-LLM turns and 50 real-provider turns on the routing fixture prompts, measuring Send→thinking, Send→first LLM call and Send→first tool dispatch. Record the results in verification.md (SC-001/SC-002 baseline).

- [X] K02 kos-wiki checkpoint (Setup + spec/plan milestone): create `wiki/astral-typesafe-routing.md` (type synthesis) stub with scope, owner directives (`conv:2026-09-17`) and baseline SHAs; update `project-a8p` status/Timeline, `project-astral` Components/Timeline, `astral-feature-timeline`, `astral-ci-gates` (089 CI exception); record baseline latency summary from T004; `index.md` entry; `log.md` `## [date] checkpoint | 089 setup — spec/plan committed, baselines recorded`; commit + push vault.

---

## Phase 2: Foundational (blocks all user stories)

- [X] K03 Reference kos-wiki before Foundational: `astral-primitives`, `astral-primitive-serialization-contract`, `astral-llm-credential-resolution`, `astral-feature-flags`, `astral-project-constitution`.
- [X] T005 [P] PR: Add `ActionGroup`, `StatGroup`, `Gauge`, `PipelineStepper`, `DonutChart` and `RadarChart` to `PR:src/astralprims/primitives.py` per `contracts/ui-primitives-089.md`. Also:
  - export them and register them for `from_dict` in `PR:src/astralprims/__init__.py`;
  - document shapes in `PR:README.md`;
  - add round-trip, default and nested-serialization tests to `PR:tests/test_primitives.py`;
  - bump `PR:pyproject.toml` to `0.4.0` and regenerate the contract digest.
- [X] T006 [P] PL: Implement migration `089.001` with both tables, plus `EncryptedTypeSafeCredentialRepository` and `DataSharingAcknowledgmentRepository` (`PL:src/astralplane/repositories/preferences.py`), per data-model §1, §2 and §2a:
  - files: `PL:src/astralplane/database/typesafe_credential_schema.py`, `migrations.py`, `revision.py`, `baseline.py`, `repositories/secrets.py`;
  - tests: upgrade from a populated `088.008` fixture, repeat no-op, tampered-structure rejection, rollback rehearsal, owner isolation, fingerprint-conditional `record_outcome`, and acknowledgment upsert preserving `first_acknowledged_at`, in `PL:tests/test_schema_migrations.py` and `PL:tests/repositories/test_typesafe_credential.py`;
  - docs: update `PL:docs/migration-and-recovery.md` and `PL:provenance/transformations.json`.
- [X] T007 [P] Extend secret hygiene:
  - `D:backend/llm_config/log_scrub.py`: redact any dict key equal to or ending in `api_key`, and add the TypeSafe token regex;
  - `D:backend/llm_config/audit_events.py`: `_assert_no_api_key` rejects `*api_key`;
  - `D:.gitleaks.toml`: add a TypeSafe rule;
  - `D:backend/verification/config.py`: add `OTHER_SECRET_ENV_NAMES`.

  The TypeSafe token pattern is derived by the implementer from the owner's real key format under T003a. Only the pattern is committed, never the key. Tests use a synthetic canary key matching the pattern.
- [X] T008 [P] Implement environment exclusion (FR-005, research R3):
  - production-posture startup refuses when `TYPESAFE_API_KEY`, `TYPESAFE_BASE_URL` or `TYPESAFE_DEFAULT_MODEL` is set, in `D:backend/orchestrator/session_store.py`;
  - non-production logs a warning;
  - add the names to the sandbox env denylist in `D:backend/orchestrator/sandbox.py`;
  - extend `D:backend/tests/test_llm_env_inert.py` with a behavioral test (env set + no user key ⇒ zero TypeSafe requests) and a source scan for `TYPESAFE_` environment reads.
- [X] T009 Add `D:backend/llm_config/typesafe_store.py` (after T006), implementing Fernet encrypt/decrypt via `_resolve_fernet`, `get_key`, `status`, `save`, `clear`, `record_outcome_async` and undecryptable-row discard with audit. Tests go in `D:backend/llm_config/tests/test_typesafe_store.py`.
- [X] T010 [P] Add `D:backend/orchestrator/typesafe_routing/client.py`:
  - lazy `typesafe_sdk` import (`ImportError` ⇒ unavailable);
  - `TYPESAFE_API_BASE` and `TYPESAFE_MODEL` code constants, with the base URL validated through `shared.external_http.validate_egress_url`;
  - explicit `api_key`/`base_url`/`model`, `RetryPolicy(max_retries=0)`;
  - an adapter-owned shared `httpx2.AsyncClient` closed on shutdown.

  Tests use a mocked transport and assert that no environment variable is read (`monkeypatch` sets `TYPESAFE_*` to sentinels).
- [X] T011 [P] Add `D:backend/orchestrator/typesafe_routing/budget.py`: monotonic deadline, 3 attempts, per-attempt timeout, 100/200 ms jittered backoff clipped to the remaining budget, transient/non-transient classification, bounded `Retry-After` handling, and the per-user circuit (closed/open/half-open, auth-block by fingerprint, idle eviction). Tests use a fake clock covering every error class in `contracts/typesafe-routing.md`.
- [X] T012 Add `D:backend/orchestrator/typesafe_routing/questions.py` and `decision.py`:
  - `RoutingRequest` bounds (FR-038), security core, agent Choice with `no_tool_needed`, per-agent tool fan-out with `none_fit` and the local index map, presentation Choice, `MAX_ROUTING_AGENTS`/`MAX_TOOLS_PER_AGENT` truncation priority, response parsing, and tiers with provisional thresholds.
  - Tests cover prefixed names, invalid options, missing answers, truncation and exclusion of attachment, tool-output and credential content.
- [X] T013 Add the deterministic TypeSafe fake `D:backend/tests/fakes/typesafe_fake.py`, with recorded answer sets per fixture prompt and failure injection (latency, each error class, malformed responses, per-attempt scripts) at the `client.py` boundary. All later tests use it; no test calls the network. For the quickstart, also add a local fault-injection switch `ASTRAL_TEST_TYPESAFE_FAULT` (`timeout`, `auth`, `ratelimit`, `5xx`). It wraps the real client, is honored only outside production posture, and has a test proving it is ignored in production posture. It deliberately does not use the refused `TYPESAFE_` prefix.
- [X] T014 [P] Add the routing fixture corpus at `D:backend/tests/fixtures/typesafe_routing/`:
  - labeled prompts (single-tool, multi-tool, conversational follow-up, ambiguous, cross-agent name collision, disabled agent, deselected tool, large catalog) with accepted tool labels;
  - a benign reference corpus (fixtures plus the 088 reference journey prompts);
  - an index of `D:backend/security_benchmark/` cases to use.
- [X] T015 Add the local-only `D:scripts/typesafe_routing_bench.py`. It reads the owner's TypeSafe key from stdin under T003a (FR-044), never from env, and never writes or logs it. It measures p50/p95/p99 latency against question and option counts on the real bundled catalog and a synthetic 60×8 catalog, plus tier accuracy on T014. Run it and record the results. Then set `ATTEMPT_TIMEOUT_MS`, `MAX_ROUTING_AGENTS`, `MAX_TOOLS_PER_AGENT`, the forced-choice provider allowlist and provisional tier thresholds in code, and record the values in verification.md (research R16).

- [X] K04 kos-wiki checkpoint (Foundational + dependency-approval milestone): update `astral-primitives` (0.4.0 types, PR SHA), `astral-llm-credential-resolution` (Plane `089.001` credential table, env exclusion), `astral-typesafe-routing` (adapter design, measured constants from T015 with date/SHA), `astral-feature-flags` (new constants); `log.md` checkpoint; commit + push.

**Checkpoint**: The primitives, credential storage, hygiene, adapter core, fake, corpus and measured constants are ready.

---

## Phase 3: US1 — Bring my own TypeSafe key (P1) 🎯

- [X] K05 Reference kos-wiki before US1: `astral-llm-credential-resolution`, `astral-auth-architecture`, `astral-audit-system`.
**Independent test**: Save valid, rejected and empty keys and clear them on web and one native client (no client code change). Verify ciphertext, write-only display, status, audit and gate isolation.

- [X] T016 [P] [US1] Write tests in `D:backend/tests/test_typesafe_settings.py` per `contracts/typesafe-settings.md`:
  - save valid, rejected, unreachable, empty and too-long keys;
  - probe rate limit; clear idempotency;
  - the key is absent from HTML, SDUI components, frames, logs, audit and durable-operation records;
  - the section is hidden in first-run mode;
  - `llm_configured_for` is unaffected; a user with a TypeSafe key but no LLM config stays gated;
  - no `unlock_after_save`/`regate_after_clear` calls;
  - `components()` parity with `render()` fields.
- [X] T017 [US1] Add `D:backend/llm_config/typesafe_handlers.py`: input validation, `_check_probe_rate`, `models.list()` probe (5 s, no retries), durable save via the credential-operation mechanism, clear, circuit reset and audit events (data-model §5).
- [X] T018 [US1] Add the TypeSafe section to `D:backend/orchestrator/projection_surfaces/llm.py` `render()` and `components()`: heading, help text, status badge from `typesafe_store.status`, password field, Save and Remove; hidden when `first_run`.
- [X] T019 [US1] Route `chrome_typesafe_save`/`chrome_typesafe_clear` through `D:backend/orchestrator/orchestrator.py` as durable credential operations (next to `_LLM_CREDENTIAL_SAVE_ACTIONS`) and register the handlers in `D:backend/orchestrator/chrome_events.py`. Do not add them to gated-state allowed actions.
- [X] T020 [US1] Record turn-level key outcomes (valid on success, rejected on 401/403, unavailable when the circuit opens) through `record_outcome_async` with fingerprint conditioning. Test that a stale outcome cannot mark a newly saved key.
- [ ] T021 [US1] Exercise locally on the web client and one existing native client build: save, status, replace, remove. Record observations in verification.md, and confirm with T002 that no client files changed.

- [X] K06 kos-wiki checkpoint (US1 + US7): update `astral-llm-credential-resolution` (TypeSafe settings section, gate isolation, data-sharing acknowledgment checkbox and enforcement, audit events) and `astral-typesafe-routing`; `log.md` checkpoint; commit + push.

### US7 — Acknowledge data sharing before saving credentials (P1)

**Independent test**: On the web LLM settings page and in the first-run dialog, a save with the box unchecked is rejected before any provider request; a checked save records the acknowledgment; a reload shows it checked with a date; existing users are never prompted during chat.

- [X] T064 [P] [US7] Write tests in `D:backend/tests/test_llm_data_sharing_ack.py` per `contracts/typesafe-settings.md`:
  - the warning and checkbox are placed below the credential inputs and above the actions in `render()`, `components()` (SDUI `boolean`) and first-run mode;
  - unchecked saves on `chrome_llm_save`, `chrome_typesafe_save` and `llm_config_set` are rejected with the field error and zero probe calls;
  - a checked save persists the acknowledgment and audits it once per version;
  - an already-acknowledged save is allowed; an explicit `False` is rejected;
  - a `NOTICE_VERSION` bump requires a new acknowledgment;
  - Test connection, Load models and clear actions are unaffected;
  - chat and routing never read the store;
  - a pinned-text test fails when the wording changes without a version bump;
  - web a11y: the label is associated with the checkbox and `aria-describedby` points at the warning.
- [X] T065 [US7] Add `D:backend/llm_config/data_sharing.py`: `NOTICE_VERSION`, title, body and label strings, the `require_acknowledgment(user_id, submitted)` facade over the Plane `DataSharingAcknowledgmentRepository`, and `llm_data_sharing.acknowledged` / `save_blocked` audit events.
- [X] T066 [US7] Render the warning and checkbox in `D:backend/orchestrator/projection_surfaces/llm.py`, directly below all credential inputs (after the TypeSafe field when present) in `render()`, `components()` and first-run mode. Add the pre-checked "Acknowledged on {date}" state and the inline error.
- [X] T067 [US7] Enforce acknowledgment as the first step of `chrome_llm_save` (`llm.py`), `chrome_typesafe_save` (`D:backend/llm_config/typesafe_handlers.py`) and the legacy `llm_config_set` handler (`D:backend/llm_config/ws_handlers.py::handle_llm_config_set`), before validation probes. Map the failure to each path's existing field or error response.
- [X] T068 [US7] Style the warning block and checkbox for the web client in `P:backend/webrender/static/astral.css` using `ThemeView` warning roles and Open Sans, consistent with the a8p settings dialog styling. Wire the inline error display in `P:backend/webrender/static/client.js` if the existing form-error path does not already cover boolean fields. Web only.
- [ ] T069 [US7] Exercise the flow locally on the web client (settings page and first-run dialog with a fresh test user) using the owner's test credentials under T003a. Record observations in verification.md (SC-014).

**Checkpoint**: Users can manage their own key and must acknowledge data sharing before saving any credential. With no key, behavior is unchanged.

---

## Phase 4: US2 — Better-targeted routing with my key (P1)

- [X] K07 Reference kos-wiki before US2: `astral-orchestrator`, `astral-agent-catalog`, `astral-performance-architecture`, `project-a8p` (33/33 live routing evidence and known a8p defects).
**Independent test**: Scripted LLM + TypeSafe fake over the T014 fixtures. Verify tier effects, the first-round-only rule, eligible subsets and no-key identity.

- [X] T022 [P] [US2] Write tests in `D:backend/tests/test_typesafe_turn_routing.py`:
  - no key ⇒ zero fake calls and byte-identical round-1 `tools_desc`/`tool_choice` versus baseline;
  - high ⇒ single tool + forced choice (supported provider) or `"auto"` (custom/ollama/lmstudio);
  - medium ⇒ shortlist ⊆ eligible;
  - low, `no_tool_needed`, `none_fit` and invalid options ⇒ unchanged;
  - round ≥2 full list; prefixed collision names; disabled agent and deselected tool never offered;
  - slash-command, onboarding, preflight-failure and empty-eligible turns ⇒ no call;
  - REST `POST /api/chats/{id}/messages` and `async_mode` turns use the same seam.
- [X] T023 [US2] Implement `start_routing`, `await_decision` and `apply_round_one` in `D:backend/orchestrator/typesafe_routing/__init__.py`, composing client, budget, questions and decision. They never raise. Register `FF_TYPESAFE_ROUTING` (default ON) in `D:backend/shared/feature_flags.py`. When OFF, routing is treated exactly like the no-key path. Tests cover both states.
- [X] T024 [US2] Add seams I1 and I2 to `_handle_chat_message_impl` in `D:backend/orchestrator/orchestrator.py`:
  - build `RoutingRequest` from eligible pairs, datamarked message, bounded history, active agent and selected tools;
  - start the task immediately after eligibility is computed;
  - await it with the remaining deadline before round 1;
  - carry the decision on the turn context.
- [X] T025 [US2] Add a `tool_choice` keyword to `_call_llm` (default preserves `"auto"`), with the forced-choice provider allowlist and a one-shot `"auto"` retry on provider rejection that does not count against `MAX_RETRIES`. Test each provider preset class with the fake OpenAI client.
- [X] T026 [US2] Implement the HTTP Work `kind="chat"` security-only request path (`D:backend/orchestrator/work_submit.py` / `D:backend/persistent_agents/chat_episode.py`) with tests. Confirm `VirtualWebSocket` background turns receive notices.
- [X] T027 [US2] Add metrics and observability: `perf_span("turn.typesafe")`, `RuntimeObservability` metrics (data-model §6) and the `typesafe.routing_fallback` audit event. Tests assert no content or key in labels or metadata.
- [X] T028 [US2] Calibrate tiers with the bench script against the T014 labels to meet SC-005: ≥95% high-tier acceptance, and task completion no worse than baseline in a scripted comparison. Set final thresholds in `decision.py` and record the confusion matrix in verification.md.

- [X] K08 kos-wiki checkpoint (US2 + tier-calibration milestone): update `astral-orchestrator` (routing seams I1–I3, round-1-only rule) and `astral-typesafe-routing` (tiers, final thresholds and SC-005 accuracy summary with date/SHA, local-evidence label); `log.md` checkpoint; commit + push.

**Checkpoint**: Keyed users get TypeSafe-narrowed first rounds; unkeyed users are unchanged.

---

## Phase 5: US3 — Uninterrupted flow when TypeSafe misbehaves (P1)

- [X] K09 Reference kos-wiki before US3: `astral-runtime-reliability-hardening`, `astral-performance-architecture`.
**Independent test**: Failure injection through the fake. Measure added delay, notices, circuit behavior and terminal-failure handling.

- [X] T029 [P] [US3] Write tests in `D:backend/tests/test_typesafe_resilience.py`:
  - each transient class retried up to 3 attempts with the backoff schedule;
  - each non-transient class not retried;
  - wall time ≤1.5 s + 50 ms tolerance;
  - exactly one "retrying" notice and one "standard routing" notice, both non-persisted;
  - circuit opens after 3 consecutive fallbacks; ≤5 ms added while open; half-open trial; auth-block until fingerprint change;
  - cancellation and disconnect abandon attempts;
  - fallback LLM failure ⇒ existing explicit error surface and the turn stops, with no duplicate tool calls and no lingering progress state.
- [X] T030 [US3] Add `D:backend/orchestrator/typesafe_routing/notices.py` over `_send_chat_status`: statuses `retrying`/`info`, copy per contract, a once-per-turn guard, delivery to `VirtualWebSocket`, and no history persistence. Check the web client already renders `retrying`/`info` statuses; if not, a web-only `P:backend/webrender/static/client.js` status rendering change is allowed.
- [X] T031 [US3] Add seam I6: cancel the routing task on turn cancellation (`cancelled_sessions`) and socket close in `D:backend/orchestrator/orchestrator.py`, with tests.
- [ ] T032 [US3] Qualify latency on the local candidate stack: 200 unkeyed turns (SC-001), 200 keyed turns against TypeSafe (SC-002), and injected-failure runs (SC-003) and circuit-open runs (SC-004). Record the distributions against the T004 baseline in verification.md.

- [X] K10 kos-wiki checkpoint (US3 + latency milestone): update `astral-typesafe-routing` (budget, notices, circuit, SC-001–SC-004 measured distributions vs baseline) and `astral-performance-architecture` cross-link; `log.md` checkpoint; commit + push.

**Checkpoint**: The owner's failure sequence is enforced with measured bounds.

---

## Phase 6: US4 — An extra, fast safety screen (P1)

- [X] K11 Reference kos-wiki before US4: `astral-security-defense-layers`, `astral-security-benchmark-harness`, `astral-execution-layer-security-gaps`, `astral-delegated-authority-framework`.
**Independent test**: The security benchmark and benign corpus through the fake with recorded answers. Verify verdicts, HITL enforcement, never-relax behavior and audit.

- [X] T033 [P] [US4] Write tests in `D:backend/tests/test_typesafe_security.py`:
  - refuse ⇒ no LLM or tool call, refusal `Alert`, audit `typesafe.security_verdict` without text;
  - confirm_tools ⇒ every tool call in the turn goes through the existing HITL approval;
  - property test over the `_run_gate_stack` decision matrix: with any TypeSafe verdict, no denied or approval-required call becomes allowed;
  - pass and `None` ⇒ gate decisions identical to baseline;
  - no additional TypeSafe request is issued for security.
- [X] T034 [US4] Add `D:backend/orchestrator/typesafe_routing/security_policy.py`: verdict rules, provisional thresholds, `REFUSE_TIER_ENABLED=False` until T036, and downgrade of refuse to confirm_tools while it is disabled.
- [X] T035 [US4] Wire the I2 refusal short-circuit (existing refusal rendering, `chat_status done`, audit) and the I4 confirm_tools consultation at the supervisor + HITL step of `_run_gate_stack` in `D:backend/orchestrator/orchestrator.py`.
- [ ] T036 [US4] Calibrate against `D:backend/security_benchmark/` and the T014 benign corpus with the bench script to meet SC-006 false-positive bounds. Set final thresholds, enable the refuse tier only if the bounds are met, and record the calibration tables and decision in verification.md.

- [X] K12 kos-wiki checkpoint (US4 + security-calibration milestone): update `astral-security-defense-layers` (additive TypeSafe ingress layer, verdict tiers, never-relax property), `astral-security-benchmark-harness` (089 calibration use and SC-006 results summary), `astral-typesafe-routing`, `astral-open-follow-ups` (refuse-tier status if not enabled); `log.md` checkpoint; commit + push.

**Checkpoint**: The additive screen is active with calibrated thresholds.

---

## Phase 7: US5 — Richer, instantly arranged results (P2)

- [X] K13 Reference kos-wiki before US5: `astral-adaptive-ui-designer`, `astral-sdui-pipeline`, `astral-primitives`, `astral-rote`, `astral-cross-client-contracts`.
**Independent test**: Fixture rounds per style. Verify arrangement correctness, designer fallback, web rendering of the new types and down-conversion on every non-web profile.

- [X] T037 [P] [US5] Write Projection renderer tests in `P:tests/webrender/test_primitives_089.py` for the six types and multi-dataset `bar_chart`: escaping of every text field, numeric clamping, no `style`/`on*`/`href` emission from data, a11y roles and labels, and theme-class series colors.
- [X] T038 [US5] Implement the six renderers and multi-dataset bar chart in `P:backend/webrender/renderer.py`, registered in `PRIMITIVE_RENDERERS`, with series and state CSS classes bound to theme variables in `P:backend/webrender/static/astral.css`.
- [X] T039 [US5] Add ROTE ladders and explicit field mappings in `P:backend/rote/fallback.py`/`adapter.py`, with tests in `P:tests/rote/test_fallback_089.py`.
- [X] T040 [US5] Update the manifest and dispositions: `P:contracts/ui_protocol.json` `component_types` + server-side disposition data; `D:backend/tests/test_ui_protocol_manifest.py`; `P:tests/test_disposition_matrix_088.py`. Run the unmodified native and Windows drift-guard tests locally and record the expected failures in the verification.md known-divergence register. **Do not edit those tests.**
- [X] T041 [US5] Add the non-web guardrail `P:tests/rote/test_non_web_unchanged_089.py`: golden adapter outputs for the `windows`/`android`/`ios`/`macos`/`watch`/`tv`/`voice` profiles over a fixture of all component types, byte-identical to the 089 base except that new types appear only as fallbacks.
- [X] T042 [P] [US5] Write layout composer tests in `D:backend/tests/test_typesafe_layout.py`: each style's ordering and grouping, every component exactly once, no invented components, accepted by the existing `ui_designer` validator and lint, and `None` for `as_delivered` and <2 components.
- [X] T043 [US5] Implement `D:backend/orchestrator/typesafe_routing/layout.py` per `contracts/ui-primitives-089.md`.
- [X] T044 [US5] Add seam I5 in `_deliver_round_components` (`D:backend/orchestrator/orchestrator.py`): upsert-first unchanged, style layout for keyed web turns, `_run_designer` fallback, flat-stays fallback, explicit error only when flat delivery fails, and `typesafe_layout_applied_total` metrics. Tests cover keyed, unkeyed, designer failure and native device skip.
- [X] T045 [US5] Adopt the new types in bundled agents where their existing data fits:
  - weather current conditions → `gauge`/`stat_group`;
  - ML Services benchmark metrics → `stat_group`/`radar_chart`;
  - remote compute job lifecycle → `pipeline_stepper`;
  - result actions → `action_group`.

  Files are under `D:backend/agents/`. Update agent tests. Non-web clients receive the fallbacks.

- [X] K14 kos-wiki checkpoint (US5 + known-divergence milestone): update `astral-adaptive-ui-designer` (style layout primary for keyed turns, designer fallback), `astral-rote` (ladders, non-web guardrail), `astral-cross-client-contracts` (manifest change; native/Windows drift-guard divergence as an owner-accepted known divergence), `astral-open-follow-ups` (native adoption of new types); `log.md` checkpoint; commit + push.

**Checkpoint**: New components render on web and degrade elsewhere; keyed turns get instant arrangements.

---

## Phase 8: US6 — The a8p layout on the Astral web client (P2)

- [X] K15 Reference kos-wiki before US6: `astral-web-client`, `astral-canvas-first-uiux`, `astral-rote`, `astral-cross-client-contracts`.
**Independent test**: Desktop parity ≥90% at 3 viewports; responsive checklist 100% at 4 viewports; Open Sans everywhere; 088 journeys, a11y and CSP pass; no client or workflow diffs.

- [X] T046 [US6] Capture a8p reference screenshots and region bounding-box JSON at 1920×1080, 1440×900 and 1280×800 for the five states in `contracts/web-layout-parity.md`, from a locally running a8p. Store them in `specs/089-typesafe-a8p-integration/reference/`.
- [X] T047 [P] [US6] Build the Playwright parity scorer and responsive checklist automation in `P:tests/web_layout_parity/`: computed bounding boxes, structural checks, computed font family for all text nodes, no horizontal scroll, overlap and clipping detection, touch target sizes, drawer focus trap, and a CSP violation listener. Output a scored report.
- [X] T048 [US6] Self-host Open Sans WOFF2 (400/500/600/700/800, latin) + `OFL.txt` in `P:backend/webrender/static/fonts/`. Add `@font-face` and set `--astral-font` to Open Sans for all text including code in `P:backend/webrender/static/astral.css`. Remove the Inter and JetBrains Mono references from web shell CSS, the Tailwind config in `shell.html` and `client.js`. Leave `P:contracts/assets/fonts/` untouched. Update the resource tests.
- [X] T049 [US6] Restructure `P:backend/webrender/templates/shell.html` into the a8p regions: sidebar (brand, agent directory header + count, search, agent list, recent work, profile widget + cog), main column (canvas with landing and conversation feed), composer bar with 088 controls, full-screen overlay host and modal host. Keep the element IDs and data hooks `client.js` and tests rely on, or rename them together with all references. Keep nonce scripts only.
- [X] T050 [US6] Restyle `P:backend/webrender/static/astral.css` to the a8p layout and component styling (dimensions per the parity contract) using only `--astral-*` ThemeView variables and `color-mix()`. Add `P:tests/test_no_hex_literals_089.py`, which fails on hex or rgb color literals outside the theme variable definitions.
- [X] T051 [US6] Update `P:backend/webrender/static/client.js` (ES5 IIFE convention, `addEventListener` only, `textContent` for untrusted strings):
  - agent directory rendering + live filter;
  - landing header status pills and scenario grid with filter tabs;
  - response cards with header meta, hover expand chip and full-screen overlay (Esc to exit);
  - settings modal chrome styling hooks and tabs for surfaces declaring sections;
  - return-to-landing from brand.

  Must pass ESLint via `P:tooling/web-ci`.
- [X] T052 [US6] Supply the landing and sidebar data from the server without changing native frames:
  - scenario examples and categories from `D:backend/orchestrator/welcome.py`;
  - the agent directory from the existing agents view model (`P:src/astralprojection/chrome/agents.py`) through the web target only. If a new frame or field is needed, emit it only for the web target and add a test proving native registration frames are unchanged.
- [X] T053 [US6] Implement responsive breakpoints in `astral.css`/`client.js` per research R13: 1024–1279 narrowed sidebar; <1024 drawer with focus trap; <768 pinned composer with overflow menu, full-viewport sheets, single-column landing, scrollable filter chips; safe-area insets; 44px targets; reduced motion.
- [X] T054 [US6] Tune ROTE for the web profiles only in `P:backend/rote/adapter.py`/`capabilities.py`: the new-type rules in `contracts/ui-primitives-089.md`, plus existing-type adjustments for any failing responsive checklist item. Keep T041 guardrails green, confirm the web client re-registers viewport on resize and orientation change, and add tests in `P:tests/rote/test_web_profiles_089.py`.
- [X] T055 [US6] Run qualification locally and record the scored reports in verification.md:
  - parity ≥90% at each desktop viewport (SC-010);
  - responsive checklist 100% (SC-012);
  - 088 web journeys at desktop, tablet and phone;
  - keyboard, screen reader, reduced motion and 200% text (SC-009);
  - zero CSP violations.

- [X] K16 kos-wiki checkpoint (US6): update `astral-web-client` (a8p layout, Open Sans, ThemeView colors, desktop parity scores, responsive checklist results, CSP/a11y results — local-evidence labeled), `astral-rote` (web-profile tuning); `log.md` checkpoint; commit + push.

**Checkpoint**: The web client matches a8p on desktop and works well on all web screen sizes.

---

## Phase 9: Polish and qualification

- [X] K17 Reference kos-wiki before qualification: `astral-dev-verification-workflow`, `astral-open-follow-ups`, `astral-feature-timeline`.
- [X] T056 Repin Deep: update `components/AstralPrimitives`, `components/AstralPlane` and `components/AstralProjection` submodules to the 089 commits, and `D:config/astral-composition.json` (primitives `package_version` 0.4.0 + `contract_sha256`, `ui_protocol.sha256`, `data_plane.schema_revision` `089.001` + `migration_sha256`, component commits). Run the composition verification locally.
- [X] T057 Rehearse a populated upgrade `088.008 → 089.001` on the local candidate stack with the synthetic dataset, plus a repeat start and the rollback procedure. Record receipts in verification.md.
- [ ] T058 Run the full local suites in Deep, Plane, Primitives and Projection (server and web), with ≥90% changed-code coverage (diff-cover), Ruff and ESLint. Record the results. List the native and Windows drift-guard failures as the known divergence, and treat any other failure as blocking (SC-008).
- [X] T059 Run a canary secret scan: run all TypeSafe tests and a local end-to-end session with a synthetic canary key, then scan logs, audit rows, durable-operation records, rendered HTML/SDUI snapshots and test artifacts for the canary and its prefix. Expect zero hits (SC-007).
- [X] T060 Run `D:scripts/check_089_scope.py` across all five repositories. Expect zero client-directory and workflow changes (SC-011). Record the output.
- [X] T061 Document user and operator behavior in `D:docs/`: the TypeSafe key in LLM settings, what routing, safety screen and layout do, the fallback and notice behavior, the circuit, env-variable refusal, and the six primitives. Include renderer target documentation for the new types (Constitution VI).
- [ ] T062 Walk through `quickstart.md` end to end on the local candidate stack and record the results.
- [X] T063 Prepare per-repository PR descriptions (Primitives, Plane, Projection, Deep) covering: dependency approval record, owner exceptions (CI ignored, web-only, desktop parity), known divergences, measured latency and calibration summaries, and merge order Primitives → Plane → Projection → Deep. No attribution lines.
- [ ] T070 Remove the owner's LLM and TypeSafe credentials from the local candidate stack (settings clear actions or owner-scoped deletion) unless the owner asks to keep them. Confirm no key material remains in local logs or artifacts (reuse the T059 scan). Record completion, without values, in verification.md (FR-044).
- [X] K18 Final kos-wiki checkpoint (repin + qualification milestones): reconcile every page listed in SC-013 with the final 089 state (SHAs, schema `089.001`, primitives 0.4.0, measured results, known divergences, open follow-ups); resolve or file contradictions noted since K01; run the vault `lint` workflow and fix 089-introduced unresolved links or unprovenanced facts; `log.md` `checkpoint` + `lint` entries; commit + push.
- [X] K19 Milestone and daily kos-wiki updates (recurring, all phases): on each milestone (dependency approval, each component PR opened/merged, `089.001` rehearsal, calibration results, known divergences, composition repin) and at the end of each working day with 089 activity, append a dated `checkpoint` entry and update the affected pages; never batch past the next phase checkpoint.

---

## Dependencies and parallelism

- **Setup**: T001–T004 run first. T004 must precede any routing seam.
- **Foundational**: T005, T006, T007, T008, T010, T011 and T014 can run in parallel. T009 needs T006. T012 needs T010. T013 needs T010–T012. T015 needs T010–T014.
- **Story dependencies**:
  - US1 (T016–T021) needs T007–T009 and T010.
  - US7 (T064–T069) needs T006 and T009 (store pattern). T067 needs T017 for the TypeSafe save path, and T066 should follow T018.
  - US2 (T022–T028) needs T011–T015 and US1's store.
  - US3 (T029–T032) needs T023–T025.
  - US4 (T033–T036) needs T023–T024.
  - US5 needs T005 (renderer, ladder and manifest) and T024 (T044's decision input). T037–T041 can start after T005 only.
  - US6 is independent of TypeSafe: T046–T053 can start at any time; T054 needs T039; T055 needs T048–T054.
- **Polish**: T056 needs T005, T006 and the Projection work to be committed. T057–T063 come last.
- **Knowledge base**: Each phase's K reference task precedes that phase's first T task, and each phase's K checkpoint task closes it. K19 recurs throughout, and K18 is last.
- **Parallel lanes**:
  - Primitives + Projection renderer/ROTE;
  - Plane;
  - Deep adapter core;
  - web shell redesign.

## Requirement coverage

| Requirements | Tasks |
|---|---|
| FR-001–006 | T006, T009, T016–T021, T007, T008 |
| FR-007–013 | T010, T012, T022–T028 |
| FR-014–020 | T011, T029–T032 |
| FR-021–024 | T033–T036 |
| FR-025–029 | T005, T037–T045 |
| FR-030–034 | T046–T055 |
| FR-035–038 | T003, T010, T012, T027 |
| FR-039 | T002, T058, T060 |
| FR-044 | T003a, T015, T069, T070 |
| FR-045–049, SC-014 | T064–T069 |
| SC-001–004 | T004, T032 |
| SC-005 | T028 |
| SC-006 | T036 |
| SC-007 | T059 |
| SC-008 | T058 |
| SC-009, SC-010, SC-012 | T055 |
| SC-011 | T060 |
| FR-040–043, SC-013 | K01–K19 |
