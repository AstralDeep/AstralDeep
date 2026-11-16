# Phase 0 result: MCP over HTTP authorization spike and client metadata drafts (T0.8, T0.11)

Status: the flow in spec section 7.4.1 works end to end against a loopback fixture. Every refusal stops with nothing granted. The Client ID Metadata Document and association file validate. Nothing is published.

## What was read

The MCP revision `2026-07-28` is published. I read it on 2026-11-15 from these pages and found no conflict with spec 106.

- https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/authorization-server-discovery
- https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration
- https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations
- https://modelcontextprotocol.io/specification/2026-07-28/changelog

The changelog confirms the stateless change. The `initialize` handshake and `Mcp-Session-Id` are gone, and each request carries `io.modelcontextprotocol/protocolVersion` in `_meta`. The spike sends that and never initializes.

## What was built

Folder `specs/106-sign-in-free-clients/phase-0/mcp-oauth/`, Python 3.11, standard library only.

- `fixture.py` runs an authorization server and an MCP resource server on loopback. Switches cover a 401 with and without `resource_metadata`, S256 present or absent, a registration endpoint, a mismatched issuer, a wrong or missing `iss`, Client ID Metadata Document support, and where the metadata lives.
- `adapter.py` runs the flow: probe, 401, protected resource metadata, authorization server metadata, issuer equality, required S256, registration, authorization code with PKCE and `resource`, the RFC 9207 check, then the bearer call. The "browser" is a function. The tests pass one that asks the fixture to approve and returns the redirect.
- `test_flow.py` holds 30 tests.

Drafts in `site-drafts/` hold `oauth/client-metadata.json`, `.well-known/apple-app-site-association`, `validate_drafts.py` and 20 tests.

## Commands and versions

```
cd .claude/worktrees/106-p0-mcp-oauth
/Users/sam/Desktop/Work/AstralDeep/.venv/bin/python -m pytest -q -p no:cacheprovider specs/106-sign-in-free-clients/phase-0
/Users/sam/Desktop/Work/AstralDeep/.venv/bin/ruff check specs/106-sign-in-free-clients/phase-0
```

Python 3.11.15, pytest 9.1.1, ruff 0.15.21. Result: 50 passed in 1.04 s. Ruff reports no findings.

## Discovery trace

This is the `adapter.trace` of test `test_trace_for_deepest_metadata_fallback_is_exactly_this`, which asserts every line. The issuer is `http://127.0.0.1:<port>/tenant1` and the metadata is served only at the OpenID path-appending location, the last URL in the fixed order. The test replaces the port-bearing issuer with `<issuer>`.

```
probe without token returned 401
resource metadata url taken from WWW-Authenticate
GET /.well-known/oauth-protected-resource/mcp -> found
GET /.well-known/oauth-authorization-server/tenant1 -> missing
GET /.well-known/openid-configuration/tenant1 -> missing
GET /tenant1/.well-known/openid-configuration -> found
authorization server <issuer> supports S256
registration: dynamic
iss check passed
code redeemed with resource and code_verifier
```

Test `test_authorization_server_metadata_order` covers five placements and checks the exact request order. `test_discovery_from_401_with_resource_metadata_then_bearer_call` covers the header route and the bearer call. `test_401_without_resource_metadata_falls_back_path_then_root` covers the fallback, with the path URL tried before the root.

## S256 refusal

When `code_challenge_methods_supported` is absent, empty, or lists only `plain`, the adapter raises `no_s256`. The fixture then records zero registrations, zero authorization requests and zero token requests, and the keystore stays empty. The tests are the three cases of `test_missing_s256_is_refused_with_nothing_granted`. `test_second_authorization_server_with_s256_is_used_when_first_lacks_it` shows the adapter skipping a server without S256 and using the next listed one.

The revision's security page (https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations) says "If `code_challenge_methods_supported` is absent, the authorization server does not support PKCE and MCP clients MUST refuse to proceed." It also says clients "MUST use the `S256` code challenge method when technically capable". It states no refusal for a list that is present without S256. Spec 106 section 7.4.1 refuses in that case too, so the adapter is stricter than the revision's explicit text. The revision's intent for that case is UNVERIFIED.

