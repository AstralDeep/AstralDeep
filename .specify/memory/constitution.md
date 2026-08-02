<!--
  Sync Impact Report
  ==================
  Version change: 2.8.0 → 2.9.0 (MINOR — Principle X materially adds a
    fail-closed bootstrap path for evidence inputs that cannot exist until an
    exact candidate SHA is remotely addressable, without weakening merge or
    release authorization)

  Amendment (2026-08-02, v2.9.0) — candidate evidence bootstrap:
    X. Production Readiness (EXPANDED) — release evidence still runs locally
        before ordinary pushes. A lead-approved, time-bounded bootstrap window
        may now admit an exact clean candidate to diagnostic remote CI and
        protected evidence producers only when required provider-issued
        identities or native artifacts cannot exist before that SHA is remotely
        addressable. The PR remains draft and non-mergeable; locally executable
        gates and a missing-input inventory run first; every bootstrap SHA and
        repair is recorded; and the final exact SHA must complete the full local
        parser plus protected CI before promotion, merge, production deployment,
        distribution, or release. Bootstrap may not waive staging, coverage,
        trust, evidence integrity, security, or any feature-designated
        non-waivable check.
    Technology Stack — Continuous Integration now distinguishes diagnostic
        evidence-bootstrap CI from merge/release authority.
    Development Workflow — release-affecting PRs must either complete the
        ordinary local-before-push gate or satisfy every bounded bootstrap
        condition and remain draft until the exact-candidate gate closes.
    Principles added: None
    Principles removed: None
    Sections added: None
    Sections removed: None
    Templates and guidance requiring updates:
      ✅ .specify/templates/plan-template.md — Constitution Check now requires
         bootstrap eligibility, scope, expiry, preflight, and closure design
      ✅ .specify/templates/spec-template.md — generic and compatible
      ✅ .specify/templates/tasks-template.md — polish examples now include
         bounded bootstrap setup and exact-candidate closure
      ✅ AGENTS.md — release-evidence guidance now records the bootstrap limits
      ✅ .github/release-evidence-leads.json — default-branch lead allowlist for
         provider-native approval verification
      ✅ scripts/verify_release_evidence_bootstrap.py — default-branch verifier
         and external missing-input inventory producer
      ✅ scripts/prepare_release_evidence.py — optional machine-readable
         fail-closed receipt distinguishes structural missing-provider inputs
         from other evidence rejection
      ✅ backend/tests/test_release_evidence_bootstrap.py — golden and denial
         coverage for approval, scope, expiry, draft, dispatch, and inventory
         checks
      ✅ backend/tests/test_prepare_release_evidence_060.py — verifies the
         structural-failure receipt and generic rejection boundary
      ✅ .github/workflows/ci.yml — lock-pinned host tooling lane executes and
         measures the default-branch bootstrap verifier; draft candidates
         cannot invoke privileged readiness, and full-history secret scanning
         uses a checksum-pinned CLI without a repository license secret
      ✅ .github/workflows/apple-release.yml,
         .github/workflows/build-windows-candidate.yml,
         .github/workflows/release-evidence-exception.yml,
         .github/workflows/release-readiness.yml,
         .github/workflows/release-windows-publisher-controller.yml, and
         .github/workflows/release-windows.yml — privileged manual-dispatch
         jobs refuse candidate refs and run only from the default-branch ref
      ✅ backend/tests/test_release_workflows_060.py — static workflow guard
         requires the bootstrap verifier tests in the measured host lane and
         the privileged-readiness draft and candidate-ref dispatch exclusions
      ✅ backend/tests/test_python_ci_supply_chain_060.py — pins and verifies
         the secret-free Gitleaks CLI supply chain
      ✅ .gitleaksignore — exact fingerprints retain 12 reviewed historical
         false positives without weakening future or path-wide detection
      ✅ specs/060-runtime-reliability-hardening/verification/dependencies.md —
         records the Gitleaks action-to-checksum-pinned-CLI transition
      ✅ specs/029-agents-adaptive-ui-ci/contracts/ci-pipeline.md — authoritative
         secret-scan contract now names the checksum-pinned, secret-free CLI
      ✅ docs/production-deployment.md and
         specs/053-apple-production-release/contracts/release-pipeline.md and
         apple-clients/README.md — Apple manual release is documented as
         default-ref-only
      ✅ specs/060-runtime-reliability-hardening/quickstart.md — manual
         readiness reruns dispatch protected default-branch workflow bytes and
         bind candidate SHA separately
      ✅ .specify/templates/commands/ — directory absent; no command templates
         require changes
    Transition: this amendment PR is a governance proposal only. It authorizes
        no product bootstrap until reviewed and merged; existing feature work
        remains governed by v2.8.0 meanwhile.
    Follow-up TODOs:
      ⚠ Feature 065 must update its stricter T003/quickstart/verification text
        and create the external approval plus missing-input inventory before
        invoking this bootstrap path.

  -- Prior amendment (2026-07-16, v2.8.0) --------------------------------
  Version change: 2.7.0 → 2.8.0 (MINOR — Principle X materially adds a
    local-before-push release-evidence boundary and standardizes protected
    publication on native CI identity without repository-scoped GitHub Apps)

  Amendment (2026-07-16, v2.8.0) — local evidence and native CI publication:
    X. Production Readiness (EXPANDED) — release evidence is collected,
        normalized, and parsed locally before push. Local output is diagnostic,
        not release authority; protected CI independently validates canonical
        evidence, identities, digests, and pinned policy before authorizing a
        release. Protected verification and publication remain CI jobs, using
        native short-lived GitHub Actions identity and job-scoped permissions.
        AstralDeep release workflows must not require repository-scoped GitHub
        Apps, installation tokens, or a custom token broker. Candidate jobs
        remain read-only, while the separately protected publisher receives
        release-write/OIDC authority only inside its approved publication job.
        When the bounded exception policy is used, protected CI performs the
        approved append-only debt/resolution transition with a narrowly gated
        branch write; local and candidate jobs cannot authorize it.
    Technology Stack — Continuous Integration now records local pre-push
        evidence preparation and the native protected-publisher identity.
    Development Workflow — release-affecting PRs must run the local evidence
        preparation gate before push and preserve independent CI validation.
    Templates requiring updates:
      ✅ .specify/templates/plan-template.md — Constitution Check now prompts
         for the local/CI evidence boundary and native protected publisher
      ✅ .specify/templates/spec-template.md — generic and compatible
      ✅ .specify/templates/tasks-template.md — polish examples now include
         local evidence preparation and native CI publication validation
    Follow-up TODOs: None

  -- Prior amendment (2026-07-15, v2.7.0) --------------------------------
  Version change: 2.6.1 → 2.7.0 (MINOR — Principle X materially adds a
    bounded, auditable exception model for temporary client-platform
    unavailability while preserving non-waivable staging and trust gates)

  Amendment (2026-07-15, v2.7.0) — bounded platform-unavailability evidence:
    X. Production Readiness (EXPANDED) — every affected live client remains
        required, but a temporarily unavailable runner/platform may use one
        owner-approved, candidate-bound exception lasting at most seven days;
        approval must be independently machine-verifiable through a protected
        release-owner control, never a self-asserted field.
        When candidate-controlled code or workflows can influence release
        evidence, a candidate-independent protected verifier pinned outside
        the candidate must reconstruct the inputs, execute pinned policy, and
        attest the required final decision; the repository gate binds that
        installed workflow identity and consumers enforce decision expiry.
        Candidate-controlled workflows also cannot hold or exercise signing
        or public-release mutation authority; a separately pinned protected
        publisher must consume the protected decision.
        Exceptions cannot hide a product failure or waive qualifying staging,
        real dependencies/data/workers, candidate/artifact identity, evidence
        integrity, or a feature-designated non-waivable check. The append-only
        protected ledger stays outside the candidate tree; its debt requires a
        later pass plus a durable resolution receipt.
        Candidate-influenced release evidence requires a pinned protected final
        verifier/required decision, and signing/public mutation requires a
        separately pinned protected publisher. An already-shipped signer SAN
        may use only an exact-byte-pinned, mutation-free compatibility bridge
        with the minimum read scopes needed to retrieve its exact protected-
        decision artifact plus OIDC signing authority.
    Development Workflow — live parity verification now points to Principle
        X's bounded exception rules rather than contradicting them.
    Templates requiring updates:
      ✅ .specify/templates/plan-template.md — Constitution Check now prompts
         for exception boundaries/debt, protected final verification, and
         protected publication in addition to staging and lint
      ✅ .specify/templates/spec-template.md — generic and compatible
      ✅ .specify/templates/tasks-template.md — setup/polish examples now
         include exception-policy/debt, protected-verifier, and publisher
         validation
    Follow-up TODOs: None

  -- Prior amendment (2026-07-15, v2.6.1) --------------------------------
  Version change: 2.6.0 → 2.6.1 (PATCH — Principle X clarifies what
    qualifies as a staging environment, and Principles IV/XI plus the
    development workflow make maintained-language lint enforcement
    mechanically consistent)

  Amendment (2026-07-15, v2.6.1) — candidate staging and lint enforcement:
    IV. Code Quality (CLARIFIED) — maintained JavaScript/TypeScript requires
        a tracked standard lint configuration and an automated CI gate; a
        test-only linter remains governed by the existing Principle V/XI
        isolation carve-out and does not become a product dependency.
    X. Production Readiness (CLARIFIED) — staging may be a persistent shared
        environment or an ephemeral isolated candidate deployment. It
        qualifies only when it runs the same candidate artifact/SHA with real
        configured authentication, database, workers, and affected clients
        against representative migrated data, and emits evidence bound to the
        candidate. Mock-only or source-only local checks do not qualify.
    XI. Continuous Integration (CORRECTED) — the independently reported lint
        gate covers Python and every maintained client language changed by the
        candidate, explicitly including JavaScript/TypeScript, instead of
        naming only Python despite Principle IV's broader requirement.
    Development Workflow — the PR lint gate is described as maintained-
        language lint so reviewers cannot treat the Python check as complete
        evidence when client JavaScript/TypeScript changes.
    Templates requiring updates:
      ✅ .specify/templates/plan-template.md — Constitution Check now prompts
         for qualifying staging topology/evidence and maintained-language lint
      ✅ .specify/templates/spec-template.md — generic and compatible
      ✅ .specify/templates/tasks-template.md — setup/polish examples now
         include per-language lint and candidate-bound staging evidence
    Follow-up TODOs: None

  -- Prior amendment (2026-07-08, v2.6.0) --------------------------------
  Version change: 2.5.0 → 2.6.0 (MINOR — Principle XII materially expanded:
    device-client consistency now covers the visual design language — one
    shared palette/theme across every client — and layout parity on
    similarly sized devices, with the committed UI-protocol manifest and
    per-client drift-guard suites named as the mechanical enforcement)

  Amendment (2026-07-08, v2.6.0) — device client consistency:
    XII. Cross-Client Consistency (EXPANDED) — every device/client that
        connects to the orchestrator+ROTE backend (web, Windows desktop,
        Android, Apple, and any future ROTE target) MUST be consistent
        with every other client on three axes:
        (a) COLORS/THEMING — one server-owned palette/theme definition
            (including the user's selected theme) drives every client;
            equivalent components use the same palette roles on every
            client; no client hard-codes or locally forks a divergent
            palette;
        (b) LAYOUT — devices of a comparable form-factor class receive
            equivalent layout for the same content; divergence between
            similarly sized devices MUST trace to a declared ROTE
            device-profile capability difference, never to which client
            codebase is connecting;
        (c) FUNCTIONALITY — capability/chrome parity as already required
            by this principle.
        Enforcement named: the committed UI-protocol manifest
        (backend/shared/ui_protocol.json) plus the parity/drift-guard
        suites in every client test stack; a change to shared UI behavior
        (frames, component types, chrome, theming) lands on all in-scope
        clients in the same feature or records an explicit, justified
        divergence in the owning spec (web-only carve-out pattern).
    Development Workflow — chrome-parity PR bullet extended to cover the
        shared visual design language (palette/theme tokens) and to
        require keeping the manifest + drift guards green.
    Templates requiring updates:
      ✅ .specify/templates/plan-template.md — generic Constitution Check, compatible
      ✅ .specify/templates/spec-template.md — generic, compatible
      ✅ .specify/templates/tasks-template.md — generic, compatible
    Follow-up TODOs: None

  -- Prior amendment (2026-07-06, v2.5.0) --------------------------------
  Version change: 2.4.0 → 2.5.0 (MINOR — Principle VII expanded: Cresco
    external-infrastructure integration sanctioned; Technology Stack gains
    a Cresco Integration entry; Defense-track Documentation entry corrected
    to note the docs/ tree is gitignored)

  Amendment (2026-07-06, v2.5.0) — sanctions spec 050 (Cresco integration):
    VII. Security (EXPANDED) — Cresco (the CrescoEdge distributed
        edge-computing fabric) is an APPROVED external-infrastructure
        integration, reached only via a first-party Python bridge agent over
        the fabric's wsapi WebSocket seam using existing dependencies (no new
        third-party library; pycrescolib not adopted at runtime), behind a
        fail-closed flag (FF_CRESCO, default off). The JVM/OSGi + ActiveMQ
        fabric stays external infrastructure — never embedded in the product
        image, used as an internal message bus, or allowed to replace the
        A2A/WebSocket agent protocol (Principle I). Fabric credentials are
        runtime-only secrets; dial-out is egress-validated with verified TLS;
        executor-wrapping tools are system-scoped, default-deny, and
        hard-security-flagged.
    Technology Stack — added "Cresco Integration"; corrected the
        "Defense-track Documentation" entry to note the docs/ tree is
        gitignored (research/decision artifacts kept outside the public
        repository). Principle XIII is unchanged and still governs any
        documentation/research-only feature.
    Templates requiring updates:
      ✅ .specify/templates/plan-template.md — generic Constitution Check, compatible
      ✅ .specify/templates/spec-template.md — generic, compatible
      ✅ .specify/templates/tasks-template.md — generic, compatible
    Follow-up TODOs: None

  -- Prior amendment (2026-07-02, v2.4.0) --------------------------------
  Version change: 2.3.1 → 2.4.0 (MINOR — Principle XIII added: documentation
    & research integrity; Principle V expanded: eval/benchmark-only dependency
    tier; Principle VII expanded: recursive provenance-bearing delegation;
    Principle XI clarified: eval-harness tooling carve-out)

  Amendment (2026-07-02, v2.4.0) — sanctions the thesis defense-track
    (specs 045–049):
    XIII. Documentation & Research Integrity (NEW) — a feature whose diff is
        confined to documentation/research/decision artifacts and its own spec,
        touching no product runtime code/schema/wire/client, is a
        "documentation/research-only" feature: exempt from the code-execution
        gates (III changed-code coverage; the test-suite/boot-smoke/image-
        behavior checks of X and XI) that have no code to exercise, but bound
        instead to a documentation-integrity bar — every external claim traced
        to a pinned, dated citation; no fabricated source, quotation, or
        decision outcome; decision records that honestly reflect PENDING vs.
        RATIFIED; self-claims that match the code as merged; no committed
        secret. Lint, secret-scan, and image-build gates still apply repo-wide.
        Sanctions specs 045 (framing memo + lock record), 046 (AIP
        differential), 049 (I-D decision brief + pending record).
    V. Dependency Management (EXPANDED) — a test-, eval-, or benchmark-only
        dependency (property-test generator, external attack-benchmark corpus)
        MAY be introduced without counting as a product-runtime dependency when
        it is declared in a separate manifest (e.g. requirements-eval.txt) never
        installed into the runtime layer, never imported by any product module,
        and enforced by an automated isolation guard that fails CI on a product
        import — still documented in the PR; promotion into the runtime still
        needs lead-dev approval. Sanctions spec 047 (requirements-eval.txt +
        isolation_check.py).
    VII. Security (EXPANDED) — delegation MAY be recursive: a delegated token
        MAY mint a further-attenuated nested-`act` child (no new token format)
        for a sub-agent or dynamically-created agent, subject to four
        property-tested invariants (monotonic attenuation, no escalation,
        actor-chain completeness, depth-bounding), a child that never outlives
        or exceeds its parent, per-hop provenance in the hash-chained audit, and
        a fail-closed flag (default off; flag-off = unchanged single-hop).
        Sanctions spec 048.
    XI. Continuous Integration (CLARIFIED) — the "CI-only tooling" carve-out
        explicitly covers an eval/benchmark harness's isolated dependencies,
        provided the Principle V isolation guard gates them.
    Templates requiring updates:
      ✅ .specify/templates/plan-template.md — generic Constitution Check, compatible
      ✅ .specify/templates/spec-template.md — generic, compatible
      ✅ .specify/templates/tasks-template.md — generic, compatible
    Follow-up TODOs: None

  -- Prior amendment (2026-07-01, v2.3.1) --------------------------------
  Version change: 2.3.0 → 2.3.1 (PATCH — Principle XII: deliberate,
    documented, server-enforced web-only capability carve-out clarified)

  Amendment (2026-07-01, v2.3.1):
    XII. Cross-Client Consistency (PATCH) — clarified that a capability MAY be
        deliberately scoped WEB-ONLY (offered on the web, intentionally absent
        from other clients) without being the accidental drift the principle
        forbids, PROVIDED the omission is enforced from the shared server-owned
        definition (the item is dropped from other clients' menu channels by
        the server, not hidden client-side), documented in the owning feature
        spec, and applied uniformly to every non-web client. Sanctions the
        existing admin-tools web-only scoping (feature 042) and the feature-043
        removal of "Take the tour" from the Windows and Android clients.
    Templates requiring updates:
      ✅ .specify/templates/plan-template.md — generic Constitution Check, compatible
      ✅ .specify/templates/spec-template.md — generic, compatible
      ✅ .specify/templates/tasks-template.md — generic, compatible
    Follow-up TODOs: None

  -- Prior amendment (2026-07-01, v2.3.0) --------------------------------
  Version change: 2.2.0 → 2.3.0 (MINOR — Principle XII added: cross-client
    consistency; Principle II expanded: shared server-owned chrome + minimal
    client wrapping; Principle X clarified: verify every affected client)

  Amendment (2026-07-01, v2.3.0):
    XII. Cross-Client Consistency (NEW) — every client target (web, desktop,
        native mobile, and any future client such as iOS) MUST present the
        same user-facing capabilities and application chrome, rendered from
        shared, server-owned definitions. No client may add, omit, rename,
        reorder, or otherwise diverge from those shared definitions; a new
        client is a thin consumer of them, never a parallel reimplementation.
    II. UI Delivery Architecture — expanded: the application chrome (top-bar
        controls + settings-menu structure) MUST be described ONCE, server-
        side, and consumed by every client; clients hold only minimal generic
        wrapping and MUST NOT reimplement generative UI or maintain a parallel
        per-client definition of the menu/chrome. Generative UI remains owned
        by the orchestrator (render) + ROTE (per-device adaptation).
    X. Production Readiness — clarified: a UI change MUST be exercised against
        EVERY affected client target (e.g. web in a real browser, the desktop
        client launched, the native mobile client on an emulator), not just
        one, before it is declared complete.
    Templates requiring updates:
      ✅ .specify/templates/plan-template.md — generic Constitution Check, compatible
      ✅ .specify/templates/spec-template.md — generic, compatible
      ✅ .specify/templates/tasks-template.md — generic, compatible
    Follow-up TODOs: None

  -- Prior amendment (2026-06-24, v2.2.0) --------------------------------
  Version change: 2.1.0 → 2.2.0 (MINOR — Principle VII expanded: in-process
    first-party agents + owner-safe marking)

  Amendment (2026-06-24, v2.2.0):
    VII. Security — added two clauses sanctioning feature 040
        (040-inprocess-agents-skills-commands):
      (a) First-party agents bundled with the system MAY run IN-PROCESS within
          the orchestrator trust boundary, exempt from the per-agent transport
          AGENT_API_KEY handshake, PROVIDED every runtime authorization control
          and the audit trail remain fully in force and per-user secrets are
          decrypted only inside the agent boundary (never materialized in
          orchestrator plaintext). External + user-created agents still
          authenticate their transport.
      (b) An owner/admin MAY mark a first-party agent "safe", flipping its
          per-call permission baseline deny→allow at evaluation time — bounded
          by an always-winning explicit user opt-out, never-cleared
          security-flag hard-blocks, full audit of every transition, and a
          reset whenever the agent's code is revised.
    Templates requiring updates:
      ✅ .specify/templates/plan-template.md — generic Constitution Check, compatible
      ✅ .specify/templates/spec-template.md — generic, compatible
      ✅ .specify/templates/tasks-template.md — generic, compatible
    Follow-up TODOs: None

  -- Prior amendment (2026-06-11, v2.1.0) --------------------------------
  Version change: 2.0.1 → 2.1.0 (MINOR — Principle XI added; III, IX clarified)

  Amendment (2026-06-11, v2.1.0):
    XI. Continuous Integration (NEW) — the repository MUST carry an automated
        CI pipeline (GitHub Actions) whose named gates are the enforceable
        definition of "passes CI" used throughout this constitution: lint,
        full test suite against a real database, changed-code coverage ≥ 90%,
        production image build, boot smoke (liveness/readiness probes + the
        fail-closed configuration gate), secret scan, and registry publish of
        a versioned image from the main branch.
    III. Testing Standards — clarified that the 90% threshold is measured on
        the lines changed by a PR (changed-code coverage), which is the gate
        CI mechanically enforces; aspirational module-wide coverage remains
        encouraged but is not the merge gate.
    IX. Database Migrations — corrected the framework clause to name the
        project's actual sanctioned mechanism: idempotent, guarded schema
        migrations executed automatically at application startup
        (shared/database.py::_init_db). The Alembic wording was an example
        that never matched reality. Technology Stack updated to match.
    Development Workflow — "PRs MUST pass CI checks" now references the
        Principle XI gate set explicitly.

  Templates requiring updates:
    ✅ .specify/templates/plan-template.md — generic Constitution Check gate,
       compatible (no gate enumeration embedded)
    ✅ .specify/templates/spec-template.md — generic, compatible
    ✅ .specify/templates/tasks-template.md — generic, compatible

  Follow-up TODOs: None

  -- Prior amendment (2026-05-29, v2.0.1) --------------------------------
  Version change: 2.0.0 → 2.0.1 (PATCH — Principle II render-ownership clause corrected)

  Amendment (2026-05-29, v2.0.1):
    II. UI Delivery Architecture — corrected the render-ownership clause. The
        prior wording ("astralprims defines AND renders") was inaccurate: the
        published `astralprims` package is schema-only. Now: `astralprims`
        DEFINES primitives (+ their structured representation) only; the
        ORCHESTRATOR renders them; ROTE adapts that rendering per device.
        Propagated to Principle IV (lint clause), Principle VI (primitive vs
        renderer docs), Principle VIII (rendering wording), Technology Stack
        (UI Delivery), and Development Workflow (primitive vs renderer PRs).
        Classified PATCH: no principle added/removed; Principle II's name,
        SDUI mandate, and intent are unchanged — only the internal
        responsibility for rendering is corrected to match reality.
    Resolves the tracked Principle II deviation noted in
    specs/026-frontend-removal-astralprims/plan.md (Complexity Tracking).

  -- Prior amendment (2026-05-29, v2.0.0) --------------------------------
  Version change: 1.1.0 → 2.0.0 (MAJOR — Principle II redefined; React/Vite SPA mandate removed)

  Principles modified:
    II. Frontend Framework → II. UI Delivery Architecture
        (was: "frontend MUST be Vite + React + TypeScript SPA")
        (now: backend delivers server-driven UI via FastAPI; ROTE adapts to the
         connecting device/client; `astralprims` defines AND renders all primitives)
    IV.  Code Quality — TypeScript/ESLint clause generalized to "any client-side
         TypeScript/JavaScript emitted or maintained" (no standalone SPA assumed)
    VI.  Documentation — generalized TS-export clause; added astralprims primitive
         documentation requirement
    VIII. User Experience — primitives now sourced from the `astralprims` package
         rather than a predefined frontend component set

  Principles added:   None
  Principles removed: None (II redefined, not removed)

  Sections updated:
    - Technology Stack: replaced "Frontend: Vite + React + TypeScript" with the
      SDUI delivery model (FastAPI + ROTE + astralprims)
    - Governance footer: Last Amended date bumped to 2026-05-29; version → 2.0.0

  Templates requiring updates:
    ✅ .specify/templates/plan-template.md — generic Constitution Check gate + generic
       "frontend/" example dir, compatible (no React/Vite mandate referenced)
    ✅ .specify/templates/spec-template.md — generic, compatible
    ✅ .specify/templates/tasks-template.md — generic "frontend/src/" example, compatible
    ✅ No command templates require changes
    ✅ No runtime guidance docs require changes (CLAUDE.md is auto-generated)

  Follow-up TODOs: None
