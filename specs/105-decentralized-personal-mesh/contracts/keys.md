# Key contract, `astral.keys/v1`

This contract fixes the keys a device holds, key groups and epochs, content keys, the wrapping format `astral.wrap/v1`, the recovery key with its 24-word encoding, and the encrypted backup file.

Reference code: `reference/astral_mesh_ref/keys.py` and `crypto.py`. Vectors: `keys.json`, `crypto.json`.

## 1. Keys on a device

| Key | Algorithm | Kept in | Public form | Used for |
|---|---|---|---|---|
| Identity | P-256 | Secure Enclave, StrongBox or TPM. Never exported | 65-byte uncompressed point | Roster entries, checkpoints, policies, attestations, pairing. Certifies the other keys |
| Presence | P-256 | Same hardware, released by biometrics for each use | 65-byte uncompressed point | Consent, and the approval signature on roster changes |
| Operations | Ed25519 | Keychain or keystore. Rotated | 32 bytes | Event headers and capability tokens. Its public key is the feed id |
| Wrap | X25519 | Keychain or keystore. Rotated | 32 bytes | Receiving wrapped epoch keys |

Signing rules are in `event.md` section 3. The certificate that ties the four keys together is in `roster.md` section 1.

## 2. Key groups and epochs

A **key group** is a set of devices that share encryption keys. There are two in version 1.

- `personal`: the owner's own devices and nodes.
- `phi`: the members that hold a valid PHI approval (`roster.md` section 7).

An **epoch** is one 32-byte symmetric key of a group. Its id is the roster entry id of the `epoch` entry that introduced it, and the id of the genesis entry for the personal group's first epoch. Epoch keys are random.

The epoch key of the personal group seals event headers. It also encrypts the body of `key.content` events (section 4). The epoch key of the phi group encrypts the content keys of PHI scopes, so a device outside the group cannot decrypt PHI even if it holds the ciphertext.

Rules.

- Each epoch key is wrapped separately for each member of its group, and the wraps are recorded in the roster entry that introduces the epoch.
- Removing a device starts a new epoch of every group it belonged to. The new epoch is wrapped to the remaining members only. The removed device cannot read anything written under the new epoch and keeps what it had.
- Two devices may start epochs while apart. Both are valid. The writing epoch is the newest head (`roster.md` section 5). Every event header names the epoch it was sealed under, so any epoch stays usable for reading.
- Any member that holds an epoch key may wrap it for another member that lacks it, with a `wrap` roster entry.
- An institution's node and another person are never in the personal group. They receive individual content keys by grant, re-wrapped to them.

## 3. Wrapping, `astral.wrap/v1`

To wrap a 32-byte secret to a recipient's X25519 public key `R`:

1. Choose a fresh ephemeral X25519 private key and compute its public key `E`.
2. Compute the shared secret `X25519(ephemeral, R)`.
3. Derive a 32-byte key with HKDF-SHA256. The input is the shared secret. The salt is `E || R` (64 bytes). The info is the ASCII text `astral.wrap/v1`.
4. Encrypt the secret with XChaCha20-Poly1305 under that key with a random 24-byte nonce. The associated data is the canonical JSON of the object described below.

The output is an object with exactly `epk` (E), `nonce` and `ct`, all unpadded base64url. `ct` is 48 bytes, the 32-byte secret and the 16-byte tag.

The associated data for an epoch key is `{"group": <group>, "purpose": "epoch", "recipient": <device id or "recovery">}`. It makes a wrap useless for another group or another recipient.

To unwrap, repeat the key derivation with the recipient's private key, `E` from the object, and `R` computed from the private key.

| Failure | Error |
|---|---|
| A field is missing | `wrap.malformed` |
| The key or associated data is wrong, or `ct` was edited | `wrap.decrypt_failed` |
| An unusable X25519 key | `wrap.bad_key` |
| The device is not among an epoch body's recipients, or a PHI epoch is opened as recovery | `wrap.not_a_recipient` |

The salt `E || R` is added to the specification of the shared choices. It binds the derived key to both public keys and changes nothing else.

## 4. Content keys

Each chat has its own random 32-byte content key, and so does the `mesh` scope. The header's `key` field names it as `ck:` plus the unpadded base64url of 16 random bytes, the key's id.

A content key reaches peers as a `key.content` event with scope `mesh`. Its body is `{"id": "<ck:...>", "key": "<base64url of the 32 bytes>", "scope": "<chat id, mesh, or new>"}`. The header's `key` is `ek:<group>:<epoch id>`, so the body is encrypted directly under that epoch key. The value `new` means the key was made for a chat not yet created. The chat's `chat.create` event names the key in its own `key` field and so binds it.

A content key for a chat that holds PHI is introduced by a `key.content` event under a `phi` epoch. The rule is: **an event whose confidentiality label is `phi` must use a content key that was introduced under a phi epoch key.** A device outside the phi group sees the event header and the ciphertext hash. It cannot decrypt the body, and it is never sent the body (`sync.md`).

### 4.1 Credential items

A credential, such as a model provider's API keys, must sync to every member yet open only on the devices the owner chose. It travels as a `key.credential` event with scope `mesh`. The header label is `{"confidentiality": "secret", "integrity": "owner"}`. The header's `key` is the mesh content key like any other mesh event.

The body has the fields `name` and `boxes`, and optionally `audience`. `name` is the credential's name, 1 to 128 characters of `a-z`, `0-9`, `.`, `_` and `-`, starting with a letter or digit. `boxes` maps a device id to a box, at most 64 of them. The value is never in the body in the clear. A body with no `boxes` is `credential.not_sealed` and any other wrong shape is `credential.malformed`. A `key.credential` event whose label is not secret is `credential.label`.

