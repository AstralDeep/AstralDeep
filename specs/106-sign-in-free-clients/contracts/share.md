# Share session `astral.share/v1`

This contract fixes the second sync session kind. It admits a contact's device to read only the chats an owner granted to that contact. It covers the transport and messages, admission, which events a server may serve, the wraps of content keys, the share envelope, the receiver steps, the chunk policy, PHI and every code. The Kernel, the node and the Apple apps implement against this document and its vectors.

Contacts, grants, the roster digest and the follow rule come from [contacts.md](contacts.md). PHI destinations come from [phi-destinations.md](phi-destinations.md) and the label rules from [labels-v2.md](labels-v2.md). The member session `astral/sync/1`, its Kernel handler and the roster fold are not changed.

Reference code: `reference/share.py`. Generator: `reference/generate_share_vectors.py`. Vectors: `vectors/share-v1.json`. LETS-CJ/1, hashes, signatures, the wrap and the event body seal come from the preserved 105 reference. Rosters, the follow rule and the reader checks run through `reference/contacts.py`. The attachment chunk rule runs through the 105 attachments reference.

## 1. Transport

The ALPN is `astral/share/1`. A device that offers `astral/sync/1` offers it too, and a deployed kernel refuses it with `net.unknown_alpn`. iroh authenticates the peer's operations key. Each message is a frame, a 4-byte big-endian length followed by the canonical JSON of the message, as in the 105 sync contract. A frame over 8388608 bytes (8 MiB) is `share.too_large`. A length that does not match the data, text that does not parse, a duplicate key or a message that fails section 3 is `share.malformed`. A length field above the limit is `share.too_large` before any data is read.

A session has one kind, named in the first `hello`. The kinds are `contact`, `read`, `verify` and `nudge`. A `kind` field is never added to `astral/sync/1`, because deployed kernels reject an unknown `hello` field and it would put contact admission inside member sync.

## 2. Messages

Every message is an object with exactly the fields listed and a field `t` that names it. A message with another kind, a missing field or an extra field is `share.malformed`. A list over its limit is `share.too_large`, checked before the elements. The sender column says which side may send it. A reader is the contact's device and a server is the owner's device.

| `t` | Sender | Fields |
|---|---|---|
| `hello` | both | Section 2.1 |
| `roster_want` | both | `ids`: at most 256 entry ids, sorted, no duplicates |
| `roster` | both | `entries`: at most 256 roster entries as LETS-CJ/1 text |
| `grants_want` | reader | `grants`: at most 256 grant ids the reader holds, sorted, no duplicates |
| `grants` | server | `grants`: at most 256 grant entries (section 2.2) sorted by `id` |
| `want` | reader | `grant`, `ranges`: at most 64 of `{"feed", "from", "to"}` |
| `events` | server | `grant`, `events`: at most 256 share envelopes (section 6) |
| `chunks_want` | reader | `grant`, `hashes`: at most 256 chunk hashes, sorted, no duplicates |
| `chunks` | server | `grant`, `chunks`: at most 256 of `{"hash", "ct"}`, `withheld`: at most 256 of `{"hash", "reason"}` |
| `revoked` | server | `grants`: grant ids from the reader's `grants_want` that are revoked |
| `error` | both | `code`: one of the codes of section 12. The sender then closes |
| `bye` | both | none |

A range starts at 1, is ordered by feed and then by `from`, does not overlap the one before it and is at most 512 long (`share.malformed` for order or a bad bound, `share.too_large` for a span over 512). A chunk `ct` is unpadded base64url of the sealed chunk bytes. A withheld `reason` is `phi`, `unknown` or `secret`. A reader that wants to know which of its grants ended names them in `grants_want`, and the server answers with `grants` for the ones in effect and `revoked` for the ones revoked.

### 2.1 Hello

The fields of `hello` are `t`, `v` (1), `kind`, `mesh`, `device`, `roster`, `nonce` and the extra fields below.

| Kind | Extra fields |
|---|---|
| `contact` | `invite`, `name` |
| `read` | `phi` |
| `verify` | `name` |
| `nudge` | none |

`mesh` is the sender's mesh id and `device` its device id. `roster` is `{"heads": [...], "count": n}` with at most 64 sorted heads and a count at least as large as the number of heads. `nonce` is 16 random bytes as unpadded base64url. `invite` is the 16-byte nonce of the invitation the sender is answering. `name` follows the contact name rule of `contacts.md`. `phi` claims PHI group membership of the sending device and is checked in section 3.

