# Contract: local command execution on the Mac host

Three new verbs on the computer-use-1 host channel. Advertised only by the DirectDownload macOS host when computer access is enabled and at least one workspace folder is selected. Deep exposes them as tools on the computer-use agent. No new frame type.

## Verbs

### `run_command`

Request

```json
{
  "command": "wc -l notes.md",
  "argv": ["wc", "-l", "notes.md"],
  "cwd": "/Users/sam/Projects/x",
  "env": {"FOO": "bar"},
  "timeout_s": 60,
  "background": false,
  "stdin": "optional text, at most 16 KiB"
}
```

- Exactly one of `command` or `argv`. `command` runs through `/bin/zsh -lc` with the sanitized environment; `argv` executes directly. The card shows whichever was given, verbatim.
- `cwd` is required and must resolve, after symlink resolution, inside a selected workspace folder. Otherwise `cwd_outside_workspace` before launch.
- `env` adds or overrides at most 32 variables; names matching `ASTRAL_*`, `*TOKEN*`, `*SECRET*`, `*KEY*`, `*PASSWORD*` are refused with `env_forbidden`.
- `timeout_s` defaults to 60, maximum 600 for foreground, 3600 for background.
- `stdin`, when present, is written then closed.

Result (foreground)

```json
{
  "exit_code": 0, "signal": null, "timed_out": false, "duration_ms": 42,
  "stdout": "...tail...", "stderr": "...tail...", "stdout_bytes": 120, "stderr_bytes": 0,
  "truncated": false, "cwd": "/Users/sam/Projects/x", "started_at": "RFC3339"
}
```

Result (background): `{"job_id": "uuid", "started_at": "RFC3339"}`.

### `read_output`

Request `{"job_id": "uuid", "stream": "stdout" | "stderr" | "both", "offset": 0, "max_bytes": 65536, "wait_s": 0}`.

Result `{"job_id", "state": "running" | "exited" | "timed_out" | "interrupted" | "uncertain", "exit_code", "chunks": [{"stream", "offset", "data"}], "next_offset", "truncated"}`.

`wait_s` at most 25 blocks until new output, exit, or the wait elapses.

### `interrupt_command`

Request `{"job_id": "uuid", "signal": "INT" | "TERM" | "KILL"}`. Result `{"job_id", "state", "exit_code"}`.

## Host behavior

- The process runs in its own process group via `posix_spawn` with `POSIX_SPAWN_SETPGROUP`; timeouts and interrupts signal the whole group; `KILL` follows `TERM` after 2 seconds.
- Environment: a fixed allowlist from the user's login shell (`PATH`, `HOME`, `USER`, `LANG`, `LC_*`, `TMPDIR`, `SHELL`, `TERM=dumb`) plus `env`. No Astral variables, no inherited tokens. `PATH` is resolved once from `/bin/zsh -lc 'print -r -- $PATH'` at host start.
- Output: stdout and stderr each in a 256 KiB ring; foreground results return the last 32 KiB of each with byte counts and `truncated`; nothing is written to disk by the host.
- Limits: at most 4 concurrent jobs per session, 64 retained job records, jobs end with the session, Stop, pause, app quit or host deregistration (`TERM` then `KILL`); ended jobs report `interrupted`.
- Uncertainty: if the socket drops after launch, the host finishes or kills the job per timeout and reports `uncertain` on the next `read_output`; the server never retries a `run_command` on its own.
- The host never reads or writes files for the agent through this verb; file verbs keep their own bounds.

## Server behavior

- Scope `tools:execute`; effect class privileged; source-bound review required; unattended execution refused.
- Card: Mac name, the command exactly as proposed, `cwd`, `timeout_s`, `env` keys and values, background flag. Decisions: Approve, Deny, Allow `<argv0>` in `<folder>` for this task (grant per `task-grant.md`).
- Destructive list, always exact review even under a grant: `sudo`, `su`, `rm -r*`, `rm -f* /`, `rmdir`, `mkfs*`, `diskutil`, `dd`, `chmod -R`, `chown -R`, `launchctl`, `defaults write`, `security`, `osascript`, `killall`, `shutdown`, `reboot`, `curl … | sh`, `wget … | sh`, `git push --force*`, `git reset --hard`, `git clean -f*`, `npm publish`, `pip install` outside a virtual environment, `brew install`, any command containing `>` or `>>` whose target resolves outside the workspace folder, and any command with `&&`, `;` or `|` where a segment matches the list. Matching is on the tokenized command after shell-aware splitting.
- Tool descriptions are macOS-specific and say: prefer `run_command` over Terminal keystrokes; use `argv` for exactness; read results, never screenshots, for command output.
- Scanner: `tool_security.py` keeps its `run_command` pattern; `terminal_host_review.py` is generalized to clear the exact computer-use implementation class and these three tool names only.
- Audit: `computer_command.proposed` (redacted preview), `.approved`, `.denied`, `.started`, `.finished` (exit code, duration, byte counts), `.timed_out`, `.interrupted`, `.uncertain`. Output text is tagged untrusted and passes the PHI gate before reaching the model.
- Delegation: the tool declares `scope: tools:execute`; the delegated token is minted from `is_tool_allowed` as today; unattended schedules refuse it.

## Windows and others

Windows and Android hosts do not advertise these verbs; `registry.request` returns `unsupported` as today. Recorded as a Principle XII divergence in the spec.
