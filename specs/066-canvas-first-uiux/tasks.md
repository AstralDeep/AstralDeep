# Tasks: Canvas-First Adaptive UI/UX

**Input**: [plan.md](plan.md) · [spec.md](spec.md)
Status legend: [ ] open · [x] done · [~] in progress

## Phase P0 — Groundwork (bug fixes; code committed, tests here)

- [x] T001 bind_chat: coordinator + protocol + InMemory + Postgres repositories (None→chat only, fence-checked) — committed `38ce6ac2`
- [x] T002 `_adopt_operation_chat` wired after chat creation in the ui_event chat_message branch — committed `38ce6ac2`
- [x] T003 `openai_auth_kwargs` + shared keyless httpx client; applied at client_factory, probe, REST probes, agent codegen, knowledge synthesis — committed `38ce6ac2`
- [x] T004 `custom` preset key-optional + surface copy + pinned providers test update — committed `38ce6ac2`
- [ ] T005 Tests: bind_chat unit (both repos: None→chat, idempotent re-bind, cross-chat refusal, stale fence) + first-message-of-new-chat integration (typed + welcome-example) + fence strictness regression (FR-028/029, SC-002)
- [ ] T006 Tests: openai_auth_kwargs (real key passthrough; empty/sentinel → no Authorization header on the wire) + key-optional custom save/test/load-models paths (FR-025/026)

## Phase P1 — Web core (layout, composer, envelope)

- [ ] T010 shell.html: composer rework (voice default control with SVG icon + honest reason, connection pill, input min-width row), canvas toolbar host, chat collapse toggle, drawer/overlay hosts
- [ ] T011 astral.css: three layout modes (`stacked <700`, `collapsed 700–1023 default`, `expanded ≥1024 + toggle`), floating composer (collapsed), overlay drawer, stacked sheet with dim, SVG icon masks for `data-icon`, queued/error bubble styles, bounded canvas width
- [ ] T012 client.js: layout-mode state machine + localStorage persistence + toggle wiring; auto-reveal badge/peek on assistant text (reduced-motion aware)
- [ ] T013 client.js: connection state surfacing + bounded send queue (pre-registration + disconnected), flush-on-register, 45s refusal with retry; input preserved (FR-013/014; SC-003)
- [ ] T014 client.js: voice default state before frames; composer_state refines; enable-without-reload (FR-011/012; SC-005)
- [ ] T015 Capability envelope: client `capability_update` (debounced resize/orientation/permission/connection/reduced-motion) + `reduced_motion`/`pointer_type` fields; server handler refreshes ROTE profile + pushes `rote_config` (FR-007..010; SC-004)
- [ ] T016 rote/capabilities.py additive fields with safe defaults

## Phase P2 — Lifecycle & chrome

- [ ] T020 Turn failure UX: keep user bubble, inline error + retry (same payload), never blank canvas, stuck-request recovery (FR-017/021; SC-008)
- [ ] T021 Status phases near composer (routing / tool-with-identity / composing) from existing progress frames (FR-016)
- [ ] T022 Markdown bubbles (safe subset, escape-first) + preview strip (FR-018)
- [ ] T023 Boilerplate → metadata: provenance/sample-data lines attach to the narrative message as a caption, not standalone bubbles (FR-019)
- [ ] T024 Title generation: verify post-keyless fix; deterministic fallback (truncated first message) on failure (FR-020; SC-009)
- [ ] T025 Component chrome at rest hidden; hover/focus reveal + touch/keyboard affordance; provenance marker consolidated (FR-022; SC-007)
- [ ] T026 Canvas page actions in stable toolbar (no scroll overlap) (FR-023)
- [ ] T027 Welcome on explicit new chat + wel_ excluded from provenance stamping (FR-024)

## Phase P3 — Voice reliability & diagnosability

- [ ] T030 GET /api/voice/status (authed): admitted workers (identity/load), last admission refusal + reason, last preflight verdict (FR-034; SC-012)
- [ ] T031 Worker: bounded preflight re-check on failure (recovery without restart) + startup verdict log lines (FR-036; worker log silence fix)
- [ ] T032 Composer refusal reasons: session-create failure surfaces reason within 2s; permission-pending timeout reports permission-shaped reason, not network_interrupted (FR-033; SC-011)
- [ ] T033 Prod remediation (operator): run voice-prod-diagnosis.md runbook on sandbox; record outcome — OPERATOR-GATED
- [ ] T034 docs: deployment topology contract (voice vhost, env incl. closure digest provenance + replica id) (FR-037)

## Phase P4 — Verification, native consistency, handoff

- [ ] T040 Browser sweep: width matrix {320..2560}, adverse-send suite, failure drills, chrome-reveal checks (SC-001/003/007/008)
- [ ] T041 Backend suites in CI posture + new tests green; ruff clean; changed-line coverage ≥90% (SC-010)
- [ ] T042 Windows client parity pass per checklist (+ tests)
- [ ] T043 Android client parity pass per checklist (+ unit tests)
- [ ] T044 apple-handoff.md + final annotated screenshots (web ×3 sizes, Windows, Android) + parity checklist status (FR-030/031)
- [ ] T045 Non-regression audit vs FR-032 (055 contract, workspace identities, exports, a11y landmarks, voice flows)