-->

# AstralDeep Constitution

## Core Principles

### I. Primary Language

All backend code MUST be written in Python.

- No other backend languages are permitted without a
  constitution amendment.
- Python version MUST be kept current with the project's
  declared minimum (see `pyproject.toml` or equivalent).

### II. UI Delivery Architecture

The user interface MUST be server-driven. The backend composes
and delivers UI to clients; there is no standalone single-page
application framework acting as the source of truth for UI.

- The backend MUST deliver server-driven UI (SDUI) to clients
  via FastAPI.
- The ROTE layer MUST adapt delivered UI to the connecting
  device/client target. Each target receives the format it
  expects (e.g., web → HTML/CSS/JS; future desktop, native
  phone, or native watch clients receive their own appropriate
  format).
- All UI primitives MUST be **defined** by the `astralprims`
  package. It is the single source of truth for primitive
  definitions and their serializable structured representation.
  `astralprims` does NOT render; it only defines.
- The **orchestrator** MUST render those primitives into the
  client-appropriate format. The ROTE layer MUST adapt that
  rendering to the connecting device/client target. Rendering
  and per-device adaptation are orchestrator responsibilities,
  not `astralprims` responsibilities.
- Adding a new client target MUST be achievable by adding a
  renderer within the orchestrator's render layer — without
  changing `astralprims` primitive definitions or agent
  response-building code.
