# Implementation Plan: TypeSafe Routing and a8p UI Integration

**Branch**: `089-typesafe-a8p-integration` | **Date**: 2026-09-17 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/089-typesafe-a8p-integration/spec.md`

## Summary

Bring a8p's two contributions into the post-088 Astral ecosystem without disturbing the user's flow.

**1. TypeSafe System One routing.** For users who save their own TypeSafe key:
- A single, strictly time-boxed request judges security, target agent, target tool and presentation style.
- It starts concurrently with turn preparation. Its answer narrows only the first LLM round.
- It adds a confirmation or refusal layer on top of an unchanged gate stack.
- It drives a deterministic, style-based arrangement of results in place of the LLM designer pass.
- Failures follow the owner's sequence: notice, up to 3 attempts in 1.5 s, fallback to today's routing and designer, then an explicit failure and stop. A per-user circuit prevents repeated delays.

**2. The a8p web experience.**
- The web shell adopts a8p's layout at ≥90% desktop parity, using Open Sans and colors from the existing `ThemeView`.
- It gets a new responsive layout and ROTE tuning for tablet and phone browsers.
- AstralPrimitives gains six component types, rendered on web only and down-converted server-side for every other client.

Only the web client changes. Native and Windows clients are untouched, and CI does not gate the work.

## Technical Context

**Language/Version**:
- Python 3.11 (Deep image and CI baseline; local `.venv` 3.13 permitted).
- Vanilla ES5-convention JavaScript, CSS and HTML for the web shell (no build step).
- AstralPlane and AstralPrimitives on Python ≥3.11.

**Primary Dependencies**:
- Existing: FastAPI, websockets, psycopg2, cryptography (Fernet), the OpenAI-compatible client via `llm_config/client_factory.py`, astralprims, AstralPlane, AstralProjection.
- **New (owner-approved 2026-09-17; no approval gate under Constitution V v3.0.0)**: `typesafe-sdk==0.6.0`, bringing transitive `httpx2`, `msgspec` and `tenacity` (`typing-extensions` is already present).
- New static asset: Open Sans WOFF2 (SIL OFL 1.1, license file shipped alongside).

**Storage**:
- PostgreSQL through an AstralPlane guarded migration `089.001` that adds `user_typesafe_credential` (ciphertext only) and `user_data_sharing_acknowledgment` (notice version + time).
- Everything else is ephemeral per-turn state or in-process circuit state.

**Testing**:
- pytest + pytest-asyncio (Deep, in the `astraldeep` container), Plane `uv run pytest`, Primitives `pytest`.
- Projection `pytest`, plus `tooling/web-ci` ESLint and Playwright (web shell, CSP, layout parity, responsive checklist).
- Ruff; ≥90% changed-code coverage measured locally.
- Deterministic TypeSafe fake at the adapter boundary; scripted LLM driver (`verification/drivers/scripted_llm.py`).
- **CI is excluded** (owner directive; see Constitution Check).

**Target Platform**: Linux backend (port 8001) serving the web client at desktop, tablet and phone browser viewports. Native and Windows clients continue unchanged against the same server.

**Project Type**: Multi-repository server-driven application.
- AstralDeep: orchestrator, surfaces, credential facade, routing adapter.
- AstralProjection: web shell, renderer, ROTE, protocol manifest.
- AstralPlane: credential table and repository.
- AstralPrimitives: six new types.
- LETS: no change.

**Performance Goals**: From the spec:
- SC-001: unkeyed users within 5% of baseline.
- SC-002: keyed success path adds ≤150 ms p95 before the first model call.
- SC-003: worst-case TypeSafe delay ≤1.5 s.
- SC-004: ≤5 ms while the circuit is open.

A baseline is captured first (T004) using existing `perf_span` lines `turn.route`, `turn.tools` and `turn.narrative`, plus a new `turn.first_llm_call_start` marker.

**Constraints**:
- Additive security only.
- The first-run LLM gate is never affected.
- No environment-supplied TypeSafe configuration.
- CSP (nonce scripts, `font-src 'self'`).
- 088 FR-003, FR-023 and FR-028 preserved.
- Web-only client changes; byte-identical ROTE output for non-web profiles.
- No CI workflow edits.

**Scale/Scope**:
- 4 repositories changed (Primitives, Projection web/server portions, Plane, Deep).
- About 12 new Python modules, 6 primitive classes, 6 renderers, 1 migration, 1 settings section.
- Full web shell rework (`shell.html`, `astral.css`, shell portions of `client.js`, about 8.6k lines total today).
- Per-user eligible tool sets range from a handful to (synthetic) 480 tools.

## Constitution Check

*GATE: evaluated before Phase 0 research and re-checked after design. Owner directives recorded 2026-09-17 are cited where they create explicit, bounded exceptions.*

| Principle | Assessment |
|---|---|
| I. Python backend | ✅ All backend work is Python. |
| II. UI delivery | ✅ The shell remains server-emitted assets. Primitives are **defined** in astralprims and **rendered** by the Projection web renderer, and ROTE adapts per device. The style layout composes only astralprims references. Settings stay server-owned SDUI. |
| III. Testing ≥90% changed coverage | ✅ Required and measured **locally** for every changed Python module; web shell covered by Playwright and ESLint locally. |
| IV. Code quality | ✅ Ruff and the tracked ESLint configuration run locally; no inline lint suppressions without justification. |
| V. Dependencies | ✅ Constitution amended to **v3.0.0** (2026-09-17): any dependency may be installed without approval; dependencies are declared in the owning manifest. `typesafe-sdk==0.6.0` (+ `httpx2`, `msgspec`, `tenacity`) is pinned in `backend/requirements.txt` and was also explicitly owner-approved. The import is lazy, and absence degrades to standard routing. |
| VI. Documentation | ✅ Docstrings on all new modules; README entries for the six primitives; renderer target documentation; `/docs` unaffected (no new REST endpoint). The external knowledge base kos-wiki is referenced before each phase and updated at every checkpoint (see "Knowledge base cadence"). |
| VII. Security | ✅ Keycloak ownership unchanged. Users acknowledge third-party and TypeSafe data sharing before any credential save (US7). The owner's test credentials are confined to the local stack (FR-044). The key is encrypted with the existing credential key and never materialized outside Deep's handler and adapter. TypeSafe is additive and never bypasses gates, delegation, PHI, egress or LETS. Egress is validated against the TypeSafe host. Boot refuses environment-supplied TypeSafe configuration in production posture. |
| VIII. UX | ✅ New primitives are added to astralprims, documented and approved before use (Phase 2 precedes renderer and agent use). |
| IX. Migrations | ✅ Plane guarded revision `089.001`, idempotent, auto-applied at startup through the existing runtime. Rollback is documented (data-model §2) and tested on a populated `088.008` fixture. |
| X. Production readiness | ⚠️ **Owner exception (CI)**: CI-based evidence gates are not used for 089. Local release-evidence diagnostics may still be run for information. Staging qualification uses an isolated local candidate stack (docker compose) with a populated synthetic dataset; results are bound to exact local SHAs in `verification.md`. No deployment or release is authorized by this plan. |
| XI. Continuous Integration | ⚠️ **Owner exception**: "ignore CI tests on all Astral/LETS repos." CI results do not gate 089 and no workflow file is modified (FR-039, SC-011). All CI-equivalent checks run locally. |
| XII. Cross-client consistency | ⚠️ **Owner exception (web-only)**, recorded in the spec as a justified divergence:<br>• the a8p layout and responsive work apply to web only;<br>• native and Windows clients are unchanged and receive new types only as server-side fallbacks;<br>• their manifest drift guards will fail locally against the new `component_types` (accepted known divergence);<br>• the settings key field reaches all clients through existing server-owned SDUI, so capability parity is preserved for that surface;<br>• colors remain sourced from `ThemeView`, so palette consistency is preserved. |
| XIII. Research integrity | ✅ Latency and threshold values are measured (T015, T028, T036) and recorded. Nothing is claimed from TypeSafe marketing or unmeasured assumptions. Wiki facts are provenance-tagged, keep local evidence separate from qualified evidence, and defer to current code when they disagree. |

**Post-design re-check**: The design introduces no new service, ORM, frontend framework, identity path or migration framework. The three ⚠️ rows are the owner-directed exceptions above. No unrecorded violation.

## Architecture

```text
Send ─► handle_chat_message ─► _handle_chat_message_impl
         │ eligible_tool_pairs ──┬─► [I1] start_routing() ──► TypeSafe (≤3 attempts, 1.5 s deadline)
         │                        │        │ notices via _send_chat_status
         │ prompt/history prep ◄──┘ (concurrent)
         │
         ├─ [I2] await_decision ─► refuse? ─► refusal Alert + audit ─► done
         │                        └► RoundOnePlan (high: [tool]+forced | medium: shortlist | low/None: unchanged)
         ├─ round 1 _call_llm(tools_desc, tool_choice)      rounds ≥2: unchanged
         ├─ per tool call: _run_gate_stack (unchanged) + [I4] confirm_tools ⇒ HITL
         └─ _deliver_round_components: flat upsert first ─► [I5] style layout.compose()
                                                         └► else _run_designer (today) ─► else flat stays
