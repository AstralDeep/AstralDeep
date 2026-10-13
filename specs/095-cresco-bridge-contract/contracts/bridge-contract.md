# Contract: General Cresco Bridge — Bounded Integration Contract

**Feature**: 095-cresco-bridge-contract | [spec.md](../spec.md)
**Status**: Ratified and bounded (2026-10-13). The bridge agent and its wsapi client are **not implemented**; nothing in this contract asserts runtime completion.
**Governing authority**: `.specify/memory/constitution.md` v6.2.0, Principle VII (Cresco clause) and Principle V. Where spec 050 disagrees with this contract, this contract wins.

This contract bounds any future general Cresco bridge in AstralDeep: what it may speak (`wsapi` profile), what it may see (bounded topology), what it may do (risk tiers), where it may act (member-target mapping), how it fails (fault states), and what it may never do (unsupported matrix). It is enforced by deterministic conformance cases ([`conformance-cases.json`](conformance-cases.json), executed by `backend/tests/test_cresco_contract_095.py`) that fail closed on repository drift.

## 1. Scope and disposition

- **In scope**: one first-party Python bridge agent, in-process, over the Cresco fabric's `wsapi` WebSocket seam, behind `FF_CRESCO` (fail-closed, default off), inheriting every existing AstralDeep gate (registration, per-tool scopes, security flags, safe-marking, RFC 8693 delegation, PHI/taint, egress validation, hash-chained audit).
- **Not implemented**: the bridge agent, its wsapi client, its tools, and its registration do not exist in this snapshot. This contract is a specification, not a runtime claim.
- **Relationship to GaiaKeep (spec 092)**: the merged GaiaKeep integration is a specialized, Cresco-gated archival path — a caller-owned DGX SSH tunnel to a GaiaKeep gateway carrying the `cresco_service_key` header. It is not a topology/discovery/dispatch bridge, does not satisfy this contract by existing, and its flag posture is independent of bridge activation.

## 2. wsapi profile

Shape-pinned from the 2026-07-06 evaluation (`wsapi` @ `d5b4bcd`, cross-read from `pycrescolib` 1.3.0). The wire format is implementation-defined upstream and upstream is under active development: **byte-level re-verification against the then-current upstream is a hard precondition for implementation.**

