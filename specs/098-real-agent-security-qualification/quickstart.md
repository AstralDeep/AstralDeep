# Validation Guide: Real Agent Security Qualification

Run from the repository root with the production Python 3.11 environment, the pinned component wheels, and a local isolated PostgreSQL service. Required checks use no provider account, Apple/Android device, or live third-party network.

1. Verify composition and start the existing local development services with `make up` and `make sync` when Docker is available. The current Windows host may need Docker Desktop started first. Do not run qualification against a production database.
2. Run the narrow module suite: `cd backend; python -m pytest security_benchmark/tests -q`. A database-required case must fail or run; it must not silently skip while the qualification gate reports success.
3. Run the exact-candidate offline real-dispatch command documented by the implemented CLI. Use a synthetic owner namespace and the pinned local case set. Inspect the JSON report: evidence class, SHA/pins, source and effect receipts, paired benign outcome, and teardown must all be present.
4. Change one fixture permission or gate in an isolated test, rerun, and confirm the effect oracle reports the unauthorized effect and exits nonzero. Restore the gate before broader testing.
5. Request a missing or unsupported external corpus and confirm the command exits nonzero with an exclusion inventory; it must not use sample cases.
6. Run the grouped backend web suite, Ruff, and changed-line coverage. Exercise any affected web-facing behavior against the live local backend. Native Apple/Android tests are intentionally omitted at the user's request.

The historical `in_process` ASR mode remains a diagnostic refusal; use the explicit real-dispatch command below.

## Local implementation evidence, 2026-10-20

From a clean exact candidate checkout with the isolated PostgreSQL 17 test service, run `cd backend; python -m pytest security_benchmark/tests/test_real_dispatch.py -q`, then `python -m security_benchmark --qualify-real-dispatch --out security_benchmark/_artifacts`. The required test is discovered by the existing backend `modules` group. The CLI binds a report to the exact Git commit, policy digest, composition digest, and 12-case fixture digest. An interrupted run writes an incomplete diagnostic report and exits nonzero. Legacy `in_process` and `external` ASR modes now refuse their unimplemented real-turn path; external corpus requests fail visibly instead of substituting sample cases.

The local 12-case fixture drives signed registered turns, a test-signed token exchange through the product delegation request path, read tools, a physical effect ledger, and hash-chain audit checks. Each attack has a separate benign read control. It checks deterministic host enforcement under a scripted proposer; it does not measure model resistance or production Keycloak availability.

On this Windows host, the combined focused backend suite passed **265 tests**, including qualification model/report tests. Web renderer Python tests passed **64 tests**, and Projection tooling web tests passed **11 tests**. The required registered-turn test **failed before execution** at Orchestrator import with `ModuleNotFoundError: fcntl`; Docker Desktop could not start an isolated PostgreSQL service. No Apple or Android tests ran, as requested.

This feature is **partially implemented and unqualified**. AgentDojo/ASB/InjecAgent real-corpus mapping is not yet available; requests fail closed. The required local cohort lacks a distinct delegated and delayed-action case, no weakened-gate effect regression or held-out adaptive study has run, and no grouped Linux/changed-line coverage or live web dispatch result exists. Do not treat the fixture report or synthetic ASR as release evidence until those gaps and the required gates are closed.
