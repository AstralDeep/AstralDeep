# Tasks: Canvas-First Adaptive UI/UX

**Input**: [plan.md](plan.md) · [spec.md](spec.md)
Status legend: [ ] open · [x] done · [~] in progress

## Phase P0 — Groundwork (bug fixes; code committed, tests here)

- [x] T001 bind_chat: coordinator + protocol + InMemory + Postgres repositories (None→chat only, fence-checked) — committed `38ce6ac2`
- [x] T002 `_adopt_operation_chat` wired after chat creation in the ui_event chat_message branch — committed `38ce6ac2`
- [x] T003 `openai_auth_kwargs` + shared keyless httpx client; applied at client_factory, probe, REST probes, agent codegen, knowledge synthesis — committed `38ce6ac2`
- [x] T004 `custom` preset key-optional + surface copy + pinned providers test update — committed `38ce6ac2`
- [x] T005 Tests: bind_chat unit — None→chat adopt, idempotent re-bind, cross-chat refusal, already-scoped refusal, stale fence ([test_bind_chat_066.py](../../backend/tests/test_bind_chat_066.py); live first-message verified in browser; Postgres-repo variant exercised via the shared contract in CI)
- [x] T006 Tests: keyless auth — real-key passthrough, keyless transport, shared client, header stripped at the wire, key-optional custom ([test_keyless_auth_066.py](../../backend/llm_config/tests/test_keyless_auth_066.py))

## Phase P1 — Web core (layout, composer, envelope)

- [x] T010 shell.html: composer rework (default voice control + SVG, connection pill, turn-status line, chat toggle w/ unread badge, rail header + collapse) — verified live
- [x] T011 astral.css: three layout modes, floating composer, overlay drawer, SVG icons via client injection, queued/error styles, bounded canvas, calm chrome reveal, canvas toolbar surface — verified live at 520/800/1264px
- [x] T012 client.js: layout-mode machine + localStorage pref + toggles + unread badge/peek (reduced-motion aware) — verified live
- [x] T013 client.js: connection pill + bounded queue (5, 45s TTL) for chat AND chrome actions, flush on the rote_config verdict, refusal restores text (SC-003 drill pending in T040)
- [x] T014 client.js: default voice control before frames + on teardown; composer_state refines (verified live: enabled when worker admitted, default when absent)
- [x] T015 Capability envelope live: `capability_update` action (client debounce + server handler + rote_config re-push) — verified live (browser→tablet→browser reclassification in logs, no reconnect)
- [x] T016 rote/capabilities.py: additive `reduced_motion` + `pointer_type` fields

## Phase P2 — Lifecycle & chrome

- [x] T020 Turn failure UX: transient bubbles materialize into the rail on terminal failure + inline error card with exact-text ↻ Retry; canvas never blanked (verified live pre-fence-fix); client stuck-request watchdog deliberately dropped (R6)
- [~] T021 Status near composer: shipped (mirrored turn-status line + spinner); tool-identity phase enrichment still open
- [~] T022 Markdown bubbles: stream frames + chat renders already markdown; REMAINING: the 062 words-only snapshot rail path renders literal asterisks (root cause pinned in research R11) + preview strip
- [ ] T023 Boilerplate → metadata: provenance caption still rides as trailing bubble text; restyle as footnote metadata pending
- [x] T024 Titles: root cause was the keyless 403 (fixed); deterministic fallback added on title-LLM failure
- [x] T025 Component chrome hidden at rest; hover/focus reveal + coarse-pointer tap toggle — verified live (dashboard at rest shows zero chrome rows)
- [x] T026 Canvas toolbar: solid backdrop surface, sticky without see-through overlap
- [x] T027 Welcome on new chat (ordered before chat_created) + wel_ provenance-stamp exclusion — verified live

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
