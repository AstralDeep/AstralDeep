# Production deployment (operator guide)

AstralDeep is a **server-driven UI (SDUI) backend service**: one Python
process serves the web shell, static assets, REST API, WebSocket channel, and
the rendered UI itself on port **8001**. There is no frontend build step, no
Node toolchain, and no separate static server — `astralprims` defines the
primitives, the orchestrator renders them (`backend/webrender/`), and ROTE
adapts the output per device.

Companion docs: [keycloak-realm-settings.md](keycloak-realm-settings.md)
(identity provider), [keycloak_agent_delegation_setup.md](keycloak_agent_delegation_setup.md)
(RFC 8693 agent delegation), [byo-client-agents.md](byo-client-agents.md)
(features 057/058 — user-authored, desktop-hosted BYO agents; `FF_BYO_AGENTS`).

## Fail-closed posture (read this first)

`ASTRAL_ENV` **unset means production**. A production-mode boot refuses to
serve (exit code 78, one consolidated operator checklist in the log) unless:

| Requirement | Why |
|---|---|
| `USE_MOCK_AUTH=false` | Mock auth accepts any token as an admin user. |
| `WEB_SESSION_ENC_KEY` (or `OFFLINE_GRANT_ENC_KEY`) set | Durable web sessions are Fernet-encrypted at rest; refused unencrypted. |
| `AUDIT_HMAC_SECRET` set to a real value | The audit hash chain is forgeable under the shipped placeholder. |
| `KEYCLOAK_AUTHORITY` / `KEYCLOAK_CLIENT_ID` / `KEYCLOAK_CLIENT_SECRET` set | The OIDC flow cannot operate without them. |

Additionally, in production mode:

- **Agent registrations without a valid `AGENT_API_KEY` are refused**
  (WS close 1008). Leaving it unset is safe but means no specialist agents
  come up — the boot log warns about this.
- Unauthenticated shell requests redirect to Keycloak; unauthenticated
  REST/WS requests are refused. Entry requires the `user` or `admin` realm
  role.

Local development opts out explicitly with `ASTRAL_ENV=development`.

## Secrets are runtime-only

The image does **not** bake `.env` (secrets in image layers leak via
`docker history` and registry caches). Configuration enters at runtime:

```bash
docker compose up -d            # uses env_file: .env (already wired)
docker run --env-file .env …    # for non-compose runs
```

Generate the Fernet keys:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## LLM credentials (feature 054 — bring your own)

The product ships with **no LLM credential** and no way to supply a
deployment-wide default for user traffic:

- **Users** connect their own provider through the mandatory first-run
  dialog (or Settings → LLM settings later). The configuration persists
  per-user (`user_llm_config`; API key Fernet-encrypted under
  `CREDENTIAL_ENCRYPTION_KEY`) and applies to all of the user's devices.
- **Admins** configure the deployment-wide **System LLM** (Settings →
  System LLM, admin-only, web) used exclusively for background work —
  scheduled jobs, attachment-parser codegen, knowledge synthesis,
  conversation compaction, workspace combine/condense, job summaries.
  Without it those features skip/fail honestly; user chat is unaffected.
- **Migration from pre-054 deployments**: the legacy env vars
  (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `LLM_MODEL`, `KNOWLEDGE_LLM_MODEL`)
  are **inert** — remove them from the host `.env`. They are never
  migrated into any store: each user configures fresh at next sign-in,
  and an admin sets the System LLM in-app if background features are
  wanted. Boot does not require any LLM configuration.

## BYO client-side agents (features 057/058 — default OFF)

User-authored agents that run on the **user's own desktop** as an isolated child
process and tunnel inward over the client's authenticated UI socket. **Off by
default** and safe to leave off. To enable, set `FF_BYO_AGENTS=true` in the host
`.env` and run `make apply-config` — the flag is read once at import, so a plain
container restart does not reload a changed Compose environment. The apply target
recreates the service and prints only the normalized effective flag value. There
is no admin UI for this boot switch. No new runtime dependency or port is added.
User code is never executed on the orchestrator (pure-AST static validation); it
runs only on an eligible user's desktop and is untrusted at the boundary. Full
enablement, verification, security posture, lifecycle/recovery, compatibility,
rollback, and desktop-host packaging guidance: **[byo-client-agents.md](byo-client-agents.md)**.

## TLS / reverse proxy