- **Endpoint**: `wss://<host>:8282` with paths `/api/apisocket` (control/RPC — the only path in the general bridge's initial scope), `/api/dataplane` and `/api/logstreamer` (out of scope; §8).
- **Authentication**: a `cresco_service_key` HTTP header on the WebSocket upgrade. One all-or-nothing fabric credential; the fabric performs no per-caller scoping (§7).
- **Envelope**: JSON `{"message_info": {…}, "message_payload": {"action": <verb>, …}}`; `message_info` carries message type, event type, RPC flag, and the RPC correlation id. Bulk parameters travel `base64(gzip(json_bytes))` inside the payload. The canonical encoding rule pinned for fixtures and tests: `base64(gzip(json_bytes))` where `json_bytes = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()` and gzip uses `compresslevel=9, mtime=0` (deterministic).
- **Egress**: before dialing, the configured wsapi host MUST be validated through `shared/external_http.py::validate_egress_url` in its http(s) form (`https://<host>:<port>`; the validator admits only http/https schemes, and `wss` is the TLS form of the same dial target). Private/on-prem hosts are permitted only via the validator's host-scoped private-host allowance for the configured host — never a global bypass.
- **TLS**: verified by default (system trust), with operator opt-in to a trusted CA bundle or a pinned SHA-256 certificate fingerprint. `CERT_NONE` and global verification bypass are prohibited in every mode. A self-signed certificate is refused unless trust is explicitly configured.
- **Timeouts and reconnect**: bounded connect and RPC timeouts; reconnect/backoff via stdlib only.
- **Drift**: a frame that does not match the pinned envelope fails safe with a diagnostic (maps to the `protocol` fault state) — never mis-parsed, never partially applied.

Conformance cases: C-07 (envelope shape + deterministic bulk-param encoding), C-08 (egress/TLS posture is contract text only in this snapshot — no client exists to execute; recorded as unexecuted-by-necessity, see §9).

## 3. Bounded topology

The bridge's read surface is bounded to:

- `listregions` — regions known to the configured fabric, rendered as a bounded table;
- `listagents` (optionally per region) — agents per region with status;
- per-agent info and host sysinfo for a named admitted target;
- bounded, capped log/metric window reads for a named admitted target (truncation noted; never an unbounded stream).

Discovery is read-only and produces no routes, no agent cards, no automatic registration of fabric members into AstralDeep, and no advertisement of AstralDeep into the fabric. Topology data is untrusted fabric content: it flows the normal taint/PHI/render path and is never executed or linked.

## 4. Risk tiers

| Tier | Scope | Surface | Gate |
|---|---|---|---|
| Read | `tools:read` | topology/telemetry reads (§3) | standard per-user permission gate |
| Write | `tools:write` | file put/get through the fabric filerepo | standard per-user permission gate |
| System | `tools:system` | the `executor` wrapper only | default-deny; hard `tool_security` flag; explicit per-user override; never enabled by the safe-agent baseline; every attempt (allow and deny) audited |

The executor wraps an arbitrary-shell remote execution surface (executor @ `8e9020e`); it is the most tightly gated tool in the feature and may never be relaxed below the System row above.

## 5. Member-target mapping

A member target identifies where an action is directed: `region_agent[_plugin]` — region and agent components required, plugin optional.

- **Grammar (bounded)**: lowercase components separated by `_`; each component matches `[a-z0-9][a-z0-9-]{0,62}`; exactly two or three components; total length ≤ 192; no wildcards, no empty components, no whitespace, no path separators, no scheme or authority syntax.
- **Admission**: a target is actionable only if the operator configured the fabric and the target lies within it; the bridge does not discover-and-trust targets on its own. The service key authenticates the bridge to the fabric and grants **no Astral role, scope, or mesh admission** (§7). A member target never confers, maps to, or implies any Astral permission.
- **Attribution**: every audited bridge call records the full `region_agent[_plugin]` tuple of the target it acted on.

Conformance cases: C-09 (grammar/admission examples), C-10 (fault-state table), C-11 (credential boundaries).

## 6. Fault states

Closed set; every bridge-visible failure maps to exactly one. Anything unmapped fails safe into `protocol`.

| State | Trigger | User-legible result | Retryable |
|---|---|---|---|
| `unavailable` | `CRESCO_WSAPI_URL` or `CRESCO_SERVICE_KEY` unset | "Cresco fabric not configured" | no |
| `unreachable` | dial failure, RPC timeout, socket error | "Cresco fabric unreachable" | yes |
| `auth` | fabric rejects the service key | "Cresco authentication failed" | no |
| `protocol` | frame drift / undecodable / unmapped failure | "Unexpected fabric response" | no |
| `tls` | certificate verification or pin failure | "Cresco TLS verification failed" | no |

Fault results never contain credential material, never contain raw exception text, and never expose the configured host beyond what the egress posture already permits. Flag-off (`FF_CRESCO` off) is not a fault state: with the flag off the bridge does not exist — no tool, no registration, no route.

## 7. Credential and admission boundaries

- `CRESCO_SERVICE_KEY` is an operator-local runtime secret: configured via environment, never committed, never logged, never echoed into errors, results, audit rows, or UI payloads. It is not documented in `.env.example`; operator enablement documentation lives with the future implementation's quickstart, and the variable name (not value) is the only permitted reference in committed text.
- The key authenticates the bridge **to** the fabric. It confers no Astral role or scope, adds no mesh admission, registers no agent, and opens no inbound path. The fabric admits nothing into AstralDeep: the bridge dials out only, through egress validation, with verified TLS.
- The GaiaKeep path shares the same variable as its tunnel credential under its own dual-flag boundary (`FF_GAIAKEEP` AND `FF_CRESCO`); sharing the variable does not couple the two integrations' activation.

## 8. Supported / unsupported matrix

**Supported** (when the future bridge is implemented and the flag is operator-enabled): bounded read-only topology and telemetry (§3); audited file put/get at write scope; the executor wrapper under the full System-tier gate; typed fault states (§6); reconnect with bounded stdlib backoff.

**Unsupported — permanently or until a new constitution-sanctioned decision**:

- Embedding the Cresco JVM/OSGi runtime, ActiveMQ, or any broker in the product image; using the fabric as an internal message bus; replacing or extending the A2A/WebSocket agent protocol with fabric semantics.
- Fabric-initiated inbound connections to AstralDeep; exposing an AstralDeep agent card to the fabric.
- Enabling the executor wrapper by scope alone, by the safe-agent baseline, or by any flag other than an explicit per-user override.
- Unbounded log/metric streaming; `/api/dataplane` and `/api/logstreamer` subscriptions in the initial scope.
- Treating topology data as trusted content, or mapping fabric members into AstralDeep agents/identities.
- Disabling TLS verification (globally or per dial) in any mode.
- Granting any Astral permission, scope, or admission from possession of `CRESCO_SERVICE_KEY`.
- Activating any of this by shipping: this snapshot contains no bridge runtime, and `FF_CRESCO`'s default remains off.

## 9. Authoritative sources

Pin discipline per Constitution XIII: identifier, status, retrieval date. External re-verification status is explicit; a pin not re-verified in this snapshot is a precondition debt for implementation, not a claim of currency.

| Source | Identifier | Status | Retrieved |
|---|---|---|---|
| CrescoEdge/agent | git @ `4093d7d` | pinned-unreverified | 2026-07-06 |
| CrescoEdge/controller | git @ `1b3cc3c` | pinned-unreverified | 2026-07-06 |
| CrescoEdge/library (docs) | git @ `826df9c` | pinned-unreverified | 2026-07-06 |
| Cresco wsapi seam | `wsapi` git @ `d5b4bcd` | pinned-unreverified | 2026-07-06 |
| Cresco executor plugin | `executor` git @ `8e9020e` | pinned-unreverified | 2026-07-06 |
| pycrescolib | PyPI 1.3.0 (wire cross-read; TLS-verify-off default) | evaluated, not adopted | 2026-07-06 |
| GaiaKeep SDK | `GaiaKeep/gfs` git @ `9f30ebaeebdfdf1f44a2980d62ab0908d809e7a9` | pinned, verifiable in `backend/requirements-gaiakeep.txt` | 2026-10-13 (from in-repo pin) |
| AstralPlane dependency | AstralPlane issue #67 | open, external, unverified in this environment | 2026-10-13 |

Internal authorities (current tree, verified 2026-10-13): `backend/shared/feature_flags.py` (`"cresco": self._read("FF_CRESCO", False)`); `backend/orchestrator/local_agents.py` (`BUILT_IN_AGENT_DIRS`, `FIRST_PARTY_PUBLIC_AGENT_IDS`, the `gaiakeep` dual-flag guard); `backend/start.py::_gaiakeep_enabled`; `backend/agents/gaiakeep/transport.py` (`cresco_service_key` header over the SSH tunnel); `backend/shared/external_http.py::validate_egress_url` (http/https-only scheme admission).

## 10. Conformance cases

The cases in [`conformance-cases.json`](conformance-cases.json) are the executable acceptance surface of this contract. Each case id below is executed by the named test in `backend/tests/test_cresco_contract_095.py`; the suite fails closed if a case is added without an executor or removed without updating this document.

- **C-01** flag default: `FF_CRESCO` default off, fail-closed.
- **C-02** bridge runtime absent: no `agents.cresco` package is importable.
- **C-03** bridge unregistered: `cresco` is not a built-in agent directory and discovery never yields it.
- **C-04** bridge unlisted: `cresco-1` is absent from `FIRST_PARTY_PUBLIC_AGENT_IDS`.
- **C-05** GaiaKeep dual flag: enablement requires `FF_GAIAKEEP` AND `FF_CRESCO` (`backend/start.py::_gaiakeep_enabled`).
- **C-06** dual-flag truth table: both-off / one-off → disabled; both-on → enabled; flag-read failure → disabled.
- **C-07** wsapi envelope: the pinned canonical request shape parses, and the pinned bulk-param encoding is exactly `base64(gzip(json_bytes))` under the §2 canonical rule, with exact round-trip.
- **C-08** profile boundary records: the contract's egress/TLS profile statements remain present (text-level guard; no client exists to execute behaviorally in this snapshot).
- **C-09** member-target grammar: valid targets accepted; wildcards/empty/over-depth/non-grammar targets rejected.
- **C-10** fault states: the closed set of §6 with fixed dispositions and retryability, and no credential material in any result text.
- **C-11** credential boundary: no committed file contains a literal `cresco_service_key` value; committed references are env-name references only.
- **C-12** no new bus: no Cresco/JVM/broker/OSGi artifact in runtime manifests (requirements, Dockerfiles, compose files).
- **C-13** source pins: every authoritative external source carries identifier, status, and retrieval date; unreverified pins are labeled as such.
