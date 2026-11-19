# Production deployment (operator guide)

AstralDeep serves the personal node API on port **8001**. It publishes the node
descriptor and provides loopback pairing and operator controls. When enabled,
MCP and A2A accept calls with owner-minted tickets. Owner devices send turns
over the mesh, and the Kernel runs them.

This guide covers one personal node in production posture. A personal node
serves one owner and has no sign-in. Callers authenticate by a roster-listed
device key with a mesh ticket, or by an owner-minted caller ticket. Two hosts
run it. The Mac node host runs the node from the installer, and Vessel runs it
in Docker with a sandbox executor.

Companion docs: [personal-node.md](personal-node.md) (install, pairing, mesh
peer, sandboxed tools), the [Vessel README](../vessel/README.md) (the Docker
deployment), [byo-client-agents.md](byo-client-agents.md) (user-authored,
client-hosted BYO agents, `FF_BYO_AGENTS`).

## Fail-closed posture (read this first)

`ASTRAL_ENV` **unset means production**. A production-mode boot refuses to
serve (exit code 78, one consolidated operator checklist in the log) unless:

| Requirement | Why |
|---|---|
| No mock-auth setting | Mock auth was removed. A leftover setting refuses to boot. |
| `AUDIT_HMAC_SECRET` set to a real value | The audit hash chain is forgeable under the shipped placeholder. |
| Node key and data directory writable | The node signs tickets and mesh events with its own key. |
| Pinned Plane schema revision and digests match | The node refuses a database that differs from `config/astral-composition.json`. |

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
- Owner devices send turns over the mesh. The optional MCP and A2A endpoints
  require an owner-minted caller ticket and a proof signed by its holder.
  Loopback pairing and operator requests use separate local secrets.

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

The owner configures a model provider on their Mac or iPhone. The device uses
its secure field and keystore. A pasted key can sync sealed to the owner's
devices. A sign-in credential stays on the device that signed in. Booting
the personal node requires no provider credential. See the
[credential item contract](../specs/106-sign-in-free-clients/contracts/credential-items.md).

## Client-hosted agents

The legacy desktop agent tunnel required a browser UI socket and is unavailable
on a personal node. For the historical transport and feature flag, see
[BYO client agents](byo-client-agents.md). The current node pairs over the
mesh and publishes tools from its configured executor. See
[sandboxed tools](personal-node.md#sandboxed-tools).

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
   network). This is what makes `request.base_url` https.
3. Deployments that enable the voice worker control socket must forward WebSocket
   upgrades for `/api/voice/worker-control` on the worker's configured route.
4. Set `PUBLIC_BASE_URL`/`BACKEND_PUBLIC_URL` to the public https origin.

## Conversational voice (features 065/066/075) — deployment topology contract

Mac and iPhone voice input runs on the owner device. The personal node can
host an authenticated worker control socket. The worker settings below also
apply to the isolated qualification app that exercises voice session lifecycle
and media.

### Production URLs

Voice is fail-closed about transport outside development. The two URLs are
different things and BOTH must be TLS in production:

1. `LIVEKIT_PUBLIC_URL=wss://<voice vhost>` — what CLIENTS join
   (e.g. `wss://sandbox-voice.example.edu`). Plain `ws://` is refused at boot
   (`insecure_public_url`).
2. `LIVEKIT_INTERNAL_URL=https://<voice vhost>` — what the ORCHESTRATOR and
   the VOICE WORKER use. The coordinator derives the worker's RTC URL from it
   (`http`→`ws`, `https`→`wss`) and refuses a plaintext derivation outside
   development with `invalid_livekit_url`. The Compose default
   (`http://livekit:7880`) is for local development. Set an HTTPS URL for a
   production worker.

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
The main app vhost proxies the configured worker-control WebSocket at
`/api/voice/worker-control` to the orchestrator. The worker serves `/api/voice/watch-bridge` itself on
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

For a planned backend change in the isolated voice session qualification app:

