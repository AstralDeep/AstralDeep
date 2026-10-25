# Production preparation

The owner directed work on 2026-10-25 to prioritize the Mac client and server for production rather than CI. The CI repair work remains preserved separately; this branch prepares the operational pair and its live checks. Authentication, authorization, local consent, data preservation, LETS enforcement and rollback remain required.

The client is the signed DirectDownload build from Projection `63deedd8ffde83f0e2890bdb280322bfa32e09ba`, executable SHA-256 `3f4f0a6acfe32ff52e366b66ef8abd6b1785dc5673ab9288a415887acda62bf7`. It preserves the installed designated signing requirement, has only the audio-input entitlement, and needs a genuine local enable action after restart. The client was installed at `2026-10-25T12:50:56Z` after signature verification and a process-scoped termination of the old app. Its previous installed bundle is preserved at the private local backup location. Launch and live interaction remain pending the locked Mac; no local consent or lock screen was bypassed.

The rebuildable preparation branch pins that same Projection revision. Its server-owned `src`, `backend` and `contracts` trees are byte-identical to the previous `1704975` pin, so the server contract stays compatible with the already-rehearsed `9117730b` runtime. AstralPlane, AstralPrimitives, LETS, the Plane schema revision and migration digest remain unchanged.

Preparation must retain the current immutable server image/configuration and private data backups, validate the exact composition and production posture, and render a narrowly scoped deployment override. The production deployment binds `backend/agents` from the host, so all changed tracked first-party agent files must move to the exact prepared revision while preserving ignored or untracked user-generated agent directories. The current host checkout is `ca822e68d5477fb60fa9076107a4dab4138eeda6`; deployment must refuse tracked local edits and ignored-file collisions, retain its Compose files privately, and update the source only while the application is quiesced. Enable `FF_COMPUTER_USE` while retaining real Keycloak, existing encrypted provider configuration, audit keys and LETS enforcement.

Live acceptance requires normal client authentication and host registration, a genuine local enable action, visible browser/native postconditions, exactly reviewed Terminal interaction, selected-folder file round-trip, and Pause/Stop/disconnect denials. Any required sign-in, system permission or workspace grant stays with the owner. A unit-test result does not substitute for these checks.

Rollback selects the saved application image and deployment override while keeping compatible current database/blob/LETS state. No database downgrade, ledger rewind, key rotation or unrelated service restart is part of this preparation. Deployment and live acceptance are recorded only after they actually occur.

## Selected operational artifacts

To avoid a large rebuild on the production host's low disk reserve, the selected server artifact is the already-built immutable image `sha256:580bd01d0cb34a2f9fcaa02369bb3099bc0fb8cc274b79ec2bd1fa43c308749c`, source Deep `9117730bedc0950cc296bc896af7458c2125fd46`, Projection `1704975e45a1bfdeaf5e8f1894eb0c9c9e1e7163`. Its composition manifest SHA-256 is `8caa48415b3b22ccf5054ea9d41889bf6c8af3579c9c1760fe8e1a063ad56f22`. All backend runtime source, requirements, Dockerfile and installer bytes match this preparation branch; later backend differences are test files. Projection server runtime and contract bytes are identical between `1704975` and signed client source `63deedd8`. Server and client identities are recorded independently rather than relabeling an older image as a newer build.

The exact server image completed the isolated real-IAM/LETS/PostgreSQL and paired current-production recovery service phase (exit 0, 215.554 seconds); its retained receipt explicitly grants no release authority. Full CI is incomplete and remains parked by owner direction. This operational preparation does not claim a protected release decision or publication.

The Mac is currently locked. Launching or exercising the updated native app and renewing physical consent remain pending the owner's return; no lock-screen, sign-in or local-consent bypass is attempted.

## Rollout observations

The first rollout stopped at the user-file preservation guard because a knowledge file is root-owned. The guard failed before source/image changes and automatically restored the original server to HTTPS readiness 200 at `2026-10-25T12:51:21Z`. No database or LETS state was rewound. A second attempt uses a scoped privileged read-only hash operation, verified against all 118 retained non-cache files before stopping the application; existing file permissions stay unchanged. Final deployment and health observations follow only after that attempt finishes.

The second attempt deployed the selected server at `2026-10-25T12:55:51Z`: container `bdecc21648a43b242a10da2b44f0f345de704fd7fa94587333e15cbc6db2c762`, exact image `580bd01d0cb3…`, clean host source `9117730b`, preserved mounts and all 118 non-cache untracked files unchanged before startup. No other observed running container changed. HTTPS readiness is 200, persistence/publication report healthy, LETS remains enforce/healthy, and the container has zero restarts. Host-local private backup/rollback inputs are retained; no schema change, database downgrade, ledger rewind or key rotation occurred.

Live unauthenticated HTTP smoke at `2026-10-25T12:58:34Z`: `/healthz` and `/readyz` return 200; `/` redirects to sign-in (302); `/api/chats`, `/api/chrome/menu` and `/lets/health` return 401. Native owner acceptance stays pending. The live public A2A catalog subsequently verifies connected, safe-marked `computer-use-1`; the missing informational success log is expected at the production WARNING level.

