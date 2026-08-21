# AstralDeep Codex Guide

This is the durable repository guide for Codex. Keep it focused on stable rules and routing; feature history belongs in `specs/`, Git, and the optional knowledge vault.

## Authority and freshness

- `.specify/memory/constitution.md` is the highest-authority engineering policy. Read it before planning architecture, security, schema, dependency, UI, protocol, or client changes.
- For feature intent, use the active feature's `spec.md`, `plan.md`, `tasks.md`, `contracts/`, and clarification records. A spec describes intent; verify implementation claims against the live tree and tests.
- The live branch, working tree, and current code are the source of truth for present state. `CLAUDE.md` is a useful generated history digest, but it can lag or contain superseded technology notes.
- Before feature work, check `git status --short --branch`, recent commits, `.specify/feature.json`, and matching `specs/<number>-*/` directories. Do not assume the highest-numbered spec or `feature.json` on `main` is the work currently in progress.
- Work may continue on another machine. Do not edit an active feature branch/spec, overwrite handoff artifacts, or recreate work merely because it is absent from the current checkout. Inspect remotes and ask when ownership is genuinely ambiguous.
- Prefer paths supplied by the current workspace and repository-relative paths. Absolute paths recorded in old docs or the knowledge vault are historical, not commands.

## Mental model

AstralDeep is a clinical-AI, multi-agent platform with one FastAPI orchestrator and PostgreSQL backend. It serves REST, WebSocket/A2A transport, the web shell, and static assets on port 8001. Bundled first-party agents run in-process by default; the kill switch restores their transport path, while external, draft, and user-authored agents retain explicit trust boundaries.

The UI contract is:

`AstralPrimitives defines -> AstralProjection renders and adapts -> AstralDeep orchestrates`

- There is no React/Vite SPA. Do not reintroduce a parallel frontend source of truth.
- Web UI is backend-served HTML plus vanilla JS/CSS owned under `components/AstralProjection/backend/webrender/` and exposed to Deep through the installed Projection resource accessors.
- Windows (PySide6), Android (Kotlin/Compose), and Apple (Swift/SwiftUI) are thin native consumers of server-owned structured UI and chrome contracts.
- "LLM proposes, code decides" is the default design rule: models may draft layouts or artifacts, but deterministic code validates, authorizes, bounds, and renders them.

## Repository routing

- `backend/orchestrator/orchestrator.py`: central dispatch/runtime hub. Keep edits narrow and cover surrounding authorization and delivery seams.
- `backend/orchestrator/`: auth, permissions, delegation, agent lifecycle, workspace, streaming, scheduling, UI design, and chrome event logic.
- `backend/shared/protocol.py`: shared transport structures.
- `components/AstralProjection/contracts/ui_protocol.json`: authoritative cross-client frame/component vocabulary, consumed from the pinned Projection component.
- `backend/orchestrator/plane_composition.py`: fail-closed binding to the one
  application-scoped AstralPlane runtime and its pinned contract metadata.
- `components/AstralPlane/`: pinned data-plane component. Its public facade,
  guarded migration registry, repositories, PostgreSQL pool, recovery logic,
  and configured blob stores own all durable mechanics; Deep code must not
  borrow connections or reintroduce `backend/shared/database.py`.
- `backend/shared/external_http.py`: approved outbound HTTP/egress path.
- `components/AstralProjection/backend/webrender/`: authoritative primitive rendering, sanitization, accessibility, web shell, reusable chrome, and static resources.
- `components/AstralProjection/backend/rote/`: authoritative device capabilities and semantic adaptation.
- `backend/orchestrator/projection_surfaces/`: Deep-owned, host-authorized chrome surface adapters; reusable rendering remains in Projection.
- `backend/agents/`: bundled first-party agents. Generated draft directories can appear here during development; treat unexpected untracked directories as user/generated data, never as files to stage blindly.
- `backend/tests/` plus module-local `*/tests/`: backend verification. Root pytest discovery does not include every module suite.
- `components/AstralProjection/windows-client/`, `components/AstralProjection/android-client/`, `components/AstralProjection/apple-clients/`: authoritative native clients and their parity/drift guards.
- `specs/`: numbered Spec Kit feature artifacts; the spec number is more reliable than historical branch naming.
- `docs/`: operator documentation. Most of this tree is ignored; check `.gitignore` before assuming a new doc will be committed.

The pinned `components/AstralPrimitives` repository owns primitive definitions and Python serialization. It is a separate release train; do not edit or publish it as an incidental AstralDeep change.

## Non-negotiable engineering rules

