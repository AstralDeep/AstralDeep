# Sync contract, `astral.sync/v1`

Peers exchange events over the transport ALPN `astral/sync/1`. This document fixes the messages, the order of a session, how received events are accepted, how bodies are fetched and withheld, checkpoints, fork and shortened-feed evidence, and the rule for a restored device.

Reference code: `reference/astral_mesh_ref/sync.py`. Vectors: `sync.json`, `feed.json`.

## 1. Transport

The transport is iroh with the ALPN `astral/sync/1`. A device's iroh endpoint key is its operations key, so the transport authenticates the peer's operations key. A receiver looks up that key in the roster and refuses a peer that is not a member.

A session uses one bidirectional stream per request round. Each message is a frame: a 4-byte big-endian length, then the canonical JSON of the message. A frame over 8388608 bytes is `sync.too_large`. A length that does not match the data, or a message that does not parse, is `sync.malformed`. Duplicate keys in a message raise `canonical.duplicate_key`.

Direct, relayed and mailbox paths carry the same messages and envelopes. The mailbox's own request authentication and the lost-device removal notice are a later contract. Mailbox requests will need a signing context of their own.

## 2. Messages

Every message is an object whose field names are exactly those listed. The field `t` names the kind. Any other kind or any other field raises `sync.malformed`.

| `t` | Other fields | Meaning |
|---|---|---|
| `hello` | `v` (1), `mesh`, `device`, `roster` (`{"heads": [ids], "count": int}`), `stop`, `clock`, `phi` (bool) | Opens a session. `phi` claims PHI group membership. The receiver verifies it against the roster |
| `heads` | `feeds` (feed id to `{"seq", "id"}`), `checkpoints` (array) | The newest event the sender holds for each feed, and any countersigned checkpoints it knows |
| `want` | `ranges` (array of `{"feed", "from", "to"}`), `roster` (array of entry ids) | Asks for events and roster entries |
| `roster` | `entries` | Roster entries |
| `events` | `events` | Wire envelopes (`event.md` section 7) |
| `bodies_want` | `hashes` | Asks for ciphertext objects by hash |
| `bodies` | `bodies` (array of `{"hash", "ct"}`), `withheld` (array of `{"hash", "reason"}`) | Ciphertext objects and the ones withheld. `reason` is `phi`, `secret` or `unknown` |
| `checkpoint` | `checkpoint` | One checkpoint (section 6) |
| `fork` | `evidence` | Fork evidence (section 7) |
| `error` | `code` | A refusal. The sender then closes |
| `bye` | none | Ends the session |

Limits: at most 64 ranges per `want`, each range from 1, ordered, and at most 512 long. At most 256 roster ids, 256 hashes and 256 envelopes in one message. Larger raises `sync.too_large`. The `hello` version must be 1 and its ids hashes.

## 3. A session

1. Each side sends `hello`. A side refuses with `error` (`sync.wrong_mesh`) if the mesh differs. It refuses with `sync.not_a_member` if the peer's device is not on its roster after step 2.
2. If a roster head in the peer's `hello` is unknown, the side sends `want` with those entry ids and the peer replies with `roster`. The side folds the entries (`roster.md`) and repeats until it knows every head. A side that sees a higher `stop` than its own does not act on it until the stop entry arrives.
3. Each side sends `heads`. The receiver computes the ranges it lacks with the rule below, and sends `want`.
4. The peer replies with `events`. Each is accepted by the pipeline in section 4.
5. The receiver collects the body hashes it needs by policy, which means recent and pinned chats eagerly and others on demand, and sends `bodies_want`. The peer replies with `bodies`.
6. Either side may send `checkpoint` at any time. Either side ends with `bye`.

**What each side asks for.** For every feed in the peer's `heads`, if the peer's `seq` is above the local `seq`, ask for the range from local plus one up to the peer's, split into ranges of at most 512, and at most 64 ranges per message. Because each feed is a chain, this is exact. A side that is behind a peer's feed by more than it asked for sends another `want` afterwards. A peer answers a `want` with the events it holds in the range, at most 256 per message, in order.

## 4. Accepting events

For each envelope, in order:

1. Open the header (`event.md` section 7). Errors: `event.malformed`, `event.epoch_unknown`, `event.decrypt_failed`, `event.bad_signature`, `event.future_clock`, `event.envelope_mismatch`. A missing epoch key is not a failure of the sender. The receiver keeps the envelope aside and asks any member for a `wrap` entry.
2. Ask the roster for the feed's status and run the chain steps of `event.md` section 8. Errors: `feed.unknown`, `feed.removed`, `feed.forked`, `feed.fork`, `feed.gap`, `feed.clock_regress`, `feed.after_clock`.
3. On `feed.gap`, keep the header aside and ask for the missing range. Retry when it arrives.
4. On `feed.fork`, mark the feed forked and send `fork` evidence to the owner's other peers.
5. If the envelope has a body, check its hash against the header (`event.body_mismatch`). Decrypt it if the key is held.

Nothing from a rejected envelope enters any fold.

## 5. Bodies by hash

A ciphertext object, which is an event body or an attachment chunk, is requested by the hash of its bytes. A sender decides per object, from the event's labels:

| Confidentiality of the event | The peer is in the PHI group | The peer is not |
|---|---|---|
| `public` or `private` | send | send |
| `phi` | send | withhold, reason `phi` |
| `secret` | withhold, reason `secret` | withhold, reason `secret` |
| hash unknown | withhold, reason `unknown` | withhold, reason `unknown` |

A peer outside the PHI group holds the header only. It can verify the chain and shows that the content lives on approved devices. The reference result is `{"send": [...], "withheld": [...]}`. A peer verifies that a received `phi` claim in `hello` matches the roster's PHI members and otherwise treats the peer as not in the group.

## 6. Checkpoints

A **checkpoint** is an object with exactly `v` (1), `mesh`, `feed`, `seq`, `head` (the event id at that position), `by` (the countersigning device id), `clock` and `sig`. The signature is by the identity key of `by` under the checkpoint context.

A device countersigns a feed's head when it syncs and holds that head. Peers keep checkpoints.

Verification takes the identity key and the mesh id: exact fields and `seq` of at least 1 and `head` a hash (`checkpoint.malformed`), the mesh (`checkpoint.wrong_mesh`), the key belongs to `by` (`checkpoint.wrong_signer`), the signature (`checkpoint.bad_signature`). Then against the held feed the status is one of:

- `consistent`: the held event at `seq` has the id `head`.
- `behind`: fewer than `seq` events are held.
- `forked`: the feed is already marked forked.
- an error `checkpoint.contradicts_feed`: a different event is held at that position. This is evidence of a fork or a forged history.

**A shortened feed.** A device cannot prove another device withheld its newest events. A peer can detect a feed shorter than a checkpoint it has countersigned. When a peer's `heads` claims, for a feed, a `seq` below a held checkpoint or the same `seq` with a different id, the result is evidence `{checkpoint, claimed_seq}` or `{checkpoint, claimed_id}`. The peer stops treating the sender's claim as the feed's head and tells the owner.

## 7. Fork evidence

A fork is two headers at one position, or a header whose `prev` differs from the held head. The evidence is `{"feed", "a", "b"}` with both headers. Peers keep both, accept nothing more from the feed, and ask the owner to re-enroll the device (remove it and add it again as a new device). Honest forks are avoided by the next section.

## 8. A restored device

A phone restored from a backup would reuse sequence numbers it already used. The kernel keeps a marker in storage that backups do not carry. When the marker is missing at start-up the kernel does not continue its old feed. It makes a new operations key, which is a new feed, writes a `rotate` roster entry (same identity key) or enrolls again as a new device if its identity key is gone, and starts the new feed at sequence 1. The old feed stays as history and is never written again.

If two different events ever appear at one position anyway, peers handle it as a fork.

## 9. Honest state

Every client shows what it knows for each peer: synced, waiting for a named device, or last seen at a time. A request that went to an absent peer is shown as waiting, never as done. The inputs to that state are the last session time of each device and the list of feeds whose heads a peer has announced but that the local peer has not yet received.

## 10. Errors

`sync.malformed`, `sync.too_large`, `sync.wrong_mesh`, `sync.not_a_member`, `checkpoint.malformed`, `checkpoint.wrong_mesh`, `checkpoint.wrong_signer`, `checkpoint.bad_signature`, `checkpoint.contradicts_feed`, and the `event.*` and `feed.*` errors of `event.md`. The reference implements the message checks, framing, planning, body policy, checkpoints and shortened-feed evidence. The error `sync.wrong_mesh` and `sync.not_a_member` are session-level decisions the kernel makes from the roster and are not computed in the reference.
