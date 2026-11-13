# Attachment contract, `astral.attachments/v1`

An attachment is a file that belongs to a chat. This document fixes the event that names it, how its bytes are cut into chunks, sealed and addressed, which files and sizes are allowed, how chunks reach other devices, and how labels, device removal and key changes affect them.

Reference code: `reference/astral_mesh_ref/attachments.py`. Vectors: `attachments.json`.

## 1. The event

`chat.attachment` has the family `chat` and a chat scope. A scope of `mesh` is `event.malformed`. The body is encrypted under the chat's content key like any chat event (`event.md` section 6). The body has exactly these fields.

| Field | Rule |
|---|---|
| `name` | 1 to 255 characters. No `/` or `\`, no control characters, and not `.` or `..`. The name is a label for people. It is never used as a path |
| `mime` | One of the types in section 3, written exactly as listed |
| `size` | Integer from 1 to 16777216, the length of the content in bytes |
| `sha256` | The hash of the whole content (`event.md` section 2) |
| `chunks` | Array of hashes, one per chunk in order, of the sealed chunk bytes (section 2). Its length is `ceil(size / 262144)` and no hash repeats |

Event bodies carry no floats, so `size` is an integer. A `size` that is a float, a string or a boolean is `attachment.malformed`.

The event id is the attachment's handle. The text form is `res:<event id>`, the same form as a tool result handle (section 7).

## 2. Chunks

The content is cut into chunks of exactly 262144 bytes. The last chunk holds the rest and is 1 to 262144 bytes long. A content of at most 262144 bytes is one chunk. The largest attachment has 64 chunks.

Each chunk is sealed with XChaCha20-Poly1305 under the content key the attachment event's header names in `key` (the chat's content key, `keys.md` section 4).

- The nonce is 24 random bytes, new for every chunk.
- The associated data is the canonical JSON of `{"chat": <chat id>, "index": <chunk index from 0>, "key": <key reference>, "purpose": "attachment"}`. It binds a chunk to its chat, its position and its key, so a chunk is useless anywhere else.
- The stored bytes, called `chunk_ct`, are the nonce followed by the ciphertext and its 16-byte tag. The length of `chunk_ct` is the plaintext length plus 40.
- The chunk's address is the hash of `chunk_ct`.

An attachment event is written after its chunks are sealed. The chunk hashes in the body are its only link to them, so a reader needs only the event to know every chunk it must fetch and exactly how long each one is.

## 3. Allowed content

A device refuses to write or accept an attachment outside these rules.

| Rule | Value |
|---|---|
| Types | `application/json`, `application/pdf`, `image/gif`, `image/jpeg`, `image/png`, `image/webp`, `text/csv`, `text/markdown`, `text/plain` |
| Parameters | None. A type with a charset or other parameter is not in the list |
| Size | 1 byte to 16777216 bytes (16 MiB) |
| Attachments on one message | At most 8 |
| Image shown in a surface | At most 1048576 bytes (section 7) |

The type is declared by the writer. A device that renders an attachment checks the content against the declared type before it draws anything.

| Failure | Error |
|---|---|
| A wrong shape, a size that is not an integer, a bad hash, a chunk hash that repeats, or a message whose attachment list is not a list of distinct ids | `attachment.malformed` |
| The name breaks the rule above | `attachment.bad_name` |
| The type is not in the list | `attachment.mime_not_allowed` |
| A size of 0 or less, or no content | `attachment.empty` |
| A size above 16777216 | `attachment.too_large` |
| The number of chunk hashes differs from `ceil(size / 262144)` | `attachment.chunk_count` |
| The event's confidentiality is `secret` | `attachment.label` |
| More than 8 attachments on one message | `attachment.too_many` |

## 4. Labels and PHI

The attachment event carries labels like every event, and its chunks carry the same label. The confidentiality of an attachment is `public`, `private` or `phi`. A `secret` attachment is refused, because a secret never leaves its device (`labels.md` section 6).

A device that writes an attachment into a chat gives it the join of the chat's labels (`labels.md` section 2). A chat that holds PHI therefore gets PHI attachments. An attachment whose label is `phi` is written under a content key that was introduced under a phi epoch (`keys.md` section 4), so a device outside the phi group cannot decrypt it even if it holds the bytes.

A chunk is a ciphertext object addressed by hash (`sync.md` section 5). The peer decides whether to send it from the label of the attachment event that names it:

| Label of the attachment | The peer is in the PHI group | The peer is not |
|---|---|---|
| `public` or `private` | send | send |
| `phi` | send | withhold, reason `phi` |
| `secret` | withhold, reason `secret` | withhold, reason `secret` |
| No attachment event names the hash | withhold, reason `unknown` | withhold, reason `unknown` |

When two attachment events name one hash, the more confidential label decides. A peer outside the PHI group receives the header of a PHI attachment event and never its body, so it does not learn its chunk hashes, its name or its size. The reference result is `{"send": [...], "withheld": [...]}` in the order the hashes were asked.

## 5. How chunks sync

Chunks are never part of the eager set of a sync session. Eager sync (`sync.md` section 3, step 5) carries event bodies, and an attachment event's body is only the small list of hashes above. A device fetches a chunk only when a person or a surface needs the content.

1. The device finds the attachment event and opens its body. If the body is absent, withheld (`phi`) or its key is unknown, the content is unavailable. The error is `attachment.unavailable`.
2. It checks the body (section 1) and notes which chunk hashes it already holds.
3. It asks for the missing hashes in `bodies_want`, at most 256 per message. The peer answers with `bodies` and `withheld` as for any body hash. A sender splits its answer so no message passes the frame limit.
4. It checks every received chunk before it keeps it (section 6) and drops a chunk that fails.
5. When every chunk is held, it opens them in order and checks the whole (section 6).

A chunk that arrives for a hash the device did not ask for, or that no attachment event of this device names, is refused with `attachment.chunk_unreferenced`. A device keeps asking for what is still missing at its next session with another peer. A fetch that no peer can satisfy ends with `attachment.chunks_unavailable` and names how many chunks are still missing. It is never reported as done.

A device holds at most one chunk and the assembled content in memory while it opens an attachment. The content is at most 16777216 bytes.

## 6. Verifying

A receiver checks each chunk in this order, and the first failure ends the check.

| Step | Check | Error |
|---|---|---|
| 1 | The chunk is present | `attachment.chunk_missing` |
| 2 | The hash of `chunk_ct` equals the hash the body names at that position | `attachment.chunk_hash` |
| 3 | The length of `chunk_ct` is exactly the plaintext length of that position plus 40 (262184 for every chunk but the last) | `attachment.chunk_size` |
| 4 | When the content key is held, the chunk opens under the associated data of section 2 | `attachment.chunk_decrypt` |

A receiver that holds no key yet checks steps 1 to 3 and keeps the chunk. After the last chunk the receiver checks that the opened bytes are `size` long and hash to `sha256` (`attachment.content_mismatch`). A writer that lied in `sha256` or `size` is caught here, because every chunk can be valid and the whole still wrong.

## 7. Using an attachment

**In a message.** A `chat.user_message` body may carry `attachments`, an array of at most 8 distinct attachment event ids. A reader shows an id that does not name a `chat.attachment` of the same chat as unavailable and never follows it. A writer refuses such an id.

**In a surface.** A handle `res:<event id>` that names a `chat.attachment` resolves to an image when the type is `image/png`, `image/jpeg`, `image/gif` or `image/webp`, the size is at most 1048576 bytes and the content is held or fetched. Other types and larger sizes are refused with `handle.refused` and `handle.too_large`. An image stays inside the mesh. The resolver never fetches a URL, and a surface cannot name an attachment of another chat.

## 8. Removal, rotation and loss

- An attachment event is never deleted. The log is append-only, so an owner who does not want an attachment shown does not reference it.
- A device may delete the chunk bytes it holds to free space and fetch them again later. The event and the chunk hashes stay.
- Removing a device starts new key epochs (`keys.md` section 2). The removed device keeps the chunks and keys it already held. It is no longer a member, so no peer sends it a chunk (`sync.not_a_member`).
- Chunks are never re-encrypted. A chunk's key reference is the one in its attachment event's header, and the associated data binds it. If a chat later gets another content key, old chunks stay readable with the key they were sealed under, which every device that can read the chat holds.
- A device that restores from a backup (`keys.md` section 6) holds the attachment events but no chunks unless the backup was written with them. It fetches the chunks it needs on demand.

## 9. Errors

`attachment.malformed`, `attachment.bad_name`, `attachment.mime_not_allowed`, `attachment.empty`, `attachment.too_large`, `attachment.chunk_count`, `attachment.label`, `attachment.too_many`, `attachment.chunk_missing`, `attachment.chunk_hash`, `attachment.chunk_size`, `attachment.chunk_decrypt`, `attachment.content_mismatch`, `attachment.chunk_unreferenced`, `attachment.unavailable`, `attachment.chunks_unavailable`.

The reference implements the body checks, sealing, chunk and whole-content verification, and the rule for which peers receive a chunk. The last three codes are decisions the kernel makes from its store and its peers. They are not computed in the reference.

## 10. Operations in the vectors

| Operation | Input | Result |
|---|---|---|
| `attachment_check` | `body`, `labels` | `chunks`, `mime`, `size` |
| `attachment_message_check` | `body` | `attachments`, the count |
| `attachment_seal` | `content` (`label`, `length`), `key_hex`, `key`, `chat`, `nonces`, optional `name`, `mime`, `labels` | `body` and `lengths`, the length of each sealed chunk |
| `attachment_receive` | the inputs of `attachment_seal`, `index`, optional `tamper`, `key_held`, `wrong_key`, `open_chat` | `decrypted` and `length` |
| `attachment_open` | the inputs of `attachment_seal`, optional `tamper`, `wrong_key`, `open_chat` | `length` and `sha256` of the opened content |
| `attachment_serve` | `attachments` (event id to `labels` and `chunks`), `requested`, `peer_in_phi_group` | `send` and `withheld` |

The content of a case is the stream `rng("attachment/" || label)` cut to `length` bytes, where `rng` is the stream in the README conventions. The nonce of chunk `i` is bytes `24 * i` to `24 * i + 24` of the stream `rng("attachment-nonce/" || nonces)`. A `tamper` step is one of `flip` (`chunk`, `offset`, optional `rehash`), `truncate` (`chunk`, `to`, optional `rehash`), `drop` (`chunk`), `swap` (`chunks`) and `forge_sha256`. `rehash` rewrites the hash in the body so the edit reaches the later checks.
