# Contacts and grants `astral.contacts/v1`

This contract fixes how two people become contacts and how an owner grants a contact a view of one chat. It covers the invitation, the roster digest and comparison code, the contact record and its fold, how a verified contact follows the other mesh's roster, the grant record and its fold, revocation, the consent digests and every code. The Kernel, the node and the Apple apps implement against this document and its vectors. The session that carries a share between two meshes is `share.md`, and the PHI destination rules come from [phi-destinations.md](phi-destinations.md).

Existing roster v2 stores keep working unchanged. This contract adds no event type and no roster field. It adds four families of `settings.set` keys and two consent kinds, which deployed kernels accept and fold as settings no code reads.

Reference code: `reference/contacts.py`. Generator: `reference/generate_contact_vectors.py`. Vectors: `vectors/contacts-v1.json`. LETS-CJ/1, hashes, signatures and the consent object come from the preserved 105 reference. The consent steps are the ones of [connection.md](connection.md) sections 5 and 6.1 and run through the same code. The name rule reads the caller ticket label table `reference/label_classes.json`. The roster entries in the vectors are signed roster v2 entries built on the roster v2 test devices, and the roster fold is the one of `reference/phi_destinations.py`.

## 1. Carrier

Every record is a `settings.set` event with scope `mesh` and labels `{"confidentiality": "private", "integrity": "owner"}`. Any other scope or labels is `contact.malformed` or `share.malformed` by the key's family.

| Record | Key | Value | Consent |
|---|---|---|---|
| Contact | `contact/<64 lowercase hex of the contact's mesh id>` | The record of section 3, or null to remove | Kind `contact` when `state` is `verified`. None for `unverified` and for null |
| Contact roster mark | `contact.roster/<same 64 hex>` | The mark of section 3.3 | None |
| Grant | `share/<32 hex>` | The record of section 7 | Kind `share`, always |
| Revocation | `share.revoked/<32 hex>` | `{"v": 1, "ms": <integer>}` | None |

The contacts fold and the shares fold read every event of these keys in order, not only the settings winner. The deployed `settings` fold is unchanged and keeps folding them as settings. Rotation of a chat key is an ordinary `key.content` event (section 8). No new event type and no `auth.grant` is used, because `auth.grant` grants tool authority to an agent and deployed authority views would show it.

The reference folds take one input object. `mesh` is the owner's mesh id, `members` and `removed` are the roster entries of the owner's devices with `kind`, `presence` and `feeds`, `events` are the events, `chats` (grants fold only) lists the ids of existing chats, and `limits` optionally replaces the two caps. A production fold passes no `limits`.

## 2. Keys

| Form | Grammar | Result of `parse_key` |
|---|---|---|
| Contact | `contact/` then 64 lowercase hex digits | `contact` |
| Mark | `contact.roster/` then 64 lowercase hex digits | `mark` |
| Grant | `share/` then 32 lowercase hex digits | `grant` |
| Revocation | `share.revoked/` then 32 lowercase hex digits | `revoked` |

A key that starts with `contact/`, `contact.`, `share/` or `share.` and matches no row is reserved and malformed. It raises `contact.malformed` for the two contact prefixes and `share.malformed` for the two share prefixes. Every other key is `other`, including `contacts/x`, `shared/x` and the bare word `contact`. The 64 digits of a contact key are the digits of the contact's `sha256:` mesh id.

## 3. Contacts

### 3.1 The record

```
{"v": 1, "mesh", "name", "state", "heads", "count", "ms"}
```

`mesh` is the contact's `sha256:` mesh id and must match the key. `name` is what the owner typed, 1 to 64 code points, none of them refused by the caller ticket label table. `state` is `unverified` or `verified`. `heads` is a sorted list of distinct `sha256:` ids, and `count` is an integer, both being the roster state the owner compared. They are `[]` and 0 when `state` is `unverified`. A verified record has at least one head and a `count` of at least the number of heads. At most 64 heads are listed. `ms` is a non-negative integer. Any other shape is `contact.malformed`. At most 256 contacts are live.

### 3.2 Consent

