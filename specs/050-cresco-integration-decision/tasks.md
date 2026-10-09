# Tasks — Cresco Integration Decision (Spec 050)

## Bounded scope

### T1 — Archival (supported)
- [x] GaiaKeep write to approved store via bounded wsapi
- [x] Owner/tool/PHI/egress boundaries enforced
- [x] Audit trail preserved; no cross-deployment egress

### T2 — Bounded wsapi (supported)
- [x] Read from approved store only
- [x] Member-target mapping documented
- [x] Fault states enumerated
- [x] Risk tier T2 behaviour deterministic

### T3 — Generic bridge (unsupported, explicit)
- [ ] Document unsupported state
- [ ] No runtime path exists
- [ ] No bus admission gate

### T4 — Mesh admission (unsupported, explicit)
- [ ] Document unsupported state
- [ ] No service-key admission
- [ ] No mesh gate

## Conformance acceptance checks

1. **Reconcile spec 050, GaiaKeep and current constitution**: wsapi profile, bounded topology, risk tiers, member-target mapping, fault states.
2. **Retire stale dependency assumptions**: pin authoritative external sources; service keys remain operator-local, grant no mesh admission.
3. **Document explicit support/unsupported behavior**: deterministic conformance cases; no runtime activation or new bus.
4. **Complete reviewable contract**: source traceability, explicit unsupported states, executable acceptance cases; specification status documented without claiming runtime completion.

## Unsupported-state declarations

```
CRESO_BRIDGE_GENERIC = False       # T3 — no runtime path
CRESO_MESH_ADMISSION = False       # T4 — no service-key gate
CRESO_ARCHIVAL_ENABLED = True      # T1 — GaiaKeep only
CRESO_WSAPI_BOUNDED = True         # T2 — approved store read
```

## Linkage

- Tracker: AstralDeep #242 (non-bounty)
- Bounty child: #279
- Dependency: AstralPlane #67
- Existing PR: #248 (retain relationship, coordinate scope)
