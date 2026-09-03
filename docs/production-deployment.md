# Production deployment (operator guide)

AstralDeep is a **server-driven UI (SDUI) backend service**: one Python
process serves the web shell, static assets, REST API, WebSocket channel, and
the rendered UI itself on port **8001**. There is no frontend build step, no
Node toolchain, and no separate static server — `astralprims` defines the
primitives, the orchestrator renders them (`components/AstralProjection/backend/webrender/`), and ROTE
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
- **`AGENT_API_KEY` now authenticates BOTH directions.** As well as gating
  inbound registrations, the orchestrator presents it as the
  `X-Astral-Agent-Key` header when it dials *out* to an agent's card and
  `/agent` endpoints, so a client-hosted agent (e.g. the Windows tools agent,
  which reads and writes files and runs commands on a user's PC) can tell this
  orchestrator apart from any other host that reaches its port. The key is sent
  only after the peer answers an unauthenticated probe with the
  `AstralAgentKey` challenge, and only to a destination listed under
  `AGENT_KEY_TRUSTED_HOSTS` (see below).
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

## Server-hosted generated agents (features 027/031) — pre-execution gate

Chat capability gaps (`create_capability`) and uncovered attachment uploads
(auto-parsers) generate agent code with an LLM and run it on the **server**.
Codegen is available to every authenticated user — there is no role gate on
asking for it. What is gated is the *output*:

- A static analyzer (`code_security.CodeSecurityAnalyzer`) inspects every
  generated file, and any finding at **HIGH or CRITICAL** refuses the code
  **before it is imported, called, validated, or spawned**. HIGH covers
  `os.environ`/`os.getenv` access, `globals()`/`setattr()`, and obfuscation
  patterns; CRITICAL covers `eval`/`exec`, `subprocess`, raw sockets, unsafe
  deserialization, and class-hierarchy escapes. The same floor re-runs on
  every LLM auto-fix round and on live-agent revisions, so a second-round
  payload gets the same treatment as the first.
- Approval is downstream of that gate, never upstream of it: a refused draft
  is never executed to produce the card an owner would approve. Auto-created
  attachment parsers additionally require **admin** approval before going
  fleet-wide (the uploader cannot self-approve).
- `FF_SANDBOX_CODEGEN` defaults **ON**: the draft/parser subprocess gets a
  secret-scrubbed environment (`DATABASE_URL`, `CREDENTIAL_ENCRYPTION_KEY`,
  provider keys, …) plus rlimits on POSIX. Set it to `false` only to restore
  the legacy secret-inheriting child, and expect to explain why.

To disable server-side generation entirely, set `FF_AGENTIC_CREATION=false`
and `FF_ATTACHMENT_AUTOPARSE=false`.

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

## Conversational voice (features 065/066/075) — deployment topology contract

This section is the FR-037 production topology contract: the reverse-proxy
voice host rules, the environment contract (including worker closure digest
provenance and coordinator replica identity), and the diagnosis runbook.

### Production URLs

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

### Reverse-proxy voice host rules

The voice vhost (e.g. `sandbox-voice.<domain>`) terminates TLS and forwards
to the LiveKit container. It must pass BOTH the WebSocket upgrade (`/rtc`)
and LiveKit's HTTP admin API (`/twirp`) — a plain `proxy_pass` to
`livekit:7880` with upgrade headers covers both:

```apache
# Apache (sandbox topology); nginx equivalent needs the same two behaviors.
<VirtualHost *:443>
  ServerName sandbox-voice.example.edu
  SSLEngine on
  RewriteEngine on
  RewriteCond %{HTTP:Upgrade} =websocket [NC]
  RewriteRule ^/(.*) ws://livekit:7880/$1 [P,L]
  ProxyPass        / http://livekit:7880/
  ProxyPassReverse / http://livekit:7880/
</VirtualHost>
```

WebRTC MEDIA does not traverse the proxy: `LIVEKIT_NODE_IP` must be the
host's routable address, and the UDP media range (`50000-50099` in the
bundled config, plus TCP 7881 fallback) must be reachable from clients.
The MAIN app vhost proxies `/api/voice/*` REST plus the worker-control
WebSocket (`/api/voice/worker-control`) to the orchestrator like every
other `/api` route. The watchOS PCM bridge is the one exception: the
WORKER serves `/api/voice/watch-bridge` itself on
`VOICE_WATCH_BRIDGE_LISTEN_PORT` (compose publishes `127.0.0.1:7890`), so
`VOICE_WATCH_BRIDGE_PUBLIC_URL`'s host needs its own upgrade-capable proxy
rule to the worker's port — proxying that path to the orchestrator 404s.
The voice vhost is only for LiveKit RTC.

