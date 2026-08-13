# Keycloak realm settings for AstralDeep (operator guide)

> Feature 028 (FR-017). This document replaces the never-committed
> `keycloak-persistent-login-realm-settings.md` referenced by earlier docs.
> Agent-delegation (RFC 8693) client setup lives in
> [keycloak_agent_delegation_setup.md](keycloak_agent_delegation_setup.md).

AstralDeep authenticates exclusively against Keycloak (Constitution VII).
The orchestrator drives a **server-side** OIDC Authorization Code + PKCE flow
(`/auth/login` → Keycloak → `/auth/callback`); tokens never live in the
browser beyond the short-lived access token used for the WS handshake.

External agents may additionally require administrator-attested identity
claims. Follow [Keycloak ORCID identity for restricted external
agents](keycloak-orcid-identity.md) for the protected attribute, client-scope
mapper, approved account linkage, verification, and rollback procedure.

## Realm

| Setting | Required value | Why |
|---|---|---|
| SSO Session Idle / Max | operator's choice (browser SSO only) | The app session does not depend on the Keycloak browser session after login. |
| **Offline Session Idle** | **≥ 365 days** | The app requests `offline_access`; silent refresh + unattended jobs (feature 025) use offline refresh tokens. Idle below 365 d silently breaks the persistent-login promise (016 FR-001). |
| **Offline Session Max Limited** | **disabled**, or Max ≥ 365 days | Same as above. |
| **Access Token Lifespan** | **5–15 minutes** | The orchestrator refreshes silently server-side (028 D2); short access tokens keep revocation latency bounded (SC-004). |
| Revocation endpoint | enabled (default) | `/auth/logout` POSTs the refresh token to `…/protocol/openid-connect/revoke` (028 FR-012). |
| Login → Remember Me | **OFF** (Keycloak default) | 028 FR-010 / 016 FR-001: the sign-in page must not offer a "Remember me"/"Stay signed in" choice — persistence is the app's 365-day server-side session, not a user toggle. |
| **Login → User registration** | **OFF** | **App-store policy gate.** With self-registration ON the app "supports account creation", which activates Apple App Review guideline 5.1.1(v) and Google Play's account-deletion policy — both of which then REQUIRE an in-app account-deletion path. AstralDeep has none (the only per-resource purge helpers have no UI and no non-test callers), so a realm that offers `kc-registration` puts every iOS/Android submission at risk of rejection. Accounts are operator-provisioned; keep this OFF, and treat turning it ON as a change that must ship an in-app deletion surface first. Verify against the realm config, not the login page — `registrationAllowed` is the authoritative field: `curl -s -H "Authorization: Bearer $ADMIN_TOKEN" "$KEYCLOAK_URL/admin/realms/$REALM" \| jq .registrationAllowed` → expect `false`. (Do **not** grep the login page for `kc-registration`: with a placeholder or non-registered `redirect_uri` Keycloak returns 400 with an empty body, so the grep reports "absent" whether registration is on or off — it looks like a pass either way.) |

## Roles (required)

Create realm (or `astral-frontend` client) roles:

- `user` — required to enter the application at all.
- `admin` — admin chrome surfaces + admin REST endpoints.

A token carrying **neither** role is rejected at the WebSocket handshake and
by every REST dependency; the user gets an explicit no-access outcome.

## Client: `astral-frontend` (confidential)

| Setting | Value |
|---|---|
| Client authentication | On (confidential; secret → `KEYCLOAK_CLIENT_SECRET`) |
| Standard flow | Enabled (Authorization Code) |
| PKCE | `S256` (Advanced → Proof Key for Code Exchange) |
| Valid redirect URIs | `https://<host>/auth/callback` (plus `http://localhost:8001/auth/callback` for dev) |
| Valid post-logout redirect URIs | `https://<host>/` |
| Default/optional scopes | must include `offline_access` (the app requests `openid profile email offline_access`) |
| OAuth 2.0 Token Exchange | Enabled (agent delegation — see the delegation doc) |

## Orchestrator environment

