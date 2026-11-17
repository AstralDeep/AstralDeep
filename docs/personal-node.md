# Run your own node

A personal node is one AstralDeep orchestrator that serves one owner. It runs on your Mac, a home server or a VM. Your apps talk to it and not to a shared server.

## What the personal profile changes

Set `ASTRAL_NODE_PROFILE=personal` and the node starts with a different posture.

- The node needs no sign-in and no Keycloak. Your owner is the mesh you pair it with.
- The node has its own Ed25519 signing key. It creates the key on first start in the node data directory. The file is mode 0600 and the key is never logged.
- Tool delegation tokens are signed by the node. They last at most 300 seconds, name the agent in the `act` claim, and carry `aud` set to the agent audience and to this node's id. Another node refuses them.
- The node publishes its public keys at `/.well-known/astral-node/jwks.json`.

Everything else still fails closed. A missing secret or an unwritable key directory stops the start with exit code 78. Mock auth no longer exists.

## Install

Run the installer once. It is safe to run again.

```
scripts/node/install-personal-node.sh
```

The old `--issuer`, `--client-id` and `--owner-subject` options were removed. The installer refuses them and says what replaced them.

It creates `~/.astral-node/data` and `~/.astral-node/node.env`, both private to you. It generates the node key and the encryption secrets. It keeps any value that already exists and it never prints a secret. An env file from the sign-in era keeps its `ASTRAL_NODE_OWNER` line untouched. The node reports that owner as a legacy owner and keeps your data. The production posture refuses to start while the line is set, so remove it yourself before you pair.

The node stores its data in a SQLite file named `plane.sqlite3` in the node data directory. The file is mode 0600 and needs no database server. Pass `--database-url postgresql://...` to use PostgreSQL instead. A `sqlite:///absolute/path.db` URL picks another SQLite file. The shared multi-user deployment stays on PostgreSQL and refuses a SQLite URL.

The node refuses to start when the SQLite schema revision, migration digest or structure digest differs from the values pinned in `config/astral-composition.json`.

Start the orchestrator with that env file. For the container image use `--env-file` and mount the data directory at the path the file names.

## Settings

| Name | Meaning |
|---|---|
| `ASTRAL_NODE_PROFILE` | `personal` selects this profile |
| `ASTRAL_NODE_DATA_DIR` | Where the node key, owner binding and SQLite database live |
| `DATABASE_URL` | Optional. `sqlite:///absolute/path.db` or a PostgreSQL URL. Unset means SQLite in the node data directory |
| `ORCHESTRATOR_HOST` | The address the server binds. A personal node defaults to `127.0.0.1`. Every other profile defaults to `0.0.0.0`. Use an IP address or `localhost` |
| `ASTRAL_NODE_OWNER` | Optional. The owner's mesh id. Leave it unset and the node binds the mesh it joins |

## Who owns the node

The node binds to the mesh it joins. Its owner id is derived from the mesh id. The binding is a file named `owner` in the data directory. After that the node serves only that mesh.

A node from the sign-in era may hold an owner id that starts with `oidc.`. The node reports it as a legacy owner and refuses to pair until you resolve it. Its data stays where it is.

Existing data keyed on a bare subject is not migrated. A node starts clean.

## Pair a device

Pairing needs no sign-in.

1. Start the node. While it is not enrolled it writes a secret to `pairing-secret` in the data directory. The file is mode 0600.
2. On the same machine, call `POST /api/node/mesh/offer` on `127.0.0.1` with the secret in the `Astral-Pairing-Secret` header. The call is refused from any other address and when a `Forwarded` or `X-Forwarded-For` header is present.
3. The answer holds a short code, a payload for a QR code and an expiry. Scan it with your Mac or iPhone app and approve.
4. Once the node is enrolled, the same call returns the enrolled state and the secret is deleted.

The node replaces the secret after each offer it serves. It serves at most 5 offers a minute.

`GET /api/node/descriptor` is public. It returns the node id, the node public key, a hash of the owner, the capabilities and the transport addresses. The address list is empty until the transport is configured.

## Emergency stop

Raising the stop counter halts new effects on the node. The counter is stored in the database and only rises. The node mints no delegation for an owner whose stop is engaged.

## Mesh peer and mailbox

A personal node joins its owner's mesh as a peer when the pinned kernel binding is installed. The image build installs the wheel and the `astral-mailbox` binary from the artifacts named in `config/astral-kernel-binding.json`. Pass them with `docker build --build-context kernel-artifacts=<directory>`. Without that context the node runs with no mesh peer.

