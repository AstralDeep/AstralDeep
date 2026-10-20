# Implementation Plan: Source-Bound Agent Actions

**Branch**: `codex/097-source-bound-agent-actions` | **Date**: 2026-10-20 | **Spec**: [spec.md](spec.md)

## Summary

Make the shared server dispatch distinguish signed human intent from lower-trust agent-visible material. Classify registered actions by effect, bind each proposed effect to its actor, owner, target, arguments, source state, and current authorization, and require a single-use exact approval for source-derived effects. Unknown lineage, unclassified effects, enforcement failure, or missing audit evidence fail closed. Reuse the existing server-rendered approval card and `authorize_action` action, so no client protocol or native source changes are needed.

## Technical Context

**Language/Version**: Python 3.11 production target; existing vanilla-JavaScript web client only for live verification.

**Primary Dependencies**: Existing orchestrator dispatch, Keycloak claims, RFC 8693 delegation, HITL confirmation, LETS/Plane audit. No new dependency.

**Storage**: Bounded in-memory lineage for live turns. Previously retained or restarted material without verified lineage is unknown and cannot authorize an effect; an owner may reauthorize the exact action. Existing approval pending state is short-lived and single-use. No new product schema.

**Testing**: `backend/tests/` denial/failure/approval tests, real registered dispatch with synthetic owners, grouped backend web suite, Ruff and 90% changed-line coverage; web live approval-card test. Native tests omitted at the user's request.

**Target Platform**: All server dispatch channels and backend-served web UI. Shared protocol, AstralProjection, Apple, and Android code remain unchanged.

**Project Type**: Security-relevant orchestrator and policy change.

**Performance Goals**: Bounded per-turn lineage state and no model/network call in the enforcement decision. Existing hosted jobs finish within 30 minutes.

**Constraints**: No trust promotion by byte equality, no reliance on optional taint/policy flags, no bypass of existing authn/authz/privacy/delegation/LETS/audit gates, fail closed on uncertain effect outcome.

**Scale/Scope**: Registered first-party, external, and user-authored agent tools across interactive, delegated, scheduled, and retry paths.

## Constitution Check

| Rule | Design response |
|---|---|
| Identity, authority, and audit | Preserve signed human request, owner isolation, RFC 8693 attenuation, existing permission/PHI/egress/HITL/LETS checks. Source material never mints human authority. Decision and outcome enter the existing hash-chained audit. |
| Fail closed production posture | New source/effect gate runs independently of optional `FF_TAINT_TRACKING` and `FF_POLICY_ENGINE`. Missing classification, lineage, approval, or audit denies effect. |
| Client parity | Reuse existing `Card`, `Text`, `CodeBlock`, `Button`, and `authorize_action`; no new frame, primitive, or native contract. Only web client is tested under the user's instruction, so native behavior remains unverified. |
| Plane ownership | Do not add SQL or borrow Plane connections. Restart invalidates volatile provenance/approval and requires fresh exact owner decision before effect. If durable reuse becomes necessary, a separate qualified Plane migration is required. |
| Source and CI | Python files carry short headers and minimal comments. Required tests cover golden, denial, failure, and real dispatch; changed lines retain 90% coverage; no live third party in CI. |
| Release evidence | Local tests are diagnostic; exact-candidate protected qualification remains authoritative and no local commit authorizes push/merge/release. |

No constitution violation is planned. Skipping native tests limits qualification claims; no client behavior is changed.

## Phase 0: Research Decisions

See [research.md](research.md). The current optional `taint.py` fingerprints exact strings, uses name lists, and treats coincident user text as trusted. Its output recording only covers presentation components. Enabling it cannot meet the spec. A conservative provenance envelope and explicit action declarations at the shared dispatch gate are required. Existing HITL exact held calls provide a bounded approval UI and physical-call replay defense, but source-bound enforcement must trigger independently and make audit failure visible.

## Phase 1: Design

See [data-model.md](data-model.md), [contracts/source-bound-decision.md](contracts/source-bound-decision.md), and [quickstart.md](quickstart.md). An origin set is attached to the turn and carried through tool results, derived messages, delegation, and retained context. If precise lineage is unavailable, the value is `unknown`; unknown is no stronger than untrusted for an effect. The action declaration describes read, local mutation, disclosure, and privileged control. The approval is bound to the canonical tool, owner, exact arguments/destination, authority state, and one physical attempt.

## Project Structure

```text
backend/orchestrator/
  source_bound.py           # origin/effect declarations and decisions
  orchestrator.py           # shared authorization and physical-call checks
  hitl_confirmation.py      # exact approval and audit propagation
backend/tests/
  test_source_bound.py
  test_source_bound_dispatch.py
specs/097-source-bound-agent-actions/
  research.md
  data-model.md
  contracts/source-bound-decision.md
  quickstart.md
  tasks.md
```

**Structure Decision**: Keep all enforcement on the host. Projection continues rendering existing primitives without a component or client change.

## Failure Modes and Migration

1. Inventory registered tools and aliases before enabling enforcement. A tool lacking a sound effect declaration is unavailable for effects, with an operator diagnostic.
2. Mark external, uploaded, generated, remote-agent, tool-produced, and retained content with an origin set; preserve it across supported in-process transformations. On restart, unverified historic content is unknown and cannot authorize an effect.
3. Before an effect, bind current signed actor and intent, owner, canonical tool, target, material identity, source state, delegated authority, and existing policy. Require exact owner review when source-derived material contributes; deny unattended effects requiring review.
4. Recheck the binding and one-use approval immediately before physical invocation. Uncertain completion cannot silently retry under the same approval.
5. Emit privacy-preserving audit decisions and outcomes; if an effect-path audit write fails, deny the effect and surface a failure.
6. Roll out after inventory and dispatch tests are complete. Do not infer protection merely from flipping an existing optional flag.

## Post-Design Constitution Check

No schema, UI protocol, primitive, or native-client change is required. The shared server boundary remains authoritative across channels. Web and backend results will be reported separately from unverified native behavior.
