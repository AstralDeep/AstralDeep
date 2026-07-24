# Contract: Result Vocabulary

**Feature**: `063-remote-compute-agents` | **Spec**: [../spec.md](../spec.md)

A **fixed** result vocabulary (FR-034). Every verb maps every outcome onto it. No failure is
silent; no generic "something went wrong" is permitted (SC-011). Every failure names the
**machine** it concerns and the **next action** available to the user (FR-035).

## Verdict enum

| Verdict | Meaning | Next action shown to the user |
|---|---|---|
| `ok` | The operation succeeded; typed result attached | — |
| `partial` | Some but not all of a multi-item operation succeeded (e.g. a listing truncated at its bound, or a batch where some items failed) | Review the returned items and truncation notice |
| `unreachable` | No route / connection refused / DNS failure within the timeout | Check the machine is on and reachable from the deployment; verify address/port |
| `auth_failed` | Reached the host; the credential was rejected | Re-check the username and credential for this machine |
| `host_key_mismatch` | The host's key differs from the one recorded at registration | If the machine was legitimately rebuilt, re-trust it deliberately; otherwise do not proceed |
| `credential_not_configured` | No credential stored for this machine | Add a credential for this machine |
| `credential_undecryptable` | A credential exists but can no longer be decrypted (encryption key changed) | Re-enter the credential for this machine |
| `blocked_address` | The resolved address is loopback/link-local/metadata/reserved and refused by the egress gate | Register a routable machine address; this target is not permitted |
| `permission_denied_remote` | The remote OS rejected the operation (e.g. no write permission, sudo required) | Use an account with sufficient rights on the machine |
| `quota_exhausted` | A scheduler allocation/quota or filesystem quota is exhausted | Check your cluster allocation / free space, then retry |
| `mfa_required` | The target demanded an interactive second factor (unsupported, D5) | This machine requires MFA, which is not supported; use a key/password-only account |
| `timeout` | The operation exceeded the verb's declared time bound | Retry later; if it recurs the command may be hanging on the machine |
| `not_found` | The referenced job/path/service/package/process does not exist | Verify the identifier |
| `invalid_argument` | An argument failed its shape guard (e.g. a shell fragment, a non-integer job id) | Correct the argument |
| `confirmation_required` | A destructive verb produced a proposal instead of acting | Approve or decline the proposal |
| `confirmation_expired` | An approval was attempted after the proposal expired | Re-request the operation |
| `unconfirmed` | A consequential verb's outcome could not be confirmed (slow/lost response); **not** retried automatically (FR-036) | Check the queue / machine to see whether it took effect before re-issuing |
| `unattended_refused` | A consequential verb was reached on a turn with no live human principal | Re-issue interactively; only status polling runs unattended (FR-033/FR-044) |

## Rules

- Every `RemoteResult`/`MCPResponse.error` carries a `verdict` from this enum, the `machine`
  label/id it concerns, and a `next_action` string (FR-035). The `Alert`/`Card` rendered to the
  user is built from these, never a raw exception string.
- **Secrets never appear** in a verdict, message, log, notification, or rendered field (FR-049):
  no key bytes, passphrases, or passwords.
- `mfa_required` exists because D5/Assumptions forbid an interactive second factor; a target
  that demands one fails honestly here rather than hanging.
- `unconfirmed` and `unattended_refused` both carry `retryable=False` so the dispatch layer's
  default retry (`orchestrator.py:13893`) cannot duplicate a consequential operation.
- Verbs returning collections use `partial` + an explicit `{shown, total, truncated}` triple
  rather than silently cutting (FR-040).
