# Running AstralDeep with an external LETS warden

Feature 074 Phase B wires the orchestrator to a LETS v1.0.11 warden over
authenticated HTTPS. This page is the operator view of that posture: what the
warden is, what the orchestrator needs, how to turn the modes, and what the
sandbox deployment looks like. The contract itself lives in
[specs/074-multirepo-lets-integration/contracts/lets-enforcement.md](../specs/074-multirepo-lets-integration/contracts/lets-enforcement.md).

## What runs where

| Piece | Process | Source |
|---|---|---|
| LETS warden | its own container (`astral-lets-warden` on the sandbox), `generic-production` runtime provider, mTLS, signed single-warden cluster manifest | `components/LETS` (pinned, signed tag `v1.0.11`) plus one operator file: a software Ed25519 signer helper |
| Orchestrator client | inside `astraldeep` | `backend/orchestrator/lets_*.py` (config, client, lifecycle, gateway, reconciler, probe, health) |
| Generated / draft agent runtimes (`server_dynamic`) | child processes of the orchestrator | receive their authority descriptor, per-runtime executor audience and private replay-store/anchor roots in the child environment (`backend/orchestrator/dynamic_runtime_authority.py`) |
| BYO agents (`byo_user`) | the owner's desktop host | the Windows host injects the same descriptor (`win_agent/byo_host.py`) |

Built-in agents are not in a governed cohort (`LETS_GOVERNED_COHORTS` accepts
only `server_dynamic` and `byo_user`).

## Modes

| `LETS_MODE` | Warden calls | Effect authorization | Physical effect |
|---|---|---|---|
| `off` | none — byte-identical to a build without LETS | none | unchanged |
| `shadow` | performed and recorded | performed; a would-deny is recorded | Astral's own decision stays operative |
| `enforce` | required | required | runs only after a verified, claimed receipt |

`FF_LETS_EXTERNAL_WARDEN=true` is the master flag; an active mode with the
flag false is a startup error. Values are read at boot — use
`docker compose up -d --force-recreate astraldeep`, not `docker restart`.

In `shadow`, any configuration or reachability problem degrades (the app
boots, `/readyz` reports `lets.status: degraded|unavailable`). In `enforce`,
an invalid configuration refuses to boot, and an unreachable warden denies
every governed effect (never a bypass) and makes `/readyz` return 503 once the
reachability probe observes it.

## Orchestrator configuration

All of these are host-contract names (see the contract table for rules):

```dotenv
FF_LETS_EXTERNAL_WARDEN=true
LETS_MODE=shadow                       # off | shadow | enforce
LETS_GOVERNED_COHORTS=server_dynamic,byo_user
LETS_GOVERNED_AGENT_ALLOWLIST=         # empty = cohort rule only
LETS_WARDEN_URL=https://astral-lets:8443
LETS_CA_BUNDLE=/etc/astral/lets/lets-ca.pem
LETS_CLIENT_CERT_FILE=/etc/astral/lets/client-cert.pem
LETS_CLIENT_KEY_FILE=/etc/astral/lets/client-key.pem
LETS_TENANT_ID=…  LETS_ENVELOPE_ID=…   # must equal the signed manifest
LETS_POLICY_DIGEST=sha256:…  LETS_MACHINE_DIGEST=sha256:…
LETS_DEFAULT_ALLOCATION=5000,5000,5000,5000,5000,5000
LETS_DEFAULT_TTL_SECONDS=1800
LETS_REQUEST_TIMEOUT_SECONDS=10
LETS_REQUEST_ATTEMPTS=2
LETS_SIGNED_TRUST_MANIFEST=/etc/astral/lets/lets-manifest.json
LETS_MANIFEST_OPERATOR_KEYS_FILE=/etc/astral/lets/lets-operator-trust.json
LETS_WARDEN_ID=astral-warden-1
LETS_EXECUTOR_INSTANCE_ID=astral-orchestrator-sandbox-1
LETS_EXECUTOR_DB_ROOT=/var/lib/astral-lets-executor
LETS_EXECUTOR_AUTHORITY_ROOT=/var/lib/astral-lets-executor-authority
# service identity — exactly one of:
LETS_IDENTITY_SEED_FILE=/etc/astral/lets/identity.seed   # per-request EdDSA JWTs (recommended)
LETS_IDENTITY_KID=…  LETS_IDENTITY_ISSUER=…  LETS_IDENTITY_AUDIENCE=…
LETS_IDENTITY_SUBJECT=astraldeep-orchestrator
LETS_IDENTITY_SCOPES=lets.lease.issue lets.lease.manage lets.branch.revoke
LETS_IDENTITY_TOKEN_TTL_SECONDS=120
# LETS_SERVICE_TOKEN_FILE=…                               # static bearer alternative
LETS_HEALTH_PROBE_INTERVAL_SECONDS=30
LETS_EXECUTOR_RUNTIME_RETENTION_DAYS=30
```

