# Contract: General Cresco Bridge — Bounded Integration Contract

**Feature**: 095-cresco-bridge-contract | [spec.md](../spec.md)
**Status**: Bounded topology is implemented behind `FF_CRESCO` (default off). Generic work dispatch and bounded executor results are unavailable. Real-fabric acceptance is pending.
**Authority**: `.specify/memory/constitution.md`, Principles VII and V. This contract supersedes stale assumptions in spec 050 without rewriting its evaluation history.

The original document-only ratification preceded the runtime child. Current conformance executes the present bridge's admission and wire behavior; it no longer asserts that the bridge package is absent. Neither source review nor the synthetic TLS fixture establishes a deployed fabric or completed dispatch.

## 1. Scope and activation

The bridge is a first-party Python agent over the external fabric's wsapi seam. `FF_CRESCO` remains fail-closed and default off. Static discovery never includes the bridge; when the flag is off, in-process registration and standalone/subprocess start exclude the bridge and attempted tool execution is refused before dialing. The public first-party identity is declared but is never automatically marked safe. Normal owner, permission, scope, confirmation, security-flag, RFC 8693, PHI/taint, egress and hash-chained audit gates remain in force.

The GaiaKeep integration is an independent specialized archival path. It retains its own `FF_GAIAKEEP` AND `FF_CRESCO` boundary and caller-owned SSH tunnel. It does not establish general topology or work-dispatch acceptance.

## 2. Verified control profile

The reviewed control sources are bound by commit, path and SHA-256 in [wsapi-wire-profile.json](wsapi-wire-profile.json). They were inspected as data, without executing upstream code. The profile is source-verified; deployment compatibility remains unverified.

- **Endpoint**: `wss://<host>:8282/api/apisocket`. A configured base address receives that control path. `/api/dataplane`, `/api/logstreamer`, other paths, query/fragment and embedded URI credentials are refused before credential delivery.
- **Authentication**: the runtime-only `cresco_service_key` HTTP upgrade header. Fabric possession confers no Astral identity, role, scope or mesh admission.
- **Requests**: JSON with `message_info` and `message_payload`; every value in those two maps is a string. `message_type` is `global_controller_msgevent` or `global_agent_msgevent`; `message_event_type` is `EXEC` or `CONFIG`; `is_rpc` is the string `"true"`. A client-generated `client_rpc_id` is carried in the payload.
- **Replies**: plain JSON response params with string values. They have no response envelope. The controller echoes `client_rpc_id`; only the matching reply can satisfy the call. Stray correlated replies are discarded within the bounded result window.
- **Regions**: global controller `EXEC`, action `listregions`, compressed `regionslist` containing `regions` records whose identifier is `name`.
- **Agents**: global controller `EXEC`, action `listagents`, optional `action_region`, compressed `agentslist` containing `region_id`/`agent_id` records.
- **Agent information**: global agent `CONFIG`, action `getagentinfo`, `dst_region`/`dst_agent` in message info. Its verified `agent-data` result is the data-directory path, not a fabricated plugin inventory or running-state record.
- **System information**: global controller `EXEC`, action `resourceinfo`, `action_region`/`action_agent`, compressed `resourceinfo` containing `agentresourceinfo` and its serialized `perf` object. There is no invented `getsysinfo` wire verb.
- **Canonical fixture encoding**: `json_bytes = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')`; write those bytes once to `gzip.GzipFile(filename='', mode='wb', fileobj=io.BytesIO(), compresslevel=9, mtime=0)`, close the gzip member, then base64-encode the complete buffer. The fixed ten-byte header is `1f8b08000000000002ff`: DEFLATE, no optional fields, zero timestamp, maximum-compression hint, OS unknown (`255`). `GzipFile` controls that header independently of the host; `gzip.compress(..., mtime=0)` does not on production Python 3.11. The golden binds the complete compressed bytes and exact JSON round-trip; a compressor change requires explicit fixture re-verification.

- **Compression**: base64(gzip(canonical JSON)), implemented with `gzip.GzipFile(compresslevel=9, mtime=0)`. The platform-independent gzip header uses OS byte 255. Incoming frames are capped at 2 MiB and expansion at 4 MiB before JSON materialization; invalid UTF-8, malformed bulk or unexpected result shapes are refused.
- **Egress**: `shared/external_http.py::validate_egress_url` validates the configured dial host in its HTTPS form. Private-host allowance is scoped to that exact configured host; no global verification bypass is permitted.
- **TLS**: CA-chain and hostname verification are mandatory. An operator may add a trusted CA bundle and a pinned SHA-256 certificate fingerprint. A pin adds a requirement; it never replaces CA/hostname verification. The pin is checked during TLS wrapping before WebSocket HTTP headers expose the service key. `CERT_NONE` is prohibited.
- **Lifetimes**: bounded connect/RPC budgets and stdlib backoff. Dial retries apply only before an operation is sent; no operation is automatically replayed after send.

## 3. Bounded topology and targets

Read tools expose bounded region/agent listings, agent data location and system information. Lists and sysinfo are capped at 50 records/fields; output follows the normal taint/PHI/render path through astralprims. Topology never adds approved targets, agent cards, routes, identities or permissions. Log/metric streams and file operations remain unimplemented.

Targets retain the exact `region_agent[_plugin]` grammar: two or three lowercase components matching `[a-z0-9][a-z0-9-]{0,62}`, total length at most 192, without wildcards, empty components, whitespace, paths or URI syntax. Every named target must appear in `CRESCO_APPROVED_TARGETS` before dialing. The full approved tuple is retained in audit even where a read addresses its node. Fabric discovery cannot widen that set.

