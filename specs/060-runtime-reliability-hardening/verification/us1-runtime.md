# US1 Runtime Reliability Verification

**Feature**: 060 Runtime Reliability and Release Readiness

**Branch**: `060-runtime-reliability-hardening`

**Executed**: 2026-07-16 (America/New_York)

**Environment**: Python 3.11 in `astraldeep:latest`; PostgreSQL 17 on the local
`astraldeep_default` network. Secrets and payloads are not included here.

## Connection saturation and drain

Command:

```text
python -m pytest tests/perf/test_runtime_reliability_060.py -q --tb=short
```

Result: **39 passed in 1.19s**.

The 1,000-frame trial ran independently through both the legacy and FastAPI WebSocket entry points.
For each trial, the configured interactive ceiling was 20 active plus 100 queued:

| Entry point | Frames | Accepted | Capacity-refused | Peak active | Accepted terminals | Post-drain tracked work |
|---|---:|---:|---:|---:|---:|---:|
| legacy | 1,000 | 120 | 880 | 20 | 120 exactly once | 0 |
| FastAPI | 1,000 | 120 | 880 | 20 | 120 exactly once | 0 |

The same suite passed registration timeout/flood, duplicate retry, FIFO mutation, non-overlapping
live read/mutation generations, cancellation-control bypass, stale execution lease, disconnect
ownership, and five-second-bounded drain contracts.

## T129 preregistration-refusal correlation regression

The completed T129 convergence guard received a focused backend rerun of **54 passing tests**. It
proves that every preregistration frame retains its canonical `submission_id`, every queued or
overflow-triggering submission receives one exact seven-field admission-refusal envelope before
the connection closes, and no refusal correlated to a client submission can carry a null
submission ID. The authoritative protocol manifest and each shipping client's strict decoder or
reducer are covered by the corresponding drift tests.

The focused local client reruns that include this exact-envelope/correlation regression completed
as follows:

| Client lane | Result |
|---|---:|
| Web Playwright contracts | 15 passed |
| Windows protocol/status/accessibility contracts | 20 passed |
| Android protocol/status/IME contracts | 27 passed |
| AstralCore protocol and reducer suite | 146 passed |
| iOS status contracts | 8 passed |
| Watch status contracts | 5 passed |

These are local contract regressions for T129, not live continuity evidence for T057 and not a
protected candidate qualification.

## Background admission, retention, and shutdown

The combined Python 3.11 gate for background tasks, runtime observability, and production wiring
completed with **115 passed in 3.71s**. The ceiling remained five active tasks, queued work retained
full UUID identities, terminal operations remained queryable for 24 hours and were purge-eligible by
25 hours, and shutdown stayed deadline-bounded under stalled submit/query/claim calls. A
cancellation-resistant coroutine is reported as an explicit fenced remainder after its durable
operation and captured-output authority are revoked.

## Scheduled occurrence/effect trial

The deterministic PostgreSQL interleaving trial alternated two store instances for 10,000
reservation/replay operations against one durable occurrence and observed exactly one visible
effect:

| Interleavings | Occurrence identities | Effect-ledger rows | Published effects | Duplicate visible effects |
|---:|---:|---:|---:|---:|
| 10,000 | 1 | 1 (`published`) | 1 | 0 |

The integrated scheduler gate also covers repeated/two-instance polling, cadence advancement,
run-now replay without cadence mutation, pause/delete versus claim/start, claim expiry/recovery,
separate occurrence and operation lease renewal, stale-fence refusal, attempt-scoped operations,
atomic chat/effect publication, and crash-boundary replay. The final exact integrated command and
duration are:

```text
python -m pytest \
  tests/test_schema_revision_guard.py \
  scheduler/tests/test_occurrence_claims_060.py \
  scheduler/tests/test_schedule_actions_060.py \
  scheduler/tests/test_atomic_chat_publication_060.py \
  scheduler/tests/test_schedule_api_060.py \
  scheduler/tests/test_handler_eligibility_060.py \
  tests/chrome/test_surface_personalization.py \
  tests/test_operation_observability.py \
  tests/test_work_admission_repository.py \
  -q --tb=short
```

The feature-060 migration proof was superseded by the extracted AstralPlane
suite. Run the pinned component's `tests/test_schema_migrations.py` and live
`tests/integration/test_empty_database_startup.py` in addition to the Deep
runtime-policy tests above.

Result: **184 passed in 80.95s** (81.55s container wall time). The gate used isolated throwaway
databases created and dropped by the tests. It also proves a cancellation-resistant scheduled
handler becomes an explicitly tracked, output-fenced remainder instead of holding dispatch or
shutdown unbounded, and a lease lost at the final commit is retryable authority loss rather than a
misreported handler failure.

## Final local regression checkpoint (pre-protected)

- The final backend default-suite rerun completed cleanly with **4,911 passed, 2 skipped, and 2
  deselected**.
- The explicit backend module suites completed with **725 passed and 1 warning**.
- The concurrent-surface performance case completed with **1 passed in 0.58 seconds**.

"Completed cleanly" describes the test result against the current local tree; it is not a
clean-checkout, signed-candidate, protected-workflow, staging, or distribution claim. The live
T057/T102 exercises and the T121–T128 protected qualification sequence remain open.

## Verdict

US1 satisfies SC-001 and SC-002: connection and background admission are finite and drain-bounded;
scheduled occurrences retain one durable identity across retry, both execution authorities are
renewed/fenced, Run-now is owner-scoped and idempotent, pause/delete is linearizable against start,
and the 10,000-operation trial publishes one visible effect with no duplicate. This verdict is
limited to the local implementation and deterministic evidence recorded above; it does not close
the outstanding live or protected release-qualification tasks.
