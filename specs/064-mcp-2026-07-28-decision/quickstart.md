# MCP server quickstart

This endpoint is fail-closed and absent by default. These steps are for a staging deployment of a candidate image; they are not evidence that FR-058 passed.

## Enable the endpoint

1. Configure `PUBLIC_BASE_URL` as the externally reachable HTTPS origin. The reverse proxy must preserve the `/mcp` path and must not permit untrusted forwarded headers to redefine the canonical host.
2. Set `FF_MCP_SERVER=true` in the orchestrator service environment.
3. Recreate the orchestrator container. Feature flags are read once at import; a restart of an existing container is not sufficient when its baked source or Compose environment changed.
4. Confirm `POST ${PUBLIC_BASE_URL}/mcp` is routed to port 8001 and that GET/DELETE receive 405.

The endpoint enforces a 1 MiB body limit, header-only bearer credentials, Origin checks, and its own credential-free CORS posture.

## Configure Keycloak

Create a dedicated `astral-mcp` client/audience; do not add `astral-mcp` to tokens for the existing web/native clients.

1. Create or select the client used by the third-party MCP host.
2. Add an audience mapper that includes `astral-mcp` in the access-token `aud` claim.
3. Define and assign only the required scopes: `mcp:discover`, `mcp:tools:read`, and `mcp:tools:invoke`. Do not publish or challenge `offline_access` as a resource scope.
4. Verify the token issuer exactly matches `KEYCLOAK_AUTHORITY` and the token audience contains `astral-mcp`.
5. Leave native logout/revocation client allowlists unchanged.

Protected-resource metadata is available only while the flag is enabled at `${PUBLIC_BASE_URL}/.well-known/oauth-protected-resource/mcp`.

## Minimal diagnostic request

Send `server/discover` with:

- `Authorization: Bearer <astral-mcp access token>`
- `Content-Type: application/json`
- `Accept: application/json, text/event-stream`
- `MCP-Protocol-Version: 2026-07-28`
- `Mcp-Method: server/discover`

The JSON-RPC params `_meta` must contain the matching protocol version and an explicit client-capabilities object. A web-client token must receive 401; an MCP token missing `mcp:discover` must receive 403 with one complete scope challenge.

## Live-verification checklist (FR-058)

Run every item against the staging deployment of the exact candidate image and record the image digest and Git SHA.

- [ ] A real third-party MCP host implementing `2026-07-28` discovers the server without legacy fallback.
- [ ] The host follows the protected-resource metadata challenge and obtains an `astral-mcp` audience-bound token.
- [ ] Two users with different agent/tool permissions list different, correct private tool sets with no cross-user entry.
- [ ] The host invokes a read-only tool and renders both content blocks and structured content.
- [ ] A destructive tool is refused before agent transport contact.
- [ ] A permission revoked after listing is refused at invocation.
- [ ] Invalid Origin, missing/mismatched headers, cookie/query credentials, and an oversized body are refused with the documented status/code.
- [ ] A purpose-built client conformance run is retained as diagnostic evidence but is not substituted for the real-host run.
- [ ] Evidence contains no bearer token, request payload, tool arguments, user/chat identifier, or other secret and is bound to the candidate SHA/image digest.

If no real third-party host supports the revision, leave this checklist and T091 open and label Phase B code-shaped but unproven.

## Rollback

Set `FF_MCP_SERVER=false` and recreate the orchestrator container. The MCP
route, metadata route, renderer registration, and advertisement are absent.
The additive idle PostgreSQL admission-class rows may remain; they expose no
surface and affect no other workload while the route is disabled.
