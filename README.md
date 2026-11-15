# AstralVessel

AstralVessel is the AstralDeep base with its command-line tools and no graphical interface. `astralvessel up` runs a personal node: the backend, and a way to talk to it.

The node is an AstralDeep orchestrator in its personal profile. It runs Deep's turn loop, stores its data in SQLite in a Docker volume and joins the owner's mesh. It serves A2A and MCP, and the command line talks to it.

The AstralDeep app is a separate installation. It carries everything here and adds the interface.

**Documentation:** <https://astraldeep.github.io/AstralVessel/>

## What runs

- **The node.** An AstralDeep image configured for one owner. No PostgreSQL.
- **The executor.** A service in front of each agent's sandbox. It verifies the owner's ticket, the action it covers and the tool definition before a sandbox runs a tool.
- **A sandbox per agent.** No network, no secrets of the node, a read-only root and read-only data. Each call is confined with Landlock and seccomp.
- **An egress.** Optional. It holds a model key so the node never does, and runs a filtered web proxy.

## Start

```bash
pip install astralvessel
astralvessel up --issuer https://keycloak.example.com/realms/astral --client-id astral-app --example
export ASTRAL_OWNER_TOKEN=<an access token for the owner>
astralvessel check
astralvessel pair
astralvessel ask "hello"
```

`up` needs an AstralDeep node image on the machine. The [quickstart](https://astraldeep.github.io/AstralVessel/get-started/quickstart/) says how to build one.

| Command | Does |
|---|---|
| `up` | Starts the node and the executor, waits, and runs the checks |
| `check` | Runs the isolation probes and the node checks |
| `conformance` | Runs the acceptance checks against the running node |
| `pair` | Prints the node's pairing code and payload for the AstralDeep app |
| `ask`, `chat` | Talk to the node over A2A |
| `down` | Stops the deployment |

## What it protects, and what it trusts

AstralVessel assumes the model, and everything it reads, may be hostile. It keeps:

- the node's data in a private volume, with keys readable only by the node
- tool effects behind tickets that the executor verifies offline, once each
- agent data read-only, tool calls with no network, and each call out of other calls' files and processes
- agent-driven web traffic to public addresses on ports 80 and 443

It trusts the operator, the host kernel, Docker, the owner's issuer and the owner's devices. See the [threat model](docs/security/threat-model.md).

## Upgrading from 0.6.0

0.6.0 was a separate agent server. Its loop, A2A server, identity model and search are gone. Read the [migration note](docs/run/migration.md).

AstralVessel is licensed under Apache 2.0. Report security problems as described in [SECURITY.md](SECURITY.md).
