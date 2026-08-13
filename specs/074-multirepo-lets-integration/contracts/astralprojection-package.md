# Contract: AstralProjection Presentation and Client Package

**Contract version**: `astralprojection.contract/v1`
**Dependency direction**: `AstralDeep -> AstralProjection -> AstralPrimitives`

## Ownership

AstralProjection owns:

- deterministic primitive rendering, sanitization, accessibility, and unknown-component behavior;
- ROTE device capabilities and semantic adaptation;
- shared application chrome/menu presentation from supplied state;
- the authoritative UI-protocol manifest and cross-client fixtures;
- web shell/templates/static assets/vendor notices/checksums;
- complete Windows, Android, iOS, macOS, and watchOS client products;
- client/platform tests, packaging, signing configuration templates, and release workflows;
- Projection-only changed-coverage/lint/build tooling and client setup/release documentation.

AstralDeep owns:

- authenticated data queries, authorization, policy, persistence calls, and mutations behind surfaces;
- chrome event authorization/command dispatch;
- orchestration and frame/transport delivery;
- root product composition and same-origin FastAPI hosting;
- cross-component integration and release-readiness aggregation.

AstralPrimitives owns primitive definitions and serialization.

## Initial extraction map

The first low-risk replacement preserves recognizable source directory names before later package normalization:

```text
backend/webrender/**
backend/rote/**
contracts/ui_protocol.json
contracts/fixtures/**
windows-client/**
android-client/**
apple-clients/**
tooling/web-ci/**
scripts/<client-specific helpers>
tests/<pure renderer, ROTE, protocol, and client tests>
docs/<client setup and release sections>
.github/workflows/<client CI and release workflows>
```

The build package exposes `webrender` and `rote` import compatibility initially and adds an `astralprojection` facade for resources and metadata. A later internal normalization may move code under `src/astralprojection/` only after consumers use the public facade.

## Independence and host ports

Projection runtime code must not import these AstralDeep roots:

```text
orchestrator
audit
feedback
onboarding
llm_config
personalization
scheduler
dreaming
shared
```

An architecture test enforces the rule. Current `webrender/chrome/surfaces` modules that query/mutate Deep state are split into:

- Deep controller/query/command handlers: authorization, persistence, audit, mutation, feature policy;
- Projection view builders: accept immutable/plain view models and return HTML or SDUI component dictionaries.

Projection output remains protocol-neutral values. Deep wraps it in its own transport frame classes. Host interaction uses typed protocols/callbacks injected by Deep; Projection never locates or imports the Deep repository.

## Public facade

Conceptual public surface:

```python
CONTRACT_VERSION: str
UI_PROTOCOL_VERSION: str
UI_PROTOCOL_SHA256: str

def protocol_manifest_path() -> Traversable: ...
def static_root() -> Traversable: ...
def template_path(name: str) -> Traversable: ...

def render_component(component: Mapping[str, object], context: RenderContext) -> RenderedValue: ...
def render_workspace(workspace: WorkspaceView, context: RenderContext) -> RenderedValue: ...
def render_chrome(model: ChromeViewModel, context: RenderContext) -> RenderedValue: ...
def adapt(value: SemanticValue, capabilities: DeviceCapabilities) -> AdaptedValue: ...
```

Actual exports may retain existing narrower modules, but callers rely only on documented symbols. Assets are package resources; no caller assumes `backend/webrender` is adjacent to `orchestrator.py`.

## UI protocol

The authoritative manifest moves to `contracts/ui_protocol.json` and includes an explicit version. Its canonical SHA-256 is exported by the package and recorded in the Astral composition.

- Windows, Android, and Apple resolve the manifest inside their own repository.
- Apple drift tests fail if it is missing; they no longer skip.
- Shared voice/client conformance fixtures move beside the manifest.
- Deep retains an integration test comparing all frame/action dispatch sites to the submodule manifest.
- Historical Spec Kit references may remain documentation links but cannot be runtime filesystem dependencies.
- Every new primitive still originates in AstralPrimitives; Projection supplies all affected renderers/dispositions in the same compatible release.

## Web serving and assets

