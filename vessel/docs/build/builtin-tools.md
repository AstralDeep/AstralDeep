# Built-in tools

AstralVessel ships seven tools an agent can list in `[tools] builtin`. This page gives each one's arguments,
limits and where it runs.

```toml
[tools]
builtin = ["read", "grep", "glob", "list", "bash"]
```

| Tool | Runs in | Scope | Does |
|---|---|---|---|
| `read` | sandbox server | `tools:files` | Reads a text file as numbered lines |
| `grep` | sandbox, confined | `tools:search` | Searches file contents with a regular expression |
| `glob` | sandbox server | `tools:files` | Finds files by path pattern |
| `list` | sandbox server | `tools:files` | Lists a directory |
| `bash` | sandbox, confined | `tools:execute` | Runs a shell command |

Paths may be absolute or relative to the agent's `workdir`. The file tools can reach only the system directories,
the agent folder (`/agent`), the `workdir`, the mounted data and the turn's workspace; anything else is refused as
outside the agent's data. `read`, `glob` and `list` check where an opened path really points, so a symbolic link
cannot lead them outside.

## read

| Argument | Type | Default | Notes |
|---|---|---|---|
| `path` | string | required | A file |
| `offset` | integer | `1` | First line to return |
| `limit` | integer | `400` | Lines to return, at most 2,000 |

Returns lines as `     N<tab>text`. A line longer than 4,000 characters is cut and marked `[line truncated]`. When
more lines follow, the last line says which `offset` to continue with.

## grep

| Argument | Type | Default | Notes |
|---|---|---|---|
| `pattern` | string | required | ripgrep regular expression syntax |
| `path` | string | the workdir | File or directory |
| `glob` | string | | Only files matching this glob, such as `*.md` |
| `ignore_case` | boolean | `false` | |
| `max_results` | integer | `100` | At most 1,000 |

Returns `path:line:text` lines, each cut at 400 columns. Uses `rg` when the image has it (the AstralVessel image
does), and `grep -rnE` otherwise. Runs as a confined process, like `bash`.

## glob

| Argument | Type | Default | Notes |
|---|---|---|---|
| `pattern` | string | required | Relative, without `..`; a pattern with no `/`, such as `*.md`, searches the whole tree |
| `path` | string | the workdir | Directory to search from |
| `max_results` | integer | `200` | At most 2,000 |

Stops after the agent's `tool_timeout_seconds` and says so.

## list

| Argument | Type | Default |
|---|---|---|
| `path` | string | the workdir |

Lists directories first, each with a trailing `/`, then files, up to 500 entries.

## bash

| Argument | Type | Default |
|---|---|---|
| `command` | string | required |

Runs `/bin/sh -c <command>` in the `workdir` as a confined process and returns the combined output followed by
`[exit code N]`. A non-zero exit marks the result as an error. The same confinement, environment and output limits
as [declared tools](declared-tools.md#where-it-runs) apply. `bash` gives the caller a general-purpose shell over
everything the agent can read, so it carries the strongest scope.

