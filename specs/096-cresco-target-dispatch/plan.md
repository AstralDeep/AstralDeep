# Plan: Cresco Target-Bound Work Dispatch

**Feature**: 096-cresco-target-dispatch | **Spec**: [spec.md](spec.md)

## Architecture

The bridge is a bundled first-party agent following the feature-040 in-process pattern,
modeled on the merged GaiaKeep agent's governance shape:

```
backend/agents/cresco/
├── __init__.py        package header
├── targets.py         target grammar + approved-target admission (CRESCO_APPROVED_TARGETS)
├── wsapi_client.py    wsapi profile: envelope, bulk params, TLS, egress, faults, timeouts
├── mcp_tools.py       tool handlers + TOOL_REGISTRY (scope per tool)
├── mcp_server.py      MCP dispatch, owner binding, admission, audit, presentation
├── cresco_agent.py    CrescoAgent(BaseA2AAgent), agent_id "cresco-1"
└── tests/             deterministic suites incl. an in-process staged fabric
```

Orchestrator-side seams: `local_agents.py` (flag-gated registration + public id),
`orchestrator.py` (owner-context binding, confirmation/MCP-channel tuples, safe-seed
exclusion), `remote_confirmation.py` (approval-card policy), `taint.py` (untrusted source,
effect sinks), `tool_feedback.py` (public fault messages), `start.py` (subprocess guard).

## Key decisions

- **Sync wsapi client**: `websockets.sync` matches the MCP server's `to_thread`
  execution model; no event-loop juggling; per-operation bounded connection with
  stdlib-only reconnect/backoff between attempts.
- **Unconfirmed-not-success**: an effect whose result is missing/late/malformed is
  reported `unconfirmed` with the work id retained (GaiaKeep reconciliation posture).
- **Executor wrapper**: hard-blocked by the registration security analyzer; its handler
  additionally demands an operator env switch and an explicit per-user override, so no
  single gate flip can expose it.
- **Wire profile**: pinned client-side envelope
  (`message_info`/`message_payload`, canonical `base64(gzip(json_bytes))` bulk params,
  `compresslevel=9, mtime=0`); any drift fails safe into `protocol`. Upstream
  byte-level re-verification remains an operator precondition before pointing the
  bridge at a real fabric.

## Verification

- Module suite `backend/agents/cresco/tests/` (its own CI modules-group suite):
  target grammar/admission; wsapi client against an in-process staged fabric
  (`websockets.sync.server` on loopback, TLS via in-test self-signed CA); dispatch
  success/denial/revocation/malformed/late/connection-failure/recovery; audit rows;
  secret scrubbing; security posture.
- Orchestrator seam suite `backend/tests/test_cresco_dispatch_096.py`: registration
  gating, safe-seed exclusion, security-flag hard block through the gate stack,
  confirmation classification, taint posture, public error codes.
- Focused regression: ui-v2-contracts profile, `backend/tests` group suites touching
  local_agents/taint/tool_feedback/remote_confirmation, ruff.
