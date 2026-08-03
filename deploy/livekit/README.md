# LiveKit single-node voice topology

AstralDeep launches voice media on the official LiveKit server image
`livekit/livekit-server:v1.13.5@sha256:3497163e15c48fef6e7830c78716f9e9d5edc28abf7aa90b61c86e93bbc306b1`.
The image is an ephemeral media plane, not an authorization, task, transcript, or conversation
store. PostgreSQL and the ordinary authenticated AstralDeep dispatcher remain authoritative.
There is no Redis dependency in this five-user launch topology.

The three tracked, secret-free profiles are:

- `livekit.local.yaml`: Docker Desktop/developer networking with an explicit Mac-host media
  address and direct ICE; embedded TURN is disabled locally.
- `livekit.staging.yaml`: candidate-bound external staging with trusted WSS and externally
  terminated TURN/TLS.
- `livekit.production.yaml`: the initial production single node with JSON log sampling and the
  same externally terminated TURN/TLS boundary.

The server reads its API key pair from the runtime-only `LIVEKIT_KEYS` environment value assembled
by Compose. The profiles deliberately contain no `keys`, certificate paths, credentials, or
deployment hostnames. Do not commit `.env`, rendered Compose output, key files, certificates, or
secret-manager exports.

All tracked profiles hold LiveKit vendor logging at `warn`. In the pinned v1.13.5 server, ordinary
`info` participant-start records contain the full SDP offer, including ephemeral ICE credentials;
those records are therefore outside the permitted zero-retention boundary. AstralDeep's own
content-free readiness, capacity, lifecycle, audit, and playout metrics remain the operational
surface. Do not lower the vendor level or archive container logs as a diagnostic shortcut.

## Required ports

| Port | Transport | Purpose | Exposure |
|---|---|---|---|
| `7880/tcp` | HTTP/WebSocket | LiveKit signaling and server API | Local HTTP only; staging/production place it behind trusted HTTPS/WSS termination. |
| `7881/tcp` | ICE/TCP | WebRTC fallback | Expose directly on the media node; do not route it through an HTTP proxy. |
| `50000-50099/udp` | ICE/UDP | Preferred WebRTC media | Expose the complete range with one-to-one port mapping. |
| `3478/udp` | TURN/UDP | UDP relay fallback | Expose for staging/production when embedded TURN is enabled. |
| `443/tcp` | TURN/TLS | TLS relay fallback | Public listener owned by the trusted L4 TLS terminator for the dedicated TURN domain. LiveKit advertises this standard port. |
| `5349/tcp` | TURN/TCP upstream | Plaintext post-termination listener | Container-only with `external_tls: true`; staging maps it only to host loopback `127.0.0.1:15349`. Never expose it directly. |
| `7890/tcp` | Watch PCM WebSocket upstream | Authenticated, short-lived Watch media relay | Plaintext worker listener mapped only to host loopback. A trusted HTTP/WebSocket ingress terminates TLS and publishes the configured `wss://` URL. Never expose it directly. |
| `51000-51099/udp` | TURN relay allocations | Relay-to-SFU media | Publish one-to-one and allow narrowly in the staging/production firewall whenever embedded TURN is enabled. |

The embedded TURN relay-to-SFU range `51000-51099/udp` does not replace the client-facing ICE
range; it must also be published one-to-one through container NAT. Firewall both ranges narrowly.
If a deployment cannot
provide L4 external TLS termination, create and review a deployment-specific rendered config with
`external_tls: false` plus runtime-mounted certificate/key files; hash those exact rendered bytes
for release evidence. Never weaken certificate verification.

## Runtime-only configuration

The deployment secret mechanism must provide strong, independent values for `LIVEKIT_API_KEY`,
`LIVEKIT_API_SECRET`, `VOICE_CONTROL_SECRET`, and the orchestrator-only
`VOICE_UI_BINDING_SECRET`. The UI-binding secret is never copied into the media worker, so a worker
cannot mint a user/device control bearer. Staging and production also provide a public
`wss://` `LIVEKIT_PUBLIC_URL` and `LIVEKIT_TURN_DOMAIN`. The TURN domain must match the certificate
used by the external TLS endpoint. The public URL and every advertised ICE/TURN address must be
reachable from browsers, Windows, Android, iOS, macOS, and watchOS; localhost is not a valid production advertisement.

