# Implementation Plan: Astral Multi-Repository Decomposition and LETS Integration

**Branch**: `074-multirepo-lets-integration` | **Date**: 2026-08-13 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/074-multirepo-lets-integration/spec.md`

## Summary

Preserve AstralDeep as the independent orchestration and product-composition repository while replacing the legacy AstralProjection and AstralPlane contents through ordinary commits descended from their refreshed default histories. Projection becomes the sole owner of rendering, ROTE, the UI protocol, web assets, and complete Windows/Android/Apple clients. Plane becomes an embedded Python durable-state library over the existing PostgreSQL database and configured blob roots. AstralPrimitives remains the primitive-definition release train. LETS remains an external independently deployed warden, initially consumed from its signed immutable `v1.0.10` tag through public client/executor contracts.

AstralDeep pins all four repositories as exact submodules and validates a machine-readable composition manifest at build/startup. Migration proceeds through default-branch normalization, component seed, dual-source parity, consumer cutover, duplicate removal, LETS off/shadow/enforce, and late local qualification checkpoints. No archive refs or history rewrites are used. Existing production remains on its monorepo-derived artifact. The active paper stays ignored/local; new Astral case-study evidence is exact-revision-bound and does not relabel v1.0.10 release evidence.

## Technical Context

**Language/Version**: Python 3.11; vanilla JavaScript; Python/PySide6; Kotlin/JVM 17; Swift 5.9-compatible sources; JSON Schema 2020-12; Git 2.x/PowerShell migration tooling

**Primary Dependencies**: FastAPI/ASGI, existing PostgreSQL driver/pool and cryptography, Keycloak/RFC 8693, Docker/Compose, `astralprims`, ROTE/webrender, pinned LETS `lets-agent[client]==1.0.10` public client/executor contracts (`httpx` transport), existing LiveKit/native client SDKs

**Storage**: Existing PostgreSQL 17-compatible database and configured filesystem roots; LETS warden storage remains external; executor replay stores/authority anchors and local manuscript/evidence remain persistent state outside repositories

**Testing**: pytest and ruff; Projection web JavaScript lint/tests/browser verification; PySide6 offscreen pytest; Android Gradle wrapper lint/unit/Kover/build plus emulator/device; Swift Package/XCTest/xcodebuild plus live Apple targets; JSON Schema/manifest/digest/import/SQL-ownership guards; Docker image/boot checks; local fault, recovery, concurrency, and case-study harnesses

**Target Platform**: Linux Python 3.11 orchestrator/container with PostgreSQL and Keycloak; same-origin web; supported Windows desktop, Android, iOS, macOS, and watchOS clients; GitHub private/public repositories; local Windows migration host and macOS for Apple qualification

**Project Type**: Five-repository composed web service, embedded data library, presentation package, desktop/mobile clients, independent authorization-service integration, and local research artifact

**Performance Goals**: Preserve flag-off and non-governed behavior without a new Plane network hop; bound every LETS request by configured deadline, response size, and retry count; safely serialize initial per-binding authorization-through-claim; characterize p50/p95/p99 authorization/end-to-end overhead, throughput, recovery time, and storage growth from exact-revision evidence before a later rollout SLO is set

**Constraints**: No archive refs, force-push, orphan history, or published-ref deletion; use fresh worktrees and preserve ignored/sensitive state; no new Plane service/database or first-cut blob-root move; no LETS permission widening or fail-open path; no duplicate mutable source after cutover; no credentials in submodules/config/evidence; private Plane/Projection access required; broad hosted CI deferred until late local qualification; production, releases, stores, and paper submission unchanged

**Scale/Scope**: Five repositories; four submodules; AstralDeep baseline 2,273 tracked files; about 468 obvious renderer/client files plus tooling/tests; about 68 PostgreSQL tables and SQL in 44 non-test Python files; six tool-scope dimensions; web, Windows, Android, iOS, macOS, and watchOS behavior

## Constitution Check

*GATE: Passed before Phase 0 and re-checked after Phase 1 design under the owner's explicit feature-specific waiver.*

The owner explicitly waived the constitutions of AstralDeep, LETS, AstralPlane, AstralProjection, and AstralPrimitives for this task. Only kos-wiki operating rules remain binding. Historical monorepo mandates such as renderer ownership in the orchestrator or migrations in `backend/shared/database.py` are migration-impact evidence, not vetoes. The feature updates stale guides/workflows so removed paths are not described as current truth.

The following properties remain implementation constraints because the specification independently requires them:

| Gate | Status and design |
|---|---|
| Language/runtime compatibility | PASS — Plane and Deep remain Python 3.11 compatible; client language floors remain unchanged. |
| Dependency control | PASS — component packages are first-party; LETS uses its pinned public client extra. Any new non-first-party runtime dependency is separately surfaced. |
| Existing Astral security | PASS — Keycloak, RFC 8693, owner isolation, permission/policy/security/taint/PHI/egress/confirmation/audit gates run before LETS. LETS only restricts. |
| External input/secret handling | PASS — strict config/wire parsing, HTTPS without redirects, authenticated trust manifest, bounded response/deadline, runtime secret files, redacted evidence. |
| Database migration/recovery | PASS — Plane retains guarded repeat-safe schema evolution, one explicit boot initializer, representative-data tests, read-compatibility gate, and documented recovery. No first-cut data move. |
| Cross-client UI consistency | PASS — Projection owns one manifest/fixture and every client; Deep retains producer integration checks. Missing Apple manifest becomes failure, not skip. |
| Changed-code verification | PASS by plan — narrow tests run per slice; late local qualification covers component/backend/client/security/migration matrices before hosted CI. |
| Runtime staging | NOT CLAIMED — production remains unchanged; no merge/deploy/release claim is made. A later deployment decision must supply candidate-bound staging. |
| Client-platform exception | NOT USED — unavailable native verification is reported as unavailable and blocks release claims. |
| Release evidence bootstrap/publication | NOT USED — no signing, store, tag, package, image, public release, or evidence-bootstrap action is authorized. |
| Research integrity | PASS — exact-revision evidence schema, immutable v1.0.10 baseline, historical/current Astral distinction, accurate AMIA citation, anonymous neutral self-citation, no fabricated results. |
| kos-wiki checkpoint rule | PASS by plan — every durable decision, pushed branch/PR, spec/plan/tasks phase, result/fix/risk gets a separate curated wiki commit and push. |

### Post-design re-check

Phase 1 introduces no unresolved clarification and no unwaived gate failure. Component contracts preserve one-way dependencies, stable security ownership, same-database transactions, same-origin web serving, exact client/store identities, fail-closed LETS enforcement, and recoverable default history through ordinary descendant commits. The two private submodules intentionally make full composition checkout access-controlled; diagnostics distinguish authorization failure from a missing component without embedding credentials.

The owner declined archive refs. Prior default-branch commits remain ordinary ancestors. Three Plane commits observed only behind already-deleted remote branch names are recorded as a known non-preservation risk and are not republished.

## Project Structure

### Documentation (this feature)

```text
specs/074-multirepo-lets-integration/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── checklists/
│   └── requirements.md
├── contracts/
│   ├── composition-manifest.schema.json
│   ├── case-study-evidence.schema.json
│   ├── astralplane-library.md
│   ├── astralprojection-package.md
│   ├── git-migration.md
│   └── lets-enforcement.md
└── tasks.md
```

### AstralDeep (composition and orchestration)

```text
components/
├── AstralProjection/                # exact private Git submodule
├── AstralPlane/                     # exact private Git submodule
├── AstralPrimitives/                # exact public Git submodule
└── LETS/                            # signed v1.0.10 public Git submodule

