# Implementation Plan: Remote Compute Agents — Cluster Jobs and SSH Host Operations

**Branch**: `063-remote-compute-agents` | **Date**: 2026-07-24 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/063-remote-compute-agents/spec.md`; grounded
research in [research.md](research.md); design in [data-model.md](data-model.md) and
[contracts/](contracts/).

**Sequencing**: Per spec Dependencies, implementation follows the **MCP 2026-07-28 upgrade**
(the standing next-pickup directive). This plan is authored ahead of it deliberately — planning
now is consistent with the spec's own sequencing; only the *implementation* waits.

## Summary

Ship **two in-process first-party agents, split by risk tier**: a **read-only** agent
(`remote-observe-1`, safe-seeded — works out of the box) and a **mutating** agent
(`remote-control-1`, never safe-seeded — explicit per-user grant required), both reaching
user-registered Ubuntu/Windows/macOS machines and Slurm clusters over a **single SSH
transport**. Users self-register their **own** machines and credentials (nothing seeded or
hardcoded, FR-009); the read half answers "what's in my queue / how's my host" with structured,
typed fields; the mutating half submits/administers, and **every destructive operation stops and
waits for an explicit, single-use, argument-bound, restart-durable approval** — the feature's
one net-new mechanism.

**Technical approach** (all seams grounded in current code and a live DGX probe):
1. **Transport** — a single injectable `RemoteTransport` boundary (`paramiko` production impl,
   `FakeTransport` test double) executing **discrete argv** through a **login shell**
   (`bash -lc 'exec "$@"' _ …`, proven live to resolve Slurm *and* neutralise shell
   metacharacters), fronted by the product's **first non-HTTP egress gate** (refuses
   loopback/link-local/metadata/reserved, **permits RFC1918** because on-prem clusters live
   there — the DGX resolves to `10.33.77.11`) and **host-key pinning** (`RejectPolicy` against a
   per-machine recorded key; explicit re-trust only).
2. **Output** — typed fields only, from Slurm native `--json` and fixed typed host commands;
   bounded + sanitised; both agents registered as taint sources.
3. **Permissions** — decouple *visibility* from *safe-seeding* (split the welded
   `_FIRST_PARTY_PUBLIC_AGENT_IDS` into a visibility set + a safe-seed subset) so the mutating
   agent is discoverable but not pre-authorised (FR-004).
4. **Confirmation** — a **durable** `remote_operation_proposal` table + a `remote_op_decision`
   button/`ui_event` (mirroring the proven `scheduling_chat` flow, not the dormant
   `authorize_action` path), **enforced at the shared dispatch gate** (`_authorize_and_prepare`)
   so parallel batches and chained hops cannot bypass it, and **refused outright on machine
   turns**.
5. **Durability** — a `tracked_job` table with boot reconciliation and read-only unattended
   polling under the existing machine-turn authority; consequential verbs declared
   non-retryable with an idempotency marker.

One new approved runtime dependency (`paramiko`, D1); four new tables; one new chrome surface
(render + native); zero new client code (server-driven).

## Technical Context

**Language/Version**: Python 3.11 (backend, production image; local `.venv` 3.13). Both agents
are Python `BaseA2AAgent` subclasses run in-process. The inventory/credential UI is a
server-driven chrome surface (no new client-side code).

**Primary Dependencies**: **NEW (approved, D1 / FR-054)** — `paramiko` (SSHv2), pulling
`bcrypt`, `pynacl`, `cffi`, and `cryptography`; `cryptography` (already transitive via
`python-jose[cryptography]`, `requirements.txt:65`; used directly by `credential_manager.py:17`)
gains an **explicit pin**. **Existing only** — FastAPI, `websockets`, psycopg2, the in-process
agent framework (`orchestrator/local_agents.py`, `shared/base_agent.py`,
`shared/local_transport.py::LoopbackSocket`), `tool_permissions` + `agent_trust` (safe baseline),
`taint`, `chain_authority` (`MachineTurnAuthority`), the `scheduling_chat` propose→decision
pattern, `shared/external_http` resolver helpers, `credential_manager` (Fernet), the
`webrender/chrome` surface framework + `_sdui`, and the hash-chained `audit`.

**Storage**: PostgreSQL via `shared/database.py::_init_db()` idempotent guarded startup deltas.
Four new tables — `remote_machine`, `machine_credential`, `remote_operation_proposal`,
`tracked_job`. **`SCHEMA_REVISION` `060.004` → `063.001`** (`database.py:32`), and the CI guard
constant in `backend/tests/test_schema_revision_guard.py:24` bumps to match. Rollback + an
idempotent retirement cleanup documented in [data-model.md](data-model.md).

**Testing**: pytest (container, both invocations vs `postgres:17-alpine`) — the FR-051 verb
contract test, the egress/host-key gate test, the US3 adversarial confirmation suite, the US6
output-containment suite — all against `FakeTransport` (no machine, no SSH server, no network,
SC-015). The real-world proof is the separate **live-verification checklist**
([quickstart.md](quickstart.md#live-verification-checklist-fr-052), SC-016).

**Target Platform**: Linux server (orchestrator, Docker). Clients (web, Windows, Android, Apple)
consume the inventory surface and the consent `Card`/`remote_op_decision` frame **server-driven**
with no per-client code; the **watch** degrades to "continue on phone/desktop" from the shared
definition (FR-033, Constitution XII carve-out).

**Project Type**: Server-driven multi-client system — two new in-process bundled agents, a new
transport boundary, and a new durable-confirmation mechanism.

**Performance Goals**: SC-001 correct save-time verdict ≥ 9/10; SC-002 queue/status rendered
< 30 s ≥ 9/10; every verb bounded by a declared timeout (FR-021, default no worse than the 30 s
dispatch budget).

**Constraints**: fail-closed `FF_REMOTE_COMPUTE` (default off, read once at import → container
recreate); no model-supplied value ever assembled into a shell string (FR-022); structured typed
output only (FR-038); per-user isolation (FR-010); the new non-HTTP egress path explicitly gated
(FR-019); idempotent guarded migrations (Constitution IX); cross-client parity via the manifest +
drift guards (Constitution XII); consequential verbs non-retryable (FR-036); implementation
sequenced after the MCP 2026-07-28 upgrade.

**Scale/Scope**: per-user machine inventories (modest). ~2 new agent packages (16 verbs total),
4 tables, 1 chrome surface, 1 transport module, 1 `net_guard` helper, 1 confirmation
module + gate hook, 1 job module, plus edits to `local_agents`, `taint`, `database`,
`feature_flags`, `ui_protocol.json`, and the shared chrome menu.

## Constitution Check

*GATE: evaluated before Phase 0 and re-checked after Phase 1 design (below). Constitution v2.8.0.*

| Principle | Status | Note |
|---|---|---|
| I. Primary Language (Python) | PASS | Backend + both agents Python; no language change. |
| II. UI Delivery (astralprims → orchestrator renders → ROTE) | PASS | New surface is `render()`+`components()`; consent is an astralprims `Card`; no client-side UI. New UI-facing `remote_op_decision` action added to `ui_protocol.json`. |
| III. Testing Standards (≥90% changed-line) | PASS | Contract + adversarial + containment + gate suites against `FakeTransport`; changed-code coverage gate met (SC-015). |
| IV. Code Quality (lint) | PASS | Python ruff from repo root. **No new client TS/JS/Kotlin/Swift** — clients dispatch the new button action generically (`Basic.kt:148-150`), so no maintained-client-language lint surface is added. |
| V. Dependency Management | PASS (approved exception) | `paramiko` (+ `bcrypt`/`pynacl`/`cffi`; explicit `cryptography` pin) is the one new runtime dependency, **approved in D1**, recorded in the PR with its transitive set (FR-054). See Complexity Tracking. |
| VI. Documentation | PASS | Contracts + data-model + quickstart + docstrings; `docs/remote-compute-agents.md` operator/security doc. |
| VII. Security | PASS (central) | Reuses the full gate stack + audit; adds the connection egress gate, host-key pinning, per-user credential isolation, durable arg-bound confirmation, non-retry, and taint registration. **In-process clause honoured**: credentials are decrypted **inside the agent's tool boundary** for the connection, and no artifact claims process isolation — FR-014 states plainly that decrypted material transiently exists in orchestrator memory. Cresco explicitly rejected (Out of Scope), so its system-scoped/hard-flag rule is N/A. |
| VIII. User Experience | PASS | astralprims primitives; honest enumerated verdicts (FR-034); plain-language proposals. |
| IX. Database Migrations | PASS | Idempotent guarded `_init_db` deltas; `SCHEMA_REVISION` bump + CI guard update; documented rollback + idempotent retirement cleanup; migration exercised against a representative dataset (seeded rows), not an empty DB. |
| X. Production Readiness | PASS | Fail-closed flag default off; no stubs; structured failure logs + audit for observability. The runtime-infra change (new dependency, new egress path) is validated by the **live-verification checklist from the deployed orchestrator** against the real DGX (staging evidence), distinguishing proven-live from code-shaped (FR-052). |
| XI. Continuous Integration | PASS | New tests fit lint/test/coverage/build/smoke/secret-scan; `paramiko` documented; schema-revision guard updated. |
| XII. Cross-Client Consistency | PASS | One surface, dual render/native; the consent `Card` + `remote_op_decision` frame land in the manifest and every client renders them with no per-client code; the watch degradation is a documented, server-enforced carve-out; the "Remote machines" menu item is added once to the shared chrome. |
| XIII. Documentation & Research Integrity | PASS | research.md is grounded in cited code + a dated live probe; proven-live vs code-shaped is explicit; no fabricated APIs. |

**Result**: no violations. The single notable deviation from the project's usual zero-new-deps
posture — the `paramiko` runtime dependency — is **approved (D1)** and recorded in Complexity
Tracking below.

## Project Structure

### Documentation (this feature)

```text
specs/063-remote-compute-agents/
├── plan.md                     # This file
├── spec.md                     # Feature spec (done)
├── research.md                 # Phase 0 (done)
├── data-model.md               # Phase 1 (done) — 4 tables + retirement/rollback
├── quickstart.md               # Phase 1 (done) — enablement + live-verification checklist
├── contracts/                  # Phase 1 (done)
│   ├── transport.md                # RemoteTransport boundary + egress gate + host-key + login-shell exec
│   ├── verbs.md                    # the fixed closed verb set (FR-051 contract-test target)
│   ├── confirmation.md             # durable proposal + remote_op_decision + dispatch-gate enforcement
│   ├── result-vocabulary.md        # FR-034 enumerated verdicts
│   ├── inventory-surface.md        # remote_machines surface (render + components)
│   └── job-tracking.md             # durable jobs + boot reconcile + unattended read-only poll
├── checklists/
│   └── requirements.md         # (speckit-checklist, optional)
└── tasks.md                    # Phase 2 (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
backend/
├── agents/
│   ├── remote_observe/                    # NEW — read-only agent (remote-observe-1)
│   │   ├── remote_observe_agent.py            # class + card_metadata
│   │   ├── mcp_server.py
│   │   └── mcp_tools.py                       # 8 read verbs, TOOL_REGISTRY, scopes tools:read
│   ├── remote_control/                    # NEW — mutating agent (remote-control-1)
│   │   ├── remote_control_agent.py
│   │   ├── mcp_server.py
│   │   └── mcp_tools.py                       # 8 mutating verbs + declared destructive-classification map
│   └── tests/
│       ├── test_remote_verbs_contract.py      # NEW — FR-051
│       └── test_remote_transport_gate.py      # NEW — egress denylist + host-key policy (FakeTransport)
├── orchestrator/
│   ├── remote_transport.py                # NEW — RemoteTransport protocol, ParamikoTransport, FakeTransport, factory
│   ├── remote_confirmation.py             # NEW — durable proposal store + remote_op_decision handler + gate predicate
│   ├── remote_jobs.py                     # NEW — tracked-job store + boot reconciliation + read-only poller
│   ├── local_agents.py                    # EDIT — add remote_observe/remote_control to BUILT_IN_AGENT_DIRS
│   ├── orchestrator.py                    # EDIT — destructive gate in _authorize_and_prepare; remote_op_decision route; machine-turn refusal; boot reconcile call; narrower safe-seed set
│   ├── taint.py                           # EDIT — add both ids to _UNTRUSTED_AGENTS
│   ├── credential_manager.py              # EDIT — machine_credential set/get/delete + wired revocation (fix the unwired remove path)
│   └── tests/
│       ├── test_remote_confirmation.py        # NEW — US3 adversarial suite
│       └── test_remote_output_containment.py  # NEW — US6
├── shared/
│   ├── database.py                        # EDIT — 4 tables, SCHEMA_REVISION 063.001, id-set split (_SAFE_SEED_AGENT_IDS), retirement cleanup
│   ├── net_guard.py                       # NEW — scheme-neutral SSH egress predicate (allows RFC1918)
│   ├── external_http.py                   # EDIT — expose/relocate _resolve_host_addresses for reuse
│   ├── feature_flags.py                   # EDIT — FF_REMOTE_COMPUTE (default off)
│   └── ui_protocol.json                   # EDIT — accept remote_op_decision
├── webrender/chrome/
│   ├── surfaces/remote_machines.py        # NEW — render() + components() + HANDLERS
│   └── <shared chrome menu def>           # EDIT — add the "Remote machines" item once
└── tests/
    └── test_schema_revision_guard.py      # EDIT — EXPECTED_SCHEMA_REVISION → 063.001