The service speaks plain HTTP on `:8001` and expects a TLS-terminating
reverse proxy (nginx, Caddy, Traefik) in front of it in production:

1. Proxy `https://your-host/` → `http://127.0.0.1:8001` and forward
   `X-Forwarded-Proto` / `X-Forwarded-For` (every mainstream proxy's default).
2. The orchestrator trusts those headers only from `FORWARDED_ALLOW_IPS`
   (default `127.0.0.1`; set to your proxy's address or `*` inside a private
   network). This is what makes `request.base_url` https — which drives the
   session cookie's `secure` flag and the OIDC `redirect_uri`.
3. WebSockets: ensure the proxy upgrades `/ws` (and `/agent` if agents
   connect through it). nginx needs the standard
   `proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade";`.
4. Set `PUBLIC_BASE_URL`/`BACKEND_PUBLIC_URL` to the public https origin and
   register `https://your-host/auth/callback` as a valid redirect URI on the
   Keycloak client.

## Conversational voice (feature 065) — production URLs

Voice is fail-closed about transport outside development. The two URLs are
different things and BOTH must be TLS in production:

1. `LIVEKIT_PUBLIC_URL=wss://<voice vhost>` — what CLIENTS join
   (e.g. `wss://sandbox-voice.example.edu`). Plain `ws://` is refused at boot
   (`insecure_public_url`).
2. `LIVEKIT_INTERNAL_URL=https://<voice vhost>` — what the ORCHESTRATOR and
   the VOICE WORKER use. The coordinator derives the worker's RTC URL from it
   (`http`→`ws`, `https`→`wss`) and refuses a plaintext derivation outside
   development — the symptom is every `POST /api/voice/sessions` failing
   `503 invalid_livekit_url`. The compose default
   (`http://livekit:7880`) is for local development only; production MUST
   override it in `.env`.

The reverse proxy for the voice vhost must pass BOTH the WebSocket upgrade
(`/rtc`) and LiveKit's HTTP admin API (`/twirp`) to the LiveKit service —
a plain `proxy_pass` to `livekit:7880` with upgrade headers covers both.
Remember `LIVEKIT_NODE_IP` must be the host's routable address for WebRTC
media, and `VOICE_CONTROL_SECRET` changes require a compose RECREATE (a
`docker restart` keeps the old value; the worker then loops 401 on the
worker-control WebSocket).

## Health probes

| Endpoint | Meaning | Use |
|---|---|---|
| `GET /healthz` | Process is serving | Liveness probe |
| `GET /readyz` | Database answers (`503` + `{"db":"unreachable"}` otherwise) | Readiness probe / compose healthcheck |

Both are ungated (no user data) and excluded from access logs.
`docker-compose.yml` wires `/readyz` as the container healthcheck
(`start_period: 90s` covers agent auto-start).

## Sessions, sign-out, multi-instance

- Sessions live in Postgres (`web_session`, encrypted) — restarts and N>1
  instances do not log anyone out. All instances must share
  `WEB_SESSION_ENC_KEY` (cookie signatures fall back to it via
  `WEB_SESSION_SECRET → WEB_SESSION_ENC_KEY → OFFLINE_GRANT_ENC_KEY`).
- Silent renewal is server-side and never extends the 365-day interactive
  anchor; sign-out revokes the Keycloak refresh token (queued in
  `auth_revocation_queue` and retried when the IdP is unreachable) plus all
  feature-025 offline grants.

## Native clients (Windows, Android, Apple) & device sign-in

- The native clients (feature 044/041/051: `windows-client/`, `android-client/`,
  `apple-clients/` iOS + macOS + watchOS) consume the same public origin as the
  browser — no extra ports or services. They authenticate as **public PKCE
  clients**: `astral-desktop` (Windows + macOS), `astral-mobile`
  (Android + iOS), `astral-watch` (watch). All must appear in
  `KEYCLOAK_ALLOWED_AZP` and exist in the realm per
  [keycloak-realm-settings.md](keycloak-realm-settings.md).
- **Watch QR sign-in (feature 051)** is the backend-brokered OAuth 2.0 Device
  Authorization Grant (RFC 8628): the watch calls
  `POST /api/auth/device/{start,poll,refresh}` and never contacts the IdP.
  Requirements: `FF_DEVICE_LOGIN=true` (default) and the **device grant
  enabled on the `astral-watch` client** in the realm (a per-client toggle;
  see keycloak-realm-settings.md §051). Fail-closed: flag off, IdP
  unreachable, or grant not enabled all yield a clean 503 with an actionable
  message on the watch — never a hung or partial session.
- Device-login start/poll are rate-limited per client address and codes are
  single-use + TTL-bound; the lifecycle is audited (`auth` class,
  `auth.device_login_*`). Token material is never logged.
- Apple/Android/Windows sign-out calls `POST /api/auth/logout` with its
  client id; revocation is queued offline-tolerantly
  (`auth_revocation_queue.client_id`) exactly like the web client.
- iOS/macOS refresh directly against the IdP token endpoint (Windows
  precedent); the watch refreshes via the backend broker (single TLS peer).
  Silent refresh never extends the 365-day interactive anchor.

## Apple clients (App Store)

Feature 053 ships the `apple-clients/` family — iOS + macOS (one multiplatform
`AstralApp` target) plus an embedded watchOS companion (`AstralWatch`) — to the
App Store as a single Universal Purchase record (bundle id
`com.personalailabs.astraldeep`). Two things are operator-facing: the backend
`.env` the shipped apps expect, and the signed release pipeline.

### Production `.env` the Apple clients depend on

A stock App Store build compiles in `https://sandbox.ai.uky.edu` and
`https://iam.ai.uky.edu/realms/Astral` as its endpoint
(`apple-clients/Config/Release.xcconfig` → both Info.plists → `AstralConfig`).
The endpoint can be repointed at runtime (FR-011 override) or by rebuilding, but
a stock build talks to that exact host — so the production posture there must
satisfy:

| Key | Value | Why the Apple clients need it |
|---|---|---|
| `ASTRAL_ENV` | unset (== production) | Fail-closed; mock auth refuses to boot. |
| `USE_MOCK_AUTH` | `false` | Real tokens only; mock auth would admit anyone as admin. |
| `KEYCLOAK_AUTHORITY` | `https://iam.ai.uky.edu/realms/Astral` | Must match the authority the Release build ships with. |
| `KEYCLOAK_ALLOWED_AZP` | includes `astral-mobile`, `astral-desktop`, **and** `astral-watch` | iOS→`astral-mobile`, macOS→`astral-desktop`, watch→`astral-watch`; a token whose `azp` is not listed is rejected. |
| `KEYCLOAK_DEVICE_CLIENTS` | `astral-watch` | Only the watch may use the backend-brokered device grant. |
| `FF_DEVICE_LOGIN` | `true` | Enables `POST /api/auth/device/*`; off ⇒ watch QR sign-in returns a clean 503. |
| `FF_LLM_STREAMING` | `true` (default) | Token-wise narrative streaming the clients render live; any provider error falls back to the non-streamed call. |
| high-entropy secret set | `WEB_SESSION_ENC_KEY` / `OFFLINE_GRANT_ENC_KEY` (Fernet), `AUDIT_HMAC_SECRET`, `AGENT_API_KEY`, `KEYCLOAK_CLIENT_SECRET` | The exit-78 boot gate refuses placeholders. |
| `FORWARDED_ALLOW_IPS` | the TLS proxy's address | Makes `request.base_url` https so the OIDC `redirect_uri` the clients round-trip is https. |

Also size the connection pool for the added client fan-in: **`DB_POOL_MAX` ×
(app process count) must stay below Postgres `max_connections`**. Each instance
holds up to `DB_POOL_MAX` connections (default 10); N app processes plus the
`postgres` service's own overhead have to fit under the server ceiling, or a
sign-in storm exhausts it.

### Realm prerequisites

Per [keycloak-realm-settings.md](keycloak-realm-settings.md) §051 (native Apple
clients):

- **`astral-mobile`** (shared with Android) serves iOS and **`astral-desktop`**
  (shared with Windows) serves macOS — both standard flow + PKCE. No realm
  change beyond registering the Apple redirect URIs already documented for those
  shared clients.
- **`astral-watch`** is a dedicated *public* client with **OAuth 2.0 Device
  Authorization Grant** enabled (Capability config). Append it to
  `KEYCLOAK_ALLOWED_AZP` and keep `KEYCLOAK_DEVICE_CLIENTS=astral-watch`.
- All three clients must exist in the realm; the watch never contacts the IdP
  directly (the backend brokers the device grant).

### Apple release runbook

The Apple release pipeline is `.github/workflows/apple-release.yml` — a separate
workflow from `ci.yml` (the six backend gates are untouched; `apple-ci.yml`
gains only a `generate_app_icons.py --check` step). It runs on `macos-15` and
does **archive → sign → export → validate → upload**. It does **not** submit for
review (see below).

**Trigger.** Three ways in:
1. **Merge to `main` that changes `apple-clients/**`** — auto-releases. A cheap
   `gate` job checks the push's diff; if `apple-clients/**` changed it runs the
   full archive → upload, building the project's current `MARKETING_VERSION`.
   Ordinary backend-only merges do NOT upload. This is the everyday path — bump
   `MARKETING_VERSION` in the Xcode project as part of the client change and the
   new build uploads on merge.
2. **Push a tag `apple-v*`** that equals `apple-v$MARKETING_VERSION` exactly
   (a mismatched tag fails the guard) — a versioned release of record.
3. **`workflow_dispatch`** — manual release from the `main`/default ref only
   (e.g. to re-upload a build for an already-merged version).

The `apple-v*` namespace is deliberately disjoint from the Windows release's
`v*` trigger — a `v-apple-*` tag would double-fire that workflow — so do not
rename it. A `paths:` filter is intentionally NOT used (it interacts unreliably
with tag pushes); the `apple-clients/**` check lives in the `gate` job so tag
and dispatch runs are never path-filtered. The tag-vs-`MARKETING_VERSION` guard
runs only on tag pushes. The build number is `$GITHUB_RUN_NUMBER`, passed to
`xcodebuild` as `CURRENT_PROJECT_VERSION` (both Info.plists already read it — no
agvtool rewrite), so every run gets a unique, monotonic build number.

**Required secrets (names only — never commit or echo values).** The workflow
fails fast, before any signing step, if any of these seven repository secrets is
unset:

| Secret | Purpose |
|---|---|
| `APPLE_TEAM_ID` | Apple Developer Team id (also injected as `ASTRAL_DEVELOPMENT_TEAM`). |
| `APPLE_DISTRIBUTION_CERT_P12_BASE64` | Base64 of the Apple Distribution certificate `.p12`. |
| `APPLE_CERT_PASSWORD` | Password for that `.p12`. |
| `APPLE_PROVISION_PROFILE_BASE64` | Base64 tar carrying **all three** App Store profiles. |
| `ASC_KEY_ID` | App Store Connect API key id. |
| `ASC_ISSUER_ID` | App Store Connect API issuer id. |
| `ASC_KEY_P8_BASE64` | Base64 of the App Store Connect API `.p8` private key. |

Rendering the export-options plists additionally consumes three profile-**name**
secrets — `APPLE_PROFILE_IOS`, `APPLE_PROFILE_MACOS`, `APPLE_PROFILE_WATCH` —
through `Scripts/render_export_options.py` (stdlib only; exits non-zero on any
unset placeholder).

**Three provisioning profiles.** A `.mobileprovision` is per bundle-id *and*
platform, so the shared `com.personalailabs.astraldeep` id needs one App Store
profile each for iOS, macOS, and watchOS. All three ride inside
`APPLE_PROVISION_PROFILE_BASE64`; the import step refuses to proceed if fewer
than three land.

**Two archives, one record.** The iOS archive embeds `Watch/AstralWatch.app`
(asserted present); the macOS archive must contain no watch app (asserted absent
— the embed phase is platform-filtered to iOS). Both are `-exportArchive`-d,
`altool --validate-app`-ed, and `altool --upload-app`-ed into the one Universal
Purchase App Store Connect record. There is **no `notarytool` step** — App Store
(including Mac App Store) builds are signed-checked by Apple after upload;
notarization is the outside-the-store Developer-ID path.

**Submission is operator-performed.** The pipeline stops at a validated,
uploaded build. Pressing **Submit for Review** in App Store Connect requires a
complete store listing — screenshots for iPhone 6.9", iPad 13", Mac, and Apple
Watch; description; privacy-policy URL; age rating — which only the operator can
author, and Apple's submission API refuses an incomplete listing. Outstanding
operator work: the four device-class screenshots, the App Store Connect record +
listing copy, the operator's Team id / distribution certificate / three
provisioning profiles / ASC API key, and the on-device verification evidence.

## Database

- Postgres 17 (compose service `postgres`, named volume `pgdata`).
- Schema migrations are idempotent and run automatically at boot
  (`shared/database.py::_init_db`) — no migration step to operate. Since
  feature 052 a `schema_meta` revision marker lets boots with a current
  schema skip the full migration pass; to force a full re-run once, execute
  `DELETE FROM schema_meta WHERE key='revision';` and restart.
- Connections are pooled (feature 052): `DB_POOL_MIN` (default 2) and
  `DB_POOL_MAX` (default 10) size the shared pool; `DB_POOL_DISABLE=1`
  reverts to the legacy connection-per-query behavior as a kill switch.
- Back up `pgdata` and the `backend/data` bind mount (uploads, agent keys).

## Performance knobs (feature 052)

- `FF_LLM_STREAMING` (default on) — streams the narrative answer token-wise
  to all clients when the configured model supports it; any provider error
  falls back to the non-streamed call automatically. Set `false` to disable.
- `FF_PHI_WARM` (default on) — pre-loads the PHI analyzer in a background
  thread at boot so the first personalization write does not stall.
- `JWKS_REFRESH_SECONDS` (default 500) — background refresh interval for the
  identity-provider signing keys warmed at boot; token validation stays
  fail-closed regardless.
- `UI_DESIGNER_MAX_ROUNDS` — the adaptive UI designer now defaults to **1**
  design pass per turn (was 3); raise it to restore multi-round refinement.
  Components are always delivered to clients before the designer runs.
- Static assets are served with immutable per-file versioned URLs; a deploy
  changes the URLs, so no cache purge is ever needed.

## Logging & observability

- `LOG_LEVEL` (default `info`) controls uvicorn/app verbosity; health-probe
  and agent-card polls are filtered out of access logs.
- Timing spans (feature 052) are logged as `perf <name> duration_ms=<int>`
  lines (surface renders, sign-in phases, chat-turn phases, boot phases);
  summarize with `python scripts/perf_report.py` (run from `backend/`,
  feeding it the app log).
- The tamper-evident audit trail (per-user HMAC hash chain) is queryable via
  `GET /api/audit` (per-user) and verifiable server-side:
  `python -m audit.cli verify-chain --user-id <id>`.

## Deploying to sandbox.ai.uky.edu (GHCR pull)

CI (feature 029, `.github/workflows/ci.yml`) publishes the production image
to GitHub Container Registry on every push to `main` that passes all gates:
an immutable `ghcr.io/<owner>/<repo>:sha-<commit>` tag plus a moving
`:latest`. The sandbox host **pulls the verified image** instead of building
locally — the bytes that passed CI are the bytes that serve.

### 1. Pull the image

```bash
docker login ghcr.io -u <github-username>        # PAT with read:packages
docker pull ghcr.io/<owner>/<repo>:sha-<commit>  # always pin the immutable tag
```

Deploy by `sha-<commit>` (the tag CI stamped on the exact verified build);
treat `:latest` as a convenience pointer only — never as the deployed ref.

### 2. Compose override — `image:` instead of `build:`

Keep the repo's `docker-compose.yml` (its `env_file`, volumes, healthcheck
and `depends_on` all still apply) and add a `docker-compose.override.yml`
on the host that swaps the local build for the registry image:

