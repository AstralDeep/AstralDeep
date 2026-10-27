# sandbox-runtime qualification pilot (opt-in, Linux only)

This directory declares the pinned `@anthropic-ai/sandbox-runtime` (SRT) artifact and the
Linux primitives required by the opt-in qualification runner in
`backend/orchestrator/sandbox_qualification.py`. It qualifies exactly one effect: a
short-lived, harmless local scratch-file write, authorized by a real LETS receipt through
the existing generated-executor verifier (`orchestrator/generated_lets_executor.py`) and
launched through the existing `shared.process_supervision.ProcessSupervisor`. No production
launch path, schema, shared contract, component pin, or release artifact changes.

## What is pinned

See `manifest.json`: SRT `0.0.78`, downloaded at most once per digest from the npm
registry, verified by SHA-512 before extraction, never executed via unpinned `npx`.
Required Linux primitives: node >= 22.12, bubblewrap, socat, ripgrep, unprivileged user
and network namespaces, and the vendored `apply-seccomp` helper (x64/arm64).

Qualified architecture/profile: **ubuntu-24.04 on linux/amd64 or linux/arm64** with the
distribution bubblewrap/socat/ripgrep, unprivileged user + network namespaces permitted,
and no weakening of host AppArmor or user-namespace policy. Every other host — other OS,
other architecture, container or kernel that refuses the namespace probe, missing or
degraded dependency — reports `unavailable` and fails closed **before any effect starts**.
An unavailable or failed pilot is a valid diagnostic outcome with an actionable record.

## Immutable per-invocation profile

Each run writes a fresh session directory (`0700`) holding a `0400` SRT settings file:
empty network allowlist with deny-all (`deniedDomains: ["*"]`, strict allowlist), all
filesystem reads denied at `/` and re-allowed only for the runtime trees, the vendored
seccomp helper, and the session's effect script, spec file, and scratch directory; writes
allowed only for the scratch directory, with SRT's unconditional `/tmp/claude` write
convenience explicitly denied; the environment passed to the wrapper is a fixed minimal
set (no HOME, no inherited secrets, no inherited sockets — the child gets only
stdin/stdout/stderr pipes). The profile is structurally re-validated in code before every
run (`validate_srt_settings`), and upstream warnings that would reduce the required
profile (seccomp degradation) are hard failures — the runner never falls back to an
unsandboxed process.

## Running

```bash
python -m orchestrator.sandbox_qualification --probe-only
python -m orchestrator.sandbox_qualification --permit-file permit.json \
    --allow-artifact-download --record-log qualification-log.jsonl
```

`--probe-only` prints a support record (exit 4 when unsupported). The full mode needs the
standard LETS executor environment (or `--permit-file` with one operator-minted permit)
and performs one authorized scratch-write effect. Exit codes: 0 passed, 2 usage, 3 failed,
4 unavailable. Records contain digests, check codes, and counters only — never effect
content, secret material, or receipt signatures — and can be appended to a hash-chained
log with `--record-log`.

## Check semantics

Every child outcome is verified from the parent side, independently of the child's exit
text: the authorized output file's digest, absence of secret material in the scratch tree
and captured output, absence of out-of-scratch files, zero accepted connections on
parent-held loopback TCP and Unix-socket listeners, unchanged symlink targets, bounded
output rings, deadline enforcement with descendant cleanup, and a stderr/stdout scan for
upstream degradation markers. Denial probes attempt a secret read, out-of-scratch writes,
direct-IP and loopback connects (TEST-NET/RFC1918/link-local plus a parent listener), Unix
socket connect/bind, and a symlink escape — each with its own verified receipt, each
expected to be stopped by the sandbox. No live third-party network is required.

Out of scope for this pilot: SRT control-fd updates, long-lived draft agents, interactive
terminals, Windows/macOS, nested weak container mode, AppleEvents, and provider egress.
