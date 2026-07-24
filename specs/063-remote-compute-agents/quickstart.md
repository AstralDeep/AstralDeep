# Quickstart: Remote Compute Agents

**Feature**: `063-remote-compute-agents` | **Spec**: [spec.md](spec.md)

## Operator enablement

The whole capability sits behind a single fail-closed flag, **`FF_REMOTE_COMPUTE`
(default off)** (FR-005). Like every flag in `shared/feature_flags.py`, it is read **once at
import**, so enabling it needs a **container recreate**, not a restart.

With the flag off: neither `remote-observe-1` nor `remote-control-1` registers, no verb is
listed or invocable, the `remote_machines` surface and its menu item do not exist, no schema
element is required beyond the (inert) tables, and observable behaviour is identical to the
product without this feature (SC-013).

To enable:
1. Add the runtime dependency and rebuild the image (D1 / FR-054 — recorded as a Constitution V
   exception in the PR): `paramiko` in `backend/requirements.txt` (pulls `bcrypt`, `pynacl`,
   `cffi`, `cryptography`; `cryptography` is now pinned explicitly). No new apt package —
   `python:3.11-slim` resolves the manylinux wheels; the repo-root `Dockerfile:27-34` apt list is
   unchanged.
2. Ensure `CREDENTIAL_ENCRYPTION_KEY` is set (already required by `credential_manager`) — machine
   credentials are Fernet-encrypted under it.
3. Set `ASTRAL_ENV=development` locally (production posture fails closed otherwise), or configure
   the production secrets per `docs/production-deployment.md`.
4. Recreate the container with `FF_REMOTE_COMPUTE=1`.
5. **Network reachability from the deployment to the target machines is the operator's
   responsibility** (Assumptions). See the checklist below — this must be proven **from the
   orchestrator**, not just from a workstation.

The read-only agent is safe-seeded (works immediately for any signed-in user). The mutating
agent is visible but requires an explicit per-user grant before any of its verbs run (FR-003).

## User walkthrough (the demo)

1. **Register a machine.** Settings → *Remote machines* → Add. Enter a label, address, port,
   username, OS family, role (cluster/plain), and a credential (paste a multi-line SSH private
   key, with a passphrase if the key is encrypted, or a password). Save → the product opens a
   real connection and shows a verdict (authenticated / unreachable / auth-rejected / host-key
   mismatch). The inventory started empty; nothing was pre-filled.
2. **See what's happening (no grant needed).** In chat: *"what's in my queue?"* → a structured
   table of your jobs. *"status of job 12345?"* → typed fields (state, elapsed, resources, reason,
   exit code). *"disk and load on my workstation?"* → typed host facts. Nothing changed on any
   machine.
3. **Submit work (needs the mutating agent granted).** *"submit /home/me/train.sbatch on the
   cluster to the h200 partition"* → a durable job id you can ask about later, from any device,
   even after a restart. Opt in to be told when it finishes (in your AstralDeep clients — there
   is no email).
4. **A destructive request stops and waits.** *"delete /scratch/old_run"* → nothing is deleted.
   You see a proposal naming the machine, the operation, and the exact path, with Approve /
   Decline. Only when you press Approve (or type an approval) does the removal run — exactly once,
   against exactly that path. The approval cannot be reused, cannot be redirected, expires, and a
   pending proposal survives a restart.

## Live-verification checklist (FR-052 / SC-016)

No test substitute can prove reachability, authentication, or real scheduler behaviour. This
checklist is completed against a **real machine and a real cluster**, from the **deployed
orchestrator**, with evidence recorded, before the capability is declared proven. It honestly
distinguishes what is already proven from what remains code-shaped.

### Already proven live (workstation → `dgx.ai.uky.edu`, 2026-07-24)

These are host-independent transport/environment facts, proven from the user's workstation:
- [x] SSH reachability + key auth to the DGX login node (`slogin-02`, Ubuntu 22.04).
- [x] Slurm 23.02.8 present; **login shell required** to resolve it (Bright env-modules).
- [x] The `bash -lc 'exec "$@"' _ …` wrapper resolves Slurm **and** passes arguments as discrete
      argv — shell metacharacters in an argument are inert (injection attempt returned verbatim).
- [x] Slurm native `--json` works for `squeue`/`sinfo`/`sacct` (the typed-field substrate).
- [x] Typed host-fact command shapes (`/proc/loadavg`, `free -b`, `df -B1 --output=…`, `ps -eo`,
      `find -printf`); `nvidia-smi` absent on the login node (GPU facts from Slurm GRES).
- [x] `dgx.ai.uky.edu` resolves to a public **and** an RFC1918 address (egress-gate design input).

### Must be proven from the deployed orchestrator (code-shaped until checked)

- [ ] **Reachability from the deployment**: the orchestrator at `sandbox.ai.uky.edu` can open
      TCP/22 to `dgx.ai.uky.edu` (consult/consume `project-dgx-tunneling`, owner Vaiden Logan,
      before relying on this — Assumptions / research R16).
- [ ] Register the DGX in-product, save, and observe an **authenticated** verdict; register a wrong
      credential → `auth_failed` naming the host; register an unroutable address → `unreachable`
      within the timeout (SC-001).
- [ ] Multi-line PEM round-trips intact on **web and one native client** (US1 Independent Test).
- [ ] `list_queue`/`job_status`/`host_facts` render as structured workspace components in < 30 s
      (SC-002); a no-grant user can run all read verbs and **zero** mutating verbs (SC-003).
- [ ] `submit_job` to a real partition returns a real Slurm id and a durable record; restart the
      orchestrator and confirm the job is reconciled and still truthfully reportable (SC-009).
- [ ] Each destructive verb produces a proposal and **no effect** on first call (SC-005); approve
      one and confirm it runs exactly once; run the adversarial suite live (SC-004).
- [ ] Host-key mismatch path against a rebuilt/spoofed key → refuse + re-trust flow (FR-020).
- [ ] A Windows target without OpenSSH Server → `unreachable` + prerequisite, not a hang (US5-4).
- [ ] Injection corpus in remote fields (banner, filename, job name, queue reason, process comm)
      → zero tool calls attributable, zero destructive proposals generated (SC-007).

Anything on the second list not yet checked is reported as **code-shaped and unproven** in the PR
and in the CLAUDE.md "Recent Changes" entry, per FR-052 and Constitution XIII.

## Automated test entry points (no machine / no network — SC-015)

- `backend/agents/tests/test_remote_verbs_contract.py` — the FR-051 verb-set/scope/args/
  classification/retry contract.
- `backend/agents/tests/test_remote_transport_gate.py` — egress denylist (loopback/link-local/
  metadata refused; RFC1918 allowed) + host-key policy, against `FakeTransport`.
- `backend/orchestrator/tests/test_remote_confirmation.py` — the durable-proposal adversarial
  suite (US3: single-use, expiry, user-binding, arg-binding, restart durability, parallel/hop
  paths, machine-turn refusal).
- `backend/orchestrator/tests/test_remote_output_containment.py` — bounded typed fields +
  sanitisation + truncation notice (US6).
- Run inside the container: `docker exec astraldeep bash -c "cd /app/backend && python -m pytest -q"`.
