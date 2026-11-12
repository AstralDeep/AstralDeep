# Mailbox contract, `astral.mailbox/v1`

A mailbox holds wire envelopes for peers that are never online at the same time, and it holds the notices that must wait for a device that comes online later. It is blind. It sees device public keys, sizes, timing and counts. It never sees an event body, an event header, a roster entry other than the three allowed in section 7, a label, a chat or a key that opens anything.

Peers reach a mailbox over the transport ALPN `astral/mailbox/1`. The mailbox is an iroh endpoint with an Ed25519 key. Its endpoint id is that public key, written as unpadded base64url, and it is the `mailbox` field of every request.

Reference code: `reference/astral_mesh_ref/mailbox.py`. Vectors: `mailbox.json`.

## 1. Transport and framing

One request uses one bidirectional stream. The client sends one frame and the mailbox answers with one frame. A frame is a 4-byte big-endian length followed by the canonical JSON of the message, as in `sync.md`. A frame over 8388608 bytes is `mailbox.too_large`. A frame that does not parse, or has duplicate keys, is `mailbox.malformed`. A connection may carry many requests, one per stream.

## 2. Signing

The context is `astral.mailbox/v1`. A signature covers the context, a newline, and the canonical JSON of the object without its `sig` field, as in `event.md`. Requests are signed with the Ed25519 operations key of the requesting device. Membership statements are signed with a member's presence key (P-256) or with the recovery signing key (Ed25519).

The operations key is the same key the transport authenticates. The mailbox checks that the two agree, and the signature also binds the request to this mailbox and this moment.

## 3. Requests

A request is an object with exactly these fields.

| Field | Rule |
|---|---|
| `v` | `1` |
| `op` | `members`, `deposit`, `fetch`, `notice_put`, `notice_get` or `status` |
| `mesh` | The mesh id (a hash) |
| `mailbox` | The mailbox's key |
| `device` | The requester's operations key |
| `ts` | Seconds since the epoch |
| `body` | An object that depends on `op` (section 5) |
| `sig` | Ed25519 signature by `device` |

The mailbox checks a request in this order and stops at the first failure.

1. Shape of the fields, then of the body (`mailbox.malformed`, or `mailbox.too_large` for counts above a limit).
2. `mailbox` is this mailbox's key (`mailbox.wrong_audience`).
3. `ts` is within 300 seconds of the mailbox's clock (`mailbox.stale`).
4. `device` is the key the transport authenticated (`mailbox.wrong_peer`).
5. The signature (`mailbox.bad_signature`).
6. For every op but `members`: the mesh is known (`mailbox.unknown_mesh`). A device that was removed gets `mailbox.removed`. Any other device that is not listed gets `mailbox.not_a_member`.
7. The rules of the op.

A response is an object `{"v": 1, "ok": true, "result": {...}}` or `{"v": 1, "ok": false, "code": "<dotted code>"}`. Responses are not signed. The client reached the mailbox by its key, so the transport authenticates the answer.

## 4. How the mailbox learns who may use it

The roster is never sent to a mailbox. A device sends a **membership statement** instead. It names only public keys.

A statement is an object with exactly these fields.

| Field | Rule |
|---|---|
| `v` | `1` |
| `kind` | `members` |
| `mesh` | The mesh id |
| `seq` | Integer, at least 1 |
| `members` | 1 to 64 objects `{"ops": key, "presence": key}`, sorted by `ops`, no duplicate `ops` or `presence`. `ops` is 32 bytes. `presence` is a 65-byte uncompressed P-256 point |
| `recovery` | The recovery signing key from the genesis entry (32 bytes) |
| `by` | The signer's key. A 65-byte presence key when `via` is `presence`, the recovery key when `via` is `recovery` |
| `via` | `presence` or `recovery` |
| `sig` | The signature by `by` |

