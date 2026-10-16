# Tasks: Cresco Target-Bound Work Dispatch

**Feature**: 096-cresco-target-dispatch | **Spec**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md)

- [x] T001 Target grammar and approved-target admission (`targets.py`), default-deny.
- [x] T002 wsapi client: pinned envelope, canonical bulk params, verified TLS
      (system/CA/fingerprint), egress validation with host-scoped private opt-in,
      bounded timeouts, closed fault set, bounded reconnect.
- [x] T003 Read tools (`cresco_list_regions`, `cresco_list_agents`, `cresco_agent_info`).
- [x] T004 `cresco_dispatch_work` effect tool: admission at effect time, correlation id,
      bounded result window, unconfirmed-on-missing/late/malformed result, audited.
- [x] T005 `cresco_run_command` executor wrapper: system scope, hard security flag,
      default-deny, operator env switch + explicit per-user override in-handler,
      denial audited.
- [x] T006 MCP server: flag/config gates, owner binding, secret scrubbing, fault
      mapping, astralprims rendering, fabric-identifier audit records.
- [x] T007 Registration, safe-seed exclusion, confirmation policy, taint posture,
      public error codes, start.py subprocess guard.
- [x] T088 Deterministic tests: grammar/admission; client against staged in-process
      multi-node fabric (plain + TLS); dispatch success/denial/stale/malformed/late/
      connection-failure/recovery; audit; secret scrubbing; executor denial; flag-off
      no-op; registration posture.
- [x] T009 Focused regressions: cresco module suite, orchestrator seam suite,
      ui-v2-contracts profile, touched-seam suites, ruff.
