# Roster contract, `astral.roster/v1`

The roster is the signed record of who belongs to a mesh. It is a set of entries that each name the entries they follow, so it forms a directed acyclic graph and merges without a coordinator. This document fixes the entry format, every entry type, the merge into state, outside attestations (the identity statement from Keycloak and the PHI approval), unattested devices and pairing.

Reference code: `reference/astral_mesh_ref/roster.py`, `attestation.py`, `pairing.py`. Vectors: `roster.json`, `attestation.json`, `pairing.json`.

## 1. Devices

A device has four keys, described in `keys.md`: an identity key and a presence key (P-256), an operations key (Ed25519) and a wrap key (X25519). The identity key certifies the other keys.

A **device record** is an object with exactly these fields.

| Field | Type | Rule |
|---|---|---|
| `identity` | string | Unpadded base64url of the 65-byte uncompressed P-256 point |
| `presence` | string | Same encoding, a second P-256 point |
| `ops` | string | 32-byte Ed25519 public key |
| `wrap` | string | 32-byte X25519 public key |
| `kind` | string | `desktop`, `pocket`, `satellite` or `node` |
| `label` | string | 1 to 64 characters, chosen by the owner |
| `cert` | string | Identity-key signature of the pairing context over the canonical JSON of `{"device": <record without cert>, "mesh": <mesh id or null>}` |

The `cert` proves the identity key holder chose these other keys, and it binds them to one mesh. The first device signs with `mesh` null because the mesh id does not exist yet. A record whose cert fails raises `roster.bad_device_cert`. A record with other fields or bad encodings raises `roster.malformed`.

The **device id** is the hash of the identity public key. A device id is never reused. A removed device cannot be added again, and a restored phone with a new identity key is a new device.

## 2. Entries

An entry is a JSON object. Its fields are exactly these, plus `presence_sig` where required.

| Field | Type | Rule |
|---|---|---|
| `v` | integer | `1` |
| `type` | string | One of the types in section 3 |
| `mesh` | string or null | Null only for `genesis`. Otherwise the mesh id |
| `follows` | array of hashes | Entry ids this entry follows. Sorted, no duplicates. Empty only for `genesis` |
| `clock` | object | `{"ms": int, "n": int}` as in `event.md` |
| `author` | string | The device id of the writer, or the word `recovery` |
| `body` | object | Depends on `type` |
| `sig` | string | Signature over the entry without `sig` and `presence_sig`, under `astral.roster/v1` |
| `presence_sig` | string | Present exactly on the types listed in section 3 as needing presence, and never on recovery entries. Signature over the same bytes by the author's presence key |

The **entry id** is the hash of the canonical JSON of the entry without `sig` and `presence_sig`. The mesh id is the entry id of the genesis entry.

For a device author, `sig` is made with its identity key. For `recovery`, `sig` is Ed25519 by the recovery signing key from the genesis entry.

Roster changes that add or remove authority need `presence_sig`. The presence key is released by biometrics on hardware that can enforce it, so a thief with a device and its passcode cannot change the roster.

An entry with unknown type, fields or encodings raises `roster.malformed`. A recovery entry of a type recovery may not write raises `roster.recovery_not_allowed`.

## 3. Entry types

| Type | Presence | Recovery may write | Body |
|---|---|---|---|
| `genesis` | yes | no | `device`, `epoch`, `issuers`, `recovery` |
| `add` | yes | yes | `device`, `attestation` (identity attestation or null) |
| `remove` | yes | yes | `device` (device id), `cutoff` |
| `rotate` | no | no | `device` (a record with the same identity key and new `ops` and `wrap`) |
| `attest` | no | no | `attestation` (identity attestation of the author) |
| `issuer` | yes | yes | `{"action": "add", "issuer": {...}}` or `{"action": "remove", "iss": string}` |
| `phi_authority` | yes | yes | `{"action": "add", "institution": {"key": P-256 key, "name": string}}` or `{"action": "remove", "key": P-256 key}` |
| `phi_approval` | yes | no | `{"action": "grant", "attestation": PHI approval}` or `{"action": "revoke", "subject": string}` |
| `epoch` | no | yes, personal group only | `group`, `wraps`, `recovery_wrap` (see `keys.md`) |
| `wrap` | no | yes | `group`, `device`, `grants` (see `keys.md`) |
| `stop` | no | yes | `counter` (integer, at least 1) |

Details.

