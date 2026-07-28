# Remote-compute agent operations

This guide covers the fail-closed `FF_REMOTE_COMPUTE` capability introduced by
feature 063. The unified in-process agent `remote-compute-1` ("Remote Compute")
reaches a user's **own** registered SSH machines and Slurm clusters: nine
read-only verbs (inventory, queue, job status/history/output, host facts,
files, processes, reachability) and nine mutating verbs (submit/run/cancel jobs,
create/delete paths, upload files, control services/packages, signal
processes). Every machine, address, and credential is user self-service —
nothing is hardcoded and no host is a default.

With the flag off (the default) the agent does not register, no verb is listed
or invocable, the Remote machines surface and its menu entry are absent, and
the job poller is never started; behavior is byte-identical to the pre-063
product.

## Enable and verify the effective setting

The flag defaults to false and is read **once, at import** of the feature-flag
registry (`backend/shared/feature_flags.py`). Set the boot value in the
deployment's normal Compose environment file without printing that file:

```text
FF_REMOTE_COMPUTE=1
```

Because the value is read at import, changing it requires a **container
recreate, not a restart** — restarting the existing container does not reload
a changed Compose environment:

```bash
docker compose up -d --force-recreate astraldeep
docker compose exec -T astraldeep python -c 'import os; v = os.getenv("FF_REMOTE_COMPUTE", "false").strip().lower(); print("Effective FF_REMOTE_COMPUTE=" + ("true" if v in {"1", "true", "yes"} else "false"))'
```

**The recreate-wipes-docker-cp trap**: source is baked into the image, and the
dev workflow syncs single edits with `docker cp <file> astraldeep:/app/...`.
A recreate rebuilds the container filesystem from the baked image, so any
`docker cp`'d edits that were never baked are silently discarded by the same
command that enables the flag. Rebuild the image (or re-`docker cp` after the
recreate) whenever you toggle this value on a container carrying unbaked edits.

A false result means the agent, verbs, surface, and poller remain unavailable;
fix the deployment input and recreate again. Every surface entry point —
render, native `components()`, and each `chrome_*` handler — re-checks the
flag itself, so a crafted `ui_event` cannot mutate machine state while the
feature is off.

## Dependency record (FR-054, Constitution V exception D1)

Feature 063 adds the product's SSH client library to the backend runtime
image, under the recorded lead-developer approval **D1 (2026-07-23)** in
[spec.md](../specs/063-remote-compute-agents/spec.md). The exact pins in
[`backend/requirements.txt`](../backend/requirements.txt):

| Package | Pin | Why |
|---|---|---|
| `paramiko` | `>=3.4.0` | SSH transport for cluster (Slurm) + host operations |
| `cryptography` | `>=42.0.0` | Already used directly (credential-manager Fernet) and by paramiko/python-jose; pinned **explicitly** rather than relying on transitive resolution |

`paramiko` pulls `bcrypt`, `pynacl`, and `cffi` **transitively**; that
transitive set is part of the recorded FR-054 exception and must be named in
the PR alongside the D1 approval. No other third-party runtime dependency is
added. `paramiko` is imported lazily inside transport methods, so every module
in the feature imports and tests without it.

## Security posture

### Honest in-process disclosure (FR-014)

`remote-compute-1` runs **in-process** in the orchestrator. To open an SSH
connection the transport must decrypt the machine credential, so decrypted key
or password material **transiently exists in orchestrator memory**. The
protections are encryption at rest and per-user isolation — **not** process
isolation. No artifact, UI text, or operator claim may state that the
orchestrator never sees the key.

### Per-machine credentials

Each registered machine has at most one credential (`machine_credential`
table, 1:1 with `remote_machine`, FK `ON DELETE CASCADE`), Fernet-encrypted
under `CREDENTIAL_ENCRYPTION_KEY` — the same key that already protects agent
and per-user LLM credentials, so the existing key-management posture applies
unchanged. Every inventory query is owner-scoped: one user can never see,
name, address, or drive another user's machine, and address/port/username
always come from the stored row, never from the model.

### Host-key pinning (TOFU + refuse-on-change)

