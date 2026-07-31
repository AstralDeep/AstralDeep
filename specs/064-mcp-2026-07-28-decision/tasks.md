# Tasks: MCP `2026-07-28` — Conformance Decision and Phased Upgrade

**Input**: Design documents from `specs/064-mcp-2026-07-28-decision/`
**Prerequisites**: spec.md, plan.md (present); research.md, quickstart.md, contracts/ created by Phase 1 and Phase 2 below

**Tests**: INCLUDED — the spec explicitly mandates them (FR-056 adversarial suite, FR-057 drift guard, FR-058 live verification, SC-018 ≥90% changed-line coverage).

## Format: `[ID] [P?] [Story?] Description with file path`

- **[P]** = different files, no incomplete-task dependency; same-file tasks are serial even if conceptually parallel.
- **[US#]** is carried only by user-story phases. Setup, Foundational, and Polish tasks carry no story label.
- Every task names the requirement, success criterion, or constitution principle it satisfies.
- Repo-relative paths are shown; the repo root is the AstralDeep working tree.

---

## Phase 1: Setup (Shared Infrastructure)

- [ ] T001 [P] Write `specs/064-mcp-2026-07-28-decision/research.md` pinning the construct inventory from the published spec and `schema.ts`: the six `io.modelcontextprotocol/*` reserved `_meta` keys, `ResultType`, `CacheableResult`, `DiscoverResult`, `Tool`/`ToolAnnotations`, `InputRequiredResult`, `SubscriptionFilter`, and the `-32020`/`-32021`/`-32022` constants — each with identifier, normative level, and retrieval date per Constitution XIII.
- [ ] T002 [P] Record in `research.md` the corrections the changelog does not make obvious: `server/discover` is a server MUST and a client MAY, and its era-probe SHOULD is stdio-only (HTTP probes by inspecting a `400` body); extensions are versioned **independently of the core revision**, so there is no `2026-07-28` Tasks or Apps — Tasks is an independently evolving experimental extension whose normative document remains on its `draft` track, while Apps' published release is `2026-01-26` with its own `protocolVersion` axis; MCP Apps' identifier is `io.modelcontextprotocol/ui`; and the Tasks draft specifies `-32003` where core assigns `-32021`, while core leaves `-32000..-32019` implementation-defined and Astral deliberately allocates none there. (Constitution XIII — pinned citations)
- [ ] T003 Declare `FF_MCP_SERVER` in the single `FeatureFlags._flags` dict in [`backend/shared/feature_flags.py`](../../backend/shared/feature_flags.py) as `"mcp_server": self._read("FF_MCP_SERVER", False)`, with the house comment block naming the feature number, what OFF means byte-for-byte, the fail-closed default rationale, "read once at import (container recreate to enable)", and `See specs/064-mcp-2026-07-28-decision/.` (D9, FR-019)

---

## Phase 2: Foundational (Blocking Prerequisites)

**⚠️ CRITICAL**: no user-story work begins until this phase is complete.

- [ ] T004 Write `contracts/mcp-envelope.md`: the exact envelope delta (`result_type`, protocol-version and peer-identity metadata), the unknown-key tolerance rule, and — enumerated explicitly — **all five** implementations of the dispatch contract — including the orchestrator's draft/attachment-parser codegen template, which imports the shared dataclasses, and counting the BYO bundle header and body as one — with what changes in each and how a version-skewed pair behaves during rollout (FR-003).
- [ ] T005 [P] Write `contracts/mcp-endpoint.md`: the Streamable HTTP contract — POST-only path, the `MCP-Protocol-Version`/`Mcp-Method`/`Mcp-Name` header rules and the `=?base64?…?=` sentinel, header↔body validation, 202/400/403/404/405 semantics, and the exact `server/discover`, `tools/list`, `tools/call` request and result shapes (FR-020..FR-037).
- [ ] T006 [P] Write `contracts/mcp-render-target.md`: how a component tree projects into MCP content blocks, which primitive types map to which block type, the fallback for a type with no faithful projection, and where structured content comes from (FR-049, FR-050).
- [ ] T007 [P] Write `contracts/mcp-authorization.md`: the protected-resource metadata document, the `WWW-Authenticate` challenge shape on 401 and 403, the `astral-mcp` audience, the issuer check, and the explicit statement that **an OAuth scope is never sufficient authorization** in this system (FR-035, FR-038..FR-046).
- [ ] T008 Write `quickstart.md`: enablement, the Keycloak client and audience mapper, the reverse-proxy requirement that makes the canonical HTTPS resource identifier derivable, and a named `## Live-verification checklist (FR-058)` anchor the polish tasks link to.

**Checkpoint**: every contract exists and the envelope contract enumerates all five dispatch implementations. No code has changed yet.

---

## Phase 3: User Story 1 — The MCP envelope survives a version-skewed peer (P1) 🎯 MVP 🔒

**Goal**: an additive field on any MCP frame is survivable on every transport and every implementation.

**Independent Test**: a peer emits an MCP frame carrying an unknown key; the call completes normally with no dropped frame and no 30-second timeout.

### Tests (write first, ensure fail)

- [ ] T009 [P] [US1] In `backend/tests/test_mcp_envelope_064.py`, assert an `mcp_response` carrying an unknown key parses, populates known fields, discards the unknown one, and resolves the pending future — **must fail on pre-fix code** (FR-001, SC-001).
- [ ] T010 [P] [US1] Assert `mcp_request` unknown-field tolerance at the networked WebSocket agent parse ([`base_agent.py:279`](../../backend/shared/base_agent.py#L279)), 20 trials — **must fail on pre-fix code**. Separately assert, as a non-regression expected to pass both before and after, that the in-process and tunnel paths already ignore unknown request fields because neither routes a request through `Message.from_json` (FR-001, SC-001).
- [ ] T011 [P] [US1] Assert a genuinely malformed frame fails the pending request promptly with a logged protocol error rather than being swallowed to timeout (FR-002).
- [ ] T012 [P] [US1] Assert the fenced personal-agent path accepts an envelope field the orchestrator itself emits, and still refuses a field outside the allowlist fail-closed (FR-005).
- [ ] T013 [P] [US1] Assert `AgentCard` serialization round-trips a newly added skill field and that deserialization tolerates an unknown card key (FR-006).

### Implementation

- [ ] T014 [US1] Add an explicit valid-field filter to `MCPRequest`/`MCPResponse` construction in `Message.from_json` at [`backend/shared/protocol.py:94`](../../backend/shared/protocol.py#L94)–`97`, mirroring the `RegisterUI.from_json` pattern at `:1466`–`1477` and carrying the same explanatory comment (FR-001).
- [ ] T015 [US1] Distinguish "unknown field" from "malformed frame" in the two swallowing handlers — [`base_agent.py:292`](../../backend/shared/base_agent.py#L292)–`293` and [`orchestrator.py:3774`](../../backend/orchestrator/orchestrator.py#L3774)–`3775` — so a malformed frame fails its pending future instead of leaving the caller to the 30 s timeout at `orchestrator.py:14156` (FR-002).
- [ ] T016 [US1] Update the strict personal-agent response allowlist and the fence-equality check ([`orchestrator.py:2403`](../../backend/orchestrator/orchestrator.py#L2403)–`2415`, [`local_transport.py:118`](../../backend/shared/local_transport.py#L118)–`133`) so the fenced path never fails closed on a field the orchestrator emits (FR-005).
- [ ] T017 [US1] Make `AgentCard.to_dict` derive its skill keys rather than hand-building a fixed list, and give `AgentCard.from_dict` unknown-key tolerance ([`protocol.py:1002`](../../backend/shared/protocol.py#L1002)–`1033`) (FR-006).
- [ ] T018 [P] [US1] Apply the same tolerance to the BYO generated bundle template and its stdio admission check in [`backend/orchestrator/agent_generator.py`](../../backend/orchestrator/agent_generator.py) (FR-003).
- [ ] T019 [P] [US1] Apply the same tolerance to the Windows desktop client dispatch in [`windows-client/win_agent/agent.py`](../../windows-client/win_agent/agent.py) (FR-003).
- [ ] T020 [P] [US1] Apply the same tolerance to the A2A executor bridge in [`backend/shared/a2a_executor.py`](../../backend/shared/a2a_executor.py) and confirm [`a2a_bridge.py`](../../backend/shared/a2a_bridge.py) carries the envelope across the round trip (FR-003).
- [ ] T021 [US1] Verify already-delivered BYO bundles still function against the new orchestrator; if any cannot, mark them `revalidation_required` through the existing mechanism rather than letting them fail silently, and record which case applies (FR-004).

**Checkpoint**: Phase A's prerequisite is complete and independently mergeable. A live 30-second-hang defect is fixed.

---

## Phase 4: User Story 2 — Astral's MCP dialect honestly claims `2026-07-28` (P2)

**Depends on**: Phase 3 (US1) — the envelope cannot gain a field until an added field is survivable.

**Goal**: the envelope carries the protocol version and caller capabilities, both peers' identities, and a result-type discriminator, with error codes inside the reserved partition and a validated schema dialect.

**Independent Test**: capture one tool call's frames on each internal transport; every request carries a protocol version and the caller's declared capabilities, every response a discriminator and responder identity, and none carries an error alongside renderable components.

### Tests (write first, ensure fail)

- [ ] T022 [P] [US2] Assert every emitted MCP request carries the protocol version **and the caller's declared capabilities** — the two fields the specification marks REQUIRED — and that caller identity (optional) and responder identity are present at SHOULD level, across all three transports (FR-007, FR-008, SC-003).
- [ ] T023 [P] [US2] Assert no MCP response in a full suite run carries an error together with renderable components — **must fail on pre-fix code**, where all ten `agents/*/mcp_server.py` return `MCPResponse(error=…, ui_components=…)`. Cover the two pass-through relays too ([`orchestrator.py:2466`](../../backend/orchestrator/orchestrator.py#L2466), [`base_agent.py:679`](../../backend/shared/base_agent.py#L679)), neither of which enforces exclusivity today (FR-010, SC-002).
- [ ] T024 [P] [US2] Assert a peer declaring an unsupported protocol version is refused with the specification's unsupported-version error carrying the supported list, and is never processed (FR-011, SC-004).
- [ ] T025 [P] [US2] Assert no error code emitted on the MCP path falls in the reserved-legacy range (FR-012, SC-005).
- [ ] T026 [P] [US2] Assert a tool schema violating the declared dialect is refused at registration naming the tool and reason, and that no schema is rewritten before validation (FR-015, SC-006).
- [ ] T027 [P] [US2] Assert a tool schema whose `$ref` resolves to a network URI triggers **zero** outbound requests during validation and is refused rather than treated as permissive, across 10 attempts (FR-017, SC-019).
- [ ] T028 [P] [US2] Assert a schema declaring a dialect the system does not support produces an error naming the dialect, and that an absent declaration is treated as 2020-12 (FR-014).
- [ ] T029 [P] [US2] Assert protocol metadata never appears inside the tool arguments map, by dispatching to each of the six agents that splat `tool_fn(**arguments)` unfiltered (FR-008).

### Implementation

- [ ] T030 [US2] Add the result-type field to `MCPResponse` in [`backend/shared/protocol.py:173`](../../backend/shared/protocol.py#L173)–`185`, defaulting to the completed value, and add it to the personal-agent allowlist; decide and document whether it participates in the response digest at [`orchestrator.py:2435`](../../backend/orchestrator/orchestrator.py#L2435)–`2445` (FR-009).
- [ ] T031 [US2] Resolve result-xor-error: rework the paths that currently return an error dict alongside `ui_components` — [`agents/general/mcp_server.py:119`](../../backend/agents/general/mcp_server.py#L119)–`124` and its nine near-copies — to exactly one of the two (FR-010).
- [ ] T032 [US2] Set the result type at every `MCPResponse` construction site across the ten `agents/*/mcp_server.py` copies (FR-009).
- [ ] T033 [US2] Set the result type at the ~20 synthetic `MCPResponse` construction sites inside [`backend/orchestrator/orchestrator.py`](../../backend/orchestrator/orchestrator.py), including the offline, capacity, and not-connected paths (FR-009).
- [ ] T034 [P] [US2] Set the result type in the three non-dataclass implementations: the BYO bundle template, the Windows client, and the A2A executor bridge (FR-003, FR-009).
- [ ] T035 [US2] Add protocol-version, **caller-capabilities** and peer-identity fields to the envelope — protocol version and caller capabilities are REQUIRED by the specification, identity is a SHOULD — on the envelope itself and never inside `params["arguments"]`, and stamp them at the three `MCPRequest` construction sites and in `BaseA2AAgent`'s response path (FR-007, FR-008). Note [`windows-client/win_agent/agent.py:116`](../../windows-client/win_agent/agent.py#L116) already reads a `meta` field nothing has ever sent — this makes it real.
- [ ] T036 [US2] Refuse an unsupported declared protocol version with the specification's error and supported-version list, **and refuse a frame omitting a required metadata field with the specification's invalid-params semantics rather than processing it under assumed defaults**, at the agent-side and orchestrator-side entry points (FR-011).
- [ ] T037 [US2] Move Astral's MCP-path error codes out of the reserved-legacy range where they carry a specification-defined meaning, keeping routing keyed on the retryability flag as today (FR-012, FR-013).
- [ ] T038 [US2] Document the three coexisting error-code namespaces — the MCP integers, the string codes on the same error dict, and `OperationStatus._ERROR_CODES` — and name the reconciliation point at [`orchestrator.py:2447`](../../backend/orchestrator/orchestrator.py#L2447)–`2452` (FR-013).
- [ ] T039 [US2] Declare the JSON Schema 2020-12 dialect on tool input schemas (FR-014).
- [ ] T040 [US2] Split `_sanitize_tool_schema` at [`orchestrator.py:16713`](../../backend/orchestrator/orchestrator.py#L16713)–`16751` into validate-then-adapt: refuse a dialect-violating schema at registration with the tool named, and apply the model-facing function-grammar adaptation strictly downstream of validation (FR-015).
- [ ] T041 [US2] Enforce the schema-safety constraints in the registration validator: refuse network `$ref` dereference, refuse a schema that cannot validate without one, and bound validation by schema depth, subschema count, or time budget (FR-017).
- [ ] T042 [US2] Populate `AgentSkill.output_schema` at agent registration where the tool's result shape is derivable, and set it explicitly null where it is not, threading it through `AgentCard.to_dict` and the codegen templates (D11, FR-016).
- [ ] T043 [US2] Prove Phase A is independently shippable: run the full suite with `FF_MCP_SERVER` absent and no Phase B module importable, and assert no Phase A code path references `mcp_server_endpoint`, `mcp_authz`, or `mcp_projection` (FR-054).

**Checkpoint**: **Phase A is complete and shippable on its own** (FR-054). A merge may stop here.

---

## Phase 5: User Story 3 — A third-party MCP host can discover and list Astral's tools (P3) 🔒

**Depends on**: Phase 4 (US2).

**Goal**: a flag-gated, audience-bound, header-validated endpoint that discovers and lists the requesting user's authorized tools.

**Independent Test**: with the flag on, a real MCP host completes discovery, authenticates, and lists a set that changes when the user's agent permissions change.

**⚠️ Ordering**: authorization (T051–T056) MUST land with or before listing (T058–T061). An endpoint that lists tools before it validates audience is an unauthenticated catalog leak.

### Tests (write first, ensure fail)

- [ ] T044 [P] [US3] In `backend/tests/test_mcp_flag_off_064.py`, assert that with the flag off there is no MCP route, no protected-resource metadata document, no advertisement, and the suite result is identical to the pre-feature build (FR-019, SC-008).
- [ ] T045 [P] [US3] In `backend/tests/test_mcp_authz_064.py`, assert a token minted for the existing web client is refused at the MCP endpoint in 20 of 20 attempts, and an `astral-mcp` token is accepted (FR-039, SC-009).
- [ ] T046 [P] [US3] Assert the 401 challenge names the resource-metadata location and required scopes, and that a scope-insufficient valid token gets 403 with all needed scopes in one challenge (FR-042).
- [ ] T047 [P] [US3] In `backend/tests/test_mcp_adversarial_064.py`, sweep ≥20 attempts across missing header, mismatched header, non-ASCII name without encoding, invalid origin, removed HTTP method, oversized body, cookie-only credentials, and token-in-query-string; assert zero are served and each refusal carries the specification-defined status and code (FR-021, FR-022, FR-025, FR-026, FR-027, FR-028, FR-030, FR-031; SC-010).
- [ ] T048 [P] [US3] Assert a request omitting a required metadata field is refused with the specification's invalid-params error and HTTP 400, and a request needing an undeclared capability is refused with the missing-capability error carrying the missing list and HTTP 400 — the bodies a dual-era client inspects (FR-023, FR-024).
- [ ] T049 [P] [US3] In `backend/tests/test_mcp_endpoint_064.py`, assert discovery advertises supported versions, capabilities, identity, model-facing guidance, and **both** required cache fields — a freshness lifetime and a scope (FR-032).
- [ ] T050 [P] [US3] Assert two users with different agent permissions each see exactly their own chat-path authorized set, with zero cross-user leakage, and that the listing is marked privately cacheable (FR-033, SC-011, and the FR-057 drift guard).

### Implementation

- [ ] T051 [US3] Create `backend/orchestrator/mcp_authz.py`: an MCP-only bearer dependency validating audience against the `astral-mcp` client and validating the issuer, deliberately separate from `get_current_user_payload` so no existing route inherits it (FR-039, FR-040, FR-041).
- [ ] T052 [US3] Emit the `WWW-Authenticate` challenge on 401 and on 403 insufficient-scope, carrying the resource-metadata location, all required scopes in a single challenge, and the error parameter (FR-042).
- [ ] T053 [US3] Serve the protected-resource metadata document at the specification's well-known location, naming the authorization server and the supported scopes, with the resource identifier derived from the deployment's canonical HTTPS URL (FR-038).
- [ ] T054 [US3] Reject cookie credentials and query-string tokens on the MCP path; accept the authorization header only (FR-030).
- [ ] T055 [US3] Give the MCP path its own cross-origin policy with credentials disabled, leaving the existing application policy untouched (FR-031).
- [ ] T056 [US3] Ensure enabling the endpoint does not widen which client identifiers may drive refresh-token revocation — break the coupling with the native-logout allow-list derivation first if needed (FR-046).
- [ ] T057 [US3] Implement scope-hierarchy accounting when deciding whether a token is sufficient for an operation, and exclude the offline-access scope from both the challenge and the published protected-resource metadata (FR-043, FR-044).
- [ ] T058 [US3] Create `backend/orchestrator/mcp_server_endpoint.py`: the POST-only Streamable HTTP route, mounted only when the flag is on; header extraction and validation with the base64 sentinel decode; origin validation; 202 for notifications, 400 header-mismatch, 403 origin, 404 method-not-found, 405 for removed methods; removed session and resumption headers ignored; required-metadata and undeclared-capability refusals emitted as recognizable modern 400 bodies (FR-019, FR-020, FR-021, FR-022, FR-023, FR-024, FR-025, FR-026, FR-027).
- [ ] T059 [US3] Register the endpoint with the existing durable work-admission mechanism under its own admission class with stated active and queue limits, and enforce a documented request-body size cap (FR-028).
- [ ] T060 [US3] Implement `server/discover` returning supported versions, capabilities, server identity, model-facing guidance, and cache lifetime and scope (FR-032).
- [ ] T061 [US3] Create `backend/orchestrator/mcp_projection.py`: resolve the requesting user's authorized tool set through the **same** permission gate the chat path uses, project it into conformant tool descriptors with the specification's field naming, derive read-only and destructive hints from the existing single-source destructive classification, carry the authorization scope in metadata without treating it as authorization, and mark the result privately cacheable (FR-033, FR-034, FR-035).

**Checkpoint**: an authenticated third-party host can discover the server and list exactly what its user may run. Nothing executes yet.

---

## Phase 6: User Story 4 — A third-party MCP host can invoke a tool and render its result (P3) 🔒

**Depends on**: Phase 5 (US3) — nothing executes before discovery and authorization are proven.

**Goal**: invocation that faces the full gate stack, refuses destructive verbs honestly, and returns renderable content.

**Independent Test**: a real MCP host invokes a read-only tool and renders the result; the same call with permission revoked is refused.

### Tests (write first, ensure fail)

- [ ] T062 [P] [US4] Assert a tool whose permission is revoked between listing and invocation is refused at invocation in 10 of 10 trials (FR-047, SC-012).
- [ ] T063 [P] [US4] In `test_mcp_adversarial_064.py`, assert across ≥20 attempts spanning direct, chained, and parallel dispatch that zero destructive verbs execute over MCP and every refusal states the reason (FR-048, SC-013).
- [ ] T064 [P] [US4] Assert a raising tool is reported as a tool-level error inside a completed result, never as a protocol error (FR-051).
- [ ] T065 [P] [US4] Assert every MCP invocation emits the same audit events and correlation as the equivalent chat dispatch, distinguishable by invocation channel, and that the hash chain verifies (FR-053, SC-015).
- [ ] T066 [P] [US4] Assert that closing the response stream mid-invocation cancels the request, produces no further messages for it, and releases the admission slot (FR-029).
- [ ] T067 [P] [US4] Assert a tool that starts a tracked long-running operation returns that operation's identifier and Astral-side status, and never a protocol task handle (FR-052).

### Implementation

- [ ] T068 [US4] Treat a closed response stream as cancellation: stop work as soon as practical, emit nothing further for that request, and release the admission slot (FR-029).
- [ ] T069 [US4] Route MCP invocation through the shared `_authorize_and_prepare` gate stack rather than any reduced copy, so policy, taint, supervisor/HITL, delegation mint, credential injection, and the concurrency cap all apply (FR-047).
- [ ] T070 [US4] Refuse a destructive verb invoked over MCP before any transport contact, reusing feature 063's unattended-refusal predicate and extending it to recognize the MCP invocation channel; enforce it in the shared gate stack so chained and parallel paths inherit it (FR-048).
- [ ] T071 [US4] Create `backend/webrender/targets/mcp_renderer.py` and register it as the `mcp` target through `register_target()` in [`backend/webrender/registry.py`](../../backend/webrender/registry.py), projecting a component tree into MCP content blocks per the contract, with an honest fallback for any type that has no faithful projection (FR-049).
- [ ] T072 [US4] Return the machine-readable portion of a tool result as structured content alongside the rendered content (FR-050).
- [ ] T073 [US4] Report tool failures as tool-level errors within a completed result (FR-051).
- [ ] T074 [US4] Surface a tracked long-running operation by identifier and Astral-side status (FR-052).
- [ ] T075 [US4] Emit audit events for MCP invocation matching the chat dispatch's class and correlation, with an invocation-channel attribute (FR-053).
- [ ] T076 [US4] Assert and enforce that a token received at the MCP endpoint is **never** forwarded to any upstream service: any upstream call made while serving an MCP request uses a separately minted credential, and a test plants the inbound token as a tripwire value and asserts it appears in zero outbound requests (FR-045).

**Checkpoint**: full round trip works from a real MCP host, and the security-critical refusals are proven before the projection is polished.

---

## Phase 7: User Story 5 — The endpoint declares honestly what it does not support (P3)

**Depends on**: Phase 5's discovery; may run alongside Phase 6.

**Goal**: no over-claimed capability, anywhere.

**Independent Test**: a host requesting an unsupported extension or notification type gets an explicit non-advertisement or a specification-defined error, never a stall.

### Tests (write first, ensure fail)

- [ ] T077 [P] [US5] Assert discovery advertises no unsupported extension and advertises list-changed support as absent, and that a `subscriptions/listen` request is **acknowledged with an empty agreed notification set** rather than refused with method-not-found (D6, FR-036).
- [ ] T078 [P] [US5] Assert a host that declared the Tasks extension never receives a task handle, and that a task-shaped result type is refused (FR-037).
- [ ] T079 [P] [US5] Assert an unimplemented method is refused with 404 and method-not-found per the transport rules (FR-026).
- [ ] T080 [P] [US5] Assert that turning the flag off after it has been on leaves no residual route, advertisement, or persisted record (FR-055, SC-016).

### Implementation

- [ ] T081 [US5] Accept `subscriptions/listen` and acknowledge it with an **empty agreed notification set**, emitting no notification on that subscription, so the specification's subscribe-and-notify message pattern is answered rather than refused (D6, FR-036).
- [ ] T082 [US5] Implement the capability advertisement: no extensions, `listChanged` absent, task result type refused (FR-036, FR-037).
- [ ] T083 [US5] Implement flag-off teardown so that disabling the flag after it has been on removes the route, the advertisement, and any persisted record (FR-055, SC-016).

**Checkpoint**: every capability claim the endpoint makes is one it honors.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [ ] T084 [P] Run `ruff check .` from the repository root on the host (Constitution IV, XI).
- [ ] T085 Run both pytest invocations inside the built image against `postgres:17-alpine`; confirm changed-code coverage ≥90% via diff-cover (Constitution III, XI; SC-018).
- [ ] T086 [P] Assert [`backend/shared/ui_protocol.json`](../../backend/shared/ui_protocol.json) is byte-identical to the pre-feature build and that all four clients' drift-guard suites pass unchanged (FR-018, SC-007, Constitution XII).
- [ ] T087 [P] Confirm zero new third-party runtime dependencies; if any was unavoidable, record the approval and full transitive set in the PR description and add a Complexity Tracking row (FR-060, SC-017, Constitution V).
- [ ] T088 [P] Write the tracked `docs/mcp-server-endpoint.md` operator guide (with an explicit `.gitignore` exception): enablement, identity-provider configuration, the audience and scope model, the honest statement of what is not supported and why, the destructive-verb refusal, and the rollback path (FR-059).
- [ ] T089 [P] Update [`docs/keycloak-realm-settings.md`](../../docs/keycloak-realm-settings.md) with the `astral-mcp` client and its audience mapper, following the pattern the delegation setup already documents (FR-059).
- [ ] T090 Validate `quickstart.md` end to end on a staging deployment of the candidate image with real auth, database, and workers — including the identity-provider configuration the operator doc references (FR-059, Constitution X).
- [ ] T091 Execute the `## Live-verification checklist (FR-058)` against a **real third-party MCP host** on that staging deployment: discovery, authentication, listing, read-only invocation, destructive refusal, revoked-permission refusal. Bind the evidence to the candidate SHA. If no third-party host implementing `2026-07-28` is available, use a purpose-built conformant client for diagnostic coverage and **record the gap explicitly**; do not check off T091 or claim FR-058/SC-014 complete until the real-host run passes (FR-058, SC-014, D10).
- [ ] T092 [P] Assert `backend/tests/test_mcp_adversarial_064.py` covers all seven FR-056 categories, with a test that fails if any category has zero cases (FR-056).
- [ ] T093 Run the deterministic local release-evidence collection, normalization and parsing command before push; retain the canonical inputs and their digests, and prove the local result is diagnostic only and emits no authorization claim (Constitution X).
- [ ] T094 Add the CLAUDE.md "Recent Changes" entry, distinguishing what is PROVEN LIVE from what is code-shaped, and stating plainly that MCP Apps and Tasks are not adopted, while `subscriptions/listen` is supported only as an empty-set acknowledgement with no advertised notification types (D4–D6, FR-058, Constitution X).
- [ ] T095 Update the kos-wiki `astral-mcp-2026-07-28-upgrade` page and `astral-feature-timeline` with the shipped outcome, and append a `log.md` entry *(external repo — not part of the candidate diff; see spec.md §Dependencies)* (Constitution XIII).
- [ ] T096 [P] Add redacted structured failure logging and low-cardinality MCP request/refusal/completion/duration metrics through `backend/orchestrator/runtime_observability.py`; test that bearer material, request payloads, tool arguments, user/chat identifiers, and arbitrary labels cannot appear (FR-061, SC-020, Constitution X).

---

## Dependencies & Execution Order

### Phase dependencies

- Phase 1 (Setup) → Phase 2 (Foundational) → everything else.
- Phase 3 (US1) blocks Phase 4 (US2): the envelope cannot gain a field until an added field is survivable.
- Phase 4 (US2) blocks Phase 5 (US3): the endpoint's result shaping depends on the envelope semantics.
- Phase 5 (US3) blocks Phase 6 (US4): nothing may execute before discovery and authorization are proven.
- Phase 7 (US5) depends only on Phase 5's discovery and may run alongside Phase 6.
- Phase 8 (Polish) last.

### Critical ordering

- **T014 before everything else in Phase 3.** It is the tolerance fix; every other envelope change depends on it.
- **T051–T057 (authorization) with or before T058–T061 (endpoint and listing).** Listing tools before validating audience is an unauthenticated catalog leak. This is the security gate of Phase B, mirroring 063's US3-before-US5 ordering.
- **T069–T070 (gate stack and destructive refusal) before T071–T074 (projection).** Prove nothing dangerous executes before making results pretty.
- **T083 (flag-off residue) after T082**, since it verifies the advertisement it disables.
- **T091 (live verification) last among implementation work**, and it gates the merge of Phase B per Constitution X.

### Parallel opportunities

- T001/T002 with T003.
- T005/T006/T007 with each other (T004 is the blocker, not a peer).
- All Phase 3 tests (T009–T013) with each other; then T018/T019/T020 with each other.
- All Phase 4 tests (T022–T029) with each other.
- All Phase 5 tests (T044–T050) with each other.
- All Phase 6 tests (T062–T067) with each other.
- T084, T086, T087, T088, T089 with each other.

## Implementation Strategy

Ship **Phase A alone first** (through T042) as its own reviewable unit. It fixes a live defect, touches no client, needs no flag, and needs no identity-provider change — and per FR-054 it is complete on its own. Phase B (T044 onward) is a second unit behind `FF_MCP_SERVER`, and its merge is gated on T091's live verification.

If Phase B slips, Phase A still delivers: the envelope becomes version-skew-tolerant, stops delivering renderable components alongside an error, and honestly declares the revision it speaks.

## Notes

- **[P]** = different files, no incomplete-task dependency; same-file tasks are serial even if conceptually parallel.
- The ten `agents/*/mcp_server.py` copies are near-duplicates but **separate files**, so T032 is parallelizable in principle; it is left serial because the ten diverge in kwarg handling (six splat unfiltered, three filter by signature, one passes positionally) and a single reviewer pass over all ten is cheaper than ten reviews.
- Consolidating the five parallel dispatch implementations into one shared implementation is the right long-term fix and is **deliberately not in this feature** — doing it under a wire change would conflate two risks. It belongs in the follow-ups register.
- Two pre-existing findings surfaced while surveying the HTTP surface — the unauthenticated `/a2a` JSON-RPC mount with its constructed-but-unused security validator, and several unauthenticated REST routes — are real, are **out of scope here**, and belong in the follow-ups register rather than in this diff.
- Total: **96 tasks** — Setup 3, Foundational 5, US1 13, US2 22, US3 18, US4 15, US5 7, Polish 13.