The operator-provided speech values are remapped only at the voice-worker service boundary:

```text
OPENAI_BASE_URL -> VOICE_SPEECH_BASE_URL
OPENAI_API_KEY  -> VOICE_SPEECH_API_KEY
```

Both Compose files set `OPENAI_BASE_URL` and `OPENAI_API_KEY` to empty inside the orchestrator and
the final worker process. The worker receives only the explicit `VOICE_SPEECH_*` aliases. Neither
the orchestrator nor an ordinary agent receives those aliases, so deployment speech credentials
cannot become a user/System LLM fallback.

For LiveKit media, the worker is dial-out only. It receives no `LIVEKIT_API_KEY`,
`LIVEKIT_API_SECRET`, or static `LIVEKIT_URL`. It opens the authenticated bounded worker-control
WebSocket and receives a separate short-lived, room-scoped RTC join grant only in an accepted
`session_bind`; the orchestrator alone retains LiveKit administration credentials. The Watch path
is the narrow exception: the worker owns a bounded PCM WebSocket listener whose host publication
must remain loopback-only. Short-lived, identity-bound Watch tickets authorize that listener; it
must never be used as a general worker ingress or logged with bearer values intact.

The local Compose profile explicitly declares development posture, a local opaque worker
identity, a four-session capacity, and an all-zero *unapproved local closure* marker so its private
bridge may use `ws://`. Protected staging has no such fallback: it requires a verified
`CLOSURE.json` digest, explicit identity/capacity, and a TLS worker-control URL.

## Local macOS startup

The optional `test` Compose profile adds `voice-worker-test`, a one-shot build
of the exact `Dockerfile.voice` test target. It is intentionally networkless,
read-only, credential-free, and absent from normal runtime startup. Use the
commands in the feature quickstart to run the locked worker suite.

Set the required runtime values in the existing ignored `.env` or export them through the operator
secret mechanism. Set `LIVEKIT_NODE_IP` to the Mac's host-reachable LAN address (for example, the
IPv4 address of the interface reported by `route -n get default`). LiveKit advertises that explicit
address for ICE; it does not advertise its Docker-private address or discover a WAN address through
STUN. `LIVEKIT_PUBLIC_URL` defaults to `ws://localhost:7880` for a browser on the Mac. Set it to the
same trusted LAN address when an Android emulator, Apple simulator, or physical device cannot reach
Mac loopback. Signaling URL and ICE advertisement are separate settings, so changing only
`LIVEKIT_PUBLIC_URL` is insufficient. `LIVEKIT_BIND_IP` defaults to `0.0.0.0` for media
reachability; use the macOS firewall and never run the local profile on an untrusted network.

The local profile disables embedded TURN rather than grant blanket relay access to RFC1918
networks. Local clients use the explicit host ICE candidate. Any deployment needing a private peer
exception must review and supply the exact narrow network outside the tracked profile.

Validate interpolation without printing the expanded secret-bearing document, then start the
topology:

```bash
docker compose config --quiet
docker compose up -d postgres livekit astraldeep voice-worker
docker compose ps
```

Local direct ICE is diagnostic, not production proof. Staging/production require trusted WSS,
TURN/UDP plus TURN/TLS, the published relay allocation range, and candidate-bound evidence.

The Watch bridge listener itself is plaintext inside the trusted host boundary. Local Compose maps
it to `127.0.0.1:${VOICE_WATCH_BRIDGE_PORT:-7890}` but does not terminate TLS. To exercise a Watch
simulator or device, place a trusted local HTTPS/WebSocket terminator in front of that loopback
port and set `VOICE_WATCH_BRIDGE_PUBLIC_URL` to its exact
`wss://.../api/voice/watch-bridge` endpoint. Do not change the advertised scheme to `ws://` or
publish port 7890 on a LAN interface.

## Staging and production

The protected staging runner supplies the digest-qualified backend and voice-worker images plus
the runtime variables above. It must retain the SHA-256 of `livekit.staging.yaml`, the immutable
LiveKit/worker image digests, the exact speech-inventory/profile hashes, and the candidate SHA.
Run `docker compose --file docker-compose.staging.yml config --quiet`; never archive or print the
expanded output because it contains runtime substitutions.