Host identity is pinned by SHA256 fingerprint. The first interactive probe —
the user's deliberate registration act — records the key; every later
connection verifies it, and system `known_hosts` is never loaded. A changed or
unknown key **refuses** with the `host_key_mismatch` verdict — there is no
code path that accepts a changed pin. The only way to accept a rebuilt
machine's new identity is the deliberate, owner-scoped re-trust action
(`remote_machines.retrust_host_key`), which clears the recorded key so the
next probe re-records it. The unattended job poller never trusts-on-first-use:
a machine with no pinned key is skipped until an interactive probe pins it.

### Connection-time egress gate (first non-HTTP egress)

SSH is the product's first non-HTTP outbound path; the HTTP egress guard
scheme-locks to http/https and cannot cover it. `shared/net_guard.py` runs
**before any socket opens**: it resolves **all** A/AAAA records and refuses if
any is loopback, link-local (including the `169.254.169.254` cloud-metadata
address), multicast, unspecified, or reserved — while deliberately
**permitting RFC1918**, because legitimate on-prem clusters live there (the
design-input host resolves to both a public and a `10.x` address). IPv4-in-IPv6
encodings (`::ffff:`, 6to4, NAT64) are collapsed to their embedded IPv4 so a
wrapped blocked address cannot slip past. After connect, the peer address
actually reached is re-verified against the gate's resolved set, closing the
DNS-rebinding TOCTOU where a name re-resolves to a blocked address between
gate and connection. Reaching an RFC1918 host still requires the machine to be
in the caller's own inventory, the stored address/port, a matching pinned host
key, and successful SSH auth.

### No shell-string execution

Remote commands are built as a **discrete argv vector** and executed as
`bash -lc 'exec "$@"' _ <shlex-quoted argv…>`: each token is quoted for the
single sshd-exec parse and `exec "$@"` re-vectorises, so metacharacters in an
argument are never interpreted (proven live). `-l` sources the login profile
so cluster tools (Slurm via env-modules) resolve on PATH. Inline job scripts
(`run_job`) are written to the cluster **as data** over SFTP and then run by a
structured `sbatch` argv — the control plane never assembles a shell string.

### Destructive-verb confirmation (durable, single-use, TTL)

Destructive operations (delete a path, cancel a job, signal a process,
stop/disable/restart a service, remove a package, overwrite an existing file)
are gated by `orchestrator/remote_confirmation.py`, enforced at the **shared
dispatch gate** so a differently-named verb, a parallel batch, or a chained
hop cannot bypass it. The classification lives in one place
(`DESTRUCTIVE_CLASSIFICATION`) that both the gate and the agent's registry
import, so verb and classification cannot drift. First reach of a destructive
verb creates a durable proposal (`remote_operation_proposal` row): **15-minute
TTL** in absolute server time, **single-use** (atomic
`UPDATE … WHERE status='approved' RETURNING`), bound to the owner, the verb,
and a SHA256 **fingerprint of the exact arguments** — approval re-enters the
tool through the full gate stack with the stored args, and any drift in
arguments invalidates it. `upload_file` is destructive if-and-only-if the
target already exists (decided by a read-only `stat`; unknowable is treated as
destructive, fail-closed). Submitting a job is consequential but not
destructive (it creates new work). The whole proposal lifecycle
(proposed/approved/declined/expired/consumed/refused) rides the hash-chained
audit under `agent_lifecycle`, correlated by proposal id, with **no secrets
and no argument values** in the rows.

### Unattended refusal and honest timeouts

A destructive verb on any turn with no live human — a machine turn, a
background `VirtualWebSocket`, or no socket — is refused outright
(`unattended_refused`); it cannot show a proposal, so it never runs. A
consequential (non-retryable) call whose deadline expires surfaces the honest
`unconfirmed` verdict — the command may or may not have taken effect; verify
before re-issuing — never a `timeout` a caller might treat as safely
re-attemptable.

### Read-only job poller

The background Slurm poller (started in `Orchestrator.start` only when the
flag is on; `REMOTE_CLUSTER_POLL_SECONDS`, default 30) is **read-only by
construction**: it only ever runs `squeue`, `sacct`, and `tail`. It updates
each tracked job's canvas card in place, notifies on a terminal state when
asked, orphans a job after eight consecutive transport failures or when its
machine/credential disappears, and — as above — never polls a machine whose
host key is not yet pinned.

