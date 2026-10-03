# Production continuation — 2026-10-03

The initial default-disabled agent merged in PR #226 as `a13c977e6b22c0ce8f1f4980de5e483d15dd9447` and was deployed with both flags false. The owner now supplied the existing enrolled client and authorized checks, PR, qualified merge and sandbox update. The previous disabled-only staging waiver is not extended.

Branch `codex/092-gaiakeep-production` uses owner-scoped pinned SSH execution. Gaia signing/profile/service credentials stay on DGX. Exact SDK 0.2.0 artifact and 23 source/resource hashes are locked and privately snapshotted; verified TLS overrides the development transport. Account/collection discovery yields 111 tools. Existing native controls and bounded workflows retain their permission/approval/taint/audit path. Ingest request IDs support explicit reconciliation without replay. Review repaired watchdog cleanup, stale credential blocking and an unbounded upstream profile reread.

Python 3.11.15 deterministic command in quickstart, including the Gaia and listed surrounding authorization/transport/runtime suites: **545 passed**. Agent coverage: **1092/1115 statements, 97.94%**. Root `ruff check .` and `git diff --check` pass. The SDK lock matches the actual DGX wheel and files. The source adapter ran verified `whoami` against the enrolled account; that is source smoke, not candidate-image staging. No Gaia writes or live product activation occurred in these checks.

Exact-candidate Linux staging with real Keycloak, representative Plane data/migrations, workers, permission/approval/denial/reconciliation/integrity and affected-client acceptance remains required. Candidate SHA/image/evidence receipts stay outside the source tree so recording identity cannot change the candidate. Full hosted CI, merge, immutable image publication and enabled sandbox rollout remain pending. The historical record below describes the original native compatibility implementation.

# Local verification — 2026-10-02

State: locally implemented and test-passing; unpushed, unmerged, undeployed and unreleased. The existing Gaia interface is pending per the owner. This record is local diagnostic evidence, not protected merge/release qualification.

## Measured checks

- Python 3.11.15: 396 deterministic tests passed across the Gaia module, Gaia shared-dispatch/tunnel tests, existing remote-confirmation/coverage/taint/orchestrator wiring suites, and startup suite. A process-local import blocker refused every private `gaiakeep` import during this run, proving required deterministic tests do not depend on that package or a live Gaia service.
- Separate optional qualification: three tests passed against installed SDK gfs `02f7d83cc92db50968d4076da8afad2cc7a811b7`, checking exact VCS provenance, actual P-384 core signing/audience, and prototype peer-audience identity. This is not complete Linux transfer qualification.
- Ruff 0.15.21, the repository CI version: `ruff check .` passed. `git diff --check` passed.
- Real module-runner discovery returned `backend-agents-gaiakeep-tests` in the modules group. SDK qualification resides outside required backend suite discovery.
- Union coverage: 733 of 744 added/changed executable production statements covered, 98.52%, against base `1e23538deeaf68bad71b432e45e78155c484c820`. Each touched shared production file's changed executable statements has 100% coverage; new modules range from 94.38% to 100%. Test files are excluded from this production-statement metric. [Machine-readable observations](local-verification.json) bind source-file digests and the raw coverage report digest.

Commands are in [quickstart.md](quickstart.md). Coverage measured `agents.gaiakeep`, `orchestrator.remote_transport`, `orchestrator.remote_confirmation`, `orchestrator.local_agents`, `orchestrator.taint`, `shared.feature_flags`, `start`, and `orchestrator.orchestrator`; SDK observations were appended. Hunk ranges from `git diff --unified=0` were intersected with executable statements, and new production files were counted in full. Whole orchestrator coverage from these targeted suites is not a complete-suite coverage claim.

## Behavior exercised

Catalog/schema completeness and protected fields; real dispatcher owner binding, tool denial, durable approval creation/consumption/replay/change/expiry/owner denial, unattended/MCP refusal, and policy denial after approval; server-generated delegation context reaching the agent; default-off registration and both transport placements; exclusion from automatic safe grants; owner-scoped target lookup and credential failures; pinned SSH channels and cleanup; real local TLS verification on control and data paths; exact RPC correlation, admitted redirects, stream activation, bounded framing/decompression including permissive-base64 bypass attempts; malformed post-write responses and retained request IDs; certificate identity/signature checking and redaction; bounded transfer fixtures and temporary-file cleanup.

Read-only research review identified and repaired malformed native JSON/SDK parsing uncertainty, have head-guard support, lost reconciliation IDs, redaction-damaged verified proofs, compressed-framing bypass and incomplete legacy fetch dispatch. Critical seams were personally checked against pinned source and exercised by tests. No product component pin, schema/migration, primitive or client protocol changed. Optional dependencies are declared in backend/requirements-gaiakeep.txt.

## Open work

T023–T024: qualify the actual existing backend-reachable SSH forward, gateway/core trust, Linux SDK encrypted transfers, native role denials, approved effects/jobs and every affected client against an exact candidate. Complete repository merge/release evidence remains required. The local Docker daemon was unavailable; complete container/PostgreSQL/merge suites and real clients were not run. No security, platform or coverage waiver is implied.

T025: legacy fetch receiver is incomplete and refuses before connection. The packaged SDK offers no supported receiver; the evaluation harness requires separate origin/range/frame/integrity confinement and qualification. Legal-order shortening remains explicitly unsupported due to the upstream routing/signature collision. Certificate checking does not claim Merkle leaf inclusion. These limitations prevent claiming every native operation live-ready.

Earlier live discovery reached the sandbox/login node through the existing host-key-verified SSH forward and inspected the dashboard/gateway read-only. It did not verify a Gaia storage plugin, public/container reachability of port 40000, or every regional DGX agent. No server installation, service mutation or independent instance was performed. All development in this phase stayed local.
