# Contract: The Fixed, Closed Verb Set

**Feature**: `063-remote-compute-agents` | **Spec**: [../spec.md](../spec.md)

This is the authoritative, reviewable definition of every verb the two agents expose
(FR-022, FR-023, FR-024, FR-025). It is the **contract-test target** (FR-051): a test
asserts the exact verb set, argument shapes, scope declarations, timeouts, retry posture,
and destructive classifications — so adding a verb, widening an argument, or reclassifying
a destructive operation cannot pass unnoticed.

## Global rules (apply to every verb)

- **No command strings.** No argument accepts a shell fragment, pipeline, redirection, or
  command substitution; no code path assembles model-supplied values into a shell string.
  Arguments are discrete typed values, executed via the login-shell `exec "$@"` argv wrapper
  ([transport.md](transport.md), proven live 2026-07-24). (FR-022)
- **Machine addressing.** Every verb takes a `machine_id` referencing a row in the invoking
  user's own `remote_machine` inventory. Address, port, username come from that row — never
  from arguments. A `machine_id` not owned by the caller returns `permission_denied` and is
  audited. (FR-018, FR-010, SC-012)
- **Typed output only.** Every returned value is a bounded, typed field
  ([result-vocabulary.md](result-vocabulary.md), FR-038/FR-040/FR-041). No raw text/log/file
  content in v1.
- **Declared time bound.** Each verb declares a timeout; exceeding it yields the `timeout`
  verdict, never an indefinite wait. (FR-021)
- **Honest failure.** Every outcome maps onto the result vocabulary; every failure names the
  machine and the next action. (FR-034/FR-035)
- **Path arguments** are validated: absolute, NUL-free, length-bounded; passed as a single
  argv token (metacharacters are inert per the transport proof). Validation is defence in
  depth, not the primary control.

---

## Read-only agent — `remote-observe-1`

Safe-seeded (FR-002). Every verb is incapable of changing remote state (provable from the
verb set, FR-023 / US2 scenario 4). All `tools:read`/`tools:search` scope; all **retryable**
(idempotent reads).

| Verb | Scope | Args (typed) | Returns (typed) | Timeout | Destructive |
|---|---|---|---|---|---|
| `list_machines` | `tools:read` | — | table: `{label, address, port, os_family, role, last_verdict, last_checked_at}` per owned machine | 5 s | no |
| `probe_machine` | `tools:read` | `{machine_id}` | `{verdict, os_family, authenticated: bool, host_key_recorded: bool}` — performs a real connection; records host key on first (FR-013/FR-020) | 20 s | no |
| `list_queue` | `tools:read` | `{machine_id, state?: enum}` | table of the user's own jobs: `{job_id, name, state, elapsed, time_left, nodes, cpus, min_memory, tres, partition, reason}` via `squeue --me --json` | 30 s | no |
| `job_status` | `tools:read` | `{machine_id, job_id}` | `{state, elapsed, start_time, submit_time, nodes, cpus, memory, tres, reason, exit_code?}` via `squeue --json` then `sacct --json` if terminal | 30 s | no |
| `job_history` | `tools:read` | `{machine_id, days?: int≤30}` | table: `{job_id, name, state, elapsed, exit_code, partition, alloc_cpus, req_mem, end_time}` via `sacct --json` | 45 s | no |
| `host_facts` | `tools:read` | `{machine_id}` | `{os_family, uptime_seconds, load_1, load_5, load_15, cpu_count, mem_total_bytes, mem_used_bytes, disks:[{mount, fstype, size_bytes, used_bytes, avail_bytes, use_pct}], gpus?:[{model, count}]}` (GPU from Slurm GRES for cluster role; not `nvidia-smi`) | 30 s | no |
| `list_directory` | `tools:read` | `{machine_id, path}` | `{path, entries:[{type, size_bytes, mtime, name}], shown, total, truncated}` via `find -maxdepth 1 -printf`; names sanitised + bounded | 30 s | no |
| `list_processes` | `tools:read` | `{machine_id, own_only?: bool=true}` | table: `{pid, user, cpu_pct, mem_pct, rss_bytes, comm}` via `ps -eo …`; `comm` sanitised; defaults to the user's own processes | 30 s | no |

Notes:
- `probe_machine` is the only read verb that opens a connection for its own sake; it is still
  read-only (no remote state changes) and is how US1's save-time verdict (FR-013) is produced.
- `list_processes` defaults `own_only=true` because the shared login node exposes other users'
  process names (observed live); the field is still untrusted+sanitised regardless.

---

## Mutating agent — `remote-control-1`

