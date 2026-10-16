# Contract: MCP authorization

## Resource and discovery

The canonical MCP resource is `${PUBLIC_BASE_URL}/mcp`, with scheme/host normalized and no fragment. When the feature is enabled, `PUBLIC_BASE_URL` must be an externally correct HTTPS origin; proxy headers from arbitrary clients are not trusted to invent it.

Protected-resource metadata is served at:

`/.well-known/oauth-protected-resource/mcp`

```json
{
  "resource": "https://astral.example/mcp",
  "authorization_servers": ["https://id.example/realms/astral"],
  "scopes_supported": ["mcp:discover", "mcp:tools:read", "mcp:tools:invoke"],
  "bearer_methods_supported": ["header"]
}
```

`offline_access` is never challenged or published as a resource scope.

## Token validation

The MCP-only dependency accepts exactly one header bearer token and validates signature, expiry/not-before, issuer equality with the configured Keycloak realm, and audience membership containing `astral-mcp`. It does not change the validators used by web, native, A2A, or delegation routes. A token minted only for the existing web client is rejected even if its scopes look sufficient.

The inbound token is used only at this resource-server boundary. It is never forwarded to an agent, provider, external service, or delegated call; downstream credentials are independently resolved or minted by the existing gate stack.

## Challenges

Missing/invalid credentials return 401 with one header of this shape:

`WWW-Authenticate: Bearer resource_metadata="https://astral.example/.well-known/oauth-protected-resource/mcp", scope="mcp:discover", error="invalid_token"`

A valid token missing one or more operation scopes returns 403 with all scopes needed for that operation in one challenge:

`WWW-Authenticate: Bearer resource_metadata="https://astral.example/.well-known/oauth-protected-resource/mcp", scope="mcp:tools:read", error="insufficient_scope"`

Scope hierarchy is explicit: `mcp:tools:invoke` implies `mcp:tools:read`, and `mcp:tools:read` implies `mcp:discover`. No unrelated Keycloak role or `offline_access` implies an MCP scope.

## Authorization invariant

An OAuth scope is never sufficient authorization in Astral. It establishes that a token may ask for an operation. Listing and invocation still run through authenticated owner isolation, current agent visibility, per-tool permission, policy, PHI, taint, delegation, credential, confirmation, destructive-operation, concurrency, and hash-chained audit gates. Revocation between list and call therefore takes effect at call time.

## Bounded Work status resources

`resources/list` and `resources/read` use the existing `mcp:tools:read` transport scope. This scope grants no Work data authority. The implemented Work authority is a resolved `afk_` framework credential with `operations.read`, matching authenticated owner, and a current Plane credential fence in every Work read transaction. Attenuated callers retain their narrower scopes; delegated or machine claims cannot become human Work authority. The current credential is checked again after sharing-policy evaluation and after admission release, immediately before HTTP delivery.

Ordinary framework get/list/poll reads use the same current credential fence with `operations.read`; retained-result reads require `artifacts.read` and retain the existing MAC-verified WorkService result projection. Emergency status checks current authority before coordinator access and after its awaited result; completion-subscription listing retains its in-transaction authorization and rechecks before returning. MCP responses from these six read tools recheck again after admission release. Resource sharing policy remains specific to the resource surface.

Only `astral://work/{canonical-uuid4}/status` is supported. Listing returns at most 50 owner-visible status descriptors; reading returns bounded scalar status fields through the existing WorkService projection. Both responses use `Cache-Control: no-store` and the local fail-closed PHI sharing policy. A missing policy, malformed projection, revoked credential, foreign owner, or read failure returns no Work content.

Artifact resources are unavailable until retained-result MAC verification, artifact permission and PHI policy, and the paired renderer are integrated. A valid Keycloak MCP JWT alone does not currently provide a supported Work resource authority and is refused. This status transport does not qualify ChatGPT Apps rendering or external-client acceptance; those remain separate required verification.

