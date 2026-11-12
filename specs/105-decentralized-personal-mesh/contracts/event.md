# Event contract, `astral.event/v1`

This contract fixes the bytes of a signed, encrypted event, the chain each device keeps, and the rules that make every peer compute the same state from the same events. It covers canonical encoding, ids, signatures, the header, the body, the wire envelope, feeds, the event types, and the merge rules of the event families.

Reference code: `reference/astral_mesh_ref/` (`cj.py`, `crypto.py`, `event.py`, `sync.py`, `fold.py`). Vectors: `canonical-json.json`, `crypto.json`, `event.json`, `feed.json`, `fold.json`.

The roster, keys, sync messages, policy, ticket, labels and surfaces have their own documents. This one only refers to them.

## 1. Canonical JSON, `LETS-CJ/1`

Every hash and every signature in the mesh covers bytes produced by this profile. It is the profile LETS already publishes, byte for byte.

- Objects have string keys sorted by Unicode scalar value (the same as byte order of the UTF-8 text). No normalization is applied to any string.
- Arrays keep their order. A set is always written as an array sorted by the rule of the field that holds it.
- Values are null, booleans, strings, integers within signed 64 bits, arrays and objects. Floats, NaN and infinities are refused.
- Output has no whitespace, is UTF-8, escapes control characters as JSON requires, and does not escape other characters.
- Bytes are written as unpadded base64url text. A decoder refuses padding, characters outside the alphabet, and any spelling that is not the canonical one.
- A parser refuses duplicate keys at any depth, integers outside 64 bits, and lone surrogates. It parses integers losslessly before it checks the range.

| Invalid input | Error |
|---|---|
| A float or an exponent | `canonical.float` |
| NaN or an infinity | `canonical.non_finite` |
| An integer outside signed 64 bits | `canonical.range` |
| A duplicate object key | `canonical.duplicate_key` |
| A lone surrogate in a string | `canonical.surrogate` |
| Text that is not JSON | `canonical.parse` |
| A non-string key, a set, or another type | `canonical.type` |
| Padded, non-canonical or non-alphabet base64url | `canonical.base64` |

## 2. Ids and names

| Name | Form | Meaning |
|---|---|---|
| hash | `sha256:` plus 64 lowercase hex digits | SHA-256. Uppercase or short forms are refused with `id.malformed` |
| object id | hash of the canonical JSON of the object | Used for events, policies and roster entries |
| device id | hash of the 65-byte identity public key | One per enrolled device. See `roster.md` |
| feed id | unpadded base64url of the 32-byte operations public key | One feed per operations key |
| event id | hash of the canonical JSON of the full header including `sig` | See section 5 |
| mesh id | the roster id of the genesis entry | See `roster.md` |
| chat id | the event id of the `chat.create` event | The scope of every later event of that chat |
| key reference | `ck:` plus 22 base64url characters, or `ek:personal:<epoch id>`, or `ek:phi:<epoch id>` | Names the key that encrypts a body. See `keys.md` |

Because the event id covers `sig`, a verifier must refuse signatures that are not in canonical form. Ed25519 signatures are refused when `s` is not below the group order. P-256 signatures are refused when `s` is above half the group order (low-S only). Signers produce low-S signatures.

## 3. Signatures and domain separation

A signature covers the bytes `<context>` followed by one newline byte, followed by the canonical JSON of the object with its signature fields removed. A signature is the raw bytes, written as unpadded base64url.

| Context | Signed object | Algorithm and key |
|---|---|---|
| `astral.event/v1` | An event header without `sig` | Ed25519, the feed's operations key |
| `astral.roster/v1` | A roster entry without `sig` and `presence_sig` | `sig`: P-256, the author's identity key. `presence_sig`: P-256, the author's presence key. Recovery entries: Ed25519, the recovery signing key |
| `astral.checkpoint/v1` | A checkpoint without `sig` | P-256, the identity key of the countersigning device |
| `astral.consent/v1` | A consent object without `sig` | P-256, the presence key |
| `astral.policy/v1` | A policy document without `sig` | P-256, the identity key of the author |
| `astral.attestation/v1` | An attestation record without `sig` | P-256. Identity attestation: the device identity key. PHI approval: the institution key |
| `astral.pairing/v1` | A pairing invite or request without `sig`, and a device key bundle (see `roster.md`) | P-256, the identity key of the signing device |

