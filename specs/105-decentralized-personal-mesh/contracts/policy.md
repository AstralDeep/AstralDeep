# Budget policy contract, `astral.policy/v1`

The budget policy is the signed, mesh-level document that bounds what each device may spend in a period. LETS derives each period's budget from it. This document fixes the policy's shape and signature, the rules for narrowing and widening, how concurrent policies merge, and the input handed to LETS.

Reference code: `reference/astral_mesh_ref/policy.py` and `consent.py`. Vectors: `policy.json`.

## 1. The document

A policy is a JSON object with exactly these fields.

| Field | Type | Rule |
|---|---|---|
| `v` | integer | `1` |
| `mesh` | string | The mesh id |
| `author` | string | Device id of a member |
| `follows` | array of hashes | Policy ids this policy replaces. Sorted, no duplicates. Empty for the first policy |
| `clock` | object | `{"ms", "n"}` |
| `period` | object | `{"unit": "day", "length_s": int above 0, "anchor_s": int at least 0}` |
| `freshness_s` | integer | Seconds of silence after which a device falls to its solo share. At least 0 |
| `classes` | object | For each device kind (`desktop`, `pocket`, `satellite`, `node`), a share block |
| `devices` | object | Optional overrides. Device id to share block |
| `sig` | string | Identity-key signature of the policy context over the document without `sig` |

A **share block** is `{"fresh": caps, "solo": caps}`. **Caps** is an object with exactly the three integer classes `notes_write`, `outside_effect` and `model_tokens`, each at least 0. They are per-period counts for the fresh and solo shares of spec section 8.3. Reads inside the mesh and outward reads are not capped here because the flow rules govern them. A solo cap above the matching fresh cap raises `policy.solo_exceeds_fresh`.

The **policy id** is the hash of the canonical JSON of the document including `sig`.

The starting values of the allowance table are in the vectors (`pocket`: fresh 200, 20 and 200000, solo 50, 0 and 50000). The owner tunes them.

| Invalid policy | Error |
|---|---|
| Wrong fields, types, missing kind or class, bad period | `policy.malformed` |
| `mesh` is not this mesh | `policy.wrong_mesh` |
| `author` is not a member | `policy.unknown_author` |
| Signature fails | `policy.bad_signature` |
| A solo cap exceeds its fresh cap | `policy.solo_exceeds_fresh` |

## 2. Consent for widening

A policy travels in a wrapper `{"policy": <document>, "consent": <consent or null>}`, which is the body of an `auth.policy` event. A **consent** is an object with exactly `v` (1), `kind`, `digest`, `device`, `clock` and `sig`. `kind` is `policy` here. `digest` is the policy id. `device` is a member. `sig` is the presence-key signature of the consent context over the object without `sig`. The same object with another `kind` is used for actions and grants.

| Failure | Error (as wrapped by the policy merge) |
|---|---|
| Wrong fields or kind | `policy.consent.malformed` |
| `digest` or `kind` differs from what is being approved | `policy.consent.wrong_action` |
| `device` is not a member | `policy.consent.unknown_device` |
| Signature fails | `policy.consent.bad_signature` |

## 3. Narrowing and widening

Compare two policies P and Q. P is **narrower or equal** than Q when `freshness_s` of P is at most Q's, every cap in every kind and mode is at most Q's, and for every device in either policy's overrides, P's effective caps for that device are at most Q's. A device's effective caps are its override if it has one, otherwise its kind's caps.

A policy is accepted in this order of steps.

1. Its shape and signature are valid, and its author is a member.
2. Every policy it follows is accepted. If a followed policy was ignored the policy is ignored with `policy.parent_rejected`. If a followed policy has not arrived the policy is **pending**.
3. The base is the element-wise minimum of the followed policies, or all zeros if it follows none.
4. If the policy is narrower or equal than the base and has the same period, no consent is needed. This is narrowing, and it applies the moment a peer sees it.
5. Otherwise it is a **widening**. Changing the period always counts as widening. A widening needs a valid consent. Without one it is ignored with `policy.widening_needs_consent`.

The first policy of a mesh is a widening from zero, so it needs the owner's consent.

## 4. The effective policy

**Heads** are accepted policies that no accepted policy follows. The effective policy is the element-wise minimum over the heads. So two devices that narrowed different things while apart end up with both narrowings. A later policy that follows both heads may widen again with consent. If the heads disagree on the period, the effective period is the newest head's and the result is flagged `conflict`. A conflicting result is a signal to the kernel to use solo shares until the owner writes a merging policy.

A result lists `accepted`, `heads`, `ignored` (policy id to error), `pending`, `conflict` and `effective`. The effective part has `classes`, `devices`, `freshness_s` and `period`.

## 5. Input to LETS

LETS derives a period's budget. This contract fixes only the input. The expected shares are LETS's and are checked in LETS's own vectors.

The input is the canonical JSON of

```
{"v": 1, "mesh": <mesh id>, "period": <period>, "period_index": <int>,
 "freshness_s": <int>, "policy": <hash of the effective classes, devices, freshness_s and period>,
 "members": [{"device": <id>, "kind": <kind>}, ...sorted by device id], "stop": <stop counter>}
```

`period_index` is `floor((now_s - anchor_s) / length_s)`. A time before the anchor raises `policy.before_anchor`. The input's digest is the hash of its canonical JSON. Two devices that hold the same roster and policy build the same input and so derive the same budget with the same disjoint shares.

Whether a device is fresh or solo depends on the device's own clock and contact. It is not part of the input. LETS applies it when the device spends.

## 6. Carrying a transfer

A device that has headroom can give part of its own share to another device with the LETS prepare, accept and finalize steps (spec section 8.3). LETS owns the voucher and the acknowledgement, their signatures and the accounting. The mesh only carries them, as `auth.transfer` events with scope `mesh`. The labels are private and owner.

The body has exactly two fields. `kind` is `voucher` or `ack`. `transfer` is the LETS object, an object with a string `type` and a string `transfer_id`. The type is `lets.transfer-voucher/v1` for a voucher and `lets.transfer-ack/v1` for an acknowledgement.

| Failure | Error |
|---|---|
| Wrong fields, an unknown kind, or a transfer that is not an object with a string `type` and `transfer_id` | `transfer.malformed` |
| The object's `type` does not match the kind | `transfer.type_mismatch` |

Rules for the devices that read these events. Every step is idempotent, so a retry or a restart changes nothing.

- The giver writes the voucher once per `transfer_id`. After a pairing approval the voucher is the new device's headroom offer, and the giver writes it again after a restart if it is still unsettled and no voucher event for it exists.
- A device acts on a voucher only when LETS names it as the target. It accepts the voucher into its holder, then writes the acknowledgement once per `transfer_id`. LETS refuses a voucher that was signed for another period, another target or another policy digest, and the device leaves such an event alone.
- The giver finalizes when it sees an acknowledgement for a voucher it gave. LETS refuses an acknowledgement that does not match its voucher.
- A transfer moves headroom and never creates it. What the giver gave is subtracted from its allowance for the period, and the target's allowance rises by the same amount only after acceptance.

Vectors: `transfer.json`.

