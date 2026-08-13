# Keycloak ORCID identity for restricted external agents

Astral accepts an ORCID iD for agent authorization only when it is a claim in a
Keycloak access token that passed the normal issuer, signature, audience,
authorized-party, and expiry checks. Tool arguments, browser state, request
headers, and agent-supplied values are never identity evidence.

The first deployment uses an **administrator-attested** account attribute. This
does not perform an ORCID OAuth proof-of-control ceremony; it is appropriate for
the current two-account allowlist because both accounts and identifiers are
operator-approved. Add an ORCID identity provider and brokered account-link flow
before allowing self-service linkage.

On the Astral server, explicitly authorize PanAtlas to receive a projected
identity claim (registration trust alone is intentionally insufficient):

```dotenv
IDENTITY_CLAIM_TRUSTED_AGENTS=panatlas-1
```

The default is empty and fail-closed. Restart/recreate the orchestrator after
changing it because production environment is loaded at process start.

## 1. Create the protected user-profile attribute

In the `Astral` realm, open **Realm settings → User profile → Create attribute**:

| Setting | Value |
|---|---|
| Attribute name | `orcid` |
| Display name | `ORCID iD` |
| Required | Off |
| User can view | Off |
| User can edit | Off |
| Admin can view | On |
| Admin can edit | On |
| Validation | `pattern` |
| Pattern | `^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$` |

Keeping user edit/view disabled prevents a principal from self-asserting or
changing the authorization value. Astral and PanAtlas also perform the ORCID
MOD 11-2 checksum validation, so a format-only match is insufficient.

## 2. Emit the claim from every Astral login client

Create an OpenID Connect client scope named `orcid-identity`. Under its
**Mappers** tab, add **User Attribute** with:

| Setting | Value |
|---|---|
| User Attribute | `orcid` |
| Token Claim Name | `orcid` |
| Claim JSON Type | `String` |
| Add to access token | On |
| Add to ID token | On |
| Add to userinfo | Off |
| Multivalued | Off |

Assign `orcid-identity` as a **Default** client scope to:

- `astral-frontend` (web)
- `astral-desktop` (Windows + macOS)
- `astral-mobile` (Android + iOS)
- `astral-watch` (watchOS)
- `astral-agent-service` (background/offline delegated work)

Default assignment is required: Astral does not request an optional ORCID scope
during sign-in. Accounts without the attribute simply receive no `orcid` claim.

## 3. Link the two approved accounts

Under **Users**, set the administrator-managed `orcid` attribute on exactly the
approved Cody and Sam accounts:

- Cody: `0000-0001-9588-3501`
- Sam: `0009-0003-6606-0831`

Do not add the attribute to another account unless the PanAtlas deployment's
`PANATLAS_ALLOWED_ORCIDS` is deliberately changed too. PanAtlas remains the
authoritative allowlist and independently rejects any other valid ORCID iD.

Existing tokens do not gain a newly mapped claim. After the mapper or attribute
changes, sign out of Astral on each device and sign back in. A refresh may work,
but a full sign-out/sign-in is the deterministic verification path.

## 4. Verify without exposing a token

Use the Keycloak Admin Console's **Client scopes → Evaluate** view for each
approved user/client and confirm the generated access-token claims contain the
exact canonical `orcid` value. Do not paste bearer tokens into tickets or logs.

Then verify Astral behavior:

1. An approved, freshly signed-in account sees PanAtlas tools and can call
   `summarize_atlas_state`.
2. A user with no ORCID attribute does not receive PanAtlas tools in chat or the
   Astral MCP `tools/list` projection.
3. Forced calls, parallel calls, chained agent hops, component re-execution, and
   external MCP calls without the verified claim are refused by the shared gate.
4. PanAtlas independently returns non-retryable MCP error `-32001` for a valid
   but unlisted ORCID, or for a missing/malformed/spoofed identity envelope.

For operator-approved agent IDs only, Astral forwards the card-declared claim as
`caller_info.verified_identity.orcid`. It does not forward the access token,
subject, username, email, roles, or unrelated claims.

## 5. Rollback

Remove `orcid-identity` from the clients or remove the two user attributes, then
revoke active sessions. Astral immediately hides/refuses the tools on newly
minted tokens; PanAtlas remains fail-closed behind its own allowlist. For an
immediate PanAtlas-side stop, run `systemctl stop panatlas-astral` on its host.
