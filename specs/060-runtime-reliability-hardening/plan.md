# Implementation Plan: Runtime Reliability and Release Readiness

**Branch**: `060-runtime-reliability-hardening` | **Date**: 2026-07-15 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/060-runtime-reliability-hardening/spec.md`

## Summary

Harden the existing AstralDeep architecture at its failure and release seams without replacing its
platforms or changing security policy. One durable operation/admission layer bounds foreground,
background, scheduled, and maintenance work; PostgreSQL claims make scheduled occurrences,
maintenance units, startup migration ownership, and optimistic authoring transitions safe across
restarts and replicas. Personal-agent delivery and calls gain immutable revision/runtime/request
generations, one sticky authoritative desktop host, crash-safe two-phase promotion, durable deletion,
and bounded child supervision. Conversation restoration becomes one atomic, generation-scoped
snapshot backed by a non-credential per-user active-chat locator on every client. Client loads and
turns allocate their own request fences; detached, scheduled, persisted-stream, and long-job commits
allocate fresh server fences that clients open only from the exact six-field
`conversation_commit_ready` prelude before one complete commit snapshot.

The release work ships Windows 0.4.0 with one validated, immutable deployment profile and fully
locked packaged runtime; replaces fragmented progress/lifecycle signaling with shared protocol
contracts; fixes the Apple first-login LLM Save flow at the generic operation seam; and adds
artifact-level web, Windows, Android, macOS, iOS, and watchOS evidence gates. A same-candidate,
externally reachable request-namespaced staging deployment supplies real Keycloak, representative
migrated PostgreSQL, background workers, and affected client flows before release evidence can pass.
Feature 059 remains the owner
of building the macOS personal-agent host; this feature conditionally validates that host only when
the candidate-owned capability map declares feature 059 support and never duplicates it.

## Technical Context

**Language/Version**: Python 3.11 (backend and Windows release runtime); backend-served vanilla
JavaScript/CSS; Kotlin with AGP 9.3.0 and Gradle 9.6.1 (Android); Swift/SwiftUI with Xcode 26.6 and
the installed iOS/watchOS 26.5 simulator runtimes (Apple); Node.js 24 only in the isolated web-
quality CI environment

**Primary Dependencies**: Existing only — FastAPI/Starlette, asyncio, psycopg2/PostgreSQL,
websockets, PySide6/PyInstaller, Jetpack Compose/AndroidX, SwiftUI/AstralCore, and first-party
`astralprims`. Standard-library UUID, hashing, subprocess, filesystem, and JSON facilities provide
new coordination/validation logic. A separate test-only Node manifest pins ESLint and Playwright;
its isolation guard keeps it out of product images and runtime imports. No new product runtime
dependency is planned.

**Storage**: PostgreSQL guarded startup migrations through schema revision `060.004` for operation records, schedule claims, agent
revisions/instances, draft versions, maintenance claims, and release-safe idempotency records;
existing client-native non-secret stores (QSettings, Android preferences, UserDefaults, browser
storage) for an account-scoped active-chat locator; immutable/staged local agent bundle directories;
tracked JSON release profiles/evidence and a hash-locked Windows release manifest

**Testing**: pytest/pytest-asyncio plus real PostgreSQL and loop guards; language-appropriate
changed-code coverage reports for backend Python, root release-tooling Python, Windows Python,
maintained JavaScript, Kotlin, and Swift, combined by one fail-closed ≥90% gate against the immutable
PR base SHA, main-push `before` SHA, or verified manual base;
offscreen PySide6 pytest and Windows packaged-EXE smoke; Android JVM/Compose/instrumentation tests;
Swift package, hosted unit, XCUITest/accessibility, iOS Simulator, macOS tests, and the shipping
Watch app/test targets on a booted watchOS simulator (plus physical hardware only when an explicit
release runner provides it); pinned Playwright
driving real browser/Keycloak flows against candidate staging; tracked ESLint over maintained web
JavaScript and `xcrun swift-format lint --strict` over changed Swift; protocol drift guards; fault
injection and multi-instance stress harnesses

**Target Platform**: Linux containerized service; real browser; Windows 10+ packaged desktop;
Android phone/tablet; iOS 26.5 simulator/device, macOS 26.5 desktop, and the shipping Watch app on a
booted watchOS 26.5 simulator where the existing client participates; physical Watch hardware is
additional evidence only when explicitly available

**Project Type**: Multi-target service plus backend-served web and three native client families

**Performance Goals**: Never exceed configured active limits; drain connection work within five
seconds; 1,000-frame stress with one terminal result per accepted operation; 10,000 scheduler and
registry interleavings without duplicate effects or partial views; BYO exit/host-loss terminal in
two seconds and hang terminal in seven; conversation snapshot visible within five seconds; Apple
Save acknowledgement/UI response within 250 ms, phase by one second, success within five seconds in
at least 95% of trials, and terminal failure by ten seconds; unrelated interactive work p95 within
two seconds and max five seconds during maintenance/process stress

**Constraints**: Product-runtime security behavior is unchanged and explicitly out of scope;
release-provenance, CI evidence-trust, and protected approval/publication controls remain in scope;
no new runtime
dependency without approval; all schema work is additive/idempotent with rollback; `astralprims`
remains the primitive definition source and the orchestrator/ROTE remain render owners; protocol,
chrome, theme, and lifecycle changes land across every in-scope client; the production Windows
artifact contains no shared credential; feature flags retain their current fail-closed/default-off
posture; the old Windows v0.3.0 release is immutable; Apple first-login evidence is non-waivable;
release evidence names the immutable candidate image/artifact digest and cannot be inferred from a
source-only or empty-database run

**Scale/Scope**: Nine user stories, 59 functional requirements, and 22 success criteria across the
orchestrator, scheduler, data layer, web, Windows, Android, Apple, docs, and release workflows. The
release load profile is 20 concurrent interactive clients producing 1,000 total frames while five
background slots, a finite 100-item background queue, repeated scheduler polls, and maintenance
work are active; exact production limits remain operator-configurable and are published
non-sensitively in `system_config`.

## Constitution Check

*GATE: evaluated against constitution v2.8.0 and re-checked after the 2026-07-16 owner decisions.*

| Principle | Verdict | Design evidence |
|---|---|---|
| I. Python backend | PASS | All server/runtime coordination remains Python 3.11; native changes stay in their existing languages. |
| II. UI Delivery Architecture | PASS | New progress, lifecycle, and snapshot payloads are server-owned and ROTE-adapted; clients remain thin reducers/renderers; no parallel SPA or primitive system is introduced. |
| III. Testing Standards | PASS (plan) | Every new state transition, failure, migration, protocol reducer, and release validator has unit/integration coverage. A tracked fail-closed collector maps an immutable event-aware base-to-candidate diff to backend, root-tooling, and Windows Python XML, parser-filtered lock-pinned V8-to-Istanbul JavaScript statement coverage, counter-validated Android app/core Kover XML, and line-complete Apple/Watch coverage; raw/unfiltered V8 ranges, partial native reports, and candidate-hidden maintained hunks are rejected. Every changed maintained language and the combined changed executable lines must each remain at least 90%, and a missing/unmapped applicable report or unexpected empty code diff fails. Executable release orchestration is Python, not uncovered shell. |
| IV. Code Quality | PASS (plan) | Ruff, tracked ESLint, Android lint, and strict recursive `xcrun swift-format lint` cover every maintained language changed by 060. Each lint result is an independently visible CI check and no blanket exception is planned. |
| V. Dependency Management | PASS (plan) | Existing runtime dependencies and standard libraries are sufficient. Lock-pinned ESLint/Playwright, the digest-pinned browser image, isolated next-major Android tools, and commit-pinned workflow actions remain outside product artifacts/imports. T126 requires the PR to record every CI/test tool/action's version/digest, rationale, transitive impact, isolation proof, and approval disposition under Principle V. |
| VI. Documentation | PASS | Contracts document every new external seam; complex coordinators/supervisors receive docstrings; operator guides and release schemas are tracked and link-checked. |
| VII. Security | PASS / unchanged | Existing Keycloak, authorization, delegation, policy, PHI, egress, audit, and credential-storage behavior is preserved. Profiles/evidence never contain secrets; tunnel authority remains the authenticated UI principal. |
| VIII. User Experience | PASS | Existing primitives render status/forms; no new primitive type is needed. Immediate, accessible operation feedback and coherent restoration improve the existing SDUI flow. |
| IX. Database Migrations | PASS (plan) | One additive guarded 060 revision, PostgreSQL advisory ownership, post-lock recheck, representative old-data tests, repeat safety, and rollback/recovery are specified in data-model.md. |
| X. Production Readiness | PASS (plan) | Runtime-infrastructure proof reuses the CI-built candidate image and build-once unsigned client artifacts, then a configured staging host deploys the image into a TLS-reachable request namespace with real Keycloak, representative PostgreSQL data migrated normally, and real background/scheduler paths. Evidence is collected, normalized, and parsed locally before push without authorization; protected CI independently validates canonical bytes, run/job/artifact identities, pinned policy, and any bounded platform-exception approval/debt transition. Detached Sigstore publication remains in a protected environment-approved CI job, keeps the tested EXE byte-identical, verifies all three draft assets after re-download, and uses only the built-in short-lived job token; no repository-scoped GitHub App or custom broker is introduced. |
| XI. Continuous Integration | PASS (plan) | `ci.yml` invokes one reusable readiness workflow automatically for every PR and main push (manual dispatch remains available). Per-language lint, all coverage producers, connected Android, Apple/Watch tests, build-once Windows smoke, digest-pinned Playwright, docs links, stable-release Android readiness, staging proof, byte re-hashing, evidence policy, and the ≥90% changed-code decision converge on one named required aggregate after the local pre-push evidence command; main image publication depends on it. |
| XII. Cross-Client Consistency | PASS (plan) | `conversation_commit_ready`, `conversation_snapshot`, `operation_status`, and `agent_lifecycle` enter the manifest and every client drift guard/handler together; shared state names and generation rejection are uniform. |
| XIII. Documentation & Research Integrity | PASS | Product feature; research decisions cite verified repository evidence, and release evidence is SHA/artifact-bound rather than asserted from source-only tests. |

**Post-design re-check**: PASS. There are no constitution violations requiring a complexity
exception. The broad client matrix is required by Principles X and XII, not an architectural fork.

## Project Structure

### Documentation (this feature)

```text
specs/060-runtime-reliability-hardening/
├── spec.md
├── review-findings.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── runtime-operations.md
│   ├── scheduled-occurrences.md
│   ├── personal-agent-runtime.md
│   ├── conversation-continuity.md
│   ├── operation-and-lifecycle-status.md
│   ├── windows-deployment-profile.schema.json
│   ├── release-evidence.schema.json
│   └── release-trust.schema.json
├── checklists/requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
backend/
├── shared/
│   ├── database.py                 # 060 additive schema + advisory migration ownership
│   ├── protocol.py                 # snapshot/status/lifecycle and generation fields
│   ├── ui_protocol.json            # authoritative new push vocabulary
│   └── process_supervision.py      # NEW backend-local pipe/process-tree supervisor
├── orchestrator/
│   ├── work_admission.py           # NEW durable operation + connection admission coordinator
│   ├── runtime_registry.py         # NEW atomic immutable runtime snapshot registry
│   ├── orchestrator.py             # connection scope, snapshot delivery, BYO fencing/routing
│   ├── async_tasks.py              # compatibility view over operation coordinator
│   ├── task_state.py               # shared terminal vocabulary
│   ├── user_agents.py              # durable delete/revision/instance transitions
│   ├── agent_authoring.py          # expected-revision CAS and generation claims
│   ├── agent_lifecycle.py          # staged publication + backend-local supervisor
│   ├── artifact_publication.py     # NEW authoring-only fsync/atomic revision publisher
│   ├── agent_generator.py          # immutable revision bundle manifest/runtime contract
│   ├── knowledge_synthesis.py      # durable unit claims + atomic output publication
│   ├── chrome_events.py            # operation ids/status for long chrome actions
│   ├── llm_gate.py                 # bounded first-login unlock transitions
│   ├── api.py                      # authenticated operation status/metrics
│   └── welcome.py                  # truthful curated dice request
├── llm_config/
│   ├── probe.py                    # probe budget below ten-second outer deadline
│   └── ws_handlers.py              # single-flight operation-status save flow
├── scheduler/
│   ├── store.py                    # transactional occurrence claims and leases
│   ├── loop.py                     # dispatch claimed occurrences only
│   └── runner.py                   # stable occurrence identity/effect ledger
├── agents/dice_roller/mcp_tools.py # normalized d6 result metadata
├── webrender/static/client.js      # resume locator, generation filtering, shared statuses
└── tests/                          # stress, migration, fault, protocol, and browser contracts

