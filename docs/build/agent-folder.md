# The agent folder

An agent is a folder. This page shows its layout and walks through writing a small agent from scratch.

## Layout

```text
my-agent/
├── vessel.toml          # required: the manifest
├── tools/               # optional: declared tools
│   ├── lookup.toml
│   └── lookup.py
└── sandbox.Dockerfile   # optional: extra software for the sandbox
```

| File | Required | What it does |
|---|---|---|
| `vessel.toml` | yes | Names the agent, lists its tools, and says what data and limits its sandbox needs. See [vessel.toml](vessel-toml.md). |
| Declared tool files | no | One TOML file per tool, listed in `[tools] declared`. Paths are relative to the folder and must stay inside it. See [Declared tools](declared-tools.md). |
| A sandbox Dockerfile | no | Named by `[sandbox] dockerfile`, built `FROM astralvessel`. See [Extra software](vessel-toml.md#extra-software-in-the-sandbox). |

The manifest lists everything the agent has. A tool that is not listed does not exist for the executor.

The sandbox mounts the folder read-only at `/agent`, and tool calls may read it, so keep secrets out of it.

## Write an agent

This agent answers questions about a folder of Markdown notes.

1. Create the folder:

    ```bash
    mkdir -p agents/notes
    ```

2. Write the manifest, `agents/notes/vessel.toml`:

    ```toml
    [agent]
    name = "notes"
    description = "Answers questions from a folder of notes."

    [tools]
    builtin = ["read", "grep", "glob", "list"]

    [sandbox]
    workdir = "/data"
    mounts = [{ source = "notes", target = "/data" }]
    ```

    The mount says the agent needs data at `/data`. `source` is only a label here; the deployment decides what is
    actually mounted.

3. Grant it in `deployment.toml`:

    ```toml
    [egress]
    upstream_url = "https://api.example.com/v1"

    [[agents]]
    id = "notes"
    package = "agents/notes"
    [agents.sandbox]
    mounts = [{ target = "/data", source = "/srv/notes" }]

    [[apps]]
    name = "web"
    key_sha256 = "env:WEB_APP_KEY_SHA256"
    agents = ["notes"]
    ```

    `package` is relative to the deployment file. The grant's `target` must match the manifest's mount target.

4. Deploy it:

    ```bash
    astralvessel deploy deployment.toml
    docker compose -f compose.generated.yml --env-file .env up -d --build
    ```

    `deploy` refuses a deployment whose grants do not match what the agent declares, so a missing or extra mount
    fails here, not at run time.

## One folder, several agents

A deployment can list the same folder under several agent ids. Each id gets its own sandbox.

## Check a folder before deploying

`load_package` parses and validates a folder the same way the executor does:

```bash
python -c 'from astralvessel.manifest import load_package; p = load_package("agents/notes"); print(p.tool_scopes())'
```

It raises an error naming the first problem: an unknown setting, an unknown built-in tool, a declared tool outside
the folder, or two tools with the same name.