docs/
└── remote-compute-agents.md               # NEW — operator enablement + security posture (FR-054 dependency record)
```

**Structure Decision**: extend the existing server-driven backend. The two agents follow the
bundled-agent shape exactly (`BaseA2AAgent` + `mcp_tools.py::TOOL_REGISTRY`, registered via
`local_agents.register_built_ins`). The genuinely new modules are small and single-purpose — the
transport boundary, the confirmation mechanism, the job tracker, and the `net_guard` helper;
everything else is an edit that reuses an existing seam. No new top-level project, no new client
codebase.

## Phasing (maps to spec user stories)

Ordering honours the spec's priorities: **US3 confirmation (P1) is built and proven before any
mutating verb is enabled** (spec §4), and US2 depends on US1.

- **Phase A — US1 MVP (P1)**: `net_guard` egress gate; `RemoteTransport` + `ParamikoTransport` +
  `FakeTransport` (login-shell argv exec, host-key pinning); `remote_machine` +
  `machine_credential` tables + Fernet storage + **wired revocation**; the `remote_machines`
  surface (`render()`+`components()`, multi-line PEM); `probe_machine` + `list_machines`;
  `FF_REMOTE_COMPUTE`. Delivers "register + prove reachability" — the hardest, least-precedented
  slice. Proves per-host credentials, multi-line PEM, native parity, and the transport at once.
- **Phase B — US2 (P1, the demo)**: the six remaining read verbs via Slurm `--json` + typed host
  commands; structured workspace rendering; output containment (bounds + sanitise + truncation
  notice). (The visibility/safe-seed id-set split lands in **Foundational** so both agents register
  correctly at boot; taint-source registration is **US6**.)
- **Phase C — US3 (P1, co-critical, net-new)**: `remote_operation_proposal` table +
  `remote_confirmation` module + the **dispatch-gate check in `_authorize_and_prepare`** +
  `remote_op_decision` frame/route + machine-turn refusal + watch degradation + the adversarial
  suite (SC-004). **Lands before Phases D–E enable any mutating verb.**
- **Phase D — US4 (P2)**: `submit_job` + `tracked_job` durability + boot reconciliation +
  read-only unattended polling + finish-notification opt-in + orphaning + idempotency marker.
- **Phase E — US5 (P2)**: `remote-control-1` mutating verbs with the declared destructive map;
  explicit-grant requirement; non-retry + argument-shape guards; `upload_file` `if_exists` stat;
  `control_service`/`manage_package` `by_action` classification.
- **Phase F — US6 (P2)**: containment hardening + the injection corpus test (SC-007) — largely
  realised in B/verbs; F is the dedicated adversarial pass and the taint-source assertion.
- **Phase G — US7 (P3)**: idempotent retirement cleanup; flag-off parity proof (SC-013);
  revocation on account removal/logout destroys machine credentials.

## Complexity Tracking

One justified addition — recorded because it departs from the project's usual zero-new-dependency
posture, though it is **approved** and therefore not a Constitution violation.

| Item | Why needed | Simpler alternative rejected because |
|---|---|---|
| New runtime dependency `paramiko` (+ `bcrypt`/`pynacl`/`cffi`) | The product has no SSH capability (no library, no `ssh` binary in the image); a programmatic SSH client is required for host-key policy, per-connection timeouts, key/password auth, and the argv exec pattern | Shelling out to a bundled `ssh` binary reintroduces a shell-string surface (violates FR-022) and needs an apt package; writing an SSH client is out of the question. Approved in D1; recorded in the PR (FR-054). |
| First non-HTTP egress path (`net_guard`) | SSH is the product's first non-HTTP outbound; the HTTP egress guard scheme-locks to http/https and cannot cover it | Reusing `validate_egress_url` verbatim is impossible (it rejects the SSH scheme and blanket-blocks RFC1918, which would block the real DGX target at `10.33.77.11`). The new predicate reuses the resolver and keeps every denylist clause except `is_private`. |

## Post-Design Constitution Re-Check

After Phase 1 design (data-model + contracts): **still PASS.** The design adds exactly one
approved dependency; keeps all UI server-driven with the new frame in the manifest; makes the new
egress path explicitly gated and — grounded in the live DNS result — correctly permits RFC1918 so
the actual cluster is reachable while still refusing loopback/link-local/metadata; makes
credentials per-user and Fernet-encrypted with an **honest** in-process disclosure (FR-014, no
process-isolation claim); and makes the confirmation mechanism durable and **gate-enforced across
the single, parallel, and hop paths** rather than trusting a tool's cooperation. The one carried
risk that needed an early decision rather than deferral — **orchestrator→cluster reachability** —
is resolved as a deployment responsibility with a live checklist item and a `project-dgx-tunneling`
consult (research R16), not left implicit. Complexity Tracking is limited to the two justified
items above.
