# Implementation Plan: Persistent Agents

**Branch**: `codex/079-persistent-agents` | **Date**: 2026-09-05 | **Spec**: [spec.md](spec.md)

**Input**: `specs/079-persistent-agents/spec.md`

## Summary

Add durable owner-directed assignments that wake for scheduled source checks, retain progress and delegated results, and wait between bounded execution episodes. The initial live example watches a public release page with `web-research-1:fetch_page`; it needs no new provider credentials. Registered inbox/bug readers can use the same controller when available.

AstralPlane owns definitions, revisions, claims, deduplication, action/proposal records, task checkpoints, activity and cumulative reservations. Deep owns policy, bounded planning, normal dispatch, grant derivation, approval re-entry and supervision. Projection owns reusable views built from existing primitives. No SQL in Deep, model loop while idle, separate frontend or in-memory recovery fallback.

## Technical Context

**Language/Version**: Python 3.11; existing vanilla JavaScript, PySide6, Kotlin/JVM 17 and Swift 5.9-compatible client contracts.

**Primary Dependencies**: Existing FastAPI, Pydantic, asyncio, PostgreSQL/AstralPlane, Keycloak offline grants/RFC 8693, configured OpenAI-compatible LLM client, AstralProjection/astralprims. No new third-party runtime dependency.

**Storage**: Plane guarded `079.001` migration from `075.001`. Four bounded assignment/event/action/activity tables; validated bounded JSON carries the task graph and checkpoint. No credentials in assignment records.

**Testing**: Pytest/coverage, real PostgreSQL migration/concurrency tests, Deep dispatch denial/recovery suites, Projection views, Windows/Android/Apple parity gates, real web/native flows, candidate-bound recovery evidence.

**Target Platform**: Existing Docker/FastAPI on port 8001; web, Windows, Android, macOS, iOS and explicit server-owned wrist disposition.

**Project Type**: Multi-repository server/client platform extension.

**Performance Goals**: 25 active assignments per owner; minimum 60-second cadence; due discovery within a 30-second tick; controls acknowledged within two seconds in the verification workload; no model calls between eligible wakeups.

**Constraints**: `FF_PERSISTENT_AGENTS` defaults off; exact composition pins; no automatic sensitive mutation; immutable proposal binding; finite resource/retention caps; source text never creates authority.

**Scale/Scope**: One assignment lease at a time, bounded concurrent children and global admission, bounded retries/steps/output/context, explicit lifetime limits. No new provider connector or webhook.

## Constitution Check

Pre-design: PASS for the proposed architecture. Implementation evidence remains pending.

| Principles | Obligation |
|---|---|
| I, IV, V | Python 3.11, existing dependencies, root Ruff and tracked client lint gates for each changed language. |
| II, VIII, XII | Existing primitives; Projection reusable views; Deep authorized snapshots; one action manifest and all client drift guards. Explicit wrist status/chat controls and full-client sensitive review. |
| III, VI | Golden/edge/denial/failure/recovery tests; measured changed-code coverage at least 90%; documented APIs and contracts. |
| VII | Explicit fresh offline authority; current per-tool permission checks; normal PHI/taint/egress/provenance gates; foreground-only sensitive changes. |
| IX | Plane alone owns guarded `079.001`, representative repeat-safe migration and non-destructive recovery; Deep pins only the qualified exact revision/digest. |
| X, XI | Real configured Docker/Keycloak/PostgreSQL/workers and affected clients; candidate/artifact-bound evidence. Source tests alone are not live correctness. |

Qualifying staging is the local candidate-derived Docker deployment using real existing Keycloak/PostgreSQL configuration and representative existing data, with a dedicated owner-consented assignment and public source. Record safe IDs, digests, migration/recovery observations and exact artifact identity. Controlled source-revision fixtures supplement, but do not replace, the real public-page run. Never include credentials or private source content in evidence.

