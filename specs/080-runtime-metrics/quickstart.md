# Local verification and operator interpretation

Use Python 3.11 with the repository's existing qualified dependencies. Disable dotenv before importing pytest or application modules; use synthetic principals/data and an isolated runtime. Do not run ordinary Compose against retained production-like data for these tests.

Run the feature's collector, access and background integration tests plus existing operation API, operation observability, voice observability, async-task and MCP endpoint regressions. The local candidate passed 269 tests, root Ruff and the backend-only changed-line diagnostic at 88/88 executable changed lines (100%). Exact commands, image/source identities, review results and qualification limits are recorded in [verification.md](verification.md). This is not the full strict three-producer release gate.

After deploying a qualified candidate, operators use the existing metrics route with their normal verified admin identity. A normal user's diagnostic request returns 403 before profile persistence or metrics collection; an admin role still requires a valid user identity. Do not put tokens into shell arguments, examples or logs. Existing user-owned operation reconciliation retains its prior permissions. This local implementation has not changed the running server.

Interpret queue_wait versus execution to locate admission delay versus running time; end_to_end includes both. Compare bucket-count deltas across two snapshots of the same process to estimate an interval distribution. Buckets bound quantiles; they do not provide exact individual durations. After a process restart counters reset and interval baselines must restart. Never combine samples from different instances without an explicit aggregation policy.

Observe omission counts alongside latency to detect incomplete timing evidence. Never-started cancellations/expirations have queue/end-to-end samples and no execution sample. This measures background operations observed by the manager, not every model call, first pixel, user session or complete durable population. It is not billing, audit or PhD statistical evidence by itself.

No local test result authorizes deployment. Before promotion, verify real configured IAM denials/admin success and representative background operations on the exact candidate, plus applicable full merge gates. Reverting to the previous artifact resets the new aggregates and restores the previous broader metrics-access policy.
