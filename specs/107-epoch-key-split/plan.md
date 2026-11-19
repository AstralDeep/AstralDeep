# Plan for spec 107: split the epoch key

**Date:** 2026-11-18. **Status:** planned, with the owner's rulings in section 6. Nothing here is built.
**Tracking:** issue 681.

This is the plan for "Design A", which the owner ruled on 2026-11-16. It covers the design, the
owner's rulings on the open questions, the phases, and how to tell each phase is finished. The
[spec](spec.md) and [tasks](tasks.md) follow from it.

Paths under "105" are `specs/105-decentralized-personal-mesh/contracts/`. Paths under "106" are
`specs/106-sign-in-free-clients/contracts/`. Kernel paths are in the AstralKernel repository at
`eb9821f4`. A line marked **today** was read from the code or a contract on 2026-11-18. The
[spec](spec.md) checked these lines again at Kernel `dbab48b9` and corrected one, in section 3.6.

## 1. The ruling

Each epoch key splits into a header key and a content key. A device that joins with limited history
gets header keys for every earlier epoch, and content keys only for the epochs it was granted.

The accepted cost is that such a device sees old metadata (kinds, clocks, scopes, sizes, labels) and
never old content.

The first half of the ruling shipped. The Kernel refuses limited history with
`pairing.history_unavailable` (Kernel PR 4, `faf60a98`). This plan is the second half.

## 2. The problem today

- **One key, two jobs.** The personal group's epoch key seals every event header (105 `event.md`
  section 7). The same key encrypts the body of each `key.content` event, which carries a chat's
  content key (105 `keys.md` section 4).
- **A peer must open every header.** Accepting an event runs the chain checks on the opened header
  (105 `sync.md` section 4). A device that cannot open a header cannot verify or forward the event.
- **So limited history is all or nothing.** A joiner needs every old epoch key to follow the log, and
  each of those keys also opens every old content key.
- **The refusal.** `require_readable_history` in `crates/astral-kernel/src/pairing.rs` refuses
  `from_now` always, and `current` when the roster or the approver holds more than one epoch. The code
  `pairing.history_unavailable` is in no contract. 106 `phi-destinations.md` line 140 records that
  pairing refuses limited history.

Three more facts shape the design. All are **today**.

- **The phi epoch key has one job.** It never seals a header. `sealing_epoch` always returns the
  personal epoch (`crates/astral-kernel/src/mesh_core.rs`). The phi key only encrypts the content keys
  of PHI chats.
- **Personal chat keys do not rotate when the epoch changes.** `rotate_personal_chat_key` runs only
  when a share is revoked (`crates/astral-kernel/src/shares.rs`). A chat keeps its key across epochs.
  So a new epoch alone does not hide later messages of an old chat from a device that held the old
  key, and it would not show them to a device that holds only the new epoch. The contract already
  requires the rotation (section 3.6). The Kernel does not follow it.
- **No member re-wraps a personal epoch on request.** `grant_epochs` for the personal group runs at
  pairing and at recovery only. An older app version left in the mesh therefore cannot hand an old
  epoch key to a limited joiner by itself.

## 3. Design

### 3.1 Names

"Header key" and "content key" already mean other things. 106 `share.md` derives a share header key
from a chat's content key, and `ck:` keys are per chat. This plan uses these names.

| Name | What it is |
|---|---|
| Epoch secret | The 32 random bytes that an `epoch` entry introduces. It is the epoch key of 105 |
| Epoch header key | Derived from the epoch secret. Seals event headers |
| Epoch introduction key | Derived from the epoch secret. Encrypts the body of `key.content` events |
| Header-only holding | A device holds an epoch's header key and nothing else of that epoch |

### 3.2 Derivation

Both keys come from the epoch secret with HKDF-SHA256, the epoch id as salt, and a 32-byte output.

| Info text | Result |
|---|---|
| `astral.epoch.header/v1` | Epoch header key |
| `astral.epoch.introduce/v1` | Epoch introduction key |

A holder of the epoch secret derives both. A holder of the header key can derive neither the secret
nor the introduction key.

The same rule applies to epochs that already exist. Their epoch key becomes the epoch secret, so
every current member can derive a header key for every old epoch with no new roster entry.

### 3.3 What changes on the wire

- **Headers.** A split-format envelope seals its header under the epoch header key. The leading 32
  bytes of `header_ct` name the header key, and the associated data binds the format.
- **Content key events.** A split-format `key.content` event of the personal group encrypts its body
  under the epoch introduction key, and its header says so with a new key reference form.
- **Old events stay valid.** An event id is the hash of the signed header (105 `event.md` sections 2
  and 5), and it does not cover `header_ct`. So a peer that holds the epoch secret can open an old header and
  seal the same signed header again under the header key. The event id, the signature and the chain
  are unchanged.
- **Old content keys stay closed.** An old `key.content` body is encrypted under the epoch secret
  itself. A header-only holder cannot open it.

