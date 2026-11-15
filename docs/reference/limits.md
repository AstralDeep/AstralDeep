# Limits

| Limit | Value | Set by |
|---|---|---|
| Tool output returned by the executor | 20000 characters by default, 1000 to 200000 | `tool_output_chars` in the agent's `[sandbox]` |
| Tool call time | 60 seconds by default, 1 to 600 | `tool_timeout_seconds` |
| Turn deadline | 600 seconds by default, 5 to 3600 | `deadline_seconds` |
| Files a call may return | 4 per call, types listed in `[output] files` | The manifest |
| Disk per turn and per user | A quarter and a half of `scratch` | `turn_disk` and `user_disk` |
| Open files in a call | 4096 | The sandbox |
| Tickets remembered against replay | 65536 live ids, each kept 3600 seconds, in a file | `VESSEL_REPLAY_RETENTION_SECONDS` |
| Pairing offer life | 10 minutes | The node |
