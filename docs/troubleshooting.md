# Troubleshooting

## The node image is not on this machine

`up` stops with the image name. Build or pull a node image with the kernel binding and tag it with that name, or set `image` in `[node]`.

## The node never becomes ready

Run `docker compose -f compose.generated.yml logs node`. The node exits with code 78 when a secret, the issuer or the data directory is missing or unwritable. Production is the default, so an `http` issuer needs `ASTRAL_ENV = "development"` under `[node.environment]`. Use that only for development.

## A sandbox exits at start

```text
this kernel lacks Landlock ABI 6
```

The host kernel is too old. See [Platform support](security/platforms.md).

## The probes fail

`check` prints the failing check by container. A failure in a sandbox probe means isolation does not hold on this host. Do not use the deployment until it passes.

## `pair` says the node refused the owner token

The token must be current, from the issuer in `[owner]`, for the one owner the node serves. A token for another owner gets HTTP 403.

## `pair --executor` says the node has not joined

The executor waits for the node's mailbox, which exists only after the node has joined the owner's mesh. Pair the node first.

## Every call is refused with `executor.no_kernel`

The kernel binding does not import in the executor's image. An image built here always has it, because the build fails without the pinned wheel and mailbox. Rebuild from this checkout with `[executor] kernel_artifacts` set, or use the released image.

## The image build says it needs the pinned kernel wheel

The build checks the wheel and mailbox sha256 values against `config/kernel-binding.json` and stops if either artifact is missing or differs. Point `[executor] kernel_artifacts` at the folder that holds `astral_kernel-<version>-py3-none-manylinux_2_36_<arch>.whl` and `astral-mailbox-linux-<arch>`. The binding pins only the architectures it lists.

## A call is refused with a `ticket.*` code

The ticket did not verify. The code says why, for example `ticket.expired`, `ticket.mesh_mismatch` or `gate.mesh_stopped`. The executor runs nothing in that case.