A put with state `verified` carries a consent of kind `contact` whose `digest` is the `sha256:` id of the LETS-CJ/1 bytes of the value. The checks are the ten steps of connection.md section 6.1 in the same order, with the prefix `contact.` in place of `connection.`.

| Step | Check | Code |
|---|---|---|
| 1 | Scope and labels, the key, a body of `key`, `value` and at most `consent`, the record of section 3.1 | `contact.malformed` |
| 2 | The body has a `consent` | `contact.consent_missing` |
| 3 | The consent has exactly its six fields, `v` 1, text `kind`, `digest`, `device` and `sig`, and a clock of two non-negative integers | `contact.malformed` |
| 4 | `kind` is `contact` and `digest` is the digest of `value` | `contact.consent_wrong_action` |
| 5 | `device` is a current member | `contact.consent_unknown_device` |
| 6 | That member's roster kind is not `node` | `contact.consent_node_device` |
| 7 | The event's own feed is one of that member's feeds | `contact.consent_foreign_feed` |
| 8 | The signature verifies against that member's presence key, low-S only | `contact.consent_bad_signature` |
| 9 | `event.clock.ms - 600000 <= consent.clock.ms <= event.clock.ms` | `contact.consent_stale` |
| 10 | No earlier accepted consent of this fold equals this one on every field except `sig` | `contact.consent_reused` |

Step 7 stops another device carrying an older verified put again. Step 10 stops the same device doing it inside the window. A put naming the owner's own mesh is `contact.self`, checked after the record shape and before the consent.

A put counts only while its consenting device is a current member, as connection.md section 6.4 says. A put whose approver was removed fails step 5 and is ignored. An older valid put for the same key then stands, so removing the device that verified a contact returns that contact to its earlier record until another device verifies again.

A put with state `unverified` and a null removal need no consent. Their body is exactly `key` and `value`, and a consent field on either is `contact.malformed`. Their feed must belong to a current or removed member, otherwise `contact.writer_not_member`. A node may write them. They only narrow, like a revoke.

### 3.3 The fold and the marks

Per key the latest valid event by `(clock.ms, clock.n, feed, seq)` wins. A re-carried older verified put on another feed fails step 7, on the same feed inside the window fails step 10 and after the window fails step 9, so a downgrade is undone only by a new presence approval. Invalid events go to `ignored` with their code. If more than 256 contacts are live, the 256 whose winning events are earliest stay and the rest are ignored with `contact.too_many`.

A mark is the value `{"v": 1, "heads", "count", "removed"}`. `heads` is a sorted list of at most 64 distinct ids, `count` is an integer of at least the number of heads, and `removed` is a sorted list of at most 256 distinct device ids. A mark is written by a device that learns newer heads for a contact (section 6.4). Its body is exactly `key` and `value`. Its feed must belong to a current member of kind `desktop` or `pocket`, otherwise `contact.writer_not_member` for a feed no current member owns and `contact.writer_kind` for another kind. A mark naming the owner's own mesh is `contact.self`.

The fold merges all valid marks of a key. `removed` is the union, and `heads` and `count` come from the mark with the greatest `count`, ties broken by the greater event order key. A mark counts even when the key has no contact record.

Removing a contact writes a `share.revoked` for each of its grants first and then the null put. Without the revocations a grant for that mesh would come back when the same mesh is added and verified again, and a vector pins both outcomes.

The fold's output has `contacts` (sorted by mesh, each with `mesh`, `event`, `state`, `record` and `approved_by`), `marks` (by the 64 hex digits) and `ignored`.

`propose_share(contacts, mesh)` refuses with `contact.unverified` when the contact is absent or not verified (section 7).

## 4. Invitation

```
{"v": 1, "mesh", "device", "identity", "addr", "nonce", "expires_s", "name", "sig"}
```

`mesh` is the inviting mesh's id. `device` is the inviting device's id and must equal the `sha256:` device id of `identity`, its P-256 identity public key as unpadded base64url. `addr` is the device's address text, 1 to 1024 visible ASCII characters. `nonce` is 16 random bytes as unpadded base64url. `expires_s` is a positive integer of Unix seconds. `name` is the inviter's name under the rule of section 3.1. `sig` is that key's P-256 low-S signature, as unpadded base64url, over the bytes `astral.contact/v1`, a newline, and the LETS-CJ/1 bytes of the invitation without `sig`.

