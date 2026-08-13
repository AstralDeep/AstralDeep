# Quickstart: Implementing Feature 074 Safely

**Branch**: `074-multirepo-lets-integration`

**Purpose**: execution and recovery guide for the repository split, LETS integration, local qualification, and paper case study
**Production posture**: unchanged throughout this feature; no deployment or release is authorized

This is an ordered migration. Do not skip directly to source deletion or submodule Gitlinks. Each checkpoint must have exact repository hashes, narrow verification, a pushed feature branch, and a separate pushed kos-wiki update.

## 0. Protect local-only state

Before any replacement operation, inventory without printing contents:

- `Y:\WORK\MCP\LETS\paper\submission` and `Y:\WORK\MCP\LETS\results`;
- `Y:\WORK\MCP\AstralPlane\app\app.db`, logs, and environment state;
- `Y:\WORK\MCP\AstralProjection\node_modules`;
- `Y:\WORK\MCP\AstralDeep\android-client\keystore.properties`;
- `Y:\WORK\MCP\AstralDeep\android-client\local.properties`;
- AstralDeep runtime data, uploads, generated knowledge, tmp, credentials, and generated agents.

Rules:

1. Never run cleanup or recursive replacement in an original checkout.
2. Never stage ignored state or follow a reparse/symbolic-link boundary.
3. Keep ignored Android originals until a signed build succeeds from Projection.
4. Keep manuscript and result artifacts local; do not move them into a product repository.
5. Stop if any destructive target is not the exact validated fresh worktree named in the Git migration contract.

## 1. Complete the planning gates

From `Y:\WORK\MCP\AstralDeep`, with the exact feature pinned for every Spec Kit script:

```powershell
$env:SPECIFY_FEATURE_DIRECTORY = 'specs/074-multirepo-lets-integration'
git status --short --branch
```

Complete and checkpoint:

1. specification and clarification;
2. plan/research/data model/contracts/quickstart;
3. dependency-ordered tasks;
4. cross-artifact analysis.

Each phase uses branch `074-multirepo-lets-integration`; no stale `.specify/feature.json` inference is accepted.

## 2. Refresh Plane and Projection without creating archive refs

Read and follow `contracts/git-migration.md`. The key order is:

1. Re-query live remote heads/tags/defaults/workflows.
2. Verify observed default SHAs against the contract.
3. Normalize origin URLs.
4. Fetch with explicit `--no-prune`; no archive tag or branch is created.
5. Record, as a known risk only, that three commits behind already-deleted Plane branch names are not guaranteed remote recovery.
6. Create `main` at the exact refreshed `master` tip, push it, change GitHub default, and retain `master`.
7. Update/push kos-wiki with the observed baseline hashes, default-branch evidence, no-archive decision, and private-repository protection limitation.

Checkpoint name: `074-repository-baselines-and-main`.

## 3. Create fresh extraction worktrees

After collision and path validation:

```text
Y:\WORK\MCP\.migration-worktrees\AstralPlane-074
  branch codex/074-extract-data-plane

Y:\WORK\MCP\.migration-worktrees\AstralProjection-074
  branch codex/074-extract-projection
```

Verify each worktree's resolved root, common Git directory, branch, tracked inventory, and absence of reparse points. Replacement/deletion is allowed only inside these exact roots. Original repositories remain continuity copies and must stay untouched except for non-destructive ref/remote inspection.

## 4. Seed AstralProjection while Deep still owns its working copy

Extract the immutable tracked source groups listed in `contracts/astralprojection-package.md` and create the provenance manifest. Do not activate hosted workflows yet.

Implement in this order:

1. Existing `webrender`/`rote` import compatibility plus `astralprojection` metadata/resource facade.
2. Projection-owned `contracts/ui_protocol.json` and fixtures.
3. Package data for templates/static assets/vendor notices/checksums.
4. Pure renderer/ROTE/protocol tests and the forbidden-AstralDeep-import guard.
5. Complete client products and client-specific tooling/docs/workflows in inactive/manual posture.
6. Host-neutral view models/builders for chrome surfaces.

Run narrow checks in the Projection worktree:

- Python import/compile and pure renderer/ROTE tests;
- UI-manifest schema/digest/drift tests;
- web JavaScript lint/tests through Projection's own tooling;
- secret/ignored-file scan of the exact staged set;
- standalone package build and package-resource verification.

Commit and push `codex/074-extract-projection`, verify remote SHA, open at most one draft PR against `main`, and update/push the wiki.

Checkpoint name: `074-projection-seed`.

## 5. Seed AstralPlane as an embedded library

Replace the legacy Plane tracked tree in its fresh worktree, retaining repository history and adding provenance. Build the package in slices:

1. Compatibility metadata, errors, immutable records, pool/transaction kernel, detached `CommandResult`.
2. Explicit migration registry and boot initializer preserving schema lineage/advisory IDs.
3. Leaf repositories and blob stores.
4. Coupled admission/history/workspace/scheduler/effect/voice/personal-agent cluster.
5. Durable outbox, purge tombstones, audit retention anchor, LETS state records.
6. Architecture/import/SQL ownership guards and recovery tooling.

