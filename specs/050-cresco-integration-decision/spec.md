# Spec 050 — Cresco Integration Decision

## Status
**Bounded / Unsupported for generic topology-dispatch.**

The general Cresco bridge contract (spec 050) was planned but the owning agent/runtime is absent. The current GaiaKeep integration is a **specialized** Cresco-gated archival path, not a generic topology/discovery/dispatch bridge between approved Astral deployments. This spec reconciles the original intent with the bounded reality.

## Scope

### In scope
- GaiaKeep integration as a Cresco-gated archival path only.
- WSAPI profile: bounded topology, risk tiers, member-target mapping, fault states.
- Owner/tool/PHI/egress/confirmation and audit boundaries preserved.
- Keycloak / RFC 8693 boundaries unchanged.

### Out of scope (unsupported)
- Generic Cresco bridge topology/discovery/dispatch.
- New bus activation or mesh admission.
- Runtime bridge contract beyond the GaiaKeep archival path.
- Service-key based external admission.

## Risk Tiers

| Tier | Behaviour | Supported |
|------|-----------|-----------|
| T1 — archival | GaiaKeep write-only to approved store | ✅ |
| T2 — bounded wsapi | Read from approved store, no cross-deployment dispatch | ✅ |
| T3 — generic bridge | Topology/discovery/dispatch across deployments | ❌ Unsupported |
| T4 — mesh admission | New bus or key-based external admission | ❌ Unsupported |

## Conformance

Deterministic conformance cases are documented in `tasks.md`. No runtime activation occurs. The image includes explicit unsupported-state declarations for T3/T4 paths.

## Dependencies

- AstralPlane #67 — bounded wsapi profile
- PR #248 — existing GaiaKeep integration (retained, scoped to T1/T2)
- Tracker #242 — non-bounty parent

## Source Traceability

- Original intent: `specs/050-cresco-integration-decision/spec.md`
- Tasks: `specs/050-cresco-integration-decision/tasks.md`
- Feature flag: `backend/shared/feature_flags.py`
