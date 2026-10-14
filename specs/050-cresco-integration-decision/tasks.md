# Tasks: Cresco Integration (Bridge Agent)

**Feature**: 050-cresco-integration-decision | **Plan**: [plan.md](plan.md) | **Spec**: [spec.md](spec.md)
**Design artifacts**: [research.md](research.md) · [data-model.md](data-model.md) · [quickstart.md](quickstart.md) · [contracts/](contracts/) (wsapi-client / tool / audit).
Product feature — implementation tasks. Verification is end-to-end against a local single-node Cresco fabric plus unit/integration tests (Constitution III/X/XI apply).

**Bounded 2026-10-13 by [`specs/095-cresco-bridge-contract/`](../095-cresco-bridge-contract/spec.md)** (issue #279): all tasks below remain unchecked; any implementation MUST adopt the spec 095 contract and re-verify the 2026-07-06 upstream source pins first. T003a's registry target is retired (stale); see the reconciliation note in [spec.md](spec.md).

## Phase 0 — Scaffold

- [ ] T001 Create `backend/agents/cresco/` package matching the feature-040 discovery convention: `__init__.py`, **`cresco_agent.py`** (`class CrescoAgent(BaseA2AAgent)`, `agent_id="cresco-1"`), **`mcp_server.py`** (router; `self.tools = TOOL_REGISTRY`), **`mcp_tools.py`** (handlers + `TOOL_REGISTRY`), `wsapi_client.py`, `tests/`. (Discovery requires `<dir>/<dir>_agent.py`; do **not** name it `agent.py`.)
- [ ] T002 Add `FF_CRESCO` (default off, fail-closed) to `backend/shared/feature_flags.py`.
- [ ] T003 Register the agent: add `"cresco"` to `BUILT_IN_AGENT_DIRS` in `backend/orchestrator/local_agents.py` **and** add a `FF_CRESCO` `continue` guard in the `register_built_ins` loop (no per-agent flag pattern exists yet — introduce one). Seeded **not** safe (owner approval required). Flag-off ⇒ not registered (SC-001).
- [ ] T003a ~~Add `'cresco-1'` to `_FIRST_PARTY_PUBLIC_AGENT_IDS` in `backend/shared/database.py`~~ **RETIRED 2026-10-13 (stale target)**: `backend/shared/database.py` no longer exists; the equivalent registry is `FIRST_PARTY_PUBLIC_AGENT_IDS` in `backend/orchestrator/local_agents.py`. (Do **not** add it to `_UNTRUSTED_AGENTS` in `taint.py` — Cresco data is fabric-internal, not open-web content.)

## Phase 1 — wsapi client (P1 foundation)

- [ ] T004 [wsapi_client] Connect to `wss://{CRESCO_WSAPI_URL}/api/apisocket` with the `cresco_service_key` header using the existing `websockets` library; **no new dependency** (FR-002, SC-005).
- [ ] T005 [wsapi_client] Implement the RPC envelope `{message_info:{message_type, message_event_type, is_rpc}, message_payload:{action,…}}` and gzip+base64 param (de)coding with stdlib (`json`/`gzip`/`base64`).
- [ ] T006 [wsapi_client] Verified-TLS `ssl` context — system trust by default, `CRESCO_CA_BUNDLE` (trusted CA) or `CRESCO_TLS_FINGERPRINT` (pinned SHA-256); **never** `CERT_NONE`/global bypass; reject self-signed unless configured (FR-007, SC-006). This is the concrete divergence from `pycrescolib` (verify-off default).
- [ ] T007 [wsapi_client] Validate the wsapi host via `shared/external_http.py::validate_egress_url` before dialing; on-prem private hosts allowed only via the host-scoped `CRESCO_ALLOW_PRIVATE_HOST` opt-in (not a global bypass) (FR-007).
- [ ] T008 [wsapi_client] Bounded connect/RPC timeouts + stdlib reconnect/backoff (no `backoff` dependency); fail-safe on unexpected frames with a diagnostic (edge cases).
- [ ] T009 [tests] Unit-test the client against a mocked socket using the frame shapes captured in the evaluation (envelope, gzip params, TLS context, egress validation, fail-safe).

## Phase 2 — Read tools (US1, P1)

- [ ] T010 [tools] `cresco_list_regions`, `cresco_list_agents`, `cresco_agent_info`, `cresco_get_sysinfo`, bounded log/metric reads — **read scope**; render as SDUI (FR-005).
- [ ] T011 [agent] Config fail-safe: `CRESCO_WSAPI_URL`/`CRESCO_SERVICE_KEY` unset ⇒ tools report unavailable, no boot impact (FR-003, SC-002).
- [ ] T012 [audit] Route every tool call through the `agent_tool_call` audit path, recording fabric identifiers (`region_agent[_plugin]`) (FR-008, SC-007).
- [ ] T013 [tests] Integration test: flag-on + configured client → read tools round-trip (mocked + live-fabric fixture) (SC-003).

## Phase 3 — Write + executor tools (US2, P2)

- [ ] T014 [tools] File put/get (filerepo) at **write scope**; audited (FR-005).
- [ ] T015 [tools] `cresco_run_process` (executor) at **system scope + hard `tool_security` flag, default-deny**; never enabled by the safe-agent baseline (FR-006, SC-004).
- [ ] T016 [tests] Executor default-deny for a normal user; allowed only via explicit per-user override; denial audited (SC-004).

## Phase 4 — Verification

- [ ] T017 Flag-off no-op: full existing suite green, no Cresco path reachable (SC-001).
- [ ] T018 Live E2E vs local single-node fabric (`agent-1.3-SNAPSHOT.jar`, `-Dis_global=true -Denable_wsapi=true`): read round-trip; executor default-deny; audit rows carry fabric ids; self-signed cert rejected without a configured CA/fingerprint (SC-003/004/006/007).
- [ ] T019 Changed-code coverage ≥ 90%; ruff clean; secret-scan green (no `cresco_service_key` material committed) (SC-008, Constitution XI).

## Dependencies

- Builds on: feature 029 (plug-and-play agent), feature 040 (in-process agents), `shared/external_http`, `tool_permissions`/`tool_security`, `orchestrator/delegation.py`, `audit`.
- Sanctioned by: the Cresco external-infrastructure clause in the constitution (Principle VII + Technology Stack).
- Deferred: Cresco-side Java bridge plugin; dataplane streaming beyond bounded reads.