Migrate SQL to native psycopg placeholders as it moves. Do not expose cursors outside connection scope. Keep deterministic schema work separate from Deep reconciliation and Projection seed definitions.

Run narrow checks in the Plane worktree:

- package build/import and forbidden-dependency scan;
- pool/transaction/detached-result tests;
- repeat-safe migrations against disposable PostgreSQL and a representative pre-split fixture;
- owner-isolation, CAS/fence, atomic-publication, outbox, purge, and audit-anchor tests;
- staged-content secret/runtime-data scan.

Commit and push `codex/074-extract-data-plane`, verify remote SHA, open at most one draft PR against `main`, and update/push the wiki, including every discovered/fixed data bug.

Checkpoint name: `074-plane-seed`.

## 6. Add the four submodules and composition manifest

On the AstralDeep feature branch, add exact canonical HTTPS submodules with no branch tracking:

```text
components/AstralProjection
components/AstralPlane
components/AstralPrimitives
components/LETS
```

Initial pins:

- Projection: pushed extraction checkpoint;
- Plane: pushed extraction checkpoint;
- Primitives: `c1feada40e104ff345c3a94348305dcf27870054` unless a coordinated primitive release becomes necessary;
- LETS: signed `v1.0.10`, peeled commit `82dbe4f5ddf410cc86778784bb612440725ec66d`.

Generate `config/astral-composition.json` and validate it against `contracts/composition-manifest.schema.json`. Verify:

- Gitlink, submodule HEAD, manifest commit, repository URL, contract version, and domain digest agree;
- missing/private-auth/dirty/mismatched components produce precise diagnostics;
- clean-build tooling records `(AstralDeep commit, composition manifest SHA-256)` without attempting to self-embed the parent commit.

Update Docker/local sync/bootstrap so components are installed/built from exact submodules. Never place credentials in `.gitmodules`; explain that Plane/Projection require private-repository access.

## 7. Cut AstralDeep over to Projection

Keep the monorepo copy temporarily for parity while changing consumers:

1. Split Deep stateful/authenticated chrome controllers from Projection view builders.
2. Keep `orchestrator/chrome_events.py` and all policy/mutation logic in Deep.
3. Replace hard-coded shell/static/protocol paths with Projection resource accessors.
4. Update orchestrator imports and Docker/source sync to installed Projection.
5. Update Deep integration tests to consume the submodule manifest/package.
6. Compare old versus new representative output for renderer, sanitizer, ROTE, assets, chrome, and protocol.
7. Prove no runtime import or build uses the old copy.
8. Preserve ignored Android config in the new Projection client root without staging; keep originals.
9. Remove the duplicate Projection-owned tracked paths from Deep as one explicit, reviewed set.

Do not publish a client release. Implement and locally test the bounded Windows dual-identity bridge and Apple/Android build/store identity continuity; remote secret/environment recreation remains a late qualification task.

Checkpoint name: `074-deep-consumes-projection`.

## 8. Cut AstralDeep over to Plane

Move in the transaction-cluster order from the Plane contract:

1. Install Plane and introduce the temporary `shared.database` facade.
2. Switch boot migration and compatibility checks.
3. Move leaf repositories and update callers.
4. Move the atomic coordination cluster together.
5. Move outbox/purge/audit/LETS persistence.
6. Validate unchanged table names, schema path, lock identities, and configured blob roots.
7. Remove the facade and enforce no production raw SQL/direct psycopg imports in Deep.

Existing database/blob contents do not move. Exercise upgrade/recovery with representative data and verify owner isolation and atomicity after every slice.

Checkpoint name: `074-deep-consumes-plane`.

## 9. Integrate LETS with flag off first

Use `contracts/lets-enforcement.md`.

1. Add strict configuration/trust-manifest parsing and health diagnostics.
2. Add Plane-owned authority binding/lifecycle/effect operation records and migration.
3. Add the Astral-owned adapter over LETS public v1.0.10 client contracts.
4. Add `ProtectedDispatchContext` to every invocation channel and one lower gateway.
5. With the flag off, prove current behavior and audit output remain unchanged and no fake LETS success appears.
6. Implement shadow mode for server-dynamic and BYO-user populations.
7. Add final actuator receipt verification/replay stores for server-hosted generated runtimes first.
8. Change physical retries so every attempt gets a distinct operation/nonce/receipt; reconcile ambiguous non-idempotent outcomes.
9. Turn on enforce only in local/test posture after golden, denial, outage, replay, concurrency, crash, and recovery tests pass.

Do not claim BYO/external protected-executor enforcement until their exact runtime/sidecar version verifies and claims the receipt immediately before its actuator.

Checkpoint name: `074-lets-shadow-and-enforcement`.

## 10. Primitive decision gate

Render LETS health, budgets, leases, denials, and evidence using existing Card, Table, Timeline, Badge, Progress, Alert, and KeyValue primitives first.

