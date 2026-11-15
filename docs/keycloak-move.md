# Moving Keycloak to a new realm

This runbook covers a move of the identity provider, for example from the UK realm to a UofL realm. It applies to a personal mesh, to personal nodes and to institution nodes.

The short version: add the new issuer, re-attest every device against it, then retire the old issuer. The mesh keeps working the whole time because it rests on its own keys and not on Keycloak.

## What a move changes

Keycloak proves who you are when a device enrolls. The proof is an ID token, signed by the realm, that names the device key and the mesh. The roster stores it. Any peer checks it offline against the issuer's published keys.

A move changes which issuers the roster accepts. It does not change device keys, epoch keys, chat history or the mesh id. Nothing is orphaned, whether or not the new realm keeps your subject.

The mesh principal is the mesh id plus the attested `(issuer, subject)` pairs of its devices. During a move it holds pairs from both realms. Nothing is keyed on a bare subject.

## Before you start

You need all of these:

- The new issuer URL and its JWKS endpoint, reachable from every node.
- A client in the new realm for each app. The default client ids are `astral-desktop`, `astral-mobile` and `astral-watch`. Their redirect URIs must match the old realm.
- The same client id on nodes. A node reads `KEYCLOAK_CLIENT_ID` once and uses it for every issuer it lists.
- One device in the mesh that you can unlock with biometrics. Roster changes need presence consent.

Pick a window of at most 30 days. An attestation goes stale 30 days after it was issued. Institution nodes refuse a stale device.

## Step 1: nodes accept both issuers

On every personal node and institution node, add the new issuer next to the old one and restart.

```
KEYCLOAK_AUTHORITY=https://old.example/realms/astral
ASTRAL_ACCEPTED_ISSUERS=https://new.example/realms/astral
```

The node now verifies tokens from both realms. Its descriptor lists both under `accepted_issuers`. Check it:

```
curl -s https://node.example/api/node/descriptor
```

Institution nodes partition owners by issuer and subject. An owner who signs in through the new realm is a new owner to the node, even with the same subject. See the section on institution nodes below before you retire anything.

## Step 2: the owner adds the new issuer to the roster

On one existing device, add the new issuer as an accepted issuer, with its URL, its client ids and its signing keys. The device asks for biometrics. The apps cannot do this yet, see "What works today".

This writes an `issuer` roster entry that follows every current head. It merges on every device like any other entry. A device that is offline picks it up when it syncs.

If every device is lost, the recovery key may write the same entry.

Until a device has this entry, it refuses attestations from the new realm with `attestation.unknown_issuer`. The kernel tests cover that refusal.

## Step 3: re-attest every device

On each device, sign in to the new realm. The app sends a new authorization request with the usual nonce, which is a hash of the device key and the mesh id. It does not use a token refresh, because Keycloak leaves the nonce out of refreshed tokens.

The result is an `attest` entry. The device is fresh again for 30 days.

Work through the devices in any order. Devices that are not yet moved keep working, and so do the nodes that serve them.

A new device added during the move can sign in to either realm.

## Step 4: check

For each device, check that it is attested and not stale. The kernel sets the `unattested` flag on a device with no attestation or a stale one, and clears it when the device attests.

Send one request from each device to an institution node. Each should be answered. A refusal with `institution.attestation_stale` means that device still needs step 3.

On a node, confirm the descriptor and the mesh status. The member count should match the devices you expect.

## Step 5: retire the old issuer

Do this only when every device is attested under the new issuer, or when 30 days have passed and the stragglers can wait for their next sign-in.

On one device, remove the old issuer from the accepted list. The device asks for biometrics. This writes a second `issuer` entry.

Then remove the old issuer from each node's settings:

```
KEYCLOAK_AUTHORITY=https://new.example/realms/astral
```

Delete the old URL from `ASTRAL_ACCEPTED_ISSUERS` and restart.

Attestations written while the old issuer was accepted stay valid. They age out after 30 days like any other.

## Rolling back

An add that comes after a removal restores the issuer. If the new realm has a problem, add the old issuer again from any device. Keep the old realm running until step 5 is done.

## Institution nodes

An institution node partitions owners by issuer and subject. Each owner has their own mesh peer and kernel data directory under the node data directory.

After a move, an owner who signs in through the new realm is a different owner. The node creates a new peer for them and they pair it into their mesh like any other device.

If the new realm keeps the old subject, nothing collides. The node keys the orchestrator's history, bridge and wake targets by the canonical owner id, which includes the issuer, so the same subject under the new issuer is a separate owner with its own peer.

The node does not carry web chat history between the two owners. The mesh log is unaffected because it belongs to the mesh.

The institution key and the PHI approvals do not change in a move.

## What works today

The kernel accepts several issuers when a mesh is created. A device can re-attest under any issuer the roster accepts. An institution node serves devices attested under different issuers side by side. The tests for these run against the real kernel binding with two test issuers.

The kernel binding does not yet export a call that writes an `issuer` entry. Steps 2 and 5 need it. Until it exists, a mesh can only accept the issuers it listed at creation, and the add and remove entries cannot be written from the apps. The mesh crate has the writers and their tests.

Nothing here has been run against a real Keycloak move.