No product commit, push, PR, merge, release, remote deployment or store publication is requested. Prepare and test component edits before any final exact-pin commit needed for normal image qualification; do not weaken clean-pin checks. Baseline: Deep `34609998`, composed Projection `b69597a`, Plane `4a1d990`, Primitives `8dadde1`, LETS `6245189`.

No release exception/bootstrap is authorized. Run all locally executable gates and record unavailable native/provider inputs honestly. Later requested publication remains subject to Constitution X's independent protected verifier, exact-candidate evidence and native CI identity. This feature changes no publisher or evidence policy.

## Architecture

1. **Create/consent**: Validate owner, instructions, source tool/arguments, exact allowed-tool set, cadence and limits. Store an idempotent proposal; activation requires explicit owner consent and a valid captured offline grant. Missing grants enter authorization-waiting state.
2. **Wake/claim**: A bounded supervised loop claims due assignments through Plane and global work admission. Reconstruct all execution from durable records. Expired leases cannot renew, dispatch or publish. No runnable work means no model calls.
3. **Source check**: Run selected read tools through `execute_single_tool` and the normal gate stack under fresh machine authority. Normalize bounded outputs, distinguish failures from valid content, and derive stable source/revision identity. Unchanged content advances cadence without model work or repeated notifications.
4. **Plan/execute**: A bounded structured model decision proposes a task step, checkpoint, delegation, wait or completion. Deterministic code validates configured tools, bounded graphs and transitions. Record completion only after actual success. Resume unfinished work and reuse immutable completed results.
5. **Budget**: Reserve mandatory model/tool/token/time capacity before each dispatch; children and retries share cumulative limits. Failed/uncertain work retains conservative charges. A currency ceiling is optional and requires trusted bounded quotes; activation/revision/execution with a selected currency cap fails closed when no quote exists. Without one, monetary cost is unknown, never zero/free; the no-extra-configuration example uses hard resource limits.
6. **Dispatch/control ordering**: Recheck revision, lease, state, grant, current permission and reservation at every effect boundary. Plane's assignment row lock orders control acknowledgment against durable dispatch-start permits without holding transactions across external requests. A permit issued before control is a begun action and is shown as in flight; no later permit, stale continuation or stale publication is allowed.
7. **Sensitive actions**: Persist exact owner/revision/argument/precondition-bound expiring proposals. Consume approval once in authenticated foreground dispatch through all existing gates, then durably return the result. Uncertain external outcomes require reconciliation; never blindly retry.
8. **Controls/activity**: Idempotent revision-CAS revise/pause/resume/stop/revoke commands invalidate old leases and approvals, cancel local work and fence other workers. Store bounded meaningful activity; unchanged polls do not spam notifications. Stop is terminal.

## Project Structure

### Documentation

`specs/079-persistent-agents/`: spec, plan, research, data-model, quickstart, contracts/plane-assignment-api, contracts/user-interface, tasks, requirements checklist, verification records.

### Source Code

- `components/AstralPlane/src/astralplane/repositories/assignments.py`, migration/revision, public API/catalog/exports and repository/migration tests.
- `backend/persistent_agents/{models,store,service,runner,api,chat_tools,dispatch_context}.py` and focused tests.
- Narrow integration in `backend/orchestrator/orchestrator.py`, `chain_authority.py`, personalization surface/controller registries and feature flags.
- Projection reusable personalization assignment views, action manifest and all affected client drift/interaction tests.
- `config/astral-composition.json` and `scripts/verify_persistent_agents_079.py` after exact component qualification.

**Structure Decision**: Keep ongoing-work logic in a small Deep package, durable mechanics in Plane, and reusable UI in Projection. Central dispatch receives only the necessary safety/resource seams.

## Complexity Tracking

No constitutional exception proposed. Four tables provide indexed lease ownership, arbitrary event deduplication, immutable action/approval identity and owner activity. A bounded task graph avoids a new generic workflow engine.

Post-design architecture review passes with the detailed Plane and UI contracts. Cross-artifact analysis follows task generation. Test, live and exact-pin qualification requirements are implementation gates, not claims made by this plan.