1. Announce the maintenance window and drain or end known voice sessions
   through their normal authenticated controls. Keep the current selector, set
   `FF_CONVERSATIONAL_VOICE=false`, and force-recreate `astraldeep`. Graceful
   shutdown ends sessions owned by the outgoing replica; the replacement
   process refuses new voice admission. Verify the qualification client's
   capability and status responses report `feature_disabled`.
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

Use `docker compose logs --since=10m voice-worker astraldeep` to inspect both
ends of the worker connection. Worker logs record speech preflight failures
and reconnect attempts. Orchestrator logs record worker-control closure
reasons. If the worker repeatedly fails its authentication challenge, verify
that both containers were recreated with the same worker-control secret.
For a closure mismatch, verify the deployed worker digest against its build
manifest.

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
| `GET /readyz` | Checks persistence and LETS readiness. A failed persistence probe returns 503 with `persistence: unready`. | Readiness probe / Compose healthcheck |

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
  In `shadow` mode, a degraded warden leaves HTTP readiness at 200. Inspect
  the `lets` entry in the same response for its reason.

## Apple clients (App Store)

The `components/AstralProjection/apple-clients/` family (iOS, macOS and an
embedded watchOS companion) ships to the App Store as one Universal Purchase
record (bundle id `com.personalailabs.astraldeep`). The apps embed the kernel
and pair with a node through the offer flow in [personal-node.md](personal-node.md).
They need no sign-in. The signed release pipeline follows.

### Direct Mac download preparation

The `AstralAppDirectDownload` scheme and `DirectDownload` configuration produce
the Developer ID distribution. It retains the registered bundle identity and
explicit local-computer consent. The current shipping candidate is Apple
silicon with macOS 14 or later; bundled universal frameworks do not make an
arm64-only app universal.

To prepare a private drag-to-Applications disk image from an already-signed app:

```sh
python scripts/package_macos_candidate.py \
  --app /private/path/AstralDeep.app \
  --output /private/path/new-candidate-directory \
  --team-id "$APPLE_TEAM_ID" \
  --deep "$DEEP_COMMIT" --projection "$PROJECTION_COMMIT"
```

The packager verifies the registered identity, explicit distribution marker,
timestamped Developer ID signature, hardened runtime and minimal entitlements.
It re-verifies the copied app and its executable digest before building a UDZO
DMG containing the app and an Applications shortcut. It refuses output
replacement and writes a checksum plus a diagnostic manifest in a private
directory. Supplied source revisions are operator declarations, not build or
release attestations. `.candidate.dmg`, `releaseAuthorized: false` and
`notarization: not-established` deliberately prevent confusion with a release.

