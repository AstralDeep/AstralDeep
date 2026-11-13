# AstralDeep Agent Guide

This is the single repository instruction file for all coding agents, including Codex and Claude. Update `AGENTS.md` directly; do not create a separate agent-specific instruction file. Keep it focused on stable rules and routing; feature history belongs in `specs/`, Git, and the optional knowledge vault.

## Authority and freshness

- For feature intent, use the feature's `spec.md`, `plan.md`, `tasks.md`, `contracts/`, and clarification records under `specs/`. A spec describes intent; verify implementation claims against the live tree and tests.
- The live branch, working tree, and current code are the source of truth for present state. Older generated feature digests remain in Git history; their technology notes may be superseded and are not current instructions.
- Before feature work, check `git status --short --branch`, recent commits, and matching `specs/<number>-*/` directories. Do not assume the highest-numbered spec on `main` is the work currently in progress.
- Work may continue on another machine. Do not edit an active feature branch/spec, overwrite handoff artifacts, or recreate work merely because it is absent from the current checkout. Inspect remotes and ask when ownership is genuinely ambiguous.
- Prefer paths supplied by the current workspace and repository-relative paths. Absolute paths recorded in old docs or the knowledge vault are historical, not commands.

## Mental model

AstralDeep is a clinical-AI, multi-agent platform whose FastAPI orchestrator is a node in an owner's personal mesh. Peers embed the shared Rust AstralKernel for signed event logs, sync, authorization, turns and adaptation; AstralPlane supplies SQLite for personal nodes and PostgreSQL for institution nodes. The orchestrator serves REST, WebSocket/A2A transport, the web shell and static assets on port 8001, and bundled first-party agents retain their runtime authorization and audit gates regardless of transport placement.

The UI contract is:

`AstralPrimitives defines -> the base decides, authorizes and adapts -> AstralProjection renders`

The base is AstralKernel, AstralDeep, AstralPlane, LETS and ROTE, distributed as AstralVessel. ROTE belongs to the kernel's Python package; base code must not import Projection rendering code. Models propose artifacts and deterministic code validates, bounds and renders them.

The evidence control adapter depends on the orchestrator's private ephemeral archive and stays host-bound. It has no standalone transport process; disabling in-process registration preserves complete source responses and makes its inspection commands unavailable.

- There is no React/Vite SPA. Do not reintroduce a parallel frontend source of truth.
- Web UI is backend-served HTML plus vanilla JS/CSS owned under `components/AstralProjection/backend/webrender/` and exposed to Deep through the installed Projection resource accessors.
- Apple clients (Swift/SwiftUI) embed the kernel and render structured UI from their local signed log. Android (Kotlin/Compose) and Windows (PySide6) remain thin node clients until their kernel ports qualify; a protocol change must preserve those clients during the transition.
- Voice runs on-device through the same turn and consent gates as typed input; no hosted-voice or system-speech fallback may silently substitute for an unavailable local model.
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
- `components/AstralKernel/python/rote/`: ROTE (`astral-rote`, import `rote`), the base's authoritative device capabilities and semantic adaptation. Deep and Projection both depend on it, and it imports neither.
- `backend/orchestrator/projection_surfaces/`: Deep-owned, host-authorized chrome surface adapters; reusable rendering remains in Projection.
- `backend/agents/`: bundled first-party agents. Generated draft directories can appear here during development; treat unexpected untracked directories as user/generated data, never as files to stage blindly.
- `backend/tests/` plus module-local `*/tests/`: backend verification. Root pytest discovery does not include every module suite.
- `components/AstralProjection/windows-client/`, `components/AstralProjection/android-client/`, `components/AstralProjection/apple-clients/`: authoritative native clients and their parity/drift guards.
- `specs/`: numbered feature specifications, plans, tasks, and contracts; the spec number is more reliable than historical branch naming.
- `docs/`: operator documentation. Most of this tree is ignored; check `.gitignore` before assuming a new doc will be committed.

For deployment and agent workflows, read [production deployment](docs/production-deployment.md), [BYO client agents](docs/byo-client-agents.md), [remote compute agents](docs/remote-compute-agents.md), and [authoring agents and skills](docs/your-own-agents-and-skills.md).