### 2.2 Grant entry

A grant entry has exactly `id`, `chat`, `title`, `phi`, `withheld_phi`, `withheld_fields`, `withheld_keys`, `supersedes`, `heads` and `wraps`.

- `id` is the grant id and `chat` the granted chat id. `title` is the chat title, at most 256 characters and not containing characters the name rule refuses. It is display text from the owner's chat and untrusted.
- `phi` says whether this session serves PHI under this grant. It is true only when the grant has `phi`, the reader's `hello.phi` was confirmed and the destination answer of section 9 is true.
- `withheld_phi` counts events not served because they are PHI. `withheld_fields` counts events not served because of section 4.2. `withheld_keys` counts keys not wrapped because of section 5.
- `supersedes` is null or the id of the grant this one replaced. A reader that holds the older grant moves that item to the new id.
- `heads` maps each feed that has a served event to the highest served `seq`. It counts served events only.
- `wraps` is at most 64 of `{"key", "wrap"}` sorted by `key` (section 5). More is `share.too_many_keys`.

## 3. Sessions and admission

Until a peer is admitted, only `hello`, `roster_want`, `roster`, `error` and `bye` pass. After admission, a `contact`, `verify` or `nudge` session carries the same five and nothing else. A `read` session adds the messages of section 2 by their sender. Any other message, or one from the wrong side, is `share.unexpected_message`.

Before admission a receiver bounds the peer as the sync handler bounds an unknown peer: at most 4 rounds, at most 16 frames and 1048576 bytes in each round, and a 10 second deadline. More is `share.too_large`. A server holds its `hello` back until the peer is admitted, so a stranger learns no mesh id or roster head.

| Kind | The peer is admitted when | Checks, first failure wins |
|---|---|---|
| `contact` | A matching open invitation exists | The hello is well formed. Its mesh is not the local one (`contact.self`). `invite` equals the nonce of an invitation whose `expires_s` is after the local time (`share.invite_unknown`) |
| `verify` | The peer is a current member of a contact's roster | Well formed. Not the local mesh. A contact record exists in any state (`share.contact_unknown`). Every head the peer announced is in the roster the receiver holds after the exchange, otherwise the receiver sends `roster_want` and, if still missing, `share.roster_incomplete`. The roster has a genesis equal to the mesh and folds (`roster.no_genesis`). The `device` is a member and the transport key is one of its feeds (`share.peer_unknown`) |
| `read` | The same, and the contact is verified, and the roster follows, and the reader passes the reader checks | As `verify`, with the contact in state `verified` (`share.contact_unverified`). Then the follow rule of `contacts.md` section 6.2 (`contact.roster_rejected`, `contact.heads_missing`, `contact.roster_too_large`). Then the reader checks of section 6.5 of that contract in order: kind `desktop` or `pocket` (`share.reader_kind`), not in the merged `removed` (`share.device_removed`), roster holding every merged head (`share.roster_stale`) |
| `nudge` | As `read`, from a device that serves | As `read`, but the sender must be of kind `desktop`, `pocket` or `node` (`share.server_kind`) and the kind check of the reader is not made |

`verify` does not run the follow rule. Verifying a contact again is how a downgraded roster recovers, so it needs only a genesis equal to the mesh and a fold-valid roster. The mark is read only after the receiver has checked it against its own cache, as `contacts.md` says. A `nudge` carries no data. It tells the receiver to start a `read` session.

The reader's `phi` is the claim in `hello.phi` confirmed against the presented roster. A claim of `true` is kept only when the roster fold lists the device among its PHI members. A false claim is treated as `false` and is not an error. A claim of `false` is never raised.

## 4. Served types

The server decides per event. Deny by default.

| Served | Body fields served |
|---|---|
| `chat.create` | `title` |
| `chat.user_message` | `text`, `base`, `attachments`, `steered` |
| `chat.assistant_message` | `turn`, `text`, `notice` |
| `chat.spoken` | `kind`, `text`, `turn` |
| `chat.attachment` | `name`, `mime`, `size`, `sha256`, `chunks` |
| `chat.turn_start` | `continues`, `message`, `cancelled` |
| `chat.turn_end` | `turn`, `reason`, `code` |
| `chat.turn_outcome_unknown` | `turn`, `calls` |
| `ui.surface_put` | `document`, `turn`, `source` |
| `ui.patch` | `surface`, `ops` |
| `chat.tool_result` | Every field, and only when a served surface names it |

