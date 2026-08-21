# Keycloak setup — native Android client (`astral-mobile`)

The Android client (`components/AstralProjection/android-client/`) authenticates with **real Keycloak** using
OIDC **Authorization Code + PKCE** through the system browser / Chrome Custom Tabs
(RFC 8252 native-app posture), exactly like the Windows client — just a different
public client and redirect scheme. There is no client secret (public client; PKCE
is the proof).

## 1. Create the public client

In the realm (e.g. `Astral` at `https://iam.ai.uky.edu`), create an OIDC client:

| Setting | Value |
|---|---|
| Client ID | `astral-mobile` |
| Client authentication | **Off** (public client) |
| Standard flow | **On** (Authorization Code) |
| Direct access grants | Off |
| PKCE — Proof Key code challenge method | **S256** |
| Valid redirect URIs | `com.personalailabs.astraldeep:/oauth2redirect` |
| Valid post-logout redirect URIs | `com.personalailabs.astraldeep:/oauth2redirect` (or `+`) |
| Web origins | leave blank (native app — no browser CORS origin) |

The redirect URI's scheme must match the app's `appAuthRedirectScheme` manifest
placeholder (`com.personalailabs.astraldeep`, set in `app/build.gradle.kts`) and
`AppConfig.OIDC_REDIRECT_URI`.

> **Migration note (2026-07-07):** the app's package/scheme was renamed from
> `com.kyopenscience.astral` to `com.personalailabs.astraldeep` for the Play
> Store release. A realm provisioned before that date still lists the old
> `com.kyopenscience.astral:/oauth2redirect` URI — add the new one (and keep
> the old entry only while sideloaded builds of the old package remain in use).

> `astral-mobile` may be cloned from the existing `astral-desktop` client; just
> **replace** the desktop loopback redirect with the Android custom-scheme
> redirect above (or keep both if the same client serves both — but a dedicated
> client per platform is cleaner).

## 2. Allow-list the client on the orchestrator

The orchestrator only accepts access tokens whose `azp` is allow-listed. Add
`astral-mobile` to `KEYCLOAK_ALLOWED_AZP` (comma-separated, alongside
`astral-desktop`):

```
KEYCLOAK_ALLOWED_AZP=astral-desktop,astral-mobile
```

Restart the orchestrator after changing `.env`. (Validated by
`backend/orchestrator/auth.py::is_azp_allowed`.)

## 3. Roles

Entry is role-gated like every other client: the signed-in user needs the realm
`user` (or `admin`) role, or the callback returns no-access. No Android-specific
roles are required.

## 4. App configuration

Defaults live in `app/.../AppConfig.kt`:

- `KEYCLOAK_AUTHORITY = https://iam.ai.uky.edu/realms/Astral`
- `OIDC_CLIENT_ID = astral-mobile`
- `OIDC_REDIRECT_URI = com.personalailabs.astraldeep:/oauth2redirect`
- `WS_URL = wss://sandbox.ai.uky.edu/ws`, `API_BASE = https://sandbox.ai.uky.edu`

Point these at your deployment if different.

## 5. Dev builds

There is no dev-token shortcut: the former debug-only `DevAuth.kt` path was
removed in feature 044. Debug and release builds both authenticate exclusively
via Keycloak OIDC (Authorization Code + PKCE); debug builds differ only in
their endpoints (`ws://10.0.2.2:8001` local orchestrator vs. the TLS
deployment — see `AppConfig.kt`).