**A box** is the wrap of section 3 with a different payload and associated data. The payload is the canonical JSON of the credential's value, which is an object of at most 65536 bytes, so `ct` is longer than 48 bytes. The associated data is `{"name": <name>, "purpose": "credential", "recipient": <device id>}`. A box is useless for another device or another name. Each box is sealed to the X25519 wrap key of one device from the roster.

**Audience.** `audience` is `all` when the writer chose the default, every current device at the time of writing, and `list` when the writer named an explicit list. A body without `audience` is read as `list`. Any other value is `credential.malformed`.

**Recipients.** The default is every current device. A caller may name an explicit list, which must be non-empty and made only of current devices (`credential.bad_recipients`). A removed device is not current, so it receives nothing new. It keeps what it opened before, and the owner must rotate the credential itself at its provider when a device is lost. A writer that has a box for itself keeps the plain value only in memory and in its own keystore.

**Late devices.** A device paired after an item was written has no box in it. When a device approves a pairing, it writes one new item for every credential whose winning item has the audience `all`, that it can open, and that lacks a box for some current device. The new item has the same name, value and audience and a box for every current device. The rule is idempotent, because the new item covers every current device and the next approver finds nothing to write. An item with the audience `list`, an item with no `audience` and a removed credential are never re-sealed. The reference function is `credential.reseal_names`.

**Fold.** For each `name` the winner is the item with the greatest `(clock.ms, clock.n, feed)` among accepted items with a valid body (section 10.4 of `event.md` uses the same order). A device opens its own box in the winner. If the winner has no box for the device, or the box does not open, the credential is absent on that device, and an older item is never used instead. An item with no boxes removes the credential everywhere. The reference function is `credential.fold`.

**Never in a model context.** The opened value is used only where the kernel injects it at dispatch. It is not put in an event body in the clear, a log line, an error text, a return value of the binding surface, or any model input. Flow rules refuse any turn that has a secret label in its context (`labels.md` section 4).

| Failure | Error |
|---|---|
| A body with no `boxes` (for example a plain value) | `credential.not_sealed` |
| Wrong fields, a bad name, a box that is not an `epk`, `nonce`, `ct` object, or a recipient that is not a device id | `credential.malformed` |
| The label is not secret | `credential.label` |
| An explicit recipient list that is empty or names a device that is not current | `credential.bad_recipients` |
| The value or the number of recipients is too large | `credential.too_large` |
| The device has no box in the winning item | `credential.not_a_recipient` |
| The box does not open (wrong key, name or recipient, or an edited box) | `wrap.decrypt_failed` |

Vectors: `credential.json`.

## 5. The recovery key

The recovery key is 32 random bytes that the owner holds. It is shown once, when the mesh is created, as 24 words. Nothing escrows it.

**Words.** Append the first byte of the SHA-256 of the secret to the secret. That makes 264 bits. Cut it into 24 groups of 11 bits, most significant first. Each group is an index into the English BIP-39 word list (2048 words, one per line, SHA-256 of the file `2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda`, stored in `reference/astral_mesh_ref/wordlist_en.txt`). Words are joined by single spaces. This is the BIP-39 encoding of 256 bits of entropy. A BIP-39 library produces the same words.

When decoding, input is lowercased and split on any whitespace.

| Failure | Error |
|---|---|
| Not 24 words, or a secret that is not 32 bytes | `recovery.bad_length` |
| A word that is not in the list | `recovery.unknown_word` |
| The checksum byte does not match | `recovery.bad_checksum` |

**Keys derived.** Both use HKDF-SHA256 with no salt over the 32-byte secret and a 32-byte output.

| Info text | Result |
|---|---|
| `astral.recovery.sign/v1` | The seed of an Ed25519 signing key. Its public key goes in the genesis entry |
| `astral.recovery.wrap/v1` | An X25519 private key. Its public key goes in the genesis entry |

The recovery signing key may add a device and outranks any device (`roster.md` section 5). Every epoch of the personal group is also wrapped to the recovery wrap key, in the epoch body's `recovery_wrap`. Phi epochs are not, so PHI history is not recoverable from the recovery key alone.

## 6. The encrypted backup file

The backup is written by the app to any place the owner chooses. It is sealed to the recovery wrap **public** key, so the app can write it without the recovery secret.

The file is a JSON object with exactly these fields.

| Field | Value |
|---|---|
| `format` | `astral.backup/v1` |
| `mesh` | The mesh id |
| `epk` | A fresh ephemeral X25519 public key |
| `nonce` | 24 random bytes |
| `ct` | XChaCha20-Poly1305 ciphertext and tag of the canonical JSON of the payload |

The key is derived as in section 3, with the recovery wrap public key as the recipient, salt `epk || recipient public key`, and info `astral.backup/v1`. The associated data is the canonical JSON of `{"format", "mesh"}`.

The payload has exactly two fields: `roster` (an array of roster entries) and `events` (an array of wire envelopes, recent history). The epoch keys are already in the roster, wrapped to the recovery key, and the content keys are in `key.content` events inside `events`.

| Failure | Error |
|---|---|
| Wrong fields, an unknown format, or a payload with other fields | `backup.malformed` |
| Wrong recovery key, an edited file, or a swapped `mesh` | `backup.decrypt_failed` |

Without a surviving copy of the roster and wrapped keys (a device, a node, a mailbox or this file), the recovery key restores nothing but the right to start again.

## 7. Secrets in vectors

Every key in the vectors is derived from a published test seed and is for tests only. The derivation is in README.md.
