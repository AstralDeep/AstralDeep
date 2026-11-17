# CLI

| Command | Does |
|---|---|
| `astralvessel up` | Writes a deployment if there is none, writes Compose, starts the containers, waits, runs the probes and the node checks. |
| `astralvessel check` | Runs the isolation probes and the node checks again. |
| `astralvessel conformance` | Runs the acceptance checks against the running node. |
| `astralvessel pair` | Prints the node's pairing code and payload. `--executor` prints the executor's. |
| `astralvessel ask QUESTION` | Sends one question to the node over A2A and prints the answer. |
| `astralvessel chat` | Starts a conversation with the node. Press Ctrl-D to stop. |
| `astralvessel down` | Stops the deployment. `--volumes` also deletes the data volumes. |
| `astralvessel deploy FILE` | Writes the Compose file and prints the start command. `--up` starts it. |
| `astralvessel executor` | Runs the executor. Used inside its container. |
| `astralvessel sandbox PACKAGE` | Runs one sandbox. Used inside its container. |

## up

| Option | Meaning |
|---|---|
| `--issuer URL` | The owner's OIDC issuer. |
| `--client-id ID` | The client id the owner's apps sign in with. |
| `--subject SUB` | Bind the node to this subject now. |
| `--image REF` | The node image. |
| `--port N` | The local port to publish. Default 8001. |
| `--upstream-url URL`, `--api-key env:NAME` | Add the model gateway. |
| `--example` | Add the example agent. |
| `--agent FOLDER`, `--mount AGENT:/TARGET=FOLDER` | Add an agent and its data. |
| `--timeout SECONDS` | How long to wait. Default 300. |

These options only apply when `deployment.toml` does not exist yet. With a terminal, `up` asks for the issuer and client id it is missing.

## The owner's token

`pair`, `ask`, `chat` and `conformance` read an access token for the owner from `ASTRAL_OWNER_TOKEN`, or from the file named by `--token-file`. They never print it.

## Exit codes

`up` exits non-zero when the node or a sandbox is not ready, or when a probe or node check fails. `check` and `conformance` exit 1 on any failure.