The text form is `astral-contact:v1:` followed by the unpadded base64url of the LETS-CJ/1 bytes. The link form is `https://astraldeep.github.io/contact/#` followed by the same text, so no server sees the payload. The QR encodes the link. A reader decodes either form, and the decoded bytes must equal the canonical bytes of the parsed value, so spaced, unsorted or duplicate-key JSON is refused.

Creation takes a lifetime of 60 to 86400 seconds, default 1800, otherwise `contact.invite_lifetime`. `expires_s` is the creation time plus the lifetime. An invitation is held in memory, has one use and is one of at most one open per device. Only a member of kind `desktop` or `pocket` creates or accepts one.

Checks on an invitation, first failure wins.

| Step | Check | Code |
|---|---|---|
| 1 | The text form, canonical bytes, the fields, their shapes, a 16-byte nonce, and `device` equal to the id of `identity` | `contact.invite_malformed` |
| 2 | The signature verifies against `identity`, low-S only | `contact.invite_bad_signature` |
| 3 | `mesh` is not the accepting device's own mesh | `contact.self` |
| 4 | The accepting device's clock in seconds is below `expires_s` | `contact.invite_expired` |

The accepting device dials the inviting device on `astral/share/1` with `hello` kind `contact`. Each side checks that the other's genesis id equals the claimed mesh, that the roster resolves after section 5.1's drop pass, and that the talking device and the invitation's device are current members. Each side then writes a `contact/` put with state `unverified`.

## 5. Roster digest and comparison code

### 5.1 The canonical roster

A device computes a roster's `heads` and `count` over the canonical roster of an entry set. The pass is: fold the entries with the roster v2 fold and no clock, remove every entry whose rejection code is `unverifiable` (section 6.1) and that no other entry follows, and fold again until none is left. The three structural codes `roster.malformed`, `roster.extra_genesis` and `roster.wrong_mesh` are removed even when followed, because the fold never resolves an entry that follows one of them. Any other unverifiable entry that an honest writer built on stays, as the Kernel's heads include rejected entries and honest writers follow them. Dropping it would leave a later removal pending and hide the removal from the marks. The result is the fold of the remaining entries. `count` is the number of remaining entries that resolve, which are the ones the fold does not list as pending, and `heads` is the sorted ids of those that no other resolved entry follows. Entries rejected for any other code stay and count. Entries whose `follows` names a missing entry are pending and do not count. Both sides apply the same pass to their own roster and to the one they receive, so the two sides agree on every digest.

A cache with no genesis whose id equals the mesh has no canonical roster (`roster.no_genesis`).

### 5.2 Digest and code

The roster digest is `crypto.object_id` of `{"count": <count>, "heads": <sorted head ids>}`, which is the `sha256:` id of its LETS-CJ/1 bytes.

The comparison code is the SHA-256 of the bytes `astral.contact.code/v1`, a newline, and the LETS-CJ/1 bytes of

```
{"nonces": [<the two nonces>], "sides": [{"mesh", "roster"}, {"mesh", "roster"}]}
```

The nonces are sorted by their decoded bytes, and each is written as unpadded base64url. The text order of two base64url strings can differ from the byte order of what they decode to, and the vectors pin a pair that does. The sides are sorted by `mesh` as text, and `roster` is the digest of that side's canonical roster. The invitation flow's nonces are the invitation nonce and the acceptor's `hello` nonce. A later re-verification uses the two `hello` nonces. Two equal nonces are `contact.malformed`, and the same mesh on both sides is `contact.self`.

The screen shows the first 8 bytes of the code as an unsigned big-endian integer modulo 10^12, written with 12 digits and leading zeros, in groups of four as `dddd dddd dddd`. The QR form carries all 32 bytes as unpadded base64url, and scanning the other screen compares them exactly.

When both people say the codes match, each owner approves with the presence key. The Kernel draws the prompt from the contact's name, mesh fingerprint and device count, and writes the verified put with the compared `heads` and `count`. A choice that the codes differ writes nothing.

