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

`astral://work/{canonical-uuid4}/status` returns bounded scalar status fields through the existing WorkService projection. Listing requires `operations.read` and returns at most 50 owner-visible status descriptors. A caller with only `artifacts.read` cannot list or read status.

`astral://work/{canonical-uuid4}/result` requires the existing `artifacts.read` permission. It reads the WorkService retained research result, which reconstructs the page selection from the settled actions and verifies its result MAC inside the authorized Plane transaction. The copied JSON view contains Work identity/revision, the closed result envelope, bounded selected text passages, source title, and receipt identities/digests. Source URLs, raw source content, authored HTML, executable actions, and artifact/share links are absent. Unsupported, unfinished, non-retained, or unverifiable results preserve the existing unavailable envelope; unavailable never implies successful completion. `operations.read` alone cannot read a result.

The method-specific authority is captured per request and retained through sharing-policy evaluation and the final post-admission delivery check. Both resources use `Cache-Control: no-store` and the local fail-closed PHI sharing policy. A missing policy, malformed projection, revoked credential, foreign owner, or read failure returns no Work content.

## Bounded external-host Work panel resource

An optional Work panel resource exists for external MCP hosts and is controlled by `FF_MCP_WORK_PANEL`, which defaults off and additionally requires `FF_MCP_SERVER`. While either flag is off, the panel surface does not exist: `resources/list` returns only the status and result descriptors, and a panel read returns `resource_unavailable` without performing any Work read.

When enabled, listing appends one panel descriptor per owner-visible status record: `astral://work/{canonical-uuid4}/panel` with MIME type `application/json`, a bounded display name, and `_meta` recording the panel kind, the closed state vocabulary (`waiting`, `running`, `input_required`, `failed`, `cancelled`, `succeeded`), the owner-scoped/no-third-party-data-flow restriction, and that the body is an authorized-host view model without embedded authority. Reading a panel URI requires the same framework credential authority as status (`operations.read`; an `artifacts.read`-only credential is refused), resolves the owner's own Work record, and refuses a record whose identity does not match the URI, whose disposition does not map into the closed state vocabulary, or whose display fields are absent or unsafe.

The panel body is a bounded view model with a closed key set: `kind` (fixed panel kind), `state` (mapped from the current disposition through a closed table), `title` (truncated to 200 characters), and optional `detail` (bounded Work phase) and `status_message` (the bounded safe error code, present only in the failed state). No URL, artifact link, bearer token, credential identifier, owner identifier, usage, or instruction text is ever copied into the view; the mapping copies only the allowlisted fields. Disposition-to-state mapping is truthful: queued maps to waiting, active/awaiting-event/retry-eligible/paused/budget-blocked to running, approval/authority/reconciliation waits to input-required, completed to succeeded, failed to failed, and cancelled/unsupported-version to cancelled. The response carries the same `_meta` restrictions plus the server info, and `Cache-Control: no-store`.

Panel reads use the same current credential fence, local fail-closed PHI sharing policy, admission class, and post-admission revocation recheck as the other Work resources. A guessed or replayed URI for foreign work fails closed at the owner-scoped Work read; a revoked credential is refused before delivery; a PHI-positive title never crosses the resource boundary. Panel resources are read-only: no view-model field carries an action, and host-triggered tool calls continue to route exclusively through the normal admitted, confirmed, idempotent, and hash-chained-audited Work dispatch. Unsupported MCP clients are unaffected: tool results keep their existing text content and structured envelopes, and no protocol version, capability, or advertisement changes.

The external-host HTML presentation layer — a static bounded panel document, its output-template tool metadata, and the CSP (`default-src 'none'` with inline script/style only and all network directives closed), sandbox (`allow-scripts` without `allow-same-origin`), and host-origin restrictions — is owned by Projection's qualified panel renderer and is adopted only by moving the Projection pin to that exact qualified revision. The currently pinned Projection revision does not carry that renderer, and the companion revision that does predates the bypass protocol manifest entries the composed product serves, so adopting it as-is would regress qualified cross-client behavior; the pin move is therefore held until the component revision that carries both is qualified. Until then this data resource is the whole panel surface, external-host HTML rendering is not claimed, and live ChatGPT host acceptance remains separate required verification.

Generic artifact resources remain unavailable pending their explicit authorization/link policy and qualified paired renderer. A valid Keycloak MCP JWT alone does not currently provide a supported Work resource authority and is refused. These JSON transports do not qualify ChatGPT Apps rendering or external-client acceptance; those remain separate required verification.