windows-client/
├── deployment/release-profile.json       # reviewed non-secret 0.4.0 profile
├── requirements.in
├── requirements-release.lock.txt         # full Windows/Python 3.11 hash lock
├── AstralDeep.spec                        # bundle profile + runtime manifest
├── main.py                                # pre-Qt validate/worker branches
├── astral_client/
│   ├── deployment.py                      # NEW whole-profile selection/validation/freeze
│   ├── app.py                             # one effective profile + resume/status handling
│   ├── protocol.py                        # snapshot/generation/status contracts
│   ├── integrity.py                       # strict SemVer/update behavior
│   └── __init__.py                        # 0.4.0
├── win_agent/
│   ├── agent.py                           # consume resolved agent disposition
│   ├── process_supervision.py              # NEW frozen-client supervisor-contract implementation
│   └── byo_host.py                        # instance fencing, heartbeat, local bounded supervisor
└── tests/                                 # config, frozen worker, clean-profile, lifecycle, update

android-client/
├── build.gradle.kts / settings.gradle.kts / gradle.properties  # built-in Kotlin migration
├── app/src/main/kotlin/.../
│   ├── auth/ConversationResumeStore.kt    # NEW account-scoped non-secret locator
│   ├── transport/OrchestratorClient.kt    # connection/request generation
│   ├── ui/AppViewModel.kt                 # atomic snapshot + status/lifecycle reducers
│   └── ui/AdaptiveShell.kt               # native composer IME behavior + accessible chrome
├── core/src/main/kotlin/.../protocol/     # wire/manifest/status models
└── app/src/{test,androidTest}/             # continuity, semantics, connected smoke