## 4. Effect disposition

| Surface | Scope | Required gates | Current disposition |
|---|---|---|---|
| Bounded topology | `tools:read` | normal owner/per-user permission and PHI/taint gates | implemented; real-fabric acceptance pending |
| Generic work dispatch | `tools:write` | target admission, confirmation, unattended-channel denial, audit | unavailable; no verified generic work/result contract |
| Executor wrapper | `tools:system` | hard security flag, default deny, operator switch, explicit permission, confirmation, audit | unavailable; bounded result transport not qualified |
| File put/get | `tools:write` | normal owner/per-user gates | unimplemented |

No reviewed controller, wsapi or Python caller source exposes `submitwork`. The executor plugin supports `config_process`/`run_process`; its stdout/stderr use `/api/dataplane`, outside this contract. A control acknowledgement that a runner started is not proof of execution completion. The bridge therefore refuses generic dispatch and executor effects before any dial and never reports synthetic work completion. Their existing denial/confirmation/security/audit gates remain, without admitting an alternative shell or credential path.

## 5. Faults and credential boundaries

The closed states remain `unavailable`, `unreachable`, `auth`, `protocol`, `tls`. Only transport unreachability is retryable; unavailable operations, authentication, protocol drift and TLS/pin failures are not. Public faults use bounded fixed diagnostics, never raw exception text. Service-key bytes are scrubbed from both untrusted keys and values before results or UI rendering; audit carries identifiers and dispositions, never work text or credentials. A persisted hash-chained intent is required before any handler can dial. Results are withheld if their audit disposition does not persist; the fixed `audit_unavailable` refusal is non-retryable. Missing recorders, queued inserts and insert exceptions do not permit a fabric read. Local refusals remain typed even when audit storage is unavailable. Public feedback distinguishes unavailable effect transport (`CRESCO_OPERATION_UNAVAILABLE`) and audit storage (`CRESCO_AUDIT_UNAVAILABLE`) from missing fabric configuration.

`CRESCO_SERVICE_KEY` is an operator-local runtime secret. It opens only an outbound authenticated fabric connection and never grants Astral permissions or inbound admission. No credential value is committed or published. Sharing the variable with GaiaKeep does not couple their activation.

## 6. Unsupported operations and acceptance limits

The Cresco JVM/OSGi/ActiveMQ fabric remains external; it is never embedded, used as an internal bus, or substituted for A2A/WebSocket agent transport. Fabric-initiated inbound connections, automatic member trust, arbitrary dialect fallback, weakened TLS, dataplane/log streaming, generic work and unqualified executor completion are unsupported. Shipping code never activates the bridge by default.

Synthetic loopback topology tests establish adapter behavior and denial boundaries. Real operator-approved multi-node fabric acceptance is still required. A future effect implementation must supply an actual supported request/result contract and pass all existing owner, permission, confirmation, security, PHI/taint, audit and egress gates; this preparation neither invents that contract nor authorizes activation.

## 7. Source traceability

Full audited file hashes are retained in the wire profile. Current source-only verification:

| Source | Commit | Status | Retrieved |
|---|---|---|---|
| Cresco wsapi seam | `8d57820c58f2290b272e7805508c3e39ae015df4` | source-verified | 2026-10-13 |
| pycrescolib control callers | `8813399c48d8c6a3c8965fe52b1f707d2ef9f8a1` | source-verified, not executed/adopted | 2026-10-13 |
| CrescoEdge/controller | `02ffc9327b94196c2e17e5c5542ff0f92412db61` | source-verified | 2026-10-13 |
| CPython gzip | [CPython v3.11.15 `Lib/gzip.py`](https://github.com/python/cpython/blob/v3.11.15/Lib/gzip.py) and [Python 3.11 gzip documentation](https://docs.python.org/3.11/library/gzip.html) | pinned source, official documentation verified | 2026-10-14 |
| Cresco executor plugin | `8e9020e45d23a863c8eb3ba555e6fe9e02c3457d` | source-verified, result transport unsupported | 2026-10-13 |

Historical pins remain explicit: CrescoEdge/agent `4093d7d` and CrescoEdge/library `826df9c` are pinned-unreverified from 2026-07-06; the pycrescolib PyPI 1.3.0 evaluation is historical, not current adoption. GaiaKeep SDK `9f30ebaeebdfdf1f44a2980d62ab0908d809e7a9` remains pinned by its owning requirements. AstralPlane dependency issue #67 remains recorded as open-external/unverified here; this does not stand in for owner qualification.

## 8. Executable conformance

The [conformance cases](conformance-cases.json) bind every case to `backend/tests/test_cresco_contract_095.py`; protocol/TLS loopback denial regressions additionally run in `backend/agents/cresco/tests/`.

- **C-01** default-off feature flag.
- **C-02** present runtime's flag-off kill switch refuses before dialing.
- **C-03** real in-process registration excludes the bridge when off and includes it only when enabled.
- **C-04** declared public identity is never automatically marked safe.
- **C-05** GaiaKeep dual-flag boundary.
- **C-06** dual-flag truth table and flag-read failure denial.
- **C-07** real string-valued request, flat correlated reply and public deterministic bulk codec.
- **C-08** runtime verified-TLS, exact control endpoint and private-egress denial.
- **C-09** actual target parser and approved-target admission, including refusals.
- **C-10** closed fault-state set and bounded non-secret dispositions.
- **C-11** no committed credential literals.
- **C-12** no embedded fabric/broker artifacts in manifests.
- **C-13** source identifiers, statuses, dates and hashed reviewed files; historical unreverified pins remain labeled.
