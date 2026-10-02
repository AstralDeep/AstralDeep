# AstralDeep Constitution

## Core Principles

### I. Primary Language

All backend code MUST be written in Python.

- No other backend languages are permitted without a
  constitution amendment.
- Python version MUST be kept current with the project's
  declared minimum (see `pyproject.toml` or equivalent).

### II. UI Delivery Architecture

The user interface MUST be server-driven, along one pipeline:
AstralPrimitives defines → AstralProjection renders and adapts →
AstralDeep orchestrates. There is no standalone single-page
application framework acting as the source of truth for UI.

- AstralDeep MUST deliver server-driven UI (SDUI) to clients via
  FastAPI using the pinned AstralProjection package: its
  renderer, sanitization, ROTE device adaptation, web shell,
  reusable chrome, and static resources, reached through
  Projection's installed resource accessors.
- All UI primitives MUST be **defined** by the `astralprims`
  package (AstralPrimitives). It is the single source of truth
  for primitive definitions and their serializable structured
  representation; it does not render.
- AstralDeep MUST NOT fork, patch, or reimplement rendering,
  sanitization, ROTE adaptation, the web shell, or reusable
  chrome in this repository. Such changes land in
  AstralProjection under its constitution and reach AstralDeep
  through a pin (Principle XIV).
- Agents and orchestrator surfaces MUST compose UI from
  `astralprims` primitives (constructed with `.to_dict()` or
  `create_ui_response()`). A type already approved in
  Projection's UI protocol manifest but not yet defined by the
  installed package MAY be emitted as a plain dict.
- Adding a new client target MUST be achievable by adding a
  renderer within AstralProjection's render layer — without
  changing `astralprims` primitive definitions or AstralDeep's
  agent response-building code.
- AstralDeep owns surface discovery, authorization, and
  dispatch. Its host adapters
  (`backend/orchestrator/projection_surfaces/`) supply
  host-authorized state, verified roles, and availability to
  Projection's single, role-aware, server-owned chrome model;
  AstralDeep MUST NOT hard-code a parallel menu or chrome
  definition. Settings surfaces are composed from `astralprims`
  primitives and rendered by Projection like any other
  server-driven UI.
- No standalone React/Vite (or other SPA) frontend may be
  (re)introduced as the primary UI source of truth without a
  constitution amendment.

**Rationale**: Separating primitive **definition**
(AstralPrimitives) from **rendering and adaptation**
(AstralProjection) and **orchestration** (AstralDeep) keeps the
wire-level primitive contract stable while presentation evolves
without coordinated client releases. New device targets stay
additive — a new Projection renderer over the same primitives —
rather than a parallel reimplementation.

### III. Testing Standards

Every new feature MUST include unit and integration tests with
a minimum of 90% code coverage on the code it changes.

- Tests MUST be written for all new code paths.
- Coverage MUST be measured and enforced in CI as
  **changed-code coverage**: the lines added or modified by a
  pull request MUST be ≥ 90% covered by the test suite. This is
  the mechanical merge gate (see Principle XI).
- When a change contains no measurable executable lines,
  changed-code coverage is not applicable and is recorded as
  such (Principle XI).
- Module-wide and repository-wide coverage improvements remain
  encouraged but are not the merge gate.
- No feature branch may merge without meeting the 90%
  threshold on changed code where the coverage gate applies.

### IV. Code Quality

All code MUST adhere to established style standards.

- Python code MUST comply with PEP 8. Linting MUST be enforced
  via tooling (e.g., ruff, flake8).
