# How it works

`astralvessel up` turns `deployment.toml` into a Compose file. Nothing in the running system controls Docker.

| Container | Networks | Holds |
|---|---|---|
| `node` | `front`, `outside`, and `egress` when a gateway is set | The node's data volume with SQLite, its signing key and its mesh keys |
| `executor` | `front`, and one tools network per agent | Its own mesh keys, and the sandbox master token |
| `sandbox-<agent>` | its own tools network, which is internal | Nothing of the node |
| `egress` | `egress`, `outside` | The model key, only when `[egress]` is set |

## The node

The node runs with `ASTRAL_NODE_PROFILE=personal`. Its data directory is mode 0700 and its database file and keys are mode 0600. It trusts one issuer and one owner. Its A2A endpoint is `/a2a` and it publishes a descriptor at `/api/node/descriptor`. Deep's turn loop runs every turn. AstralVessel has no loop of its own.

## The executor

A tool call for a sandboxed agent goes to the executor with a ticket and the action it covers. The executor refuses unless the ticket verifies against its own copy of the owner's roster. Then it forwards the call to the right sandbox with that sandbox's own token.

The executor is a member of the owner's mesh, so it can verify tickets without asking anyone. The owner adds it the same way as the node. See [The executor](../run/executor.md).

## The sandboxes

A sandbox runs file, shell and declared tools. It has no network route, a read-only root and read-only data. Each call is confined with Landlock and seccomp, so users who share an agent cannot see each other. The per-agent bearer token stays as a second check behind the ticket.
