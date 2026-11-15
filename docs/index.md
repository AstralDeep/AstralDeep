# AstralVessel

AstralVessel is the AstralDeep base with its command-line tools and no graphical interface. One command, `astralvessel up`, runs a personal node: the backend, and a way to talk to it.

The node is an AstralDeep orchestrator in its personal profile. It stores everything in SQLite in a Docker volume, joins the owner's mesh, and serves A2A and MCP. Beside it runs the executor, which checks a ticket before a sandbox runs a tool, and an optional egress for model calls.

The AstralDeep app is a separate installation. It carries everything here and adds the interface.

## What runs

- **The node.** An AstralDeep image configured for one owner, with no PostgreSQL. See [How it works](get-started/how-it-works.md).
- **The executor.** A service in front of each agent's sandbox. It verifies the ticket, the action it covers and the tool definition, then forwards the call. See [The executor](run/executor.md).
- **A sandbox per agent.** No network, no secrets of the node, a read-only root and read-only data. Each call is confined with Landlock and seccomp. See [Isolation and probes](security/isolation.md).
- **An egress.** Optional. It holds a model key so the node never does. See [The egress](run/models-and-egress.md).

## Where to start

| If you want to | Read |
|---|---|
| Install and start a node | [Quickstart](get-started/quickstart.md) |
| Add the node from the AstralDeep app | [Pairing](run/pairing.md) |
| Move from 0.6.0 | [Migration from 0.6.0](run/migration.md) |
| Look up a setting | [Configuration tables](reference/configuration.md) |
| Write tools for an agent | [The agent folder](build/agent-folder.md) |
| Judge the risk | [Threat model](security/threat-model.md) |

## Requirements

- Linux 6.12 or newer with Landlock ABI 6, or Docker Desktop on Apple silicon. See [Platform support](security/platforms.md).
- Docker with Compose.
- Python 3.11 or newer for the `astralvessel` command.
- An AstralDeep node image built with the kernel binding. See [Quickstart](get-started/quickstart.md).

AstralVessel is licensed under Apache 2.0.
