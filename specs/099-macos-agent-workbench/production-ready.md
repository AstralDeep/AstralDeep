# Production preparation

The owner directed work on 2026-10-25 to prioritize the Mac client and server for production rather than CI. The CI repair work remains preserved separately; this branch prepares the operational pair and its live checks. Authentication, authorization, local consent, data preservation, LETS enforcement and rollback remain required.

The client is the signed DirectDownload build from Projection `63deedd8ffde83f0e2890bdb280322bfa32e09ba`, executable SHA-256 `3f4f0a6acfe32ff52e366b66ef8abd6b1785dc5673ab9288a415887acda62bf7`. It preserves the installed designated signing requirement, has only the audio-input entitlement, and needs a genuine local enable action after restart. The client has not yet been installed or exercised against the prepared server.

The rebuildable preparation branch pins that same Projection revision. Its server-owned `src`, `backend` and `contracts` trees are byte-identical to the previous `1704975` pin, so the server contract stays compatible with the already-rehearsed `9117730b` runtime. AstralPlane, AstralPrimitives, LETS, the Plane schema revision and migration digest remain unchanged.

Preparation must retain the current immutable server image/configuration and private data backups, validate the exact composition and production posture, and render a narrowly scoped deployment override. The production deployment binds `backend/agents` from the host, so all changed tracked first-party agent files must move to the exact prepared revision while preserving ignored or untracked user-generated agent directories. The current host checkout is `ca822e68d5477fb60fa9076107a4dab4138eeda6`; deployment must refuse tracked local edits and ignored-file collisions, retain its Compose files privately, and update the source only while the application is quiesced. Enable `FF_COMPUTER_USE` while retaining real Keycloak, existing encrypted provider configuration, audit keys and LETS enforcement.

Live acceptance requires normal client authentication and host registration, a genuine local enable action, visible browser/native postconditions, exactly reviewed Terminal interaction, selected-folder file round-trip, and Pause/Stop/disconnect denials. Any required sign-in, system permission or workspace grant stays with the owner. A unit-test result does not substitute for these checks.

Rollback selects the saved application image and deployment override while keeping compatible current database/blob/LETS state. No database downgrade, ledger rewind, key rotation or unrelated service restart is part of this preparation. Deployment and live acceptance are recorded only after they actually occur.

## Selected operational artifacts

To avoid a large rebuild on the production host's low disk reserve, the selected server artifact is the already-built immutable image `sha256:580bd01d0cb34a2f9fcaa02369bb3099bc0fb8cc274b79ec2bd1fa43c308749c`, source Deep `9117730bedc0950cc296bc896af7458c2125fd46`, Projection `1704975e45a1bfdeaf5e8f1894eb0c9c9e1e7163`. Its composition manifest SHA-256 is `8caa48415b3b22ccf5054ea9d41889bf6c8af3579c9c1760fe8e1a063ad56f22`. All backend runtime source, requirements, Dockerfile and installer bytes match this preparation branch; later backend differences are test files. Projection server runtime and contract bytes are identical between `1704975` and signed client source `63deedd8`. Server and client identities are recorded independently rather than relabeling an older image as a newer build.

The exact server image completed the isolated real-IAM/LETS/PostgreSQL and paired current-production recovery service phase (exit 0, 215.554 seconds); its retained receipt explicitly grants no release authority. Full CI is incomplete and remains parked by owner direction. This operational preparation does not claim a protected release decision or publication.

The Mac is currently locked. Updating or exercising the running native app and renewing physical consent remain pending the owner's return; no lock-screen, sign-in or local-consent bypass is attempted.