## Registration order

The adapter takes the first branch that applies. `application_type` is set to `native` in dynamic registration.

| Order | Branch | Test |
|---|---|---|
| 1 | Pre-registered client for this issuer | `test_registration_prefers_preregistered_over_everything` |
| 2 | Client ID Metadata Document, when the server advertises `client_id_metadata_document_supported` | `test_registration_uses_client_id_metadata_document_before_dynamic` |
| 3 | Dynamic registration, when `registration_endpoint` exists | `test_registration_falls_to_dynamic_when_server_has_no_metadata_document_support` |
| 4 | Ask the owner for a client id | `test_registration_asks_the_owner_when_nothing_else_is_offered` |
| none | Refuse with `no_client_id` | `test_no_registration_path_and_no_owner_client_id_is_refused` |

Test 2 loads the real draft document and the fixture checks the redirect URI against it, so the draft is exercised on the server side too.

## Other refusals

- Issuer mismatch in metadata raises `issuer_mismatch` and the flow ends before registration. Test `test_issuer_mismatch_in_metadata_is_refused`.
- A wrong `iss` on the redirect raises `iss_mismatch` with both issuers in the message. The fixture sees no token request, so the code is discarded. Test `test_iss_mismatch_abandons_the_flow_and_never_redeems_the_code`.
- A missing `iss` is refused when the server advertises `authorization_response_iss_parameter_supported` and accepted when it does not. A wrong `iss` is refused either way, which matches the third row of the revision's table.
- A changed `state` or a redirect to another address is refused before any token request.
- An `http` endpoint is refused unless the loopback allowance is set. In production the egress path owns this (C14).

## Token hygiene

`test_tokens_never_appear_in_logs_traces_or_reprs` runs the full flow plus a bearer call at DEBUG level. It then scans the captured log, the trace, `repr` and `vars` of the connection and keystore for every code, verifier, access token and refresh token the fixture issued. None appears. `Tokens` marks both token fields `repr=False`. The keystore key is connection id plus issuer (C9). `call` sends the token only to the connection's endpoint and refuses any other address with `token_passthrough`. The fixture returns 400 for a token in the query string and the tests never send one.

## What today's code does differently

Compared with `backend/orchestrator/remote_mcp_oauth.py` and `remote_mcp_connectors.py`.

- **Protocol.** Today's client sends `initialize` at `2025-11-25` and stores a negotiated `_protocol_version`. The revision is stateless, so the handshake and the header go away.
- **Token storage.** `access_token`, `refresh_token` and `oauth_client_secret` sit in the connector record. Spec C8 and C9 move them to the device keystore and leave the record with none.
- **Issuer check.** Today a metadata issuer mismatch skips that server quietly and tries the next. The spike refuses the flow with a named error.
- **RFC 9207.** Today's `complete_authorization` has no `iss` check. The revision requires one before redeeming the code.
- **Registration.** Today's code uses a stored client id, else dynamic registration. It has no Client ID Metadata Document branch, no owner prompt branch and sends no `application_type`.
- **Resource metadata URL.** Today's code pins the header URL to the endpoint host and the well-known prefix. The spike trusts the header as the revision says. T2.6 should keep today's pin, because the egress path does not replace it.
- **Discovery order.** The authorization server metadata order is the same as today. The protected resource fallback order is also the same, path first and then root.
- **Shape.** Today's flow is split between a begin call and a browser callback with pending state in the settings store. The spike runs begin to end in one object, which suits a device that holds the verifier in memory.

## Spec 106 calls

