# CI Ownership Design for Feature 074

**Status**: Approved design  
**Date**: 2026-08-21  
**Decision owner**: Repository owner  
**Scope**: AstralDeep, AstralProjection, AstralPlane, AstralPrimitives, and LETS

## Context

Feature 074 replaced monorepo-owned presentation and persistence source with exact repository pins. The existing hosted workflows still reflect the former layout:

- AstralDeep attempts Projection client and contract jobs without credentials for its private Projection submodule.
- AstralProjection and AstralPlane contain deliberately disabled seed workflows.
- AstralPrimitives has publishing automation but no pull-request qualification.
- LETS comparison tests require the signed `v1.0.10` tag, while most CI jobs use a shallow checkout.
- AstralDeep's history scan reports seven reviewed historical fixture/digest detections that require exact-fingerprint disposition, not broad path exclusions.

Giving an untrusted pull-request job a cross-repository credential would violate the candidate isolation and secret-free constraints. The owner therefore approved repository-owned CI, with full private composition qualified locally for this migration under Feature 074's explicit constitution waiver.

## Decision

CI follows mutable-source ownership. Each repository qualifies the code it owns; AstralDeep qualifies orchestration and exact composition declarations. No workflow receives a cross-repository secret, repository-scoped GitHub App, or custom token broker.

| Repository | Required pull-request qualification |
|---|---|
| AstralProjection | Python package/render/ROTE/protocol tests; web lint/unit/browser tests; Windows client tests; Android lint/unit/coverage/build/instrumented tests; Swift format/package/iOS/macOS/watchOS tests and builds |
| AstralPlane | Ruff; package architecture and import checks; unit tests; PostgreSQL migration/integration/recovery tests; wheel build and clean install |
| AstralPrimitives | Supported-Python unit/serialization tests; source and wheel build; clean-install import and serialization smoke |
| LETS | Lockfile, Ruff, format, mypy, full tests on supported operating-system/Python lanes, package build, distributed acceptance, and existing security checks; comparison lanes fetch complete tag history |
| AstralDeep | Ruff; Deep-owned backend/unit/security/contract tests; exact composition and ownership declarations; reviewed exact-fingerprint secret baseline; locally executable release-tooling checks |

Projection-owned Android, Apple, Windows, web, and renderer jobs are removed from AstralDeep's hosted workflows. Plane-owned persistence mechanics are not retested through duplicate source in Deep. Deep retains cross-component declaration and integration guards that can run without reading private component contents.

## Private composition boundary

GitHub-hosted AstralDeep pull-request jobs cannot fetch private Projection or Plane submodules with the repository's default token. They therefore must not claim a composed image, boot, or full integration result.

Before any PR is marked ready, the exact five candidate commits are checked out locally and the full recursive composition is qualified, including backend flags on/off, nested suites, component ownership and composition checks, migration/recovery, client suites, image build, boot/fail-closed checks, and changed-code coverage. The exact commands, commit identities, and results are recorded in the PR handoff and the curated knowledge vault. Hosted owner-repository checks remain independently required after push.

This is a migration-only exception. Restoring hosted full-composition qualification later requires a separately approved protected runner or another secret-free mechanism; this design does not add one.

## Test-first workflow changes

Every workflow repair starts with a local failing regression that demonstrates the observed defect:

1. LETS gains a history preflight exercised against an intentionally shallow checkout; CI then checks out the tag/history required by signed comparison tests.
2. AstralDeep's secret baseline test requires only the reviewed fingerprints; the history scan then passes without a path-wide allowlist.
3. Projection and Plane workflow contract tests reject disabled jobs and stale monorepo paths before their owner workflows are activated.
4. Primitives gains a workflow contract test and package smoke that prove pull requests run tests without invoking publication.
5. Deep workflow contract tests reject Projection client jobs and reject a synthetic green full-composition claim when private components were not available.

Tests assert behavior or parsed workflow structure rather than matching incidental YAML formatting.

## Local-first execution

Changes are held locally until the complete feasible matrix passes on this Mac, using Linux containers where the workflow requires Linux and the local Apple/Android toolchains for native checks. Windows-only and GitHub event semantics are the unavoidable hosted remainder and run only after all local equivalents pass.

Each repository is committed and pushed once after local qualification. Draft PRs are then marked ready, triggering one intended hosted run. A failing hosted lane is diagnosed and reproduced locally before another push whenever the platform permits.

## Readiness and merge order

A PR is ready only when its working tree is clean, its branch is current enough to merge without ambiguity, local owner gates pass, required artifacts are recorded, and no known failure is hidden by a skip or weakened assertion.

Recommended merge order is AstralPrimitives, AstralProjection, AstralPlane, LETS, then AstralDeep. The first four PRs must be green and their exact commits must match Deep's composition declarations before Deep may merge. Marking a PR ready is not merge or release authorization.

## Failure handling

- Missing tools or platform access are reported as unavailable; they are not converted to success.
- Coverage remains at least 90% for changed executable product lines in the repository that owns those lines.
- Unrelated baseline failures are demonstrated separately and are not silenced.
- Security findings receive exact reviewed dispositions; broad suppressions are prohibited.
- Publishing, deployment, store submission, merging, and release remain out of scope.

