# Run an institution node

An institution node is one AstralDeep orchestrator that serves many owners and keeps each owner's mesh apart. It is the shared deployment with mesh peers added. It stays on PostgreSQL.

Set `ASTRAL_NODE_PROFILE=institution`. The node needs your institution's public key and at least one accepted issuer. A missing or malformed value stops the start with exit code 78, in development as well as production.

## What the profile does

- Each owner is an issuer and a subject. The node gives each owner their own mesh peer, with its own kernel data directory and keys, under `owners/` in the node data directory.
- An owner opens a pairing offer with `POST /api/institution/pairing` and pairs the node into their own mesh like any other device.
- Every mesh request is checked before the node's own gates run. The device must be a member with a current identity attestation, otherwise the request is refused with `institution.attestation_stale`.
- PHI-labeled work is accepted only from a member of that mesh's PHI group. Anyone else gets `institution.peer_not_phi`.
- Every ticket still goes through the kernel check and the node's tool gates.
- The descriptor at `/api/node/descriptor` declares the institution key and, when you configure one, the signed approval for this node.

## Settings

| Name | Meaning |
|---|---|
| `ASTRAL_INSTITUTION_KEY` | The institution's P-256 public key, base64url. Required |
| `ASTRAL_INSTITUTION_NODE_APPROVAL` | Path to a signed approval for this node, optional |
| `KEYCLOAK_AUTHORITY`, `ASTRAL_ACCEPTED_ISSUERS` | The issuers the node accepts. At least one is required |
| `ASTRAL_INSTITUTION_MAX_OWNERS` | How many owners one node serves, 1000 by default |
| `ASTRAL_NODE_MAILBOX_MAX_MESHES` | How many meshes the shared mailbox holds |
| `ASTRAL_NODE_WAKE` | Set to `off` to stop content-free wake pushes |

The wake push uses the existing `PUSH_APNS_*` credentials. Without them the node sends no wakes.

## Signing approvals

The institution signs with its own key, offline. The node never holds the private key. Run these from the `backend` directory.

```
python -m shared.institution_profile public-key --key institution.pem
python -m shared.institution_profile sign --key institution.pem --kind node --subject astral-node:... --days 90
```

Save the second command's output to a file and point `ASTRAL_INSTITUTION_NODE_APPROVAL` at it. The node checks the signature and the subject at start. An expired approval is left out of the descriptor.

Each owner's mesh also needs an approval for the node's device in that mesh, with `--kind device`. The owner adds your institution to their roster as a PHI authority first.

No build ships an institution key. Which institution signs, and by what process, is not decided.
