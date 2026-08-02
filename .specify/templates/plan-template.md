# Implementation Plan: [FEATURE]

**Branch**: `[###-feature-name]` | **Date**: [DATE] | **Spec**: [link]

**Input**: Feature specification from `/specs/[###-feature-name]/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command. See `.specify/templates/plan-template.md` for the execution workflow.

## Summary

[Extract from feature spec: primary requirement + technical approach from research]

## Technical Context

<!--
  ACTION REQUIRED: Replace the content in this section with the technical details
  for the project. The structure here is presented in advisory capacity to guide
  the iteration process.
-->

**Language/Version**: [e.g., Python 3.11, Swift 5.9, Rust 1.75 or NEEDS CLARIFICATION]

**Primary Dependencies**: [e.g., FastAPI, UIKit, LLVM or NEEDS CLARIFICATION]

**Storage**: [if applicable, e.g., PostgreSQL, CoreData, files or N/A]

**Testing**: [e.g., pytest, XCTest, cargo test or NEEDS CLARIFICATION]

**Target Platform**: [e.g., Linux server, iOS 15+, WASM or NEEDS CLARIFICATION]

**Project Type**: [e.g., library/cli/web-service/mobile-app/compiler/desktop-app or NEEDS CLARIFICATION]

**Performance Goals**: [domain-specific, e.g., 1000 req/s, 10k lines/sec, 60 fps or NEEDS CLARIFICATION]

**Constraints**: [domain-specific, e.g., <200ms p95, <100MB memory, offline-capable or NEEDS CLARIFICATION]

**Scale/Scope**: [domain-specific, e.g., 10k users, 1M LOC, 50 screens or NEEDS CLARIFICATION]

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

[Gates determined based on constitution file. For changes that affect runtime
infrastructure, name the qualifying persistent or ephemeral staging topology,
the representative migrated dataset, real dependencies/client flows, and how
evidence is bound to the candidate SHA or immutable artifact. For every
maintained implementation language changed, name its tracked lint
configuration and CI gate, explicitly including TypeScript/JavaScript. If the
feature permits temporary client-platform evidence exceptions, name the exact
eligible checks, non-waivable staging/trust checks, seven-day bound, append-only
protected ledger outside the candidate tree, machine-verifiable protected
release-owner approval/registration, durable next-release resolution receipts,
and exact ledger snapshot. When candidate code can influence qualifying evidence
or policy, identify the candidate-independent protected verifier, pinned policy
identity, reconstructed trusted inputs, attested bounded-lifetime decision, and
required gate bound to the installed protected workflow identity rather than a
name-only check. Identify the deterministic local evidence collection,
normalization, and parsing command that must run before push; keep its result
diagnostic and explain how protected CI independently validates canonical
evidence, identities, digests, and policy. If exceptions are permitted, keep
their approval and append-only debt/resolution mutation in native protected CI;
local and candidate jobs cannot authorize them.
If a required canonical input structurally cannot exist until the exact clean
candidate SHA is remotely addressable, either redesign the producer to preserve
ordinary local-before-push ordering or document Principle X's bounded bootstrap:
the named branch/PR and structural blocker, provider-native lead approval bound
to exact initial SHA and RFC 3339 UTC expiry within 168 hours, path-bounded
candidate/evidence-repair scope, locally executable gates, external
candidate-bound fail-closed missing-input inventory, lead-attested exact local
commands plus parseable local-report digests, provider state re-queried for
draft/non-mergeable state, scope, and expiry before every push, per-SHA records
outside the candidate tree, and exact verifier/lead-allowlist identities loaded
from the current default branch, unprivileged isolated candidate jobs,
default-ref guards on every privileged manual-dispatch job, and final
exact-candidate local/protected closure before promotion.
Bootstrap cannot waive staging, coverage, trust, integrity, security, privacy,
or feature-designated non-waivable checks and cannot authorize publication.
For a distributable release, identify the separately pinned protected publisher,
its approval boundary, native short-lived CI identity, create-only/collision
policy, and how candidate workflows remain unprivileged. Repository-scoped
GitHub Apps, installation tokens, and custom token brokers are not permitted.]

## Project Structure

### Documentation (this feature)

```text
specs/[###-feature]/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)
<!--
  ACTION REQUIRED: Replace the placeholder tree below with the concrete layout
  for this feature. Delete unused options and expand the chosen structure with
  real paths (e.g., apps/admin, packages/something). The delivered plan must
  not include Option labels.
-->

```text
# [REMOVE IF UNUSED] Option 1: Single project (DEFAULT)
src/
├── models/
├── services/
├── cli/
└── lib/

tests/
├── contract/
├── integration/
└── unit/

# [REMOVE IF UNUSED] Option 2: Web application (when "frontend" + "backend" detected)
backend/
├── src/
│   ├── models/
│   ├── services/
│   └── api/
└── tests/

frontend/
├── src/
│   ├── components/
│   ├── pages/
│   └── services/
└── tests/

# [REMOVE IF UNUSED] Option 3: Mobile + API (when "iOS/Android" detected)
api/
└── [same as backend above]

ios/ or android/
└── [platform-specific structure: feature modules, UI flows, platform tests]
```

**Structure Decision**: [Document the selected structure and reference the real
directories captured above]

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| [e.g., 4th project] | [current need] | [why 3 projects insufficient] |
| [e.g., Repository pattern] | [specific problem] | [why direct DB access insufficient] |
