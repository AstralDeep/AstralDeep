# Tasks: Remote Compute Agents — Cluster Jobs and SSH Host Operations

**Input**: Design documents from `specs/063-remote-compute-agents/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md (all present)

**Tests**: INCLUDED — the spec explicitly mandates them (FR-050 injectable-transport test seam,
FR-051 verb contract test, FR-052 live-verification checklist, SC-015 full E2E without a network).

**Sequencing note**: Per spec Dependencies, *implementation* follows the **MCP 2026-07-28
upgrade**. These tasks are ready to execute after that. All backend commands run in the
`astraldeep` container (`docker exec astraldeep bash -c "cd /app/backend && python -m pytest -q"`).

## Format: `[ID] [P?] [Story?] Description with file path`

- **[P]** = parallelizable (different files, no dependency on an incomplete task)
- **[US#]** = user-story phase task; Setup/Foundational/Polish carry no story label
- Repo-relative paths are shown; the repo root is `C:\Users\sear234\Desktop\Containers\MCP\AstralDeep`.

---

## Phase 1: Setup (Shared Infrastructure)

- [ ] T001 Add `paramiko` and an explicit `cryptography` pin to `backend/requirements.txt`; in the PR description record the D1 Constitution V approval and the transitive set (`bcrypt`, `pynacl`, `cffi`) per FR-054. Confirm the manylinux wheels install on `python:3.11-slim` with the unchanged repo-root `Dockerfile:27-34` apt list.
- [X] T002 [P] Add `FF_REMOTE_COMPUTE` (default **off**, read once at import) to `backend/shared/feature_flags.py`, mirroring the existing flag pattern (`FF_INPROCESS_AGENTS` at `:85`).
- [X] T003 [P] Scaffold agent packages `backend/agents/remote_observe/` and `backend/agents/remote_control/` (`__init__.py`, empty `mcp_tools.py`/`mcp_server.py`) following the bundled-agent layout (`agents/weather/`).

---

## Phase 2: Foundational (Blocking Prerequisites)

**⚠️ CRITICAL**: no user-story work begins until this phase is complete. These are the transport,
schema, agent-registration, and permission-decoupling pieces every story depends on.

- [X] T004 Add the `remote_machine` and `machine_credential` tables (idempotent guarded DDL, CHECK constraints, indexes, FK `ON DELETE CASCADE`) inside `shared/database.py::_apply_full_schema`, per [data-model.md](data-model.md).
- [X] T005 Bump `SCHEMA_REVISION` `060.004` → `063.001` in `backend/shared/database.py:32` and update `EXPECTED_SCHEMA_REVISION` in `backend/tests/test_schema_revision_guard.py:24`.
- [X] T006 [P] Create `backend/shared/net_guard.py` — a scheme-neutral SSH egress predicate refusing loopback/link-local(incl. `169.254.169.254`)/multicast/unspecified/reserved and **permitting RFC1918** (per research R5); resolve-all-records anti-rebinding.
- [X] T007 [P] In `backend/shared/external_http.py`, expose `_resolve_host_addresses` (and the private-host env helper) for reuse by `net_guard` without changing the HTTP gate's behaviour. *(superseded — unified `remote-compute-1` agent; 2026-07-27 reconciliation)*
- [X] T008 Create `backend/orchestrator/remote_transport.py` — the `RemoteTransport` protocol, `MachineTarget`, `RemoteResult`, and the login-shell `bash -lc 'exec "$@"' _ …` argv builder + per-call timeout mapping (per [contracts/transport.md](contracts/transport.md)).
- [X] T009 Implement `ParamikoTransport` in `backend/orchestrator/remote_transport.py`: connect (calls `net_guard` + `RejectPolicy` host-key verify against the recorded key), `run` (exec argv), `put_file`/`stat` (SFTP), `probe`; map every outcome to a [result-vocabulary.md](contracts/result-vocabulary.md) verdict, converting its own timeout to a structured non-`None` result.
- [X] T010 [P] Implement `FakeTransport` in `backend/agents/tests/fake_transport.py` — in-memory machine: scripted reachability/auth, virtual FS for `stat`/`put_file`, canned Slurm `--json`, injectable host-key-mismatch/timeout/quota cases.
- [X] T011 Add the transport factory/injection seam (mirroring `llm_config/client_factory.py`) wiring `ParamikoTransport` in production and `FakeTransport` in tests; inject it into both agents via `_runtime`/constructor.
- [ ] T012 [P] Create a shared remote-output helper in `backend/orchestrator/remote_transport.py` (or a sibling) that bounds field sizes, strips control chars/ANSI escapes, and marks truncation (`{shown,total,truncated}`) — used by every verb (FR-040/FR-041).
- [X] T013 [P] Extend `backend/orchestrator/credential_manager.py` with `machine_credential` set/get/delete (Fernet under `CREDENTIAL_ENCRYPTION_KEY`) and `credential_undecryptable` detection distinct from "not configured".
- [X] T014 [P] Add an argument-shape guard module (absolute-path/int/enum/service-name validators) in `backend/orchestrator/remote_transport.py` or a sibling, per [contracts/verbs.md](contracts/verbs.md) "Argument-shape guards".
- [X] T015 Create the read-only agent `backend/agents/remote_observe/remote_observe_agent.py` (`remote-observe-1`, `BaseA2AAgent`) + `mcp_server.py` wrapping `TOOL_REGISTRY`. *(superseded — unified `remote-compute-1` agent; 2026-07-27 reconciliation)*
- [X] T016 Create the mutating agent `backend/agents/remote_control/remote_control_agent.py` (`remote-control-1`) + `mcp_server.py` + an empty declared destructive-classification map skeleton. *(superseded — unified `remote-compute-1` agent; 2026-07-27 reconciliation)*
- [X] T017 Register both dirs in `BUILT_IN_AGENT_DIRS` (`backend/orchestrator/local_agents.py:27-37`). *(superseded — unified `remote-compute-1` agent; 2026-07-27 reconciliation)*
- [X] T018 Decouple visibility from safe-seeding in `backend/shared/database.py`: add both agent ids to `_FIRST_PARTY_PUBLIC_AGENT_IDS` (visibility) and introduce `_SAFE_SEED_AGENT_IDS` (existing 9 + `remote-observe-1` **only**); update the boot `seed_safe(...)` call (`orchestrator.py:17230-17231`) to consume `_SAFE_SEED_AGENT_IDS` (FR-004). *(superseded — unified `remote-compute-1` agent; 2026-07-27 reconciliation)*

**Checkpoint**: transport, schema, both agents (empty verb sets), permission split, and the flag exist. Stories can begin.

---

## Phase 3: User Story 1 — Register my own machine and prove I can reach it (P1) 🎯 MVP

**Goal**: A user registers a machine (address/port/user/OS/role + multi-line key or password) and, on save, sees a real, specific reachability verdict. Inventory starts empty; one user's machines are invisible to others.

**Independent Test**: empty inventory → register reachable (valid key) = authenticated; wrong cred = auth-rejected naming the host; unroutable = unreachable within timeout; PEM round-trips on web + one native client; a second user sees none of the first's machines.

### Tests (write first, ensure fail)
- [ ] T019 [P] [US1] Integration test `backend/agents/tests/test_remote_us1_reachability.py` — register reachable/wrong-cred/unroutable against `FakeTransport` → correct enumerated verdicts (SC-001).
- [X] T020 [P] [US1] Test in `backend/webrender/chrome/tests/test_remote_machines_surface.py` — multi-line PEM survives a round trip through the surface (web `render()` textarea + native `components()` `kind="textarea"`).
- [ ] T021 [P] [US1] Test `backend/orchestrator/tests/test_remote_isolation.py` — user B cannot list/address/act on user A's machine (SC-012).
- [ ] T022 [P] [US1] Test in `backend/agents/tests/test_remote_transport_gate.py` — egress denylist (loopback/link-local/metadata refused; RFC1918 allowed) and host-key-mismatch refusal + explicit re-trust.

### Implementation
- [X] T023 [US1] Implement `probe_machine` + `list_machines` in `backend/agents/remote_observe/mcp_tools.py` (probe opens a real connection and records the host key on first contact; both `tools:read`).
- [X] T024 [US1] Implement `backend/webrender/chrome/surfaces/remote_machines.py` `render()` (web HTML form: label/address/port/username/os_family/role/cred_type + **`<textarea>` private key** + passphrase / password), with the empty-state invitation and no seeded host.
- [X] T025 [US1] Implement `components()` in the same surface (native, via `_sdui.form`/`_sdui.field` incl. `kind="textarea"`), bound to the same `chrome_*` handler keys (template: `webrender/chrome/surfaces/llm.py`).
- [ ] T026 [US1] Implement `HANDLERS`: `chrome_machine_add` (validate + store + Fernet-encrypt + immediate `probe_machine` verdict), `chrome_machine_probe`, `chrome_credential_set`/`chrome_credential_delete`, `chrome_machine_retrust`, `chrome_machine_delete` (cascade + orphan any tracked job); each owner-scoped.
- [X] T027 [US1] Register the surface in `SURFACE_MODULES` (`webrender/chrome/surfaces/__init__.py:28`) and add the flag-gated "Remote machines" item to the shared server-owned chrome menu (Constitution XII).
- [ ] T028 [US1] Wire credential **revocation** on machine delete, credential delete, account removal, and logout (fix the unwired `remove_agent_credentials` path; add `DELETE FROM machine_credential` to the account/logout flows) — FR-015.
- [ ] T029 [US1] Audit machine registration/removal, credential set/delete, and every connection attempt + verdict; secrets never appear in any row (FR-047/FR-049).

**Checkpoint**: US1 fully functional — a user can register a machine and get an honest reachability verdict on web + native.

---

## Phase 4: User Story 2 — See what my cluster and machines are doing (P1) — the demo

**Goal**: From chat, a signed-in user with no grants gets their queue, a job's status, history, and host facts as structured typed components; nothing can change remote state.

**Independent Test**: with one cluster host + one plain host, ask queue/status/host facts → each renders as a structured workspace component; a no-grant user runs all read verbs; every value is a typed field.

### Tests (write first, ensure fail)
- [ ] T030 [P] [US2] Test `backend/orchestrator/tests/test_remote_safe_baseline.py` — a no-grant user runs every read verb and **zero** mutating verbs (SC-003).
- [ ] T031 [P] [US2] Test `backend/agents/tests/test_remote_read_verbs.py` — queue/status/history parse canned Slurm `--json` into typed fields; host_facts/list_directory/list_processes typed, bounded, sanitised (SC-002/SC-008).

### Implementation
- [ ] T032 [US2] Implement `list_queue`, `job_status`, `job_history` in `backend/agents/remote_observe/mcp_tools.py` via Slurm `--json` (`squeue --me --json`, `squeue --json`, `sacct --json`) through the login-shell transport; delimited `-o` fallback mapped to the same typed shape.
- [ ] T033 [US2] Implement `host_facts` (typed `/proc/loadavg`, `/proc/uptime`, `nproc`, `free -b`, `df -B1 --output=…`; GPU from `sinfo` GRES for `cluster` role — never `nvidia-smi` on the queried host).
- [X] T034 [P] [US2] Implement `list_directory` (`find -maxdepth 1 -printf`, bounded entries + "showing N of M") and `list_processes` (`ps -eo …`, `own_only` default true), each via the sanitiser (T012).
- [ ] T035 [US2] Render read-verb results as structured astralprims components (tables/metric cards) that persist in the workspace; verify safe-seed (`remote-observe-1` in `_SAFE_SEED_AGENT_IDS`) makes them run under the baseline (SC-002/SC-003).

**Checkpoint**: US1 + US2 work — the read-only demo is complete with no mutation risk.

---

## Phase 5: User Story 3 — Destructive operations stop and wait for me (P1, net-new) 🔒

**Goal**: Every destructive verb produces a proposal and no effect on first call; nothing proceeds without an explicit, single-use, argument-bound, restart-durable approval by the issuing user.

**⚠️ BLOCKS Phase 7 (US5) and the destructive verbs of US4** — must land and pass before any mutating verb is enabled.

**Independent Test**: invoke each destructive verb → proposal, no effect; approve → runs exactly once against exactly the shown args; a proposal cannot be reused/expired/other-user/redirected; the flow completes on web/Windows/Android/Apple and the watch refuses+redirects.

### Tests (write first, ensure fail)
- [ ] T036 [P] [US3] Adversarial suite `backend/orchestrator/tests/test_remote_confirmation.py` — differently-named verb, repeated call, parallel tool batch, chained hop, expired/reused/other-user/redirected-args approval, and a machine-initiated turn → **zero** destructive executions without a fresh matching approval (SC-004).
- [ ] T037 [P] [US3] Test in the same file — a pending proposal survives an orchestrator restart and a restart never auto-approves (SC-006); each destructive verb yields a proposal + no effect on first call (SC-005).

### Implementation
- [X] T038 [US3] Add the `remote_operation_proposal` table to `shared/database.py::_apply_full_schema` (per [data-model.md](data-model.md)).
- [X] T039 [US3] Create `backend/orchestrator/remote_confirmation.py` — durable proposal store (create/approve/decline/expire/**atomic consume**), `args_fingerprint = sha256(canonical args)`, absolute-time TTL (per [contracts/confirmation.md](contracts/confirmation.md)).
- [X] T040 [US3] Implement the destructive-classification **gate predicate** inside the shared `_authorize_and_prepare` path (covers single, parallel, and chained-hop dispatch): classify (`always`/`never`/`if_exists` via read-only `stat`/`by_action`), and on an unapproved destructive call create the proposal + consent `Card` + `confirmation_required` verdict without running the verb.
- [X] T041 [US3] Implement the `remote_op_decision` `ui_event` handler + WS route in `backend/orchestrator/orchestrator.py` (ownership check vs session `sub`, expiry, single-use consume, then re-enter `execute_single_tool` with the **stored** args through the full gate stack).
- [X] T042 [US3] Add `remote_op_decision` to `backend/shared/ui_protocol.json` `accept_actions` and keep the manifest + every client's drift guard green (Constitution XII).
- [ ] T043 [US3] Implement the no-live-human refusal: any `remote-control-1` verb on a `MACHINE_TURN_CLASSES` turn is refused `unattended_refused` regardless of scope (FR-033), in the same gate.
- [X] T044 [US3] Implement the watch-client degradation ("continue on phone/desktop", no approve control) from the shared definition (US3-7).
- [X] T045 [US3] Audit every proposal lifecycle event and every executed consequential op with its target, correlated by `proposal_id` (FR-047/FR-048).

**Checkpoint**: the confirmation gate is proven. Mutating verbs may now be enabled.

---

## Phase 6: User Story 4 — Submit work and track it after I close the tab (P2)

**Goal**: Submit a batch job → a durable record + scheduler id; hours later, from any device and across a restart, get a true status; opt-in finish notice; unattended submit/cancel refused, polling read-only.

**Independent Test**: submit → durable record with scheduler id/host/chat; restart → reconciled + still reportable; slow submit → `unconfirmed`, no duplicate; unattended submit/cancel refused, poll allowed.

### Tests (write first, ensure fail)
- [ ] T046 [P] [US4] Test `backend/orchestrator/tests/test_remote_jobs.py` — submit writes a durable `tracked_job`; boot reconciliation resolves a job that finished during the outage (SC-009).
- [ ] T047 [P] [US4] Test — a slow/lost submit returns `unconfirmed` (non-retryable) and creates **no** second job across ≥20 induced slow responses (SC-010); unattended submit/cancel refused, status poll permitted.

### Implementation
- [X] T048 [US4] Add the `tracked_job` table to `shared/database.py::_apply_full_schema` (per [data-model.md](data-model.md)).
- [ ] T049 [US4] Implement `submit_job` in `backend/agents/remote_control/mcp_tools.py` (`tools:write`, non-destructive, **retryable=False**, idempotency marker `--comment=<nonce>`); on confirmed id → INSERT `tracked_job`.
- [ ] T050 [US4] Create `backend/orchestrator/remote_jobs.py` — the tracked-job store + a boot reconciliation pass (read-only, under `derive_machine_authority`) wired into orchestrator start.
- [ ] T051 [US4] Implement the read-only unattended poller (machine-turn authority narrowed to status verbs), `notify_on_finish` opt-in via the existing client notification channel, and orphaning when the machine/credential is gone (FR-044/FR-045/FR-046).

**Checkpoint**: jobs are durable and truthfully reportable across restart; nothing consequential runs unattended.

---

## Phase 7: User Story 5 — Run a fixed set of tasks on my own machines (P2)

**Goal**: With the mutating agent granted, exercise each non-destructive verb; every destructive verb routes through US3; with no grant, all US5 verbs denied while US2 verbs still work.

**Depends on**: US3 (destructive gate) must be complete.

**Independent Test**: granted → each non-destructive verb effects change; destructive → proposal; upload to new path = no proposal, to existing path = proposal; no grant → all denied, reads unaffected; Windows without OpenSSH → unreachable + prerequisite.

### Tests (write first, ensure fail)
- [ ] T052 [P] [US5] Test `backend/agents/tests/test_remote_mutating_verbs.py` — granted vs ungranted (SC-003); `upload_file` `if_exists` overwrite → proposal, new path → no proposal (US5-2).
- [ ] T053 [P] [US5] Test — arg-shape guards reject a shell fragment/pipeline/redirection/substitution in any argument (US5-3); a Windows target without OpenSSH → `unreachable` + documented prerequisite (US5-4).

### Implementation
- [X] T054 [US5] Implement `make_directory` (`mkdir -p`, non-destructive) and `upload_file` (SFTP; declared `destructive_if_exists`, decided by a read-only `stat`) in `backend/agents/remote_control/mcp_tools.py`.
- [X] T055 [US5] Implement `remove_path`, `cancel_job`, and `signal_process` (all `always` destructive → gate).
- [X] T056 [US5] Implement `control_service` (`by_action:{stop,disable,restart}`) and `manage_package` (`by_action:{remove}`) with `tools:system` scope.
- [ ] T057 [US5] Populate the declared destructive-classification map, set `retryable=False` + timeouts + scopes, and apply the T014 arg-shape guards for every mutating verb; confirm the mutating agent is **not** in `_SAFE_SEED_AGENT_IDS` so verbs require an explicit `agent_scopes` grant.
- [ ] T074 [US5] Show both agents' complete verb lists — each with its one-line description and a destructive marker — on the agents chrome surface **before** any grant, sourced from `TOOL_REGISTRY` metadata (FR-025). *(analyze remediation COV1)*

**Checkpoint**: the full mutating surface works, with every destructive verb gated by US3.

---

## Phase 8: User Story 6 — What a remote machine says cannot make the agent act (P2)

**Goal**: Text from a remote machine is inert data; both agents are untrusted taint sources; no unbounded remote text reaches the model.

**Independent Test**: instruction-shaped text in hostname banner, filename, job name, queue reason, and process command → zero tool calls attributable, zero destructive proposals; both agents classified untrusted; every remote value bounded.

### Tests (write first, ensure fail)
- [X] T058 [P] [US6] Test `backend/orchestrator/tests/test_remote_output_containment.py` — an injection corpus placed in every remote field a verb can return produces zero tool calls and zero destructive proposals (SC-007); every returned value is a bounded typed field with truncation visibly marked (SC-008). *(superseded — unified `remote-compute-1` agent; 2026-07-27 reconciliation)*
- [ ] T059 [P] [US6] Test — both agent ids are classified untrusted by `taint.classify_source` when taint is enabled.
- [ ] T075 [P] [US6] Test `backend/orchestrator/tests/test_remote_no_secret_leak.py` — no private-key, passphrase, or password byte appears in any audit row, log line, verdict/error message, notification, or rendered field across register/probe/verb/failure paths (FR-049). *(analyze remediation COV2)*

### Implementation
- [X] T060 [US6] Add `remote-observe-1` and `remote-control-1` to `taint._UNTRUSTED_AGENTS` (`backend/orchestrator/taint.py:37`) — FR-039.
- [X] T061 [US6] Harden the T012 sanitiser and assert declared per-field bounds across every verb; ensure directory/process listings truncate with an explicit notice (FR-040/FR-041).

**Checkpoint**: the lethal-trifecta configuration is contained structurally and by taint.

---

## Phase 9: User Story 7 — Turning it off is as reliable as turning it on (P3)

**Goal**: An operator disables the whole capability; an admin retires it — leaving no stored secrets, orphaned permissions, or half-tracked jobs.

**Independent Test**: flag off → both agents absent, every verb unreachable, behaviour otherwise unchanged; retirement → zero orphaned credential/permission/trust/inventory/job rows, and re-running changes nothing.

### Tests (write first, ensure fail)
- [ ] T062 [P] [US7] Test `backend/orchestrator/tests/test_remote_flag_off.py` — with `FF_REMOTE_COMPUTE` off, neither agent registers, no verb lists/invokes, and the surface/menu item are absent (SC-013).
- [ ] T063 [P] [US7] Test `backend/shared/tests/test_remote_retirement.py` — retirement purges credential/permission/trust/ownership/inventory rows and orphans tracked jobs, idempotently (SC-014); account removal/logout destroys machine credentials.

### Implementation
- [X] T064 [US7] Verify structural flag-off (registration, surface, handlers, verbs all guarded by `agent_authoring`-style `FF_REMOTE_COMPUTE` checks; behaviour byte-identical when off).
- [ ] T065 [US7] Implement the idempotent `_cleanup_retire_063(...)` in `shared/database.py` (destroy credentials; purge `agent_scopes`/`tool_overrides`/`agent_trust`/`agent_ownership` for both ids; orphan non-terminal `tracked_job`; drop the four tables on full retire) per [data-model.md](data-model.md).
- [ ] T066 [US7] Confirm the account-removal/logout revocation (T028) destroys machine credentials as part of the existing revocation flow (FR-015).

**Checkpoint**: the capability is fully reversible.

---

## Phase 10: Polish & Cross-Cutting Concerns

- [X] T067 [P] Author `backend/agents/tests/test_remote_verbs_contract.py` (FR-051) — assert the exact verb set, scopes, argument schemas, destructive classifications, retry posture, and timeouts for both agents against the live `TOOL_REGISTRY`.
- [ ] T068 [P] Write `docs/remote-compute-agents.md` — operator enablement, the FR-054 dependency record (paramiko + transitive set), and the security posture including the honest FR-014 in-process disclosure.
- [X] T069 Run the Python lint gate from the repo root (`ruff check .`) and fix findings; no new client-language lint surface is added.
- [ ] T070 Run both pytest invocations inside the built image against `postgres:17-alpine`; confirm changed-code coverage ≥ 90% (Constitution III/XI, SC-015).
- [ ] T071 Validate the runtime-infrastructure change (paramiko dependency + non-HTTP egress + schema delta) in a qualifying staging deployment of the candidate image, then complete the FR-052 **live-verification checklist from the deployed orchestrator** against the real DGX ([quickstart.md](quickstart.md#live-verification-checklist-fr-052), SC-016); record evidence bound to the candidate SHA.
- [ ] T072 Confirm the `ui_protocol.json` manifest and every client's drift-guard suite are green (Constitution XII); run `quickstart.md` validation.
- [ ] T073 Add the CLAUDE.md "Recent Changes" entry for 063 distinguishing proven-live from code-shaped (FR-052, Constitution XIII), and confirm the `project-dgx-tunneling` reachability consult (research R16) is resolved before declaring reachability proven.
- [ ] T076 [P] Test `backend/agents/tests/test_remote_failure_vocabulary.py` — every verb's failure outcomes each map to a `result-vocabulary` verdict carrying `machine` + `next_action`, with zero generic/empty errors (SC-011/FR-035). *(analyze remediation COV3)*
- [ ] T077 Migrate a **representative dataset** (a seeded `remote_machine` + `machine_credential` + `remote_operation_proposal` + `tracked_job`) through `_init_db` and assert the guarded `063.001` delta applies and the documented rollback restores prior state — not an empty-DB run (Constitution IX). *(analyze remediation CON1)*

---

## Dependencies & Execution Order

### Phase dependencies
- **Setup (P1)** → no deps.
- **Foundational (P2)** → after Setup; **blocks all stories**.
- **US1 (P3)**, **US2 (P4)** → after Foundational; independently testable (US2 practically demos against a machine registered via US1, but its code is independent).
- **US3 (P5)** → after Foundational; **blocks the destructive verbs of US4 and all of US5**.
- **US4 (P6)** → after Foundational + US2 (poller reuses `job_status`); `submit_job` is non-destructive (no US3 dependency), but any `cancel` demo uses US5's `cancel_job`.
- **US5 (P7)** → after **US3** (destructive gate must exist) + Foundational.
- **US6 (P8)** → after US2 (verbs to contain exist); largely realised there, F is the dedicated adversarial + taint pass.
- **US7 (P9)** → after the pieces it retires exist (touches all).
- **Polish (P10)** → after all desired stories; T067 (contract test) requires the full verb set (US5).

### Critical ordering
`US3 (confirmation) MUST be green before US5 (mutating verbs) is enabled` — the spec's hard requirement (§4). Do not implement Phase 7 destructive verbs before Phase 5 passes.

### Parallel opportunities
- Setup: T002, T003 in parallel.
- Foundational: T006/T007 (net_guard+resolver), T010 (FakeTransport), T012/T013/T014 in parallel after T008; T015/T016 after their deps.
- Within each story, all `[P]` test tasks run together; read-verb impls T032/T033/T034 are largely parallel (same file — coordinate) — treat same-file tasks as serial.

---

## Implementation Strategy

**MVP** = Setup + Foundational + **US1** (register + reachability). Stop and validate against the real DGX (register `dgx.ai.uky.edu`, observe an authenticated verdict) before proceeding.

**Then, in priority order**: US2 (the read-only demo — highest value, zero mutation risk) → **US3 (confirmation gate — build and prove before any mutating verb)** → US4 (submit + durable tracking) → US5 (the rest of the mutating verbs) → US6 (containment hardening) → US7 (reversibility) → Polish (contract test, docs, lint, coverage, live-verification).

Each story is an independently testable increment; commit after each task or logical group.

---

## Notes
- `[P]` = different files, no incomplete-task dependency; same-file tasks are serial even if conceptually parallel.
- Every mutating verb is `retryable=False`; the transport converts its own timeout into a structured `unconfirmed`, never a bare `None` (FR-036).
- No verb ever assembles a model-supplied value into a shell string; the `exec "$@"` wrapper is proven to pass args inertly (research R3).
- `signal_process` is retained in the mutating set: FR-024 enumerates a *minimum* set, and D12/FR-027 explicitly classify *killing a process* as destructive — so it ships always-destructive, gated by US3 *(analyze decision SCP1)*.
- Total: **77 tasks** — Setup 3, Foundational 15, US1 11, US2 6, US3 10, US4 6, US5 7, US6 5, US7 5, Polish 9 (T074–T077 added by /speckit.analyze remediation).

---

## 2026-07-27 reconciliation (code-verified sweep, Mac session)

A 10-agent static verification mapped every task above against the branch tip. Checked boxes
are evidence-backed; `(superseded)` marks work the unified-`remote-compute-1` redesign made
moot. **38 boxes remain open — predominantly test-coverage debt, docs, and audit wiring, not
missing product code.** Fixed this session: T064 (FF_REMOTE_COMPUTE re-check at every surface
entry point, with flag-off tests) and T069 (repo-root ruff clean). Top remaining pre-merge
gaps, in rough priority order:

1. **T029** — US1 emits no audit rows (machine register/remove, credential set/delete,
   connection attempts + verdicts; FR-047/FR-049).
2. **T028/T066** — FR-015's account-removal leg: `remove_machine_credentials_for_user` has no
   caller (machine-delete leg works; logout is *not* in FR-015's scope).
3. **T043** — non-destructive mutating verbs return before the unattended-turn check.
4. **T062/T077** — no flag-off agent-registration test; no 063 migration/rollback test.
5. **T068/T073** — operator doc (`docs/remote-compute-agents.md`) and the CLAUDE.md
   Recent-Changes entry do not exist.

Full per-task evidence: session workflow `reconcile-063-tasks` (2026-07-27).
