# Keycloak setup for the native Windows client (`astral-desktop`)

> Operator guide. Create a **dedicated public Keycloak client** for the native
> Windows desktop client (`windows-client/`) and tell the orchestrator to accept
> it. This is the by-the-book native-app posture (RFC 8252 / OAuth 2.0 for Native
> Apps) and keeps the desktop and web auth surfaces isolated.
>
> Realm-wide settings (offline sessions, token lifespans, the required `user`/
> `admin` roles) are unchanged from
> [keycloak-realm-settings.md](keycloak-realm-settings.md) — read that first if
> the realm is brand new. This document only adds the desktop client.

---

## Why a separate client?

The web app uses the **confidential** `astral-frontend` client (it has a
secret, kept server-side). A desktop app can't safely hold a secret, so it uses
its own **public** client and proves possession of the auth code with **PKCE**
instead. Benefits:

- The desktop exchanges the auth code and refreshes tokens **directly against
  Keycloak** — no dependency on the orchestrator's BFF.
- Web and desktop sign-ins are **isolated**: revoking or reconfiguring one never
  touches the other.
- Tokens minted for the desktop carry `azp=astral-desktop`, which the
  orchestrator accepts through a small **allow-list** (below) — the web client's
  single-`azp` check is left intact.

---

## Part 1 — Create the `astral-desktop` client in Keycloak

Sign in to the Keycloak Admin Console as a realm admin and select your realm
(the same realm as `astral-frontend`).

### 1. Create the client

`Clients` → `Create client`.

| Field | Value |
|---|---|
| Client type | **OpenID Connect** |
| Client ID | **`astral-desktop`** |
| Name | `AstralDeep Desktop (Windows)` *(optional, display only)* |

Click **Next**.

### 2. Capability config

| Setting | Value | Why |
|---|---|---|
| **Client authentication** | **Off** | Makes this a **public** client — native apps cannot hold a secret. |
| **Authorization** | Off | Not used. |
| **Standard flow** | **On** | Authorization Code flow (the browser login step). |
| **Direct access grants** | Off | No password grant; login is via the browser. |
| **Implicit flow** | Off | Deprecated; never use. |
| **Service accounts** | Off | Not a machine client. |

Click **Next**, then on **Login settings**:

### 3. Login settings (redirect URIs)

The desktop opens a **loopback** HTTP listener on `127.0.0.1` on a random port
(RFC 8252 §7.3) and uses `http://127.0.0.1:<port>/callback` as the redirect.
Keycloak must allow any loopback port:

| Field | Value |
|---|---|
| **Valid redirect URIs** | `http://127.0.0.1/*` |
| (add a second) | `http://localhost/*` |
| **Valid post-logout redirect URIs** | `http://127.0.0.1/*` |
| **Web origins** | leave empty (the token call is a native HTTP POST, not a browser fetch — no CORS) |

Click **Save**.

### 4. Require PKCE (S256)

Open the client → **Advanced** tab → **Advanced settings**:

| Setting | Value |
|---|---|
| **Proof Key for Code Exchange Code Challenge Method** | **`S256`** |

Click **Save**. This makes Keycloak reject any auth-code exchange that doesn't
present a valid PKCE verifier — the security guarantee that lets a secretless
public client be safe.

### 5. Client scopes (roles + offline refresh)

Open the client → **Client scopes** tab. The defaults assigned to a new client
are correct; just confirm:

- **`roles`** is present as **Default** — so the user's realm roles
  (`realm_access.roles`) ride in the access token. The orchestrator needs to see
  `user` or `admin` here.
- **`offline_access`** is present as **Optional** — the desktop requests the
  `offline_access` scope so it gets an **offline refresh token** for silent
  re-login. (It is a realm default optional scope; add it if your realm removed
  it.)

> No audience mapper is needed: the orchestrator does **not** enforce strict
> `aud`; it validates `azp` (above) plus the `user`/`admin` role.

### 6. Confirm the user has a role

The signing-in account must have the realm role **`user`** (or **`admin`**) —
exactly the same requirement as the web app. A token with neither is rejected at
the WebSocket handshake. `Realm roles` → assign `user` to the account if needed
(`Users` → user → `Role mapping` → `Assign role`).

---

## Part 2 — Tell the orchestrator to accept the desktop client

The orchestrator accepts the web client's `azp` plus any client ids listed in
**`KEYCLOAK_ALLOWED_AZP`** (comma-separated). Add `astral-desktop`.

This is read by both auth gates (the WebSocket `register_ui` handshake and the
REST dependencies). With the list empty, behavior is unchanged (web client
only), so this is a safe additive change.

---

## Part 3 — Fill these into `.env`

Open the repo-root **`.env`** and set the values below. Replace every
`<…>` placeholder; leave the rest of the file as-is.

