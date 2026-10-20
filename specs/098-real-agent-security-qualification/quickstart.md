# Validation Guide: Real Agent Security Qualification

Run from the repository root with the production Python 3.11 environment, the pinned component wheels, and a local isolated PostgreSQL service. Required checks use no provider account, Apple/Android device, or live third-party network.

1. Verify composition and start the existing local development services with `make up` and `make sync` when Docker is available. The current Windows host may need Docker Desktop started first. Do not run qualification against a production database.
2. Run the narrow module suite: `cd backend; python -m pytest security_benchmark/tests -q`. A database-required case must fail or run; it must not silently skip while the qualification gate reports success.
3. Run the exact-candidate offline real-dispatch command documented by the implemented CLI. Use a synthetic owner namespace and the pinned local case set. Inspect the JSON report: evidence class, SHA/pins, source and effect receipts, paired benign outcome, and teardown must all be present.
4. Change one fixture permission or gate in an isolated test, rerun, and confirm the effect oracle reports the unauthorized effect and exits nonzero. Restore the gate before broader testing.
5. Request a missing or unsupported external corpus and confirm the command exits nonzero with an exclusion inventory; it must not use sample cases.
6. Run the grouped backend web suite, Ruff, and changed-line coverage. Exercise any affected web-facing behavior against the live local backend. Native Apple/Android tests are intentionally omitted at the user's request.

The exact CLI command and run identity are filled in after implementation; this guide does not claim that the current `in_process` mode works.
