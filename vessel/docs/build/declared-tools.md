# Declared tools

A declared tool is a command the agent's sandbox runs when it is called. This page covers the tool file, how
arguments reach the command, and what comes back.

## Write a tool

A tool is a TOML file in the agent folder, listed in `vessel.toml`:

```toml
# vessel.toml
[tools]
declared = ["tools/species.toml"]
```

```toml
# tools/species.toml
name = "species"
description = "List every species in the guide with its family, habitats and length, as JSON."
command = ["python3", "{package}/tools/species.py"]
scope = "tools:read"

[parameters]
type = "object"
[parameters.properties.habitat]
type = "string"
description = "Only species with this habitat (optional)"
```

```python
# Prints every species page's frontmatter as JSON, optionally filtered by habitat; reads the request from stdin.
import json
import pathlib
import sys

request = json.load(sys.stdin)
wanted = (request.get("habitat") or "").lower()
rows = []
for page in sorted(pathlib.Path("species").glob("*.md")):
    head = page.read_text().split("---")[1]
    fields = dict(line.split(": ", 1) for line in head.strip().splitlines())
    habitats = [h.strip() for h in fields["habitat"].strip("[]").split(",")]
    if wanted and wanted not in habitats:
        continue
    rows.append({"page": page.name, "name": fields["name"], "family": fields["family"],
                 "habitat": habitats, "length_cm": int(fields["length_cm"])})
print(json.dumps(rows, indent=1))
```

The command runs with the agent's `workdir` as its current directory, so `species` above is `/data/species` when
`workdir = "/data"`.

## Fields

| Key | Default | Meaning |
|---|---|---|
| `name` | required | Lowercase letters, digits and underscores, starting with a letter, up to 63 characters. May not repeat a built-in tool or another declared tool. |
| `description` | required | What the caller gets to decide when to call it. |
| `command` | | The argument vector to run. `{package}` is replaced with the agent folder's path in the sandbox (`/agent`). |
| `parameters` | `{"type": "object", "properties": {}}` | A JSON Schema for the arguments, kept as the tool's input schema. |
| `input` | `"json-stdin"` | How arguments reach the command: `json-stdin` or `args`. |
| `timeout_seconds` | the agent's `tool_timeout_seconds` | This tool's time limit. |
| `scope` | `"tools:read"` | Its [permission scope](vessel-toml.md#permission-scopes). |

## How arguments arrive

With `input = "json-stdin"` (the default), the command receives the call's arguments as one JSON object on
standard input.

With `input = "args"`, each argument becomes a `--name=value` flag appended to the command, and standard input is
empty. Only names declared under `parameters.properties` are accepted; a call with any other argument is refused
before the command runs, so a caller cannot add flags of its own.

```toml
# tools/word_count.toml
name = "word_count"
description = "Count the words in one file."
command = ["python3", "{package}/tools/word_count.py"]
input = "args"
[parameters]
type = "object"
required = ["path"]
[parameters.properties.path]
type = "string"
```

```python
# tools/word_count.py: called as word_count.py --path=notes/a.md
import argparse
import pathlib

parser = argparse.ArgumentParser()
parser.add_argument("--path", required=True)
print(len(pathlib.Path(parser.parse_args().path).read_text().split()))
```

Values are passed with Python's `str()`, so a boolean arrives as `True` or `False` and a list as its Python form.
Prefer `json-stdin` for anything beyond plain strings and numbers.

The executor refuses arguments that are not a JSON object.

## What the caller gets

The command's standard output and standard error, combined, are the result. An exit status other than zero marks
the call as an error. Empty output becomes `[exit code N]`.

Output is cut to the agent's `tool_output_chars`, with a note saying how much was dropped. A call
that runs past `timeout_seconds` (or `tool_timeout_seconds`) has its process group killed and returns
`[timed out after N s]`. The fixed output caps are in [Limits](../reference/limits.md).

## Where it runs

Each call runs in the agent's sandbox as its own confined process:

- It can read the system directories, the agent folder (`/agent`), the `workdir` and the mounted data, and write
  only the turn's workspace (`$TMPDIR`, also `$HOME`).
- It has no network, cannot see or signal other processes, cannot make socket files, and cannot use shared memory or
  message queues.
- Its environment holds only `PATH`, `LANG`, `HOME`, `TMPDIR` and `VESSEL_OUTBOX`, never a secret.
- It cannot start a new session or process group (`setsid` and `setpgid` are blocked), so processes it leaves
  running in the background are killed with it. It may hold at most 4,096 open files, and may not write a single
  file larger than the turn's disk limit (see [Disk limits](workspaces-and-files.md#disk-limits)).

A tool that needs software beyond the base image ships a [sandbox Dockerfile](vessel-toml.md#extra-software-in-the-sandbox).
A tool can return files by writing them into `$VESSEL_OUTBOX`; see [Workspaces and files](workspaces-and-files.md).

## Test a tool locally

Run the script the way the sandbox does, with the arguments on standard input and the workdir as the current
directory:

```bash
cd examples/sample-data
echo '{"habitat": "rivers"}' | python3 ../field-guide/tools/species.py
```
