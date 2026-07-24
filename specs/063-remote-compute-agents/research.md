# Phase 0 Research: Remote Compute Agents

**Feature**: `063-remote-compute-agents` | **Date**: 2026-07-24 | **Spec**: [spec.md](spec.md)

This document resolves the unknowns in the plan's Technical Context. Every decision
is grounded either in a **live probe** of the target cluster (recorded 2026-07-24)
or in a **cited line** of the current codebase. Per Constitution XIII, claims about
the system's own behaviour are traced to code as merged, and claims proven live are
distinguished from claims that are only code-shaped.

## Evidence provenance and its limits (honesty note — FR-052)

The live evidence below was gathered by SSH from the **user's workstation** to
`dgx.ai.uky.edu` (UKy AI cluster login node), using the user's own key
(`~/.ssh/id_ed25519_dgx`, `Host dgx` alias). It proves the **cluster environment,
Slurm behaviour, command shapes, and the transport technique** — all of which are
host-independent facts the design depends on. It does **not** prove that the deployed
orchestrator at `sandbox.ai.uky.edu` can reach `dgx.ai.uky.edu` on the deployment
network; that is a separate deployment-routing question owned by the operator (see
[R16](#r16-consuming-project-dgx-tunneling)) and is one of the items the FR-052
live-verification checklist must close **from the orchestrator** before the capability
is declared proven. What is proven-live vs code-shaped is tracked in
[quickstart.md](quickstart.md#live-verification-checklist-fr-052).

---

## R1 — Live reachability and cluster environment

**Decision**: Target the cluster as a **Bright Cluster Manager + Slurm 23.02.8**
environment reached over SSH to a login node, and design the cluster verbs around
Slurm's native tooling rather than any Bright-specific path.

**Rationale (live probe, 2026-07-24)**:
- SSH to `dgx.ai.uky.edu` reached the SSH daemon and authenticated with the user's
  key. Login node identity: `slogin-02`, **Ubuntu 22.04.5 LTS**, kernel 6.8, x86_64.
- Bright Cluster Manager is present (`/cm/...`). **Slurm 23.02.8** binaries live at
  `/cm/shared/apps/slurm/current/bin/{squeue,sbatch,sacct,sinfo,scancel,scontrol,srun}`.
- Partitions observed: `defq*` (8×H100, 224 CPU, ~2 TB RAM, 12 h limit) and `h200`
  (8×H200, no time limit). The user's queue and 7-day `sacct` history were empty.
- Host key recorded at first registration (already in the user's `known_hosts`):
  ed25519 `SHA256:enqFrr+qR4oh52JVYasQV9OHJqkWv2tKm09xxwxLbT0`.

**Alternatives considered**: A Bright-native or REST scheduler API — rejected: D4
fixes the scheduler as Slurm, the standard Slurm CLIs are portable across Slurm
clusters, and FR-009 forbids baking in any Bright/site-specific path (the login-shell
approach in R3 resolves binaries via the user's own environment, not a hardcoded path).

---

## R2 — SSH transport library

**Decision**: Add **`paramiko`** (pure-Python SSHv2) as the single transport
implementation behind an injectable boundary (R15, [contracts/transport.md](contracts/transport.md)).
Approved in D1; recorded as a Constitution V exception in the PR (FR-054).

**Rationale**:
- No SSH capability exists today: `backend/requirements.txt` (68 lines) contains no
  `paramiko`/`asyncssh`/`pypsrp`, and the repo-root `Dockerfile:27-34` installs only
  `poppler-utils libmagic1 build-essential cmake git` — no `openssh-client`. A library
  is required; a shell-out to a bundled `ssh` binary is rejected because it reintroduces
  a shell-string surface (FR-022) and an apt dependency.
- `paramiko` gives programmatic control over host-key policy (R6), per-connection
  timeouts (FR-021), key/password/passphrase auth (D5/FR-011), and `exec_command` for
  the argv pattern in R3 — all without a subprocess.
- **Transitive footprint** to record in the PR (FR-054): `paramiko` → `bcrypt`,
  `pynacl`, `cffi`, and `cryptography`. `cryptography` is already resolved transitively
  via `python-jose[cryptography]` (`requirements.txt:65`) and used directly by
  `credential_manager.py:17`; this feature will **pin it explicitly**. `bcrypt`/`pynacl`
  ship manylinux wheels, so no new apt build package is needed on `python:3.11-slim`.

**Alternatives**: `asyncssh` (native asyncio) — rejected to minimise the dependency
delta and because the transport runs in a worker thread already (dispatch offloads
blocking work via `asyncio.to_thread`, matching `base_agent.py:354`); `fabric` — rejected
(wraps paramiko, adds surface for free-form command running we explicitly forbid).

---

## R3 — Login-shell command execution + argument safety

**Decision**: Every remote command is executed as a **discrete argv vector** through a
**login shell** using the wrapper `bash -lc 'exec "$@"' _ <binary> <arg1> <arg2> …`,
with each token `shlex.quote`d for the single SSH-exec parse. No model-supplied value
is ever concatenated into a shell string (FR-022).

**Rationale — two problems solved at once, both proven live (2026-07-24)**:
1. **Slurm is invisible without a login shell.** A plain `ssh host 'squeue'` runs a
   non-login shell whose `PATH` lacks Slurm — every Slurm binary probed as `MISSING`.
   A login shell (`bash -l`) sources the Bright environment-modules profile, which
   auto-loads `slurm/slurm/23.02.8`. Proven: `ssh dgx 'bash -lc "command -v squeue"'`
   → `/cm/shared/apps/slurm/current/bin/squeue`.
2. **Argument safety.** The `exec "$@"` idiom hands the positional parameters to the
   target binary as an exact argv vector, so shell metacharacters in an argument are
   **never interpreted**. Proven: passing the argument
   `harmless; echo PWNED; $(whoami); ` + backtick-`id`-backtick to `/bin/echo` returned
   it **verbatim** — no command substitution, no `PWNED`, no `whoami`/`id` execution.

This reconciles the login-shell requirement with FR-022. The one unavoidable parse is
the SSH exec channel itself (sshd runs the string through the user's shell); `shlex.quote`
per token covers that parse, and `exec "$@"` re-vectorises so even a residual quoting
gap cannot become executable. GPU facts note: `nvidia-smi` is **absent on the login
node** (GPUs are on compute nodes), so cluster accelerator facts come from Slurm GRES
(`sinfo`), never from `nvidia-smi` on the queried host.

**Alternatives**: hardcoding `/cm/shared/apps/slurm/current/bin/` — rejected (FR-009,
Bright-specific, brittle across clusters); `paramiko invoke_shell` (interactive PTY) —
rejected (Out of Scope: no PTY/interactive session); building `bash -lc "<cmd string>"`
with hand-quoted args — rejected (double-parse, error-prone; the `exec "$@"` form removes
the inner parse entirely).

---

## R4 — Structured field extraction (FR-038, FR-041)

**Decision**: Return **typed fields only**, parsed from machine formats: Slurm's native
**`--json`** for cluster verbs, and fixed typed commands for host facts. No raw text,
log, or file content is returned to the model in v1.

**Rationale (live probe)**:
- Slurm 23.02.8 supports `squeue --json`, `sinfo --json`, and `sacct --json` — all
  three returned valid JSON. `squeue --me --json` yields `{meta, jobs, warnings, errors}`
  with typed job objects. This is an authoritative typed source — no text scraping, so
  no terminal escape sequences or instruction-shaped text ride in on a formatted table
  (FR-041). A delimited fallback (`squeue -o '%i|%P|%T|%M|%L|%D|%C|%m|%b|%R'`) is retained
  for a scheduler too old for `--json`, mapped onto the same typed result.
- Host facts have clean typed sources: `/proc/loadavg` (1/5/15), `/proc/uptime` (seconds),
  `nproc`, `free -b` (bytes), `df -B1 --output=source,target,fstype,size,used,avail,pcent`
  (per mount), `ps -eo pid,user,pcpu,pmem,rss,comm --sort=-pcpu`, and
  `find <dir> -maxdepth 1 -printf '%y|%s|%TY-%Tm-%Td|%f\n'` for directory listings.
- Every remote-derived string field (job name, filename, `comm`, queue reason) is
  **bounded and sanitised** (control chars/escape sequences stripped, truncation marked)
  before it reaches a primitive — see [contracts/verbs.md](contracts/verbs.md) field bounds.
  The shared-NFS reality (`/home`, `/scratch` on Isilon) and a process list that exposes
  **other users'** `comm` values confirm this text is genuinely untrusted (R12).

**Alternatives**: parsing default human `squeue`/`df` output — rejected (fragile columns,
locale-sensitive, carries escape sequences); returning raw command stdout — rejected
(Out of Scope in v1; FR-038).

---

## R5 — Connection-time egress gate (FR-019)

**Decision**: Add a **scheme-neutral SSH connection gate** that resolves the target at
connect time and refuses **loopback, link-local (incl. the `169.254.169.254` metadata
address), multicast, unspecified, and reserved** addresses — but **permits RFC1918
private addresses**, because legitimate on-prem clusters live there. Reuse the resolve-all
helper from `external_http`; do **not** reuse its HTTP predicate unchanged.

**Rationale (code + live DNS, the load-bearing surprise)**:
- This is the product's first non-HTTP outbound path; `external_http.validate_egress_url`
  (`external_http.py:116-141`) scheme-locks to http/https (`:123`) and so cannot cover it.
- `dgx.ai.uky.edu` resolves to **both** `128.163.37.132` (public UKy) **and**
  `10.33.77.11` (RFC1918). The existing predicate `_is_private_address`
  (`external_http.py:95-108`) rejects `ip.is_private`, so reusing it verbatim would
  **block the DGX itself**. The SSH gate therefore drops the `is_private` clause and keeps
  the rest, resolving **all** records via `_resolve_host_addresses` (`:81-92`) and refusing
  if **any** resolved address is in the SSH denylist (anti-rebinding, FR-019).
- Allowing RFC1918 widens SSRF-to-internal surface; it is contained by the layered
  controls that must **all** hold before a byte is sent: the target must be in the
  **invoking user's own** inventory (FR-018), address/port come from the **stored record**
  not model arguments (FR-018), the recorded **host key must match** (R6/FR-020), and SSH
  auth must succeed with the user's own credential. A blind pivot cannot satisfy all four.
- Implementation: promote the two helpers into a small shared `net_guard` (or add a
  scheme-neutral public wrapper) so the SSH gate does not import underscore-prefixed names
  across modules.

**Alternatives**: blanket-block all RFC1918 — rejected (breaks the actual target, proven
by the DNS result); trust the OS/network to prevent SSRF — rejected (FR-019 requires an
explicit in-product gate).

---

## R6 — Host-key verification and re-trust (FR-020)

**Decision**: Record the host's public key **fingerprint at first registration** and
verify it on **every** connection with paramiko's **`RejectPolicy`** against a per-machine
recorded key (never the system `known_hosts`, never `AutoAddPolicy`). A mismatch refuses
the operation, audits it, and requires a **deliberate explicit re-trust** action by the
user; there is no automatic-accept path anywhere.

**Rationale**: FR-020 forbids automatic acceptance of a changed/unknown identity on any
path. paramiko's default `RejectPolicy` refuses unknown keys; we bind the client to the
single key recorded for that machine so a legitimate rebuild (edge case) surfaces as an
explicit host-key-mismatch verdict (FR-034) the user must resolve by re-recording, which
is distinguishable from silent acceptance. The recorded fingerprint is a property of the
`remote_machine` row ([data-model.md](data-model.md)).

**Alternatives**: `AutoAddPolicy` / TOFU-on-every-connect — rejected outright (FR-020);
shared system `known_hosts` — rejected (leaks identities across users, violates per-user
isolation FR-010).

---

## R7 — Two agents, and decoupling visibility from safe-seeding (FR-001–FR-004)

**Decision**: Ship **two** in-process bundled agents — a **read-only** agent
(`remote-observe-1`, safe-seeded) and a **mutating** agent (`remote-control-1`, never
safe-seeded). Split the single `_FIRST_PARTY_PUBLIC_AGENT_IDS` concept into **two sets**:
a *visibility* set (both agents, so both are discoverable) and a *safe-seed* set
(read-only agent only).

**Rationale (grounded)**: Today `_FIRST_PARTY_PUBLIC_AGENT_IDS` (`database.py:2990-2994`)
does double duty — it drives public visibility (registration `orchestrator.py:3448-3452`
+ backfill `database.py:3011-3016`) **and** boot safe-seeding
(`orchestrator.py:17230-17231` → `agent_trust.seed_safe`, `FF_SAFE_AGENTS` default on).
`_safe_flip_allowed` (`tool_permissions.py:400-424`) only honours a safe flip for
public/ownerless agents, so the two are currently welded. FR-004 requires them separated.
The minimal, faithful change: keep `_FIRST_PARTY_PUBLIC_AGENT_IDS` as the *visibility*
set (add both agent ids) and introduce a `_SAFE_SEED_AGENT_IDS` subset that
`seed_safe` consumes at boot (`remote-observe-1` only; the existing nine remain seeded).
The mutating agent is thus **discoverable but not pre-authorised** — its `tools:write`/
`tools:system` verbs need an explicit `agent_scopes` grant, which in
`_resolve_tool_allowed` (`:335-340`) wins ahead of any flip. Its grant is independently
revocable (a distinct `agent_scopes`/`agent_id` row), satisfying FR-003.

**Alternatives**: one agent with per-tool exclusions — rejected (spec §3: the baseline
flip is agent-wide, cannot be narrowed per tool); leaving the sets welded and marking the
mutating agent unsafe after seeding — rejected (races the boot window; FR-003 says "never
seeded under any configuration").

---

## R8 — Durable operation confirmation (the net-new mechanism, FR-029–FR-033)

**Decision**: Build a **durable, single-use, expiring, user-bound, argument-bound**
proposal record in a new table, surfaced through the **proven** `Button(action=…)` →
`ui_event` pattern (a new `remote_op_decision` action), **not** the dormant
`authorize_action` path. Approval re-enters the **full gate stack** via
`execute_single_tool` against the **stored** arguments.

**Rationale (grounded)**:
- The `authorize_action` round-trip is fully coded server-side (`orchestrator.py:7489-7506`,
  `ui_protocol.json:94`) but **dormant**: every confirm/HITL refusal emits a **buttonless**
  `Alert` (`orchestrator.py:12539-12551`, `12604-12623`; `Alert` has no action field,
  `primitives.py:197-204`), and **no client emits `authorize_action`** (no
  `Button(action="authorize_action")` anywhere; clients dispatch button actions generically,
  e.g. Android `Basic.kt:148-150`). Reviving it is out of scope (spec Dependencies).
- The **proven-across-clients** confirmation is `scheduling_chat`: a consent `Card` whose
  `Button`s carry `action="schedule_decision"` + `payload{proposal_id, decision}`
  (`scheduling_chat.py:246-249`), routed at `orchestrator.py:7332-7337`, validated for
  ownership (`:317`), TTL (`:321`), re-validated at approval (`:338`), and popped single-use
  (`:373`). Its weakness is the store: an **in-memory dict** with `PROPOSAL_TTL_S=900`
  (`scheduling_chat.py:36`, `:118-122`) that does not survive restart. FR-032 requires
  durability, so the proposal becomes a **table** (`remote_operation_proposal`,
  [data-model.md](data-model.md)) with a boot-safe status machine; the existing
  `scheduler/store.py::ScheduledJobStore` is the durable-store template.
- Enforcement lives at the **dispatch gate**, not in the tool: hard blocks already sit
  there (`orchestrator.py:12478-12489`), independent of `tool_permissions`
  (`tool_permissions.py:332-334`). The destructive-classification gate refuses any
  destructive verb lacking a matching, fresh, unused approval bound to the exact
  arguments — closing the parallel-batch (`execute_parallel_tools`) and chained-hop paths
  (FR-030/US3 scenario 6), because the check is on the verb's declared class, not its name
  (FR-007) or the tool's cooperation. See [contracts/confirmation.md](contracts/confirmation.md).

**Alternatives**: revive `authorize_action` + add buttons on four clients — rejected (larger,
riskier, and the spec scopes generalising HITL to a later feature); reuse the in-memory
scheduling store — rejected (fails FR-032 across restart).

---

## R9 — Non-retryable consequential verbs (FR-036, FR-037)

**Decision**: Every consequential verb is **declared non-retryable** and the transport
**never surfaces a bare `None`/timeout** into the retry path; a slow/lost consequential
call returns a structured `unconfirmed` result (FR-034) telling the user to verify.
Submission additionally carries a **duplicate-detectable identifier**.

**Rationale (grounded)**: `_execute_with_retry` (`orchestrator.py:13862`) retries up to
`MAX_RETRIES=3` (`:12140`); `is_retryable` defaults **True** (`:13890`) and reads
`error.get("retryable", True)` (`:13893`); a `None`/timeout result keeps the default and
**retries** (`:13890`, and dispatch emits explicit `retryable:True` on timeout). So two
things are required: (a) consequential verbs return `MCPResponse(error={"retryable": False, …})`
(the `unconfirmed` vocabulary entry), and (b) the transport enforces its **own** bounded
timeout (FR-021) and converts a timeout into that structured non-retryable result rather
than letting the 30 s dispatch timeout (`TOOL_TIMEOUT_OVERRIDES.get(tool, 30.0)`, `:13879`)
produce a `None` that would be retried. For submission, an idempotency marker (e.g. Slurm
`--comment=<nonce>` / a deterministic job-name tag) makes a duplicate detectable
cluster-side (FR-037), so even a genuinely ambiguous outcome can be reconciled by the next
queue read rather than by blindly resubmitting.

**Alternatives**: rely on the dispatch default — rejected (its default is to retry, the
opposite of what consequential work needs); wrap submit in an at-most-once lock only —
insufficient across a restart between send and record (edge case); the idempotency marker
plus honest `unconfirmed` covers it.

---

## R10 — Per-user, per-host credentials (FR-010, FR-011, FR-014–FR-016)

**Decision**: Store credentials in a **new `machine_credential` table keyed per user and
per machine**, Fernet-encrypted at rest under `CREDENTIAL_ENCRYPTION_KEY`, supporting a
private key (+ optional passphrase) or a password. Do **not** overload `user_credentials`.

**Rationale (grounded)**: `user_credentials` is `UNIQUE(user_id, agent_id, credential_key)`
with **no host dimension** (`database.py:430-439`) — it cannot express "this key for that
machine." Encoding host into `credential_key` would corrupt the settings-surface prompt
model (which enumerates declared keys). A dedicated table keyed on the `remote_machine`
row is cleaner and gives an explicit revocation target. Encryption reuses the proven
`credential_manager` Fernet path (`credential_manager.py:57-76`, `:110`/`:141`). **FR-014
honesty**: because these are in-process agents (R7), decrypted key material transiently
exists in orchestrator memory during a connection — the protection is **encryption at rest
+ per-user isolation, not process isolation**; no artifact may claim otherwise. Revocation
(FR-015) is wired for all four triggers — machine delete, credential delete, agent
retirement, account removal — unlike today's `remove_agent_credentials`
(`credential_manager.py:212`) which has **zero production call sites**. An undecryptable
row (key rotated) reports the distinct `credential_undecryptable` verdict (FR-016), separate
from `credential_not_configured` and `auth_failed`.

**Alternatives**: reuse `user_credentials` with a 4th column — rejected (widens a shared,
heavily-used table and its UNIQUE constraint for one feature; a scoped table is safer and
independently droppable on retirement).

---

## R11 — Both-client inventory + credential surface (FR-012)

**Decision**: Build the remote-machines surface with the **full both-client trio**
(`render()` web HTML **and** `components()` native), following `llm.py` as the template,
and use a **multi-line field** for the private key (`<textarea>` on web, `kind="textarea"`
on native `_sdui.field`).

**Rationale (grounded)**: The surface contract (`surfaces/__init__.py:3-19`) is `TITLE`,
`async render(...)`, optional `components()`, and a `HANDLERS` dict of `chrome_*` actions;
native binds to the same handler keys (`_sdui.py:6-9`). The existing agent-credential UI
(`agents.py`) is **web-only** (no `components()`) and single-line
(`<input type="password">`, `agents.py:513-514`) — a pasted PEM would be truncated and no
native client would render it. `llm.py` already implements the credential-form trio
(`:295`/`:396`/`:695`, `_sdui.form` `:479`, `_sdui.field(..., "password")` `:446`), and
`_sdui.field` supports `kind="textarea"` (`_sdui.py:88-95`). This is the model to copy.
The watch form-factor cannot present the multi-line entry or the destructive approval
control; per Constitution XII + FR-033 it degrades to a "continue on phone/desktop" message
driven from the shared definition (US3 scenario 7).

**Alternatives**: extend `agents.py` — rejected (web-only, would need a `components()` retrofit
anyway and mixes concerns); a bespoke non-astralprims form — rejected (Constitution II/XII).

---

## R12 — Taint-source registration (FR-039)

**Decision**: Add **both** agent ids to `taint._UNTRUSTED_AGENTS`.

**Rationale (grounded)**: `_UNTRUSTED_AGENTS = {"web-research-1", "summarizer-1"}`
(`taint.py:37`), consulted in `classify_source` (`:68`). Adding `remote-observe-1` and
`remote-control-1` makes their outputs classified untrusted wherever the taint machinery
is enabled (`FF_TAINT_TRACKING`/`FF_DATAMARKING`/`FF_MAS_DEFENSE` all default off today, so
this is inert until enabled — but ships now so the classification is correct the moment it
is turned on, D11/FR-039). This complements the structural containment (R4): even the typed
fields are marked untrusted, defence-in-depth for the lethal-trifecta configuration the spec
identifies (§5).

**Alternatives**: rely on structured-output alone — rejected (D11 requires both; taint is the
mechanism that tracks a remote value into a sink if v2 ever widens output).

---

## R13 — Durable job tracking, boot reconciliation, unattended read-only polling (FR-042–FR-046)

**Decision**: A new **`tracked_job`** table links a scheduler job id → machine, user, and
chat. A **boot reconciliation** pass resolves jobs that finished during an outage.
Unattended polling runs **read-only** under the existing machine-turn authority; unattended
submit/cancel are refused fail-closed.

**Rationale (grounded)**: HPC queue waits are hours/days, so a record that dies with the
process is not a feature (US4). Durability follows the `scheduler/store.py` pattern.
"No live human" is already modelled: `MACHINE_TURN_CLASSES = ("scheduled_job",
"parser_replay", "draft_self_test")` and authority is derived from stored offline-grant
consent, never a live session (`chain_authority.py:159-246`, `derive_machine_authority`
`orchestrator.py:8844-8861`); an undrivable authority is `AuthoritySkip`, fail-closed. The
poller derives **read-only** authority (status verbs only); the confirmation gate (R8) and
the machine-turn check together refuse any consequential verb on a machine turn (FR-044) —
the refusal does not depend on a scheduler flag staying off. When a tracked job's machine
or credential is deleted, polling stops and the record is closed `orphaned` (FR-046).

**Alternatives**: chat-transcript-only tracking — rejected (cannot answer "is it done?" after
restart); a brand-new scheduler — rejected (reuse the machine-turn authority + a job table).

---

## R14 — Output containment specifics (FR-040, FR-041)

**Decision**: Every remote-derived field has a **declared byte/entry bound**; over-bound
values are **truncated with a visible notice**; all strings are **sanitised** (strip control
chars and ANSI escapes, never interpret as markup). Directory listings cap entries with an
explicit "showing N of M" notice.

**Rationale**: The probe found a home directory with a 281 MB filename entry and a shared
login node exposing other users' process names — real cases where an unbounded or unsanitised
field would either flood the model or smuggle escape sequences into a rendered surface. Bounds
live per field in [contracts/verbs.md](contracts/verbs.md); sanitisation is a single helper
applied at the transport boundary so no verb can forget it.

---

## R15 — Injectable transport boundary + dependency posture (FR-050, FR-054)

**Decision**: All remote access goes through **one** `RemoteTransport` protocol
([contracts/transport.md](contracts/transport.md)) with a `ParamikoTransport` production
implementation and an in-memory `FakeTransport` test double, injected the way the LLM client
factory is (constructor/factory seam). Tests run with **no real machine, no SSH server, no
network** (FR-050). `paramiko` and its transitive deps are recorded in the PR as the D1
Constitution V exception (FR-054); `cryptography` gains an explicit pin.

**Rationale**: A single seam is the only way to meet SC-015 (full E2E in CI without a network)
while keeping the live checklist (SC-016) as the separate real-world proof. The seam also keeps
the egress gate (R5) and host-key policy (R6) testable in isolation.

---

## R16 — Consuming `project-dgx-tunneling`

**Decision**: Treat orchestrator→cluster **network reachability** as a deployment
responsibility to be **verified from the orchestrator** on the FR-052 checklist, and consult
`project-dgx-tunneling` (owner Vaiden Logan; being folded into LLM Factory as of 2026-07-10)
**before** implementation begins, consuming its result rather than duplicating it.

**Rationale**: The spec's standing assumption is that reachability "has not been measured"
from the deployment. This research **measured it from the user's workstation** (reachable,
authenticated) — which de-risks the transport design but is **not** the deployment path. The
honest status: cluster environment and transport technique are **proven-live**; deployment
reachability from `sandbox.ai.uky.edu` is **unproven** and is the first checklist item. If
`project-dgx-tunneling` already establishes that path, this feature consumes it.

---

## Resolved unknowns summary

| Unknown (Technical Context) | Resolution |
|---|---|
| SSH library + deps | paramiko (+ bcrypt/pynacl/cffi; pin cryptography) — R2 |
| Slurm version / reachability | 23.02.8 on Bright, reachable+authed from workstation — R1 |
| Getting Slurm on PATH safely | login-shell `exec "$@"` argv wrapper, proven — R3 |
| Typed output source | Slurm `--json` + typed host commands — R4 |
| First non-HTTP egress gate | SSH-specific denylist (allow RFC1918), reuse resolver — R5 |
| Host-key trust | RejectPolicy + per-machine recorded key + explicit re-trust — R6 |
| Visibility vs safe-seed | split into two id sets — R7 |
| Durable confirmation | new table + `remote_op_decision` button/ui_event — R8 |
| Retry duplication | declared non-retryable + timeout-not-None + idempotency marker — R9 |
| Per-host credentials | new `machine_credential` table, Fernet — R10 |
| Native-parity surface | `render()`+`components()` + textarea (llm.py template) — R11 |
| Taint sources | add both ids to `_UNTRUSTED_AGENTS` — R12 |
| Durable jobs / unattended | `tracked_job` + boot reconcile + machine-turn read-only poll — R13 |
| Test without a network | injectable `RemoteTransport` + `FakeTransport` — R15 |
