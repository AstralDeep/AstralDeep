# Personal-agent operations

This guide covers the fail-closed `FF_BYO_AGENTS` capability introduced by
features 057/058 and hardened by feature 060. A personal agent is authored for
one user, runs as an isolated child process on an eligible desktop client, and
connects inward over that user's authenticated UI WebSocket. Generated user code
never executes in the orchestrator process or image.

The platform continues to enforce owner isolation, permissions, policy, PHI and
egress gates, and audit provenance. A desktop-hosted agent is untrusted at the
server boundary even though its transport shares the signed-in client session.

## Enable and verify the effective setting

The flag defaults to false and is read when the service process imports the
feature-flag registry. Set the boot value in the deployment's normal Compose
environment file without printing that file:

```text
FF_BYO_AGENTS=true
```

From the repository root, recreate the service and verify the effective value:

```bash
make apply-config
```

The target uses `docker compose up -d --force-recreate astraldeep`, then reads
the running process environment from inside that container and prints exactly a
non-sensitive normalized result:

```text
Effective FF_BYO_AGENTS=true
```

Do not use `make restart` after changing this value. Restarting the existing
container does not reload a changed Compose environment. A false result means
the authoring, host-registration, delivery, and tunnel paths remain unavailable;
fix the deployment input and run `make apply-config` again.

For an authenticated candidate check, the REST dashboard and WebSocket
`system_config` projection must expose the same server-owned capability map.
That capability map determines macOS-host applicability; a client must not infer
support from its platform name.

## Hosting modes

| Client or distribution | Author/manage | Host child processes |
|---|---:|---:|
| Official Windows desktop | Yes | Yes, using the packaged and lock-verified worker |
| Generic/developer Windows desktop | Yes | Only when its explicit deployment profile and runtime pass validation |
| macOS | Yes | Only when the candidate capability map says feature-059 macOS hosting is supported and structured v2 host registration is acknowledged |
| iOS and Android | Yes | No |
| watchOS | No | No |
| Browser | Yes | No; browser sockets never receive executable bundles |

Mac App Store sandboxed builds are author/manage-only. A separately distributed
macOS host becomes eligible only when its feature-059 implementation is present
and the server advertises that exact capability. See the
[Apple client notes](https://github.com/AstralDeep/AstralProjection/blob/main/apple-clients/README.md)
and the [Windows deployment guide](https://github.com/AstralDeep/AstralProjection/blob/main/windows-client/README.md)
for client packaging.

## Lifecycle shown to users

Every authoring client consumes the same server-owned `agent_lifecycle` states:

| State | Meaning | Operator response |
|---|---|---|
| `starting` | A selected host is preparing the immutable revision. | Wait for registration and readiness; investigate a registration timeout if it does not advance. |
| `online` | The selected runtime fence is durably promoted and invocable. | No action. |
| `updating` | A candidate revision is being prepared while the previous good revision remains authoritative. | Let the candidate finish; do not stop the previous runtime manually. |
| `failed` | The current attempt ended with a safe actionable reason. | Use the reason code and server logs, correct the cause, then retry the same durable work identity. |
| `offline` | No selected invocable runtime exists because of stop, loss, deletion, or absent host. | Reopen/reconnect an eligible desktop host and allow inventory reconciliation. |

Clients compare `(lifecycle_generation, state_revision)` and ignore equal or
older updates. A legacy `agent_offline` notification may appear during a bounded
compatibility window, but it cannot override a newer canonical lifecycle pair.
The wire definition is in the
[operation and lifecycle contract](../specs/060-runtime-reliability-hardening/contracts/operation-and-lifecycle-status.md).

## Recovery and failover

1. Keep the orchestrator and database running; never repair lifecycle rows with
   ad-hoc SQL.
2. Reopen or reconnect an eligible desktop client under the owning user. The
   host registers a fresh session and reports its complete local inventory.
3. The server reconciles that inventory before it sends any start/stop/delete
   action. Only the server-selected host and current durable generation may
   accept work.
4. A retained current worker registers and becomes ready under its exact fence;
   otherwise the server redelivers the immutable selected revision to an
   eligible reconciled host.
5. Confirm the authoring surface reaches `online`. A stale host, runtime,
   request, heartbeat, or result fence is intentionally ignored.

Registration is bounded to five seconds. Runtime heartbeats are expected every
five seconds, and the watchdog may settle a hung runtime after its declared
liveness window. Host loss fails or retries in-flight requests instead of
accepting an unfenced late result. Repeated crashes require diagnosis; the
system does not treat a failed child as online.

## Runtime compatibility

Feature 060 personal-agent bundles use runtime contract version 2. Each bundle
manifest binds its agent/revision identities, executable-file inventory,
canonical bundle SHA-256, and the exact reviewed desktop runtime-lock SHA-256.
The backend source of truth is
[`agent_generator.py`](../backend/orchestrator/agent_generator.py), and the
official Windows lock is
[`requirements-release.lock.txt`](https://github.com/AstralDeep/AstralProjection/blob/main/windows-client/requirements-release.lock.txt).

A host accepts only a supported contract version and exact required lock digest.
Incompatible registration or delivery fails explicitly with
`incompatible_runtime`; it must never fall back to an older protocol, a mutable
environment, or a different host. Upgrade the complete official client artifact
rather than replacing individual worker files.

## Rollback and disablement

The safe operational rollback is the kill switch:

1. Set `FF_BYO_AGENTS=false` in the deployment's normal Compose environment.
2. Run `make apply-config` and require `Effective FF_BYO_AGENTS=false`.
3. Confirm personal-agent authoring/hosting entries are unavailable and no new
   host registration or bundle delivery is accepted.

Disabling does not authorize deleting user artifacts or changing database rows.
Existing records remain for later recovery and audit. If a code rollback is also
required, roll back the complete orchestrator and client candidate together;
do not mix a v2 bundle with an unverified older host. Database recovery follows
the feature-060 procedure in
[`data-model.md`](../specs/060-runtime-reliability-hardening/data-model.md#rollback-and-incident-recovery),
through guarded startup migrations only.

## Troubleshooting without exposing secrets

- `Effective FF_BYO_AGENTS=false`: correct the deployment input and recreate;
  do not print `.env` or use `docker inspect` as evidence.
- `registration_timeout`: update/restart the eligible desktop client and check
  its non-sensitive packaged-runtime validation.
- `incompatible_runtime`: install one complete official client candidate whose
  contract and lock match the server.
- `offline` after reconnect: check authenticated owner identity, structured host
  acknowledgement, inventory reconciliation, and the safe lifecycle reason.
- repeated `failed`/watchdog outcomes: inspect structured orchestrator and client
  logs for reason codes, never credential or bundle source contents.

The broader service posture, Keycloak setup, TLS, readiness, and rollback entry
points remain in the [production deployment guide](production-deployment.md).