## 6. Following a contact's roster

A verified contact's roster follows the verified state while the cache holds the entries it needs and nothing hostile appeared in it. A device keeps the contact's roster entries in its local cache. An entry an unknown peer offers is checked first and dropped if it is unverifiable and no honest entry follows it.

### 6.1 Rejection classes

Every entry the roster fold rejects falls in one of three classes by its code. The table lists the live codes of the Kernel's roster check, the load-time codes and the attestation codes that a `phi_approval` entry can raise.

| Class | Codes | Why |
|---|---|---|
| `unverifiable` | `roster.malformed`, `roster.unauthorized_author`, `roster.bad_signature`, `roster.bad_presence_signature`, `roster.author_removed`, `roster.recovery_not_allowed`, `roster.extra_genesis`, `roster.wrong_mesh` | The fold has not established that the mesh's own key wrote the entry. Several of these return before any signature check, so a stranger or a lost device can produce them. `roster.recovery_not_allowed` is also raised from the shape check before any signature. The entry changes no state, and it is dropped from the cache unless an honest entry follows it (section 5.1) |
| `honest` | `roster.unknown_device`, `roster.cutoff_incomplete`, `roster.unknown_authority`, `roster.epoch_wrong_members`, `roster.not_phi_member`, `roster.unknown_epoch`, `roster.resume_not_stopped`, `attestation.unknown_authority`, `attestation.expired` | A correctly signed entry that an honest writer produces. Each depends on state that can disappear from the author's view, as when a removed device's concurrent entry is voided and a later entry built on it loses its ground, or when an exclusion or an expiry changes the PHI group. Any code the table does not list is also `honest`, so a new fold code changes nothing until this contract lists it |
| `trigger` | `roster.already_member`, `roster.device_removed`, `roster.duplicate_key`, `roster.stop_not_rising`, `attestation.bad_signature`, `attestation.malformed` | A correctly signed entry that no conforming writer and no race can produce |

The fold checks each entry only against its own ancestors, so two concurrent entries never reject each other. Two devices adding one key at once are both accepted, and a vector pins that. A code that asks for something to be absent (`already_member`, `device_removed`, `duplicate_key`, `stop_not_rising`) cannot fail for an honest writer, because the writer checks its own view first and a voided ancestor only removes state. The two attestation codes cannot be produced by a device that verified the institution's statement before relaying it. The trigger set is not empty, so the downgrade stays defined.

### 6.2 The follow rule

For a verified record `r` and a cache `E` for `r.mesh`, `follow` computes the canonical roster of `E` and then decides.

| Step | Condition | Result |
|---|---|---|
| 1 | The cache has no genesis whose id equals the mesh | Not following, reason `roster.no_genesis` |
| 2 | Some entry outside the ancestors of `r.heads` is rejected with a `trigger` code | Not following, reason and downgrade `contact.roster_rejected` |
| 3 | A head named in `r.heads` is not in the canonical roster | Not following, reason `contact.heads_missing` |
| 4 | Otherwise | Following |

A trigger entry that is an ancestor of `r.heads` was in the roster the people compared, so it does not downgrade. Entries rejected as `unverifiable` or `honest` never decide anything. A downgrade is a `contact/` put with state `unverified`, written by the observing device. Grants to that contact stop being served until the owner verifies again (section 7).

Verification and the invitation exchange do not use this rule. They need the peer's genesis id to equal the claimed mesh and the roster to resolve after the drop pass, which is the canonical roster of section 5.1. A contact whose roster once raised a trigger can therefore be verified again, because the new comparison covers the current heads.

### 6.3 Notices

While following, `follow` lists what changed since the comparison, by comparing the canonical roster of the ancestors of `r.heads` with the full one. A device present now and not then is `device_added`. A device present then and not now is `device_removed`. An accepted entry authored by the recovery key outside those ancestors is `recovery_used`. Renames, rotations, epochs, wraps, stop and resume are silent. The contact stays verified in every case.

### 6.4 Result

