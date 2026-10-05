# Contract: Backend Deployment `.env` & Realm Production Posture

**Feature**: 053-apple-production-release · **User story**: US5 · **Requirements**: FR-018, FR-019, FR-020, FR-021 · **Research**: [D10](../research.md), [D11](../research.md)

This is the verification contract for the backend the Apple clients target
(`sandbox.ai.uky.edu`, realm `iam.ai.uky.edu/realms/Astral`). It is a
**checklist to validate an existing deployment against**, not a request to
change backend behavior. The clients are only production-ready if this posture
holds and fails closed when it does not.

> **Secret hygiene**: This document NEVER prints a secret value. Every "value"
> below is a *rule* the operator verifies against the live `.env` and realm.
> Secrets live only in the deployment `.env` (runtime-only, never baked into the
> image — see [docs/production-deployment.md](../../../docs/production-deployment.md)) and, for CI, in GitHub secrets.

---

## 1. Production `.env` checklist

Reference files: [.env.example](../../../.env.example), [docs/production-deployment.md](../../../docs/production-deployment.md).

### 1.1 Environment & auth mode

| Key | Required production value / rule | Why |
|---|---|---|
| `ASTRAL_ENV` | `production` **or unset** (unset == production). Never `development` on the deployed host. | Selects the fail-closed posture; `development` re-enables mock auth and keyless agents. (FR-021) |
| `USE_MOCK_AUTH` | `false` | Mock auth accepts any token as an admin user; a production-mode boot with it on refuses to serve. Boot-gate enforced (exit 78). (FR-021) |

### 1.2 Keycloak realm / authority

| Key | Required production value / rule | Why |
|---|---|---|
| `KEYCLOAK_AUTHORITY` | `https://iam.ai.uky.edu/realms/Astral` (full realm URL) | The OIDC flow and the device-login broker resolve endpoints from this; boot-gate enforced. (FR-018, FR-019) |
| `KEYCLOAK_CLIENT_ID` | `astral-frontend` (the web confidential client) | Web-session OIDC. Boot-gate enforced. |
| `KEYCLOAK_CLIENT_SECRET` | Real confidential-client secret (not empty, not placeholder) | The web OIDC flow cannot operate without it. Boot-gate enforced. |
| `KEYCLOAK_ALLOWED_AZP` | MUST include every native client `azp` plus the web client: `astral-desktop,astral-mobile,astral-watch` (add the web client id if your gate requires it). | The WS/REST token gate rejects any access token whose `azp` is not listed. iOS presents `azp=astral-mobile` (shared with Android); macOS `azp=astral-desktop` (shared with Windows); watch `azp=astral-watch`. Omitting any one fails that client closed. (FR-018, FR-019) — **D8** |
| `AGENT_SERVICE_CLIENT_ID` / `AGENT_SERVICE_CLIENT_SECRET` | Real service-account client id + secret (RFC 8693 delegated dispatch); secret not a placeholder | Agent delegation tokens. |

> **`.env.example` correction (FR-018 / D8)**: the shipped `.env.example`
> comment block and the `KEYCLOAK_ALLOWED_AZP` example currently name the
> rejected dedicated variants `astral-ios` / `astral-macos`. Per the 2026-07-08
> clarification the Apple clients keep the **shared** identities
> `astral-mobile` (iOS) and `astral-desktop` (macOS). The comment and the
> example value MUST be rewritten to `astral-desktop,astral-mobile,astral-watch`
> so a new operator does not register non-existent clients.

### 1.3 Watch device-login (RFC 8628 broker)

| Key | Required production value / rule | Why |
|---|---|---|
| `FF_DEVICE_LOGIN` | `true` | Enables `/api/auth/device/{start,poll,refresh}` for watch QR sign-in; the broker still fails closed unless the §2 realm prerequisites hold. (FR-019, US5) |
| `KEYCLOAK_DEVICE_CLIENTS` | `astral-watch` (must also appear in `KEYCLOAK_ALLOWED_AZP`) | Allow-lists the public client permitted to use the device grant. Watch-only by default. (FR-018) |
| `DEVICE_LOGIN_START_RATE` | Default `10` (per client address per minute) acceptable | Rate-limits the unauthenticated `start` surface. |

### 1.4 Streaming

| Key | Required production value / rule | Why |
|---|---|---|
| `FF_LLM_STREAMING` | `true` | Narrative prose streams over the already-dispositioned `ui_stream_data` frame; kept ON and verified rendering on-device rather than disabled. (FR-023, SC-007) — **D13** |

### 1.5 Secrets that MUST be real, high-entropy values

Every key below MUST be set to a real high-entropy value with **no placeholder,
no empty string, no shipped default**. Generate Fernet keys with
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
Rows marked **[boot-gate]** are enforced by the exit-78 production boot gate;
the rest are operator-required for full, secure functionality.