apple-clients/
├── .swift-format                          # tracked strict Swift lint policy
├── AstralCore/Sources/AstralCore/Protocol/ # new frame models/dispositions/generation guards
├── AstralApp/AstralApp/
│   ├── ConversationResumeStore.swift       # NEW account-scoped locator
│   ├── AppModel.swift                      # snapshot/status/LLM operation reducer
│   ├── Views/ComponentView.swift           # responsive single-flight ParamPicker + a11y
│   └── Views/ChatView.swift                # native keyboard dismissal + accessible composer
├── AstralApp/AstralAppTests/               # deterministic operation/continuity tests
├── AstralApp/AstralAppUITests/             # NEW iOS/macOS first-login UI target
├── AstralWatch/ConversationResumeStore.swift # NEW Watch account-scoped locator
├── AstralWatch/Accessibility060.swift       # stable Watch accessibility semantics
├── AstralWatch/Views/WatchChatView.swift    # shipping Watch chat controls
├── AstralWatch/Views/WatchComponentView.swift # shipping Watch component controls
└── AstralWatchTests/                        # NEW Watch continuity/lifecycle/coverage target

scripts/
├── validate_release_evidence.py            # NEW stdlib schema/policy aggregator
├── run_candidate_staging.py                # NEW isolated candidate staging/evidence driver
├── check_changed_coverage.py                # NEW fail-closed Python/JS/Kotlin/Swift diff gate
├── run_android_next_major_canary.py         # NEW covered isolated compatibility driver
└── check_doc_links.py                       # NEW tracked-Markdown link guard