- No standalone React/Vite (or other SPA) frontend may be
  (re)introduced as the primary UI source of truth without a
  constitution amendment. Client-side assets emitted by the
  orchestrator's render layer for a target (e.g., HTML/CSS/JS
  for the web) are permitted and expected.
- The application **chrome** (the top-bar controls and the
  settings-menu structure) MUST be described ONCE on the server
  as a single, role-aware, data-driven source of truth, and
  every client MUST render its chrome from that description.
  Clients MUST NOT hard-code a parallel menu/chrome definition.
- Clients hold only minimal, generic wrapping (a shell plus a
  reuse of the shared server-driven-UI renderer). They MUST NOT
  reimplement generative UI or settings surfaces natively in a
  way that can diverge from the server; settings surfaces are
  composed from `astralprims` primitives, rendered by the
  orchestrator, and adapted per device by ROTE, exactly like any
  other server-driven UI.

**Rationale**: Separating primitive **definition** (in the
`astralprims` package) from **rendering** (in the orchestrator,
adapted per device by ROTE) keeps the wire-level primitive
contract stable and reusable while letting the orchestrator
evolve presentation without coordinated client releases. New
device targets stay additive — a new orchestrator renderer over
the same primitives — rather than a parallel reimplementation.

### III. Testing Standards