| Key | Rule | Why |
|---|---|---|
| `WEB_SESSION_ENC_KEY` | Real Fernet key (or `OFFLINE_GRANT_ENC_KEY` set as the accepted fallback) **[boot-gate]** | Durable web sessions are Fernet-encrypted at rest; refused unencrypted. |
| `WEB_SESSION_SECRET` | Real HMAC key; set explicitly for multi-process production (else HKDF-derived from the enc key) | Independent session-cookie signing key across worker processes. |
| `OFFLINE_GRANT_ENC_KEY` | Real Fernet key | Encrypts stored OAuth refresh tokens for offline/unattended grants; absence fails safe (scheduling refuses to store tokens). |
| `CREDENTIAL_ENCRYPTION_KEY` | Real Fernet key | Encrypts agent-scoped OAuth credentials at rest. |
| `MEMORY_HMAC_KEY` | Real high-entropy key | Memory-poisoning defense, MAS payload integrity, transaction tokens. |
| `AGENT_API_KEY` | Real shared secret | Agents present it at registration; production refuses keyless registrations (fail closed). |
| `AUDIT_HMAC_SECRET` | Real high-entropy value, **≠ the shipped `dev-audit-hmac-secret-change-me-in-prod` placeholder** **[boot-gate]** | The audit hash chain is forgeable under the shipped placeholder; the boot gate refuses the placeholder. |
| `KEYCLOAK_CLIENT_SECRET` | Real confidential-client secret **[boot-gate]** | See §1.2. |
| `AGENT_SERVICE_CLIENT_SECRET` | Real service-account secret | See §1.2. |

### 1.6 Proxy & connection-pool sizing

| Key | Required production value / rule | Why |
|---|---|---|
| `FORWARDED_ALLOW_IPS` | The TLS-terminating reverse proxy's address (not the loopback default `127.0.0.1` unless the proxy is co-located) | Only these hosts may set `X-Forwarded-*`; this drives the session cookie `secure` flag and the OIDC `redirect_uri` https derivation. |
| `DB_POOL_MIN` / `DB_POOL_MAX` | `DB_POOL_MAX × orchestrator_process_count` **< Postgres `max_connections`** (leave headroom for admin/replica connections) | Exceeding `max_connections` exhausts the pool under load and the service starts refusing DB work. Feature-052 pooling replaces connect-per-query. (FR-018, edge case "DB pool exhaustion") |
| `DB_POOL_DISABLE` | Unset (leave pooling on) | Kill switch back to the legacy connect-per-query path; not for production. |

---

## 2. Realm operator prerequisites (verify against the live IdP)

These are IdP-side settings, not `.env` keys; the `.env` above fails closed if
any is missing. Cross-reference [docs/keycloak-realm-settings.md](../../../docs/keycloak-realm-settings.md).

1. **Well-known advertises the device endpoint.** Fetching
   `https://iam.ai.uky.edu/realms/Astral/.well-known/openid-configuration` MUST
   include a `device_authorization_endpoint`. Absent ⇒ the broker returns
   `503 device_login_unavailable` and watch QR login fails closed with an
   actionable message. (FR-019)
2. **`astral-watch` has the Device Authorization Grant enabled.** The public
   `astral-watch` client MUST have the OAuth 2.0 Device Authorization Grant
   capability on. **Verified enabled 2026-07-08.** (FR-019, US5)
3. **Apple redirect registered on the SHARED clients.** The Apple OAuth
   redirect `com.personalailabs.astraldeep:/oauth2redirect` MUST be a **Valid
   Redirect URI** on BOTH shared public clients:
   - `astral-mobile` — because **iOS shares `astral-mobile` with the Android client**, and
   - `astral-desktop` — because **macOS shares `astral-desktop` with the Windows client**.

   These clients already carry their Android/Windows redirect URIs; the Apple
   redirect is **added alongside** them (do not replace). Missing the Apple
   redirect on either client makes PKCE sign-in fail closed for that platform.
   (FR-013, FR-019) — **D8/D10**

---

## 3. `astralprims` vocabulary confirmation (FR-020 / D11)

The component vocabulary the Apple clients expect is pinned by the `AstralCore`
`ManifestDriftTests` drift guard (the `ui_protocol.json` guard): **47 push
types / 35 component types / 67 accept types**.

- `backend/requirements.txt` pins `astralprims>=0.2.0`. **0.2.0 is the version
  that introduces the dashboard primitives** (badge/hero/keyvalue/timeline/rating)
  that complete the 35 component types.
- **Action**: confirm the wheel resolved into the production image yields
  exactly the drift guard's 47/35/67. The mechanical check is that the server's
  published vocabulary equals the client's `ui_protocol.json` expectation (the
  drift guard stays green — FR-025, SC-007).
- **Pin explicitly only if it differs.** If the resolved wheel yields a
  different vocabulary, pin `astralprims==<confirmed-version>` (an additive,
  dependency-neutral edit); otherwise leave the `>=0.2.0` floor. Do not
  over-pin without cause. (FR-020, FR-026)

---

## 4. Fail-closed invariant (FR-021)

Production-posture boot (`ASTRAL_ENV` unset or `production`) **exits 78** with a
single consolidated operator checklist in the log if any boot-gated secret is
**missing or a placeholder** — it refuses to serve rather than serving
insecurely. Specifically enforced at boot: `USE_MOCK_AUTH=false`,
`WEB_SESSION_ENC_KEY` (or `OFFLINE_GRANT_ENC_KEY`), `AUDIT_HMAC_SECRET` ≠ the
shipped placeholder, and `KEYCLOAK_AUTHORITY` / `KEYCLOAK_CLIENT_ID` /
`KEYCLOAK_CLIENT_SECRET`. Additionally, dev **mock auth MUST NOT boot in
production**, and agent registrations without a valid `AGENT_API_KEY` are
refused (WS close 1008).

This invariant is **asserted and documented, not changed** by feature 053 — the
backend already behaves this way; the US5 verification confirms the live
deployment satisfies it. The CI production-posture smoke leg proves the gate by
requiring the production-mode boot to exit **exactly 78** on an incomplete
`.env`.


### Fix #246: Documentation & Usage Notes
- Clarified parameter requirements and validation handling for #246.
- Refer to issue details: Add a guided production-safe installer and first-run verification for Astral.