A `chat.tool_result` is served only when a served `ui.surface_put` or `ui.patch` of the same chat names its event id in a `$bind` anywhere in its body, or a served `ui.surface_put` names it in `source.result`. A surface that is itself not served, for any reason, names nothing. The server enforces this. A reader cannot check a binding event by event, and it shows a tool result only through a surface. A bound result discloses its whole body, because a binding can address any path in it.

Never served: `chat.model_request`, `chat.model_response`, `chat.tool_proposed`, `chat.tool_authorized`, `ui.action`, `request.send`, `request.claim`, `request.refuse`, `request.cancel`, every `auth.*` type, every `run.*` type, every other type, and every event whose scope is `mesh`.

### 4.1 The checks on one event

Each event is checked in this order, and the first failure keeps it from being served.

1. Scope is the granted chat, except a `chat.create` whose scope is `new` and whose event id equals the granted chat id (`share.scope_mismatch`).
2. The type is served (`share.type_not_allowed`).
3. The labels are known, and the confidentiality is not `secret` (`share.label_unknown`, `share.secret_refused`).
4. A `phi` event needs a session that serves PHI (`share.phi_withheld`).
5. The decrypted body has no field outside the list above (`share.body_field_refused`).
6. The content key named by the header is among the keys wrapped in this session (`share.key_unknown`).

### 4.2 Body fields

The body of a served event is its original sealed bytes, so the author's signed body hash still holds and nothing can be dropped on the wire. A field outside the list therefore cannot be removed. The server does not serve that event, and a reader refuses it after it has opened the body (`share.body_field_refused`). The lists come from the fields the kernel writes today. They leave out `request` and `action` on `chat.turn_start`, which name events that are never served. A turn whose start is not served leaves later events that name it with no start. A reader treats a `continues` or `turn` that names an event it does not hold as unknown and claims no continuity.

## 5. Keys and wraps

A chat's key set is the key the `chat.create` event names, introduced by a `key.content` whose body `scope` is `new` and whose id is that key, together with every `key.content` whose body `scope` is the chat id. The group of a key, `personal` or `phi`, is the group in the `ek:` key reference of the introducing event's header. The server holds only keys it can open. It wraps the keys it holds, never more.

A session wraps keys of the `personal` group, and keys of the `phi` group only when the grant entry has `phi` true. More than 64 eligible keys are cut to the 64 latest by `(clock.ms, clock.n, feed, seq)` of the introducing event, ties by key reference. The rest are counted in `withheld_keys`. An event whose key is not wrapped is not served, so the reader never meets `share.key_unknown` for it. A phi key left out for a session that serves no PHI is not counted in `withheld_keys`, because the events under it are counted in `withheld_phi`.

A wrap is `astral.wrap/v1` of the 32-byte content key to the reader device's X25519 wrap key from its roster entry. The associated data is the canonical JSON of `{"grant", "key", "purpose": "share", "recipient"}`, where `purpose` is `share`, `grant` is the grant id as 32 hex digits, `key` is the key reference and `recipient` is the reader's device id. A wrap that opens for another grant, key or recipient does not exist. A wrap that does not open is `wrap.decrypt_failed`. Wraps are made per session, never stored in the owner's log, and a reader stores them as received.

## 6. The share envelope

```
{"grant", "key", "feed", "seq", "header_ct", "body_ct"}
```

`grant` is the grant id. `key` is the content key reference of the header, in the form `ck:` and 22 characters. `feed` and `seq` copy the header. `header_ct` is unpadded base64url of a 24-byte random nonce and the XChaCha20-Poly1305 sealing of the LETS-CJ/1 bytes of the whole signed header. `body_ct` is unpadded base64url of the original body bytes of the event, nonce included, never null.

The header key is `HKDF-SHA256(ikm = the content key, salt = the 16 bytes of the grant id, info = "astral.share.header/v1")`, 32 bytes. The associated data is the canonical JSON of `{"feed", "grant", "key", "seq"}` from the envelope. A header sealed for one grant fails under another, because the salt and the associated data both differ. The body opens as in `event.md` section 6, under the content key and the associated data of the header.

## 7. Receiver steps

A reader takes these steps in order, and the first failure wins. The reader's context is the last `grants` message with its wraps, its own wrap key and the granting mesh's roster.

