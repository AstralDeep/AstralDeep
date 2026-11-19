# Node descriptor `astral.node.descriptor/v2`

The node descriptor is the discovery document every client fetches before it trusts or talks to a
personal node. One GET answers: which node this is, which key it signs with, who owns it, and which
capabilities it serves. It is served by `GET /api/node/descriptor` with `Cache-Control: no-store`,
and the node's public signing keys are published separately at
`/.well-known/astral-node/jwks.json` with a short public cache.

Reference code: `reference/descriptor.py`. This contract is documentation of the shape the node
serves today, promoted to L1 so clients validate against committed rules instead of the
implementation.

## 1. Document

The document is a JSON object. A client refuses any document whose `format` is not
`astral.node.descriptor/v2` and any `version` it does not implement.

| Field | Meaning |
|---|---|
| `format` | The literal `astral.node.descriptor/v2` |
| `version` | The node surface version, currently `astral.node/v1` |
| `node_id` | The node's member id in the mesh |
| `node_key` | The node's public Ed25519 key as a JSON Web Key (`kty: OKP`, `crv: Ed25519`) |
| `owner_hash` | `sha256:` hash of the owner's mesh principal, or `null` while no owner is bound |
| `capabilities` | The capability names this node serves (section 2) |
| `transport_addresses` | Where the node is reachable; empty for loopback-only nodes |

## 2. Capabilities

Capabilities are advisory until the client uses the door they describe; a capability name promises
the door exists, never that a particular ticket covers it.

| Capability | Promise |
|---|---|
| `chat` | A conversational surface exists for a principal of the matching class |
| `tool-dispatch` | The node dispatches tool calls for authorized principals |
| `mesh-pairing` | The node exposes the mesh pairing offer flow |
| `delegation-eddsa` | Delegation tickets are Ed25519 signed |
| `emergency-stop` | The node serves the stop control |
| `mesh-peer` | Added when the mesh is running: the node is a roster member |
| `mesh-mailbox` | Added when the node is enrolled and a mailbox is reachable |

Unknown capabilities are ignored by clients, never treated as consent to use them.

## 3. Mesh extension

When the node's mesh is running, the document gains fields. A client must treat every mesh field as
optional; a node that has not started its mesh serves the base document only.

| Field | Meaning |
|---|---|
| `mesh` | `state`, `mesh_id`, `member_id`, `hardware_backed`, `requests`, `owner_binding` |
| `mailbox` | Present when enrolled with a reachable mailbox: `kind: iroh-mailbox` and its `address` |

`transport_addresses` gains `{kind: "iroh", address}` entries when the mesh exposes an endpoint
address. Clients never assume an address kind; they match on `kind`.

## 4. Verification

`node_key` is the trust anchor. A client that verifies node signatures fetches
`/.well-known/astral-node/jwks.json` and checks the key used is the descriptor's key. The descriptor
itself is unsigned: it is fetched over the transport the client already trusts (loopback for local
clients, the paired transport for remote ones). A client on an untrusted transport must not treat an
unsigned descriptor as authority for anything beyond discovery.

## 5. Versioning

Adding a capability or an optional mesh field is compatible. Removing or renaming a field, or
changing `format`, is breaking and bumps the format suffix. `version` tracks the node surface
independently; clients pin the versions they implement and degrade on unknowns.

## 6. Reference

`reference/descriptor.py` validates a document the way a client must: format check, field presence
and types, capability names, and the optional mesh fields. Vectors: `vectors/descriptor-v2.json`
hold a minimal loopback node, a mesh-extended enrolled node and malformed documents.
