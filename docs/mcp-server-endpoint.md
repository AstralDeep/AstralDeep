# MCP 2026-07-28 server endpoint

AstralDeep exposes an optional MCP Streamable HTTP resource at `POST /mcp`.
It is disabled by default and adds no route, metadata document, renderer, or
advertisement while disabled.

## Enablement

1. Configure `PUBLIC_BASE_URL` as the deployment's canonical external HTTPS
   origin. Do not derive this value from untrusted forwarding headers.
2. Configure the dedicated `astral-mcp` Keycloak audience and the
   `mcp:discover`, `mcp:tools:read`, and `mcp:tools:invoke` scopes as described
   in [keycloak-realm-settings.md](keycloak-realm-settings.md).
3. Forward `/mcp` and `/.well-known/oauth-protected-resource/mcp` through the
   TLS reverse proxy. Preserve `Authorization`, `MCP-Protocol-Version`,
   `Mcp-Method`, and `Mcp-Name` request headers.
4. Set `FF_MCP_SERVER=true` and recreate the orchestrator container. Feature
   flags are read once at import; a restart of an unchanged container is not a
   source-sync operation.

The endpoint accepts header bearer credentials only, validates issuer and the
`astral-mcp` audience, caps bodies at 1 MiB, and uses the durable `mcp`
admission class (8 active, 32 queued, 5-second maximum wait). Its CORS policy
allows only the canonical deployment origin and never allows credentials.

## Authorization model

OAuth scopes are admission, never sufficient authorization. `tools/list`
projects the caller's current live, non-draft, non-disabled, non-security-
blocked, per-tool-authorized catalog. `tools/call` recomputes that projection
and then runs `_authorize_and_prepare`, including policy, PHI/taint,
credential, delegation, hook, and concurrency gates. The inbound MCP bearer
is discarded after validation and is never forwarded; downstream delegation
authority is separately minted from verified claims and current permissions.

Every destructive remote-compute verb is refused before agent transport on
the unattended MCP channel. Run such work in Astral's interactive UI so the
existing durable human-confirmation flow can apply.

## Supported surface

- `server/discover`
- `tools/list`
- `tools/call`
- `subscriptions/listen`, acknowledged with an empty notification set

The server does not advertise list-changed notifications, MCP Tasks, MCP Apps,
or any other extension. Tracked Astral operations remain ordinary structured
tool results with Astral identifiers/status; they are not MCP task handles.
`GET /mcp` and `DELETE /mcp` return 405 while enabled. Session and resumption
headers are ignored and are never minted or echoed.

## Observability and rollback

Structured logs contain only bounded method/phase/outcome codes. Runtime
metrics use low-cardinality `operation_kind`, `phase`, and `result_code`
labels. Bearers, payloads, arguments, user/chat identifiers, and target URLs
are excluded.

To roll back, set `FF_MCP_SERVER=false` and recreate the container. The routes,
metadata document, and advertisement disappear. The additive idle `mcp`
admission configuration may remain in PostgreSQL; it has no effect without the
route and is retained to avoid deleting rows that an interrupted deployment
could still reference.
