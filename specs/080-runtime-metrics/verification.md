# Feature 080 verification

## Baseline

2026-09-06: isolated immutable published main image `ghcr.io/astraldeep/astraldeep@sha256:aaedbcfef877e348f9de0fe07a427f02eedc5bd958dcb0c105a8b5f2d94e83f7`, OCI revision `c1c70f83ec8ee53d78e5a74bc5e1b6c766ba482a`, Python 3.11.16. Normal entrypoint disabled, no production network or retained volumes, dotenv disabled before imports. Baked-main operation observability, voice observability, operation API and async-task suites: 142 passed in 4.21s with one upstream Starlette deprecation warning. Full command/output retained in the external implementation-validation directory, to be summarized with candidate results below.

## Design gates

Specification checklist passes with no incomplete items or critical clarification questions. New target is specs/080-runtime-metrics on codex/080-runtime-metrics; merged feature079 is untouched. Optional Spec Kit commit hooks are configured disabled. Planning introduces no dependency, schema, UI contract or infrastructure topology change.

Read-only cross-artifact analysis: ten functional requirements and five success criteria mapped to twelve tasks; 100% requirement coverage, zero unmapped tasks, zero critical/high findings or unresolved ambiguities. The task order places tests before implementation; US1 and US2 remain independently testable. Existing ignore rules cover Python caches, virtual environments, generated data, secrets and coverage artifacts; no ignore change is needed. Pre-implementation checklist and prerequisite paths pass for the exact 080 directory.

## Implemented behavior and review

Runtime implementation is anchored at local commit `4f9b4b891727d177f463df7252c06f6f79d39453`; the final feature commit also contains a stronger overflow test and these evidence notes. Only three runtime files changed:

- `backend/orchestrator/api.py`: existing verified-admin dependency runs before profile persistence, orchestrator lookup and metrics collection. Identity remains required; owner reconciliation is unchanged. OpenAPI documents the intentional access restriction.
- `backend/orchestrator/runtime_observability.py`: fixed background queue/execution/end-to-end bucket/count/sum aggregates, bounded omission signals, UTC elapsed-time normalization, fixed bucket vocabulary and atomic finite updates.
- `backend/orchestrator/async_tasks.py`: one observation after the existing local terminal guard; collector lookup/call exceptions produce fixed diagnostic text without exception details and cannot interrupt normal lifecycle handling.

Three new test modules contain 75 cases; the existing operation API metrics-success fixture alone gained a synthetic admin principal. Claude wrote the initial tests and runtime implementation; parent review and an independent agent strengthened the tests and corrected eight initial failures involving arbitrary bucket labels and raising timezone objects. Review additionally found and resolved same-zone daylight-saving fold arithmetic and collector attribute-lookup containment. Final review found no critical unresolved implementation issue. Its remaining late-phase overflow test gap was then closed: rejection in each of the three phases preserves the entire prior snapshot.

## Test evidence

The original three new modules were first run as test-only overlays on immutable main: 38 expected failures and 5 passes, with no fixture errors or hanging cleanup. Failures established the missing admin gate, collector helper and lifecycle calls. Strengthened collector and access/integration cases were also checked against main before implementation qualification; artifacts retain those separate RED runs.

Final candidate selection: **269 passed, one existing Starlette/AnyIO deprecation warning, 6.64 seconds**. No skips, expected failures or thread warnings. It includes the 75 new cases and 194 existing operation, voice, background-task and MCP endpoint regression cases. Source hashes for all seven overlays matched before and after execution.

```powershell
& 'Y:/WORK/AstralDeep-growth-study-2026-09-05/implementation-validation/run-candidate080.ps1' -Prefix 'candidate080-qualified'
```

That local harness invokes pytest under coverage with this exact selection:

```text
python -m pytest -p no:cacheprovider -q
  tests/test_runtime_metrics_access_080.py
  tests/test_background_latency_080.py
  tests/test_background_latency_integration_080.py
  tests/test_operation_observability.py
  tests/test_voice_observability_065.py
  tests/test_operation_api_060.py
  tests/test_async_tasks.py
  tests/test_mcp_endpoint_064.py
```

The harness individually mounts only the three changed runtime files, the adapted existing test and the three new tests over the pinned main image; source tests are not a rebuilt product image. Docker uses `--rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges`, temporary `/tmp`, an overridden Python entrypoint and `ASTRAL_ENV=development`. Dotenv is disabled before any application import. No repository `.env`, retained volume, user data or production network was exposed. Python 3.11.16 and existing installed Plane/Projection/Primitives distributions match the baseline. Coverage 7.15.2 and Ruff 0.15.21 were verified against existing CI locks and installed only in the external validation directory.

Root lint passed on both immutable main and the final candidate:

```powershell
& 'Y:/WORK/AstralDeep-growth-study-2026-09-05/implementation-validation/host-ruff/ruff-0.15.21.data/scripts/ruff.exe' check --no-cache .
git diff --check
```

## Changed-line coverage

The backend-only diagnostic passes **88/88 executable changed Python lines, 100%**, above the 90% requirement. All three changed runtime paths are mapped. The new API dependency lies within an existing multi-line decorator/import statement; its enforcement is independently exercised through actual FastAPI dependencies, including no bearer, invalid principal, non-admin, missing subject, admin success and owner isolation. No `verify_admin` override is used.

```powershell
$candidateSha = git rev-parse HEAD
& 'Y:/WORK/MCP/AstralDeep/.venv/Scripts/python.exe' -B scripts/check_changed_coverage.py `
  --repo 'Y:/WORK/MCP/AstralDeep' --event-name manual `
  --base-sha c1c70f83ec8ee53d78e5a74bc5e1b6c766ba482a `
  --candidate-sha $candidateSha --repository-profile deep `
  --coverage-mode partial --fail-under 90 `
  --backend-python 'Y:/WORK/AstralDeep-growth-study-2026-09-05/implementation-validation/candidate080-qualified.xml' `
  --output 'Y:/WORK/AstralDeep-growth-study-2026-09-05/implementation-validation/changed-coverage080-final-partial.json'
```

The final decision records the exact committed candidate SHA and native report identity. Raw logs, JUnit, XML/JSON coverage, stable overlay hashes and the complete runner command remain under the external `implementation-validation` directory. They are local evidence, not remotely persisted CI artifacts.

## Scope and remaining qualification

No dependency, schema/migration, feature flag, component pin, UI primitive, renderer, frame, client contract, IAM provider, hosting topology or running service changed. No product push, merge, release or deployment occurred. The local feature branch is `codex/080-runtime-metrics`; the vault separately records the checkpoint.

This result does not establish full release readiness. The strict Deep coverage gate requires distinct useful backend, voice-worker and tooling producer reports; only the backend producer was run for this local diagnostic. Applicable full backend/module/image/boot/security gates and exact-candidate verification through real configured IAM and representative background operations remain before promotion. No live server or native-client verification was performed. Existing metrics and owner-scoped APIs are regression-tested locally; this does not prove capacity at any MAU milestone or clinical compliance.

Rollback requires no data migration. Returning to the prior application artifact drops the new process-local aggregates and restores its broader authenticated-user diagnostics policy; it therefore reverses the access improvement as well as telemetry.