**Never safe-seeded** (FR-003); every verb needs an explicit per-user grant. All verbs are
**non-retryable** (FR-036 — consequential; the transport converts its own timeout into a
structured `unconfirmed`, never a bare `None`, [transport.md](transport.md) §retry). Scopes
are `tools:write` (filesystem/scheduler) or `tools:system` (host administration) per FR-006.

| Verb | Scope | Args (typed) | Destructive classification (FR-027/FR-028) | Timeout |
|---|---|---|---|---|
| `submit_job` | `tools:write` | `{machine_id, script_path, partition?, time_limit?, nodes?≥1, gpus?≥0, job_name?, account?}` — `script_path` is a remote path to an **existing** sbatch script; options are typed sbatch flags | **not destructive** (creates new work); consequential → non-retryable; carries a submit marker (FR-037) | 30 s |
| `make_directory` | `tools:write` | `{machine_id, path}` — `mkdir -p` | **not destructive** | 20 s |
| `upload_file` | `tools:write` | `{machine_id, attachment_id, remote_path}` — SFTP an AstralDeep attachment to a path | **destructive IFF `remote_path` already has content** (declared `destructive_if_exists`; a read-only stat decides — no command parsing) → proposal on overwrite (FR-027, US5-2) | 60 s |
| `cancel_job` | `tools:write` | `{machine_id, job_id}` — `scancel` | **destructive** (always) → proposal | 20 s |
| `remove_path` | `tools:write` | `{machine_id, path, recursive?: bool=false}` | **destructive** (always) → proposal | 30 s |
| `control_service` | `tools:system` | `{machine_id, service_name, action: enum{start,stop,restart,enable,disable}}` | **destructive IFF `action ∈ {stop,disable,restart}`** (declared per enumerated value; restart interrupts → destructive) → proposal | 30 s |
| `manage_package` | `tools:system` | `{machine_id, package_name, action: enum{install,remove}}` | **destructive IFF `action == remove`** (declared per enumerated value) → proposal | 120 s |
| `signal_process` | `tools:system` | `{machine_id, pid, signal: enum{TERM,KILL}}` | **destructive** (always — killing a process, D12) → proposal | 15 s |

> **Scope note (SCP1):** `signal_process` is included under FR-024's "at minimum" latitude — D12/FR-027 explicitly classify *killing a process* as destructive, so it ships always-destructive and gated by [confirmation.md](confirmation.md).

### Destructive classification is a declared property (FR-028)

Each mutating verb declares its classification in a reviewed, in-code map — one of:
- `always` (cancel_job, remove_path, signal_process),
- `never` (submit_job, make_directory),
- `if_exists` (upload_file — decided by a read-only stat of `remote_path`),
- `by_action:{…}` (control_service, manage_package — decided by the enumerated `action` value).

No classification is derived by parsing a command string, and no gate depends on the verb's
**name** (FR-007). The `if_exists`/`by_action` predicates are deterministic, reviewable
functions of typed arguments + one read probe — not heuristics. The registration-time
`tool_security` analyser (`tool_security.py:279`) is treated as **informative only**; the real
control is this declared map + the confirmation gate ([confirmation.md](confirmation.md)).

### Argument-shape guards (FR-022, US5-3)

- `path`/`remote_path`/`script_path`: absolute, NUL-free, ≤ 4096 bytes, single argv token.
- `job_id`/`pid`: integer-shaped (`^\d+$`).
- `service_name`/`package_name`: `^[A-Za-z0-9._@+-]+$` (no whitespace/metachars).
- enums validated against the closed set; unknown → `invalid_argument` (mapped to the vocab).
- Any argument containing a shell fragment is refused by the shape guard **before** dispatch;
  even if it slipped through, the `exec "$@"` wrapper renders it inert (proven).

---

## Discoverability before granting (FR-025)

Both agents' full verb lists (and one-line descriptions of what each can do, and whether it is
destructive) are visible on the agents chrome surface before a user grants anything, sourced
from the same `TOOL_REGISTRY` metadata the contract test asserts.

## Contract test (FR-051) — assertions

`backend/agents/tests/test_remote_verbs_contract.py` asserts, against the live
`TOOL_REGISTRY` of both agents:
1. **Exact verb set**: `remote-observe-1` = the 8 read verbs above; `remote-control-1` = the 8
   mutating verbs above — no more, no fewer.
2. **Scope** of each verb equals the table (read verbs `tools:read`; mutating `tools:write`/
   `tools:system` as listed).
3. **Argument schema** of each verb matches (required keys, types, enum members).
4. **Destructive classification** of each mutating verb equals the declared map
   (`always`/`never`/`if_exists`/`by_action`).
5. **Retry posture**: every `remote-control-1` verb declares `retryable=False`; read verbs may
   retry.
6. **Timeout** declared and > 0 for every verb.

A change to any of these fails the test — the mechanical enforcement FR-051 requires.
