# Implementation Plan: MCP `2026-07-28` — Conformance Decision and Phased Upgrade

**Branch**: `064-mcp-2026-07-28-decision` | **Date**: 2026-07-29 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/064-mcp-2026-07-28-decision/spec.md`; owner decisions D1–D11 recorded therein; conformance read verified against `main@e492668b`.

## Summary

Two independently shippable phases behind one spec.

**Phase A** makes Astral's own MCP envelope honestly `2026-07-28`. Its first task is not an MCP change at all: `Message.from_json` splats raw JSON keys into `MCPRequest`/`MCPResponse` ([`protocol.py:94`](../../backend/shared/protocol.py#L94)–`97`) with no unknown-key filter, so *any* additive field raises `TypeError` wherever that dispatcher parses a frame, gets swallowed, and manifests as a 30-second "Tool call timed out". The exposure is narrower than "every transport" — the request direction bites only at networked WebSocket agents, while the response direction bites everywhere — and the spec states it precisely. Nothing else in this feature is safe until it is fixed. Then the envelope gains a result-type discriminator (which also resolves the existing error-alongside-components ambiguity), per-request protocol version and caller capabilities, peer identity, an error-code posture inside the specification's reserved partition, and schema validation at registration instead of silent repair.

**Phase B** exposes a conformant MCP `2026-07-28` server endpoint behind `FF_MCP_SERVER` (default off), publishing the **per-user authorized** tool set — not the catalog — to third-party hosts over Streamable HTTP, with a real OAuth 2.1 protected-resource posture on a **new Keycloak audience**. The result projection is the interesting part: MCP becomes a **fourth `webrender` render target** registered through the existing `register_target()` seam, which is exactly what Constitution II promises and what feature 063 demonstrated when a new surface reached every client with zero per-client code.

MCP Apps and Tasks are explicitly **not** adopted. The endpoint advertises no change-notification capability, but it does implement the core subscribe-and-notify message pattern minimally: `subscriptions/listen` is acknowledged with an empty agreed notification set and emits no notifications (D4–D6).

## Technical Context

**Language/Version**: Python 3.11 (backend, production image; local `.venv` 3.13). No client-language change — Phase A is agent-channel-only (D3) and Phase B adds no client-facing frame.

**Primary Dependencies**: Existing only — FastAPI, `websockets`, psycopg2, `python-jose` (JWT/JWKS), `cryptography`, `astralprims` (consumed unchanged), the existing `audit`, `tool_permissions`, `delegation`, `web_auth`, `work_admission`, `webrender`, and `rote` modules. **Zero new third-party runtime dependencies** (FR-060, SC-017). The SSE response stream is served with FastAPI's existing streaming response; no SSE library is added.

**Storage**: One additive coordination change: `SCHEMA_REVISION` becomes
`064.001`, the admission-class constraint gains `mcp`, and the existing
durable work-admission tables receive an `mcp` child class with 8 active / 32
queued / 5000 ms maximum wait plus its eight slots. MCP clients, sessions,
subscriptions, and advertisements are not persisted by Astral. Invocations
also use the existing `audit_events` table and, where a tool starts one, the
existing `tracked_job` table. See `data-model.md` for repeat-safe migration and
rollback/recovery.

**Testing**: `pytest` + `pytest-asyncio` inside the built image against `postgres:17-alpine`, both invocations, per Constitution XI. New suites: `backend/tests/test_mcp_envelope_064.py` (Phase A tolerance + discriminator, regression-proving against the pre-fix code), `backend/tests/test_mcp_endpoint_064.py` (Phase B transport, headers, discovery, listing), `backend/tests/test_mcp_authz_064.py` (audience, challenge, isolation), `backend/tests/test_mcp_adversarial_064.py` (FR-056's ≥20-attempt sweeps), `backend/tests/test_mcp_flag_off_064.py` (SC-008 byte-identity).

**Target Platform**: Linux container (orchestrator). Phase B's endpoint is consumed by third-party MCP hosts, not by Astral's own clients.

**Project Type**: Backend service change. Phase A touches the shared protocol layer and every MCP dispatch implementation; Phase B adds one router, one renderer, one auth dependency.

**Performance Goals**: Discovery and tool listing respond within the existing REST budget. Concrete limits, referenced by FR-028 and FR-033 rather than left to the implementation: **request body cap 1 MiB**; a new admission class `MCP` with **8 active / 32 queued / max_wait_ms 5000** (the coordinator refuses a positive `queue_limit` with no positive `max_wait_ms`, and the class enum has no `mcp` member today — both are edits this feature makes); `tools/list` and `server/discover` cache lifetime **60 s**, scope **private** (the listing is per-user). The endpoint registers with `work_admission` under that class rather than inheriting an unbounded default.

**Constraints**: Flag-off must be byte-identical (FR-019, SC-008). Phase A must not touch [`backend/shared/ui_protocol.json`](../../backend/shared/ui_protocol.json) (FR-018, SC-007). Enabling real audience validation must not reach any existing route — today all three JWT validators run `verify_aud: False` and substitute an `azp` check — an allow-list in two of them, a hardcoded two-value comparison in the third, and all three skip it entirely when the token omits `azp` — so a global change would reject every token in circulation (FR-040). No token received at the MCP endpoint may be forwarded upstream (FR-045).

**Scale/Scope**: 92 tools across the 9 always-on built-in agents, 110 with `remote-compute-1` enabled, plus 8 orchestrator meta-tools across 5 pseudo-agents. **Five** independent MCP dispatch implementations must move together in Phase A: the ten `agents/*/mcp_server.py` servers, the orchestrator's draft/attachment-parser codegen template, the generated BYO bundle runner, the Windows desktop client, and the A2A executor bridge — plus the shared envelope definitions they depend on, the `base_agent`/`local_transport`/`a2a_bridge` carriers, the orchestrator's ~20 synthetic response-construction sites, and already-delivered BYO bundles and generated draft agents on disk.

## Constitution Check

*GATE: evaluated before Step S0 and re-checked after Step S1 design (below). Constitution v2.8.0.*

| Principle | Status | Note |
|---|---|---|
| I. Primary Language (Python) | PASS | All new code is Python 3.11 in `backend/`. No new language. |
| II. UI Delivery (astralprims → orchestrator renders → ROTE adapts) | PASS | FR-049 makes MCP a **render target registered via `register_target()`**, the seam `registry.py` documents as "a new target registers a renderer here; primitive definitions and agent code never change". D4 refuses MCP Apps precisely because it would invert this. No astralprims change; no agent change. |
| III. Testing Standards (≥90% changed-line) | PASS (plan) | Five new suites named under Technical Context; FR-056/FR-057 mandate the adversarial and drift-guard sets; SC-018 states the gate. Phase A tests must fail on pre-fix code (the 064 regression-proof pattern). |
| IV. Code Quality (lint) | PASS | `ruff check .` from repo root; no maintained TS/JS is touched (D3). |
| V. Dependency Management | PASS | FR-060 + SC-017: zero new third-party runtime dependencies. SSE is served by FastAPI's existing streaming response. If implementation discovers one is unavoidable, it goes to lead-dev approval, the PR description, and a Complexity Tracking row before it is written. |
| VI. Documentation | PASS (plan) | FR-059 mandates `docs/mcp-server-endpoint.md`. The new render target must document its supported targets per VI; every new public function carries a Google-style docstring; the endpoint appears in `/api/docs`. `contracts/` documents the five dispatch implementations (FR-003) and the envelope delta. |
| VII. Security | PASS | The load-bearing principle here. Keycloak remains the IAM (new audience, not a new provider). FR-047 forces MCP invocation through the **same** gate stack as chat — the 056 `_authorize_and_prepare` path, not a copy. FR-039/FR-040 add real audience validation on a **new** dependency so no existing route changes. FR-045 forbids token passthrough. FR-048 preserves 063's unattended refusal for destructive verbs across direct, chained, and parallel paths. FR-035 keeps an OAuth scope from ever being sufficient authorization. FR-030/FR-031 keep the endpoint header-bearer-only with its own CORS policy. Flag is fail-closed default off (D9). |
| VIII. User Experience | PASS / unchanged | No Astral-client UX change. MCP hosts render the same component tree every other target receives, projected by the new renderer. |
| IX. Database Migrations | PASS (code-shaped) | Implementation confirmed that a distinct durable admission class requires persistence. The repeat-safe `_migrate_mcp_admission_064` delta, `064.001` revision/guard update, `data-model.md`, and empty plus representative-existing-data assertions ship together. Rollback disables the endpoint and retains idle additive rows so referenced operation history is never destructively removed. The PostgreSQL migration suite remains a required verification gate. |
| X. Production Readiness | PASS (plan) | FR-058 + D10: Phase B validated end-to-end against a real third-party MCP host on a staging deployment of the **candidate image**, evidence bound to the candidate SHA; a purpose-built client is diagnostic only and anything lacking real-host proof remains code-shaped. No stubs or dev-only branches; the flag is real configuration, not an env-specific branch. FR-061 adds redacted structured failure logs and low-cardinality request/refusal/completion/duration metrics through the existing `RuntimeObservability` collector; FR-028 also publishes MCP admission gauges, while FR-053 supplies audit parity. FR-055/SC-016 give the rollback. **Release evidence**: the deterministic local collection/normalization/parsing command runs **before push** (a Polish task owns it); its output is diagnostic only and carries no authorization claim, and protected CI independently re-hashes and re-decides on the canonical evidence. This feature produces **no distributable release artifact**, so the separately pinned protected publisher and the exception/debt ledger are N/A, and it introduces no repository-scoped GitHub App, installation token, or custom token broker. |
| XI. Continuous Integration | PASS (plan) | All six gates apply: lint, both pytest invocations against a real database service, changed-code coverage ≥90%, image build, dev + production-posture boot smoke (production posture must still exit exactly 78), secret scan. Phase A must not perturb the boot smoke; Phase B's flag-off default means the smoke path is unchanged. |
| XII. Cross-Client Consistency | PASS | FR-018 + SC-007: `ui_protocol.json` is byte-identical and every client's drift-guard suite passes unchanged. MCP is **not a client target in the chrome sense** — it is a foreign host consuming tools, so it adds no menu, no settings surface, and no capability the four clients would need to mirror. This is not a web-only carve-out; it is a non-client surface, and the spec records it (D3, FR-018). |
| XIII. Documentation & Research Integrity | PASS | The verdict sections remain under XIII: every external claim is traceable to a cited source, and the implementation research refreshes the living sources on 2026-07-31, including the now-experimental Tasks extension whose normative document remains on its independent draft track, the separately versioned Apps release, and the implementation-defined core error partition. Claims about Astral's own behavior are anchored to `main@e492668b` with file:line, and the one place Astral does **not** meet a standard it is compared against — the 063 tracked-job durability gap — is stated rather than smoothed over. **Note the change from 064's merged decision-only revision (PR #150): this feature is no longer documentation-only**, so it does *not* claim XIII's exemption from III/X/XI for the Phase A/B diff. |

**Result**: no violations. Two principles carry conditional obligations that only bite if implementation discovers a need the design does not anticipate — V (a new dependency) and IX (persistence). Both are written above with the exact steps required, so neither can be adopted silently.

## Project Structure

### Documentation (this feature)

```text
specs/064-mcp-2026-07-28-decision/
  spec.md                       # decision verdicts (ratified) + Phase A/B requirements — AMENDED
  plan.md                       # this file — Phase 1
  tasks.md                      # Phase 2 (/speckit-tasks)
  research.md                   # the conformance read, construct-by-construct
  quickstart.md                 # enablement + the live-verification checklist
  data-model.md                 # MCP admission migration + rollback/recovery
  contracts/
    mcp-envelope.md             # envelope delta + the 5 dispatch implementations
    mcp-endpoint.md             # transport, headers, discovery, listing
    mcp-render-target.md        # component tree -> MCP content blocks
    mcp-authorization.md        # audience, PRM, challenge, scope-is-not-authz
```

### Source Code (repository root)

```text
backend/
  shared/
    protocol.py                 # EDIT - unknown-key filter on MCP envelopes (FR-001);
                                #        result_type + protocol metadata fields (FR-007..FR-009);
                                #        AgentCard.to_dict/from_dict tolerance (FR-006)
    base_agent.py               # EDIT - stamp responder identity; parse tolerance (FR-002)
    local_transport.py          # EDIT - fence/allowlist parity for the new fields (FR-005)
    a2a_bridge.py               # EDIT - carry the new envelope fields across the A2A round trip
    a2a_executor.py             # EDIT - 5th dispatch implementation (FR-003)
    feature_flags.py            # EDIT - FF_MCP_SERVER, default False, fail-closed (D9, FR-019)
  agents/*/mcp_server.py        # EDIT - ten near-copies: result_type on every construction (FR-009),
                                #        error-code partition (FR-012), result-xor-error (FR-010)
  orchestrator/
    work_admission.py           # EDIT - new AdmissionClass.MCP member + its AdmissionClassConfig
    orchestrator.py             # EDIT - the ~20 synthetic MCPResponse sites; the strict personal-agent
                                #        allowlist (FR-005); schema validate-then-sanitize (FR-015)
    agent_generator.py          # EDIT - BYO bundle template + stdio loop (FR-003, FR-004)
    mcp_server_endpoint.py      # NEW  - the Streamable HTTP router: POST, headers, 202/404/405,
                                #        server/discover, tools/list, tools/call (FR-020..FR-037)
    mcp_authz.py                # NEW  - MCP-only bearer dependency: audience + issuer validation,
                                #        WWW-Authenticate challenge, PRM document (FR-038..FR-046)
    mcp_projection.py           # NEW  - authorized-tool-set -> conformant Tool[]; annotations from
                                #        DESTRUCTIVE_CLASSIFICATION; scope in _meta (FR-033..FR-035)
  webrender/
    targets/mcp_renderer.py     # NEW  - the 4th render target, registered via register_target (FR-049)
    registry.py                 # EDIT - register the "mcp" target
  tests/                        # NEW  - the five suites named under Technical Context