`follow` returns `follows`, `reason`, `downgrade`, `dropped` (entry ids with their `unverifiable` code), `triggers` (entry ids with their code), `notices`, `roster` (`heads`, `count`, `digest`) and `mark`. The mark is `{"v": 1, "heads", "count", "removed"}` over the canonical roster, with `removed` listing every device the roster removed. A device that learns newer heads writes it as `contact.roster/<hex>` (section 3.3), and an owner device that never met the contact then refuses a removed contact device once any owner device has seen the removal.

### 6.5 Readers

`reader_check(mark, reader)` runs on a serving device in this order. A reader whose roster kind is not `desktop` or `pocket` is `share.reader_kind`. A reader whose device is in the merged `removed` is `share.device_removed`. A reader whose presented roster lacks an entry named in the merged `heads` is `share.roster_stale`. With no mark, only the kind is checked. A device that has seen no newer mark serves a removed contact device that presents its last valid roster. That is the freshness limit of 105 for removed devices, and the app states it.

## 7. Grants

```
{"v": 1, "id", "contact", "item": {"kind": "chat", "chat"}, "kind": "view", "phi", "supersedes", "ms"}
```

`id` is 32 lowercase hex digits and equals the key's digits. `contact` is a mesh id. `item.chat` is a chat id. `kind` is `view`. `phi` is a boolean. `supersedes` is null or the id of another grant. `ms` is a non-negative integer. A grant has a consent of kind `share` under the ten steps of section 3.2 with the prefix `share.`. Its body is `key`, `value` and `consent`, and a null value is `share.malformed`, because grants end by revocation and never by removal. A grant id is written once. After the consent checks pass, a later put of an id that already has an accepted put is `share.id_reused`, whatever its value. A put that was refused does not take the id.

An item with kind `surface`, `artifact` or `file` is `share.item_kind_unsupported`. Any other item that is not exactly a chat is `share.malformed`. The order is the record shape, then the item kind, then the consent.

The shares fold is an ordered pass.

1. Valid revocations are collected from every event, in any order. A revocation counts from a current or removed member's feed, otherwise `share.writer_not_member`, and its value must be exactly `{"v": 1, "ms": <integer>}`.
2. Valid grants are the ones that pass section 3.2 and take their id. They are taken in event order. A grant whose `supersedes` is not the id of an earlier accepted grant for the same contact and the same item is ignored with `share.supersedes_unknown`. Otherwise it makes its target superseded.
3. A grant is in effect when it is accepted, not revoked, not superseded, its contact has a record with state `verified`, and its chat is in `chats`. Revocation and supersession are terminal. A later put with a revoked id stays revoked, a revocation ordered before the put still counts, and a grant superseded by a grant that was later revoked stays dead. A superseding grant whose consent failed never entered step 2, so the older grant stands. A re-share is a new grant with a new id.
4. A grant whose contact has a record in state `unverified` is listed as waiting with `share.contact_unverified` and resumes after a new verification. It is not revoked. A grant whose contact has no record is listed as orphaned. A grant whose chat is missing is listed as `chat_missing`. If more than 1024 grants are in effect, the earliest 1024 stay and the rest are ignored with `share.too_many`.

The output has `in_effect` in event order of the accepted puts (each with `id`, `contact`, `chat`, `phi`, `event` and `approved_by`), `waiting`, `orphaned`, `chat_missing`, `revoked`, `superseded` (old id to new id) and `ignored`.

`propose_share` refuses an unverified or unknown contact with `contact.unverified`. PHI rules belong to the proposal and to serving, not to the fold. A chat that holds PHI can be granted only with `phi` true, which needs `phi_destination_allowed("contact", <mesh id>)` and the per-share presence confirmation. Those checks and their vectors live with the PHI destination contract and the share session.

## 8. Revocation and rotation

`revoke_share(id)` writes `share.revoked/<id>` first and then rotates the chat's keys. A revocation is terminal for that id. It needs no consent in the fold, as a revoke only narrows. A node refuses to revoke as host policy, because revoking is a user action that needs the presence approval a node cannot give, and the fold does not check this.

