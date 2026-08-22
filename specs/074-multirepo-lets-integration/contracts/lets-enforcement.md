# Contract: AstralDeep to LETS Enforcement

**Contract version**: `astral.lets-enforcement/v1`

**Initial LETS baseline**: immutable `v1.0.10` (historical evidence only)
**Current LETS pin**: immutable `v1.0.11` (`lets-agent[client]==1.0.11`), with its exact signed release anchor recorded in [`../execution/lets-v1.0.11-release-anchor.json`](../execution/lets-v1.0.11-release-anchor.json)
**Receipt wire type**: `lets.receipt/v1`

## Purpose and authority ordering

LETS adds finite, lineage-bound, state-machine authority to an effect that Astral has already allowed. It does not authenticate the user, replace RFC 8693 delegation, choose the agent, map ownership, approve PHI/egress, satisfy confirmation, or widen a permission.

For a governed effect, the mandatory order is:

```text
Astral identity/delegation
-> owner isolation
-> permission/policy/security/taint/PHI/egress/confirmation gates
-> final argument and credential preparation
-> durable operation record
-> LETS authorization
-> host binding checks
-> executor receipt verify-and-claim
-> physical effect
-> correlated Astral + LETS evidence
```

Any deny or indeterminate state before the physical effect is a deny. A valid LETS receipt cannot override an Astral denial.

## Deployment boundary

- LETS warden runs independently over authenticated HTTPS.
- AstralDeep installs only LETS public client, integration, wire-model, and executor-verifier code from the pinned component.
- LETS does not import AstralDeep and does not read Astral tables.
- AstralDeep does not read LETS storage or import LETS core service internals.
- AstralPlane persists neutral binding/operation/claim records; it does not decide enforcement policy.
- Warden URL is operator-controlled, prevalidated, and never supplied by an end user or agent.
- Redirects are disabled. Production TLS verification is mandatory. Response size and total wall-clock time are bounded.

The relevant public LETS HTTP operations are:

| Purpose | Operation |
|---|---|
| Discover compatible service | `GET /v1/info` |
| Issue governed root | `POST /v1/roots` |
| Spawn runtime/replica lease | `POST /v1/leases/{parent}/children` |
| Authorize effect | `POST /v1/leases/{lease}/transitions` |
| Renew | `POST /v1/leases/{lease}/renew` |
| Pause/resume/close | `POST /v1/leases/{lease}/{quiesce|resume|close}` |
| Reconcile snapshot | `GET /v1/leases/{lease}` |
| Revoke lineage branch | `POST /v1/branches/{lease}/revoke` |

Every mutating request uses a stable, durable `request_id`. The same ID may be retried only with byte-equivalent canonical semantics.

## Configuration

Configuration names are part of the host contract:

| Setting | Meaning | Production rule |
|---|---|---|
| `FF_LETS_EXTERNAL_WARDEN` | Master availability flag | Defaults `false` |
| `LETS_MODE` | `off`, `shadow`, or `enforce` | Invalid value fails startup |
| `LETS_GOVERNED_COHORTS` | Ordered allowlist of agent populations | Initial value contains only `server_dynamic,byo_user` |
| `LETS_GOVERNED_AGENT_ALLOWLIST` | Optional narrower agent IDs | Empty means cohort rule only |
| `LETS_WARDEN_URL` | Canonical HTTPS origin | No credentials, query, fragment, redirect, or insecure production scheme |
| `LETS_SERVICE_TOKEN_FILE` | Bearer token secret path | Runtime secret file; never logged |
| `LETS_CA_BUNDLE` | Private CA path if required | Runtime mount; default system trust otherwise |
| `LETS_CLIENT_CERT_FILE`, `LETS_CLIENT_KEY_FILE` | Optional mTLS identity | Both or neither; runtime secrets |
| `LETS_TENANT_ID` | Exact tenant | Canonical LETS identifier |
| `LETS_ENVELOPE_ID` | Exact envelope | Canonical LETS identifier |
| `LETS_POLICY_DIGEST` | Accepted policy | Must match authenticated trust manifest |
| `LETS_MACHINE_DIGEST` | Accepted state machine | Must match authenticated trust manifest |
| `LETS_DEFAULT_ALLOCATION` | Six positive/non-negative resource dimensions | One dimension per scope |
| `LETS_DEFAULT_TTL_SECONDS` | Lease lifetime | Positive and policy-bounded |
| `LETS_REQUEST_TIMEOUT_SECONDS` | Total request deadline | Positive and bounded |
| `LETS_REQUEST_ATTEMPTS` | Idempotent retry attempts | Small bounded integer |
| `LETS_SIGNED_TRUST_MANIFEST` | Warden keys and exact identity fences | Authenticated local/runtime secret mount |
| `LETS_EXECUTOR_INSTANCE_ID` | Stable gateway instance | Canonical identifier |
| `LETS_EXECUTOR_DB_ROOT` | Replay-store root | Persistent, outside code/submodules |
| `LETS_EXECUTOR_AUTHORITY_ROOT` | Independent rollback/freshness authority | Required in production |

Mode behavior:

