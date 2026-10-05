# Contract: wsapi client (`backend/agents/cresco/wsapi_client.py`)

**Feature**: 050-cresco-integration-decision | [spec.md](../spec.md) · [plan.md](../plan.md) · [research.md](../research.md)

The wsapi client is a hand-rolled JSON-over-WSS RPC client built on the **already-present `websockets` dependency + Python stdlib only** (`json`, `gzip`, `base64`, `ssl`, `asyncio`). It is the single seam the tools call. This contract is the acceptance surface for its unit tests (mocked socket, pinned fixtures).

## Construction / config

| Input | Source | Behavior when absent |
|---|---|---|
| `url` | `CRESCO_WSAPI_URL` | client is "unconfigured" → RPCs raise `CrescoUnavailable` (tools translate to an "unavailable" SDUI result) |
| `service_key` | `CRESCO_SERVICE_KEY` | unconfigured (as above) |
| TLS trust | `CRESCO_CA_BUNDLE` **or** `CRESCO_TLS_FINGERPRINT` | falls back to system trust; self-signed rejected |
| private-host allowance | `CRESCO_ALLOW_PRIVATE_HOST` | private hosts rejected by egress validation |

## C1 — Connect

- MUST connect to `wss://{host}:{port}/api/apisocket` (control plane) with the `cresco_service_key` HTTP header on the WebSocket upgrade.
- MUST call `shared/external_http.py::validate_egress_url(url)` **before** dialing; a validation failure MUST raise (no dial). The only permitted relaxation is the operator-scoped `CRESCO_ALLOW_PRIVATE_HOST` for the **configured** host — never a global private-host bypass (FR-007).
- MUST build an `ssl.SSLContext` with certificate verification **on**:
  - default: system trust store, `check_hostname=True`, `verify_mode=CERT_REQUIRED`;
  - `CRESCO_CA_BUNDLE` set ⇒ load that CA and require it;
  - `CRESCO_TLS_FINGERPRINT` set ⇒ verify the presented cert's SHA-256 matches (pinning);
  - MUST NOT ever set `verify_mode=CERT_NONE` or disable `check_hostname` globally (FR-007, SC-006). This is the concrete divergence from `pycrescolib`, which disables verification by default.
- Connection is lazy (opened on first RPC) and reused; dial has a bounded connect timeout.

## C2 — RPC envelope

- Request: `{"message_info": {…type/event/is_rpc + correlation id…}, "message_payload": {"action": <verb>, …params}}` (research.md R2).
- Bulk params MUST be encoded `base64(gzip(json_bytes))`; the client provides an encode helper and its inverse decode.
- The client MUST correlate a reply to its request via the `message_info` RPC id and return only the matching reply's payload.
- RPC has a bounded response timeout; on timeout the client raises a typed error (tools translate to a "fabric unreachable" result).

## C3 — Fail-safe on drift

- On a frame that does not match the pinned envelope (missing `message_payload`, wrong type, undecodable gzip/base64), the client MUST raise a typed `CrescoProtocolError` with a diagnostic — it MUST NOT silently mis-parse or return partial garbage (spec Edge Cases).
- On socket close / error mid-call, the client MUST surface a typed `CrescoUnreachable` and be able to reconnect on the next call using **stdlib** bounded backoff (no `backoff` package).

## C4 — Secret hygiene

- `CRESCO_SERVICE_KEY` MUST NOT be logged, echoed into errors, or included in any SDUI/audit payload. The URL host may be logged; the key never is (FR-003; Constitution VII).

## Typed errors (tools map these to SDUI results)

| Error | Meaning | Tool-facing result |
|---|---|---|
| `CrescoUnavailable` | not configured (url/key unset) | "Cresco fabric not configured" |
| `CrescoUnreachable` | dial/RPC timeout, socket error | "Cresco fabric unreachable" (retryable) |
| `CrescoAuthError` | key rejected by fabric | "Cresco authentication failed" (non-retryable) |
| `CrescoProtocolError` | frame drift / undecodable | "Unexpected fabric response" (fail-safe, non-retryable) |
| `CrescoTLSError` | verification/pin failure | "Cresco TLS verification failed" (non-retryable) |

## Unit-test surface (mocked socket, pinned fixtures — tasks T009)

- Envelope encode/decode round-trip, including gzip+base64 bulk params.
- `listregions` / `listagents` reply parsing against the **evaluated golden fixtures**.
- TLS context construction for each mode (default / CA bundle / fingerprint) and the **rejection** of a self-signed cert with no trust configured.
- `validate_egress_url` is called before dial; private host rejected unless `CRESCO_ALLOW_PRIVATE_HOST`.
- Each typed error path (unavailable / unreachable / auth / protocol-drift / TLS) is exercised and produces the right typed error.
- Secret-hygiene: the service key never appears in a raised error string or log record.


### Fix #242: Documentation & Usage Notes
- Clarified parameter requirements and validation handling for #242.
- Refer to issue details: Connect approved Astral deployments through a governed Cresco personal-mesh bridge.