The candidate packager is diagnostic only. Public Developer ID releases come
from [macOS release automation](#macos-release-automation), which adds
notarization, ticket stapling, Gatekeeper verification and final-byte checksums.
Local signing, a mounted candidate DMG or a website metadata edit supplies no
release authority. The Apple workflow below is Store-only; do not use an
`apple-v*` tag for a direct download.

### macOS release automation

`.github/workflows/release-macos.yml` publishes the Developer ID direct download
as an immutable GitHub release tagged `macos-v<version>-<build>`.

**Trigger.** It runs when the `CI` workflow completes successfully for a push to
`main`, with the same guard as `publish-image.yml`; `workflow_run` workflows load
from the default branch, so a pull request cannot change the publisher. The only
manual entry is `workflow_dispatch` from `main`, whose `dry_run` input defaults
to true. A dispatch with `dry_run` false publishes only when a successful CI push
run exists for that `main` commit. Publishing runs share one concurrency group
and never run in parallel; while one runs, a newer queued run replaces an older
queued one, and its release includes the older commit's changes. Dry runs use a
separate group, so a rehearsal never displaces a queued publication.

**Version and build.** `scripts/next_macos_release.py` reads the newest
non-draft `macos-v*` release and the `MARKETING_VERSION` and positive integer
`CURRENT_PROJECT_VERSION` of Projection's
`DirectDownload` configurations (all must agree):

- source `major.minor` newer than the newest release: `<major>.<minor>.0`;
- otherwise: the newest release's patch plus one;
- build: the largest of the newest published build, the source build, and the
  registered distributed-build floor of 80, plus one; the floor includes the
  approved locally distributed build 80 so it remains eligible for auto-update;
- no release when the newest release already targets the head commit, or when
  the head is older than that release's commit (a late or re-run CI), with a
  notice in the run log;
- the run fails when the head and the newest release's commit have diverged
  (a release cut outside `main`'s history).

Raise the minor by editing `MARKETING_VERSION` in Projection; the major moves
only by the owner's decision. The chosen values reach `xcodebuild` as
`MARKETING_VERSION` and `CURRENT_PROJECT_VERSION` overrides, and the built app's
identity must match them.

**Artifacts.** `scripts/release_macos.py` archives `AstralAppDirectDownload`,
notarizes and staples the app, builds `AstralDeep-<version>-<build>-arm64.dmg`
(the app plus an Applications shortcut), signs, notarizes and staples the DMG,
checks Gatekeeper, and re-inspects the app inside the mounted image. The DMG is
also the Sparkle enclosure. Publishing a stable release sets GitHub’s Latest badge
and verifies the tag at `/releases/latest`. A preview leaves the badge on the
previous stable release. Older Windows updaters that read this endpoint skip
macOS tags. The macOS release assets
are the DMG, `SHA256SUMS`, `release-provenance.json`, the notes `.md`,
`downloads.json`, `appcast.xml`, `mac-update-policy.json` and
`mac-update-policy.json.sig`. `scripts/macos_release_notes.py` writes the notes
from the pull requests merged along `main`'s first-parent history since the
previous release. Earlier appcast items and the policy's minimum build and serial
come from the live `https://astraldeep.github.io/appcast.xml` and
`/data/mac-update-policy.json` through a bounded HTTPS fetch. Once a release from
this pipeline has published them, the live bytes must equal that release's
assets: the stage waits up to ten minutes for the site to sync, then fails rather
than drop items, reset the serial or lower the minimum build.

The workflow also builds the pinned Apple Kernel framework and the Mac node pack
in separate read-only, 30-minute jobs before the environment-approved release job.
In parallel, a read-only job waits up to 25 minutes for a successful
`apple-ci.yml` push run on AstralProjection `main` at the exact Projection
gitlink in the decided Deep commit, using Projection's public Actions API. A
completed failure or cancellation with no successful run stops the release
immediately; a missing or pending run stops it at the deadline. Signing and
publication cannot start until this native gate
and both source-bound artifact jobs succeed, including for an emergency manual
dispatch.
The pack job runs the bundled tool-turn check and the real Swift installer check.
The release job verifies the framework's source receipt and the descriptor,
manifest and archive digests, then places the immutable same-release
`node-pack.json` URL and descriptor SHA-256 in the signed app's Info.plist.
`node-pack.json`, `node-pack-manifest.json` and the descriptor-named archive are
uploaded as release assets; a failed producer cannot reach signing or publication.
The pack's Deep source may precede the release commit only across documentation,
release-tooling and the Projection pin/metadata cycle. The publisher compares all
five packed component sources with that Deep commit's composition and gitlinks,
requires the release composition to differ only in Projection's commit, and
requires the Projection commits to differ only in `NodePack/pins.json`. A backend,
dependency, protocol, policy, or other client change after the packed source
fails the release; rebuild and requalify the pack at the new runtime commit.

**Secrets (names only — never commit or echo values).** The release job runs in
the `release-macos` environment, whose deployments are restricted to `main`; the
Developer ID and Sparkle secrets live there, so a workflow on another branch
cannot read them. The `ASC_*` secrets stay repository-wide because
`apple-release.yml` shares them. The job fails before any signing step when one
is unset:

| Secret | Scope | Purpose |
|---|---|---|
| `APPLE_DEVELOPER_ID_APP_CERT_P12_BASE64` | `release-macos` environment | Base64 of the Developer ID Application identity `.p12` (certificate and private key). |
| `APPLE_DEVELOPER_ID_CERT_PASSWORD` | `release-macos` environment | Password of that `.p12`. |
| `SPARKLE_ED25519_PRIVATE_KEY` | `release-macos` environment | Sparkle EdDSA private key text, written to a 0600 file for `--ed-key-file`. |
| `ASC_KEY_ID` | repository | App Store Connect API key id for `notarytool --key-id`. |
| `ASC_ISSUER_ID` | repository | App Store Connect issuer id for `notarytool --issuer`. |
| `ASC_KEY_P8_BASE64` | repository | Base64 of the App Store Connect `.p8` key, written to a 0600 file for `notarytool --key`. |

The `.p12` is imported into an ephemeral keychain; the key files and the keychain
are removed at the end of the job. The published release's provenance names the
workflow run that published it. If a run fails after creating its draft
release, delete that draft before the next run; a draft with the same tag stops
every later run with "already exists".

**Dry run.** Run the workflow from `main` with `dry_run` checked, or
`gh workflow run release-macos.yml --repo AstralDeep/AstralDeep --ref main -f dry_run=true`.
It builds, notarizes with Apple and keeps the staged files as the
`macos-release-<version>-<build>` artifact for 14 days without publishing.

**Owner-run release.** On the owner's Mac the same script uses the keychain
notary profile `astral-notary` and the Sparkle keychain account `ed25519`:

```sh
python scripts/release_macos.py \
  --projection-commit "$(git -C components/AstralProjection rev-parse HEAD)" \
  --version 2.0.0 --build 71 --channel stable --notes /private/path/notes.md \
  --staging-dir /private/path/stage --work-dir /private/path/work \
  --node-pack-dir /private/path/qualified-node-pack
```

Inspect the stage, then repeat the same arguments with `--publish
--publish-staged` to publish exactly those bytes. `--dry-run` prints every
command without running it.

**Website.** The release script opens no website pull request. The
`astraldeep.github.io` build discovers the newest verified `macos-v*` release
through the GitHub API, verifies it (immutable release, tag to commit to
Projection pin, asset digests, `SHA256SUMS`, appcast cross-check, policy
signature) and serves the download, `appcast.xml` and policy from that release's
own assets, with its committed `data/downloads.json` as a floor. Its
`release-sync.yml` checks every ten minutes and redeploys Pages when a newer
release exists.

### Goal-directed request execution

`FF_GOAL_HARNESS=true` enables completion verification for ordinary requests on
the shared server. It defaults to false for controlled qualification and rollback.
`GOAL_MAX_TURNS` bounds each enabled request to 128 model/tool rounds by default;
valid values are integers from 1 through 512. An invalid value dispatches no
work and produces an incomplete outcome. Exhausting a budget never means success.

The planner retains the original objective across compaction and receives only
the current authenticated project's validated folder context. Runnable project
work includes an appropriate isolated environment, declared and installed
dependencies, executed tests, and a startup smoke check. Generated code, static
review, and instructions for the owner to run later are partial results.

A separate no-tools evaluation checks the proposed answer against recorded
tool outcomes. Deterministic guards retain pending command jobs, stop repeated
failures and prose-only continuation, and refuse incomplete or malformed
evaluation. Useful partial results can commit while the underlying operation is
failed with `goal_incomplete`. Normal authentication, project grants, provider
disclosure, approvals, Stop, and execution fences continue to govern each step.
The rollout switch changes no file or command authorization.

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
- `UI_DESIGNER_MAX_ROUNDS` — the adaptive UI designer now defaults to **1**
  design pass per turn (was 3); raise it to restore multi-round refinement.
  Components are always delivered to clients before the designer runs.

## Logging & observability

- `LOG_LEVEL` (default `info`) controls uvicorn/app verbosity; health-probe
  and agent-card polls are filtered out of access logs.
- Timing spans (feature 052) are logged as `perf <name> duration_ms=<int>`
  lines (surface renders, chat-turn phases, boot phases);
  summarize with `python scripts/perf_report.py` (run from `backend/`,
  feeding it the app log).
- To verify a user's HMAC-linked audit trail, run
  `python -m audit.cli verify-chain --user-id <id>` from the backend
  directory in the configured node environment.

## Deploying the node image (GHCR path)

The composed backend image is published by `.github/workflows/publish-image.yml`.
It fires only after the owner CI aggregate (`ci.yml`) completes successfully for
a push to `main`, checks out that exact commit with all four component
submodules, re-runs the fail-closed composition preflight (`verify_composition`
plus `install_local_components.py validate --require-gitlinks`), builds
`Dockerfile`, and pushes `ghcr.io/<owner>/<repo>:sha-<commit>` (immutable) and
`:latest` (convenience pointer). PR runs, failed runs, and other branches never
publish. Hosted CI still validates only Deep-owned source and the exact
composition declarations; full private composition and release activation
remain local/owner decisions, and the voice-worker image stays behind the
`VOICE_WORKER_CLOSURE_APPROVED` gate.

Deploy only from a `sha-<commit>` tag whose commit you have qualified. A green
Deep owner-CI run proves the source-free gates, not a release decision.

### Mac node host

Run `scripts/node/install-personal-node.sh` as described in
[personal-node.md](personal-node.md). It writes `~/.astral-node/node.env` and
leaves `ASTRAL_ENV` unset, so the node starts in production posture on
`127.0.0.1` with SQLite. Pair the Mac or iPhone app with the offer flow.

### Vessel

`astralvessel up` runs the node image with the executor and a sandbox per
agent. See the [Vessel README](../vessel/README.md). The node image comes from
the steps below.

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
# docker-compose.override.yml (node host)
services:
  astraldeep:
    image: ghcr.io/<owner>/<repo>:sha-<commit>
    build: !reset null   # drop the build: block (compose >= 2.24)
```

On older compose without `!reset`, omit that line and start with
`docker compose up -d --no-build` so the registry image is used as-is.

### 3. Host `.env` posture

Everything in [Fail-closed posture](#fail-closed-posture-read-this-first)
applies. For a node behind a public origin:

```bash
# Public origin (TLS proxy in front, see below)
PUBLIC_BASE_URL=https://node.example.com
BACKEND_PUBLIC_URL=https://node.example.com

# Production posture, checked by the exit-78 boot gate
# (leave ASTRAL_ENV unset: unset == production, fail closed)
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

### 4. Reverse proxy

- TLS terminates at the proxy: `https://node.example.com/` →
  `http://127.0.0.1:8001`, forwarding `X-Forwarded-Proto`/`X-Forwarded-For`.
- The orchestrator trusts those headers **only** from `FORWARDED_ALLOW_IPS`
  (set it to the proxy's address).
- If voice worker control is enabled, its proxy route must forward WebSocket
  upgrades for `/api/voice/worker-control`.

The exit-78 boot gate is the final guard: if the host `.env` is incomplete,
the pulled image refuses to serve and prints one consolidated checklist in
`docker compose logs astraldeep` — fix and `docker compose up -d` again.

## Deployment checklist

```text
[ ] Image pulled from GHCR by its immutable sha-<commit> tag (not :latest,
    not a local build)
[ ] docker-compose.override.yml points services.astraldeep.image at that tag
    (build: dropped or compose started with --no-build)
[ ] ASTRAL_ENV unset (or =production) on the host, NOT development
[ ] no leftover mock-auth setting in the environment (it refuses to boot)
[ ] AUDIT_HMAC_SECRET high-entropy (placeholder is refused at boot)
[ ] AGENT_API_KEY set (agents refuse to register without it; it is ALSO the
    credential this orchestrator presents outbound, 16+ ASCII chars)
[ ] AGENT_KEY_TRUSTED_HOSTS set IF a client-hosted agent is reached at
    anything other than loopback or a Docker host alias (else its card
    fetch 401s and the agent never registers)
[ ] PUBLIC_BASE_URL/BACKEND_PUBLIC_URL = public https origin
[ ] Reverse proxy terminates TLS and forwards X-Forwarded-* from trusted sources
[ ] FORWARDED_ALLOW_IPS = proxy address
[ ] Node data directory backed up, including the node key and mesh keys
[ ] docker compose up -d; container healthcheck goes healthy (/readyz)
[ ] Boot log shows no posture warnings
[ ] Pair the Mac or iPhone app (docs/personal-node.md) and send a turn
```
