# CLI

| Command | Does |
|---|---|
| `astralvessel up` | Writes a deployment if there is none, writes Compose, starts the containers, waits, runs the probes and the node checks. |
| `astralvessel check` | Runs the isolation probes and the node checks again. |
| `astralvessel conformance` | Runs the acceptance checks against the running node. |
| `astralvessel pair` | Prints the node's pairing code and payload, with no sign-in. `--executor` prints the executor's. |
| `astralvessel ask QUESTION` | Sends one question to the node over A2A with a caller ticket and prints the answer. |
| `astralvessel chat` | Starts a conversation with the node. Press Ctrl-D to stop. |
| `astralvessel caller key` | Creates the private `caller-key` file and prints its public holder key. |
| `astralvessel connections list` | Shows the node's outside connections, and whether the node is signed in to each. |
| `astralvessel connections sign-in NAME` | Signs the node in to one connection from this machine. See [Connections](../run/connections.md). |
| `astralvessel down` | Stops the deployment. `--volumes` also deletes the data volumes. |
| `astralvessel deploy FILE` | Writes the Compose file and prints the start command. `--up` starts it. |
| `astralvessel executor` | Runs the executor. Used inside its container. |
| `astralvessel sandbox PACKAGE` | Runs one sandbox. Used inside its container. |

## up

| Option | Meaning |
|---|---|
| `--image REF` | The node image. |
| `--port N` | The local port to publish. Default 8001. |
| `--upstream-url URL`, `--api-key env:NAME` | Add the model gateway. |
| `--example` | Add the example agent. |
| `--agent FOLDER`, `--mount AGENT:/TARGET=FOLDER` | Add an agent and its data. |
| `--timeout SECONDS` | How long to wait. Default 300. |

These options only apply when `deployment.toml` does not exist yet.

## The caller ticket

`ask`, `chat` and `conformance` reach the node with a caller ticket that the owner mints in the AstralDeep app for this node, covering A2A messages.

1. Run `astralvessel caller key`. It creates `caller-key` beside `deployment.toml` with mode 0600 and prints only the public holder key.
2. Give the owner that holder key. They mint a ticket for it and hand you the ticket text.
3. Save the ticket to a file and pass `--ticket-file`, or set `ASTRAL_CALLER_TICKET_FILE` to the file's path.

The ticket is not a secret. The key is, so it is never printed, never an environment variable and never a command-line value. Vessel refuses a `caller-key` that group or others can read. Every request carries the ticket and a fresh proof bound to its method, path and body. The node refuses a bearer token. `ASTRAL_OWNER_TOKEN`, `ASTRAL_CALLER_KEY`, `ASTRAL_CALLER_TICKET`, `--token-file` and `--key-file` are refused with a message that says why.

`pair` needs neither. It reads the node's pairing secret inside the node's container.

## Exit codes

`up` exits non-zero when the node or a sandbox is not ready, or when a probe or node check fails. `check` and `conformance` exit 1 on any failure.