### Output containment

Verbs return structured, typed, bounded fields parsed from command output;
raw stdout never reaches the model, stderr is bounded and used only to explain
a non-zero exit, and a hard 1 MB transport read cap prevents a runaway stream
(the channel is closed on over-cap so a remote stuck on write cannot wedge the
worker). `remote-compute-1` is additionally registered as an **untrusted taint
source**, so remote-sourced text cannot be laundered into a privileged sink.

### Credential destruction (FR-015)

Every leg is wired and destroys the stored secret rows:

| Event | Path |
|---|---|
| Machine deleted | FK cascade from `remote_machine`, plus an explicit `delete_machine_credential` in the surface handler |
| Sign-out (web and native) | `web_auth._destroy_machine_credentials` on both logout legs, riding the same revocation flow as the refresh token and offline grants |
| Account removal | `remote_machines.purge_user_remote_compute` — the account-removal hook (sibling of the attachments purge): credentials, then inventory and tracked jobs |
| Agent retirement | the guarded retirement migration in `shared/database.py` destroys permission/credential rows for the retired ids |

## Slurm notes (from live DGX verification)

- **Accounts derive from project groups.** A bare cluster username is **not**
  a valid `--account`; the account is the user's project group in
  `<user>_<project>` form (e.g. `sear234_dgxuofl25`). Without it, `sbatch`
  fails with "You must specify an -a/--account" — which the bounded stderr
  surfacing reports verbatim instead of a generic message.
- **GPU visibility needs a GRES allocation.** A job with no GPU request gets
  no devices in its cgroup — `nvidia-smi` reports "No devices were found".
  The submit verbs' `gpus` argument maps to `--gpus=N`; on clusters where
  that does not propagate a device allocation, put the directive in the
  script body (`#SBATCH --gres=gpu:N`).
- The login-shell exec (`bash -lc …`) is what makes Slurm binaries resolve at
  all on Bright-style clusters (env-modules populate PATH in the login
  profile); do not "optimize" it away to a bare exec.

## Rollback and disablement

The safe operational rollback is the kill switch: set `FF_REMOTE_COMPUTE=0`
(or remove it) and recreate the container as above. On the flag-off boot the
agent never registers, so no verb is listed or invocable; the surface and menu
entry are absent and no poll task is created. Disabling does not authorize
deleting user rows: machines,
credentials, proposals, and tracked jobs remain (encrypted/inert) for later
re-enablement and audit. Schema rollback for the four feature tables
(`remote_machine`, `machine_credential`, `remote_operation_proposal`,
`tracked_job`) is documented in
[data-model.md](../specs/063-remote-compute-agents/data-model.md#rollback-whole-feature)
— guarded startup migrations only, never ad-hoc SQL against a live deployment.

## Troubleshooting without exposing secrets

Every remote failure surfaces as one of the fixed verdicts in
[contracts/result-vocabulary.md](../specs/063-remote-compute-agents/contracts/result-vocabulary.md),
each with a next action — trust the verdict rather than reading logs for
secrets:

- `unreachable` / `blocked_address`: check the machine is on and routable from
  the deployment (VPN posture included); loopback/link-local/metadata targets
  are never permitted.
- `auth_failed` / `credential_not_configured` / `credential_undecryptable`:
  re-check or re-enter the machine's credential on the Remote machines
  surface; undecryptable rows usually mean `CREDENTIAL_ENCRYPTION_KEY`
  changed.
- `host_key_mismatch`: if the machine was legitimately rebuilt, re-trust it
  deliberately; otherwise do not proceed.
- `confirmation_required` / `confirmation_expired` / `unattended_refused`:
  the destructive gate is working as designed — approve interactively, or
  re-request after expiry.
- `unconfirmed`: the outcome of a consequential call is unknown; check the
  queue or machine before re-issuing.

The FR-052 live-verification checklist for a candidate image lives in
[quickstart.md](../specs/063-remote-compute-agents/quickstart.md#live-verification-checklist-fr-052--sc-016).
The broader service posture, Keycloak setup, TLS, readiness, and rollback
entry points remain in the
[production deployment guide](production-deployment.md).