The pinned `components/AstralPrimitives` repository owns primitive definitions and Python serialization. It is a separate release train; do not edit or publish it as an incidental AstralDeep change.

## Non-negotiable engineering rules

- Node backend code is Python and must remain compatible with the production Python 3.11 image; the shared kernel is Rust. Ruff's target is `py311`, even if a host virtualenv is newer.
- Any dependency may be added; declare it in the owning repository's manifest and name it in the PR description.
- Keep changes production-ready: no silent stubs, debug-only paths, fake success, untracked TODOs, or happy-path-only handling.
- The code documents itself. Each source file opens with a header of at most three sentences: what it does and how it connects to other files. Add no other comments or docstrings except a one-line *why* where absolutely necessary. Never put spec/task IDs, feature numbers, history, TODOs, or narration in source. Tool directives (`# noqa`, `# type: ignore`, `# pragma: no cover`, eslint/ktlint/swift-format directives, shebangs) are not comments and stay.
- Write or update tests for golden paths, edge cases, denials, and failures. Changed Python lines must retain at least 90% coverage.
- CI jobs and test suites finish within 30 minutes on hosted runners; over-budget suites get cheaper fixtures or lose their slowest tests (never sharded, raised, or waived); no soak tests; required gates stay deterministic — no live third-party network services, no exact bounds on clock-derived values, no wall-clock performance bounds on shared hosted runners, per-test retries only.
- Preserve the user's working tree. Never discard unrelated changes. Local commits for work within the task's scope are allowed without additional user authorization. Review the diff and stage only task-related files; keep unrelated changes and sensitive/generated user data out of commits.
- Do not push, merge, release, submit to a store, or mutate external issue trackers unless the request or invoked workflow explicitly calls for it. Permission to create local commits does not authorize those actions.
- Use targeted parallel research/review when it improves coverage, then personally verify critical seams and integrate the findings. Agent reports are evidence leads, not proof by themselves.
- Distinguish clearly among specified, implemented, test-passing, live-verified, merged, deployed, and released. Do not collapse them into "done."

## UI and astralprims contract

- Evaluate UX, developer experience, and agent experience together. Prefer clear layout and direct actions over taglines, helper prose, and secondary labels; preserve essential state, errors, permission and approval details, accessible names, and stable automation identifiers.
- Construct primitive payloads with `.to_dict()` or `create_ui_response()`. Primitive `.to_json()` returns a JSON string and is not the component-dict contract. Any validator/help text recommending `.to_json()` is stale behavior, not authority.
- Use `css`, not `style`. Escape and sanitize web output through the existing renderer helpers; never pass model/user text as raw HTML.
- `Primitive.attributes` flattens last and can overwrite declared fields, including `type`; use it only with trusted, deliberate keys.
- The installed `astralprims` wheel may lag the sibling repository's `main`. Before importing a newer class, bump the AstralDeep version floor, rebuild the environment/image, verify the import, and run parity tests. Until then, an already-approved manifest type may be emitted as a plain dict.
- A new primitive requires owner approval and a coordinated change: definition/exports/serialization tests/docs/version in AstralPrimitives; renderer and ROTE behavior; `ui_protocol.json`; all affected client dispositions/renderers; drift guards; and live client verification.
- A protocol, frame, chrome, theme, component, or layout change must land across every in-scope client in the same feature. A web-only exception must be explicit in the spec and enforced by the server-owned definition, never hidden independently by clients.

## Security, identity, and data

- Preserve Keycloak as the only source of user identity, proven at mesh enrollment. Mesh delegation uses offline-verifiable, attenuated tickets with exact action, actor-chain, roster, stop, expiry, receipt and consent bindings; RFC 8693 remains at the node edge. Preserve owner isolation, tool permission/policy gates, PHI gates, egress validation, confirmation gates and signed audit provenance, and never bypass the normal dispatch path.
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