If a product requirement cannot be represented:

1. record the concrete gap in the spec/tasks/wiki;
2. implement/export/document/serialize/test the primitive in AstralPrimitives;
3. bump/release Primitives independently;
4. add Projection web/native renderers and protocol dispositions;
5. update the Primitives submodule/manifest pin;
6. run every drift guard before consuming it in Deep.

No primitive is added merely to display LETS branding.

## 11. Local qualification near completion

Start from a clean recursive clone/check-out with authorized access to the two private submodules. Bind all evidence to exact commits and composition digest.

### Composition and source ownership

- recursive checkout and private-auth diagnostics;
- composition schema/digest/Gitlink/contract verification;
- package/wheel/image build from clean source;
- zero unmanaged duplicate renderer/client/data-plane mutable files in Deep;
- import/dependency/raw-SQL/secret scans.

### Plane and backend

- Plane full suite against PostgreSQL 17;
- representative pre-split data migration, repeat, failure, and recovery;
- Deep default and module-local suites mirroring workflow invocations;
- authenticated Keycloak/delegation/owner/policy/PHI/egress/confirmation flows;
- image liveness/readiness and production fail-closed configuration posture.

### LETS

- LETS v1.0.10 acceptance/unit/integration suites;
- off/shadow/enforce and all six scopes;
- dynamic lifecycle, replica creation, single/parallel/recursive/MCP/A2A/background/stream paths;
- exhaustion, expiry, quiesce/resume, close/revoke, outage, lost response, restart, policy/key epoch rotation;
- every receipt field mismatch, tamper, replay, clock/anchor/replay-store failure;
- non-idempotent post-claim crash and lost-result handling;
- invariant: no governed effect without exactly one matching claimed receipt.

### Projection and clients

- web unit/lint/browser flow against the live composed backend;
- Windows pytest with `QT_QPA_PLATFORM=offscreen`, updater trust tests, and an actual client launch;
- Android committed-wrapper lint/unit/coverage/build and emulator/device golden flow;
- Apple Swift/Xcode suites and live target flows on macOS;
- shared manifest/fixture, accessibility, sanitization, theme/layout/chrome, and supported-degradation parity.

If macOS/native infrastructure is unavailable, report the check as unavailable. Do not relabel absence as a pass or release under this feature.

### Release continuity

- exact legacy/new Windows identity and same-byte bridge verification without publication;
- Android application/signing/redirect/version-code continuity;
- Apple bundle/team/profile/build-number/App Store identity continuity;
- workflow secret/environment/protection inventory, still disabled until authorized.

Only after local qualification should draft branch heads be pushed for final hosted CI. Hosted CI remains diagnostic/review evidence until the later merge/release decision.

Checkpoint name: `074-local-qualification`.

## 12. Local LETS paper case study

Keep all manuscript/evidence writes under the ignored LETS paper/results trees. Validate evidence manifests with `contracts/case-study-evidence.schema.json`.

Required experiment anchors:

- exact commits for all five repositories;
- composition manifest SHA-256;
- LETS release/package/OpenAPI, policy, machine, and config-epoch identities;
- six-scope mapping and allocation;
- off/shadow/enforce topology and commands;
- raw evidence digests and reproduction timestamp.

Measure only after implementation is stable. Candidate metrics include p50/p95/p99 authorization and end-to-end latency, throughput, refusal counts/reasons, lifecycle convergence/recovery time, storage growth, conservation by scope, and count of unreceipted effects. Never fill result values from expectations.

Paper update rules:

1. Keep v1.0.10 release evidence immutable and label new measurements integration evidence.
2. If LETS runtime changes, create a successor release and rerun affected experiments before updating claims.
3. Cite the named background accurately: Armstrong, Klusty, Logan, Leach, and Bumgardner, “A Secure Sandbox Environment for Orchestrating Medical AI Agents Using Model Context Protocols and Role-Based Access Control,” *AMIA Jt Summits Transl Sci Proc.*, 2026, p. 57, PMCID PMC13274365.
4. Describe that paper as historical background, not proof of the current typed-SDUI/data-plane/LETS implementation.
5. Use neutral self-citation in the anonymous manuscript and scan for prohibited author/repository fingerprints.

Checkpoint name: `074-lets-case-study-local`.

## 13. Completion boundary

Feature 074 is implementation-complete only when:

- each mutable source file has one repository owner;
- all four Gitlinks and the composition contract are exact and clean;
- each replacement commit is verified as a normal descendant of its recorded default baseline, with no archive refs or history rewrite;
- Plane migration/recovery and Projection/client parity pass at the available required platforms;
- every enabled protected effect path satisfies the receipt-claim invariant;
- the final local qualification record and each repository branch are pushed;
- all incidental fixes/results/risks are in a pushed curated wiki update;
- the local paper makes only reproduced, exact-revision claims.

It remains **not deployed, not merged to default branches, not released, and not submitted** until those actions receive separate authorization.
