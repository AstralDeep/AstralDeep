# Data model

No durable entities or schema changes.

- Input: existing terminal projection's state, accepted_at, started_at and terminal_at. All present timestamps must be datetime values with non-null UTC offset. Normalize to UTC before requiring accepted <= started <= terminal, or accepted <= terminal when never started, so daylight-saving folds measure elapsed time correctly. Invalid timezone/conversion/arithmetic values are timing omissions.
- Valid terminal outcome: completed, failed, cancelled, retryable. No raw terminal code is retained.
- Aggregate key: metric name plus fixed deployment label, phase, outcome and (bucket samples only) latency_bucket. Values are finite non-negative numbers.
- Omission counter: fixed reason missing_timestamp, invalid_timestamp, invalid_order or invalid_state. It retains no offending value.
- Lifecycle: manager's existing terminal-observation flag prevents repeat recording for that local task; collector state resets on process restart. No cross-process exactly-once or billing claim.