```yaml
# docker-compose.override.yml (sandbox host)
services:
  astraldeep:
    image: ghcr.io/<owner>/<repo>:sha-<commit>
    build: !reset null   # drop the build: block (compose >= 2.24)
```

On older compose without `!reset`, omit that line and start with
`docker compose up -d --no-build` so the registry image is used as-is.

### 3. Host `.env` posture

Everything in [Fail-closed posture](#fail-closed-posture-read-this-first)
applies; for sandbox.ai.uky.edu specifically:

```bash
# Public origin (TLS proxy in front — see below)
PUBLIC_BASE_URL=https://sandbox.ai.uky.edu
BACKEND_PUBLIC_URL=https://sandbox.ai.uky.edu

# Identity — realm + client settings per docs/keycloak-realm-settings.md
KEYCLOAK_AUTHORITY=https://iam.ai.uky.edu/realms/<realm>
KEYCLOAK_CLIENT_ID=astral-frontend
KEYCLOAK_CLIENT_SECRET=<client credentials tab>

# Native clients — tokens from the Windows/macOS (astral-desktop), Android/iOS
# (astral-mobile) and watch (astral-watch) public clients are ACCEPTED ONLY when
# their azp is listed here (empty/unset ⇒ web-only: every native sign-in is
# rejected while the web keeps working, and the Android/Apple release builds ship
# pointing at this very host). Client provisioning:
# docs/keycloak-windows-client-setup.md, docs/keycloak-android-client-setup.md,
# and docs/keycloak-realm-settings.md §051 (Apple).
KEYCLOAK_ALLOWED_AZP=astral-desktop,astral-mobile,astral-watch

# Watch QR sign-in (device grant) — Apple watchOS + any watch client
KEYCLOAK_DEVICE_CLIENTS=astral-watch
FF_DEVICE_LOGIN=true

# Production posture — the exit-78 boot gate checks all of these
# (leave ASTRAL_ENV unset: unset == production, fail closed)
USE_MOCK_AUTH=false
WEB_SESSION_ENC_KEY=<generated Fernet key>
OFFLINE_GRANT_ENC_KEY=<generated Fernet key>
AUDIT_HMAC_SECRET=<high-entropy value>
AGENT_API_KEY=<random secret>

# Trust X-Forwarded-* only from the TLS proxy
FORWARDED_ALLOW_IPS=<proxy ip>

# Database stays compose-internal (service `postgres`)
DB_USER=astral
DB_PASSWORD=<strong password>
DB_NAME=astraldeep
DB_PORT=5432
```

### 4. Reverse proxy + Keycloak client

- TLS terminates at the proxy: `https://sandbox.ai.uky.edu/` →
  `http://127.0.0.1:8001`, forwarding `X-Forwarded-Proto`/`X-Forwarded-For`.
- The orchestrator trusts those headers **only** from `FORWARDED_ALLOW_IPS`
  (set it to the proxy's address — this is what makes the session cookie
  `secure` and the OIDC `redirect_uri` https).
- The proxy must upgrade WebSockets on `/ws` (and `/agent` if agents connect
  through it).
- On the Keycloak `astral-frontend` client, register
  `https://sandbox.ai.uky.edu/auth/callback` as a valid redirect URI and
  `https://sandbox.ai.uky.edu/` as a post-logout redirect URI
  ([keycloak-realm-settings.md](keycloak-realm-settings.md)).

The exit-78 boot gate is the final guard: if the host `.env` is incomplete,
the pulled image refuses to serve and prints one consolidated checklist in
`docker compose logs astraldeep` — fix and `docker compose up -d` again.

## Deployment checklist

```text
[ ] Image pulled from GHCR by its immutable sha-<commit> tag (not :latest,
    not a local build)
[ ] docker-compose.override.yml points services.astraldeep.image at that tag
    (build: dropped or compose started with --no-build)
[ ] ASTRAL_ENV unset (or =production) on the host — NOT development
[ ] USE_MOCK_AUTH=false
[ ] WEB_SESSION_ENC_KEY + OFFLINE_GRANT_ENC_KEY generated (Fernet)
[ ] AUDIT_HMAC_SECRET high-entropy (placeholder is refused at boot)
[ ] AGENT_API_KEY set (agents refuse to register without it)
[ ] KEYCLOAK_* configured; realm per docs/keycloak-realm-settings.md
    (incl. Remember Me OFF, Offline Session ≥ 365 d, roles user/admin)
[ ] KEYCLOAK_ALLOWED_AZP lists the native clients (astral-desktop,
    astral-mobile, astral-watch) — unset means Windows/macOS/Android/iOS/watch
    sign-ins are rejected
[ ] Native client redirect URIs registered: 127.0.0.1 loopback on
    astral-desktop (Windows + macOS), com.personalailabs.astraldeep:/oauth2redirect
    on astral-mobile (Android + iOS) (see the per-client setup docs)
[ ] PUBLIC_BASE_URL/BACKEND_PUBLIC_URL = public https origin
[ ] Reverse proxy terminates TLS, forwards X-Forwarded-*, upgrades /ws
[ ] FORWARDED_ALLOW_IPS = proxy address
[ ] https://host/auth/callback registered on the Keycloak client
[ ] docker compose up -d; container healthcheck goes healthy (/readyz)
[ ] Boot log shows no posture warnings; GET / redirects to Keycloak
[ ] Native clients: astral-desktop/astral-mobile/astral-watch in
    KEYCLOAK_ALLOWED_AZP; device grant enabled on astral-watch
    (watch QR sign-in); FF_DEVICE_LOGIN=true
```
