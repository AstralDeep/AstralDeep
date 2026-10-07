# Implementation Plan: Apple Controls and Web/Mobile Consistency

**Branch**: `codex/094-apple-wrapper-controls` | **Date**: 2026-10-05 | **Spec**: [spec.md](spec.md)

## Summary

Restore distinct settings actions through ROTE, correct native form state and accepted-theme rendering, supply the ordinary Drafts surface, and align dashboard/chrome behavior with the server-owned web reference. Preserve real authentication, owner isolation, action budgets, guidance authority and approved watch handoffs. Exercise the wrapper without sending a chat query.

## Technical Context

**Language/Version**: Python 3.11, vanilla JavaScript, Swift 5.9-compatible SwiftUI, Kotlin/JVM 17, existing PySide6 Windows client.

**Dependencies**: Existing AstralProjection ROTE/webrender, astralprims, FastAPI, AstralPlane, Keycloak, SwiftUI, Android Compose and PySide6. No new dependency or primitive.

**Storage**: Existing account/provider/theme/profile/draft state through AstralPlane. No schema migration; form edits and pending operations remain client-local.

**Testing**: pytest/pytest-cov/diff-cover, JavaScript lint and renderer fixtures, Swift package tests and Xcode app tests/builds, committed Gradle lint/unit/coverage/build gates, Windows offscreen tests, live matched-client wrapper audit.

**Platforms**: Web, iOS/iPadOS, macOS and Android; watchOS declared handoffs/shared appearance; Windows regression for shared adaptation and options.

**Performance/Constraints**: Existing bounded frame/action limits and deterministic suites within the 30-minute CI budget. No arbitrary CSS interpreter, model-driven authority, mock login or credential fixture copied from the owner's account.

**Scope**: Four stories, seventeen requirements; all ordinary wrapper/settings destinations plus approved capability dispositions. Layout comparisons at 320/390/768/1440 logical pixels and increased text size.

## Constitution Check

Pre-design and post-design checks pass for the proposed design. Current Deep constitution v6.2.0 and Projection v2.3.0 govern their respective source trees. Reusable renderer/client changes are authored in sibling `AstralProjection` on `codex/094-apple-wrapper-controls`, starting at `8d0b84da44035a1ee51e44449eacb9254f63ac6a`; Deep-owned authorization/surface composition stays in Deep. Projection extraction transformations receive exact digests and reasons in `provenance/transformations.json`.

No schema, IAM, primitive vocabulary, provider or egress change is planned. Tests include read-only/action-budget adaptation, owner/permission denials, failed operations and preserved edits. Coverage remains at least 90% for changed Python; Kotlin core retains its gate and Swift/app coverage is measured. Inspect and use existing tracked Ruff, ESLint, ktlint and Swift lint/format configurations and corresponding CI gates before edits; never waive them. Later owner-authorized release preparation is recorded below.

Qualification uses the exact local Projection candidate with the configured real sandbox backend for safe account navigation and a local candidate backend with real PostgreSQL/Keycloak for Deep-owned changes when available. Test fixtures remain separate from the owner's secrets/data. Evidence records source revision, toolchain, account role without identity, form factor, exact commands and outcomes. Local tests do not establish component CI qualification or deployment. Deep's pin moves only after the exact Projection revision meets its own CI/policy requirements; the current pin remains while qualification is incomplete.

The owner subsequently authorizes qualification, paired merges, Sandbox deployment and App Store Connect upload. Every uploaded Apple product must be exactly 1.8 / build 67, with no separate pre-upload build-number lookup or App Review submission. This does not authorize an evidence exception, bootstrap, CI/policy weakening or authentication bypass. Before a Deep candidate push, run the current default-branch evidence collection/normalization/parser and protected verifier required by Constitution X; local diagnostics cannot approve promotion. Publication remains under the separately protected native CI publisher and its approval boundary. [release-readiness.md](release-readiness.md) records the concrete infrastructure blockers and the exact source/artifact boundaries.

## Project Structure

```text
AstralDeep/
  specs/094-apple-wrapper-controls/
    spec.md, audit.md, clarification.md, plan.md, research.md
    data-model.md, quickstart.md, tasks.md, contracts/control-semantics.md
  backend/orchestrator/projection_surfaces/drafts.py
  backend/orchestrator/projection_surfaces/llm.py
  backend/orchestrator/chrome_events.py
  backend/tests/chrome/test_surface_drafts.py
  backend/tests/test_turn_selection_surface_089.py
AstralProjection/
  backend/rote/adapter.py
  backend/webrender/renderer.py, static/client.js
  src/astralprojection/chrome/_components.py, form_options.py
  tests/rote/test_adapter_contracts.py
  apple-clients/AstralCore/, AstralApp/, NativeAppearance/
  android-client/app/, core/
  windows-client/astral_client/, tests/
  provenance/transformations.json
```

## Execution and Qualification

Complete clarification, research/design, task coverage and read-only analysis gates first. Reproduce defects before repairs. Parallel work has disjoint ownership: Apple, Android/Windows, Deep Drafts/guidance, and root-owned ROTE/web/integration. Root personally reviews shared authorization and renderer seams, integrates tests and provenance, and verifies live controls.

Run narrow meaningful tests first, then affected component gates once the integrated change stabilizes. Record every inventory control as passed, failed, pending or explicitly adapted. No ordinary control receives a capability exception simply because its implementation is missing. Update the curated knowledge vault at design and implementation checkpoints; keep vault commits separate from product commits.

## Complexity Tracking

No constitution exception or additional architecture layer. Form semantics helpers normalize the existing wire contract; durable policy and success authority remain server-owned.

## October 6 owner-directed release execution

The owner explicitly directs immediate task PR merges and Apple upload without waiting for remaining CI. Preserve all measured test/coverage failures and the protected-infrastructure audit; use exact reviewed merge identities and repository-provisioned signing through the existing main-ref manual uploader. Adopt merged Projection2da04449 and its canonical protocol digest, remove run-number overrides, and select the same exact Xcode26.6/17F113 used by the passing native lane. Both export templates already disable automatic version/build management. Every uploaded product remains1.8/67; App Review submission remains outside scope.
