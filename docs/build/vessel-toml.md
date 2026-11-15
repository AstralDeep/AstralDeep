# vessel.toml

`vessel.toml` is an agent's manifest: what it is called, which tools it has, and what its
sandbox needs. Unknown keys are refused. Types, defaults and constraints for every key are generated from the code
in [Configuration tables](../reference/configuration.md#vesseltoml).

## A complete example

```toml
[agent]
name = "field-guide"
description = "Answers questions about the birds in a small sample field guide."
version = "0.1.0"


[tools]
builtin = ["read", "grep", "glob", "list", "bash"]
declared = ["tools/species.toml"]

[sandbox]
workdir = "/data"
mounts = [{ source = "guide", target = "/data", read_only = true }]
deadline_seconds = 300
tool_timeout_seconds = 30

[output]
files = ["png", "csv"]
```

## [agent]

| Key | Meaning |
|---|---|
| `name` | Lowercase letters, digits and hyphens, starting with a letter, up to 63 characters. Names the agent in the executor's catalog. |
| `description` | One or two sentences. Shown in the executor's catalog. |
| `version` | The agent's version. |

The agent's id in `deployment.toml` is what callers use, not `name`.

## [tools]

| Key | Meaning |
|---|---|
| `builtin` | Any of `read`, `grep`, `glob`, `list`, `bash`. See [Built-in tools](builtin-tools.md). |
| `declared` | Paths to declared tool files, relative to the agent folder. See [Declared tools](declared-tools.md). |

## [sandbox]

| Key | Meaning |
|---|---|
| `workdir` | The working directory for tool calls, and the base for relative paths. It must exist in the sandbox: set it to one of your mount targets. |
| `mounts` | The data the agent needs, as `{ source, target, read_only }`. `target` is the path inside the sandbox; `source` is a label (the deployment chooses the real source). `read_only` must be `true`. |
| `deadline_seconds` | How long the sandbox tracks a turn, 5 to 3600 seconds. |
| `tool_timeout_seconds` | Each tool call's time limit, 1 to 600. A declared tool may set its own. |
| `tool_output_chars` | How much of a tool's output the executor returns, 1,000 to 200,000 characters. Longer output is cut with a note saying how much was dropped. |
| `dockerfile` | A Dockerfile in the agent folder for a custom sandbox image. |

Every mount needs a matching grant in `deployment.toml`, and every grant a matching mount. Writable mounts are not
supported: each turn writes only to its own [workspace](workspaces-and-files.md).

### Extra software in the sandbox

An agent whose tools need more than the base image (Python 3.11, ripgrep, a POSIX shell) can ship a Dockerfile in
its folder. It must start `FROM astralvessel`; `deploy` points that name at the AstralVessel image it uses.

```dockerfile
# agents/charts/sandbox.Dockerfile
FROM astralvessel
USER root
RUN pip install --no-cache-dir matplotlib==3.9.2
USER vessel
```

```toml
[sandbox]
dockerfile = "sandbox.Dockerfile"
```

The base image runs as the unprivileged `vessel` user; switch back to it after installing. The Dockerfile must be
inside the agent folder. Its contents, and every file its `COPY` and `ADD` lines bring in from the folder, are part
of the agent's profile fingerprint, so editing them marks the sandbox stale until you run `deploy` and
`docker compose ... up -d --build`.

## [output]

| Key | Meaning |
|---|---|
| `files` | File types tools may return: any of `png`, `svg`, `csv`, `pdf`. See [Workspaces and files](workspaces-and-files.md). |

## Permission scopes

Each tool carries one of six permission scopes, weakest first: `tools:read`, `tools:search`, `tools:files`,
`tools:write`, `tools:system`, `tools:execute`. The executor's catalog lists each tool's scope and the strongest scope for the agent. The scope also decides the effect kind the ticket is verified for: `tools:read` and `tools:search` are outward reads and the rest are outside effects.

| Tool | Scope |
|---|---|
| `read`, `glob`, `list` | `tools:files` |
| `grep` | `tools:search` |
| `bash` | `tools:execute` |
| Declared tools | their `scope`, default `tools:read` |