A rotation is the one primitive of the PHI destination contract, `rotate_chat_key(chat, group, reason)`, with reason `revoked`. It writes a `key.content` event with event scope `mesh`, body scope the chat id, and a header key under the group's current epoch, exactly as phi-destinations.md section 8 says. An event scoped to a chat would never be learned by deployed kernels, so the event scope is `mesh` and the chat id is in the body. The chat has one writing key per group, the latest by `(clock.ms, clock.n, feed, seq)`, and revocation rotates every group the writer can write. A writer outside the PHI group rotates the personal key only.

The revocation key orders after the `share.revoked` event. Both come from one feed, the revocation first, so any device that holds the new key also holds the revocation. No upgraded device can serve an event written under the new key to the revoked contact.

A new grant wraps every key of the chat that the serving device holds, so the contact reads that history again. A reader names its grant ids at its next session and the server answers with the revoked ones, and the reader deletes that item's events, chunks and wraps and shows "<name> stopped sharing <title>". A reader offline at revocation keeps its copy until then. The owner's app says before revoking that data the contact already has cannot be recalled.

Owner devices on a deployed kernel keep writing under the old key, which the contact holds. Those devices never serve shares, so this matters only if a copy reaches the contact by another path. The app's revoke card says so.

## 9. Consent digests

The digest of a contact or a grant is the `sha256:` id of the LETS-CJ/1 bytes of its value, and the consent is the 105 consent object `{v, kind, digest, device, clock, sig}` with `kind` equal to `contact` or `share`. `sig` is the presence key's P-256 low-S signature over `astral.consent/v1`, a newline and the LETS-CJ/1 bytes of the consent without `sig`. The Kernel draws each prompt from the record and never from model output. The two consent kinds are new in each of the three places the Kernel names its consent kinds. They are `Contact` and `Share` in the authorization crate and in the Kernel's consent module, and `ContactVerify` and `ShareGrant` in the host interface. A node never signs either, because Deep's node signer signs only the other kind and refuses these with no change.

## 10. Codes

| Code | Where |
|---|---|
| `contact.malformed`, `contact.self`, `contact.writer_not_member`, `contact.writer_kind`, `contact.too_many` | Fold, a contact or a mark |
| `contact.consent_missing`, `contact.consent_wrong_action`, `contact.consent_unknown_device`, `contact.consent_node_device`, `contact.consent_foreign_feed`, `contact.consent_bad_signature`, `contact.consent_stale`, `contact.consent_reused` | Fold, a verified put |
| `share.malformed`, `share.writer_not_member`, `share.item_kind_unsupported`, `share.supersedes_unknown`, `share.id_reused`, `share.too_many` | Fold, a grant or a revocation |
| `share.consent_missing`, `share.consent_wrong_action`, `share.consent_unknown_device`, `share.consent_node_device`, `share.consent_foreign_feed`, `share.consent_bad_signature`, `share.consent_stale`, `share.consent_reused` | Fold, a grant |
| `contact.invite_malformed`, `contact.invite_bad_signature`, `contact.invite_expired`, `contact.invite_lifetime` | The invitation |
| `contact.roster_rejected`, `contact.heads_missing`, `roster.no_genesis` | The follow rule |
| `contact.unverified`, `share.contact_unverified` | A refused proposal and a waiting grant |
| `share.reader_kind`, `share.device_removed`, `share.roster_stale` | A serving device |

`roster.*` and `attestation.*` codes in section 6.1 are classes of the roster fold's own codes. They are not codes of this contract, except `roster.no_genesis`. The codes of the share session, the wraps and the receiver steps belong to `share.md`.

## 11. Existing data and mixed versions

Existing data:

| Change | Existing data | Handling |
|---|---|---|
| `contact/`, `contact.roster/`, `share/` and `share.revoked/` settings keys | v2 stores on Mac 97, iPhone 99, the phase 1 to 5 stores and nodes | Additive. The settings and authority folds give identical results for all other keys |
| Rotation `key.content` with a chat id in its body | Every chat in existing stores | Written by the PHI destination contract's triggers and by revocation. Readers on every kernel learn it. Existing chats keep their `chat.create` key until then |
| The chat key writer rule | Existing chats with no rotation | Identical key choice, the `chat.create` key |
| Shared store files | None exist | New files only, never in a backup. The log store schema is unchanged |
| Plane `share_grants` rows | Servers and any personal node that minted links | Bytes untouched, not served on a personal node, served on a server as today |
| New ALPN `astral/share/1` | Deployed kernels | They refuse it with `net.unknown_alpn`. Member sync is unchanged |
| Watch relay | Installed watches | Unchanged packets |

