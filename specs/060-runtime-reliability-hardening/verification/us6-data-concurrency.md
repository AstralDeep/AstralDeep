# US6 Data and Concurrency Verification

**Feature**: 060 Runtime Reliability and Release Readiness
**Branch**: `060-runtime-reliability-hardening`
**Executed**: 2026-07-16 (America/New_York)
**Environment**: Python 3.11 in `astraldeep:latest`, with the current checkout
bind-mounted over `/app`; PostgreSQL 17 on the local `astraldeep_default`
network. No credentials, user payloads, generated code, or owner identities are
included here.

## Consolidated implementation gate

The final US6 gate covered immutable artifact publication, authoring CAS and
generation claims, agentic creation, bounded work lanes, BYO authoring and
lifecycle regressions, owner-scoped draft archives, durable maintenance,
guarded migrations and policy markers, the immutable runtime registry and its
production wiring, startup reporting, knowledge guards, and the full
release-scale performance module.

```text
docker run --rm --env-file .env \
  --env FF_BG_CONTINUITY=true \
  --env PERSONAL_AGENT_ARTIFACT_ROOT=/tmp/personal-agent-artifacts \
  --env DB_HOST=postgres --env DB_PORT=5432 \
  --network astraldeep_default -v "$PWD:/app" -w /app/backend \
  astraldeep:latest python -m pytest \
  tests/test_agent_artifact_publication_060.py \
  tests/test_agent_authoring_concurrency_060.py \
  tests/test_agentic_creation.py tests/test_bounded_work_060.py \
  tests/test_byo_authoring.py tests/test_byo_authoring_flow.py \
  tests/test_byo_authoring_surface.py tests/test_byo_lifecycle.py \
  tests/test_byo_offserver.py \
  tests/test_byo_orchestrator_runtime_wiring_060.py \
  tests/test_draft_archive.py tests/test_draft_archive_wiring.py \
  tests/test_maintenance_claims_060.py \
  tests/test_runtime_registry.py \
  tests/test_schema_revision_guard.py tests/test_start_wait.py \
  orchestrator/tests/test_knowledge_guard.py \
  tests/perf/test_runtime_reliability_060.py -q -s
```

Feature 074 moved the schema-owner proofs to AstralPlane. Run the pinned
component's `tests/test_schema_migrations.py` and
`tests/integration/test_empty_database_startup.py`, including
`test_fifty_two_starter_migration_trials_converge_once`, alongside this Deep
policy/concurrency group.

Result: **282 passed in 8.65 seconds**.

## Authoring identity, CAS, publication, and deletion

| Profile | Attempts | Result |
|---|---:|---|
| Same-name draft creation | 100 | 100 distinct UUID4 draft identities, target identities, and storage slugs |
| Same-revision CAS | 100 | 1 applied, 99 conflict-with-refresh; maximum response 11.934 ms |
| Generation claim | 100 | 1 current claim, 99 fenced losers |
| Delete/register interleaving | 100 | 100 durable tombstones, 0 resurrected agents |
| Same-revision artifact publication | 32 | One immutable revision directory and one stable bundle digest |

A separately synchronized test also proved that a late Analyze result cannot
overwrite a newer owner edit. The owner-scoped exemplar archive now keys an
entry by draft UUID and source revision, returns an exact replay idempotently,
refuses different bytes under the same key, and returns no exemplar to another
owner.

Artifact fault injection exercised all 11 publication boundaries: before
staging; after staging-directory creation; after each of the four file writes;
after the staging-directory fsync; after validation; before atomic replace;
after replace; and after revision-directory fsync. Every replay exposed the
same validated immutable revision. A stale operation/draft fence published
nothing, and a conflicting attempt to reuse a revision identity with different
bytes was refused.

## Guarded migration and policy ownership

The two-replica profile forced both schema and independent user-agent-policy
markers stale for each round, then released two starters concurrently.

| Trials | Starters | Schema owners | Policy owners | Duration |
|---:|---:|---:|---:|---:|
| 50 | 100 | 50 | 50 | 2.496 s |

Every round converged on schema revision `060.004` and the exact combined
policy marker `constitution=0.1.0;analyze=1`. In each round exactly one starter
reported the changed marker and two non-deleted representative agents marked
for revalidation; the other reported the committed fast path and zero newly
marked agents. The deleted representative agent remained excluded.

The same gate includes a dedicated killed-owner trial: PostgreSQL terminated
the advisory-lock owner after its migration work but before commit, its
transaction rolled back, the waiting starter acquired the fixed lock and
reapplied, and the marker converged. A policy-only trial proved the schema
migration does not run, the policy lock is independent, and the next boot
reports a non-sensitive fast-path outcome. The source hash guard remained
green at revision `060.004`; startup reporting did not create a second schema
or policy implementation.

## Maintenance completion truth

The maintenance matrix proved:

- failed per-agent synthesis leaves only that unit and its memberships
  retryable/pending, while a successful agent's source interactions complete;
- retry retains the same durable unit and output-generation identities and
  advances only the claim/attempt generations;
- a crash after atomic replace reconciles the already-published bytes and
  completes under a fresh database-time lease, without republishing;
- a fault before replace leaves the prior complete output authoritative; and
- a real synthesis cycle claims and commits the per-agent technique,
  per-agent capability, and cross-agent outputs before indexing them.

Generation and maintenance use separate finite executor lanes. Saturation is
an explicit refusal, context is preserved into a worker, and blocking file,
database, model, and process-adjacent work does not occupy the event loop.

## Registry stability and release-load latency

Four concurrent writers published exactly 2,500 revisions each while two
lock-free readers continuously checked immutable, ordered, kind-correct
snapshots.

| Publications | Reader observations | Final registry version | Duration | Partial/stale views |
|---:|---:|---:|---:|---:|
| 10,000 | 120,448 | 10,000 | 0.319 s | 0 |

The latency profile kept both bounded maintenance workers occupied and eight
supervised Python child processes alive while measuring 100 unrelated event-
loop acknowledgements. Cleanup then verified every child tree terminated and
all captured pipes and readers closed.

| Acknowledgements | Within 2 s | p95 | Maximum | Required |
|---:|---:|---:|---:|---|
| 100 | 100 | 0.066 ms | 0.067 ms | p95 <= 2 s; max <= 5 s |

## Scope of this evidence

This is deterministic local candidate-image and PostgreSQL integration
evidence from a dirty feature checkout. It is not a clean-checkout attestation,
signed candidate, multi-host staging result, deployment, store submission, or
production-distribution claim. Those release-level states remain governed by
the feature's separate protected evidence workflow.