```

**Settings**: `projection_surfaces/llm.py` section → `chrome_typesafe_save/clear` → probe (`models.list`, 5 s) → Deep `typesafe_store` → Plane `EncryptedTypeSafeCredentialRepository`.

**Data-sharing acknowledgment**: The warning and checkbox (SDUI `boolean`) sit below the credential inputs in `render()`, `components()` and first-run mode. `chrome_llm_save`, `llm_config_set` and `chrome_typesafe_save` each call `data_sharing.require_acknowledgment(user_id, submitted)` **before** any probe. Acknowledgments go through Deep `data_sharing_store` to Plane `DataSharingAcknowledgmentRepository`, with audit `llm_data_sharing.acknowledged`. Chat and existing sessions never consult the store.

**Web**: Projection `shell.html` / `astral.css` / `client.js` (a8p layout, Open Sans, ThemeView variables) + `renderer.py` (6 types) + `rote/` (ladders, web-profile rules) + `contracts/ui_protocol.json`.

## Delivery sequence

Repositories are committed separately, then pinned in Deep.

1. **AstralPrimitives 0.4.0**: six types, tests, README, contract digest. *(Blocks 3 and 5.)*
2. **AstralPlane `089.001`**: both tables (credential + data-sharing acknowledgment), repositories, migration, tests, docs. *(Blocks 4.)*
3. **AstralProjection (server and web only)**: renderers, ROTE ladders and web-profile rules, manifest and disposition data, web shell redesign, Open Sans, Playwright parity and responsive suites. *(Depends on 1; the shell work can start in parallel with 1.)*
4. **AstralDeep foundation**: dependency pin, adapter package, credential and acknowledgment stores, secret hygiene, environment exclusion, settings section, data-sharing checkbox and enforcement, measurement spike and calibration with the owner's test credentials (FR-044). *(Depends on 2; the adapter can start with fakes.)*
5. **AstralDeep integration**: I1–I7 turn seams, layout composer, audit and metrics, composition repin (`config/astral-composition.json` + submodules) to the commits from 1–3. *(Depends on 1–4.)*
6. **Qualification**: local full suites, latency and failure-injection runs, parity scoring, responsive checklist, no-client-diff and no-workflow-diff checks, all recorded in `verification.md`.
7. **Continuous (all steps)**: kos-wiki reference before each phase and checkpoint updates after each phase and milestone (see "Knowledge base cadence").

The owner merges in order 1 → 2 → 3 → Deep.

## Knowledge base cadence (kos-wiki)

kos-wiki (`Y:\WORK\kos-wiki`, private remote) is the team's ground-truth knowledge graph. 089 both **consumes** and **feeds** it on a fixed rhythm (FR-040 to FR-043, SC-013). Wiki work never blocks product work, never touches product repositories, and follows the vault's own `CLAUDE.md` schema.

### Reference (read) — before each phase

| Phase | Read before starting |
|---|---|
| 1 Setup | `index.md`, `project-a8p`, `project-astral`, `synthesis-astral-rewrite-review`, `astral-open-follow-ups`, `astral-ci-gates`, `astral-feature-timeline` |
| 2 Foundational | `astral-primitives`, `astral-primitive-serialization-contract`, `astral-llm-credential-resolution`, `astral-feature-flags`, `astral-project-constitution` |
| 3 US1 settings | `astral-llm-credential-resolution`, `astral-auth-architecture`, `astral-audit-system` |
| 4 US2 routing | `astral-orchestrator`, `astral-agent-catalog`, `astral-performance-architecture`, `project-a8p` |
| 5 US3 resilience | `astral-runtime-reliability-hardening`, `astral-performance-architecture` |
| 6 US4 security | `astral-security-defense-layers`, `astral-security-benchmark-harness`, `astral-execution-layer-security-gaps`, `astral-delegated-authority-framework` |
| 7 US5 UI generation | `astral-adaptive-ui-designer`, `astral-sdui-pipeline`, `astral-primitives`, `astral-rote`, `astral-cross-client-contracts` |
| 8 US6 web shell | `astral-web-client`, `astral-canvas-first-uiux`, `astral-rote`, `astral-cross-client-contracts` |
| 9 Qualification | `astral-dev-verification-workflow`, `astral-open-follow-ups`, `astral-feature-timeline` |

When a wiki claim conflicts with code at the 089 base, the code wins. The conflict is filed back as a `> [!contradiction]` callout at the next checkpoint.

### Update (write) — at checkpoints and milestones

**When**: whichever comes first of:
- (a) the end of each of the nine task phases (the "Checkpoint" lines in `tasks.md`);
- (b) each milestone:
  - spec and plan committed;
  - dependency approval recorded;
  - each component PR opened or merged;
  - `089.001` upgrade rehearsed;
  - latency, tier and security calibration results recorded;
  - known divergences recorded;
  - composition repin;
  - final qualification;
- (c) the end of any working day on which 089 work landed.

**What**:

| Page | Content recorded over 089 |
|---|---|
| `project-a8p` | Status change from proof of concept to "integrated into Astral via 089"; which ideas were adopted and which code was deliberately not ported, with the reasons |
| `project-astral` | 089 in Components/Timeline |
| New `astral-typesafe-routing` | Deep-dive, `type: synthesis`, bare `astral-*` slug per vault convention. Covers the routing seam, tiers, budget/circuit, additive security, style layout, measured latency and calibration summaries (numbers with dates and SHAs, no raw data) |
| `astral-llm-credential-resolution` | Per-user TypeSafe credential, no env/system key, gate isolation |
| `astral-security-defense-layers` | TypeSafe ingress screen as an additive layer; verdict tiers |
| `astral-adaptive-ui-designer` | Style layout as the keyed path; designer as fallback |
| `astral-primitives` | 0.4.0 types |
| `astral-rote` | New ladders; web-profile tuning; non-web guardrail |
| `astral-web-client` | a8p layout, Open Sans, desktop parity score, responsive checklist |
| `astral-cross-client-contracts` | Manifest change and the recorded native/Windows drift-guard divergence |
| `astral-feature-flags` | Any new flag or constant |
| `astral-ci-gates` | The owner's 089 CI exception |
| `astral-feature-timeline` | Dated 089 milestones |
| `astral-open-follow-ups` | Open items (for example native adoption of new types, refuse-tier enablement status) |

**How** (vault schema):
- Provenance on every fact: `extrepo:AstralDeep/<repo>@<sha>`, `doc:089-typesafe-a8p-integration` (dated), `conv:2026-09-17` for owner directives.
- Append a `## [YYYY-MM-DD] checkpoint | 089 <phase or milestone>` entry to `log.md`.
- Update `index.md` for new pages.
- Add two-way links.
- Use contradiction callouts rather than overwrites.
- Label local-only evidence as local, never as qualified.
- Commit and push the vault per its pre-authorized rule.
- A wiki lint pass runs at Phase 9.