The statement changes who may deposit and fetch. It therefore needs the same proof as a roster change. A presence key is released by biometrics on hardware that can enforce it, so a thief holding a stolen device's operations key cannot remove the other devices from the mailbox. The recovery key covers a mesh whose devices are all lost, or removed each other.

Devices compute the member list from their roster after every merge. A device that finds the mailbox's list differs from its own sends a statement with a higher `seq`.

Rules, checked in order after the signature.

- `mesh` equals the request's `mesh` (`mailbox.wrong_mesh`).
- **A new mesh.** `seq` is 1 (`mailbox.members_seq`). `via` is `presence`, `by` is the presence key listed for the requesting device's operations key (`mailbox.members_unauthorized`). If the operator limits who may register, the requester is on that list (`mailbox.admission_denied`). The mailbox holds fewer meshes than its limit (`mailbox.quota_exceeded`). The mailbox records the `recovery` key.
- **A known mesh.** `recovery` equals the recorded key (`mailbox.recovery_mismatch`). The signer is allowed: for `presence`, `by` is the presence key of a current member. For `recovery`, `by` equals the recorded key (`mailbox.members_unauthorized`). Then `seq` is above the current one (`mailbox.members_seq`). A statement with the current `seq` and the same member list is accepted and changes nothing.
- On success the list replaces the old one. A key that left the list is remembered as removed. A key that returns leaves that memory. A new member starts with nothing collected.

**First registration is trust on first use.** The mailbox cannot check that a mesh id is the hash of a genesis entry, because it never sees one. A device registers its mesh as soon as it creates it, before it shows any invite. An outsider who learned a mesh id earlier than that could register it first. The mesh then cannot use that mailbox, and nothing else is lost. An operator who cares closes registration with the admission list.

## 5. Operations

The body of each op, and what the mailbox does.

**`members`.** Body `{"statement": ...}`. Result `{"seq": n}`. Section 4.

**`deposit`.** Body `{"envelopes": [...]}` with 1 to 64 wire envelopes (`event.md` section 7). Each has exactly `feed` (a 32-byte key), `seq` (at least 1), `header_ct` (base64url of at least 72 bytes) and `body_ct` (base64url or null). An envelope over 4194304 bytes in canonical form is `mailbox.too_large`. Any member may deposit envelopes of any feed, because peers relay each other's events and a removed device's older events stay valid.

The mailbox gives each new envelope the next number `n` of the mesh, starting at 1. An envelope identical to one it still holds is a duplicate and gets no number. Two different envelopes for one feed and sequence are both kept, since peers need them as fork evidence. If the new envelopes would take the mesh over its item or byte limit, the whole request fails with `mailbox.quota_exceeded`. Result `{"accepted": n, "duplicates": n, "head": n}`.

**`fetch`.** Body `{"after": a, "limit": l}` with `a` at least 0 and `l` from 1 to 256. The requester says it holds every envelope numbered up to `a`. An `after` above the head is `mailbox.cursor_ahead`, which means the mailbox lost state, and the client starts again from 0. Result:

```
{"items": [{"n": n, "envelope": {...}}], "more": bool, "next": n, "truncated": bool}
```

Items are those with `n` above `after`, in order, up to `l`, leaving out the ones the requester deposited. `more` is true when more are waiting. `next` is the number of the last item returned when `more` is true. Otherwise it is the head, or `after` if that is higher. `truncated` is true when envelopes the requester has not collected were discarded for age, which is when `after` is below `expired_through`. A truncated reply tells the client to fill the gap by syncing with a peer.

Asking with `after` also records what the requester holds. A client collects until a reply has no items, and the last request is the one that records everything.

**`notice_put`.** Body `{"notice": ...}`. Section 7. Result `{"id": hash, "new": bool}`.

**`notice_get`.** Body `{}`. Result `{"notices": [{"id": hash, "notice": {...}}]}` in the order first deposited. The requester is then counted as having seen each one.

**`status`.** Body `{}`. Result `{"bytes", "expired_through", "head", "items", "members", "notices", "seq"}`, all integers. `seq` is the membership statement's.

