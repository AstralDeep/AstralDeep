# The executor

The executor runs a tool only after it has verified a ticket. It sits between the node and the sandboxes and listens only on the `front` network, so no sandbox can reach it.

## The interface

All routes except `/health` need `Authorization: Bearer <token>`. Two kinds of token exist, both derived from `VESSEL_SANDBOX_TOKEN`.

- The operator token is in `.generated/executor.env`. It lists the catalog and asks for the pairing offer. It cannot run or end a call (`executor.token_scope`).
- Each agent has its own token, listed in `.generated/executor-agents.json`. It runs and ends calls for that agent only. It lists only that agent's tools. A call that names another agent gets `executor.token_agent_mismatch`.

| Route | Does |
|---|---|
| `GET /health` | Reports each sandbox's confinement and the executor's mesh state. |
| `GET /tools` | Lists each agent's tools with schema, scope and definition hash. |
| `GET /pairing` | Offers the executor's pairing code and payload to the owner. |
| `POST /run` | Verifies a call and runs it. |
| `POST /end` | Ends a turn and removes its workspace. |

`POST /run` takes `agent`, `tool`, `arguments_json`, `action_json`, `ticket_json`, `workspace` and `owner`.

The generated Compose file points the node at the executor with `ASTRAL_VESSEL_EXECUTOR_URL`. The node gets the operator token in `ASTRAL_VESSEL_EXECUTOR_TOKEN`, from `.generated/node-executor.env`, which `up` rewrites each time. It reads the agent tokens from `/run/secrets/executor-agents.json`, named by `ASTRAL_VESSEL_EXECUTOR_AGENT_TOKENS_FILE`. A deployment with no agents sets none of these.

## What it checks, in order

1. The tool exists for that agent. Otherwise `executor.tool_unknown`.
2. The action names the same tool and the digest of exactly these arguments. Otherwise `request.call_mismatch`.
3. The action's `tool_def` equals the executor's own definition hash. Otherwise `gate.tool_def_changed`.
4. The kernel verifies the ticket offline, with the effect kind taken from the tool's scope. Otherwise a `ticket.*` or `gate.*` code.
5. The verified mesh, device and turn equal the action's, and the ticket's last agent is the action's agent. Otherwise `ticket.unauthorized`.
6. The ticket's mesh is the executor's own mesh. Otherwise `ticket.wrong_mesh`.
7. The deployment names its owner (`subject` in `[owner]`). Otherwise `executor.owner_unbound`.
8. An `owner` or `workspace` in the request equals the derived one. Otherwise `request.call_mismatch`.
9. This ticket has not run before. Otherwise `ticket.replayed`.

The kernel's action has nine fixed fields and no owner or workspace, so a ticket cannot carry them. The executor never takes them from the request. The owner is the deployment's configured owner, bound to the mesh the ticket proves. The workspace is derived from the verified mesh and turn, and `/run` returns it so the node can end the turn. The agent is bound by the definition hash. It is the kernel's hash of the tool's name, description, input schema and annotations, with `{"agent": "<id>"}` added to the annotations. A tool published to the mesh with that annotation carries that hash in the ticket.

Every refusal returns HTTP 403 with a dotted `code`, and nothing runs. 

## Replay memory

The executor writes each ticket's id to `spent.db` in its data volume before the tool runs, so a restart cannot replay a ticket. An id is kept for `VESSEL_REPLAY_RETENTION_SECONDS` (default 3600), which must be longer than the longest ticket life the owner's devices mint. The kernel's default is 300 seconds. The file holds at most 65536 live ids. When it is full the executor answers HTTP 429 with `executor.replay_memory_full` and runs nothing, rather than forget an id.

## Joining the mesh

The executor waits until the node has joined the owner's mesh. Then it opens its own kernel through the node's mailbox and offers pairing. Until the owner approves, `/health` shows `not_enrolled` and every call is refused with `executor.not_enrolled`.

```bash
astralvessel pair --executor
```

The image build fails unless the pinned kernel wheel is present and has the pinned sha256. If the binding still cannot be imported at run time, the executor refuses every ticket and every pairing offer with `executor.no_kernel`. That code is never used for a transport failure, which Deep reports as `executor.unavailable`.

## Revoking access

Revoking a tool's grant on the owner's device stops new tickets for it. A ticket already minted stays valid until it expires, because the executor checks it offline. A stop on the mesh cuts every outstanding ticket at once. The executor refuses it with a `ticket.*` or `gate.*` code after the stop syncs.

## Known limits

- The executor syncs through the node's mailbox. It needs a route to it on `front`. It has no relay, so it reaches the mailbox only by its direct addresses.
- The node-side adapter that sends calls to `POST /run` is not part of AstralVessel.