**Never recorded**: key values or prefixes, credentials, raw logs or evidence, PHI, prompt content, or AI-assistant contributor credit.

## Project Structure

### Documentation (this feature)

```text
specs/089-typesafe-a8p-integration/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── verification.md            # baselines, measurements, calibration, known divergences, local evidence
├── tasks.md
├── reference/                  # a8p desktop captures + region bounding boxes (T046)
└── contracts/
    ├── typesafe-routing.md
    ├── typesafe-settings.md
    ├── ui-primitives-089.md
    └── web-layout-parity.md
```

### Source Code

```text
# AstralDeep
backend/requirements.txt                                   # + typesafe-sdk==0.6.0
backend/orchestrator/typesafe_routing/
├── __init__.py            # start_routing, await_decision, apply_round_one, security_verdict
├── client.py              # explicit-arg AsyncTypeSafeClient factory, lazy import, egress check
├── questions.py           # RoutingRequest → state + questions (fan-out)
├── decision.py            # RoutingDecision parsing + tiers
├── budget.py              # attempts, deadline, backoff, error classes, circuit
├── security_policy.py     # additive verdict + thresholds
├── layout.py              # style → designer-format layout
└── notices.py             # RoutingNotifier over _send_chat_status
backend/llm_config/typesafe_store.py                       # encrypt/decrypt facade over Plane repo
backend/llm_config/typesafe_handlers.py                    # save/clear/probe handlers
backend/llm_config/data_sharing.py                         # notice text/version, require_acknowledgment, store facade
backend/llm_config/log_scrub.py, audit_events.py           # *_api_key redaction, token pattern
backend/orchestrator/projection_surfaces/llm.py            # TypeSafe section (render + components)
backend/orchestrator/orchestrator.py                       # seams I1–I6, _call_llm tool_choice kwarg, action routing
backend/orchestrator/session_store.py                      # production-posture TYPESAFE_* refusal
backend/orchestrator/sandbox.py, verification/config.py, .gitleaks.toml
backend/tests/test_typesafe_*.py                           # adapter, store, settings, turn seams, security, layout, env inertness
scripts/verification/typesafe_routing_bench.py                          # local-only measurement spike
config/astral-composition.json                             # repins

# AstralPrimitives
src/astralprims/primitives.py, __init__.py, README.md, tests/test_primitives.py, pyproject.toml (0.4.0)

# AstralPlane
src/astralplane/database/typesafe_credential_schema.py, migrations.py, revision.py, baseline.py
src/astralplane/repositories/secrets.py, repositories/preferences.py (DataSharingAcknowledgmentRepository)
tests/test_schema_migrations.py, tests/repositories/test_typesafe_credential.py
docs/migration-and-recovery.md, provenance/transformations.json

# AstralProjection (NO changes under windows-client/, android-client/, apple-clients/, .github/workflows/)
backend/webrender/templates/shell.html
backend/webrender/static/astral.css, client.js
backend/webrender/static/fonts/open-sans-*.woff2, OFL.txt
backend/webrender/renderer.py
backend/rote/fallback.py, adapter.py, capabilities.py
contracts/ui_protocol.json
tests/webrender/test_primitives_089.py, tests/rote/test_web_profiles_089.py, tests/rote/test_non_web_unchanged_089.py
tests/web_layout_parity/ (Playwright), tooling/web-ci config (existing)
```