P-256 is ECDSA with SHA-256. A signature is the 64 bytes `r || s`, each 32 bytes big endian. A public key is the 65-byte uncompressed point. Signers should use RFC 6979 deterministic nonces. Verifiers must accept any valid low-S signature.

A signature checked under any other context, key or object fails. That is the only defense against reusing a signature across formats, so no verifier may skip the context.

## 4. Clock

The clock is a hybrid logical clock written `{"ms": int, "n": int}`. Both are non-negative integers. Order is by `ms`, then `n`.

- A peer refuses an event whose `ms` is more than 300000 above its own wall clock. The error is `event.future_clock`. The event is not stored. A peer that is offered it again later may accept it.
- Within one feed the clock must strictly rise from one event to the next. Error: `feed.clock_regress`.
- An event's clock must be strictly above the clock of every event listed in its `after` that the peer already holds. Error: `feed.after_clock`.
- A device that writes an event first sets its clock above every event it has seen, as an HLC does. This makes the order `(clock, feed id, seq)` agree with causal order.

## 5. The header

A header is a JSON object with exactly these fields.

| Field | Type | Rule |
|---|---|---|
| `v` | integer | `1` |
| `feed` | string | Feed id of the writing device |
| `seq` | integer | Position in the feed, from 1 |
| `prev` | string or null | Event id of the previous header in this feed. Null exactly when `seq` is 1 |
| `after` | array of hashes | Event ids from other feeds that this event follows. Sorted, no duplicates, and not containing `prev` |
| `clock` | object | The clock of section 4 |
| `scope` | string | `mesh`, a chat id, or `new` (only on `chat.create`) |
| `type` | string | One of the types in section 9 |
| `labels` | object | `{"confidentiality": ..., "integrity": ...}`, values from `labels.md` |
| `key` | string | Key reference that encrypts the body |
| `body` | string | Hash of the encrypted body bytes (section 6) |
| `sig` | string | Signature of section 3 |

Any other field makes the header malformed. The event id is the hash of the canonical JSON of the header including `sig`.

| Invalid header | Error |
|---|---|
| Wrong fields, version, types, `prev`/`seq` pairing, unsorted `after`, bad scope, bad labels, bad key reference, bad body hash | `event.malformed` |
| `type` not in section 9 | `event.unknown_type` |
| `chat.create` with a scope other than `new`, or a chat type with scope `mesh` | `event.malformed` |
| Signature does not verify under `astral.event/v1` with the feed key | `event.bad_signature` |
| Clock too far ahead | `event.future_clock` |

## 6. The body

The body plaintext is the canonical JSON of a JSON object. It is encrypted with XChaCha20-Poly1305 using a random 24-byte nonce. The stored bytes, called `body_ct`, are the nonce followed by the ciphertext and its 16-byte tag. The header's `body` field is the hash of `body_ct`.

The associated data is the canonical JSON of `{"feed", "key", "scope", "seq"}` taken from the header. The key is the secret named by the header's `key` (section 7 of `keys.md`).

| Failure | Error |
|---|---|
| `body_ct` too short | `event.malformed` |
| Hash of `body_ct` differs from the header's `body` | `event.body_mismatch` |
| Authentication fails (wrong key, edited ciphertext, edited associated data) | `event.decrypt_failed` |
| The reader does not hold the named key | `event.key_unknown` |

## 7. The wire envelope

An event travels as a JSON object with exactly the fields `feed`, `seq`, `header_ct` and `body_ct`.

- `feed` and `seq` are copies of the header's values. They are plaintext so a relay can file the event.
- `header_ct` is unpadded base64url of: the 32 raw bytes of the epoch id of the personal group's current epoch, then a 24-byte nonce, then the XChaCha20-Poly1305 ciphertext and tag of the canonical JSON of the whole header. The associated data is the canonical JSON of `{"epoch": <epoch id>, "feed": <feed>, "seq": <seq>}` using the envelope's values.
- `body_ct` is unpadded base64url of the body bytes, or null when the sender withholds the body (PHI, see `sync.md`).

