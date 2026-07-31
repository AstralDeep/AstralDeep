# Contract: MCP Streamable HTTP endpoint

## Surface and transport

- Endpoint: `POST /mcp` only, mounted only when `FF_MCP_SERVER=true` at process start.
- `GET /mcp` and `DELETE /mcp`: `405 Method Not Allowed` while enabled; the route does not exist while disabled.
- Body: one UTF-8 JSON-RPC 2.0 request or notification, never a batch or client response. Maximum body size is 1 MiB.
- Authentication: `Authorization: Bearer` only. Cookies and URI tokens are rejected.
- Accepted request media type: `application/json`. Responses are JSON for this implementation; clients may advertise both JSON and SSE.
- `Mcp-Session-Id` and `Last-Event-ID` are ignored and never minted or echoed.
- Present invalid `Origin`: HTTP 403. Endpoint CORS never permits credentials.
- Notification accepted: HTTP 202 with no body. This revision defines no core client-to-server HTTP notification; unknown notifications are rejected.

## Mirrored headers

Every POST requires:

- `MCP-Protocol-Version: 2026-07-28`, equal to `params._meta["io.modelcontextprotocol/protocolVersion"]`.
- `Mcp-Method`, equal byte-for-byte to the case-sensitive JSON-RPC `method`.
- `Mcp-Name` for `tools/call`, equal to `params.name` after decoding.

Header names compare case-insensitively; values compare case-sensitively. A name is plain only when it is visible ASCII without leading/trailing whitespace and does not itself match the sentinel. Otherwise its UTF-8 bytes are Base64 encoded exactly as `=?base64?{value}?=`. Malformed Base64, unsafe plain values, missing headers, or header/body mismatch return HTTP 400 and JSON-RPC `-32020`.

Unsupported protocol version returns HTTP 400 and `-32022` with `data.supported = ["2026-07-28"]`. A missing required client capability returns HTTP 400 and `-32021` with `data.missing`. A supported-version request missing required request metadata is HTTP 400 / JSON-RPC `-32602`. An unimplemented RPC returns HTTP 404 / `-32601`. Removed legacy HTTP methods return 405.

## Request metadata

Every request's `params._meta` contains:

```json
{
  "io.modelcontextprotocol/protocolVersion": "2026-07-28",
  "io.modelcontextprotocol/clientCapabilities": {},
  "io.modelcontextprotocol/clientInfo": {"name": "host", "version": "1.0"}
}
```

The first two keys are required; `clientInfo` is recommended and never used as authenticated identity.

## Methods

`server/discover` params contain only `_meta`. Result:

```json
{
  "resultType": "complete",
  "supportedVersions": ["2026-07-28"],
  "capabilities": {"tools": {}},
  "instructions": "Astral exposes the requesting user's authorized read-only MCP tools; destructive unattended calls are refused.",
  "ttlMs": 60000,
  "cacheScope": "private",
  "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "AstralDeep", "version": "..."}}
}
```

`tools/list` accepts optional pagination fields plus `_meta`. Result contains `resultType`, the authorized `tools` array, `ttlMs: 60000`, and `cacheScope: "private"`. Every tool uses `name`, `description`, object-root `inputSchema`, optional `outputSchema`, annotations, and bounded `_meta` carrying informational Astral scope/agent identifiers.

`tools/call` requires `params.name`, object `params.arguments`, and `_meta`. Result:

```json
{
  "resultType": "complete",
  "content": [{"type": "text", "text": "..."}],
  "structuredContent": {},
  "isError": false,
  "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "AstralDeep", "version": "..."}}
}
```

Tool failures set `isError: true` inside a completed result. Protocol/transport failures use the JSON-RPC `error` member instead. A tracked Astral operation is ordinary structured content containing its Astral identifier and status, never an MCP Tasks handle.

`subscriptions/listen` parses the requested filter and returns an empty agreed notification set. Astral advertises and emits no notification type. No Tasks or Apps extension is advertised; an extension-defined `task` result type is refused.