windows-client/
  win_agent/agent.py            # EDIT - 4th dispatch implementation (FR-003); it already reads a
                                #        `meta` field nothing has ever sent — that becomes real
docs/
  mcp-server-endpoint.md        # NEW  - operator doc (FR-059)
  keycloak-realm-settings.md    # EDIT - the astral-mcp client + audience mapper
```

**Structure Decision**: Phase A edits the shared protocol layer and every implementation that hand-builds an MCP frame; it adds no module. Phase B adds exactly three orchestrator modules and one renderer, all reachable only when the flag is on, so the flag-off path is an import-time absence rather than a runtime branch. The MCP render target lives under `webrender/targets/` beside the existing `stub_renderer.py`, which is the repo's own proof that a target is one `register_target()` call.

## Phasing (plan steps are lettered; numbered phases below are tasks.md's)

**Step S0 — Research** (`research.md`, produced by tasks.md Phase 1). Pin the construct inventory: the six `io.modelcontextprotocol/*` reserved `_meta` keys, `ResultType`, `CacheableResult`, `DiscoverResult`, `Tool`/`ToolAnnotations`, the `-32020`/`-32021`/`-32022` constants, the Streamable HTTP header contract including the `=?base64?…?=` sentinel, and the server-vs-client obligation split in the authorization tree. Record the corrections the changelog summary does not make obvious: extensions are versioned independently of the core revision; Apps is `io.modelcontextprotocol/ui` with a `2026-01-26` release; Tasks is experimental while its normative document remains on its independent draft track; and its `-32003` meaning collides with core's `-32021`, even though core leaves `-32000..-32019` implementation-defined.

**Step S1 — Design** (`contracts/`, `quickstart.md`, produced by tasks.md Phase 2). Four contracts as listed. The envelope contract gates everything: it must enumerate all five dispatch implementations, count the draft/attachment-parser codegen template explicitly, treat the BYO bundle header and body as one implementation, and state what changes in each implementation and how a version-skewed pair behaves during rollout.

**Step S2 — Tasks** (`/speckit-tasks` → `tasks.md`).

**tasks.md Phase 3 — US1, envelope tolerance (P1).** Ships alone and is worth shipping alone: it fixes a live 30-second-hang defect. Tests must fail on pre-fix code.

**tasks.md Phase 4 — US2, dialect alignment (P2).** Depends on Phase 3. Result-type, protocol metadata, error-code partition, schema dialect. Ends Phase A; a merge may stop here (FR-054).

**tasks.md Phase 5 — US3, endpoint + discovery + listing (P3).** Depends on Phase 4 for the shared envelope semantics, and on `FF_MCP_SERVER`. Transport and authorization land together — an endpoint that lists tools before it validates audience is an unauthenticated catalog leak.

**tasks.md Phase 6 — US4, invocation (P3).** Depends on Phase 5. Must not land before it. The destructive refusal (FR-048) and gate-stack reuse (FR-047) are the security-critical tasks of this feature and are ordered before result projection, not after.

**tasks.md Phase 7 — US5, capability honesty (P3).** Small; depends on Phase 5's discovery. Advertising nothing false is what makes D5/D6 safe.

**tasks.md Phase 8 — Polish.** Lint, both pytest invocations with ≥90% changed-line coverage, `ui_protocol.json` byte-identity check plus all four client drift-guard suites, quickstart validation, the operator doc, the Keycloak doc edit, the staging validation against a real MCP host, explicit structured-log/runtime-metric verification, the deterministic local release-evidence run before push, the CLAUDE.md "Recent Changes" entry distinguishing PROVEN LIVE from code-shaped, and the external kos-wiki record noted under the spec's Dependencies.

## Complexity Tracking

Entries are approved deviations, not violations.

| Item | Why needed | Simpler alternative rejected because |
|---|---|---|
| Five parallel MCP dispatch implementations must change together | They already exist, and the two *generated* ones differ: the draft/attachment-parser template imports the shared dataclasses (so it inherits the defect), while the BYO bundle runner uses plain dicts (so it does not). Changing a subset produces exactly the silent version-skew hang FR-001 exists to prevent. | "Change only the backend" leaves the Windows client and every delivered BYO bundle on the old envelope, which is the failure mode, not a mitigation. Consolidating the five into one shared implementation is the right long-term fix but is a larger refactor than this feature, and doing it under a wire change would conflate two risks. |
| A second, MCP-only JWT validation path | The three existing validators disable audience checking and substitute an `azp` allow-list. MCP's core anti-confused-deputy rule is the opposite. | Turning on audience validation globally rejects every token in circulation, because user tokens carry `aud: "account"`. Relaxing the MCP route to the existing posture instead would accept a token minted for the web client at a tool-execution endpoint — precisely what RFC 8707 exists to prevent. |
| A fourth render target rather than reusing the web HTML projection | MCP hosts consume content blocks, not HTML; and reusing the `html` field would make MCP a web-shaped consumer, re-importing the reach problem D4 refuses. | Emitting the web HTML into a text block is expedient and wrong: it discards the structure that makes the result navigable, and it is exactly the generated-markup posture the MCP Apps verdict argues against. |
| Declaring `listChanged: false` and no extensions while acknowledging `subscriptions/listen` with an empty agreed set | `StreamManager` states in code that surviving an orchestrator restart is not a goal, and no tool-list change notification exists. The base message pattern is answered without claiming a notification type. | Advertising a capability and not honoring it produces silent hangs on the host side, while refusing the base method would be silent non-conformance. Building durable notification subscriptions is a feature of its own. |

## Post-Design Constitution Re-Check

*Re-evaluated after the Phase 1 design above. Constitution v2.8.0.*

**Post-design re-check**: PASS. Three design choices are what carry the gate:

1. **II and XII hold because MCP enters through the renderer registry**, not through a parallel UI path. The endpoint adds no frame to `ui_protocol.json`, no chrome, and no client obligation — which is why XII is a genuine PASS rather than a carve-out.
2. **VII holds because nothing is re-implemented.** MCP invocation routes through the same `_authorize_and_prepare` gate stack, the same `is_tool_allowed` resolution, the same destructive classification, and the same audit class as a chat dispatch. The only genuinely new authorization code is the audience-validating dependency, and it is deliberately isolated so that no existing route inherits it.
3. **IX stays N/A only because the design persists nothing new.** That is a design commitment, not an observation, and the table above states the exact obligations that attach the moment it stops being true.

The one principle whose verdict *changed* relative to 064's merged decision-only revision (PR #150) is **XIII**: that revision was documentation-only and claimed the III/X/XI exemption. This amendment carries product code and does not.