To open an envelope: decode `header_ct`, read the epoch id, find that epoch key (`event.epoch_unknown` if missing), decrypt (`event.decrypt_failed`), parse strictly, verify the header (section 5), and check that the header's `feed` and `seq` equal the envelope's (`event.envelope_mismatch`, which cannot occur with an honest sender because the associated data already binds them). A malformed envelope raises `event.malformed`.

A relay that holds only `feed`, `seq`, sizes and timing learns nothing else.

## 8. Feeds and chains

A feed is the sequence of one operations key's events. Each peer keeps, for every feed, the accepted headers in order and a fork record.

To accept a verified header for a feed, a peer takes these steps in order.

1. If the feed is already marked forked, refuse with `feed.forked`.
2. Ask the roster for the feed's status. If the feed is not an operations key of any device, refuse with `feed.unknown`. If its device was removed and `seq` is above the removal's cutoff for that feed, refuse with `feed.removed`.
3. If `seq` is at most the number of held headers, then the same event id is a harmless duplicate. A different id at the same place is a fork: store both headers as evidence, mark the feed forked, and refuse with `feed.fork`.
4. If `seq` is more than one above the held count, there is a gap. Refuse with `feed.gap`. The caller keeps the header aside and asks for the missing range. Once the range arrives the header is accepted.
5. If `seq` is above 1 and `prev` is not the event id of the held head, the sender's view of the chain differs from ours. Treat it as a fork as in step 3 (`feed.fork`).
6. Check the clock rules of section 4 (`feed.clock_regress`, `feed.after_clock`).
7. Append.

An `after` hash that the peer does not hold does not block acceptance. The peer lists such hashes as missing and asks for them. A fold that needs a missing event waits for it.

A fork means the device wrote two events at one position. That happens when a backup was restored and the old key reused, or when a key was stolen. Peers keep the evidence, accept nothing more from that feed, and show the owner a prompt to re-enroll the device. See `sync.md` for the restored-device rule that avoids the honest case.

## 9. Event types

The family decides the merge rule. The body of every type is a canonical JSON object. The fields listed are the ones the merge rules read. Other fields are free and belong to the engine.

| Type | Family | Scope | Body fields the folds read |
|---|---|---|---|
| `chat.create` | chat | `new` | none. `title` is customary |
| `chat.user_message` | chat | chat id | `base` (turn id or null: the latest turn the writer knew) |
| `chat.assistant_message` | chat | chat id | none |
| `chat.model_request`, `chat.model_response` | chat | chat id | none |
| `chat.tool_proposed`, `chat.tool_authorized` | chat | chat id | none |
| `chat.tool_result` | chat | chat id | the whole body is addressable by surface bindings |
| `chat.turn_start` | chat | chat id | `continues` (turn id or null), `message` (user message id or null) |
| `chat.turn_end`, `chat.turn_outcome_unknown` | chat | chat id | none |
| `chat.spoken` | chat | chat id | none. `kind` is `answer` or `acknowledgement`, `text` is the assistant text a device actually played aloud, and `turn` is optional |
| `ui.surface_put` | ui | chat id | `document` (see `surface.md`) |
| `ui.patch` | ui | chat id | `surface`, `ops` |
| `ui.action` | ui | chat id | `surface`, `component`, `capability`, `args` |
| `auth.grant` | authority | `mesh` or chat id | `agent`, `tool`, `scope`, `widening` |
| `auth.revoke` | authority | `mesh` or chat id | `agent`, `tool`, `scope` |
| `auth.narrow` | authority | `mesh` or chat id | `key`, `rule` |
| `auth.policy` | authority | `mesh` | `policy`, `consent` (see `policy.md`) |
| `auth.consent` | authority | `mesh` or chat id | `grant`, `consent` |
| `auth.receipt` | authority | `mesh` or chat id | `receipt` (a LETS receipt) |
| `run.lease`, `run.goal`, `run.schedule` | run | `mesh` or chat id | reserved for the runs contract |
| `learn.proposal`, `learn.adoption`, `learn.reversal` | learning | `mesh` | reserved for the learning contract |
| `mem.remember`, `settings.set` | memory | `mesh` | `key`, `value` |
| `mem.supersede` | memory | `mesh` | `key`, `value`, `supersedes` |
| `key.content` | key | `mesh` | `id`, `key`, `scope` (see `keys.md`) |