tooling/web-ci/                              # CI/test-only; excluded from product images
├── package.json / package-lock.json         # pinned ESLint + Playwright
├── playwright-image.txt                     # digest-pinned matching browser/OS-dependency image
├── eslint.config.mjs                        # maintained backend-served JavaScript rules
└── tests/release-060.spec.js                # real-auth candidate browser flows

docker-compose.staging.yml                   # isolated Keycloak/PostgreSQL/candidate topology
backend/tests/fixtures/runtime_reliability_060/
├── process-supervision-vectors.json         # neutral corpus consumed by both supervisors
└── staging/
    ├── representative-057.sql               # sanitized synthetic representative 057.001 state
    ├── keycloak-realm.json                   # non-secret PKCE realm/client fixture
    └── fixture-manifest.json                 # fingerprints, provenance, coverage assertions

backend/tests/test_changed_coverage_060.py    # diff/report/fail-closed collector contract
backend/tests/test_candidate_staging_060.py  # staging-driver golden/fail-closed contract
backend/tests/test_release_workflows_060.py  # workflow DAG/reuse/cleanup/no-rebuild contract

.github/workflows/
├── ci.yml
├── build-windows-candidate.yml               # NEW reusable build-once unsigned artifact producer
├── release-evidence-exception.yml             # NEW native protected-CI exception/debt transitions
├── android-ci.yml
├── apple-ci.yml
├── release-windows.yml                       # protected build-once consumer/signer/publisher
└── release-readiness.yml                    # NEW same-artifact candidate-staging evidence matrix
```

**Structure Decision**: Extend the existing multi-target repository; add only focused coordination,
supervision, profile, store, validation, and test files. Database and wire contracts remain shared
server-owned seams. No new top-level application, migration framework, or client UI definition is
introduced.

## Design and Execution Order

1. **Foundation — operations/schema/protocol**: additive 060 migration, advisory migration owner,
   operation coordinator, immutable runtime registry, process supervisor, manifest types, and
   client parsing/generation guards. This blocks every story implementation.
2. **P1 runtime correctness**: connection admission/drain, background cap/retention, durable
   scheduled occurrences, claim renewal from post-commit enqueue through terminal at no more than
   one third of the lease, and idempotent effects.
3. **P1 personal-agent lifecycle**: selected-host lease, full generation tuple, heartbeat/watchdog,
   pending-call settlement, revision candidate/promotion, durable delete, and host reconciliation.
4. **P1 conversation continuity**: per-account resume locators plus one atomic
   `conversation_snapshot` for hydration and every committed logical update (direct turn,
   component mutation, scheduled turn, persisted stream terminal, detached/REST mutation, or
   long-job result). Client load/turn generations are client-created; every server-created detached
   generation is opened only by an exact six-field `conversation_commit_ready` immediately before
   its one commit snapshot. Legacy render/stream frames are request-scoped transient overlays only,
   and a scheduled job without an explicit target uses that job's UUID4 as its stable chat.
5. **P1 Windows release**: frozen deployment profile, 0.4.0 identity, full runtime lock,
   noninteractive validation, clean-profile/frozen-worker pre-sign gate, strict update comparison.
6. **P1 Apple first login**: generalized operation status, client-local immediate acknowledgement,
   backend ten-second attempt deadline, single flight, retry/edit behavior, iOS/macOS UI tests.
7. **P2 data/operability**: draft CAS/atomic publication, maintenance claims, registry snapshots,
   truthful examples, canonical lifecycle/progress, tracked guide/apply behavior/link checks.
8. **P2/P3 release proof and future readiness**: comprehensive local-before-push evidence parser,
   independent CI validation, reachable candidate-staging topology, same-SHA/digest artifact matrix,
   bounded protected-CI exception policy, connected client gates, Android built-in-Kotlin/stable-release readiness,
   accessibility semantics, maintained-language lint, and cross-language changed-code coverage.
9. **Verification**: focused suites after each phase, then full CI-parity suites, the isolated
   candidate deployment with representative migration/real Keycloak/workers, stress/fault matrices,
   digest-pinned Playwright plus Android/iOS/macOS/watchOS runs, trusted byte provenance, and one
   automatic quantitative evidence/coverage aggregate.

## Candidate Staging Topology

The main CI build produces the candidate container once; readiness loads that exact artifact,
publishes its immutable digest to the configured staging registry, and never rebuilds it. Reusable
platform build jobs likewise archive unsigned client artifacts by workflow run/artifact ID and
SHA-256 before testing. A `stage-deploy` job on the designated staging host creates a
request-namespaced Compose project from that exact digest, restores the tracked sanitized 057.001
fixture, starts real Keycloak/PostgreSQL/background/scheduler services, migrates only through normal
startup, and emits a TLS endpoint, environment ID, fixture fingerprint, and service/image digests as
workflow outputs and immutable artifacts. The job exits without tearing the namespace down.

Backend/browser, packaged Windows, connected Android, and Apple jobs all require `stage-deploy`,
consume that endpoint, attestation-verified deploy manifest, and archived artifact identities, and fail if any is
unreachable, mismatched, mutable, or cannot be re-hashed; there is no localhost, mock-auth, empty-
database, source-run, or caller-selected URL fallback. Before push, a deterministic local command
collects, normalizes, and parses the canonical platform reports and raw references, records their
digests, and emits `protected_release_authorization: false`. Protected CI then reconstructs exact
artifact/job IDs from the current run and GitHub API, verifies attestations and subject bytes,
re-hashes stage/raw artifacts, and executes pinned policy and coverage independently. A committed
local verdict, candidate filename, or same-name job cannot substitute for that decision.
Repository rules protect the reviewed readiness workflow identity and required check. Protected
environment-approved CI performs any bounded exception approval, append-only debt registration, or
later resolution with its built-in job token; local and candidate jobs cannot authorize those
transitions. The publisher environment, release-tag rules, signer bytes, and job-scoped permissions
are configured before any official publication. No repository-scoped GitHub App, installation token,
or custom broker is created.
A final `stage-cleanup` job with `if: always()` depends on the entire matrix and removes only that
request namespace. Missing staging-host/registry/TLS/trust inputs block unconditionally. A required
client-runner outage remains blocked unless the existing bounded, protected-CI exception policy is
satisfied; no substitute client exists.

`ci.yml` calls `release-readiness.yml` as a reusable workflow on every pull request and main push
after maintainers have run the local evidence-preparation command;
the latter also retains explicit manual dispatch. It passes the PR base SHA, `github.event.before`
for main, or a manually supplied verified ancestor, records that base, and rejects an unexpected
empty executable diff. One named protected decision check owns both release-evidence policy and the
cross-language ≥90% result, and main image publication depends on that status.

Windows publication remains a protected owner-approved GitHub Actions dispatch. Candidate and
evidence jobs retain read-only permissions. The protected publisher job uses the built-in short-lived
`GITHUB_TOKEN` with job-scoped `contents: write`, `actions: write`, `attestations: read`, and
`deployments: read` only after its environment approval and reviewed workflow identity are
established; it has no OIDC permission, repository-scoped App, or broker. It consumes the T068 EXE by
exact originating run/attempt/artifact ID and never rebuilds. To preserve the actual v0.3.0 updater's
pinned Sigstore SAN, an exact-byte-pinned compatibility bridge receives only `contents: read`,
`actions: read`, `attestations: read`, and `id-token: write`, re-hashes and signs those exact bytes,
and has no release mutation authority. The publisher refuses any existing tag/release/asset, creates
the exact SemVer tag at the validated SHA, verifies the bundle with the shipped v0.3.0 policy,
creates `SHA256SUMS`, uploads exactly the three create-only assets to a draft, re-downloads numeric
asset IDs, verifies hashes, name/tag/latest disposition and target SHA, and only then makes the
release public as latest. Failure is designed to remove only the just-created tag/draft before
publication, and disposable mode uses a same-repository temporary tag/draft that must be force-cleaned.
Neither path may activate until T120 supplies distinct review, rollback-compatible tag policy,
durable orphan recovery, and trusted-only signer-tag creation (or a verifier migration).

## Feature 059 Integration Rule

Feature 060 neither waits for nor implements the macOS personal-agent host. Applicability comes from
one immutable server-owned candidate capability map, returned identically by authenticated
`GET /api/dashboard` and `system_config.config` as
`capabilities.personal_agent_host.macos = {supported, runtime_contract_versions, source_feature}`.
The 060 candidate value is `{false, [], null}`; feature 059 alone changes it to
`{true, [2], "059"}` when its host implementation lands. Missing or malformed capability data is
unknown/blocking, never interpreted as false. No branch name, source-directory presence, connected-
client count, or client-authored evidence may substitute for this value.

When false, only the distinct macOS-hosting check is `not_applicable`; every authoring, continuity,
lifecycle-display, first-login, and accessibility check remains required. When true, the exercised
direct-download macOS artifact must advertise the structured v2 `register_ui.agent_host` contract,
receive the server-issued `agent_host_registered` acknowledgement, and pass compatibility,
supervision, lifecycle, and invocation evidence. An advertised-but-refused/missing acknowledgement
is a failure, not absence.

## Complexity Tracking

No constitution violations; table intentionally empty.