- **genesis.** `device` is the first device's record. `epoch` is an epoch body for the `personal` group wrapped to that device and to the recovery key. `issuers` lists at least one accepted issuer. `recovery` is `{"sign": Ed25519 public key, "wrap": X25519 public key}`. The genesis entry's id is the personal group's first epoch id and the mesh id. The creating device derives the mesh id before it signs in to Keycloak (section 6).
- **issuer record.** `{"iss": string, "aud": string, "keys": [JWK, ...]}` with at least one key. Each JWK has a `kid`. RSA keys use `n` and `e`. P-256 keys use `crv`, `x` and `y`. This is how a mesh learns which Keycloak realm to trust and which keys check its tokens. Moving to another realm adds an `issuer` entry approved on an existing device. Removing the old one is a second entry.
- **remove.** `cutoff` maps each feed of the removed device to the highest sequence number the remover holds (zero if none). It must name every feed the device ever had. Events past the cutoff are not accepted after the removal is seen. Events at or below it stay valid.
- **rotate.** A device that creates a new operations key adds the new feed. The old feed stays valid for what it already holds. Rotation is how a restored device continues without reusing a sequence number (`sync.md`).
- **attest.** Adds an identity attestation after the fact. The creating device uses it right after genesis, once its Keycloak sign-in has returned.
- **phi_authority.** Names an institution key that may sign PHI approvals for this mesh. An owner cannot approve their own device for institutional PHI, so a mesh with no authority has no PHI members. No build ships an authority.
- **phi_approval.** A grant carries a PHI approval attestation (section 7). A revoke removes the approval for a subject.
- **stop.** The stop counter only rises. An entry is valid only if its counter is above the highest counter in its ancestors. The mesh's stop counter is the maximum over valid stop entries. A peer that sees a counter higher than a ticket's counter refuses the ticket (`ticket.md`).

## 4. Validity of one entry

An entry is checked against the state computed from its own ancestors only (the entries it follows, directly or indirectly, that are themselves active). The checks run in this order and the first failure is recorded as the entry's rejection.

1. Shape (section 2).
2. For `genesis`: the author is the device in the body, `sig` verifies under its identity key, and `presence_sig` under its presence key.
3. For others: the author is `recovery` and `sig` verifies under the recovery key, or the author is a member in the ancestor state. A device that was removed in the ancestor state gives `roster.author_removed`. Any other non-member gives `roster.unauthorized_author`. Then `sig` must verify (`roster.bad_signature`) and `presence_sig` where present (`roster.bad_presence_signature`).
4. Type rules.

| Type | Rules and errors |
|---|---|
| `add` | The device id is not already admitted (`roster.already_member`, or `roster.device_removed` if it was removed). The operations key is not used by another device (`roster.duplicate_key`). A non-null attestation must pass section 6 (an `attestation.*` error) |
| `remove` | The target is a member (`roster.unknown_device`). `cutoff` covers exactly the target's feeds (`roster.cutoff_incomplete`) |
| `rotate` | The author is the record's device (`roster.unauthorized_author`). The identity key is unchanged and the new operations key is unused (`roster.duplicate_key`) |
| `attest` | The author is a device, and the attestation passes section 6 |
| `issuer` (remove) | The issuer is currently accepted (`roster.unknown_issuer`) |
| `phi_authority` (remove) | The key is currently an authority (`roster.unknown_authority`) |
| `phi_approval` (grant) | The attestation passes section 7. A `device` subject is a member (`roster.unknown_device`) |
| `epoch` | Every wrapped device is a member (personal group) or a PHI member (phi group), otherwise `roster.epoch_wrong_members`. A phi epoch is written by a PHI member (`roster.not_phi_member`). Recovery cannot write a phi epoch |
| `wrap` | The device is a member or PHI member of the group. Every grant names a known epoch of that group (`roster.unknown_epoch`) |
| `stop` | The counter is above the ancestors' maximum (`roster.stop_not_rising`) |

## 5. Merge into state

The input is a set of entries and, optionally, the mesh id and the current time in seconds. Duplicates are ignored.

