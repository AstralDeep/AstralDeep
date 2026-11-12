# Run your own node

A personal node is one AstralDeep orchestrator that serves one owner. It runs on your Mac, a home server or a VM. Your apps talk to it and not to a shared server.

## What the personal profile changes

Set `ASTRAL_NODE_PROFILE=personal` and the node starts with a different posture.

- Keycloak only proves who you are. The node needs no Keycloak client secret and makes no token exchange per tool call.
- The node has its own Ed25519 signing key. It creates the key on first start in the node data directory. The file is mode 0600 and the key is never logged.
- The node accepts tokens only from the issuers you list, and only for one owner.
- Tool delegation tokens are signed by the node. They last at most 300 seconds, name the agent in the `act` claim, and carry `aud` set to the agent audience and to this node's id. Another node refuses them.
- The node publishes its public keys at `/.well-known/astral-node/jwks.json`.

Everything else still fails closed. A missing secret, a missing issuer or an unwritable key directory stops the start with exit code 78. Mock auth is refused outside development.

## Install

Run the installer once. It is safe to run again.

```
scripts/node/install-personal-node.sh \
  --issuer https://keycloak.example/realms/astral \
  --client-id astral-app \
  --database-url postgresql://...
```

It creates `~/.astral-node/data` and `~/.astral-node/node.env`, both private to you. It generates the node key and the encryption secrets. It keeps any value that already exists and it never prints a secret.

Start the orchestrator with that env file. For the container image use `--env-file` and mount the data directory at the path the file names.

## Settings

| Name | Meaning |
|---|---|
| `ASTRAL_NODE_PROFILE` | `personal` selects this profile |
| `ASTRAL_NODE_DATA_DIR` | Where the node key and owner binding live |
| `KEYCLOAK_AUTHORITY` | The first accepted issuer |
| `ASTRAL_ACCEPTED_ISSUERS` | More accepted issuers, comma separated |
| `KEYCLOAK_CLIENT_ID` and `KEYCLOAK_ALLOWED_AZP` | The client ids whose tokens are accepted. A token with no `azp` is refused |
| `ASTRAL_NODE_OWNER` | Optional. The owner id to bind at install time |
| `JWKS_STALE_GRACE_SECONDS` | How long the last good issuer keys serve when the issuer is unreachable. Default 86400 |

## Who owns the node

An owner id is `oidc.<first 16 hex of sha256 of the issuer>.<subject>`. The same subject from a different issuer is a different owner.

The node binds to one owner. The installer can bind it with `--owner-subject`. Otherwise the first owner whose token the node verifies binds it. The binding is a file named `owner` in the data directory. After that the node refuses every token for any other owner.

Existing data keyed on a bare subject is not migrated. A node starts clean.

## Pair a device

1. The new device makes its device key and shows it to the app that is signed in.
2. The signed-in app calls `POST /api/node/pairing` with `label`, `device_key`, optional `scopes` and optional `ttl_seconds`. The node returns a short code such as `K7QM-4T2X`, a payload for a QR code, and the invitation.
3. The new device scans the QR code. Or it sends the code to `POST /api/node/pairing/resolve` and gets the same payload. A code works once and expires with the offer.
4. The device checks the node id and node key in the payload, signs the challenge with its device key, and finishes with `POST /api/mesh/enrollment/redeem`.

Pairing offers are held in node memory. A restart drops the short codes. The invitation payload in the QR code stays valid until it expires.

`GET /api/node/descriptor` is public. It returns the node id, the node public key, the accepted issuers, a hash of the owner, the capabilities and the transport addresses. The address list is empty until the transport is configured.

## When Keycloak is unreachable

A signed-in session keeps working. The node serves the last good issuer keys for the grace period and it signs delegation locally. A new sign-in needs the issuer.

## Emergency stop

Raising the stop counter halts new effects on the node. The counter is stored in the database and only rises. The node mints no delegation for an owner whose stop is engaged.