AstralDeep continues serving the web experience on the same FastAPI origin. Contract v1 does not create a Projection web service, avoiding new cookie, auth-session, WebSocket-origin, CSP, upload, and LiveKit boundaries.

The Projection wheel contains templates, CSS, JavaScript, fonts, images, vendor bundles, notices, and checksums. Deep mounts/reads them through resource accessors. Local sync and Docker build install the Projection package from the exact submodule commit with `--no-deps`; AstralPrimitives is installed separately from its own exact component revision.

## Client identity continuity

Moving repositories does not change product/store identities:

- Windows executable name, product metadata, `%APPDATA%/AstralDeep`, and settings organization/application keys remain stable.
- Android keeps `applicationId = com.personalailabs.astraldeep`, signing key/alias, redirect URI, and a monotonically increasing `versionCode`.
- Apple keeps bundle IDs, team, profiles, certificates, and App Store Connect records.
- GitHub Actions run numbers are not used directly after migration without a protected offset greater than the last submitted Apple build.

Secrets, variables, environments, approvals, store records, and protections are recreated explicitly in Projection; copying workflow YAML does not migrate them.

## Windows updater trust transition

Existing clients discover `AstralDeep/AstralDeep` releases and pin the exact Sigstore identity:

```text
https://github.com/AstralDeep/AstralDeep/.github/workflows/release-windows.yml@refs/tags/<tag>
```

Directly switching to Projection would strand them. The transition contract is:

1. Build one bridge version from the last trusted AstralDeep release workflow.
2. That bridge supports two exact channels: legacy Deep bounded to the bridge lineage and Projection bounded to its exact `release-windows.yml` tag identity.
3. Publish the exact same executable bytes in Projection and generate a Projection Sigstore bundle for those bytes.
4. Verify/record identical executable SHA-256 and both bundle digests.
5. Leave the bridge as the final/latest Windows release in Deep so older clients can reach it.
6. Publish later versions only from Projection.
7. Bound old trust by repository and maximum bridge version; never trust an organization wildcard.
8. Change Deep desktop code generation to advertise Projection only after the bridge exists and verifies.

This feature may implement/test the transition but does not authorize publication. The exact bridge version is selected against current release state at release-planning time, not assumed from this plan.

## Ignored local continuity files

Before removing `android-client` from Deep, preserve without reading/logging/staging:

- `android-client/keystore.properties`
- `android-client/local.properties`

The safe procedure validates the exact destination client root, copies the files without following links, retains matching ignore rules, verifies neither appears in `git status`, and keeps the originals until a signed Projection Android build succeeds. An externally referenced signing keystore is not copied merely because its path appears in configuration.

## Extraction sequence

1. Refresh Projection with `--no-prune`, establish `main` at the live remote `master` tip without deleting `master`, and create no archive refs.
2. Seed all owned source/tests/tooling/workflows on a feature branch while Deep still retains its copy.
3. Build standalone package/resources/manifest and enforce no Deep imports.
4. Add the Projection submodule to Deep and install it in local/container builds.
5. Split host-specific chrome surfaces into Deep controllers and Projection view builders.
6. Replace hard-coded templates/static/protocol paths with package accessors.
7. Run dual-source parity on representative rendering, adaptation, assets, chrome, and manifest.
8. Update Deep integration tests to consume Projection.
9. Implement/test updater and store-identity continuity; recreate remote release controls only near final qualification.
10. Preserve ignored Android continuity files.
11. Remove the Deep-owned duplicate only after runtime imports and tests no longer load it.

## Verification ownership

Projection tests cover pure render output, sanitization, accessibility, unknown components, ROTE profiles/adaptation, asset/package integrity, UI-manifest drift, every client unit/build/lint suite, and release/updater logic.

Deep tests cover surface controller authorization/data behavior, chrome events, UI transport/dispatch, server shell/auth/CSP integration, cross-component voice behavior, release-readiness aggregation, and the composed live flow.

No component revision is compatible until the shared fixture passes across web, Windows, Android, and Apple consumers or an explicit supported degradation is recorded and enforced by the shared definition.