Important: `backend/pytest.ini` discovers `tests` and `persistent_agents/tests`. The command above is not every nested module suite. For merge-level confidence, mirror the explicit invocations in `.github/workflows/ci.yml`, including the module suites and relevant feature-flag posture, and run any touched package's local tests explicitly.

CI runs the backend suites as four groups — `tests_1` and `tests_2` (the two file shards of `backend/tests`, each on four pytest-xdist workers with their own databases), `persistent_agents`, and `modules` — each test job capped at 20 minutes. `scripts/run_backend_web_tests.py --group all` (the default) still runs every suite in one pass for local use. Protected qualification runs `scripts/produce_backend_web_qualification.py --phase gate --group <group>` as one 30-minute job per group, then `--phase services` and `--phase assemble`; `--phase all` keeps the one-pass local run.

Client gates:

- Windows: set `QT_QPA_PLATFORM=offscreen`, then run `python -m pytest components/AstralProjection/windows-client/tests -q` with the client requirements installed.
- Android (from `components/AstralProjection/android-client/`): run the committed wrapper for `ktlintCheck`, `:app:lintDebug`, `:core:test`, `:app:testDebugUnitTest`, `:core:koverVerify`, and `:app:assembleDebug`.
- Apple (macOS only): run `swift test --package-path components/AstralProjection/apple-clients/AstralCore` and the affected unsigned `xcodebuild` schemes from `components/AstralProjection/apple-clients/README.md`.
- AstralPrimitives changes (only when explicitly in scope): run its pytest suite before any version bump/publish. Publication checks do not replace PR qualification.

When driving a running iOS Simulator app, prefer available simulator MCP tools, inspect screenshots or accessibility trees, and use semantic labels for interaction. Keep `xcodebuild` for builds/tests; drive the macOS app directly because it is not a simulator. Apple sign-in uses real PKCE and must be completed by the user, without a development-token bypass.

When replacing an Apple localtest app that holds existing mesh keys, retain its bundle identifier and stable developer signing identity, use Xcode-generated entitlements, and take a stopped data backup first. Use Projection's localtest build script and verify the installed app's actual path and signature. Ad-hoc re-signing or post-build rebranding can invalidate Keychain access across rebuilds; the user must handle any password or presence prompt themselves.

Tests are necessary, not sufficient. Exercise changed UI behavior against the live backend on every affected client/form factor. Verify authorization and security changes through the real dispatch path, including denial/failure cases. If an unrelated baseline failure exists, demonstrate and document the baseline rather than hiding it or weakening a gate.

Release-evidence collection, normalization, and parsing normally run locally before push and
remain diagnostic. If a canonical input structurally requires a provider identity or native
artifact that cannot exist until the exact SHA is remote, use only a provider-native
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

The active knowledge vault is the sibling repository at `../kos-wiki-v2`; `../kos-wiki` is historical. Before reading or saving knowledge, follow the active vault's `AGENTS.md` for schema, provenance, tier routing, sensitivity, intake and Git operations. Use `index.md` to route, read the relevant curated pages and tail of `log.md`, and check `to-file/` and any active `sessions/resume-*.md`; consult `local/index.md` and `local/profile.md` for personal-tier work. Vault claims must be verified against the live repositories.

The user has established an always-on checkpoint rule: whenever a major checkpoint occurs — a
branch is pushed for handoff, a spec/plan/tasks phase finishes, a PR opens or merges, a feature or
release changes state, or a durable decision is made — update the vault before declaring the
checkpoint complete. First read that vault's `AGENTS.md`, refresh the repository anchor, revise the
affected curated pages (never `raw/`), update `index.md` and `log.md`, then commit and push the vault
when its remote is available. The machine-written `sessions/` breadcrumb is not a substitute for the
curated update. Keep the vault commit separate from product-repository commits, and report explicitly
if the vault commit or push is blocked.

## Handoff standard

Finish with an evidence-backed summary: files changed, behavior changed, exact tests/checks and results, live verification performed or still required, feature flags/migrations/contracts affected, and any residual risk. Keep work-in-progress recoverable across machines through a named pushed branch only when the user has requested pushing; otherwise leave a precise local handoff without claiming remote persistence.
