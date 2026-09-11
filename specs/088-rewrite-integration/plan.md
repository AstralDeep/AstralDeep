# Implementation Plan: Rewrite integration

**Branch**: `codex/088-rewrite-integration` | **Date**: 2026-09-10 | **Spec**: [spec.md](spec.md)

## Summary

Port every rewrite capability through existing Deep/Plane/Projection boundaries, beginning with a calmer web experience. Keep institutional Keycloak, existing accounts/data and richer ecosystem capabilities. Ordinary Send uses normal dispatch; consequential effects, publication and new authority retain approval. The September 11 follow-up advances Android and Apple native parity and store preparation; Windows redesign is explicitly excluded from this milestone. Leave the standalone rewrite and its data untouched.

## Technical Context

**Language/Version**: Python 3.11; vanilla JavaScript/CSS/HTML; existing PySide6, Kotlin and Swift clients.

**Primary Dependencies**: Existing FastAPI, psycopg2, cryptography, httpx, Keycloak/RFC 8693 and pinned AstralPlane/Projection/Primitives/LETS. No new third-party runtime dependency, ORM or frontend framework.

**Storage**: Existing PostgreSQL and configured blobs through Plane public repositories and guarded migrations; existing encrypted owner stores. No Deep-owned SQL or donor database.

**Testing**: pytest/coverage, Ruff, Projection's tracked ESLint and Playwright tooling, Windows pytest, Android lint/JUnit/Kover and Apple Swift/XCTest.

**Target Platform**: Production Linux backend port 8001, web first, continued native compatibility and later native redesign.

**Project Type**: Multi-repository server-driven application with one orchestrator, Plane runtime and Projection UI authority.

**Performance Goals**: One ordinary Send without an extra planning round trip; bounded owner-paginated reads; scheduler progress despite blocked older owners. Measure admission/render latency against the exact baseline with identical fixtures; do not invent an SLO.

**Constraints**: 320 CSS pixels, 200% text, keyboard accessibility; one primary task action; current authority at mint/effect boundaries; bounded ephemeral source/guidance material; no private shell caching; changed Python coverage >=90%.

**Scale/Scope**: All families in `source-inventory.md`, including runtime, monitoring, agents, guidance, memory, providers, framework ingress, diagnostics and clients. Web and full native qualification are separate milestones.

## Constitution Check

Pre-design and post-design assessment: compatible with existing architecture and security policy. Completed contract/task analysis mapped all 36 requirements and 47 capability families, with no critical issue. Its one high inconsistency, accepted-retry receipt ordering, was corrected before implementation. The explicit web-first timing decision in FR-024 does not waive authorization, data protection, shared contracts or live evidence.

| Gate | Design and evidence |
| --- | --- |
| Python/dependencies | Reuse approved libraries and component facades; no selected new primitive or runtime dependency. Any unavoidable addition needs its concrete separate approval. |
| SDUI/parity | Projection owns reusable surfaces/theme and negotiated dispositions. Deep authorizes host actions. Retain existing native capabilities and qualify compatibility before the web milestone; redesign/live native qualification follows. |
| IAM/security | Reuse `https://iam.ai.uky.edu/realms/Astral`, existing BFF/CSRF/native PKCE, verified subject ownership and RFC 8693. Preserve PHI, egress, tool, budget and hash-chained audit gates. |
| Durability | Extend Plane's guarded registry/repositories only for missing behavior. Qualify populated upgrade, repeat migration, recovery and no duplicate effect before updating Deep's exact schema/digest pin. |
| Tests/lint | Golden, denial, failure and concurrency fixtures; >=90% changed Python coverage. Deep/Plane/Projection Ruff and Projection `tooling/web-ci` ESLint CI gates; all touched native-language lint and local module suites. |
| Staging | Isolated exact candidate images with real configured institutional IAM, PostgreSQL/workers and representative migrated synthetic/approved data. Bind commands, migration receipts and affected live client observations to exact Deep/component SHAs. Empty/mock-only fixtures do not qualify. |
| Release evidence | Existing `scripts/prepare_release_evidence.py` must run with canonical candidate inputs before an authorized product push. Local output stays diagnostic; existing protected CI verifies canonical identities, digests and policy. |
| Exceptions/bootstrap | None requested or authorized. Missing evidence remains open. Any later structural remote-only blocker requires Constitution X's exact SHA/scope/expiry, external inventory and separate provider-native lead approval before push; no trust/staging/coverage waiver. |
| Publication | The owner's September 11 follow-up explicitly authorizes review branches and draft PRs for the touched repositories. Rewrite merge, production deployment and release remain unauthorized. Keep incomplete qualification visible; retain protected publishers, native short-lived CI identity and create-only collision policy. Candidate jobs remain unprivileged. |

