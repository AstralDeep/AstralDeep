# Implementation Plan: Operational metrics access and background latency

**Branch**: `codex/080-runtime-metrics` | **Date**: 2026-09-06 | **Spec**: [spec.md](spec.md)

## Summary

Restrict the existing deployment-wide metrics route with the established verified-admin dependency. Add atomic fixed-bucket background latency aggregates to RuntimeObservability and record them at BackgroundTaskManager's existing once-per-task terminal-observation seam. Preserve owner-scoped reconciliation, lifecycle authority and all existing metric names.

## Technical Context

- Language: Python 3.11, existing FastAPI/pytest/coverage/Ruff and standard library.
- Storage: process-local fixed aggregate series only; existing Plane timestamps supply observations. No SQL/schema/pin change.
- Platform: current single Linux server; local isolated Python 3.11 container verification.
- Scope: api.py, runtime_observability.py, async_tasks.py and focused tests. No UI/client/protocol or new runtime dependency.
- Performance: constant-bounded work per observation, no I/O, no per-operation retained collection; at most 196 new series for one deployment label.
- Semantics: background-only observed terminal events, reset on process restart, not a durable exactly-once ledger or complete latency population.

## Constitution Check

Pre-research and post-design review pass: Python 3.11 retained (I); no UI change (II/VIII/XII); meaningful golden/denial/failure tests and >=90% changed Python coverage required (III); root Ruff configuration retained (IV); no dependencies added (V); public helper/API docs and operator contract provided (VI); existing Keycloak admin gate and closed content-free metric vocabulary (VII); no database change (IX). No release-evidence, workflow, image definition, topology or new background worker changes are proposed. Existing manager callbacks gain in-memory observations only.

Local tests do not establish deployed behavior. Before merge, run the ordinary applicable full backend/module, image/boot and security gates under X/XI and exercise the metrics endpoint through real configured IAM on a candidate deployment. No staging or release claim, exemption, protected exception, candidate push or release publication is created here. The initial user request authorizes implementation; it does not authorize production access or product publication. This feature does not alter the canonical release-evidence producers or their trust model.

## Design

1. Add `Depends(verify_admin)` to only `/api/runtime-reliability/metrics`, retaining existing authentication and successful `Cache-Control: no-store`. Update endpoint/OpenAPI descriptions. Verify denials precede `_get_orchestrator`, admission inspection and snapshot calls.
2. Add `RuntimeObservability.observe_background_operation(operation)` accepting an existing safe projection or duck-typed equivalent in tests. It reads only state and accepted/started/terminal timestamps, never identifiers, task kind, raw terminal code or payload.
3. Emit `background_operation_latency_seconds_bucket`, `_count` and `_sum`, labelled by deployment_instance, phase and coarse result_code; bucket samples add `latency_bucket`. Closed phase set: queue_wait, execution, end_to_end. Outcomes: completed, failed, cancelled, retryable. Buckets are defined in contracts/metrics.md.
4. Missing required timestamps, invalid types/timezones, invalid ordering or invalid state produce only a fixed omission counter. Never-started terminal work records waiting and end_to_end but no execution. Zero duration is valid. Validate all increments before mutation and update the entire observation under the collector lock, so no snapshot sees half an observation. Reject arithmetic overflow without partial writes.
5. Call the helper in `_observe_terminal` after its existing `_terminal_observed` guard. Catch telemetry exceptions with a fixed content-free diagnostic; do not alter counters, admission refresh, terminality or cleanup. No collection in per-subscriber delivery loops and no per-operation memory index.
6. Preserve existing MCP latest-duration gauge and all other metrics. This feature does not claim MCP/client/first-render latency coverage.

## Project Structure

Feature documents: spec.md, plan.md, research.md, data-model.md, contracts/metrics.md, quickstart.md, tasks.md and verification.md.

Production owners: backend/orchestrator/api.py; backend/orchestrator/runtime_observability.py; backend/orchestrator/async_tasks.py.

Test owners: focused new collector/access/background integration tests under backend/tests/, plus minimal adaptation of test_operation_api_060.py's admin-success fixture.

## Verification and Recovery

First establish the relevant unchanged-main baseline in the exact published Python 3.11 image, with its normal entrypoint disabled and no production network/data. Run candidate source on the same dependencies; disable dotenv before imports and use synthetic test principals/data. Run collector boundaries/concurrency, real FastAPI role dependencies, BackgroundTaskManager lifecycle/deduplication/failure tests, existing operation/voice/MCP observability and background lifecycle regressions. Measure changed lines across all touched Python files; retain output outside the repository and record exact commands/results in verification.md.

Rollback is the prior application artifact; no migration/data reversal. Restoring the prior artifact restores its prior diagnostic access policy and drops in-memory latency aggregates. Documentation must make that security implication explicit. Do not treat a local rollback/test container as production deployment.

## Complexity Tracking

No constitution exception or new service is required. Real-IAM candidate verification and broader merge gates remain separately tracked from local implementation.