## 6. What the mailbox keeps and for how long

An envelope is **collected** when every current member except its depositor has asked with an `after` of at least its number. A collected envelope is deleted. A device that never collects holds envelopes back, so there are limits as well.

| Limit | Default | Effect |
|---|---|---|
| `max_age_s` | 2592000 (30 days) | An envelope not yet collected is deleted after this long, and `expired_through` rises to its number |
| `max_mesh_items` | 50000 | A deposit that would pass it fails with `mailbox.quota_exceeded` |
| `max_mesh_bytes` | 134217728 | The same, counting the canonical size of each envelope |
| `max_meshes` | 10000 | A new registration fails with `mailbox.quota_exceeded` |
| `max_notices` | 64 | A new notice fails with `mailbox.quota_exceeded` |
| `notice_max_age_s` | 15552000 (180 days) | A notice is deleted after this long |

An operator may change the limits and may give an admission list of operations keys that may register a mesh. The limits are the mailbox's own policy and not part of the wire format. The state is checked and cleaned before and after every op, using the mailbox's clock.

## 7. The removal notice

When the owner removes a lost device, the other devices may all be off. The removing device deposits the signed roster entry as a **notice**, and a device that comes online later collects it. The device then merges the entry as it would from any peer.

A notice is a roster entry (`roster.md`) of type `remove`, `stop` or `epoch`. Those are the entries `roster.md` allows a mailbox to hold in the clear. Any other type is `mailbox.notice_type`. The mailbox checks only that the object has string fields `author`, `mesh`, `sig` and `type`, that `mesh` matches the request (`mailbox.wrong_mesh`), and that its canonical form is at most 16384 bytes (`mailbox.too_large`). It cannot check the entry against the roster and does not try. Receiving devices do, and drop an entry that fails.

The notice id is the hash of its canonical JSON. A notice is deleted when every current member has deposited or collected it, or when it is older than `notice_max_age_s`. The removal also arrives by the statement of section 4, so the removed device loses access at once and learns it was removed from `mailbox.removed`.

## 8. Honest state

A client shows a request that is waiting on the mailbox as waiting. The path of a peer's events is `mailbox` when they arrived through one. A mailbox that cannot be reached is not an error for the mesh, since peers still sync when they meet.

## 9. Errors

`mailbox.malformed`, `mailbox.too_large`, `mailbox.wrong_audience`, `mailbox.stale`, `mailbox.wrong_peer`, `mailbox.bad_signature`, `mailbox.unknown_mesh`, `mailbox.removed`, `mailbox.not_a_member`, `mailbox.wrong_mesh`, `mailbox.members_seq`, `mailbox.members_unauthorized`, `mailbox.recovery_mismatch`, `mailbox.admission_denied`, `mailbox.quota_exceeded`, `mailbox.cursor_ahead`, `mailbox.notice_type`, `mailbox.unavailable`.

`mailbox.unavailable` means the mailbox's own store failed. It is an operator fault and no vector produces it.

## 10. Vectors

`mailbox.json` has these operations. Each takes the mailbox key (`mailbox`) and the case input.

| `kind` | Input | Result |
|---|---|---|
| `mailbox_sign` | `alg` (`ed25519` or `p256`), `object`, `secret_hex` | `sig` |
| `mailbox_request` | `request`, `mailbox`, `now_s`, optional `peer` | `device`, `mesh`, `op` (steps 1 to 5 of section 3) |
| `mailbox_statement` | `statement` | `by`, `seq`, `via` (shape and signature) |
| `mailbox_notice` | `notice`, `mesh` | `id` |
| `mailbox_envelope` | `envelope` | `size` |
| `mailbox_replay` | `mailbox`, `steps` (each `request`, `at_s`, optional `peer`), optional `limits` and `admission` | `results`, one `{"ok": ...}` or `{"error": ...}` per step |

A replay starts with an empty mailbox. Each step is a signed request and the mailbox's clock for that step.