The node's mesh keys live in `<node data>/mesh` at mode 0600 and are created once. The node signs in software, reports that it is not hardware backed, and never signs an owner consent.

To join, request an offer as described under Pair a device. Approve the offer on the Mac or iPhone. After that, `GET /api/node/descriptor` reports `mesh.mesh_id`, `mesh.member_id` and the mailbox address.

The node supervises one mailbox for its own mesh and holds at most one mesh. The node's own kernel uses that mailbox, and devices of the mesh set its address and exchange events through it when they are never online together.

Settings: `ASTRAL_NODE_MESH=0` turns the peer off, `ASTRAL_NODE_MESH_LABEL`, `ASTRAL_NODE_MESH_RELAY` (`default`, `none` or URLs), `ASTRAL_NODE_MAILBOX=0`, `ASTRAL_MAILBOX_BIN`, `ASTRAL_NODE_MAILBOX_BIND`, and and `ASTRAL_NODE_MESH_REQUESTS=off` to stop serving requests.

A device that cannot do something itself sends a request addressed to the node. The node claims it, runs it on its own loop through its own gate stack and machine authority, and records the turn it ran with `answers` set to the request. When it cannot serve, it refuses the request with a dotted code, such as `node.phi_not_approved` or a `ticket.*` code. Turns the node runs for thin clients are recorded into the owner's chat the same way.

A request that carries a tool call comes with a ticket. The node derives the effect kind from its own record of the tool, verifies the ticket with its own roster, stop counter and clock, checks the action against the call, compares the tool definition hash in the action with the hash of its own definition of that tool (name, description, input schema and annotations), and then runs its own gates. A forged ticket, a different device or tool, a changed tool definition (`gate.tool_def_changed`), or arguments that do not match the action are refused before anything runs.

The node labels what it records. The labels are at least the join of the request, the chat and the ticket, and at least private. PHI work, including any work for an owner under PHI protection, is refused unless the roster lists the node as a PHI member. Secret content is always refused. A request claimed before a crash and never answered is refused with `node.interrupted` on the next start.

## Sandboxed tools

Tools that live in an AstralVessel sandbox run only through the Vessel executor. The node never runs them itself. Set these on the node:

| Variable | Meaning |
|---|---|
| `ASTRAL_VESSEL_EXECUTOR_URL` | The executor's address on the internal network, for example `http://executor:8800` |
| `ASTRAL_VESSEL_EXECUTOR_TOKEN` | The executor's operator token. The node uses it only to list `/tools` |
| `ASTRAL_VESSEL_EXECUTOR_AGENT_TOKENS_FILE` | Path to the JSON file of agent ids and their tokens (`.generated/executor-agents.json` in a Vessel deployment). The node uses an agent's own token for `/run` and `/end` |
| `ASTRAL_VESSEL_EXECUTOR_TIMEOUT_SECONDS` | Optional. Longest wait for one call. Default 120 |

A production node refuses to start (exit 78) when any of the first three is set and the set is incomplete, the address is not http or https, or the token file is missing or not a JSON object of non-empty strings. Tokens are never logged.

At start the node reads `GET /tools` and lists each sandboxed agent with the executor's own definition hash for every tool. Once the node has joined the owner's mesh it publishes those tools to the mesh catalog with the annotation `{"agent": "<id>"}`, so the hash in a device's ticket is the hash the executor holds. With no executor configured, or when it cannot be reached at start, no sandboxed tool is listed. A sandboxed agent whose executor is gone refuses the call with `executor.unavailable`.

A call goes to `POST /run` with the tool, the exact arguments the action covers, the signed action, the ticket and the owner the node is serving. The action must name the tool, `agent` and `mesh`. It carries no owner or workspace: the executor takes the owner from its deployment and derives the workspace from the ticket, and returns that workspace from `/run`. The node ends it with `POST /end` using the agent's token when the node stops or the workspace has been idle for 15 minutes. The node refuses a call without a ticket with `executor.ticket_required`, so a turn the node starts itself cannot reach a sandbox until the kernel can mint a ticket for it.

Executor refusals keep their dotted codes, such as `ticket.replayed` or `gate.tool_def_changed`. A tool error in the sandbox is `executor.tool_error`. If the node cannot connect, nothing ran and the code is `executor.unavailable`. If the connection fails or times out after the call was sent, the code is `executor.outcome_unknown` and the call is not retried.