### 3.4 Grants

A full grant is today's wrap of the epoch secret. A header-only grant is a wrap of the epoch header
key with its own purpose in the associated data, so one can never open as the other.

| Pairing choice | Earlier epochs | The epoch at the join |
|---|---|---|
| `all` | Epoch secret | Epoch secret |
| `current` | Header key only | Epoch secret of the current epoch |
| `from_now` | Header key only | A new epoch starts, and the joiner gets its secret |

### 3.5 Sync

- A peer serves a header-only holder the split form of each old envelope. It seals the header again
  when its stored copy is in the old form.
- A peer withholds a body from a holder that cannot open it by the roster's own facts. This is the
  PHI rule of 105 `sync.md` section 5 with a new reason, `history`.
- A header-only holder forwards headers and holds no old bodies, like a device outside the phi group
  today.
- If the owner later grants more history, the device asks for the bodies it lacks.

### 3.6 The current-key rule for personal chats

Limited history needs one rule the personal group lacks today. A chat's current key must have been
introduced under the current epoch. The first write to a chat after a new epoch begins introduces a
new key.

106 `phi-destinations.md` section 8 already states this rule for both groups, and its section 10
says the first personal write after an epoch change introduces a new key. Its vectors carry the
personal cases. The Kernel applies the rule only to PHI writes, through `chat_write_key`, so this is
Kernel conformance work and needs no contract change. Corrected 2026-11-18 against Kernel `dbab48b9`.

Two things follow.

- `from_now` then means what it says. The joiner reads every message written after it joined, in old
  chats and new ones.
- Removing a device then protects later messages in old chats, which the epoch change alone does not
  do today.

### 3.7 Older app versions in the same mesh

A 2.1.0 kernel cannot open a split-format header and does not follow the current-key rule. So the
change lands in two steps.

1. **Read both.** A kernel reads both forms, serves header-only holders, and reads other devices'
   capability statements. It keeps writing the old form. It writes its own capability statement only
   once it also carries the activation code of Phase 3, so no released kernel announces early.
2. **Activate.** When every member of the personal group has announced, the mesh activates the split
   format. From then on every member writes it, and limited history is allowed. Activation never
   reverts. A device that cannot announce the capability is refused at pairing.

Until activation, pairing keeps refusing limited history. The refusal names the devices that still
need an update.

A device that never updates stops reading new events after activation. It keeps what it had and
holds the new envelopes aside until it updates. Phase 0 proves this on a real 2.1.0 build.

### 3.8 Epochs that began before activation

A grant of an epoch that began before activation opens only the chats whose key was introduced in
that epoch. The current-key rule was not in force then.

`from_now` does not have this limit, because it starts a new epoch after activation. The plan ships
`from_now` first and lifts `current` only for epochs that began after activation.

### 3.9 Recovery and backup

- The recovery key still receives every personal epoch secret, so a restore with the 24 words reads
  full history.
- The backup file holds the roster and the envelopes a device has. A backup written by a header-only
  holder restores old metadata and needs a full peer for old bodies. The backup contract says so.
- Phi epochs are still not wrapped to the recovery key.

### 3.10 PHI

The phi key path does not change. The phi epoch key has one use, so there is nothing to split.

A limited joiner sees the headers of PHI chats, as any device outside the phi group does today. It
never receives their bodies.

## 4. What the change touches

| Area | Today | Change |
|---|---|---|
| 105 `keys.md` sections 2 to 6 | One 32-byte key per epoch, wrapped with purpose `epoch` | Derivation, the header-only wrap, the backup note |
| 105 `event.md` sections 2, 6, 7, 11 | Header sealed under the epoch key | The split envelope form, the new key reference, the error codes |
| 105 `roster.md` section 8, 106 `roster-v2.md` | "The owner may grant only the current one" | The three history choices, the header-only grant, `pairing.history_unavailable` and its reasons |
| 105 `sync.md` sections 4 and 5 | Hold aside on an unknown epoch, withhold PHI | Serving header-only holders, the `history` reason, the capability and activation statements |
| 106 `phi-destinations.md` | The current-key rule for both groups, and line 140. The Kernel follows it for PHI only | Line 140 is revised. The Kernel follows the rule for personal chats |
| Vectors and reference | 105 `event`, `keys`, `roster`, `sync`, `mailbox`. 106 `share`, `phi-destination`, `contacts`, `roster-v2` | New vectors for both forms. The Kernel's pinned copies are vendored again |
| Kernel `astral-core` | `KeyRing`, `SealRequest`, `seal_event_with`, `open_header`, `wrap_epoch_key` | Derived keys, both forms, the header-only wrap |
| Kernel `astral-mesh` | `EpochKeys`, `HistoryGrant`, `approve_request`, `grant_epochs`, `join_from_bundle` | Header-only holdings, the grant, activation |
| Kernel `astral-log` | `KeyProvider`, the held reason `epoch`, `envelopes` and `held` rows | Header key lookup, sealing again at serve time, the `history` reason |
| Kernel `astral-kernel` | `require_readable_history`, `mesh_core.rs`, `shares.rs`, recovery | The refusal lifts behind activation. The current-key rule. Backup notes |
| Kernel FFI and Python binding | The `HistoryGrant` enum | Activation state and the blocking devices in status |
| Deep and Vessel | `HistoryGrant.ALL` in the development mesh and tests. No epoch key handling | New Kernel pin, tests with a limited joiner |
| Apple clients | `.all` in `RealMeshKernel.swift`. "History access: All chats" in `MeshDeviceDetailView.swift` | A history choice at approval, the device's history shown truthfully, the update prompt. This is client work |
| Existing data | Rosters, logs and backups written under the one-key scheme | Nothing is rewritten. Old epochs gain a derived header key |

