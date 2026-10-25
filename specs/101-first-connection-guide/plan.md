# Plan: First-Connection Guide and Configured-Service Verification

**Branch**: `101-first-connection-guide`
**Spec**: [spec.md](spec.md)

## Architecture

Server-owned additions only, inside AstralDeep's existing seams:

- `backend/orchestrator/connection_guide.py` — the guide model: endpoint attestation
  (`PUBLIC_BASE_URL`/`BACKEND_PUBLIC_URL` vs the connecting origin), per-service checks
  built on the orchestrator's own real probes (`_probe_plane_readiness`, web_auth
  identity-provider discovery, the voice worker pool readiness, the generated-agent
  publication readiness service, the LETS health snapshot), overall-state computation,
  the authenticated `GET /api/connection/status` route, and the registration-time push
  helper following `llm_gate.py`'s delivery pattern.
- `backend/orchestrator/projection_surfaces/connection.py` — the surface adapter:
  web modal HTML and native primitive components from one payload; the re-check
  control re-opens the surface through the standard `chrome_open` action, so no new
  dispatch action or UI-protocol manifest entry is introduced.
- `backend/orchestrator/projection_surfaces/__init__.py` — registers the `connection`
  surface key in the server-owned registry (ownership guard updated accordingly).
- `backend/orchestrator/orchestrator.py` — includes the connection router and calls
  `connection_guide.maybe_push_on_register` after the LLM gate decision in the
  registration flow (non-fatal).

## Constitution mapping

- II/SDUI: the guide is composed from pinned `astralprims` types already approved in
  Projection's manifest (`alert`, `card`, `text`, `button`); rendering goes through the
  existing `webrender.chrome` helpers and ROTE adaptation. No new primitive, frame,
  chrome, theme, or layout change, so no cross-client drift surface is opened.
- VII/Security: the resource authenticates via the standard bearer/session dependency;
  no secret values are read or echoed; remediation strings name settings and commands,
  never credentials; the identity-provider probe reuses the cached discovery check.
- XI/CI gates: deterministic tests cover success, edge, denial, failure, and recovery;
  changed-code coverage is satisfied by the new module suites plus the two
  registration-flow tests covering the orchestrator call site.
- X/Production readiness: honest failure states with remediation; structured warnings
  on probe failures; no dev-only branches.

## Verification status

- Implemented and locally verified: module suites, REST denial/success/failure/recovery,
  surface rendering, push behavior, registration integration (see tasks.md).
- Required but NOT performed in this snapshot (recorded, not claimed): live staging
  deployment exercise; real browser/native client verification of the pushed modal and
  the re-check action on every affected target; Docker/platform acceptance in PR CI.