1. Entries with a bad shape are rejected. Entries whose `mesh` differs from the mesh id are rejected with `roster.wrong_mesh`. A second genesis is rejected with `roster.extra_genesis`. Without a mesh id, exactly one genesis must be present (`roster.no_genesis`, `roster.ambiguous_genesis`).
2. An entry whose followed entries are not all present is **pending**. Everything that follows a pending entry is pending too. Pending entries have no effect and are listed.
3. The rest are ordered topologically, ties broken by entry id.
4. The merge runs in rounds with a set of **void** entries that starts empty. Each round checks every entry in order (section 4) against its active ancestors and skips void entries. A failing entry is rejected for this round. Then the round finds new void entries.
5. **The void rule.** Let R be an active `remove` entry for device D. An active entry E of any type except `remove` and `genesis`, written by D (not by recovery), becomes void unless E is an ancestor of R. So an entry D wrote before the removal stands. An entry D wrote after it, or concurrently with it, is void. This is the rule that stops a stolen device from adding an accomplice on its way out.
6. Rounds repeat until no entry becomes void. Void entries never come back, so the loop ends. The result depends only on the set of entries.

Consequences, all covered by vectors.

- **Removal beats a concurrent addition.** A device removed by any active entry is not a member, whatever else was written. Membership is the admitted devices minus the removed devices.
- **Mutual removal.** If two devices remove each other at the same time, both are removed. Removal entries are never void, because authority merges toward less. Only the recovery key can then add a member. This costs the owner a recovery, and it is the safe side of the choice.
- **The recovery key outranks any device.** Its entries are never void, and it can add devices and write the other types in section 3.
- **An accepted issuer** is in force if no active removal for it is concurrent with or later than every add. An add that comes after a removal restores it. Among several adds of one issuer, the newest by `(clock, id)` supplies the keys.
- **A PHI authority** and **a PHI approval** follow the same rule as issuers. A revoke beats a concurrent grant, and a grant written after the revoke stands. An approval counts only while its institution is an authority and, when a time is supplied, while `issued_s` is at most the time and `not_after_s` is at least the time.
- **Epochs.** Every active `epoch` entry is valid, so two devices that start epochs while apart both succeed. The **current epoch** of a group is the newest, by `(clock, id)`, among epochs that no other epoch of the group follows. A later epoch that follows both replaces them.
- **Stop counter.** The maximum over active stop entries.

The state has these parts: `members` (device id to kind, label and feeds), `removed` (device id to cutoff and removing entry), `unattested`, `stale_attestation`, `principal`, `issuers`, `phi_authorities`, `phi_members`, `epochs`, `current_epoch`, `stop`, `recovery`, `feeds`, `void`, `rejected` (entry id to error), `pending`, `heads` and `count`. `heads` is the set of entries no other present entry follows. A new entry follows all current heads.

**Feed status.** For a feed id: `member` if its device is a member, `removed` with the cutoff for that feed if the device was removed, `unknown` otherwise. `event.md` uses this to accept or refuse events.

## 6. Identity attestation

Keycloak proves who the person is when a device enrolls. The proof is carried by the device and stored in the roster, so no peer needs the network to check it.

**The nonce.** The sign-in request for a device uses the nonce
`hex(sha256(identity_public_key || mesh_id_bytes))`
where `identity_public_key` is the 65 bytes and `mesh_id_bytes` is the 32 raw bytes of the mesh id. The nonce is 64 lowercase hex digits. The returned ID token therefore states, with Keycloak's signature, that this person authorized this key for this mesh.

An **identity attestation** is an object with exactly these fields: `kind` (`identity`), `device`, `mesh`, `issuer`, `subject`, `id_token` (the compact JWS exactly as issued) and `sig`. The `sig` is the identity-key signature of the attestation context over the object without `sig`.

Verification takes the device's identity key, the mesh id, the roster's accepted issuers and optionally the time.

1. The object has exactly those fields and `kind` is `identity` (`attestation.malformed`).
2. `device` is the hash of the identity key and `mesh` is this mesh (`attestation.wrong_device`).
3. `sig` verifies (`attestation.bad_signature`).
4. `issuer` is an accepted issuer (`attestation.unknown_issuer`).
5. The token has three segments that decode as canonical base64url and as JSON objects (`attestation.malformed`).
6. The header `alg` is `RS256` or `ES256`. Any other value, including `none`, is refused (`attestation.bad_alg`). The `kid` names a key of the issuer record (`attestation.unknown_key`). The signature verifies (`attestation.bad_token_signature`). For `ES256` the signature is 64 raw bytes.
7. The claim `iss` equals `issuer` (`attestation.wrong_issuer`). The issuer record's `aud` is among the claim `aud` (`attestation.wrong_audience`). The claim `sub` equals `subject` and is not empty (`attestation.wrong_subject`). The claim `nonce` equals the nonce above (`attestation.wrong_nonce`). The claim `iat` is a non-negative integer (`attestation.malformed`).
8. The token's `exp` is not checked. It is the time of enrollment that matters and not whether the token is still alive.

