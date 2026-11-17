# deployment.toml

`deployment.toml` says which node to run and which agents the executor serves. The node's owner is the mesh it joins, so the file names no owner. `astralvessel up` writes one when there is none. Unknown keys are refused. Types and defaults are generated in [Configuration tables](../reference/configuration.md#deploymenttoml).

## A complete example

```toml
[node]
project = "mynode"
image = "astralvessel-node:local"
port = 8001
public_url = "https://node.example.com"
front_subnet = "172.31.247.0/24"
front_address = "172.31.247.10"

[node.environment]
EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS = "model.internal"

[egress]
upstream_url = "https://api.example.com/v1"
upstream_key = "env:VESSEL_UPSTREAM_MODEL_KEY"

[executor]
kernel_artifacts = "kernel-artifacts"

[[agents]]
id = "field-guide"
package = "agents/field-guide"
[agents.sandbox]
mounts = [{ target = "/data", source = "data" }]
memory = "1g"
scratch = "64m"
```

## [node]

| Key | Meaning |
|---|---|
| `project` | The Compose project name. Two deployments on one host need different names. |
| `image` | The AstralDeep node image. Default `astralvessel-node:local`. |
| `bind`, `port` | Where the node is published on the host. Default `127.0.0.1:8001`. |
| `public_url` | The origin devices use to reach the node. Sets `PUBLIC_BASE_URL`. |
| `environment` | Extra settings for the node, as plain `NAME = "value"` pairs. Names the deployment sets itself, and names that look like secrets, are refused. |
| `front_subnet`, `front_address` | The internal network the node and executor share, and the executor's fixed address. |

The node always runs the personal profile with SQLite in its data volume. There is no setting for PostgreSQL.

## An old [owner] section

A file written for an earlier release may still have an `[owner]` section. It loads, `up`, `check` and the other commands print a notice, and the section is ignored. The file is never rewritten. Delete the section when you like. A node whose data was bound to such an owner reports `node.legacy_owner` in `check` and keeps its data untouched.

## [egress]

Optional. See [The egress](models-and-egress.md).

## [executor]

`kernel_artifacts` names a folder with the pinned kernel wheel and mailbox binary for the build architecture. It is passed to the image build, which checks both sha256 values against `config/kernel-binding.json` and fails if either artifact is missing or differs. A checkout needs it. A deployment that uses the released images does not.

## [[agents]]

| Key | Meaning |
|---|---|
| `id` | Lowercase letters, digits and hyphens. Names the agent and its sandbox. |
| `package` | The agent folder, relative to this file. |
| `sandbox` | Limits and the data the sandbox may read. See below. |

A deployment may have no agents. The node then runs alone.

### [agents.sandbox]

| Key | Meaning |
|---|---|
| `mounts` | Data folders for the agent, read-only. They must match what `vessel.toml` declares. A source is a path, `env:NAME` or `volume:NAME`. |
| `memory`, `cpus`, `pids` | Container limits. |
| `scratch` | Size of the scratch filesystem. |
| `turn_disk`, `user_disk` | Disk a turn and one user may hold inside scratch. A quarter and half of scratch by default. |

## env: values

A value of the form `env:NAME` stays a reference that Compose fills from the environment or `.env`. The secret is never copied into the Compose file.

## Check a file

`astralvessel deploy deployment.toml` loads the file, refuses a deployment whose grants do not match its agents, and writes the Compose file.
