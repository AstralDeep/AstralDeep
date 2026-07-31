# Research: MCP `2026-07-28` implementation baseline

**Retrieval date:** 2026-07-31  
**Astral baseline:** `main@e492668b`  
**Protocol baseline:** MCP `2026-07-28` pages and `schema/2026-07-28/schema.ts` from the Model Context Protocol specification repository.

This file records the implementation reading, not a claim that Phase B has passed live interoperability. The official documents are living sources; identifiers and retrieval dates below pin the facts used by this implementation.

## Primary sources

- [MCP `2026-07-28` specification](https://modelcontextprotocol.io/specification/2026-07-28)
- [TypeScript schema for `2026-07-28`](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/schema/2026-07-28/schema.ts)
- [Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
- [Versioning and compatibility](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)
- [Server discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)
- [Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
- [Extensions overview](https://modelcontextprotocol.io/extensions)
- [Tasks extension overview](https://modelcontextprotocol.io/extensions/tasks/overview)
- [MCP Apps overview](https://modelcontextprotocol.io/extensions/apps/overview)

## Pinned construct inventory

| Identifier | Normative posture used here | Astral disposition |
|---|---|---|
| `io.modelcontextprotocol/protocolVersion` | Required in request `_meta`; over HTTP it must match `MCP-Protocol-Version`. | Required on Phase A internal requests and Phase B requests; only `2026-07-28` accepted. |
| `io.modelcontextprotocol/clientInfo` | Client SHOULD send an `Implementation`; it is self-reported and must not drive security decisions. | Carried for diagnostics only; never trusted for identity or authorization. |
| `io.modelcontextprotocol/clientCapabilities` | Required per request; the empty object means no optional capabilities. | Required; no capability is inferred from earlier requests. |
| `io.modelcontextprotocol/logLevel` | Optional and deprecated in this revision; absence prohibits log notifications. | Accepted as unknown/unused metadata; Astral emits no MCP log notifications. |
| `io.modelcontextprotocol/subscriptionId` | Required on notifications delivered through a subscription and absent otherwise. | No notifications are emitted because the agreed subscription set is empty. |
| `io.modelcontextprotocol/serverInfo` | Server SHOULD identify itself on results. | Present on Phase B results and the Phase A response envelope. |
| `ResultType` | Open string union containing `complete` and `input_required`; every modern result must include `resultType`. | Internal response field is `result_type`; HTTP projection emits `resultType`. Extension-defined `task` is refused because it was not negotiated. |
| `CacheableResult` | Requires non-negative `ttlMs` and `cacheScope` of `public` or `private`. | Discovery and the user-specific tools list are private-cacheable for 60 seconds. |
| `DiscoverResult` | Contains `supportedVersions`, `capabilities`, optional `instructions`, cache fields, and normal result metadata. | Exactly one supported version, tool capability only, no extensions or change notifications. |
| `Tool` / `ToolAnnotations` | `name`, object-root JSON Schema 2020-12 `inputSchema`, optional `outputSchema`, annotations including read-only/destructive hints. | Built from the authorized per-user catalog; hints are derived from Astral's existing destructive classification and never substitute for authorization. |
| `InputRequiredResult` | Represents bounded MRTR with `inputRequests` and opaque `requestState`. | Not emitted in this phase; destructive unattended calls are refused rather than elicited. |
| `SubscriptionFilter` | Opt-in booleans for list changes plus resource URI subscriptions. | Parsed, but the server agrees to an empty filter and emits nothing. |
| `HEADER_MISMATCH` | `-32020`; modern HTTP validation failures use HTTP 400. | Used for missing, malformed, or body-mismatched standard headers. |
| `MISSING_REQUIRED_CLIENT_CAPABILITY` | `-32021`; error data names missing capabilities. | Used before dispatch if an operation requires an undeclared capability. |
| `UNSUPPORTED_PROTOCOL_VERSION` | `-32022`; error data lists supported versions. | Used before dispatch for every version other than `2026-07-28`. |

The schema leaves `-32000..-32019` implementation-defined. Astral deliberately allocates no MCP-path codes in that subrange; JSON-RPC's standard codes and the protocol-defined `-32020..-32022` codes are sufficient here.

## Findings not obvious from the changelog

1. `server/discover` is mandatory for a server implementing this revision and optional for a client. The recommended five-second era probe applies to stdio. A dual-era HTTP client instead sends a modern request and inspects a `400` body: a recognized `-32020`, `-32021`, or `-32022` body means modern MCP; an empty or unrecognized body permits legacy fallback.
2. Extensions evolve independently from the core revision. There is no implied “2026-07-28 Tasks” or “2026-07-28 Apps.” The Tasks extension is presented as experimental while its normative document remains on its independent draft track. MCP Apps uses `io.modelcontextprotocol/ui` and has its own `2026-01-26` release/version axis.
3. The Tasks draft uses `-32003` for missing client capability while core `2026-07-28` assigns that meaning to `-32021`. The former lies in an implementation-defined range, not a core-forbidden range. Astral avoids the collision by not adopting Tasks and allocating nothing in `-32000..-32019`.
4. Streamable HTTP is stateless in this revision: each message is a new POST, GET and DELETE are removed, session/resumption headers are ignored, and closing a response stream is the cancellation signal.
5. The request body is the source of truth. `MCP-Protocol-Version`, `Mcp-Method`, and conditionally `Mcp-Name` mirror body values and must be checked before dispatch. Unsafe or sentinel-looking names use the exact `=?base64?BASE64?=` encoding.

## Authorization split

Authorization is optional in MCP generally, but a protected HTTP MCP server must behave as an OAuth resource server. Astral therefore implements protected-resource metadata, header-only bearer tokens, issuer validation, and the dedicated `astral-mcp` audience. OAuth scope establishes token sufficiency only; Astral's owner isolation, tool permission, policy, PHI, delegation, confirmation, and audit gates remain authoritative for every list and call operation.

## Error namespace reconciliation

Three error vocabularies coexist and remain intentionally distinct:

- MCP/JSON-RPC integer codes live on the protocol error object. Core-defined
  modern transport failures use `-32020..-32022`; standard JSON-RPC failures
  use `-32600..-32603`. Astral allocates nothing in `-32000..-32019`.
- Existing string `code` values may appear inside an agent/tool error dict.
  They are domain failure classifications and do not become JSON-RPC codes.
- `OperationStatus._ERROR_CODES` is the durable Astral operation-status
  vocabulary. It does not define MCP transport semantics.

The reconciliation point is the orchestrator response boundary: internal
`MCPResponse.error` remains a tool-level failure for a successfully dispatched
`tools/call`, while malformed transport/envelope conditions become JSON-RPC
errors in `mcp_server_endpoint.py`. Neither domain string codes nor operation
status codes are numerically remapped.
