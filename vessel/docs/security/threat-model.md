# Threat model

This page says what AstralVessel assumes an attacker can do, what it protects against that, and what it leaves out of scope.

## The assumption

The model, and everything the model reads, may be hostile. A prompt injection can make an agent try any tool it has. The worst a hijacked agent can do is bounded by what the deployment grants it and by the tickets the owner's devices mint.

## What it protects

| Asset | Protection |
|---|---|
| The owner's data on the node | One owner per node. The node's data directory is mode 0700, and its database and keys are mode 0600. |
| The model key | Held only by the egress, when one is used. |
| The node's API | Sandboxes have no route to it. Only the owner's token and the mesh reach it. |
| Tool effects | The executor runs a tool only for a verified ticket that covers this tool, these arguments, this agent, this owner and this workspace. A ticket runs once. |
| Agent data | Mounted read-only. Writable mounts are not supported. |
| Other users of the same agent | Each tool call is confined to its own turn's workspace. See [Isolation and probes](isolation.md). |
| The host network | The egress proxy reaches only globally routable addresses on ports 80 and 443. |

## Out of scope

- Kernel and container escapes. Isolation rests on Landlock, seccomp and Docker.
- What an agent says. An agent can pass anything it may read to the owner.
- A stolen owner device before the owner removes it from the roster.
- A roster the executor has not synced. The executor trusts the roster it last received.
- Denial of service by someone who holds a valid ticket.

See [What it trusts](trust.md).