| Step | Check | Code |
|---|---|---|
| 1 | The envelope has exactly the fields, valid values and a body | `share.malformed` |
| 2 | The grant is known and in the last `grants` | `share.grant_unknown` |
| 3 | The key is among the grant's wraps, and the wrap opens | `share.key_unknown`, `wrap.decrypt_failed` |
| 4 | The header decrypts under the share header key | `share.decrypt_failed` |
| 5 | The header parses strictly: exactly the header fields, with values of the right type and the `prev` rule | `event.malformed` |
| 6 | The header's `key`, `feed` and `seq` equal the envelope's | `share.envelope_mismatch` |
| 7 | The signature verifies under `astral.event/v1` with the feed key | `share.bad_signature` |
| 8 | The feed is an operations key of a member of the granting roster, or of a removed member with `seq` at or below the cutoff for that feed. A feed with no cutoff entry has none | `share.feed_unknown` |
| 9 | Scope is the granted chat, except the `chat.create` of that chat | `share.scope_mismatch` |
| 10 | The type is in the served list | `share.type_not_allowed` |
| 11 | The labels are known and the confidentiality is not `secret` | `share.label_unknown`, `share.secret_refused` |
| 12 | The body hashes to the header's `body` | `event.body_mismatch` |
| 13 | The body decrypts under `event.md` section 6 | `event.decrypt_failed` |
| 14 | The body has no field outside the list of section 4 | `share.body_field_refused` |

Step 5 leaves an unknown type and unknown label values to steps 10 and 11, so those codes can be reached. The reader checks that the label values are known and does not check the header with the full 105 shape check.

Accepted events are ordered by `(clock, feed, seq)` and folded with the 105 chat fold. No continuity is claimed. The reader shows the item as the owner's devices chose to serve it. On ingest the reader sets every event's integrity to `untrusted`, whatever the header says, raises its confidentiality to at least `private`, and keeps the header's own labels for display only.

## 8. Chunks

A chunk is served only for a hash named by an attachment event that this session served, and never eagerly. The server answers `chunks_want` with the 105 attachment rule applied to the served attachment events only. A hash no served attachment names is withheld with reason `unknown`. A chunk of a `phi` attachment is sent only when the session serves PHI and is otherwise withheld with reason `phi`. A reader that receives a chunk it did not ask for, or whose hash no stored served attachment names, refuses it with `share.chunk_not_named`. A chunk whose bytes do not hash to its address is `attachment.chunk_hash`.

## 9. PHI

Three separate gates decide whether a session serves PHI. Each must hold.

1. The grant has `phi` true. A chat that holds PHI can be granted only that way, under the proposal rules of `contacts.md` section 7.
2. The reader's `hello.phi` is true and confirmed by the roster as in section 3.
3. The destination answer `phi_destination_allowed("contact", <mesh id>)` of phi-destinations.md is true for the contact. A withdrawn destination stops PHI under every grant to that contact at once, and other events keep flowing. The caller supplies the answer, and the reference takes it as an input.

When any gate fails, `phi` in the grant entry is false, no PHI event is served, no phi key is wrapped, and `withheld_phi` counts the PHI events the chat holds. A `phi` grant for a reader outside the PHI group is therefore served like a non-PHI grant, and the reader shows that some of the chat is not shared. The shared store of the reader holds ciphertext and wraps only. A reader whose mesh has adopted the PHI group holds the phi epoch and opens a phi key wrap and a PHI event under it.

## 10. Liveness and revocation

A reader pulls when the item view opens, then every 60 seconds while the app is in the foreground. A server sends a `nudge` session to reachable reader devices at most once per grant per 10 seconds after a served event is added. Both are best effort. There is no mailbox path in version 1.

At its next session the reader names its grant ids in `grants_want`. The server answers `revoked` with the named ids that are revoked and `grants` with the ones in effect. The reader deletes a revoked item's events, chunks and wraps. A grant that was superseded is neither revoked nor deleted. A reader offline at revocation keeps its copy until then.

## 11. Existing data and mixed versions

| Change | Existing data | Handling |
|---|---|---|
| ALPN `astral/share/1` | Deployed kernels | They refuse it with `net.unknown_alpn`. Member sync is unchanged |
| Share envelopes, wraps and the grants message | None exist | New messages on a new ALPN |
| The reader's shared store | None exist | A new file per contact mesh that holds ciphertext, wraps and the roster cache, never a backup, and the log store schema is unchanged |

A contacts kernel serves only grants in effect. A deployed kernel serves none, and a contact that dials it learns nothing beyond the refusal.

## 12. Codes

The codes of the grants and revocation records belong to `contacts.md`. This table adds the session codes. `share.malformed` is the one code both contracts use, for a record there and a message here.