```bash
# ── Real Keycloak auth (turn OFF mock auth) ───────────────────────────────
USE_MOCK_AUTH=false
ASTRAL_ENV=development          # local verification: real auth, but keeps the
                                # keyless local-agent + relaxed-secret dev path.
                                # For a real deployment use production posture —
                                # see docs/production-deployment.md.

# ── Realm + clients ───────────────────────────────────────────────────────
KEYCLOAK_AUTHORITY=<https://YOUR-KEYCLOAK-HOST/realms/YOUR-REALM>
KEYCLOAK_CLIENT_ID=astral-frontend           # the web (confidential) client
KEYCLOAK_CLIENT_SECRET=<astral-frontend secret>   # from the astral-frontend "Credentials" tab

# NEW — accept the desktop's dedicated public client at the orchestrator:
KEYCLOAK_ALLOWED_AZP=astral-desktop

# ── Windows tools agent auth (client-hosted A2A) ──────────────────────────
# Random shared secret, used in BOTH directions: the desktop's Windows-tools
# agent presents it when it registers, AND the orchestrator presents it as
# X-Astral-Agent-Key when it dials the agent's card / /agent endpoints. The
# desktop refuses to run the listener at all without it (16+ ASCII chars, no
# dev carve-out) — it serves file-write and command-exec tools.
AGENT_API_KEY=<random-string>

# Optional. The orchestrator sends the key ONLY to declared destinations:
# loopback, the Docker host aliases, A2A_EXTERNAL_AGENTS entries, and hosts
# listed here. Needed if the desktop agent is reached at some other address.
# (register_external_agent takes a user-supplied URL, and this key can register
# any agent id — so it must never travel to a host the operator did not name.)
#AGENT_KEY_TRUSTED_HOSTS=desktop.lan
```

Generate the agent key with, e.g.:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### What the desktop client itself needs

The desktop is a separate process (it runs on the Windows machine, not in
Docker). It reads these from its environment / CLI — the defaults are already
correct, so usually you only need the authority:

| Setting | Default | Notes |
|---|---|---|
| `KEYCLOAK_AUTHORITY` / `--authority` | *(none)* | **Required** for real login — same realm URL as above. |
| `ASTRAL_CLIENT_ID` / `--client-id` | `astral-desktop` | The dedicated public client. |
| `ASTRAL_WS_URL` / `--url` | `ws://127.0.0.1:8001/ws` | Orchestrator WebSocket. |
| `AGENT_API_KEY` | *(none)* | **Required** for the Windows-tools agent, in both directions — it registers with it and refuses inbound requests without it. 16+ ASCII chars, not a placeholder; without it the tools simply do not run. |
| `ASTRAL_AGENT_BIND` | `0.0.0.0` | Interface for the tools listener. Keep `0.0.0.0` when the orchestrator is in Docker (`host.docker.internal` cannot reach a loopback bind); set `127.0.0.1` when it runs natively. |

> The desktop does **not** read `.env` automatically. When verifying locally
> it's launched with these passed in (the run step handles that); for end users
> they're baked into the launch shortcut or the machine environment.

---

## Part 4 — Verify

1. Bring up the stack: `docker compose up -d --build`.
2. Confirm real auth is active — the orchestrator logs
   `Mock auth disabled — Keycloak JWKS validation active` at boot, and
   `/readyz` returns 200.
3. Launch the desktop client with `--authority <realm-url>`. The system browser
   opens to Keycloak; sign in.
4. The orchestrator log should show, in order:
   - `UI registered: <your-username>` (token validated, `azp=astral-desktop`
     accepted),
   - a `ui_render` with the welcome canvas,
   - on a chat: `ui_render` / `ui_upsert` carrying SDUI components,
   - `register_external_agent` → the Windows-tools agent discovered.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Browser shows **"Invalid parameter: redirect_uri"** | Loopback redirect not allowed on the client | Add `http://127.0.0.1/*` (and `http://localhost/*`) to **Valid redirect URIs**. |
| Login completes but the app shows **auth_required** / disconnects | Orchestrator rejected the `azp` | Ensure `KEYCLOAK_ALLOWED_AZP=astral-desktop` is set **and** the orchestrator was restarted to pick it up. |
| Token exchange fails with **"client_secret … invalid"** / `unauthorized_client` | The client is **confidential**, not public | Set **Client authentication → Off** on `astral-desktop`. |
| Login works but app says **no access** | Account lacks the `user`/`admin` realm role | Assign the `user` role to the account. |
| `invalid_grant` / PKCE error at the token endpoint | PKCE method mismatch | Set **PKCE Code Challenge Method = `S256`** on the client. |
| Windows-tools agent never registers | `AGENT_API_KEY` mismatch, or the orchestrator (in Docker) can't reach the host | Match `AGENT_API_KEY` on both sides; ensure `host.docker.internal` resolves (Docker Desktop), or set `ASTRAL_AGENT_HOST`. |
| Orchestrator logs **"refused the orchestrator's credential (401)"** | The agent got no key or the wrong one | The two `AGENT_API_KEY` values differ — or the agent's host is not loopback / a Docker alias, so the orchestrator withheld the key: add that host to `AGENT_KEY_TRUSTED_HOSTS`. |
| Desktop shows **"Windows tools are off: set AGENT_API_KEY"** | No key, or one under 16 chars / a placeholder | Set a real key on the desktop (`secrets.token_urlsafe(32)`) and relaunch. There is no dev carve-out — the listener will not run unauthenticated. |

---

## Security notes

- The `astral-desktop` client holds **no secret**; PKCE `S256` is what protects
  the auth-code exchange. Do not turn Client authentication on for it.
- The desktop talks to Keycloak directly over **HTTPS** for the token exchange;
  the loopback `http://127.0.0.1` redirect is local-only and standard for native
  apps (RFC 8252).
- `KEYCLOAK_ALLOWED_AZP` should list only **first-party** client ids you control.
- For a real internet-facing deployment, run the orchestrator in production
  posture (TLS proxy, real secrets, `ASTRAL_ENV` unset/`production`) per
  [production-deployment.md](production-deployment.md); the desktop client config
  above is unchanged.
