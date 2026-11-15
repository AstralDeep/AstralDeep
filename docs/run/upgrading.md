# Upgrading and releases

## Which images `up` uses

| Installed from | Executor, sandbox and egress images |
|---|---|
| PyPI | The released `ghcr.io/astraldeep/astralvessel:<version>` and `...-egress:<version>` |
| A checkout | Built locally from the checkout, tagged `<project>:local` and `<project>-egress:local` |

The node image is always the one named in `[node] image`. AstralVessel does not build or pull it.

## Upgrade

```bash
pip install --upgrade astralvessel
astralvessel up
```

From a checkout, run `git pull --ff-only` and then `astralvessel up`. `up` writes the Compose file again, rebuilds and recreates what changed. A sandbox whose fingerprint does not match is not served.

To upgrade the node, build or pull a new node image, set `image`, and run `astralvessel up`. The node refuses to start if its SQLite schema does not match the revision the image pins.

## What survives

| Kept | Where |
|---|---|
| The node's database, signing key and mesh keys | The `<project>_node_data` volume |
| The executor's mesh keys | The `<project>_executor_data` volume |
| The node's encryption secrets | `.generated/node.env`. They are created once and kept. |
| Per-container tokens | Derived again from `VESSEL_SANDBOX_TOKEN` |

Back up `.generated/node.env` and the data volumes together. The secrets encrypt what the node has stored.

## How releases are made

Releases come from a version tag on `main`. Set `version` in `pyproject.toml`, run the Release workflow by hand for a dry run, then push the tag `v<version>`. Release builds pass the pinned kernel artifacts as the `kernel-artifacts` build context so the executor image can verify tickets.