- C8 (connection record holds no secret). Supports. The spike keeps tokens only in a keystore keyed by connection id and issuer, and the connection object holds none. It does not build the signed record or the event.
- C9 (credentials in the device keystore, keyed by connection id and issuer). Supports. The keyed store worked with no extra field. The platform keystores are untouched.
- C10 (code picks the adapter, the model never sees a token). Supports for the token half. The adapter has no model input, and the token scan passes. The adapter choice itself is not exercised.
- C11 (system browser sign-in). Supports in outline. The browser is an injected function that returns the redirect. No real browser or platform session ran.
- C12 (the executing device needs its own credential). Supports. `test_call_without_a_stored_credential_fails_closed` shows `not_signed_in`. Routing a prompt to the device is not touched.
- C13 (first connect is an owner approval signed by the presence key). Does not touch it. No approval card or signature exists in the spike.
- C14 (discovery goes through the egress path). Does not touch it. The spike uses plain urllib. It only shows that discovery needs a guard, since it refuses non-HTTPS.
- C16 and O16. Support, with the `application_type` risk below.

## What T2.6 to T2.9 can take

- The step order, the refusal codes and the trace lines.
- The fixture switches, which map directly to the plan's cases: 401 with metadata, no S256, no registration endpoint.
- The registration order and the web retry on rejection.
- The RFC 9207 table and the rule that the code is never sent before the check.
- The draft client metadata document, which the fixture already serves.

They must not take these.

- The urllib transport. Real code uses the approved egress path and pins the address it checked.
- The loopback `http` allowance.
- Any in-memory keystore. Real code uses the platform keystores.
- The skip-and-continue handling of a server without S256 across several listed servers without a policy decision. The spike does it, and the spec has no rule for it.
- Anything for step-up on 403, refresh, or a retry limit. The spike did not build them, and T2.9 starts from the revision text.

## Drafts (T0.11)

The document sits at `https://astraldeep.github.io/oauth/client-metadata.json`. The site is an organization Pages site, so the file lands at the root with a stable path under `/oauth/`. The `client_id` equals that URL, as the revision requires. Fields are `client_id`, `client_name`, `client_uri`, `redirect_uris`, `grant_types`, `response_types` and `token_endpoint_auth_method` set to `none`. It carries no secret.

The redirect URI is `https://astraldeep.github.io/oauth/callback`. `ASWebAuthenticationSession` matches that host and path without loading the page.

The association file claims `/oauth/callback` for `593LGH35GS.com.personalailabs.astraldeep` under `applinks`, and lists the same app under `webcredentials`. The team id is not in `project.pbxproj`, which takes it from `ASTRAL_DEVELOPMENT_TEAM` at build time. I took it from `TEAM_ID` in `scripts/release_macos.py`, where the signing identity line carries the same value.

`validate_drafts.py` checks the document against the revision's rules. It covers `client_id` equal to the document URL, an HTTPS URL with a path and no fragment or dot segments, the three required fields, redirect URIs that are HTTPS or loopback HTTP with no wildcard, and no secret fields or shared-secret auth methods. It checks that the association file names the bundle id, a ten character team id and the callback path.

## Open risks

- UNVERIFIED. Apple's page for `ASWebAuthenticationSession.Callback.https(host:path:)` confirms iOS 17.4 and macOS 14.4 but does not state the association requirements. I assumed both `applinks` and `webcredentials` entries and an associated domains entitlement. The app has no associated domains entitlement today. Phase 0 device testing (spec 7.5) settles this.
- UNVERIFIED. GitHub Pages must serve `/.well-known/apple-app-site-association` over HTTPS with no redirect. Jekyll skips dot folders unless the site has `.nojekyll` or an include rule, and the file has no extension so its content type may be `application/octet-stream`. T0.13 should fetch it after publishing.
- Strict OpenID servers reject `application_type` `native` with an HTTPS redirect URI. The fixture models this and the adapter retries as `web`, which the revision allows. A real server may reject both. The Client ID Metadata Document branch avoids the problem, which supports C16.
- `/oauth/callback` has no page behind it. GitHub Pages redirects a bare directory path to a trailing slash, so T0.13 should publish a page there for any browser that does reach it.
- No live server was contacted and no real credential was used. The fixture ran over `http` on loopback only.
