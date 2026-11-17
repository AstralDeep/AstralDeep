# Pairing

Pairing adds the node to the owner's mesh. The node offers a code, and a device the owner already uses approves it.

## Print the offer

```bash
astralvessel pair
```

The command needs no sign-in and no token. It runs a small script inside the node's container. The script reads the node's pairing secret from the node's data directory and sends it in the `Astral-Pairing-Secret` header to `POST /api/node/mesh/offer`, over the node's own loopback. The secret never leaves the container and is never printed. The command prints a code, a payload and the node's address. The offer expires after ten minutes. Run the command again for a new one.

The node writes the secret while it is not in a mesh, replaces it after each offer it serves, and deletes it once it is in one.

If the node is already in a mesh, the command says which one and prints no code.

## Approve it

In the AstralDeep app on a device that is already in the mesh, choose Add a device, then enter the code or paste the payload. Approve it on that device.

When it is done, `GET /api/node/descriptor` shows the mesh id, the member id and the mailbox address.

## Add the executor

```bash
astralvessel pair --executor
```

This works once the node has joined. Approve the second offer the same way. See [The executor](executor.md).

## Who can pair

Only the owner, on a device that is already in the mesh. The node's offer is limited to its own machine and to a holder of its pairing secret, so another process on the host cannot read it without the node's own access. Whoever holds the code still needs the owner's approval, with the comparison code and the presence key, on an existing device.

## When the node refuses

- `node.legacy_owner`: the node's data belongs to an owner who signed in through an identity provider. The node keeps that data untouched and will not join a mesh. `astralvessel check` reports it. Start a new project for a mesh node.
- `node.pairing_rate_limited`: wait a minute and run the command again.
- `node.loopback_required`: the command must run on the node's own machine.