Mixed versions:

| New thing | Deployed kernel | Contacts kernel |
|---|---|---|
| Contact and grant `settings.set` events | Accepted and folded as unread settings, and the feed keeps flowing | Folded |
| Consent kinds `contact` and `share` | Never read | Verified |
| Rotation `key.content` | Learned, so it reads new events. It keeps writing under the old key | Writes under the new key |
| `astral/share/1` | Not offered. A dial is refused | Served and read |
| Contacts and shares | Not listed | Listed |

A vector, `coexistence_deployed_folds`, runs the deployed `settings` and `authority` folds over a log with and without the four key families and pins equal results outside them.

## 12. Vectors

`vectors/contacts-v1.json` uses the vector format of the connection contract. `constants` holds the consent window, the invitation lifetimes, both caps, the owner mesh id used in the folds, the folding time and the code tables of section 6.1. `test_devices` publishes the roster v2 test devices `alice` (desktop), `bob` (pocket), `carol` (satellite), `node` (node), `dave` (a removed desktop) and the stranger `mallory`, with ids, feeds and the presence and identity scalars. They are public test values. No case reads a clock or the network.

| Kind | Input | Expected |
|---|---|---|
| `contact_key` | `key` | `form` or a code |
| `contact_record` | `id`, `value` | `digest` or a code |
| `contact_invite_make` | `identity_scalar_hex`, `mesh`, `name`, `addr`, `nonce`, `now_s`, `lifetime_s` | `invite`, `text`, `link` or a code |
| `contact_invite_check` | `text`, `now_s`, `own_mesh` | `invite`, `text`, `link` or a code |
| `contact_roster` | `mesh`, `entries` | `heads`, `count`, `digest`, `dropped` or a code |
| `contact_code` | `nonces`, `sides` of `mesh`, `heads`, `count` | `digests`, `code`, `display`, `qr` or a code |
| `contact_fold` | the folding input | `contacts`, `marks`, `ignored` |
| `contact_follow` | `mesh`, `record`, `entries` | The result of section 6.4, or a code |
| `contact_reader` | `mark`, `reader` of `device`, `kind` and `entries` | Empty or a code |
| `share_fold` | the folding input and `chats` | The output of section 7 |
| `share_propose` | the folding input and `contact` | Empty or a code |
| `contact_classify` | `code` | `class` |
| `contact_coexistence` | `events`, `presence_keys` | The deployed folds with and without the keys |

| Group | Cases |
|---|---|
| Keys and records | `key_*`, `record_*` |
| Invitation | `invite_*`, valid, expired at its second, own mesh, both signature failures, a high-S signature, a device that does not match its key, every missing and extra field, and each form of non-canonical JSON |
| Code and digest | `code_*` (equal on both sides, changed by either nonce, either mesh, one head or the count, leading zeros, a nonce pair whose text order differs from its byte order), `roster_digest_*` |
| Contact fold | `fold_*`, verified with and without consent, a node consent, each stale edge, a re-carried older verified put on another feed, on the same feed and after the window, a downgrade then a new verify, a removal, an approver that was removed, marks |
| Following | `follow_*`, a stranger's unverifiable entry, a valid add, removal, rename, stop and recovery-key add, one vector per trigger code in both the downgrade and the seen-at-verification form, one per honest code, one per unverifiable code, a voided ancestor, concurrent adds of one key, missing heads and no genesis |
| Readers | `reader_*` |
| Grants | `share_*`, in effect, to an unverified or unknown contact, revoked then put again, a revocation ordered first, supersede and its terminal cases, a missing chat, reserved item kinds, every consent failure, the cap |
| Classes | `class_*`, one per code and one unknown code |

## 13. Versioning

A change to a record, a key, a code or a rule raises the format version. This document, its vectors and its reference module change together, and the reference tests pass before the change merges.
