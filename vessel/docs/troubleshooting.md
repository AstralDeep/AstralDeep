# Troubleshooting

## `up` stops with `deployment.container_data`

The folder holds a deployment from an earlier release: a `compose.generated.yml`, or Docker volumes named `node_data` and `executor_data`. Nothing was written, started or deleted.

To keep running it in Docker, add this to `deployment.toml`:

```toml
[runtime]
mode = "container"
```

To move to process mode, follow [Migration](run/migration.md#from-containers-to-processes). `check` and `down` still act on the Compose deployment, so you can inspect or stop it.

## `up` says no node pack is pinned

This release pins no pack for your platform. Pass `--bundle` with the pack's `node-pack.json` and `--bundle-sha256` with its digest. A pack on your own disk needs no digest.

## The pack install refuses

The error starts with a code. `bundle.digest` means the descriptor or archive differs from its digest. `bundle.tree` means a file differs from the pack's manifest, and the message names it. `bundle.glibc` means a Linux pack needs a newer C library than your system has. The previous pack stays in place after any refusal.

## `bash` says `exec.backend_unavailable`

The host has no way to confine untrusted tools, so it refuses to run them. The reason in brackets says what is missing.

| Reason | What to do |
|---|---|
| `landlock_abi` | The kernel is older than 6.12. Install a newer kernel. On Ubuntu 24.04 that is the HWE kernel. |
| `bwrap_missing` | Install bubblewrap with your package manager. |
| `userns_denied` | Unprivileged user namespaces are off. On Ubuntu 24.04 an AppArmor rule blocks them. See [platform support](security/platforms.md#ubuntu-2404). |
| `sandbox_exec_missing` | `/usr/bin/sandbox-exec` is missing on this Mac. |
| `app_sandboxed` | The process runs inside the App Sandbox. Use the direct-download build. |

`read`, `glob` and `list` keep working meanwhile.

## The node never becomes ready in process mode

`up` prints the tail of each role's log. The logs live in `.runtime/logs/`. The node exits with code 78 when a secret or its data folder is missing or unwritable.

## Container mode

The first four entries below apply only to a deployment with `mode = "container"`. The rest apply in both modes.

### The node image is not on this machine

`up` stops with the image name. Build or pull a node image with the kernel binding and tag it with that name, or set `image` in `[node]`.

### The node never becomes ready

Run `docker compose -f compose.generated.yml logs node`. The node exits with code 78 when a secret or the data directory is missing or unwritable. Production is the default, so an `http` issuer needs `ASTRAL_ENV = "development"` under `[node.environment]`. Use that only for development.

### A sandbox exits at start

```text
this kernel lacks Landlock ABI 6
```

The host kernel is too old. See [Platform support](security/platforms.md).

### The probes fail

`check` prints the failing check by container. A failure in a sandbox probe means isolation does not hold on this host. Do not use the deployment until it passes.

### `pair` says the node did not accept its pairing secret

An unenrolled node writes its pairing secret on the first refusal, and `pair` retries once. Run it again. If it keeps failing, check that the node container is running and that you run `pair` from the deployment folder.

### `pair` or `check` reports `node.legacy_owner`

The node's data belongs to an owner who signed in through an identity provider. The node keeps that data untouched and will not join a mesh. Nothing is deleted. Start a new project for a mesh node.

### `ask` says the node refused the caller ticket

The ticket must be current, minted for this node, and cover A2A messages. The holder key must be the one the ticket was minted for, the `caller-key` file made by `astralvessel caller key`. If Vessel says the key file is readable by group or others, run `chmod 600 caller-key`. A bearer token is always refused.

### `pair --executor` says the node has not joined

The executor waits for the node's mailbox, which exists only after the node has joined the owner's mesh. Pair the node first.

### Every call is refused with `executor.no_kernel`

The kernel binding does not import in the executor's image. An image built here always has it, because the build fails without the pinned wheel and mailbox. Rebuild from this checkout with `[executor] kernel_artifacts` set, or use the released image.

### The image build says it needs the pinned kernel wheel

The build checks the wheel and mailbox sha256 values against `config/kernel-binding.json` and stops if either artifact is missing or differs. Point `[executor] kernel_artifacts` at the folder that holds `astral_kernel-<version>-py3-none-manylinux_2_36_<arch>.whl` and `astral-mailbox-linux-<arch>`. The binding pins only the architectures it lists.

### A call is refused with a `ticket.*` code

The ticket did not verify. The code says why, for example `ticket.expired`, `ticket.mesh_mismatch` or `gate.mesh_stopped`. The executor runs nothing in that case.