- Backend code is Python and must remain compatible with the production Python 3.11 image. Ruff's target is `py311`, even if a host virtualenv is newer.
- Do not add a third-party runtime dependency without explicit lead-developer approval. Reuse existing libraries and stdlib seams first.
- Keep changes production-ready: no silent stubs, debug-only paths, fake success, untracked TODOs, or happy-path-only handling.
- Write or update tests for golden paths, edge cases, denials, and failures. Changed Python lines must retain at least 90% coverage.
- Preserve the user's working tree. Never discard unrelated changes. Do not create commits, push, merge, release, submit to a store, or mutate external issue trackers unless the request or invoked workflow explicitly calls for it.
- Use targeted parallel research/review when it improves coverage, then personally verify critical seams and integrate the findings. Agent reports are evidence leads, not proof by themselves.
- Distinguish clearly among specified, implemented, test-passing, live-verified, merged, deployed, and released. Do not collapse them into "done."

## UI and astralprims contract

- Construct primitive payloads with `.to_dict()` or `create_ui_response()`. Primitive `.to_json()` returns a JSON string and is not the component-dict contract. Any validator/help text recommending `.to_json()` is stale behavior, not authority.
- Use `css`, not `style`. Escape and sanitize web output through the existing renderer helpers; never pass model/user text as raw HTML.
- `Primitive.attributes` flattens last and can overwrite declared fields, including `type`; use it only with trusted, deliberate keys.
- The installed `astralprims` wheel may lag the sibling repository's `main`. Before importing a newer class, bump the AstralDeep version floor, rebuild the environment/image, verify the import, and run parity tests. Until then, an already-approved manifest type may be emitted as a plain dict.
- A new primitive requires constitution approval and a coordinated change: definition/exports/serialization tests/docs/version in AstralPrimitives; renderer and ROTE behavior; `ui_protocol.json`; all affected client dispositions/renderers; drift guards; and live client verification.
- A protocol, frame, chrome, theme, component, or layout change must land across every in-scope client in the same feature. A web-only exception must be explicit in the spec and enforced by the server-owned definition, never hidden independently by clients.

## Security, identity, and data

- Preserve Keycloak-only user IAM, RFC 8693 attenuated delegation, owner isolation, tool permission/policy gates, PHI gates, egress validation, confirmation gates, and hash-chained audit provenance. Do not create a convenience bypass around the normal dispatch path.
- Treat in-process first-party agents as trusted only for transport placement; runtime authorization and auditing still apply. Treat external and user-authored agents as untrusted at the server boundary.
- Backend access to user-controlled URLs must use `backend/shared/external_http.py` and its allow/deny/size/TLS controls.
- `.env` may be inspected when relevant to diagnosis, setup, or live verification; the user has explicitly authorized this. Never echo secret values or commit the file. Treat credentials, tokens, encryption keys, agent keys, user uploads, generated user code, `backend/data`, `backend/tmp`, and synthesized `backend/knowledge` as sensitive: access them only when the task requires it, redact sensitive output, and never stage them.
- `ASTRAL_ENV` unset means production. Production configuration must fail closed; missing/placeholder secrets and mock auth must retain the documented exit-78 posture.
- LLM credentials are configured in-product and encrypted. Do not reintroduce environment-backed provider keys, type user keys, or perform the user's Keycloak/provider sign-in.

## Database changes

- PostgreSQL schema evolution lives only in AstralPlane's guarded, idempotent
  migration registry. Deep pins the exact Plane schema revision and migration
  digest in `config/astral-composition.json`; it owns product policy and
  orchestration, not SQL, driver pools, baseline provisioning, or blob
  mechanics.
- Schema changes require the matching AstralPlane `SCHEMA_REVISION` bump,
  repeat-safe DDL/data migration, rollback or recovery procedure, current-schema
  verification, and tests against representative existing data. Update Deep's
  composition pin only after the exact Plane revision is qualified. Do not use
  ad-hoc deployed SQL or introduce a second migration framework.

## Spec Kit with Codex

