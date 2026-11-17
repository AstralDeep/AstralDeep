# Pairing

Pairing adds the node to the owner's mesh. The node offers a code, and a device the owner already uses approves it.

## Print the offer

```bash
export ASTRAL_OWNER_TOKEN=<an access token for the owner>
astralvessel pair
```

The command asks the node's `POST /api/node/pairing` as the owner. It prints a code, a payload and the node's address. The offer expires after ten minutes. Run the command again for a new one.

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

Only the owner. The node refuses any other token with HTTP 403, and a call with no token with HTTP 401. The first owner whose token the node verifies binds the node, unless the deployment names `subject` in `[owner]`.