### Environment contract

| Variable | Holder | Contract |
|---|---|---|
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | orchestrator + LiveKit | Grant-minting HMAC pair; never reaches clients or the worker env. |
| `LIVEKIT_PUBLIC_URL` | orchestrator | `wss://<voice vhost>` in production (boot-refused otherwise). |
| `LIVEKIT_INTERNAL_URL` | orchestrator | `https://<voice vhost>` in production; worker RTC URL is derived from it. |
| `LIVEKIT_NODE_IP` | LiveKit | Host's routable address advertised for WebRTC media. |
| `VOICE_CONTROL_SECRET` | orchestrator + worker | Worker-admission challenge HMAC. Rotation requires a compose **recreate** of BOTH containers (see runbook). |
| `VOICE_UI_BINDING_SECRET` | orchestrator only | User/device control-binding HMAC; never copied into the worker. |
| `VOICE_WORKER_CLOSURE_SHA256` | orchestrator + worker | **Closure digest provenance**: the sha256 of `backend/voice_agent/CLOSURE.json` AT THE COMMIT THE DEPLOYED WORKER IMAGE WAS BUILT FROM. `CLOSURE.json` is deliberately NOT baked into the worker image (the manifest describes the image's inputs; `tooling/voice-worker/closure_manifest.py` hard-fails if it ever becomes one), so the digest is recomputed REPO-SIDE at that commit — `python tooling/voice-worker/closure_manifest.py verify` validates the checked-in manifest and prints its digest, or `sha256sum backend/voice_agent/CLOSURE.json` at the exact deployed commit. Both containers must carry the SAME value; a stale pin refuses registration with `closure_mismatch`. The all-zero digest is a development-only marker; production boot refuses it (`unapproved_voice_worker_closure`). |
| `VOICE_COORDINATOR_REPLICA_ID` | orchestrator | **Coordinator replica identity**: stable, unique per orchestrator replica (e.g. `voice-coordinator-prod-1`). Owns durable session leases; boot-refused when unset in production (`missing_voice_replica_id`). Two replicas sharing one id would fence each other's sessions at startup/shutdown. |
| `VOICE_WORKER_IDENTITY` / `VOICE_WORKER_MAX_SESSIONS` | worker | Stable worker identity (re-registration replaces the prior socket) and its capacity offer. |
| `VOICE_SPEECH_BASE_URL` / `VOICE_SPEECH_API_KEY` | worker only | Speech endpoint (ASR + TTS routes). In compose these are wired from `OPENAI_BASE_URL`/`OPENAI_API_KEY`; the orchestrator's own copies are blanked (054). |
| `VOICE_SPEECH_BACKEND` | orchestrator only | Server-owned process-lifetime selector. Exact values are `llm_factory` and `client_local`; the value is never projected into the worker or accepted from a client. |
| `VOICE_WATCH_BRIDGE_PUBLIC_URL` / `VOICE_WATCH_BRIDGE_PORT` | orchestrator/worker | watchOS PCM bridge public URL + port. |
| `VOICE_MAX_WORKERS` / `VOICE_MAX_SESSIONS_PER_WORKER` / `VOICE_MAX_TOTAL_SESSIONS` | orchestrator | Pool bounds (defaults 8 / 4 / 100). |
| `FF_CONVERSATIONAL_VOICE` | orchestrator | Feature flag; read once at import — toggling requires a container recreate. |

### Speech backend selection, drain, and rollback

The deployment, not a user or device, owns speech selection. The only accepted
settings are `VOICE_SPEECH_BACKEND=llm_factory` for the existing remote worker
path and `VOICE_SPEECH_BACKEND=client_local` for supported on-device speech.
Missing preserves `llm_factory` for legacy deployments. An explicit empty,
unknown, or malformed value makes voice unavailable while typed conversation
remains available. No client setting, request, query, header, or frame can
override the selector, and clients never switch or fall back between backends.

`FF_CONVERSATIONAL_VOICE=false` is the independent emergency kill switch for
advertising and admitting either backend; it does not disable ordinary typed
chat. Local mode adds no deployment speech endpoint, API key, model, or voice
setting. The existing remote speech endpoint and key remain isolated to the
worker, while the selector is projected only to the orchestrator.

The selector and kill switch are read once. `docker restart` does not reload a
changed Compose environment. Apply either change with:

```bash
docker compose up -d --force-recreate astraldeep
```

For a planned backend change:

1. Announce the maintenance window and drain or end known voice sessions
   through their normal authenticated controls. Keep the current selector, set
   `FF_CONVERSATIONAL_VOICE=false`, and force-recreate `astraldeep`. Graceful
   shutdown ends sessions owned by the outgoing replica; the replacement
   process refuses new voice admission. Confirm authenticated
   `/api/voice/v2/capability` and `/api/voice/v2/status` report
   `feature_disabled`.
2. If the outgoing process did not shut down cleanly, an old-backend row stays
   bound to its original backend and the replacement refuses non-terminal work
   through another strategy. End it through the shared authenticated control,
   or use an explicit takeover that terminalizes the old generation before
   creating a new selected-backend session. No active session changes backend
   in flight.
3. Set the exact target selector in the protected `.env`, keep the kill switch
   false, and force-recreate `astraldeep` again. Recreating the worker is
   unnecessary for a selector-only change because it never receives the
   selector; recreate it separately only when its own configuration changed.
4. Check the authenticated v2 views expose only the target categorical backend
   and disabled posture, never endpoint or key material. Set the kill switch
   true, force-recreate once more, then run the selected profile's bounded smoke
   before reopening voice.

To roll back local speech, repeat the drain, set
`VOICE_SPEECH_BACKEND=llm_factory`, force-recreate the orchestrator, confirm a
ready admitted worker, and run a real ASR and TTS remote smoke on a disposable
voice session. Reopen an existing conversation and confirm its durable text is
unchanged. Selector rollback requires no conversation data rewrite and is not
a database down-migration. To return to local mode later, drain again, select
`client_local`, recreate, and run a blocked-remote-speech local smoke; never
fall back silently within a session.

### Restart ordering and self-heal guarantees

- **Orchestrator restarts**: a running worker re-registers within its
  reconnect backoff; registration refusals are logged with their exact
  reason on both sides (FR-035).
- **Speech-service outages**: a worker whose ASR/TTS preflight failed
  re-checks on a bounded 5s→60s backoff (`preflight_until_ready`, FR-036)
  and self-heals when models/routes return — no container restart needed.
  Preflight verdicts are logged per attempt on the worker.
- **Secret rotation**: `docker restart` does NOT re-read the env file. Any
  change to `VOICE_CONTROL_SECRET`, LiveKit keys, or the closure pin needs
  `docker compose up -d --force-recreate voice-worker astraldeep`.

### Diagnosis runbook

First stop (FR-034): `GET /api/voice/status` (authenticated) reports
admitted workers (identity, accepted/active sessions, registered-at) and the
most recent admission refusals with stage + reason. `reason:
"worker_unavailable"` with an `authentication`-stage refusal loop means a
control-secret mismatch — recreate the worker (stale env). An empty worker
list with NO refusals means the worker never dialed in — check its own logs
for preflight or DNS failures.

The full log-correlation runbook (both-container log reads, recreate
commands, closure verification) lives at
[specs/066-canvas-first-uiux/voice-prod-diagnosis.md](../specs/066-canvas-first-uiux/voice-prod-diagnosis.md);
its two top suspects — stale recreated env and a worker that preflighted
before the speech service was live — are the observed production failure
modes.

## External LETS warden (feature 074 Phase B — default `off`)

The orchestrator can bind to an independent LETS warden for finite,
lineage-bound authority over the governed agent cohorts (`server_dynamic`,
`byo_user`). Modes, the `LETS_*` environment block, the Compose template and
the sandbox deployment facts are in
[docs/lets-external-warden.md](lets-external-warden.md); the wire/host
contract is
[specs/074-multirepo-lets-integration/contracts/lets-enforcement.md](../specs/074-multirepo-lets-integration/contracts/lets-enforcement.md).
With `LETS_MODE=off` (the default) behaviour is byte-identical to a build
without LETS.

## Health probes

| Endpoint | Meaning | Use |
|---|---|---|
| `GET /healthz` | Process is serving | Liveness probe |
| `GET /readyz` | Database answers (`503` + `{"db":"unreachable"}` otherwise) | Readiness probe / compose healthcheck |

Both are ungated (no user data) and excluded from access logs.
`docker-compose.yml` wires `/readyz` as the container healthcheck
(`start_period: 90s` covers agent auto-start).

### `/readyz` `lets` entry

The `/readyz` body carries a bounded `lets` object describing the LETS
(external warden) posture. With the feature off it is exactly
`{"mode": "off", "status": "disabled"}` and no warden is ever contacted.
In `shadow` or `enforce` mode it reads:

```json
"lets": {"mode": "enforce", "status": "healthy", "reason": "lets_healthy",
         "governed_effects_permitted": true, "governed_dispatch_ready": true,
         "retryable": false, "observed_at_ns": 1724400000000000000}
```

- `status` is the LIVE observation: the orchestrator periodically sends a
  cheap `GET /health/ready` to the warden (on a worker thread, never on the
  tool-dispatch path; a caller waits at most 2 s) and caches the answer for
  `LETS_HEALTH_PROBE_INTERVAL_SECONDS` (default `30`, range 1–3600). The
  first probe runs at boot. `observed_at_ns` is the time of that last probe;
  `0` means the warden has never answered (`reason: lets_starting`).
- `reason` is an operator code only (`lets_healthy`, `lets_starting`,
  `lets_unavailable`, `lets_trust_failed`, `lets_configuration_invalid`, …);
  no warden response, exception text, path, or secret is ever echoed.
- **503 semantics:** `enforce` + `status: blocked` is the only LETS-driven
  503 (`"status": "degraded"` at the top level, `db` still `ok`). It is
  reached when the probe finds the warden down, unresolvable, mis-certified,
  rejecting the service credential, or when configuration is invalid — so an
  enforce instance whose warden went away is taken out of rotation within
  one probe interval. A failed probe never changes boot: the process still
  starts and the readiness probe, not the constructor, gates traffic.
  `shadow` degradation stays 200 (`status: degraded`, existing behavior
  unchanged). Admins can read the full redacted projection at
  `GET /lets/health`.

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

- The native clients (feature 044/041/051: `components/AstralProjection/windows-client/`, `components/AstralProjection/android-client/`,
  `components/AstralProjection/apple-clients/` iOS + macOS + watchOS) consume the same public origin as the
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

Feature 053 ships the `components/AstralProjection/apple-clients/` family — iOS + macOS (one multiplatform
`AstralApp` target) plus an embedded watchOS companion (`AstralWatch`) — to the
App Store as a single Universal Purchase record (bundle id
`com.personalailabs.astraldeep`). Two things are operator-facing: the backend
`.env` the shipped apps expect, and the signed release pipeline.

### Production `.env` the Apple clients depend on

A stock App Store build compiles in `https://sandbox.ai.uky.edu` and
`https://iam.ai.uky.edu/realms/Astral` as its endpoint
(`components/AstralProjection/apple-clients/Config/Release.xcconfig` → both Info.plists → `AstralConfig`).
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

AstralProjection owns active native pull-request evidence. Its own
`.github/workflows/android-ci.yml` and `.github/workflows/apple-ci.yml` qualify
the Android, iOS, macOS, and watchOS sources; AstralDeep does not duplicate
those workflows or fetch private Projection source in hosted pull-request jobs.

Release activation remains parked during Feature 074. The retained Deep
`.github/workflows/apple-release.yml` describes the separately protected
archive/sign/export/validate/upload path, but Deep owner CI neither activates it
nor treats Projection pull-request evidence as release authorization. The
pipeline does not submit for review (see below).

**Activation contract.** A merge never activates this release path. A later,
separately approved release task must use one of two explicit events:
1. **Push a tag `apple-v*`** that equals `apple-v$MARKETING_VERSION` exactly
   (a mismatched tag fails the guard) — a versioned release of record.
2. **`workflow_dispatch`** — manual release from the `main`/default ref only
   (e.g. to re-upload a build for an already-merged version).

The `apple-v*` namespace is deliberately disjoint from the Windows release's
`v*` trigger — a `v-apple-*` tag would double-fire that workflow — so do not
rename it. A `paths:` filter is intentionally not used because tag pushes can
be silently skipped by path filters. The tag-vs-`MARKETING_VERSION` guard runs
only on tag pushes. The build number is `$GITHUB_RUN_NUMBER`, passed to
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
- AstralPlane owns baseline initialization, guarded migrations, current-schema
  verification, and recovery. They run before the orchestrator admits traffic;
  an incompatible marker, digest, or live structure fails startup closed. Never
  delete or rewrite `schema_meta` by hand. Follow
  [migration-rollback-074.md](migration-rollback-074.md) with a verified backup.
- `DB_POOL_MIN` (default 2), `DB_POOL_MAX` (default 10), and
  `DB_POOL_ACQUIRE_TIMEOUT_SECONDS` size and bound the single application Plane
  pool. There is no legacy connection-per-query fallback.
- Back up `pgdata`, `ATTACHMENT_UPLOAD_ROOT`, and every configured key or
  external authority state needed by the deployment.

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

## Deploying to sandbox.ai.uky.edu (GHCR path)

The composed backend image is published by `.github/workflows/publish-image.yml`.
It fires only after the owner CI aggregate (`ci.yml`) completes successfully for
a push to `main`, checks out that exact commit with all four component
submodules, re-runs the fail-closed composition preflight (`verify_composition`
plus `install_local_components.py validate --require-gitlinks`), builds
`Dockerfile`, and pushes `ghcr.io/<owner>/<repo>:sha-<commit>` (immutable) and
`:latest` (convenience pointer). PR runs, failed runs, and other branches never
publish. Hosted CI still validates only Deep-owned source and the four exact
composition declarations; full private composition and release activation
remain local/owner decisions, and the voice-worker image stays behind the
`VOICE_WORKER_CLOSURE_APPROVED` gate.

Deploy only from a `sha-<commit>` tag whose commit you have qualified. A green
Deep owner-CI run proves the source-free gates, not a release decision.

### 1. Pull the image

```bash
docker login ghcr.io -u <github-username>        # PAT with read:packages
docker pull ghcr.io/<owner>/<repo>:sha-<commit>  # always pin the immutable tag
```

Deploy by `sha-<commit>` (the tag `publish-image.yml` stamped on the exact
commit its green CI run tested); treat `:latest` as a convenience pointer only —
never as the deployed ref.

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
# Hosts that may RECEIVE AGENT_API_KEY on outbound agent connections, beyond
# loopback and the Docker host aliases. Only needed for a client-hosted agent
# reached at some other address. Deliberately NOT inherited from
# A2A_EXTERNAL_AGENTS: that is a discovery list, and naming a third-party peer
# there is not consent for it to hold the fleet-wide registration secret.
AGENT_KEY_TRUSTED_HOSTS=
# Agent IDs allowed to receive a minimal, card-declared verified identity claim.
# Keep empty unless a reviewed external agent requires it.
IDENTITY_CLAIM_TRUSTED_AGENTS=
# JSON agent_id -> independent 32+ character secret for browser-mediated
# identity links. Keep empty when no reviewed agent provides such a flow.
EXTERNAL_IDENTITY_LINK_SECRETS={}

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
[ ] AGENT_API_KEY set (agents refuse to register without it; it is ALSO the
    credential this orchestrator presents outbound — 16+ ASCII chars)
[ ] AGENT_KEY_TRUSTED_HOSTS set IF a client-hosted agent is reached at
    anything other than loopback or a Docker host alias (else its card
    fetch 401s and the agent never registers)
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