```bash
KEYCLOAK_AUTHORITY=https://<keycloak-host>/realms/<Realm>   # full realm URL
KEYCLOAK_CLIENT_ID=astral-frontend
KEYCLOAK_CLIENT_SECRET=<from the client credentials tab>
AGENT_SERVICE_CLIENT_ID=astral-agent-service
AGENT_SERVICE_CLIENT_SECRET=<from the client credentials tab>
ASTRAL_ENV=production            # unset == production (fail closed)
USE_MOCK_AUTH=false              # mock auth refuses to boot in production
WEB_SESSION_ENC_KEY=<fernet key> # encrypts durable web sessions at rest (REQUIRED in production)
AGENT_API_KEY=<random secret>    # agent connections are refused without it in production
OFFLINE_GRANT_ENC_KEY=<fernet>   # feature 025 offline grants (logout revokes them)
WEB_SESSION_SECRET=<random>      # cookie HMAC (falls back to WEB_SESSION_ENC_KEY, then OFFLINE_GRANT_ENC_KEY)
```

Generate Fernet keys with
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

## Client: `astral-mcp` (feature 064 resource audience)

Create a dedicated Keycloak client/audience for third-party MCP hosts. Do not
append it to `KEYCLOAK_ALLOWED_AZP`; MCP validation is isolated from the
web/native allow-list and requires real `aud` validation.

1. Create the `astral-mcp` client using the flow appropriate for the host. A
   public host must use Authorization Code + PKCE; never distribute an Astral
   client secret to desktop software.
2. Add an audience mapper so issued access tokens contain `astral-mcp` in
   `aud`. A token carrying only `astral-frontend` is intentionally refused.
3. Create and assign the client scopes `mcp:discover`, `mcp:tools:read`, and
   `mcp:tools:invoke`. Invocation implies read, and read implies discovery at
   the resource server. Do not add `offline_access` to this resource's
   published or challenged scopes.
4. Set `PUBLIC_BASE_URL` to the canonical external HTTPS origin, configure the
   reverse proxy to forward `/mcp` and
   `/.well-known/oauth-protected-resource/mcp`, then recreate the orchestrator
   with `FF_MCP_SERVER=true`.

The OAuth scope only admits a request to the resource. Agent visibility,
owner isolation, per-tool permission, policy, PHI/taint, delegation,
credentials, destructive-operation refusal, concurrency, and audit gates are
still evaluated at listing or invocation time.

## Behavior summary (what these settings power)

- Unauthenticated `GET /` → 302 to Keycloak; after login the user lands on
  their original destination (deep links included).
- Sessions renew silently server-side for up to **365 days from the last
  interactive login** — refreshes never extend that cap; at the cap the user
  must sign in interactively again.
- Sessions are stored (encrypted) in Postgres: backend restarts and
  multi-instance deploys do not log anyone out.
- Sign-out: server session deleted → refresh token revoked at Keycloak
  (queued and retried if Keycloak is unreachable) → all of the user's
  feature-025 offline grants revoked → Keycloak end-session redirect.
- A different user signing in on the same browser revokes the prior user's
  session first.

## Feature 051 — native Apple clients (shared clients + `astral-watch`)

The Apple clients do **not** get dedicated `astral-ios` / `astral-macos` OIDC
clients and there is no `astral://oauth2redirect` scheme. The shipped clients
**reuse the existing shared public clients**, matching how the code
authenticates:

| Apple client | OIDC client it uses | Flow |
|---|---|---|
| iOS | **`astral-mobile`** (shared with Android) | Standard flow + PKCE (system browser session) |
| macOS | **`astral-desktop`** (shared with Windows) | Standard flow + PKCE (system browser session) |
| watchOS | **`astral-watch`** (dedicated) | **OAuth 2.0 Device Authorization Grant only** |

Because iOS/macOS reuse the shared clients, deploying the Apple family adds no
new `azp` beyond `astral-watch`. The only realm change the phone/desktop apps
require is registering the Apple redirect URI on the clients they reuse.

Setup:

1. **Apple redirect URI on the shared clients.** iOS and macOS use the
   redirect `com.personalailabs.astraldeep:/oauth2redirect`. Add it as a
   **Valid Redirect URI on BOTH `astral-mobile` and `astral-desktop`** (in
   addition to whatever Android/Windows already register). No `astral-ios` /
   `astral-macos` client is created.
