# Connections

A connection lets the owner's devices reach an outside service, such as an MCP server that asks for an OAuth sign-in. The owner adds a connection in the AstralDeep app. The node keeps its own credential for it, so the node must sign in on its own, and a headless host has no browser for the app to use. `astralvessel connections` does that from the node's host.

## List

```bash
astralvessel connections list
```

Each line shows the first eight characters of the connection id, its name and whether the node is signed in. A connection that needs attention shows why. A node that holds connectors added before connections reports how many, and keeps them as they are. The personal node does not use them.

## Sign in

```bash
astralvessel connections sign-in NAME
```

`NAME` is the connection's name, its id or the start of its id. There are three ways to finish.

| Option | What happens |
|---|---|
| none | Vessel listens on a local port, prints an address, and captures the redirect when your browser on this machine returns to it. |
| `--paste` | Vessel prints the address. You sign in on any device, copy the address your browser ends on, and paste it on standard input. |
| `--device-code` | Vessel prints a code and an address. You enter the code on any device. Vessel polls at the interval the service sets. |

`--port N` fixes the local port for the default mode. The two other options cannot be combined.

Sign-in happens in your own browser. Vessel never sees your password or the service's tokens. The node keeps its credential, and Vessel prints neither a code nor an address you paste. Only the device user code is printed.

## How the node is reached

Vessel runs a small script inside the node's container. The script reads the operator secret from the node's data directory and sends it in the `Astral-Node-Operator` header to the node's own loopback. The secret never leaves the container, is never an argument or an environment variable, and is never printed. A caller ticket cannot add, approve or sign in a connection.

## Check

`astralvessel check` lists connections that need attention, with the command that signs them in again, and the count of connectors added before connections. It changes and deletes nothing.

## When the node refuses

- `node.loopback_required`: run the command on the node's own machine.
- `node.operator_secret_required`: restart the node so it writes the operator secret, then run the command again.
