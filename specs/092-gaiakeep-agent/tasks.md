# Tasks: GaiaKeep agent

Input: spec.md, plan.md, research.md, data-model.md and contracts/agent.md in this directory.

## Setup and foundation

- [x] T001 Declare exact optional SDK revision and verified WebSocket dependency in backend/requirements-gaiakeep.txt and reproducible test dependencies in tooling/requirements-gaiakeep-tests.txt.
- [x] T002 [P] Build factual closed action metadata in backend/agents/gaiakeep/capabilities.json and schema/policy construction in backend/agents/gaiakeep/catalog.py (FR-001, FR-002).
- [x] T003 Write failing catalog and hostile-argument tests in backend/agents/gaiakeep/tests/test_catalog.py.
- [x] T004 Write failing verified TLS, SSH pin, routing, bounded decoding and cleanup tests in backend/agents/gaiakeep/tests/test_transport.py.
- [x] T005 Add a pinned SSH channel context to backend/orchestrator/remote_transport.py, retaining address and connected-peer gates (FR-006).
- [x] T006 Implement bounded wsapi/dataplane framing in backend/agents/gaiakeep/transport.py without unverified TLS or mutation replay (FR-007, FR-009, FR-010).

## US1: All public operations

Independent test: tools/list exposes the exact pinned catalog; fake native replies exercise successful reads, writes, upstream denials and unknown outcomes.

- [x] T007 [US1] Write failing native client/result tests in backend/agents/gaiakeep/tests/test_client.py.
- [x] T008 [US1] Implement pinned SDK composition, signed requests, admitted redirects and truthful error handling in backend/agents/gaiakeep/client.py.
- [x] T009 [US1] Implement typed MCP dispatch and standard primitive results in backend/agents/gaiakeep/mcp_server.py (FR-001, FR-010, FR-012).
- [x] T010 [US1] Fail closed for legacy operations without operator opt-in; constrain remote restore paths and protect caller/token fields; reject unsupported legal-order shortening (FR-002, FR-007).

## US2: Owner-isolated connection and approval

Independent test: one owner cannot load another owner's machine; altered arguments, stale approval, unattended mutation and protected-field injection fail before network activity.

- [x] T011 [US2] Write failing MCP, identity, credential and approval tests in backend/agents/gaiakeep/tests/test_dispatch.py and backend/tests/test_gaiakeep_confirmation.py.
- [x] T012 [US2] Bind initialized Plane repositories and encrypted credential metadata in backend/agents/gaiakeep/gaiakeep_agent.py (FR-006, FR-008).
- [x] T013 [US2] Extend backend/orchestrator/remote_confirmation.py for exact-argument single-use approval of all GaiaKeep mutations (FR-003, FR-004).
- [x] T014 [US2] Add default-off registration in backend/orchestrator/local_agents.py and backend/shared/feature_flags.py; classify GaiaKeep output untrusted in backend/orchestrator/taint.py (FR-003, FR-005).
- [x] T015 [US2] Verify catalog permissions, real confirmation evaluation and consumed/altered approval denials through the shared dispatch seam.

## US3: Bounded file flows

Independent test: upload/read round trips use agent-owned temporary files and upstream integrity verification; oversize input, invalid paths, wrong digests and interrupted mutations fail honestly.

- [x] T016 [US3] Write failing bounded transfer and cleanup tests in backend/agents/gaiakeep/tests/test_client.py.
- [x] T017 [US3] Implement bounded upload and read wrappers in backend/agents/gaiakeep/client.py with temporary-file cleanup and no arbitrary host filesystem access (FR-011).
- [x] T018 [US3] Add closed convenience-tool schemas and standard responses in backend/agents/gaiakeep/catalog.py and backend/agents/gaiakeep/mcp_server.py.

## Qualification and handoff

- [x] T019 Run Python 3.11 GaiaKeep tests and relevant shared policy/transport/registration tests; demonstrate at least 90% changed-line coverage and pass Ruff for changed Python.
- [x] T020 Validate automatic module-suite discovery and document optional installation, credential fields, trust configuration and pending endpoint in specs/092-gaiakeep-agent/quickstart.md.
- [x] T021 Perform local security review of owner isolation, signed principal, protected parameters, TLS on both channels, route allowlist, approval and secret redaction; repair findings with tests.
- [x] T022 Update curated knowledge vault pages, index.md and log.md, commit and push the vault; leave an exact local product handoff without product push.
- [ ] T023 Once the owner provides the interface, qualify the exact candidate in Linux staging against the real Gaia dependency, exercising read, denial, approved mutation, job reconciliation and encrypted file integrity; retain candidate-bound evidence.
- [ ] T024 Exercise standard credential/approval/results on every affected client against staging and collect required local/release-evidence gates before merge. No gate or platform waiver is implied by the pending interface.
- [ ] T025 Qualify and implement legacy fetch only if needed by the owner's deployment: resolve and admit the origin, open a verified-TLS receiver before dispatch, use exact legacy transfer/sequence framing, require contiguous bounded bytes and origin/index SHA256 checks, and validate real legacy grants. Until then the tool refuses before any connection; the pinned SDK has no supported legacy receiver. This remains an explicit incomplete capability, not a live-ready claim.

## Dependencies and execution

T001–T006 precede the native client. T007 precedes T008–T010. T011 precedes T012–T015. T016 precedes T017–T018. Qualification follows implementation. T023–T025 depend on the owner's pending endpoint and remain open in this task; no Gaia instance is provisioned to satisfy them.

Independent catalog research/tests and transport research/tests may run in parallel in separate files. Implementation follows the three stories in order, then shared verification and a scoped local commit. All mutations use the existing confirmation repository; no schema, component vocabulary, native UI contract or external fabric deployment changes are planned.