Revised contracts live in `contracts/` under this spec, with `reference/` and `vectors/`. That is the
rule 106 `plan.md` recorded. A document, its vectors and its reference module change together.

## 5. Phases

A phase is finished when its automated criteria pass in CI. Live criteria are marked *(live)* and are
recorded demonstrations.

### Phase 0: evidence

Each item ends with a written result in this folder.

1. What a 2.1.0 kernel does with an envelope whose leading 32 bytes it does not know. It must hold
   the envelope aside, keep the feed, and open it after an update.
2. What a 2.1.0 kernel does with a roster entry it rejects during sync. Its store must stay intact.
3. An inventory of `mesh` scope: every event kind, what its body holds, and whether a limited joiner
   needs it to work. This feeds ruling 1 in section 6.
4. The cost of sealing headers again at serve time, measured on a log of realistic size.
5. A check that no code path treats `header_ct` bytes as identity. Stores, relays, the mailbox and the
   share session are in scope.

### Phase 1: contracts

The revised documents of section 4, their vectors and the reference, in one change each.

Finished when the reference tests pass on the new vectors.

### Phase 2: Kernel reads both

Derivation, both envelope forms, header-only holdings, serving and withholding, and reading the
capability statement. Writers keep the old form, and no kernel writes the statement yet.

Finished when a mixed mesh of old-form and new-form writers converges in the Kernel's sync tests, and
a header-only holder verifies every chain and opens no old content key.

### Phase 3: activation and the current-key rule

Activation, split-format writing, the personal current-key rule, and the pairing refusal behind
activation.

Finished when these pass through the real Kernel calls.

- `from_now`: the joiner reads messages written after the join in an old chat, and reads nothing
  written before it.
- A removed device cannot open a message written to an old chat after its removal.
- Pairing with limited history is refused before activation, and the refusal names the devices.
- A device that cannot announce the capability is refused at pairing.

### Phase 4: consumers

The Kernel pin in Deep and Vessel, the binding, node tests with a limited joiner, and the client
work.

Finished when Deep's four backend groups pass on the new pin, and the Mac and iPhone apps pair a
device with limited history *(live)*.

### Phase 5: mixed-version qualification and release

A recorded run with one 2.1.0 device and two updated devices: before activation, at activation, and
after the old device updates *(live)*.

Then the Kernel release, the Deep pin and the apps, in that order.

## 6. Owner rulings

The owner answered these on 2026-11-18. Each ruling matches the recommendation the plan made.

1. **A limited joiner reads `mesh` scope history.** Settings, connections and agent records live
   there, and a device cannot work without the current ones. The approver introduces the mesh content
   key again under the join epoch. Chat content stays closed. Phase 0 item 3 lists exactly what this
   exposes.
2. **A header-only grant lives in the roster**, as a wrap with its own purpose. Every peer can then
   decide what to serve from the roster alone, and the grant survives the approver going offline. The
   cost is a roster format step, which is safe only after activation.
3. **Activation is a signed statement, written once.** An implied rule would depend on each device's
   view of who has announced, and views can differ.
4. **The personal current-key rule ships even if limited history slips.** It closes the removal gap
   in section 2 by itself.
5. **`from_now` ships first.** `all` and `from_now` cover the cases with clear meaning. `current` has
   the limit in section 3.8, and whether it ships is decided after `from_now` is out.

## 7. Risks

- **A device that never updates.** It stalls at activation. The owner sees which devices block
  activation and can remove them first.
- **Metadata.** A limited joiner learns chat ids, kinds, sizes and timing of old events. This is the
  accepted cost of the ruling, and the approval screen should say it.
- **Two forms of one event.** Any code that compares `header_ct` bytes would see a difference where
  there is none. Phase 0 item 5 checks for it.
- **The most sensitive surface.** Key selection for a seal changes. Every change to it lands with a
  test that fails without it, and the PHI tests run on every change.

## 8. Out of scope

- Granting one old chat to a limited device. The design allows it later.
- Any change to the phi group, share sessions or caller tickets.
- The Android and Windows clients, which are paused.
