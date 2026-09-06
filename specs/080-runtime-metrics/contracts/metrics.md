# Operational metrics contract

`GET /api/runtime-reliability/metrics` retains `{ "metrics": [{ "name": ..., "value": ..., "labels": ... }] }` and successful `Cache-Control: no-store`. It now requires both an authenticated user identity and the existing verified Keycloak admin role. Invalid/absent authentication follows existing IAM errors; authenticated non-admin users receive 403 before any snapshot/admission query. Ordinary owner-scoped operation endpoints remain unchanged.

New cumulative metric family:

- `background_operation_latency_seconds_bucket`: fixed cumulative upper-bound counts.
- `background_operation_latency_seconds_count`: observation count.
- `background_operation_latency_seconds_sum`: total observed seconds.

Labels are `deployment_instance`, `phase` (queue_wait, execution, end_to_end), `result_code` (completed, failed, cancelled, retryable). Bucket samples additionally carry `latency_bucket` from this exact vocabulary:

| Token | Upper bound in seconds |
|---|---:|
| le_0_05 | 0.05 |
| le_0_1 | 0.1 |
| le_0_25 | 0.25 |
| le_0_5 | 0.5 |
| le_1 | 1 |
| le_2_5 | 2.5 |
| le_5 | 5 |
| le_10 | 10 |
| le_30 | 30 |
| le_60 | 60 |
| le_300 | 300 |
| le_900 | 900 |
| le_3600 | 3600 |
| le_inf | Unbounded final bucket |

For started work: queue_wait=started-accepted, execution=terminal-started, end_to_end=terminal-accepted. Never-started terminal work uses terminal-accepted for queue_wait/end_to_end and emits no execution sample. Missing accepted/terminal timestamps omit the entire observation. Invalid timestamps/order/state also omit it. Increment `background_operation_latency_skipped_total` with deployment_instance and one fixed result_code reason: missing_timestamp, invalid_timestamp, invalid_order, invalid_state. No offending data is emitted.

Normalize aware timestamps to UTC before ordering and subtraction; repeated local-clock hours during daylight-saving transitions do not reduce elapsed time. Malformed timezone/conversion/arithmetic values produce invalid_timestamp omissions without exception text.

One full observation updates every affected phase atomically. All output values remain finite and non-negative. A zero observation increments all cumulative buckets/count and leaves sum unchanged. The final bucket equals count. A missing series means unobserved, not zero measured time. Counts are process lifetime, and retention_seconds elsewhere describes operation retention, not a TTL applied to these counters.

The family has at most 3 phases x 4 outcomes x (14 buckets + count + sum) = 192 series, plus four omission series, for one deployment_instance. No per-task key or sample list is retained. This JSON vocabulary is not a native Prometheus exposition format; a future adapter must map the fixed tokens to numeric `le` bounds rather than silently treating them as arbitrary labels.
