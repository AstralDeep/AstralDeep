# Migration from 0.6.0

0.6.0 was an agent server. This release is the base's headless installation. Read this page before you upgrade.

## What was removed

- The agent loop, the model client, the A2A server and the Python client for it.
- The app key, issuer and `X-Vessel-User` identity model. The owner's mesh takes its place: devices in the mesh reach the node, and scripts use caller tickets the owner mints.
- The task store, the app-side history store and the transcript format.
- Service tools, web tools (`webfetch`, `websearch`), the SearXNG search container and the rich-output tool.
- `prompt.md`, the `[model]` table and `[output] primitives` in `vessel.toml`.
- `astralvessel serve`, `astralvessel client` and the `stress/` load tool.
- The `[vessel]`, `[search]`, `[[apps]]`, `[[issuers]]` and `services` settings in `deployment.toml`.

Nothing was left behind in a disabled state. If you need the old server, stay on 0.6.0.

## What replaces it

| 0.6.0 | Now |
|---|---|
| A2A to `vessel:8700/agents/<id>` | A2A to the node at `/a2a`, served by Deep's loop |
| App key and `X-Vessel-User` | A caller ticket the owner mints in the AstralDeep app |
| `astralvessel chat AGENT` | `astralvessel chat` |
| `astralvessel ask AGENT QUESTION` | `astralvessel ask QUESTION` |
| The vessel container | The `node` and `executor` containers |
| Tasks in `vessel_data` | The node's SQLite database in `node_data` |

## Convert the deployment file

1. Replace `[vessel]` with `[node]`. Keep `project`, `front_subnet` and `front_address`. Add `image` if you do not use the default.
2. Leave out `[owner]`. The node's owner is the mesh it joins. An old `[owner]` section still loads with a notice.
3. Keep `[egress]` only if you want the model gateway. Its `upstream_url` is now optional.
4. Delete `[search]`, every `[[apps]]` and `[[issuers]]` table, and every `services` table.
5. Delete `model`, `prompt_append` and any other agent field the loader now rejects.

In each agent's `vessel.toml`, delete `[model]`, `examples` and `primitives`, remove `webfetch` and `websearch` from `builtin`, and delete `prompt.md`. Service tools must be removed or rewritten as command tools.

Run `astralvessel up`. The loader names anything it still refuses.

## Volumes and secrets

The old `<project>_vessel_data` volume is not read. Delete it when you are sure you do not need it. `VESSEL_APP_KEY` and `VESSEL_APP_KEY_SHA256` are no longer used. `VESSEL_SANDBOX_TOKEN` is unchanged. The model key is still held only by the egress, and the node keeps its provider keys in the product.

## Isolation probes

The probes now run 33 checks for the example deployment against the new containers. They check the node, the executor and the egress instead of the vessel and the search container.

## From containers to processes

A deployment folder written by an earlier release holds container data. Vessel never moves or deletes it for you.

`astralvessel up` stops with `deployment.container_data` and names the Docker volumes. `check` and `down` keep working on the Compose deployment, so you can still inspect and stop it.

You have two choices.

To keep the data where it is, add this to `deployment.toml` and nothing else changes:

```toml
[runtime]
mode = "container"
```

To move to process mode, export the data by hand. Stop the node first without `--volumes`:

```bash
astralvessel down
mkdir -p .runtime/node-data .runtime/executor
docker run --rm -v <project>_node_data:/from:ro -v "$PWD/.runtime/node-data":/to alpine \
  sh -c 'cp -a /from/. /to/ && chown -R '"$(id -u):$(id -g)"' /to'
docker run --rm -v <project>_executor_data:/from:ro -v "$PWD/.runtime/executor":/to alpine \
  sh -c 'cp -a /from/. /to/ && chown -R '"$(id -u):$(id -g)"' /to'
```

Then add `[runtime] mode = "process"` to `deployment.toml` and run `astralvessel up`. The node reads the same database and signing key from `.runtime/node-data`. The secrets in `.generated/node.env` are reused, so what the node stored stays readable.

Check the result with `astralvessel check`. Delete the Docker volumes and `compose.generated.yml` only after you are happy.