- Any client-side TypeScript/JavaScript emitted or maintained
  (including the pinned AstralProjection render-layer assets
  that AstralDeep's CI lints) MUST pass standard lint rules.
  Linting MUST be enforced in CI.
- When maintained TypeScript/JavaScript exists, the repository MUST
  carry its standard lint configuration in version control and the
  CI lint gate MUST exercise it. A linter declared only for CI remains
  isolated from product runtime dependencies under Principles V and XI.
- Linting exceptions MUST use the narrowest rule-specific directive
  (e.g., `# noqa: E402`, `// eslint-disable-next-line no-empty`);
  blanket suppressions are prohibited. The justification belongs
  in the PR description, not in an inline comment (Principle VI).

### V. Dependency Management

Any dependency may be installed. No lead-developer approval is
required to add a library.

- Any first-party or third-party package — runtime, test, eval,
  benchmark, or tooling — MAY be added.
- Every dependency MUST be declared in the owning repository's
  manifest (e.g., `backend/requirements.txt`, `pyproject.toml`,
  a lockfile, or a separate tooling/eval manifest) so builds are
  reproducible; undeclared, ad-hoc installs into an image are not
  permitted.
- A PR that adds a dependency SHOULD name it and its purpose in
  the description.

### VI. Self-Documenting Code

The code documents itself. Names, types, and structure carry the
meaning; prose inside source files is the exception. This
principle governs AstralDeep; each component repository's own
constitution carries the equivalent rule for that repository.

- Every source file MUST begin with a short header, at most three
  sentences, stating what the file does and how it connects to
  other files. Python uses a module docstring; other languages use
  their comment syntax. The header follows any shebang or tool
  directive that must come first.
- No other comments or docstrings are permitted, except a very
  short, single-line comment where one is absolutely necessary to
  explain a non-obvious *why*: an ordering, locking, or race
  constraint; a security reason; an external quirk or workaround;
  an invariant enforced elsewhere; a surprising constant; or an
  intentionally empty block. Such a comment states the reason,
  never what the code does.
- Function, method, and class docstrings; JSDoc, KDoc, and DocC
  blocks; narrating comments; section banners; commented-out code;
  TODO/FIXME notes; spec, task, and requirement IDs; feature
  numbers; and change history MUST NOT appear in source.
  Outstanding work is tracked as issues or tasks; rationale and
  history live in specs, pull requests, and `docs/`.
- Tool directives are not comments and MUST be preserved verbatim:
  shebangs, encoding lines, `# noqa`, `# type: ignore`,
  `# pragma: no cover`, `# nosec`, formatter and linter directives
  (ruff, eslint, ktlint, swiftlint, swift-format),
  `swift-tools-version`, Dockerfile parser directives, `make help`
  target text, and generator markers such as
  `// BEGIN GENERATED PUBLIC ASSETS`.
- Text the runtime reads MUST be an explicit value, not a
  docstring: CLI help (click `help=`, an explicit argparse usage
  string), OpenAPI summaries and descriptions, LLM tool
  descriptions, and schema descriptions (e.g., Pydantic
  `Field(description=...)`). A module header MAY double as an
  argparse `description=__doc__`.
- Tests MUST verify behavior and MUST NOT assert on the presence
  or wording of comments or docstrings.
- Files whose exact bytes are pinned by recorded evidence or
  fixtures (digest-bound model checkers, formal specifications,
  fingerprinted test fixtures) change only together with a
  regeneration of that evidence.
- Primitive documentation belongs to AstralPrimitives and
  renderer-target documentation to AstralProjection, under their
  constitutions.
- Backend APIs MUST expose interactive documentation at the
  `/docs` URL (e.g., via FastAPI's built-in Swagger UI).
- Files generated and managed by Spec Kit (`.specify/`,
  `.agents/skills/`, and `.claude/`) are upstream tooling,
  exempt from this principle; they change only through Spec Kit
  or, for the project-owned adapters and overrides that
  `AGENTS.md` names, deliberately and in step with it.

**Rationale**: Comments drift from the code they describe, while
names and types are checked by tools. Dense commenting also buried
the rare warning that mattered. A short file header keeps
navigation fast, and a one-line *why* keeps a genuine hazard
visible.

### VII. Security

Standard security practices MUST be implemented across all
system boundaries.

- Input validation MUST be applied to all external inputs.
- Authentication MUST use the project's Keycloak IAM instance.
  No alternative auth providers without a constitution
  amendment.
- Authorization MUST be enforced at the API layer via
  Keycloak roles/scopes.
- Agents MUST use RFC 8693 delegated tokens with attenuated
  scopes. Scopes are automatically set by the system for
  security; users MAY override or set scopes explicitly.
- First-party agents bundled with the system MAY run in-process
  within the orchestrator's trust boundary. Such agents are
  exempt from the per-agent transport-authentication handshake
  (the shared agent API key), BUT every runtime authorization
  control — per-user scope/permission gates, security-flag
  blocks, the policy engine, taint, PHI handling, and egress
  gating — and the audit trail MUST remain fully in force, and
  per-user secrets MUST be decrypted only inside the agent's own
  boundary, never materialized in orchestrator plaintext.
  Externally-hosted and user-created agents MUST still
  authenticate their transport.
- An owner or admin MAY mark a first-party agent "safe", which
  flips that agent's per-call permission baseline from deny to
  allow at evaluation time. This is bounded and never a runtime
  bypass: an explicit per-user opt-out MUST always override it,
  security-flag hard-blocks MUST never be cleared by it, the
  marking and every transition MUST be recorded in the audit
  log, and the marking MUST be reset when the agent's code is
  revised (re-approval required).
- Delegation MAY be recursive: an agent holding a delegated
  token MAY mint a further-attenuated child token for a
  sub-agent or a dynamically-created agent, expressed as a
  nested RFC 8693 `act` chain within the existing token
  construction — no new token format. Recursive delegation MUST
  preserve four invariants at every hop: monotonic scope
  attenuation (child scope ⊆ parent, child expiry ≤ parent), no
  privilege escalation (no scope, tool, audience, or relaxed
  security flag the parent lacks), actor-chain completeness (the
  nested `act` chain records every actor and terminates at the
  human principal), and depth-bounding (a configurable maximum,
  enforced at mint AND at verify). A child MUST NOT outlive,
  exceed, or survive revocation of its parent. These invariants
  MUST be property-tested, and every mint and every enforced use
  MUST append a provenance record — carrying the acting agent,
  the human authorizer, the operation, the scope/policy context,
  and a tamper-evident timestamp — to the hash-chained audit.
  Recursive delegation MUST ship behind a fail-closed feature
  flag (default off); with the flag off, the single-hop
  delegation path MUST be unchanged.
- **Cresco** (the CrescoEdge distributed edge-computing fabric) is
  an approved external-infrastructure integration. It MUST be
  reached ONLY through a first-party Python bridge agent over the
  fabric's `wsapi` WebSocket seam (any supporting library,
  including `pycrescolib`, is permitted under Principle V), behind a
  fail-closed feature flag (`FF_CRESCO`, default
  off). The Cresco fabric (a JVM/OSGi + ActiveMQ system) MUST remain
  external infrastructure: it MUST NOT be embedded in the product
  image, used as an internal message bus, or allowed to replace the
  A2A/WebSocket agent protocol (Principle I). Fabric credentials
  (`CRESCO_SERVICE_KEY`) are runtime-only secrets configured via the
  environment and never committed; dial-out MUST be egress-validated
  (`shared/external_http`) with verified TLS (no global verification
  bypass); and any tool wrapping Cresco's arbitrary-shell `executor`
  MUST be system-scoped, default-deny, and hard-security-flagged,
  never enabled by the safe-agent baseline.
- Secrets MUST NOT be committed to version control.

### VIII. User Experience

The UI MUST maintain a consistent design language while
supporting backend-driven dynamic generation.

- All UI rendering MUST be driven by primitives defined in the
  `astralprims` package and rendered by AstralProjection.
- AstralDeep MAY dynamically generate layouts by composing
  approved `astralprims` primitives, delivered as SDUI and
  adapted to the client target by ROTE.
- A new primitive MUST be approved by the owner, defined,
  documented, and released by AstralPrimitives, and adopted into
  AstralProjection's UI protocol manifest under their
  constitutions before AstralDeep emits it; AstralDeep adopts it
  by moving its pins (Principle XIV).

### IX. Data Plane Composition

AstralDeep's durable state MUST flow through AstralPlane.
AstralDeep owns product policy and orchestration, not SQL
schema, migrations, driver pools, or blob mechanics; ad-hoc SQL
against deployed environments is prohibited.

- AstralDeep MUST reach PostgreSQL and the configured blob
  stores only through AstralPlane's public facade, bound once
  per application by `backend/orchestrator/plane_composition.py`.
  It MUST NOT borrow connections, embed schema DDL, or
  reintroduce `backend/shared/database.py` or a second migration
  framework.
- Schema evolution happens only in AstralPlane's guarded,
  idempotent migration registry, under AstralPlane's
  constitution: a `SCHEMA_REVISION` bump, a repeat-safe
  migration, a rollback or recovery procedure, current-schema
  verification, and tests against representative existing data.
- Migrations MUST execute automatically when AstralDeep
  initializes the Plane runtime at startup. Manual DBA
  intervention MUST NOT be required for routine schema
  evolution.
- AstralDeep MUST pin the exact data-plane contract in
  `config/astral-composition.json` (contract version, schema
  revision, read-compatible floor, migration digest, and blob
  layout), and its Plane binding MUST fail closed when the
  installed AstralPlane differs from that pin.
- AstralDeep MUST move that pin only after the exact AstralPlane
  revision is qualified by AstralPlane's CI, and a pull request
  that moves the schema pin MUST cite that revision's
  representative-data migration evidence. A passing migration on
  an empty database is not sufficient evidence.

**Rationale**: Schema drift between code and database is the
most common cause of production outages after a deploy. One
registry in one repository, adopted through an exact pin that
fails closed, keeps every environment reproducible and makes
rollbacks deterministic.

### X. Production Readiness

Every change merged to the main branch MUST be production-ready
and thoroughly tested. "Done" means deployable, not "compiles
and the happy path works."

- No work-in-progress, stubbed, mocked, hard-coded, or
  debug-only code may be merged. `TODO`/`FIXME` markers MUST
  reference a tracked issue.
- Tests MUST exercise the golden path, edge cases, and error
  conditions for the changed behavior — in addition to
  satisfying the 90% coverage gate from Principle III where it
  applies.
- New features MUST include observability appropriate to their
  surface area (structured logs for failures, metrics for
  user-visible operations) sufficient to diagnose production
  incidents without code changes.
- Configuration MUST support production environments. No
  hard-coded localhost URLs, developer credentials, dev-only
  feature flags, or environment-specific branches in code.
- Changes that affect runtime infrastructure (database,
  authentication, deployment topology, container images,
  background workers) MUST be validated end-to-end in a
  staging environment before merge.
- A qualifying staging environment MAY be either a persistent shared
  environment or an ephemeral isolated candidate deployment. In both
  forms it MUST run the same candidate artifact or commit-derived image,
  use real configured authentication, database, background workers, and
  affected client flows, include representative data migrated through
  the product's normal migration mechanism, and produce evidence bound
  to the candidate SHA or immutable artifact digest. Mock-only fixtures,
  source-only test processes, and an empty database do not qualify as
  staging evidence.
- UI changes MUST be exercised against EVERY affected client
  target (e.g., a real browser for the web, the desktop client
  launched, the native mobile client on an emulator or device)
  running against the live backend before being declared
  complete; type-checks and unit tests do not verify feature
  correctness, and verifying one client does not stand in for
  the others (Principle XII). If a client runner or platform is
  temporarily unavailable for reasons independent of candidate
  behavior, a release MAY proceed only under an owner-approved,
  candidate- and release-bound evidence exception that names the
  missing platform/checks, explains the unavailability, expires
  within seven calendar days, and is recorded in an append-only
  protected ledger outside any candidate tree whose SHA it names. Approval
  MUST be independently machine-verifiable through
  a protected release-owner environment, ruleset, or signed approval
  attestation bound to that exception and candidate; a self-declared
  approver field is insufficient. The unavailable report MUST still bind the
  exact candidate artifact, qualifying staging identity, and a protected,
  independently re-hashed observation of the attempted target runner/platform;
  the approval MUST bind the exact exception bytes so its scope cannot be
  changed after review. The exception MUST NOT convert a product failure into
  unavailability or waive qualifying staging, real configured
  authentication/database/workers, representative migrated data,
  candidate/artifact identity and byte verification, evidence-
  policy integrity, or any check the owning feature designates
  non-waivable. Every exception MUST block the next release until
  that release supplies passing evidence for each recorded debt and
  durably appends a resolution receipt without rewriting history;
  a replacement exception does not resolve it.
- When candidate-controlled code or workflows can influence qualifying
  release evidence, policy, aggregation, or coverage, the final release
  decision MUST be produced by a candidate-independent protected verifier
  pinned outside the candidate. It MUST reconstruct bounded current-run
  inputs from trusted provider/API identities, execute protected pinned policy
  code, attest the exact decision and inputs, and own the repository-required
  gate through its installed protected workflow identity, not a name-only
  status. A caller job, candidate script, same-name check, self-declared result,
  or candidate-supplied manifest MUST NOT substitute for that decision. The
  decision MUST have a bounded validity window, and any consuming publisher
  MUST reject it after expiry.
- Release evidence collection, normalization, and human-readable parsing MUST
  normally be completed locally before any candidate push for every feature
  that uses release evidence. The local command MUST be deterministic, fail
  closed, preserve the canonical machine evidence and its digests, and emit no
  authorization claim. Local output is a developer/reviewer diagnostic only.
- A release-evidence **bootstrap window** MAY replace that ordinary pre-push
  ordering only when every following condition is satisfied:
  1. At least one required canonical input structurally depends on a
     provider-issued workflow identity or native artifact that cannot exist
     until the exact clean candidate SHA is remotely addressable. Missing
     infrastructure, skipped locally executable work, convenience, elapsed
     runtime, or an ordinary test failure is not sufficient.
  2. Before the first bootstrap push, a lead developer explicitly approves a
     named feature, branch, and pull request; the structural blocker; the exact
     initial candidate SHA; a path-bounded candidate and CI/evidence-repair
     scope; a passed-local-gate attestation naming the exact commands and
     binding the digests of every locally required, parseable coverage input;
     and an absolute RFC 3339 UTC expiry no more than 168 hours after approval.
     The attestation records lead review and MUST NOT substitute for actually
     running those commands. Authority MUST be an immutable or edit-detectable
     provider-native PR review/comment or protected receipt outside the candidate tree, with the
     actor verified against the repository's lead-developer allowlist. An
     in-tree verification record MAY mirror that authority but cannot create it.
     The allowlist and verifier MUST be loaded from the current default branch,
     never from candidate-controlled bytes, and their exact identities MUST be
     retained with the verification result.
  3. The clean committed candidate MUST first pass every required check that is
     locally executable. Its deterministic evidence command MUST run as far as
     possible and fail closed with a retained machine-readable inventory of
     the exact missing provider-bound inputs. Locally required coverage inputs
     MUST be present, non-empty, structurally parseable, and digest-bound into
     the approval. The inventory MUST be bound to the
     clean candidate SHA and retained outside the candidate tree so recording it
     cannot change that SHA; fabricated reports, relabeled older artifacts, or
     invented provider identities are prohibited.
  4. The branch MUST be non-protected and the PR MUST remain draft and
     non-mergeable throughout the window. Candidate-controlled bootstrap jobs
     MUST use isolated provider-hosted runners and receive no repository or
     environment secrets, protected environment, OIDC/cloud identity, privileged
     self-hosted runner, signing, deployment, package, tag, release, or
     protected-ledger mutation authority. Their job token MUST be explicitly
     least-privilege and read-only except for provider-native check reporting
     and artifact upload. A separately protected evidence producer MAY create
     an immutable non-release candidate artifact and isolated qualifying staging
     namespace only when required to generate evidence; neither may be promoted,
     distributed, or mistaken for a published release.
     A candidate ref MUST NOT be eligible for manual dispatch of a workflow
     with secrets, protected environments, write/OIDC authority, privileged
     runners, signing, deployment, or publication capabilities; each such
     dispatch-capable job MUST enforce an exact default-branch-ref guard.
  5. Before each bootstrap push, the candidate-independent default-branch
     verifier MUST re-query the provider-native approval, current draft state,
     exact prior/new head, expiry, and changed paths. Changes after the first
     push MUST stay within the approved path scope and be limited to repairs
     required to make the approved candidate's CI/evidence producers execute
     correctly. They MUST NOT
     weaken assertions, thresholds, evidence semantics, security/privacy
     controls, dependency locks, or product behavior. Any other change requires
     a new lead-approved scope and expiry. Each new SHA invalidates all prior
     candidate-bound evidence and MUST receive a new provider-native SHA record.
     Expiry freezes further bootstrap pushes and promotion until a newly reviewed
     window is established; repeated approval is never automatic.
  6. Before the PR leaves draft, before any non-bootstrap implementation push,
     and before merge, production deployment, distribution, signing, or release,
     the final exact SHA MUST complete the full deterministic local parser over
     canonical inputs and pass the independently protected decision required
     below.
- A bootstrap window does not waive qualifying staging, real configured
  dependencies, representative migrated data, affected-client evidence,
  changed-code coverage, candidate/artifact identity, trust reconstruction,
  evidence-policy integrity, security/privacy checks, or any check the owning
  feature designates non-waivable. It authorizes remote diagnostic evidence
  production only and makes no merge or release claim. If unavailable
  infrastructure prevents final closure, the PR MUST remain draft and blocked;
  bootstrap eligibility does not imply that closure is currently possible.
- Protected CI MUST independently schema-validate and re-hash submitted
  canonical evidence, reconstruct trusted provider/workflow identities, and
  execute its pinned policy before producing any merge or release decision;
  CI MUST NOT trust a committed local verdict merely because its parser passed.
- Candidate-controlled code or workflows MUST NOT possess signing credentials,
  OIDC publication authority, or public-release mutation permission. Release
  signing, tag/release creation, asset upload, verification, and public
  transition MUST execute in a separately pinned protected publisher that
  consumes the attested protected decision, reconstructs immutable inputs,
  refuses collisions/replacement, and is governed by protected release-owner
  approval. An unprivileged candidate workflow MAY only request publication.
  A compatibility signer required by an already-shipped pinned verifier MAY
  receive OIDC without mutation authority only when its exact workflow bytes
  equal an independently installed protected template, its ref can be created
  only by that protected publisher, and it signs only the exact protected-
  decision artifact. It MAY receive only the minimum non-mutating repository
  and workflow-artifact read scopes needed to retrieve and re-hash that exact
  input; under those constraints it is a protected bridge, not a candidate-
  controlled publisher.
- Protected release verification and publication MUST use the repository's
  native GitHub Actions controls: immutable workflow identity, protected refs
  and environments, required reviewers where applicable, and the built-in
  short-lived `GITHUB_TOKEN` with job-scoped least privilege. AstralDeep's
  release path MUST NOT create or depend on a repository-scoped GitHub App,
  GitHub App installation token, or custom token broker. Only the separately
  protected publisher job MAY receive release-mutation or OIDC authority;
  an environment-approved exception/debt job MAY receive only the narrowly
  protected branch write needed for an append-only debt or resolution record.
  Ordinary CI and candidate evidence jobs MUST remain read-only.
- The separate `pr-ci-notifications.yml` metadata controller MAY use only
  `actions: read`, `issues: write`, and `pull-requests: write` with the built-in
  token to report current-head CI failures and request maintainer review after
  all applicable qualification workflows pass. It MUST run only on exact
  `refs/heads/main` through the reviewed, commit-pinned community action,
  serialize completion events and recovery, check out no repository code,
  execute no PR input, download no artifacts, and use no secrets, OIDC,
  contents-write, approval, rerun, merge, publishing or release authority.
  Its contract tests MUST preserve those boundaries. First-time contributor
  run approval remains manual. This controller does not qualify product
  changes or replace maintainer code review.

**Rationale**: A change that is "almost done" is a future
incident. Setting the merge bar at production-ready — not
"works on my machine" — keeps the main branch continuously
deployable and prevents stub code from rotting in place.

### XI. Continuous Integration

The repository MUST carry an automated CI pipeline that runs on
every pull request and every push to the main branch. Its gates
are the enforceable definition of "passes CI" wherever this
constitution requires it.

- The pipeline MUST run, as independently-reported checks:
  1. **Lint** — Python lint from the repository-root configuration
     plus standard lint for every maintained client language changed by
     the candidate, explicitly including TypeScript/JavaScript when
     present (Principle IV). Language-specific invocations MAY share one
     check only when their outputs and failures remain clearly partitioned.
  2. **Tests** — the complete backend test suite (default suite
     plus all module suites) against a real database service,
     excluding only tests that require a live deployed
     orchestrator. The suite MAY run as separate parallel jobs
     of whole suites; the required suite set is still complete.
  3. **Coverage** — the changed-code coverage gate at ≥ 90%
     (Principle III), recorded as not applicable when a change
     contains no measurable executable lines.
  4. **Image build** — the production container image MUST
     build from a clean checkout on every run.
  5. **Boot smoke** — the built image MUST answer its liveness
     and readiness probes in development posture, and a
     production-posture boot with placeholder or missing
     secrets MUST exit with the documented configuration-error
     code, proving the fail-closed gate end-to-end.
  6. **Secret scan** — committed credential material MUST fail
     the pipeline (Principle VII).
- **Time budget** — Every CI job and every test suite MUST finish
  within 30 minutes on the project's GitHub-hosted runners, and
  every CI job MUST declare `timeout-minutes` of at most 30. A
  suite that exceeds the budget MUST be brought within it by
  making its fixtures cheaper or by deleting its slowest tests —
  never by splitting one suite across runners, raising the
  limit, or waiving the gate. A job that exceeds the budget only
  because it bundles several suites MUST be split into jobs that
  each run whole suites. Release build and signing jobs are not
  test gates.
- **No soak tests** — Tests whose value comes from repeating one
  scenario many times or sustaining load to surface rare failures
  are not part of CI or release qualification. A property that
  needs proof is proved with the fewest cycles that exercise it:
  one lifecycle, or two when the assertion compares growth
  between cycles. Deterministic enumeration, bounded load needed
  to reach a limit, and latency percentiles over a sample are
  measurements, not soak tests.
- **Fair gates** — A gate MUST fail only for a defect the change
  introduced or can fix. An immutable changed-line comparison
  that contains no measurable executable lines (for example a
  component-pointer bump, or a component change confined to
  paths the gate does not measure) is recorded as an explicit
  not-applicable outcome together with the paths it considered,
  not as a failure. Release tooling that requires a passing
  decision still fails closed.
- **Deterministic gates** — Required gates MUST NOT depend on
  live third-party network services, exact equality or exact
  bounds on clock-derived values, or wall-clock performance
  bounds on shared hosted runners. Per-test retries are
  permitted; automatic whole-suite reruns are not.
- On main-branch pushes that pass all gates, the pipeline MUST
  publish the container image to the project's container
  registry with an immutable commit-derived tag and a moving
  latest tag. The published image is the production deployment
  artifact.
- Verification failures and publish failures MUST be
  distinguishable; a publish failure MUST NOT mask green
  verification gates.
- CI-only tooling (linters, coverage tools, scanners) may be
  installed in the pipeline environment freely under Principle
  V; it is declared in the CI tooling manifest.
- No branch may merge to main with a failing required gate.

**Rationale**: Principles III, IV, VII, and X are only as real
as their enforcement. A named, versioned gate set turns
"production ready" from a review opinion into a reproducible
machine verdict, and the registry-published image makes every
green main commit deployable by pull.

### XII. Cross-Client Consistency

Every client target MUST present the same user-facing
capabilities, application chrome, and visual design language.
AstralProjection's constitution governs how the clients, the UI
protocol manifest, ROTE, and the drift guards achieve this; this
principle governs AstralDeep's part.

- AstralDeep MUST give every client the same server-owned
  chrome, menu, settings surfaces, and theme preference through
  Projection's shared chrome model and its own host adapters. No
  client-specific capability catalog may be defined in
  AstralDeep.
- Role-gating (e.g. admin-only surfaces) MUST derive from
  AstralDeep's verified roles and MUST be enforced server-side on
  every client channel, regardless of what a client displays.
- A capability MAY be deliberately scoped **web-only** when it is
  inherently tied to something other clients genuinely lack (e.g.
  admin-only desktop tooling, or a walkthrough anchored to
  web-DOM elements), PROVIDED it is (a) documented in the owning
  feature spec, (b) enforced from the shared server-owned
  definition through the channel AstralDeep selects — omitted
  from native menu channels by the server, not hidden
  client-side — and (c) applied uniformly to every non-web
  client. The admin surfaces (Tool quality, Tutorial admin) and
  "Take the tour" are the current web-only capabilities.
- A change to shared UI behavior — protocol frames, component
  types, chrome, theming, or layout — MUST land on every in-scope
  client within the same feature under AstralProjection's
  constitution (or record an explicit, justified divergence in
  the owning spec, per the web-only carve-out), and AstralDeep
  MUST adopt it by moving its Projection pin and UI protocol
  digest together.
- A change to a user-facing capability MUST reach every client
  through the shared server-owned definitions without a
  per-client change wherever those definitions make that
  possible; where a client cannot yet render something, it
  degrades to a labeled placeholder and never silently omits or
  diverges.
- UI changes MUST be verified live on every affected client
  target (Principle X).

**Rationale**: Divergent clients erode trust, multiply
maintenance, and produce exactly the drift this project has
already seen. Anchoring every client to one server-owned
definition, supplied by AstralDeep and rendered by
AstralProjection, makes "the clients match" a structural
guarantee rather than a recurring manual reconciliation.

### XIII. Documentation & Research Integrity

Some features deliver documentation, research, or decision
records rather than runtime code (for example, thesis-defense
artifacts, related-work analyses, and go/no-go decision briefs).
Such deliverables are fully governed, but the gates that assume
executable code apply only where code exists.

- A feature whose diff is confined to documentation, research,
  or decision artifacts (plus its own spec) and touches no
  product runtime code, database schema, wire protocol, or
  client is a **documentation/research-only** feature. It is
  exempt from Principle III (changed-code coverage) and from the
  code-execution gates of Principles X and XI (the test suite,
  boot smoke, and image-behavior checks) to the precise extent
  that it introduces no code for those gates to exercise. The
  lint, secret-scan, and image-build gates continue to apply to
  the repository as a whole.
- In exchange, such deliverables MUST meet a
  documentation-integrity bar: every external claim MUST be
  traceable to a cited source; citations to living documents
  (IETF drafts, arXiv papers, package releases) MUST be pinned
  with identifier, status, and retrieval date; no source,
  quotation, or decision outcome may be fabricated; a decision
  record MUST honestly reflect its state (e.g., PENDING versus
  RATIFIED) and MUST NOT assert an outcome not yet made; and no
  secret may be committed.
- Where a documentation deliverable makes a claim about the
  system's own behavior, that claim MUST match the code as
  merged. A differential or benchmark that overclaims relative
  to verified evidence MUST be refined down to the evidence.
- Documentation/research features remain subject to review
  (Governance) and to Principle VI wherever they document APIs
  or primitives.

**Rationale**: A research program produces durable prose —
framing memos, related-work differentials, decision records —
whose correctness is a matter of sourcing and honesty, not test
coverage. Holding those artifacts to a citation-and-honesty bar,
while exempting them from gates that presuppose runtime code,
keeps the constitution meaningful for both kinds of work and
prevents either a vacuous "90% coverage of zero code" ritual or
an ungoverned back channel for unverified claims.

### XIV. Component Composition

AstralDeep composes four pinned components, each governed by
the constitution in its own repository: AstralPrimitives
(primitive definitions and serialization), AstralProjection
(rendering, ROTE, the UI protocol, and the native clients),
AstralPlane (durable state), and LETS (the external
lineage-escrow authority).

- Components live as Git submodules under `components/` and are
  pinned by exact commit, contract version, and compatibility
  digests in `config/astral-composition.json`. AstralDeep MUST
  consume them only through those declared contracts.
- AstralDeep MUST NOT edit component source in its own tree. A
  component change lands first in the component's repository
  under that repository's constitution; AstralDeep adopts it only
  by moving the pin, and only after the exact component revision
  is qualified by the component's own CI.
- AstralDeep MUST verify the composition in CI and at boot, and a
  mismatch between a pin and the installed component fails
  closed. The required embedded components (AstralProjection,
  AstralPlane, and AstralPrimitives) MUST be present. LETS is
  external and feature-gated; when enabled it MUST be reached
  only through its pinned release contract (API version, OpenAPI
  digest, receipt wire type, and scope profile), failing closed
  on any mismatch.
- A change that crosses repositories — a primitive, a UI
  protocol change, a schema revision, or a LETS contract — MUST
  meet the constitution of each repository it lands in, in
  dependency order: the component first (AstralPrimitives before
  AstralProjection), AstralDeep last.
- Where a component constitution and this constitution disagree
  about work inside that component, the component's constitution
  governs; this constitution governs AstralDeep's consumption,
  composition, and release of the component.

**Rationale**: Each component has its own release train and
trust boundary. Exact pins over declared contracts keep the
composed product reproducible, while letting each repository
evolve under rules written for what it owns.

## Technology Stack

- **Backend**: Python (FastAPI or equivalent ASGI framework),
  compatible with the production Python 3.11 image
- **UI Delivery**: Server-driven UI (SDUI) delivered via
  FastAPI; UI primitives **defined** by AstralPrimitives
  (`astralprims`); **rendered** and **adapted** per device by
  AstralProjection (webrender and ROTE); **orchestrated** by
  AstralDeep; the Windows, Android, and Apple clients live in
  AstralProjection
- **Authentication**: Keycloak IAM
- **Agent Auth**: RFC 8693 token exchange with attenuated scopes
- **Data Plane**: PostgreSQL and configured blob stores through
  AstralPlane; schema evolution in AstralPlane's guarded
  migration registry, executed automatically at startup, with
  the exact schema pin in `config/astral-composition.json`
- **Components**: AstralPrimitives, AstralProjection,
  AstralPlane, and LETS as Git submodules under `components/`,
  pinned in `config/astral-composition.json` and each governed by
  its own constitution (Principle XIV)
- **Continuous Integration**: GitHub Actions running the
  Principle XI gate set; deterministic release evidence normally prepared
  locally before push, with a bounded draft-only bootstrap for structurally
  provider-bound inputs, then independently validated by protected CI;
  container images published to GitHub Container Registry; protected
  publication uses native short-lived GitHub Actions identity rather than
  repository-scoped Apps
- **Eval/Benchmark Harnesses**: dependencies declared in their
  own manifests (e.g., `requirements-eval.txt`) per Principle V;
  harness principals namespaced and torn down
- **Cresco Integration**: the CrescoEdge edge-computing fabric is
  approved external infrastructure, reached only via a first-party
  Python bridge agent over its `wsapi` WebSocket seam (any
  supporting library permitted under Principle V), behind
  `FF_CRESCO` (default off); the JVM fabric is never embedded in the
  product image (Principles I, VII)
- **Defense-track Documentation**: documentation/research-only
  artifacts are governed by Principle XIII; the `docs/` tree is
  gitignored, so any such artifacts are kept outside the public
  repository (not version-controlled here)
- **Containerization**: Docker / Docker Compose
- **License**: Apache 2.0

## Development Workflow

- All changes MUST go through pull requests unless the owner
  explicitly authorizes a direct push; a directly pushed change
  is held to the same Principle XI gates on `main`.
- PRs MUST pass the Principle XI CI gate set (maintained-language lint, tests,
  changed-code coverage, image build, boot smoke, secret scan)
  before merge.
- Schema changes land in AstralPlane. A PR that moves
  AstralDeep's Plane schema pin MUST cite the qualified
  AstralPlane revision and its representative-data migration
  evidence (Principle IX).
- Primitive definitions change in AstralPrimitives and their
  presentation in AstralProjection, each under its own
  constitution. Changes to files under `components/` MUST land
  in the component's own repository; an AstralDeep PR changes
  only the pin (Principle XIV).
- PRs that change the application chrome (top-bar controls or
  the settings menu), the shared visual design language
  (palette/theme tokens), or that add, remove, or relocate a
  user-facing capability MUST update the single server-owned
  definition (Projection's chrome model or AstralDeep's host
  adapters) rather than any per-client copy, MUST keep the
  UI-protocol manifest and every client's drift-guard suites
  green, and MUST verify live parity on every affected client
  target before merge or carry the narrowly bounded platform-
  unavailability evidence exception permitted by Principle X
  (Principle XII).
- PRs MUST be production-ready before merge — reviewers MUST
  reject changes that contain stubs, debug-only code, missing
  observability for new features, or untested error paths.
- PRs that affect release evidence or publication MUST run the deterministic
  local evidence preparation/parser before ordinary pushes, retain its
  canonical inputs and digests, and keep independent protected-CI validation
  authoritative. If provider-bound evidence cannot structurally exist before
  an exact SHA is remote, the PR MAY use only Principle X's lead-approved,
  seven-day, draft-only bootstrap window; all locally executable checks and the
  fail-closed missing-input inventory run first, each SHA is recorded, and the
  full exact-candidate local/protected gates MUST close before the PR leaves
  draft or any merge/release action occurs. Publication remains in protected CI
  with the built-in short-lived token; repository-scoped GitHub Apps or custom
  token brokers MUST NOT be introduced.
- Constitution compliance MUST be verified during code review.
- Reviewers MUST reject changes that add comments or docstrings
  beyond Principle VI (a file header plus rare one-line *why*
  comments) and MUST confirm that every new source file carries
  its header.
- Each PR MUST reference relevant spec/task IDs when
  applicable; those IDs belong in the PR, never in source.

## Governance

This constitution is the highest-authority document governing
AstralDeep development practices. It supersedes all other
guidance when conflicts arise.

- **Scope**: This constitution governs the AstralDeep repository
  and AstralDeep's consumption, composition, and release of its
  pinned components. AstralPrimitives, AstralProjection,
  AstralPlane, and LETS are each governed by the constitution in
  their own repository (`.specify/memory/constitution.md`), which
  is authoritative for work inside that repository
  (Principle XIV).
- **Amendments**: Any change to this constitution MUST land
  through a PR unless the owner explicitly authorizes a direct
  push. Each amendment MUST be approved by the owner or a lead
  developer and MUST record its rationale, version change, and
  Sync Impact Report in the PR or commit message; the report is
  not kept in this file.
- **Versioning**: This document follows semantic versioning.
  MAJOR for principle removals/redefinitions, MINOR for new
  principles or material expansions, PATCH for clarifications
  and wording fixes.
- **Compliance**: All PRs and code reviews MUST verify
  adherence to these principles. Violations MUST be resolved
  before merge, and known shortfalls are tracked as follow-up
  work until closed.

**Version**: 6.1.0 | **Ratified**: 2026-03-11 | **Last Amended**: 2026-10-01