## Final operational checks

At `2026-10-25T13:12:43Z`, more than 16 minutes after initial readiness, the exact candidate container is healthy with zero restarts, HTTPS readiness 200, PostgreSQL/publication healthy and LETS enforce/healthy. The host tracked source stays clean. The live external `/a2a/.well-known/agent-card.json` contains connected `computer-use-1`; anonymous WebSocket `register_ui` receives only `auth_required`, and verified-TLS discovery from the configured real Keycloak issuer returns 200 with an exact issuer match. No user sign-in or application effect was performed by these probes.

The actual quiesced PostgreSQL backup restores successfully in a separate network-none PostgreSQL 17 instance: backup SHA-256 `1d692f2277e7ab0e9dd4be2fcad73a71dcfc4cb8249d5ffef850eec365d8763d`. Both temporary verification containers were removed. Host-local private backup and rollback files stay under `/home/sam/astral099-production-20261025-attempt2`; these are operational rollback inputs rather than an independent disaster-recovery copy.

The ordinary `/opt/AstralDeep/docker-compose.override.yml` now persists the exact selected image, `FF_COMPUTER_USE=true`, and `pull_policy: never`. Docker Compose independently renders it equal to the previously validated candidate configuration apart from that explicit local-image pull policy. Its SHA-256 is `9fca56deb1f1f417be65ebeb630125a256b3347e0bdbcc5c15198cbe5cc2c6cc`; mode/owner and all other semantic configuration remain unchanged. This convergence required no restart. The same image also has the local retention tag `astraldeep/astraldeep:production-099-9117730b`; it has not been published to a registry or released.

A guarded operator rollback script is saved privately on the host. Its default/check mode passed without mutations:

```text
python3 /home/sam/astral099-production-20261025-attempt2/rollback.py --check
```

An explicit `--apply` restores the saved application source/image/ordinary override and verifies readiness. It refuses intervening source/configuration changes and never restores PostgreSQL, blobs or LETS authority. The script SHA-256 is `5c8d81299a321d1c37a497e4ed334255b360beadf853c75f8977e133b971d563`; this final script's apply mode has not been executed against the healthy deployment. The first rollout's automatic old-image recovery is separately live-verified.

Host storage remains tight: the final observation has 10,899,771,392 bytes free against the previously computed 9,696,796,704-byte LETS reserve. Retain the active image; avoid large builds or unscoped cleanup on this filesystem.

## Owner return

1. Unlock the Mac normally and open `/Applications/AstralDeep.app`.
2. Complete ordinary single sign-on if requested and open Computer controls with Command–Shift–C. The updated client remembers the choice for this account/server. New opt-ins and local Resume after suspension still require a physical click.
3. Select the intended workspace folders for file tasks.
4. Complete the remaining live browser, exact reviewed Terminal, selected-folder round-trip, generated-UI, takeover and Pause/Stop/disconnect checks through the ordinary signed-in dispatcher.

Those live checks remain required to establish actual Mac functionality and harness comparisons. The installed binary is signature-verified, with no claim of notarization, canonical CI qualification or complete harness parity.

## Remembered computer-access setting — 2026-10-25

The owner requests persistence across app close/reopen. Projection `229e62724b874cd6f724097cf821fae7f286e079` fixes the cold-start desktop gate and separates remembered scoped consent from live access. Lock/permission loss still immediately invalidates work and requires local rearm within that process; first enable and local Resume retain hardware-input proof. Only explicit uncheck clears the saved choice. Saved sessions, requests, queue entries and Terminal grants are never restored. A connection epoch check also prevents an obsolete desktop probe from altering a newer owner's monitor.

Verification: the six initial regressions fail against the old implementation with 11 expected assertions; the additional reentrant owner/signout regression fails twice before its fence repair. Final actual host/OS fixtures pass all 118 tests. The complete Core suite passes 364; the Mac app suite passes 556 with one existing skip before the final fence repair; the final Xcode host suite passes all 22 against the final source. DirectDownload build and deep strict code-signature/designated-requirement verification pass. Local Xcode 27.1's formatter required a temporary completed host schema; every repository format setting is retained and strict lint passes, with no repository formatter-policy change. None of these results is represented as canonical CI qualification.

The signed update is installed at `/Applications/AstralDeep.app`, executable SHA-256 `5173bd08c735bca423ce3b57f59a41757f3b9eafd8dae2efd74e266e56fd83eb`; prior source63 app is retained privately. The installer does not modify preferences, credentials or system permissions. The existing saved opt-in remains present. The launched app currently requests normal Keycloak sign-in, so actual checkbox quit/reopen verification is waiting for the owner to sign in. It is not yet counted as live-verified.

This change is client-only: Projection `src`, `backend` and `contracts` bytes are identical to source63 and the deployed source170 server package. No server update, migration, protocol change or dependency addition is required. The local preparation branch pins the new component, while production remains the verified immutable server image `580bd01d0cb3`.
