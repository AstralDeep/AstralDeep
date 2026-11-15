# What it trusts

Isolation holds only as long as the parties below behave.

| Trusted party | Trusted to | If it misbehaves |
|---|---|---|
| The operator | Write `deployment.toml`, hold the secrets, choose what each agent may reach | Can grant anything |
| The host kernel | Enforce Landlock, seccomp and namespaces | A kernel bug can break every isolation claim |
| Docker and Compose | Enforce networks, read-only mounts and resource limits | Same as the kernel |
| The owner's issuer | Sign tokens only for the real owner | Can act as the owner on the node |
| The owner's devices | Hold their keys and mint tickets only for what the owner approved | A stolen device can mint tickets until it is removed from the roster |
| The node image | Run Deep's gates and keep the owner's data private | Sees everything the owner sends it |
| The model provider | Handle the prompts and data it receives | Sees every prompt and tool result sent |

## Secrets and where they live

| Secret | Held by | Never reaches |
|---|---|---|
| Model key (`VESSEL_UPSTREAM_MODEL_KEY`) | egress | node, executor, sandboxes |
| Egress token (derived) | node, egress | sandboxes, executor |
| Sandbox master (`VESSEL_SANDBOX_TOKEN`) | executor, `deploy` | sandboxes, node, egress |
| Executor token (derived) | executor, the node's tool adapter | sandboxes |
| Per-sandbox token (derived) | executor, that sandbox | other sandboxes |
| Node encryption secrets and signing key | node | everything else |
| Executor mesh keys | executor | everything else |

Generated secrets are written to `.generated/` beside the Compose file with mode 600, in a folder of mode 700 that carries its own `.gitignore`. Never commit `.env`, `compose.generated.yml` or `.generated/`.

## Development mode is not a sandbox

`VESSEL_UNCONFINED=1` lets a sandbox start without Landlock. It exists for tests. Never use it with real data.