Every new feature MUST include unit and integration tests with
a minimum of 90% code coverage on the code it changes.

- Tests MUST be written for all new code paths.
- Coverage MUST be measured and enforced in CI as
  **changed-code coverage**: the lines added or modified by a
  pull request MUST be ≥ 90% covered by the test suite. This is
  the mechanical merge gate (see Principle XI).
- Module-wide and repository-wide coverage improvements remain
  encouraged but are not the merge gate.
- No feature branch may merge without meeting the 90%
  threshold on changed code.

### IV. Code Quality

All code MUST adhere to established style standards.

- Python code MUST comply with PEP 8. Linting MUST be enforced
  via tooling (e.g., ruff, flake8).
- Any client-side TypeScript/JavaScript emitted or maintained
  (including the orchestrator render layer's output assets) MUST
  pass standard lint rules. Linting MUST be enforced in CI.
- When maintained TypeScript/JavaScript exists, the repository MUST
  carry its standard lint configuration in version control and the
  CI lint gate MUST exercise it. A linter declared only for CI remains
  isolated from product runtime dependencies under Principles V and XI.
- No linting exceptions without inline justification comments.

### V. Dependency Management

No new third-party library may be added without explicit
approval from a lead developer.

- Proposed dependencies MUST be documented in the PR
  description with rationale.
- Lead developer approval MUST be recorded in the PR review.
- Transitive dependency impact MUST be considered.
- First-party packages owned by the project (e.g.,
  `astralprims`) are not third-party dependencies, but their
  introduction MUST still be documented in the PR.
- A **test-, eval-, or benchmark-only dependency** (for
  example, a property-based-testing generator, or an external
  attack-benchmark corpus adapted by a measurement harness) MAY
  be introduced WITHOUT counting as a product-runtime
  dependency, PROVIDED it is: (a) declared in a separate
  manifest (e.g., `requirements-eval.txt`) that is never
  installed into the product image's runtime layer; (b) never
  imported by any product-runtime module; (c) enforced by an
  automated isolation guard that fails CI if a product module
  imports it; and (d) documented in the PR. Promoting such a
  dependency into the product runtime still requires
  lead-developer approval under this principle.

### VI. Documentation

All public APIs and complex functions MUST be documented.

- Python functions MUST have docstrings following Google or
  NumPy style.
- Any client-side TypeScript/JavaScript exports MUST have JSDoc
  comments.
- Every `astralprims` primitive MUST be documented — its data
  shape and serialization — before use. Each orchestrator
  renderer MUST document the client targets it supports and its
  rendering behavior.
- Backend APIs MUST expose interactive documentation at the
  `/docs` URL (e.g., via FastAPI's built-in Swagger UI).

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
  fabric's `wsapi` WebSocket seam, using existing dependencies (no
  new third-party library; `pycrescolib` MUST NOT be adopted at
  runtime), behind a fail-closed feature flag (`FF_CRESCO`, default
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
  `astralprims` package (the orchestrator renders them).
- The backend MAY dynamically generate layouts by composing
  `astralprims` primitives, rendered by the orchestrator as SDUI
  and adapted to the client target by ROTE.
- New primitives MUST be added to `astralprims`, approved, and
  documented before use.

### IX. Database Migrations

Any change to the database schema MUST ship with a migration
script that runs automatically; ad-hoc SQL against deployed
environments is prohibited.

- Schema changes (adding/removing tables, columns, indexes,
  constraints, enums, or types) MUST include a migration
  script committed in the same pull request as the change.
- Migrations MUST execute automatically — either on
  application startup or as a dedicated step in the deployment
  pipeline. Manual DBA intervention MUST NOT be required for
  routine schema evolution.
- The project's migration mechanism — idempotent, guarded
  schema migrations executed automatically at application
  startup (`shared/database.py::_init_db`) — MUST be the single
  source of truth for schema state. Direct ALTER/CREATE
  statements applied outside this mechanism are prohibited.
- Migrations MUST be idempotent or guarded against
  re-execution so that repeated deploys are safe.
- Migrations MUST provide a documented rollback path (down
  migration or recovery procedure) unless the change is
  intentionally destructive AND approved by a lead developer
  in the PR review.
- Migrations MUST be tested against a representative dataset
  before merge; a passing migration on an empty database is
  not sufficient evidence.

**Rationale**: Schema drift between code and database is the
most common cause of production outages after a deploy.
Treating migrations as code — versioned, reviewed, and
auto-applied — keeps every environment reproducible and makes
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
  satisfying the 90% coverage gate from Principle III.
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
     orchestrator.
  3. **Coverage** — the changed-code coverage gate at ≥ 90%
     (Principle III).
  4. **Image build** — the production container image MUST
     build from a clean checkout on every run.
  5. **Boot smoke** — the built image MUST answer its liveness
     and readiness probes in development posture, and a
     production-posture boot with placeholder or missing
     secrets MUST exit with the documented configuration-error
     code, proving the fail-closed gate end-to-end.
  6. **Secret scan** — committed credential material MUST fail
     the pipeline (Principle VII).
- On main-branch pushes that pass all gates, the pipeline MUST
  publish the container image to the project's container
  registry with an immutable commit-derived tag and a moving
  latest tag. The published image is the production deployment
  artifact.
- Verification failures and publish failures MUST be
  distinguishable; a publish failure MUST NOT mask green
  verification gates.
- CI-only tooling (linters, coverage tools, scanners) installed
  in the pipeline environment is not a product dependency under
  Principle V, but adding one MUST still be documented in the
  PR that introduces it. This carve-out also covers the
  isolated dependencies of an eval/benchmark harness (Principle
  V), PROVIDED the automated isolation guard proves no
  product-runtime module imports them.
- No branch may merge to main with a failing required gate.

**Rationale**: Principles III, IV, VII, and X are only as real
as their enforcement. A named, versioned gate set turns
"production ready" from a review opinion into a reproducible
machine verdict, and the registry-published image makes every
green main commit deployable by pull.

### XII. Cross-Client Consistency

Every client target MUST present the same user-facing
capabilities, application chrome, and visual design language.
Consistency across every device that connects to the
orchestrator+ROTE backend is a requirement, not an aspiration.

- All current and future clients — the web experience, the
  desktop client, native mobile clients, and any later target
  (e.g. an iOS client) — MUST expose the same menu structure,
  the same settings surfaces, and the same functionality, so a
  user moving between clients finds the same options in the same
  places.
- The menu/chrome structure and the settings surfaces MUST be
  sourced from the shared, server-owned definitions (Principle
  II). No client may add, omit, rename, reorder, or otherwise
  diverge from those shared definitions. Per-client cosmetic
  adaptation to the device form factor is expected; changing
  which capabilities exist or where they live is not.
- Role-gating (e.g. admin-only surfaces) MUST be derived from
  the same server-owned, verified-role source on every client,
  and MUST be enforced server-side regardless of what a client
  displays.
- Adding a new client target MUST be achievable by making it a
  thin consumer of the shared chrome definition and the shared
  server-driven-UI renderer — NOT by re-specifying the menu or
  re-implementing settings surfaces for that client.
- A change to a user-facing capability MUST be reflected on
  every client without a per-client code change wherever the
  shared server-owned definition makes that possible; where a
  client genuinely cannot yet render something, it MUST degrade
  gracefully (labeled placeholder), never silently omit or
  diverge.
- **Colors and theming**: every client MUST render the shared,
  server-owned design language. One palette/theme definition —
  including the user's selected theme preference — drives every
  client; equivalent components MUST use the same palette roles
  (surfaces, text, accents, state/severity colors) on every
  client, within platform rendering idiom. No client may
  hard-code or locally fork a divergent palette; a theme or
  palette change propagates to every client from the shared
  definition, never via per-client re-theming.
- **Layout on similarly sized devices**: devices of a comparable
  form-factor class (e.g., two phone-class devices, or desktop
  web beside the desktop client) MUST receive equivalent layout
  and arrangement for the same content. Per-device adaptation is
  keyed on the declared ROTE device profile and its capabilities
  (`backend/rote/capabilities.py`), never on which client
  codebase is connecting; any layout divergence between two
  similarly sized devices MUST trace to a declared capability
  difference, not an ad-hoc client choice.
- **Enforcement**: the committed UI-protocol manifest
  (`backend/shared/ui_protocol.json`) and the parity/drift-guard
  suites in every client test stack are the mechanical
  enforcement of this principle. A change to shared UI behavior
  — protocol frames, component types, chrome, or theming — MUST
  land on every in-scope client within the same feature (or
  record an explicit, justified divergence in the owning spec,
  per the carve-out below) and MUST keep the manifest and every
  client's drift guards green.
- A capability MAY be deliberately scoped **web-only** — offered
  on the web but intentionally absent from other clients — when it
  is inherently tied to something another client genuinely lacks
  (e.g. admin-only desktop tooling, or a walkthrough anchored to
  web-DOM elements). This is NOT the accidental drift this
  principle forbids, PROVIDED it is (a) documented in the owning
  feature spec, (b) enforced from the shared server-owned
  definition — the item is omitted from the other clients' menu
  channels by the server, not hidden client-side — and (c) applied
  uniformly to every non-web client. The admin surfaces (Tool
  quality, Tutorial admin) and "Take the tour" are the current
  web-only capabilities.

**Rationale**: Divergent clients erode trust, multiply
maintenance, and produce exactly the drift this project has
already seen (duplicated and missing menu items across the web,
desktop, and mobile clients). Anchoring every client to one
server-owned definition and one shared renderer makes
"the clients match" a structural guarantee rather than a
recurring manual reconciliation, and keeps new client targets
cheap and faithful. Visual drift is the same failure in
different clothes: a client with its own palette, or its own
layout for the same form factor, reads as a different product.
Anchoring theme and per-form-factor layout to the same
server-owned definitions — and gating them behind the committed
protocol manifest and per-client drift guards — keeps "it looks
and works the same everywhere" a machine-checked guarantee.

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

## Technology Stack

- **Backend**: Python (FastAPI or equivalent ASGI framework)
- **UI Delivery**: Server-driven UI (SDUI) delivered via
  FastAPI; UI primitives **defined** by the `astralprims`
  package; **rendered** by the orchestrator and **adapted**
  per device by the ROTE layer (web target → HTML/CSS/JS;
  future native targets additive via new orchestrator renderers)
- **Authentication**: Keycloak IAM
- **Agent Auth**: RFC 8693 token exchange with attenuated scopes
- **Database Migrations**: Idempotent, guarded startup
  migrations (`shared/database.py::_init_db`) executed
  automatically at application boot
- **Continuous Integration**: GitHub Actions running the
  Principle XI gate set; deterministic release evidence normally prepared
  locally before push, with a bounded draft-only bootstrap for structurally
  provider-bound inputs, then independently validated by protected CI;
  container images published to GitHub Container Registry; protected
  publication uses native short-lived GitHub Actions identity rather than
  repository-scoped Apps
- **Eval/Benchmark Harnesses**: eval-only dependency manifests
  (e.g., `requirements-eval.txt`) kept out of the product
  runtime and enforced by an automated isolation guard
  (Principle V); harness principals namespaced and torn down
- **Cresco Integration**: the CrescoEdge edge-computing fabric is
  approved external infrastructure, reached only via a first-party
  Python bridge agent over its `wsapi` WebSocket seam (existing
  `websockets` dependency; no new third-party library), behind
  `FF_CRESCO` (default off); the JVM fabric is never embedded in the
  product image (Principles I, VII)
- **Defense-track Documentation**: documentation/research-only
  artifacts are governed by Principle XIII; the `docs/` tree is
  gitignored, so any such artifacts are kept outside the public
  repository (not version-controlled here)
- **Containerization**: Docker / Docker Compose
- **License**: Apache 2.0

## Development Workflow

- All changes MUST go through pull requests.
- PRs MUST pass the Principle XI CI gate set (maintained-language lint, tests,
  changed-code coverage, image build, boot smoke, secret scan)
  before merge.
- PRs introducing new dependencies MUST include lead developer
  approval.
- PRs that modify the database schema MUST include the
  corresponding migration script and evidence that it ran
  successfully against a representative dataset.
- PRs that add or change UI primitives MUST do so within
  `astralprims` (definitions + serialization, documented). PRs
  that add or change how primitives are presented MUST do so in
  the orchestrator's render layer, with a renderer for each
  supported client target and per-device adaptation via ROTE.
- PRs that change the application chrome (top-bar controls or
  the settings menu), the shared visual design language
  (palette/theme tokens), or that add, remove, or relocate a
  user-facing capability MUST update the single server-owned
  definition rather than any per-client copy, MUST keep the
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
- Each PR MUST reference relevant spec/task IDs when
  applicable.

## Governance

This constitution is the highest-authority document governing
AstralDeep development practices. It supersedes all other
guidance when conflicts arise.

- **Amendments**: Any change to this constitution MUST be
  proposed via PR, reviewed by at least one lead developer,
  and documented with rationale.
- **Versioning**: This document follows semantic versioning.
  MAJOR for principle removals/redefinitions, MINOR for new
  principles or material expansions, PATCH for clarifications
  and wording fixes.
- **Compliance**: All PRs and code reviews MUST verify
  adherence to these principles. Violations MUST be resolved
  before merge.

**Version**: 2.9.0 | **Ratified**: 2026-03-11 | **Last Amended**: 2026-08-02