For production, run the pinned image with `livekit.production.yaml` mounted read-only, inject
`LIVEKIT_KEYS` and `LIVEKIT_TURN_DOMAIN` from the environment/secret manager, and preserve the
config and image digests in deployment evidence. Terminate signaling as HTTPS/WSS, expose ICE/UDP,
ICE/TCP, TURN/UDP, and TURN/TLS exactly as listed above, and reject loopback/private public
advertisements.

With `external_tls: true`, LiveKit's configured `5349/tcp` listener is plaintext behind the trusted
TLS terminator. The pinned LiveKit v1.13.5 `RoomManager` advertises
`turns:<LIVEKIT_TURN_DOMAIN>:443?transport=tcp` explicitly; `tls_port` selects the internal
listener and must not be changed to 443 to alter that advertised endpoint. Staging
publishes the upstream only as `127.0.0.1:15349`; configure the same-host L4 terminator's public
`443/tcp` listener for that dedicated TURN domain to forward to the loopback port. Do not publish
container port 5349 on all interfaces, and do not route TURN/TLS through an HTTP reverse proxy.

Terminate the Watch bridge separately at a trusted HTTP/WebSocket ingress. Forward only the exact
`/api/voice/watch-bridge` path to the loopback port selected by
`STAGING_WATCH_BRIDGE_BIND_PORT`, preserve the `Authorization` header, disable response buffering
and per-message compression, and enforce ordinary public request limits before traffic reaches the
worker. `VOICE_WATCH_BRIDGE_PUBLIC_URL` must be the externally reachable `wss://` URL for that
route. The worker then enforces the ticket, device, connection, generation, revision, sequence,
frame-size, and rate bounds.

LiveKit rooms and speech buffers are ephemeral. Recording and egress are not enabled, and no audio is retained
in volumes, databases, uploads, audit payloads, logs, crash reports, or telemetry.

## Readiness and operational kill switch

`GET /api/voice/capability` is the authenticated, rate-limited, `no-store` readiness surface. A
ready response proves the exact fixed profile, LiveKit, worker registration, ASR, TTS, voice, and
bounded capacity observed through the server cache. It never returns an internal URL, API key,
secret, room grant, provider body, or user content. `/readyz` remains the product-level health
surface; voice degradation must not make ordinary typed chat unavailable.

`FF_CONVERSATIONAL_VOICE=false` is the admission and advertising kill switch. Change it in the
protected runtime environment and recreate the AstralDeep service because feature flags are read at
process start. New voice activation then fails closed while typed chat continues. Do not remove the
client control, route around the normal dispatcher, or fall back to platform speech.

For a controlled drain:

1. Disable the flag and recreate AstralDeep so no new session is admitted.
2. Let connected clients stop their current voice sessions. Observe only bounded, content-free
   session/capacity metrics; do not inspect transcripts or audio.
3. Wait for active media sessions and worker assignments to reach zero. If a client disappeared,
   the durable lease sweeper ends its media session without cancelling already accepted agentic
   work.
4. Stop the voice worker, then LiveKit if no other product uses it. Keep PostgreSQL and AstralDeep
   running so typed work and accepted background operations can finish normally.

An emergency worker or LiveKit stop may interrupt media immediately. It must not delete chats,
cancel accepted tool side effects, synthesize a success, or re-submit a retained transcript.

## Rollback, recovery, and retirement

Rollback uses the previously approved digest-qualified AstralDeep and voice-worker images plus the
matching LiveKit/config digests. Disable the feature first, drain as above, and replace services in
dependency order. Feature 065 database changes are additive startup migrations; rollback leaves the
new tables/columns in place for forward recovery and must not run ad-hoc `DROP` statements. Restore
the prior application image only if its schema-revision guard accepts the deployed database.

To recover, restore the exact protected secrets and public WSS/ICE/TURN configuration, start
LiveKit, AstralDeep, and the worker, require the capability response to return the exact fixed
profile, then re-enable `FF_CONVERSATIONAL_VOICE` and recreate AstralDeep. Clients require a new
explicit activation and fresh short-lived grants; no prior audio, announcement, transcript envelope,
or session is replayed automatically.

The retired unauthenticated realtime proxy, caller-selected transcription/synthesis endpoints, and
configuration-only voice health path must remain absent. Retirement does not authorize deleting
durable ordinary chat messages or accepted operation history. After the normal retention period,
voice-session metadata is removed through the product's governed retention path; raw audio was never
eligible for persistence.