| Mode | Lifecycle calls | Effect authorization | Physical effect |
|---|---|---|---|
| `off` | None | None | Current Astral behavior, with no fabricated LETS success |
| `shadow` | Performed and recorded | Performed and would-deny recorded | Existing Astral decision remains operative |
| `enforce` | Required for governed populations | Required | Executes only after valid claim |

If the master flag is false, only `off` is valid. `shadow` is evaluation only and its evidence must never be labeled protected-executor enforcement.

Trust keys are not accepted merely because `/v1/keys` returned them. Production keys and warden identity come from the authenticated trust manifest, with explicit rotation overlap and expiry.

## Fixed six-scope profile

The mapping is reviewed source, not arbitrary environment configuration:

| Astral scope | LETS capability | LETS transition | Resource dimension |
|---|---|---|---|
| `tools:read` | `astral.tools.read` | `tool_read` | 0 |
| `tools:write` | `astral.tools.write` | `tool_write` | 1 |
| `tools:search` | `astral.tools.search` | `tool_search` | 2 |
| `tools:system` | `astral.tools.system` | `tool_system` | 3 |
| `tools:files` | `astral.tools.files` | `tool_files` | 4 |
| `tools:execute` | `astral.tools.execute` | `tool_execute` | 5 |

The initial LETS policy uses six resource dimensions and charges one unit in the matching dimension for a `ready -> ready` transition. An unknown scope, unknown tool classification, missing transition, incomplete allocation, ambiguous audience, or unrecognized policy/machine digest is a hard deny in enforce mode.

## Agent population and lifecycle

Initial governed populations:

- `server_dynamic`: server-generated/draft/personal agent runtimes whose gateway Astral controls.
- `byo_user`: user-authored agent runtimes only after that runtime version contains the conforming receipt verifier and persistent replay store.

Later populations (`external`, `builtin`) require an explicit composition/profile revision and evidence. An external agent that can reach its actuator outside Astral can be described only as dispatch-mediated until its own gateway/sidecar passes conformance.

Lifecycle mapping:

| Astral event | LETS action | Local state requirement |
|---|---|---|
| Governed agent/revision admitted | `issue_root` | Pending root operation committed first |
| Concrete runtime/replica created | `spawn` | Parent binding active; child runtime identity fenced |
| Exact quiesced runtime reconnects | `resume` | Host generation and lease match |
| Pause, disconnect, or host loss | `quiesce` | No new governed dispatch after intent commits |
| Lease approaches expiry | `renew` | Expected sequence/version match |
| Runtime retirement or revision supersession | `close` | Drain current operations first |
| Deletion, compromise, or ownership violation | `revoke_branch` | Root/branch intent is durable and fail-closed |
| Policy/config epoch change | quiesce, drain, new binding | Old receipts never migrate silently |

A call to an already-existing callee is not automatically a LETS replica. `spawn` is used only when Astral creates a distinct runtime/subtask identity.

## Protected effect request

After all Astral gates and argument rewrites:

1. Allocate/persist `operation_id` and a random nonce with at least 128 bits of entropy.
2. Canonicalize security-relevant effect context and compute `effect_digest`. Raw arguments, credentials, PHI, and user content are not sent to LETS.
3. Call the public AstralDeep authorizer with:

```json
{
  "request_id": "<operation_id>",
  "lease_id": "<runtime_lease>",
  "transition": "tool_<scope>",
  "executor_audience": "<exact_gateway>",
  "nonce": "<unique_nonce>",
  "expected_sequence": 42,
  "evidence": {
    "type": "astral.tool-effect/v1",
    "operation_id": "<operation_id>",
    "agent_id": "<governed_agent>",
    "runtime_id": "<fenced_runtime>",
    "tool_id": "<canonical_tool>",
    "scope": "tools:read",
    "effect_sha256": "<canonical_digest>",
    "channel": "rest|websocket|a2a|mcp|background|scheduled|chained|stream",
    "audit_correlation_id": "<astral_audit_id>"
  }
}
```

The response is strictly parsed as `Receipt`; unknown or malformed success data is a denial.

## Permit transport

The signed receipt is carried outside tool arguments. MCP may use a namespaced caller-capability extension such as `astraldeep.lets/v1`; A2A/WebSocket/internal calls use an equivalent typed envelope. Agents cannot edit the permit into a different request, and ordinary tool schemas never expose it as an agent-selectable argument.

## Final gateway checks

Immediately before the physical effect, the Astral-controlled gateway recomputes the canonical effect context and requires exact equality for:

- receipt type, request/operation ID, and receipt ID;
- owner-isolated binding, tenant, envelope, warden, lease, lineage, and subject;
- policy digest, machine digest, and configuration epoch;
- agent/runtime generation and current binding state;
- tool classification, scope-derived transition, cost dimension, and audience;
- nonce, evidence digest, and wire-argument/effect digest;
- expected/resulting sequence and accepted clock bounds.

It then calls LETS `ReceiptVerifier.verify_and_claim()`, whose policy additionally checks trusted key/signature, issuer, freshness, accepted digests, executor identity, durable replay uniqueness, and sequence watermark. The effect starts only after claim commits.