The result is `{issuer, subject, iat, stale}`. A device's attestation is **stale** when the time is more than 30 days after `iat`. A stale device keeps working. A node that demands a fresh attestation refuses it. Re-attestation is a new authorization request with the same nonce rule, written as an `attest` entry. It is not a refresh, because Keycloak leaves the nonce out of tokens issued by refresh.

**The mesh principal** is the mesh id with the set of attested `(issuer, subject)` pairs of members. Nothing is keyed on a bare `sub`.

**Unattested devices.** A device added with a null attestation and no later `attest` entry is listed in `unattested`. It is a member. Policy gives it a smaller allowance and institution nodes refuse it until it attests. Creating a mesh needs one attestation, which the creating device writes as an `attest` entry right after genesis.

Privacy. The token holds whatever claims the realm issues. Every member sees them. A mailbox must not receive roster entries in the clear, other than `remove`, `stop` and `epoch` entries. The mailbox contract fixes how.

## 7. PHI approval attestation

An institution signs, with its P-256 key, a statement that one device, node or model endpoint is approved for its PHI. It is an object with exactly: `kind` (`phi`), `institution` (the key, base64url), `subject_kind` (`device`, `node` or `endpoint`), `subject` (a device id, a node id or an endpoint URL), `issued_s`, `not_after_s` and `sig`. The signature uses the attestation context.

Verification: exact fields (`attestation.malformed`), a valid subject kind and `not_after_s` above `issued_s` (`attestation.malformed`), the institution is an authority of this mesh (`attestation.unknown_authority`), the signature verifies (`attestation.bad_signature`), and the time is inside the window when one is given (`attestation.expired`).

## 8. Pairing

Pairing adds a device. It copies Signal's flow.

1. An existing device creates an **invite**: an object with exactly `v`, `kind` (`invite`), `mesh`, `inviter` (its device id), `addr` (its transport address), `nonce` (16 random bytes, base64url), `expires_s` and `sig`. The signature uses the pairing context and the inviter's identity key. It is shown as a QR code or a link.
2. The new device scans it, checks it (`pairing.malformed`, `pairing.wrong_mesh`, `pairing.wrong_signer`, `pairing.bad_signature`, `pairing.expired`), and generates its keys. It signs in to Keycloak with the nonce rule of section 6, using the mesh id from the invite.
3. It connects to `addr` and sends a **request**: exactly `v`, `kind` (`request`), `mesh`, `nonce` (the invite's), `addr` (its own address), `device` (a record whose cert is bound to this mesh) and `sig`, by the new identity key under the pairing context. The inviter checks it (`pairing.wrong_mesh`, `pairing.wrong_nonce`, `roster.bad_device_cert`, `pairing.bad_signature`).
4. The owner approves on the inviter with biometrics. The inviter writes an `add` entry with the attestation, then an `epoch` or `wrap` entry giving the new device the epoch keys. By default it grants every earlier epoch of the personal group. The owner may grant only the current one. It sends the roster and its entries over the encrypted channel.

A device that shows its own QR code first (the other direction) uses the same request object with a nonce it chose. The inviter signs nothing in that case, and step 4 is the same.

## 9. Errors

`roster.malformed`, `roster.bad_device_cert`, `roster.recovery_not_allowed`, `roster.wrong_mesh`, `roster.extra_genesis`, `roster.no_genesis`, `roster.ambiguous_genesis`, `roster.unauthorized_author`, `roster.author_removed`, `roster.bad_signature`, `roster.bad_presence_signature`, `roster.already_member`, `roster.device_removed`, `roster.duplicate_key`, `roster.unknown_device`, `roster.cutoff_incomplete`, `roster.unknown_issuer`, `roster.unknown_authority`, `roster.epoch_wrong_members`, `roster.not_phi_member`, `roster.unknown_epoch`, `roster.stop_not_rising`, `attestation.*` (sections 6 and 7) and `pairing.*` (section 8).

Entry-level errors are recorded in `rejected` and do not stop the merge. `roster.no_genesis` and `roster.ambiguous_genesis` are raised.

## 10. Limits of version 1

- The recovery key cannot be rotated. If it leaks, start a new mesh.
- Roster entries are public to members. A device cannot hide from another member.
- A removal's cutoff is what the remover held. Events a lost device wrote that reached some other peer but not the remover are quarantined as evidence. The owner can read them but they do not enter the fold.
