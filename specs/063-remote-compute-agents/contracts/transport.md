# Contract: Remote Transport Boundary

**Feature**: `063-remote-compute-agents` | **Spec**: [../spec.md](../spec.md)

All remote access flows through **one** injectable boundary so the whole capability is
testable with no real machine, no SSH server, and no network (FR-050, SC-015). This document
defines the interface, the production paramiko implementation, the connection-time egress gate,
host-key policy, the login-shell execution technique, and the retry/timeout posture.

## The `RemoteTransport` protocol (the single seam)

```python
class RemoteTransport(Protocol):
    def run(self, target: MachineTarget, argv: list[str], *, timeout: float) -> RemoteResult: ...
    def put_file(self, target: MachineTarget, data: bytes, remote_path: str, *,
                 timeout: float) -> RemoteResult: ...
    def stat(self, target: MachineTarget, remote_path: str, *, timeout: float) -> StatResult: ...
    def probe(self, target: MachineTarget, *, timeout: float) -> ProbeResult: ...
```

- `MachineTarget` is built **server-side** from a `remote_machine` row + decrypted
  `machine_credential` — address, port, username, credential, and the recorded host key. The
  model never supplies any of these fields (FR-018).
- `argv` is a discrete vector (`["squeue", "--me", "--json"]`); the transport wraps it in the
  login-shell `exec "$@"` form (below). No verb ever passes a string.
- `RemoteResult` carries a typed `verdict` from [result-vocabulary.md](result-vocabulary.md),
  plus `stdout_bytes`/`exit_status` for the verb layer to parse into typed fields — the verb
  layer, not the transport, decides what typed fields leave the boundary (FR-038).

**Injection**: constructed via a factory the way the LLM client is
(`llm_config/client_factory.py`) — production wires `ParamikoTransport`; tests wire
`FakeTransport`. Both agents receive the transport through their `_runtime`/constructor, never
importing paramiko directly.

## Login-shell execution + argument safety (proven live 2026-07-24)

`ParamikoTransport.run` issues, over `exec_command`:

```
bash -lc 'exec "$@"' _ <argv[0]> <argv[1]> …        # each token shlex.quote'd for the sshd parse
```

- `-l` (login shell) sources the target's profile so cluster environments resolve their tools
  (on the DGX, Bright env-modules auto-loads `slurm/slurm/23.02.8`; a non-login shell finds no
  Slurm — verified). No scheduler path is hardcoded (FR-009).
- `exec "$@"` hands the positional parameters to the binary as an exact argv vector, so shell
  metacharacters in any argument are **never interpreted** — verified: the argument
  `harmless; echo PWNED; $(whoami)` reached `/bin/echo` verbatim, nothing executed. This is what
  lets FR-022 (no shell-string assembly) coexist with the login-shell requirement.
- The one parse that exists is the SSH exec channel itself; `shlex.quote` per token covers it,
  and `exec "$@"` re-vectorises so a residual quoting gap still cannot execute.

`put_file` uses SFTP (`paramiko.SFTPClient`) and writes **unconditionally** — the destructive
`if_exists` decision for `upload_file` belongs to the confirmation gate (which calls `stat`
first), not the transport (gate, not tool). `stat` uses SFTP `stat` (the read probe behind
`upload_file`'s `if_exists` destructive check); `probe` opens a connection, records the host key
on first contact, and returns reachability/auth. Every SFTP channel carries the verb's declared
timeout (FR-021).

## Connection-time egress gate (FR-019) — the product's first non-HTTP egress

Before any socket is opened, `net_guard.assert_ssh_target_allowed(address, port)`:

1. Resolve **all** A/AAAA records via `_resolve_host_addresses` (reused from
   `shared/external_http.py:81-92` — the anti-rebinding "resolve everything" helper).
2. Refuse if **any** resolved address is **loopback, link-local (incl. the `169.254.169.254`
   metadata address), multicast, unspecified, or reserved**. → `blocked_address` verdict, audited.
3. **Permit RFC1918 private addresses.** The HTTP predicate `_is_private_address`
   (`external_http.py:95-108`) rejects `is_private` and is therefore **not** reused verbatim:
   `dgx.ai.uky.edu` resolves to both `128.163.37.132` (public) and `10.33.77.11` (RFC1918), so
   a blanket private-block would break the real target. The SSH denylist keeps every clause
   **except** `is_private`.
4. Re-resolve at connect time and verify the connected peer address is one of the resolved set
   (a name resolving differently after registration cannot redirect the connection).

RFC1918 is allowed only because four independent controls must all hold before a byte is sent:
target ∈ caller's own inventory (FR-018), address/port from the stored row (FR-018), recorded
host key matches (below, FR-020), and SSH auth succeeds with the user's own credential. The
helpers are promoted into a small `shared/net_guard.py` so neither module imports the other's
underscore-prefixed names.

## Host-key policy (FR-020)

- paramiko `RejectPolicy` (never `AutoAddPolicy`), verifying against the **single key recorded
  on the `remote_machine` row** — not the system `known_hosts`, never shared across users.
- First registration records `host_key_type`/`host_key_fingerprint`/`host_key_blob`.
- A subsequent key mismatch → `host_key_mismatch` verdict, refuse, audit; re-trust is an
  explicit user action (`chrome_machine_retrust`) that overwrites the recorded key deliberately.
  No automatic-accept path exists anywhere.

## Timeout & retry posture (FR-021, FR-036)

- Every call carries the verb's declared timeout; the transport enforces it on connect, auth,
  and command completion, mapping expiry to the `timeout` verdict (a connected-but-hanging
  command is bounded — edge case).
- **Consequential calls never surface a bare `None`/timeout into the dispatch retry path.**
  `_execute_with_retry` (`orchestrator.py:13862`) treats a `None`/timeout as retryable
  (`is_retryable` default `True`, `:13890`) and the 30 s dispatch timeout would produce one. So
  a mutating verb whose transport call times out returns a **structured non-retryable**
  `MCPResponse(error={"retryable": False, …})` carrying the `unconfirmed` verdict — the user is
  told to verify (e.g. re-read the queue), and no second attempt is made (FR-036, SC-010).
- `submit_job` additionally sets a Slurm submit marker (`--comment=<nonce>` / a deterministic
  job-name tag) so a duplicate is detectable cluster-side even in an ambiguous outcome (FR-037).

## Test double

`FakeTransport` (in `backend/agents/tests/`) is an in-memory machine: scripted reachability/
auth outcomes, a virtual filesystem for `stat`/`put_file`, canned Slurm JSON for `run`, and
injectable host-key mismatch/timeout/exhaustion cases. It exercises the egress gate and
host-key policy without a socket. This is what makes SC-015 (full E2E in CI, no network) and the
adversarial US3/US6 suites possible; the real-world proof is the separate live checklist (SC-016).
