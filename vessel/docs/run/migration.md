# Migration from 0.6.0

0.6.0 was an agent server. This release is the base's headless installation. Read this page before you upgrade.

## What was removed

- The agent loop, the model client, the A2A server and the Python client for it.
- The app key, issuer and `X-Vessel-User` identity model. The node's owner token takes its place.
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
| App key and `X-Vessel-User` | The owner's access token from the owner's issuer |
| `astralvessel chat AGENT` | `astralvessel chat` |
| `astralvessel ask AGENT QUESTION` | `astralvessel ask QUESTION` |
| The vessel container | The `node` and `executor` containers |
| Tasks in `vessel_data` | The node's SQLite database in `node_data` |

## Convert the deployment file

1. Replace `[vessel]` with `[node]`. Keep `project`, `front_subnet` and `front_address`. Add `image` if you do not use the default.
2. Add `[owner]` with `issuer` and `client_id`. Add `subject` to bind the node now.
3. Keep `[egress]` only if you want the model gateway. Its `upstream_url` is now optional.
4. Delete `[search]`, every `[[apps]]` and `[[issuers]]` table, and every `services` table.
5. Delete `model`, `prompt_append` and any other agent field the loader now rejects.

In each agent's `vessel.toml`, delete `[model]`, `examples` and `primitives`, remove `webfetch` and `websearch` from `builtin`, and delete `prompt.md`. Service tools must be removed or rewritten as command tools.

Run `astralvessel up`. The loader names anything it still refuses.

## Volumes and secrets

The old `<project>_vessel_data` volume is not read. Delete it when you are sure you do not need it. `VESSEL_APP_KEY` and `VESSEL_APP_KEY_SHA256` are no longer used. `VESSEL_SANDBOX_TOKEN` is unchanged. The model key is still held only by the egress, and the node keeps its provider keys in the product.

## Isolation probes

The probes now run 33 checks for the example deployment against the new containers. They check the node, the executor and the egress instead of the vessel and the search container.