2. **Watch client `astral-watch`** — create it as *public* (no secret).
   Capability config → enable **OAuth 2.0 Device Authorization Grant**;
   Standard/Direct-access flows OFF (no redirect URI; approval happens on
   another device). The realm's well-known must then advertise
   `device_authorization_endpoint` — the orchestrator's device-login broker
   fails closed (HTTP 503) until it does.
3. Append `astral-watch` to `KEYCLOAK_ALLOWED_AZP` (alongside `astral-mobile`
   and `astral-desktop`, which the shared clients already require), and keep
   `KEYCLOAK_DEVICE_CLIENTS=astral-watch` (only the watch may use the device
   grant through the broker).
4. Session semantics (research D7): the device-grant approval is an
   interactive login. The realm's **SSO Session Max** / client-session-max
   caps bound how long silent refresh can carry any native session — set them
   to the operator's 365-day policy so watch sessions match web semantics.
5. Optional hygiene: on `astral-watch` set *OAuth 2.0 Device Code Lifespan*
   (default 600 s) and *Device Polling Interval* (default 5 s) — the broker
   relays whatever the realm issues and enforces the pacing server-side.

Watch sign-out uses the existing native logout (`POST /api/auth/logout`,
`client_id=astral-watch`) with the offline-tolerant revocation queue (044).

## Feature 068 — kiosk sign-in for keyboard-less browser terminals

`GET /kiosk` shows a two-column sign-in page: an RFC 8628 device QR on the
left (scan with a phone, approve there) and the ordinary
"sign in with email and password" redirect on the right. It reuses the same
device-login broker the watch uses — that contract is unchanged — and `GET /`
is untouched.

Default **OFF** (`FF_KIOSK_LOGIN`). While off the router is never mounted, so
there is no `/kiosk` route and no OpenAPI entry.

**No realm changes are required.** `KIOSK_DEVICE_CLIENT` defaults to
`astral-watch`, which is already a public device-grant client in both
`KEYCLOAK_DEVICE_CLIENTS` and `KEYCLOAK_ALLOWED_AZP`. To turn the page on:

1. Set `FF_KIOSK_LOGIN=true` and recreate the container (the flag is read once
   at import).
2. Point the terminal's browser at `https://<host>/kiosk`.

The realm's well-known must advertise `device_authorization_endpoint` — the
same prerequisite the watch already has. Until it does, the QR column reports
that phone sign-in is unavailable and the password column still works.

**Delegation needs nothing extra.** The orchestrator performs the RFC 8693
exchange as `astral-frontend` regardless of which client the user signed in
with, so the device-grant client does **not** need the six `tools:*` scopes
assigned to it. (That assignment trap applies to the *requesting* client only —
see the agent-delegation section.)

### Optional: a dedicated kiosk client

Sharing `astral-watch` means kiosk and watch sessions cannot be told apart in
the audit trail and cannot be revoked independently. If that matters:

1. Create a public client, e.g. `astral-kiosk` — client authentication **off**
   (never distribute a secret to a terminal). Capability config → enable
   **OAuth 2.0 Device Authorization Grant**; Standard/Direct-access flows off
   (approval happens on the phone, so no redirect URI is needed).
2. Add it to **both** `KEYCLOAK_DEVICE_CLIENTS` and `KEYCLOAK_ALLOWED_AZP`. The
   broker requires the intersection, and the WebSocket handshake refuses a
   token whose `azp` is not allow-listed — miss the second and sign-in appears
   to succeed, then the app socket fails.
3. Set `KIOSK_DEVICE_CLIENT=astral-kiosk`.

`astral-frontend` can never be used: the device grant refuses the confidential
web client by construction (and a test pins it).

> **Session note.** A kiosk session is minted by this public client, and
> Keycloak only refreshes or revokes a token for its **issuing** client. The
> orchestrator derives that client from the token's `azp` claim, so silent
> refresh and sign-out both address the right client. Nothing to configure —
> but if you add further device-grant clients, they inherit this behavior.
