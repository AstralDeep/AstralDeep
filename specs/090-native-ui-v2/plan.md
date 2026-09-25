# Implementation Plan: Native UI v2 parity

**Branch**: `codex/090-native-ui-v2` | **Date**: 2026-09-24 | **Spec**: [spec.md](spec.md)

## Summary

Capture the running v2 browser at comparable desktop, tablet and phone dimensions before changing native UI. Extend existing server-owned chrome and ROTE responses with a negotiated console presentation, reuse account-filtered landing data, and implement thin native presentation in Apple first, then Android. Keep transport, authorization, continuity, voice, attachment and export machinery. The owner explicitly defers Windows; watchOS is now included under the owner's scope expansion and receives appropriate small-screen adaptation.

## Technical Context

- **Languages**: production Python 3.11; existing vanilla JavaScript/CSS; Swift 5.9-compatible SwiftUI; Kotlin/JVM 17 and Compose.
- **Dependencies**: existing FastAPI, AstralProjection, AstralPrimitives, AstralCore, platform UI/chart libraries and voice SDKs. Reuse the web's licensed Open Sans asset, with reproducible native conversion if necessary. No new runtime library is planned. Docker ARM builds require OpenSSL development headers for the existing imaging dependency.
- **Storage**: no schema migration or new durable backend state. Existing owner-scoped history/preferences remain authoritative; shell state and current selection remain scoped to the authenticated conversation.
- **Testing**: pytest and component fixtures; real-shell Playwright; SwiftPM/XCTest/UI tests and swift-format; Kotlin/JUnit/Compose instrumentation, Kover, ktlint and Android lint. Preserve existing drift/security gates.
- **Platforms**: iPhone, iPad including split view, macOS, then Android phone/tablet. Windows source is untouched; watchOS is included for contract, adapted UI and live verification.
- **Performance**: preserve streaming/event ordering; resize re-adapts existing data without rerunning agents. Avoid redundant capability reports and duplicate live canvas mounting.
- **Constraints**: obtain live references first. Native login is real PKCE and remains user-operated. Docker/Xcode updates were completed by the owner during setup.
- **Scope**: shell/navigation, landing catalog, result-feed/fullscreen, composer, settings placement, six existing v2 primitives, selection/agent-intro affordances, capability aggregation, tests and vault evidence.

## Constitution Check

- **Server-owned UI**: native console consumes shared chrome, shared landing data and ROTE presentation; no new frontend framework or native settings catalog. Product wording/action order is emitted by shared server definitions. Existing web administrative/tour exclusions stay server-enforced.
- **Capabilities**: explicit console contract negotiates additive fields. Legacy clients receive existing shape/fallbacks; native v2 component delivery requires advertised supported types. Layout depends on logical viewport/capabilities, not platform-name forks.
- **Security**: retain real Keycloak and normal dispatch. Selection/agent-intro actions keep owner/request/connection fences and fail-closed validation. Local presentation actions grant no authority. Private data/secrets stay out of captures/vault.
- **Contracts**: existing chrome/ROTE frames and primitive types. Record additive contract/dispositions in Projection's manifest; update composition protocol metadata after qualifying the exact component revision. No Primitives or Plane changes planned.
- **Testing**: at least 90% changed-code coverage, existing CI checks, Python Ruff, maintained JS ESLint, recursive Swift format, Kotlin ktlint/Android lint. Reproduce baseline failures separately; retain meaningful authority assertions.
- **Live verification**: browser, iPhone, iPad, macOS, Android phone/tablet against actual Docker backend, real IAM/provider configuration and synthetic reference content. Bind evidence to source/artifact identity; mock fixtures supplement live acceptance.
- **Scope exception**: owner reserves Windows redesign for Windows; preserve legacy wire behavior. No all-client release, merge, deployment or store claim. Watch presentation follows its ROTE capability limits.
- **Release authority**: no signing/publication/protected-evidence mutation is authorized. Constitution X remains in force. Hosted candidate CI requires a concrete authorized push; local CI-equivalent results are distinct.
- **Vault**: meaningful phase/verification checkpoints update curated pages/index/log and commit/push only the vault under standing authorization. Preserve unrelated `.obsidian/graph.json` edits.

Initial/post-design review: no unresolved architectural violation within the bounded implementation. Live-reference and verification tasks remain required, not satisfied gates.

## Project Structure

```text
specs/090-native-ui-v2/
  spec.md, plan.md, research.md, data-model.md, quickstart.md
  contracts/console.md, checklists/requirements.md
  tasks.md, analysis.md, verification.md
backend/orchestrator/
  web_landing.py, orchestrator.py, projection_surfaces/
backend/tests/ and backend/orchestrator/tests/
components/AstralProjection/
  backend/rote/, backend/webrender/chrome/
  contracts/ui_protocol.json, contracts/fixtures/
  apple-clients/AstralCore/, apple-clients/AstralApp/
  android-client/core/, android-client/app/
  tests/, tooling/web-ci/, docs/UI_V2.md
../kos-wiki/
  wiki/synthesis-astral-native-ui-v2.md
  assets/astral-native-ui-v2/ (owner-requested captures/index)
  index.md, log.md
```

## Execution Design

1. Bring up current backend; capture and inspect references/measurements, then publish the vault reference checkpoint.
2. Add bounded console model and ROTE presentation fixtures. Reuse web landing scenarios/categories/agents. Shared chrome emits composer actions/settings navigation; non-negotiating clients retain legacy output.
3. Apple: decode/validate console and ROTE data; aggregate actual viewport/scale/input/reduced-motion/component/voice capabilities. Specify reset/reconnect/account behavior.
4. Apple: sidebar/drawer, scenario grid, conversation/result cards/fullscreen, bottom composer, More and modal settings. Retain live component state/action/capture contexts across fullscreen. Add six renderers and fonts while separately adapting watch presentation.
5. Apple: existing Advanced selection and Load/Run agent examples through shared authenticated surfaces; denial/stale/reset tests. Run Apple/backend/web checks and live iPhone/iPad/macOS/watchOS walkthroughs, then checkpoint the vault.
6. After Apple implementation and available verification, implement Android using its existing transport/model boundaries; verify phone/tablet/rotation/keyboard and checkpoint.
7. Run affected CI-equivalent checks and changed-line coverage, inspect final diff, qualify/pin component metadata, and record hosted/live gates accurately. A CI requirement alone does not authorize product publication.

## Complexity Tracking

An additive versioned payload avoids replacing transport or embedding another application. Negotiation preserves deferred Windows and shipped legacy clients. No new database, primitive definition or runtime service is introduced.
