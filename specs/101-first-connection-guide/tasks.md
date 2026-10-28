# Tasks: First-Connection Guide and Configured-Service Verification

**Branch**: `101-first-connection-guide`

## Completed in this contribution

- [x] T001 Guide model module `backend/orchestrator/connection_guide.py`: endpoint
  attestation, per-service checks over real probes, overall state, sign-in guidance,
  authenticated `/api/connection/status` route, registration push helpers.
- [x] T002 Surface adapter `backend/orchestrator/projection_surfaces/connection.py`:
  web modal render, native primitive components, standard `chrome_open` re-check control (no new action, no manifest entry).
- [x] T003 Register the `connection` surface key in the server-owned surface registry.
- [x] T004 Orchestrator wiring: include the connection router; call
  `maybe_push_on_register` after the LLM gate decision, non-fatal.
- [x] T005 Deterministic tests `backend/tests/test_connection_guide.py`: endpoint
  normalization/verified/mismatch/unconfigured; per-check success, edge, denial,
  failure, and recovery; overall-state policy; payload shape and key guidance; REST
  401/200/503 and recovery; surface HTML and components; refresh handler; push on web
  and native with open-surface note and refused-delivery handling; register-time gating
  (LLM gate, active chat, ready, required-service failure, push failure); registration
  integration with the live database.
- [x] T006 Feature spec/plan/tasks (this directory).

## Source repair verification

- [x] Probe exceptions/timeouts preserve each truthful check and cancellation; LETS application readiness matches current `/readyz`, including unknown/failure denial.
- [x] Delivery captures and rechecks the normal current owner registration/context/scope/incarnation across awaits; initial publication, gate/chat/owner/reconnect races and failed send state are covered.
- [x] Web and native/watch use the same approved component composition through pinned Projection rendering/ROTE; no HTML presentation fork, renderer or manifest addition.
- [x] Configured registration integration failures fail tests; only explicit missing PostgreSQL configuration may skip before construction. Owned chat restore precedes automatic guide evaluation.

- [x] Connection-only watch re-check/error/close disposition uses existing captured ordinary request/current owner/incarnation and close fences; stale/unknown authority and profile failure deny, while other watch surfaces retain their existing behavior.

## Required before merge/publication (not performed in this snapshot)

- [ ] T010 Full local qualification suite (controller-run): backend groups, changed-code
  coverage, lint, image build, boot smoke, secret scan, ui-v2 contract profile.
- [ ] T011 Staging exercise of a real first connection: healthy start, then database,
  authentication, and voice-worker failure cases observed through the guide.
- [ ] T012 Live client verification on every affected target (web browser plus native
  clients) of the pushed guide, the re-check action, and the REST resource.