config/
└── astral-composition.json

backend/
├── orchestrator/
│   ├── lets/                        # config, mapping, lifecycle, dispatch
│   ├── projection_controllers/      # authenticated/stateful surface controllers
│   └── ...                          # existing orchestration/policy/transport
├── shared/
│   ├── database.py                  # temporary Plane facade, then removed/thinned
│   └── ...                          # Deep-owned transport/security utilities
└── tests/
    ├── component_contracts/
    ├── lets/
    ├── projection_integration/
    └── plane_integration/

scripts/
├── verify_astral_composition.py
├── verify_component_ownership.py
└── <local qualification/evidence tooling>
```

Projection- and Plane-owned mutable source is removed from AstralDeep only after submodule consumers and parity checks pass. Deep retains cross-component tests, not duplicate implementations.

### AstralProjection (presentation and clients)

```text
pyproject.toml
backend/
├── webrender/                       # initial import-compatible package
└── rote/                            # initial import-compatible package
src/astralprojection/                # metadata/resource facade
contracts/
├── ui_protocol.json
└── fixtures/
windows-client/
android-client/
apple-clients/
tooling/web-ci/
scripts/                             # client-only helpers
tests/                               # pure render/ROTE/protocol/package tests
docs/
.github/workflows/                   # inactive until final setup
provenance/extraction.json
```

### AstralPlane (embedded durable-state library)

```text
pyproject.toml
src/astralplane/
├── compatibility.py
├── contracts/
├── database/
│   ├── pool.py
│   ├── transaction.py
│   ├── migrations.py
│   └── revision.py
├── repositories/
├── blobs/
└── outbox/
tests/
├── unit/
├── integration/
└── recovery/
provenance/extraction.json
```

### Existing independent repositories

```text
AstralPrimitives/
├── src/astralprims/
├── tests/
└── pyproject.toml