**Structure Decision**:
- A dedicated `typesafe_routing` package keeps every TypeSafe-specific decision behind a narrow interface, so `orchestrator.py` gains only call sites. A later extraction of the turn loop can move those call sites without touching the adapter.
- Web-only client scope maps onto Projection's existing `backend/webrender` and `backend/rote` directories.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| TypeSafe p95 latency exceeds budget on large tool sets | T015 spike sets per-attempt timeout and fan-out caps. Concurrency with prep absorbs most latency. The circuit bounds sustained slowness. |
| Forced `tool_choice` unsupported by a provider | Provider allowlist and a one-shot `"auto"` retry (I3). |
| Mis-routing narrows away the right tool | High tier requires a margin. Round ≥2 restores the full list. Fixture accuracy is gated by SC-005. |
| False-positive refusals interrupt users | Refuse tier stays disabled until calibration meets SC-006. Confirmation reuses the existing approval UX. |
| Owner key leakage into production | Explicit constructor arguments, boot refusal, env-inertness tests, sandbox denylist, redaction. Owner test credentials are local-only, entered through the settings path or stdin, and removed after qualification (FR-044). |
| Acknowledgment blocks existing users | Enforced only at credential save; chat never consults it (FR-048); tested. |
| Legacy `llm_config_set` clients lack the field | Explicit error directing to the server-owned settings surface, available on every client; recorded as a known web-first trade-off. |
| Web shell rewrite regresses 088 journeys | Keep `client.js` protocol and handler logic, restyle and restructure the shell DOM, and run the 088 Playwright journeys at every viewport. |
| ROTE web tuning leaks into native output | Profile-keyed rules + byte-identical golden tests for non-web profiles. |
| Native drift guards fail on manifest change | Accepted known divergence (owner directive), recorded. Runtime protected by server-side down-conversion. |
| Large `orchestrator.py` merge conflicts | Seams are small and localized. Rebase on `main` before the Deep PR. |

## Complexity Tracking

| Item | Why needed | Simpler alternative rejected because |
|---|---|---|
| Per-user in-process circuit | Required for "no hiccups" during sustained outages | Without it, every turn in an outage pays up to 1.5 s |
| Separate Plane table | Keeps the key independent of the LLM config lifecycle and the first-run gate | A column on `user_llm_config` is deleted on LLM clear and would satisfy the gate |