The September 11 PR request authorizes code-review publication of the implemented
increments and clearly labeled unfinished Plane foundation. It creates no staging
or release claim, protected exception, qualification waiver, or release-evidence
bootstrap. Run the local release-evidence diagnostic and retain its missing-input
result; draft review publication does not make that result an authorization to merge
or release. The installed local candidate continues to pin qualified Plane 079.001.
The separate LETS dependency maintenance request authorizes merging its reviewed
updates only after its normal checks pass.

The current September 11 follow-up authorizes qualified store publication and
promotion of completed draft PRs to ready for review; the owner reserves merging.
This supersedes the earlier publication-authority limit, but supplies no missing
institutional login, store credentials, protected release decision or qualification
evidence. Existing draft PRs remain drafts until those completion conditions hold.
The current native parity work targets implemented web behavior, not fictitious
clients for unimplemented operation/guidance/framework APIs.

## Architecture and sequence

1. Pin provenance and inventory; establish tests and local component branches from clean checked-out pins.
2. Simplify existing Projection shell and Deep welcome. Preserve current ordinary chat dispatch while enhanced durable tasks are built. Owner-safe draft/bootstrap behavior precedes adoption of donor browser state.
3. Extend existing Plane assignment/admission/scheduler/guidance facades. An additive one-shot profile must preserve legacy grant-required validation. WorkAdmission owns claims/capacity; assignment state owns the durable task. No competing donor lifecycle engine.
4. Add Deep services/read models and ordinary-dispatch adapters. Preserve payload-free operation reconciliation; richer authorized task details use a separate versioned interface. Retain user/system provider separation and working provider breadth.
5. Integrate declarative agents, revisioned guidance, private notes, monitoring/artifacts and framework compatibility with shared Projection surfaces. Validate exact revisions, approvals and current authority at use time.
6. Qualify web journeys, populated upgrade/recovery and every inventory family; incomplete features stay open. Complete native redesign and live qualification afterward before declaring full integration.

## Project Structure

Feature artifacts live under `specs/088-rewrite-integration/`: spec, clarification, research, data model, source inventory, runtime/UI contracts, quickstart and tasks.

```text
backend/orchestrator/                     # product services, API and dispatch
backend/orchestrator/projection_surfaces/ # authorized host adapters
backend/persistent_agents/                # existing durable execution
backend/personalization/                  # owner guidance/memory policy
backend/shared/                           # transport/egress/security seams
backend/tests/                            # integrated regression tests
components/AstralPlane/                   # repositories/migrations/recovery/tests
components/AstralProjection/              # shared UI/web/native contracts/tests
config/astral-composition.json            # exact qualified component identities
```

Contracts name concrete existing and proposed files. Commit each component separately, then pin qualified exact revisions in Deep. Primitives and LETS stay unchanged unless an explicitly scoped coordinated change proves necessary.

## Complexity Tracking

No additional service, frontend framework, ORM, identity system or migration framework. Add versioned compatible interfaces to existing lifecycle/UI owners; do not replace working capabilities with donor restrictions.