| Code | Where |
|---|---|
| `share.malformed`, `share.too_large`, `share.unexpected_message` | Framing, messages and which message may be sent |
| `share.invite_unknown`, `share.contact_unknown`, `share.contact_unverified`, `share.roster_incomplete`, `share.peer_unknown`, `share.server_kind`, `contact.self` | Admission |
| `share.reader_kind`, `share.device_removed`, `share.roster_stale` | Admission, the reader checks of `contacts.md` |
| `contact.roster_rejected`, `contact.heads_missing`, `contact.roster_too_large`, `roster.no_genesis` | Admission, the follow rule and the roster of `contacts.md` |
| `share.phi_withheld`, `share.body_field_refused`, `share.too_many_keys` | Serving and the grant entry |
| `share.chunk_not_named`, `attachment.chunk_hash` | Chunks |
| `share.grant_unknown`, `share.key_unknown`, `wrap.decrypt_failed`, `share.decrypt_failed`, `event.malformed`, `share.envelope_mismatch`, `share.bad_signature`, `share.feed_unknown`, `share.scope_mismatch`, `share.type_not_allowed`, `share.label_unknown`, `share.secret_refused`, `event.body_mismatch`, `event.decrypt_failed` | The receiver steps |

## 13. Vectors

`vectors/share-v1.json` uses the vector format of the contacts contract. `constants` holds the ALPN, the frame limit, the header info text, the key cap, the pre-admission limits, the owner mesh id, the folding time, the chat id, the grant ids, the invitation nonce, the served types and fields, the receiver steps and the codes. `test_devices` publishes the owner devices and the reader mesh devices with ids, kinds, feeds and wrap keys. They are public test values. No case reads a clock or the network. The owner chat in the vectors is built from signed and sealed events.

| Kind | Input | Expected |
|---|---|---|
| `share_message` | `message` | `t` or a code |
| `share_frame`, `share_unframe` | `message`, or `hex` | `hex`, or the message, or a code |
| `share_allowed` | `kind`, `admitted`, `sender`, `t` | Empty or a code |
| `share_pre_admission` | `rounds` of `frames` and `bytes` | Empty or a code |
| `share_admit` | `kind`, `hello`, `peer_feed`, `view` | `admitted`, `device`, `member_kind`, `phi` or a code |
| `share_select` | `chat`, `events`, `phi`, `wrapped` | `served`, `refused`, `withheld_phi`, `withheld_fields`, `heads` |
| `share_keys` | `chat`, `create`, `key_events`, `phi` | `keys`, `wrapped`, `withheld_keys` |
| `share_grants` | `reader`, `grants`, `chats`, `destination_allowed`, `rng` | `message`, `served`, `refused` |
| `share_receive` | `envelope`, `context` | The accepted event or a code |
| `share_header_key` | `content_key_hex`, `grant` | `key` |
| `share_open_wrap` | `grant`, `key`, `recipient`, `wrap_private_hex`, `wrap` | `secret` or a code |
| `share_want`, `share_revoked` | `held` and `ranges`, or `named` and `fold` | `ids` and `more`, or `revoked` |
| `share_chunks`, `share_chunks_receive` | `attachments`, `requested`, `phi`, or `message`, `named`, `requested` | `send` and `withheld`, or `kept` and `withheld`, or a code |

| Group | Cases |
|---|---|
| Messages | `message_*`, every kind valid, every missing and extra field, each limit at and over its value, and the frame forms |
| Admission | `allowed_*`, `pre_admission_*`, `admit_*`, each kind, each reader kind, the mark, the follow rule, the PHI claim and verification of a downgraded roster |
| Selection | `select_*`, every event type, tool results with and without a binding, scope, labels, body fields, wrapped keys and PHI |
| Keys and grants | `keys_*`, `grants_*`, the cap, the phi key and the withdrawn destination |
| Envelope | `receive_*`, one case for each step and one for each adjacent pair of steps, the cutoff of a removed device, a PHI event read by a PHI member and the other grant's key |
| Wraps, ranges, chunks | `wrap_*`, `header_key_*`, `want_*`, `revoked_*`, `chunks_*` |

The limits that need many elements, 257 envelopes, grant entries or chunks and a frame over 8 MiB, are checked in `reference/tests/test_share.py` instead of the vectors.

## 14. Versioning

A change to a message, a limit, a list of served types or fields, a derivation or a rule raises the format version and the ALPN. This document, its vectors and its reference module change together, and the reference tests pass before the change merges.