A `chat.spoken` event records what the person heard in voice mode. It is visible to the owner like an assistant message and carries labels from the chat it was spoken in. A reader that builds model context for a later turn uses the spoken text of a turn in place of that turn's final model text, because the person heard only the spoken text. The complete model output stays in its `chat.model_response` events for audit and is never rewritten.

The mesh family of spec section 5.3 (add or remove a device, a new key epoch, stop) is carried by roster entries, not by feeds. See `roster.md`. A run or learning event is accepted and chained like any other. This version defines no merge function for those two families. Their rules arrive with their own contracts and need no change to this one.

## 10. Merge rules

Every rule below is a pure function of the set of events. Two peers that hold the same events compute the same result, whatever order the events arrived in. The reference functions are in `fold.py`. Vectors are in `fold.json`.

### 10.1 Causal order

An event's parents are its `after` events and the previous event of its own feed. Ancestors are the transitive closure. A cycle raises `fold.cycle`.

### 10.2 Chat branches

Turn ids are the event ids of `chat.turn_start` events. Group turns by their `continues` value. A group with more than one child is a branch point, and its children are listed in the order `(clock, feed, seq)`. Fork, edit-and-resend and regenerate are all a second turn that continues the same turn. A `continues` that names no known turn raises `fold.unknown_turn` and the caller waits for the missing event.

A `chat.user_message` that no turn names in its `message` field is queued. It is placed after the latest turn in the subtree below its `base`, where latest means greatest `(clock, feed, seq)`. A message with a null `base` is placed after the latest turn of the whole chat. A queued message never creates a branch.

### 10.3 Authority

- A grant is keyed by `(agent, tool, scope)`. A grant is in effect only if every revoke with the same key is an ancestor of the grant. A revoke that is concurrent with the grant, or later, wins. A grant written after the revoke is a new grant and stands.
- A grant whose `widening` is true is in effect only if some `auth.consent` event names it in `grant` and carries a valid consent signature (`consent.kind` is `grant`, `digest` is the grant's event id, signed by the presence key of a roster member). Until then it is listed as waiting for consent.
- `auth.narrow` events give a rule per key from `allow`, `consent`, `deny`. The strictest rule across all narrow events applies. Narrowing needs no consent and applies the moment a peer sees it. Any other rule value raises `fold.bad_rule`.
- The budget policy is merged by `policy.md`.

### 10.4 Memory and settings

For each `key`, the winner among `settings.set` and `mem.remember` events is the one with the greatest `(clock.ms, clock.n, feed)`. The feed ids are compared as ASCII text. This is the only rule that depends on wall clocks, which is why section 4 limits how far ahead a clock may be.

### 10.5 Checkpoints

A checkpoint is not an event. See `sync.md`.

## 11. Errors

| Code | Raised when |
|---|---|
| `event.malformed` | A header or envelope has the wrong shape |
| `event.unknown_type` | The type is not in section 9 |
| `event.bad_signature` | The header signature fails |
| `event.future_clock` | The clock is more than five minutes ahead |
| `event.epoch_unknown` | The sealed header names an epoch the reader lacks |
| `event.decrypt_failed` | Header or body authentication fails |
| `event.body_mismatch` | The body bytes do not hash to the header's `body` |
| `event.key_unknown` | The reader lacks the body key |
| `event.envelope_mismatch` | Envelope and sealed header disagree |
| `feed.forked`, `feed.fork` | A feed is or becomes forked |
| `feed.unknown`, `feed.removed` | The feed is not a member's, or is past its removal cutoff |
| `feed.gap` | A sequence number is skipped |
| `feed.clock_regress`, `feed.after_clock` | The clock does not rise |
| `fold.cycle`, `fold.unknown_turn`, `fold.bad_rule` | A fold input is inconsistent |
| `id.malformed` | A hash is not in canonical form |
| `sig.unknown_context` | A signing context is not in the list of section 3 |
