# Validation guide

Use Python 3.11, declared dependencies, exact initialized components and an isolated feature checkout. Both flags default off. Enable capture only with explicit exact-scope host retention grants.

1. Focused tests: `python -m pytest backend/tests/test_evidence_archive.py backend/tests/test_safe_compaction.py backend/tests/test_evidence_context.py backend/tests/test_context_usage.py backend/tests/test_context_presentation.py backend/tests/test_context_evaluation.py -q`.
2. Existing compaction/context/ordinary dispatch/audit suites, root `ruff check .`, changed-line coverage >=90%, complete backend/module CI groups with isolated real PostgreSQL.
3. All four new control combinations plus legacy controls: off creates no capture/new auxiliary work; packing and compaction stay independent.
4. Synthetic exact reconstruction including empty/Unicode/limits; owner/audience/scope/policy/integrity denials, deletion/revocation/expiry/restart; no repeated source operation or durable raw-page copy.
5. Inject all compaction failure/cancellation/stale/reduction/provider/context-limit cases; history and protected host state remain unchanged.
6. Reconcile known/unknown retry/cache/failure/cancelled/late/recall records once, with no evidence in diagnostics. Retain paired fixed-workflow failures and unknowns.
7. Build the exact candidate image and exercise real configured IAM/database/workers and representative conversations. Verify labels/source capability on web, Windows, Android, macOS, iOS and watchOS, including explicit supported handoff. User performs required sign-in.
8. Record exact candidate/test/live/formative/provider evidence in verification.md. Missing merge gates keep PR draft; missing promotion evidence keeps defaults off.