For in-process tools this is immediately before `tool_fn(**arguments)`. Generated/BYO runtimes include the verifier at their actuator. A remote agent without a conforming actuator cannot claim final-boundary enforcement.

## Dispatch coverage

Every invocation channel constructs a `ProtectedDispatchContext` and reaches one lower gateway that refuses a governed call without a valid permit. Coverage includes:

- normal single, parallel, chained, and recursive calls;
- REST and WebSocket requests;
- MCP inbound/outbound tool calls;
- A2A orchestrator and individual-agent executors;
- background and scheduled work;
- component re-execution;
- credential probes;
- polling and push streams;
- remote, in-process, first-party, external, generated, and BYO runtime paths.

A bounded stream-open receipt authorizes only opening that stream. Each poll or later actuator action requires the contractually defined new authorization or renewal; a stream permit is not an unlimited session capability.

## Concurrency, retries, and crash semantics

- Receipt sequences are monotonic per `(warden, lease, audience)`. The first rollout serializes authorization through claim per runtime binding.
- If a remote runtime cannot acknowledge claim separately, the binding lock remains held through the call. A later protocol may add a claim acknowledgement.
- Every physical retry receives a distinct durable operation ID, nonce, and receipt. The current behavior of wrapping several physical attempts in one upstream authorization is not valid under enforcement.
- Transport retries before a known response reuse the exact same request ID and canonical request. LETS idempotency returns the same committed result or rejects a fingerprint conflict.
- A crash after claim but before effect can omit the effect. LETS provides at-most-once authority consumption, not generic exactly-once effects.
- A lost result after a non-idempotent effect is `outcome_uncertain`; it is reconciled or compensated, never blindly rerun.
- Where possible, a domain idempotency key or same-transaction claim/effect eliminates ambiguity.

## Failure matrix

| Condition | `off` | `shadow` | `enforce` |
|---|---|---|---|
| Missing/invalid LETS startup config | Ignore LETS, current behavior | Unhealthy shadow diagnostic, current effects | Readiness/governed dispatch closed |
| Timeout/transport/429/502/503/504 | No LETS call | Record would-deny after bounded same-ID retries | Deny after bounded same-ID retries |
| 401/403, fingerprint conflict, invalid policy/schema | No LETS call | Record terminal misconfiguration | Terminal deny; operator diagnostic |
| Missing/non-active binding | No LETS call | Record would-deny and reconcile | Deny and reconcile |
| Exhausted/expired/quiesced/closed/revoked | No LETS call | Record would-deny | Deny; never fall back |
| Malformed/tampered/wrong-bound/stale/replayed receipt | No LETS call | Record verifier failure | Final gateway denies before effect |
| Warden committed, Astral lost response | No LETS call | Reconcile same request ID | Reconcile same request ID; no effect until local commit |
| Receipt claimed, effect result lost | Current tool semantics | Record uncertainty | No blind non-idempotent retry |
| Replay store/authority anchor unavailable or full | No LETS call | Shadow unhealthy | Deny before effect |
| Clock uncertainty exceeds policy | No LETS call | Shadow would-deny | Deny before effect |

Error output is typed and redacted. It may contain stable operation/correlation IDs, reason codes, and component health; it never logs tokens, certificates, raw receipts where policy forbids them, raw arguments, PHI, or credentials.

## Key and policy rotation

- The signed trust manifest identifies accepted wardens/keys, policy, machine, tenant, envelope, config epoch, validity, and executor identity.
- Rotation overlap lasts at least maximum receipt TTL plus accepted clock uncertainty.
- Configuration-epoch change quiesces/drains old operations and provisions a new binding generation.
- Old replay stores and authority anchors remain available for their evidence/retention period; a rollback may not restore an older receipt watermark as authoritative.

## Required conformance tests

1. Flag-off byte/behavior parity and no fabricated LETS evidence.
2. Shadow versus enforce behavior and secret-redaction/startup failures.
3. Repeat-safe Plane migration and owner-scoped binding uniqueness.
4. All six scope mappings; unknown/missing/changed mapping denials.
5. Same ID/same payload retry and same ID/different payload conflict.
6. Lifecycle converge/restart/quiesce/resume/renew/close/revoke/exhaustion/epoch rotation.
7. Every dispatch channel listed above.
8. Tampering or mismatch of every receipt and host-binding field.
9. Replay, stale restore, cloned/missing/broken replay authority, clock rollback, disk-full/recovery.
10. Concurrent authorization proving no out-of-order physical execution.
11. Non-idempotent claim/effect crash and lost-result recovery.
12. End-to-end invariant: zero governed physical effects without one successfully claimed matching receipt.

## Versioning rule

The required successor was released as immutable v1.0.11 and is now the current runtime comparison anchor. Retained v1.0.10 bytes remain historical records bound to the prior tool, schema, and scanner revisions that produced them; the current validator does not relabel them as v1.0.11 evidence. Any later LETS runtime defect fix or required wire/semantic change receives another successor release, updates the component pin, invalidates affected current evidence, and is named accurately in the paper.