LETS/
├── src/lets/
├── protocol/openapi.yaml
├── docs/adapters/astraldeep.md
├── tests/
├── paper/submission/                # ignored/local
└── results/                         # ignored/local
```

**Structure Decision**: Use AstralDeep as the sole deployable composition/orchestration root, Projection as one presentation/client release unit, Plane as an embedded durable-state library, and existing independent Primitives/LETS release units. Keep initial Projection import paths compatible and use temporary Deep facades only to stage a tested cutover; the end state has one mutable owner and an acyclic dependency graph.

## Implementation Strategy

### Phase A — Repository identity and safe replacement roots

1. Re-query live Git state, record legacy default baselines, and explicitly fetch with `--no-prune`.
2. Normalize canonical origins and introduce `main` without changing content or deleting `master`.
3. Create fresh replacement worktrees and provenance manifests; create no archive refs.
4. Push baseline/default checkpoints and update kos-wiki, including the accepted deleted-branch risk.

### Phase B — Independent component seeds

1. Seed Projection with owned sources, package resources, protocol, clients, tests, inactive release workflows, and an import guard.
2. Seed Plane with the storage kernel, neutral contracts, migrations, repositories, outbox/recovery, and dependency/SQL guards.
3. Run narrow component checks, push feature branches, open at most one draft PR per component, and checkpoint the wiki.

### Phase C — Composition and dual-source migration

1. Add exact four submodules and composition manifest/schema verifier.
2. Install/build component wheels from submodule commits.
3. Split Projection host controllers from view builders; switch resources/imports; run old/new parity; remove Deep duplicates.
4. Introduce Plane facade; move leaf then coupled persistence clusters; separate boot/reconciliation; remove raw SQL/facade.
5. Preserve all existing database and blob locations during first cutover.

### Phase D — LETS integration

1. Strict configuration/trust and durable records with the feature flag off.
2. Fixed six-scope profile, lifecycle adapter, and every-channel protected dispatch context.
3. Shadow dynamic/user-authored populations.
4. Final gateway host binding plus v1.0.10 verifier/replay claim.
5. Enforce first for server-controlled generated runtimes; add BYO/external only after actuator conformance.
6. Correct retry/concurrency/uncertain-outcome semantics and run fault/recovery tests.

### Phase E — Local qualification and research

1. Run clean recursive composition, migration/recovery, backend/security, LETS, Projection, all-client, image/boot, and updater/store-continuity checks locally.
2. Push final candidates only after local qualification; use hosted CI/draft checks near completion.
3. Produce exact-revision local Astral case-study evidence, update the named/anonymous paper accurately, and retain v1.0.10 immutability or release a LETS successor if runtime changes are required.
4. Record results, bugs, fixes, commands, residual risks, and exact SHAs in kos-wiki.

## Complexity Tracking

No unwaived constitution violations require justification. The five-repository structure and four submodules are direct user requirements. Temporary compatibility facades and dual-source parity are explicit migration states with deletion gates, not permanent duplicate architectures.