Codex discovers the repository-local skills in `.agents/skills/`. Invoke them with `$skill-name` (not Claude's slash-command spelling). Start a new Codex task/session after skill installation or updates so discovery refreshes.

The ten core skills are installed and hash-tracked by `.specify/integrations/codex.manifest.json`. The five `speckit-git-*` skills are deliberately project-owned Codex adapters for `.specify/extensions/git/commands/` and its Bash/PowerShell scripts; Spec Kit integration upgrade/uninstall does not manage them. Preserve their remote-collision and working-tree safety additions when syncing extension changes.

### Mandatory feature-ownership preflight

This project is developed across machines, and feature directories can exist only on a remote branch. The following preflight overrides the ordering in generated skill bodies: perform it before any Spec Kit hook, script, branch creation, or file mutation.

For `$speckit-specify`:

- Refresh `origin` and inventory numeric `specs/<NNN>-*` directories from the working tree and the trees of every `refs/remotes/origin/*` ref, not merely remote branch names. If the remote cannot be refreshed/inspected, stop and require the user to provide an explicit collision-checked `SPECIFY_FEATURE_DIRECTORY` and `GIT_BRANCH_NAME`.
- Choose a number unused in every inventoried tree. Verify the proposed directory and branch do not collide before running the mandatory feature-branch hook.
- Pass the resolved `SPECIFY_FEATURE_DIRECTORY` and `GIT_BRANCH_NAME` through the workflow so the hook and spec creator cannot independently choose conflicting numbers. Never run the allocation script twice.

For `$speckit-clarify`, `$speckit-plan`, `$speckit-tasks`, `$speckit-analyze`, `$speckit-implement`, `$speckit-converge`, `$speckit-checklist`, and `$speckit-taskstoissues`:

- Resolve one target feature before running the skill's own pre-execution checks. On `main`, with a missing/stale pointer, when branch and feature artifacts disagree, or when work is known to be active on another machine, do not trust `.specify/feature.json` by itself. Stop and obtain an explicit target/ownership decision.
- Require the target directory to exist in the checked-out tree. Set `SPECIFY_FEATURE_DIRECTORY` for each setup/prerequisite script invocation and verify the returned `FEATURE_DIR`/`FEATURE_SPEC`/`IMPL_PLAN` paths stay inside that exact directory. Abort on any mismatch before writes or external actions.
- Never use a local older copy of a spec to mutate work whose newer version exists only on another remote branch. Switch/pull the authorized branch first, or leave it alone.

Normal feature flow after that preflight:

1. `$speckit-specify <feature description>`
2. `$speckit-clarify`
3. `$speckit-plan`
4. `$speckit-tasks`
5. `$speckit-analyze`
6. `$speckit-implement`

After implementation, `$speckit-converge` is optional: use it only when the user requests artifact-to-code reconciliation and branch/feature ownership has been re-verified, because it appends tasks. Additional workflows are `$speckit-constitution`, `$speckit-checklist`, and `$speckit-taskstoissues`. Git-extension helpers are `$speckit-git-initialize`, `$speckit-git-feature`, `$speckit-git-validate`, `$speckit-git-remote`, and `$speckit-git-commit`.

- Read the selected skill completely and follow its gates. Clarify and Analyze are real quality gates, not ceremonial steps.
- `speckit-specify` has a mandatory branch-creation hook. Later hooks may offer commits. Report these mutations before executing them and preserve unrelated working-tree changes.
- `speckit-taskstoissues` mutates GitHub; run it only when the user explicitly requests issue creation/synchronization.
- Never point a workflow at a feature merely because it is the newest directory. Resolve the intended feature from the branch, `feature.json`, artifacts, remotes, and user context first.
- This Codex integration is pinned in project metadata at Spec Kit 0.12.16. The executable on `PATH` may be older on some machines; for integration management, use a version-matched one-shot CLI or deliberately upgrade the user tool. Do not run the older executable against managed integration state, blindly reinitialize Claude, or overwrite modified Claude-managed files.
- The core skills were rendered with PowerShell paths. Script-family selection happens before the skill body: use PowerShell scripts on Windows and substitute the same-purpose Bash script plus its native flags on POSIX. A literal PowerShell path in generated content is not a requirement to install PowerShell on macOS/Linux.

## Build and verification

Start with the narrowest relevant tests, then expand in proportion to risk. Backend source is baked into the image; a container restart alone does not copy ordinary source edits or reread a changed Compose environment.

Common lifecycle commands from the repository root:

```text
make up
make sync
make test-backend
ruff check .
```

Direct backend suite:

```text
docker exec astraldeep bash -c "cd /app/backend && python -m pytest -q"
```

Important: `backend/pytest.ini` has `testpaths = tests`. The command above is not every nested module suite. For merge-level confidence, mirror the explicit invocations in `.github/workflows/ci.yml`, including the module suites and relevant feature-flag posture, and run any touched package's local tests explicitly.

Client gates:

- Windows: set `QT_QPA_PLATFORM=offscreen`, then run `python -m pytest components/AstralProjection/windows-client/tests -q` with the client requirements installed.
- Android (from `components/AstralProjection/android-client/`): run the committed wrapper for `ktlintCheck`, `:app:lintDebug`, `:core:test`, `:app:testDebugUnitTest`, `:core:koverVerify`, and `:app:assembleDebug`.
- Apple (macOS only): run `swift test --package-path components/AstralProjection/apple-clients/AstralCore` and the affected unsigned `xcodebuild` schemes from `components/AstralProjection/apple-clients/README.md`.
- AstralPrimitives changes (only when explicitly in scope): run its pytest suite before any version bump/publish. Its publish workflow does not run tests for you.

Tests are necessary, not sufficient. Exercise changed UI behavior against the live backend on every affected client/form factor. Verify authorization and security changes through the real dispatch path, including denial/failure cases. If an unrelated baseline failure exists, demonstrate and document the baseline rather than hiding it or weakening a gate.

Release-evidence collection, normalization, and parsing normally run locally before push and
remain diagnostic. If a canonical input structurally requires a provider identity or native
artifact that cannot exist until the exact SHA is remote, use only Constitution X's provider-native
lead approval bound to the exact initial SHA, path scope, and RFC 3339 expiry within 168 hours. Keep
the PR draft/non-mergeable; run every locally executable gate; retain the external candidate-bound
fail-closed missing-input inventory; re-query provider approval, draft state, scope, expiry, and
changed paths before every push; bind the lead-attested passed command list and every parseable
locally required coverage-report digest; record every SHA outside the candidate tree; and keep candidate
jobs isolated, secret-free, and read-only. Load
`scripts/verify_release_evidence_bootstrap.py` and `.github/release-evidence-leads.json` from the
current default branch rather than candidate-controlled bytes, and retain their exact identities
with the result. Candidate refs must be blocked from every privileged manual-dispatch job; only an
exact default-branch-ref guard can preserve those dispatch surfaces during bootstrap. Bootstrap
never permits gate/product weakening, never waives staging, coverage,
trust, integrity, security, privacy, or feature-designated checks, and never authorizes merge or
release. Protected CI independently
validates canonical evidence, identities, digests, policy, and any bounded exception/debt transition
before authorizing release. Release publication and protected exception/debt mutation stay in
environment-approved GitHub Actions with the built-in short-lived job token and narrowly gated job
permissions. Do not introduce repository-scoped GitHub Apps, installation tokens, or a custom token
broker for release verification or publication.

## Knowledge vault and handoffs

When available, the local knowledge vault is the sibling repository at `../kos-wiki`. Use `index.md` to route, then read the relevant concept/entity pages, the tail of `log.md`, and any active `sessions/resume-*.md`. The vault is project memory, not current-state authority; verify its claims against the live repositories.

The user has established an always-on checkpoint rule: whenever a major checkpoint occurs — a
branch is pushed for handoff, a spec/plan/tasks phase finishes, a PR opens or merges, a feature or
release changes state, or a durable decision is made — update the vault before declaring the
checkpoint complete. First read that vault's `CLAUDE.md`, refresh the repository anchor, revise the
affected curated pages (never `raw/`), update `index.md` and `log.md`, then commit and push the vault
when its remote is available. The machine-written `sessions/` breadcrumb is not a substitute for the
curated update. Keep the vault commit separate from product-repository commits, and report explicitly
if the vault commit or push is blocked.

## Handoff standard

Finish with an evidence-backed summary: files changed, behavior changed, exact tests/checks and results, live verification performed or still required, feature flags/migrations/contracts affected, and any residual risk. Keep work-in-progress recoverable across machines through a named pushed branch only when the user has requested pushing; otherwise leave a precise local handoff without claiming remote persistence.

<!-- SPECKIT START -->
## Active Feature Plan

- `074-multirepo-lets-integration`: `specs/074-multirepo-lets-integration/plan.md`

## Active Technologies

- Python 3.11; vanilla JavaScript; Python/PySide6; Kotlin/JVM 17; Swift 5.9-compatible sources; JSON Schema 2020-12; Git/PowerShell + FastAPI/ASGI, PostgreSQL, Keycloak/RFC 8693, Docker/Compose, AstralPrimitives, AstralProjection, AstralPlane, and pinned LETS v1.0.10 public client/executor contracts (074-multirepo-lets-integration)
- Existing PostgreSQL and configured blob roots remain in place; LETS warden storage remains external; executor replay/authority and local manuscript/evidence state remain outside repositories (074-multirepo-lets-integration)

- Python 3.11, vanilla JavaScript, Python/PySide6, Kotlin/JVM 17, and Swift 5.9-compatible sources + pinned self-hosted LiveKit server/direct-RTC/client SDKs, bounded WebSocket worker control, ONNX Runtime + exact Silero VAD, FastAPI, PostgreSQL, Keycloak, astralprims/ROTE (065-conversational-voice)
- PostgreSQL additive `voice_session`/`voice_turn` metadata plus immutable execution-base commit anchors/digests and commit-scoped workspace-layout versioning; ephemeral LiveKit media and speech buffers with no audio retention (065-conversational-voice)

## Recent Changes

- 074-multirepo-lets-integration: Planned independent Projection/Plane/Primitives/LETS submodules, a history-preserving content replacement, and fail-closed LETS enforcement without changing production during migration.
- 065-conversational-voice: Planned an included exact-model conversational voice path across web, Windows, Android, macOS, iOS, and watchOS while preserving the normal authenticated agentic dispatcher.
<!-- SPECKIT END -->