Why per-request identity: the bundled LETS authenticator accepts only EdDSA
JWTs with a lifetime ≤ 1 h, so a static token read at boot would expire.
`MintingLETSClient` mints a fresh token for every request under the client's
request lock. Keycloak cannot fill this role without new code on both sides
(RS256, no `nbf`, no `tenant_id`/`scope` claims) — see the design notes in the
074 spec.

The mounted files are: the private CA that signed the warden's server
certificate, the orchestrator's client certificate/key (the warden requires
mTLS in production), the signed cluster manifest (byte-identical to the
warden's copy), the operator trust bundle (operator public keys + threshold)
and the 32-byte identity seed.

## Compose

[`deploy/lets/docker-compose.lets.yml`](../deploy/lets/docker-compose.lets.yml)
is the reviewed, secret-free template for the warden service and the
orchestrator additions (read-only trust mount, two executor volumes,
`depends_on: service_healthy`). Copy it to `docker-compose.override.yml`
(gitignored) on the host, or pass it with `-f`.

## Readiness and health

- `/readyz` carries a `lets` object (`mode`, `status`, `reason`,
  `governed_effects_permitted`, `observed_at_ns`). Off mode is exactly
  `{"mode":"off","status":"disabled"}`.
- `GET /lets/health` (admin bearer) returns the full redacted projection
  including the redacted host configuration.
- The reachability probe (`GET /health/ready` on the warden) runs on a
  daemon thread, single-flight, cached for `LETS_HEALTH_PROBE_INTERVAL_SECONDS`.

## Sandbox deployment facts (2026-08-23)

- One warden, `astral-warden-1`, tenant `astral-sandbox`, envelope
  `astral-tools-2026-08`, six-dimension `astral.tools/v1` policy.
- Image: locally built from the signed `v1.0.11` commit plus the signer helper
  (the GHCR package was not pullable; re-pin to the registry digest once it is).
- Storage: five Docker named volumes (`astral-lets-{state,authority,audit,backup,trust}`)
  on one disk — declared honestly as separate volumes, **not** independent
  failure domains. Never restore state/authority/audit from a disk snapshot;
  use `lets recovery` bundles only.
- Signer: software Ed25519 seed on a read-only volume (owner-accepted; no HSM).
- Operator key and CA key: `/home/sam/astral-lets/operator/` — move offline.
- Provisioning commands and evidence: `/home/sam/astral-lets/RUNBOOK.md`,
  `/home/sam/astral-lets/EVIDENCE-2026-08-23.md` (host-local, no secrets in the repo).

## Rollback

Set `LETS_MODE=shadow` (or `off` + `FF_LETS_EXTERNAL_WARDEN=false` for
byte-identical behaviour) and recreate `astraldeep`. Keep the warden running so
in-flight uncertain operations can be reconciled with their original request
ids. Never delete warden or executor state to "reset".
