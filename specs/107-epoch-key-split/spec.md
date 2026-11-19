# Spec 107: split the epoch key

**Date:** 2026-11-18. **Status:** specified. Nothing is built.
**Tracking:** issue 681. **Plan:** [plan.md](plan.md). **Tasks:** [tasks.md](tasks.md).

This spec turns the plan's "Design A" and the owner's five rulings into requirements a test can
check. The plan holds the reasoning. This document holds what must be true when the work is done.

## 1. How to read this spec

Paths under "105" are `specs/105-decentralized-personal-mesh/contracts/`. Paths under "106" are
`specs/106-sign-in-free-clients/contracts/`. Kernel paths are in the AstralKernel repository.

The plan was read at Kernel `eb9821f4`. Kernel `main` is now `dbab48b9`. Every **today** claim this
spec rests on was checked again at `dbab48b9` on 2026-11-18. Section 4 lists what was checked, what
changed and what was not checked. Deep pins Kernel `f108c850`, one merge before `dbab48b9`.

Requirements are numbered `R` and acceptance criteria `AC`. Names follow plan section 3.1.

| Name | What it is |
|---|---|
| Epoch secret | The 32 random bytes that an `epoch` entry introduces. It is the epoch key of 105 |
| Epoch header key | Derived from the epoch secret. Seals event headers |
| Epoch introduction key | Derived from the epoch secret. Encrypts the body of `key.content` events |
| Header-only holding | A device holds an epoch's header key and nothing else of that epoch |
| Limited joiner | A device paired with `from_now` or `current`, so it has header-only holdings |
| Activation | The moment the mesh switches to the split format, by one signed statement |

## 2. The problem

One personal epoch key does two jobs. It seals every event header (105 `event.md` section 7). It also
encrypts the body of every `key.content` event, which carries a chat's content key (105 `keys.md`
sections 2 and 4).

A peer must open every header to run the chain checks (105 `sync.md` section 4). So a new device needs
every old epoch key just to follow the log. Each of those keys also opens every old content key.

Limited history is therefore all or nothing. The Kernel refuses it today, so a device gets full
history or no pairing.

A second gap sits beside it. Removing a device starts a new epoch, but a personal chat keeps the
content key it had. A removed device that later gets ciphertext of an old chat can still open it.

## 3. Requirements

### 3.1 The ruling

- **R0.1** Each personal epoch secret yields an epoch header key and an epoch introduction key.
  Holding the header key reveals neither the secret nor the introduction key.
- **R0.2** A limited joiner receives header keys for every earlier personal epoch, and epoch secrets
  only for the epochs it was granted.
- **R0.3** A limited joiner can verify and forward every old event. It can read old metadata (kinds,
  clocks, scopes, sizes, labels). It can never read old chat content. This cost is accepted.

### 3.2 The owner's rulings of 2026-11-18

- **R1. A limited joiner reads `mesh` scope history.** The approver introduces the mesh content key
  again under the join epoch, so the joiner can open settings, connections and agent records. Chat
  content stays closed. The Phase 0 inventory of `mesh` scope lists exactly what this exposes, and the
  approval screen states it.
- **R2. A header-only grant lives in the roster.** It is a wrap of the epoch header key with its own
  purpose in the associated data. A full wrap and a header-only wrap can never open as each other.
  Every peer decides what to serve from the roster alone. The grant survives the approver going
  offline. The new roster form is written only after activation.
- **R3. Activation is a signed statement, written once.** No device infers activation from its own
  view of who has announced. Activation never reverts.
- **R4. The personal current-key rule ships even if limited history slips.** Its tasks depend on no
  limited-history task and need no activation (section 7.4).
- **R5. `from_now` ships first.** `all` and `from_now` are the choices offered. `current` stays
  refused with more than one epoch until the owner decides on it after `from_now` is out.

### 3.3 Requirements that follow

- **R6.** No member wraps a personal epoch secret to a device except through pairing with `all`, the
  join epoch of a limited pairing, recovery, or a later owner grant of more history. A held-aside
  envelope never triggers a secret wrap to a header-only holder. Today's contract text allows any
  member to wrap on request (105 `keys.md` section 2, `sync.md` section 4 step 1). Both sentences are
  revised in Phase 1.
- **R7.** The recovery key still receives every personal epoch secret. Phi epochs are still never
  wrapped to the recovery key (105 `keys.md` section 5).
- **R8.** The phi key path does not change. A limited joiner sees PHI headers as any device outside
  the phi group does today, and never receives their bodies.
- **R9.** Nothing already stored is rewritten. Event ids, signatures and chains of old events stay the
  same in both envelope forms.

## 4. What was verified against the code

Checked at Kernel `dbab48b9` and Deep's `specs/` on this branch, 2026-11-18.

| Plan claim | Result | Where |
|---|---|---|
| `require_readable_history` refuses `from_now` always, and `current` when the roster or the approver holds more than one personal epoch | Holds. The message is "this device can be added only with full history" | `crates/astral-kernel/src/pairing.rs` |
| `pairing.history_unavailable` is in no contract | Holds. It appears only under `specs/107-epoch-key-split/` | `specs/` grep |
| `sealing_epoch` always returns the personal epoch | Holds. The phi group is chosen only for the body key of a mesh `key.content`, through `sealing_under` | `crates/astral-kernel/src/mesh_core.rs` |
| `rotate_personal_chat_key` runs only when a share is revoked | Holds. Callers are `revoke_grant`, `settle_rotations` for ended shares, and a testkit hook | `crates/astral-kernel/src/shares.rs`, `phi_chat_key.rs` |
| Personal `grant_epochs` runs at pairing and recovery only | Holds. Other callers in `writer.rs` are phi. `plan_wrap` has test callers only. A held envelope records reason `epoch` and nothing answers it with a wrap | `crates/astral-mesh/src/pairing.rs`, `recovery.rs`, `writer.rs`, `epochs.rs`, `crates/astral-log/src/log.rs` |
| Removal starts a new epoch but keeps chat keys | Holds. `remove_device` writes the removal and new epochs, no `key.content` | `crates/astral-kernel/src/mesh_ops.rs` |
| The event id does not cover `header_ct` | Holds. It is the hash of the signed header | 105 `event.md` sections 2, 5, 7 |
| `HistoryGrant` has three choices | Holds. `All`, `CurrentEpoch`, `FromNow`, on the wire `all`, `current`, `from_now` | `crates/astral-mesh/src/pairing.rs`, `crates/astral-kernel-ffi/src/records.rs` |
| "PHI chats already follow the current-key rule. The personal group adopts the same text" | **Wrong.** See below | 106 `phi-destinations.md` sections 8 and 10 |

**The correction.** 106 `phi-destinations.md` section 8 already states the current-key rule for both
groups: "any other event with its current personal key". Its section 10 says the first personal write
after a personal epoch change introduces a new chat key. The vectors already carry personal cases, for
example `chat_personal_epoch_changed`, which expects `rotate: epoch_changed`.

The Kernel's `chat_write_key` passes those vectors as a pure function. Only the PHI write path calls
it (`prepare_phi_chat` in `phi_chat_key.rs`). A personal write seals with the cached chat key or the
`chat.create` key. `fold_content_keys` caches the last personal chat-scoped key it meets, with no
current-epoch filter. No Kernel test writes to a chat after a personal epoch change.

So the rule is specified and the Kernel does not follow it. Ruling 4 is Kernel conformance work and
needs no contract change. Plan section 3.6 and its section 4 row are corrected on this branch.

The same fold exists at `eb9821f4`. A 2.1.0 Kernel caches a personal chat-scoped key it folds and
seals the chat's later writes with it, taking the last such key in stored order.

**What changed since `eb9821f4`.** Ten files changed. The work folds content keys at seal time
through a gate and a key-event counter (`mesh_core.rs`, `chats.rs`, `phi_chat_key.rs`, `recovery.rs`,
`astral-log`). None of it touches `astral-core`, `astral-mesh`, `pairing.rs` or `shares.rs`. The plan's
other Kernel symbols in its section 4 table are covered by that diff, without reading each again.

**Also found.** The mesh content key is introduced once. `publish_content_key("mesh")` runs only at
recovery confirmation, which creates the mesh (`crates/astral-kernel/src/mesh_ops.rs`). R1 adds the
second introduction.

**Apple and Deep.** The Mac and iPhone app approves with `.all` in `RealMeshKernel.swift` and shows a
fixed "History access" of "All chats" in `MeshDeviceDetailView.swift` (Projection `d3a296b3`). Deep and
Vessel pass `HistoryGrant.ALL` in `backend/devtools/dev_mesh.py` and tests.

**Not verified.** The two 2.1.0 behaviours of Phase 0 items 1 and 2. The cost of sealing again at
serve time. Whether any path compares `header_ct` bytes. The full `mesh` scope inventory. Whether the
watch app shows device history. All of these are Phase 0 or Phase 4 work.

## 5. What the owner sees

### 5.1 The history choice at pairing

The approval sheet on the Mac and the iPhone gains one choice with two options.

| Option | Meaning shown |
|---|---|
| All history | The device reads every chat, old and new |
| From now on | The device reads chats from this moment. For older events it sees only when they happened |

- All history stays the default.
- From now on is offered only after activation. Before activation it shows as unavailable, with the
  devices that still need an update by name (section 5.4).
- `current` is not offered (R5).
- The sheet states, for From now on, that settings, connections and agent records come across in full
  (R1).

### 5.2 What a limited joiner can and cannot read

| Data | Before the join | After the join |
|---|---|---|
| Event headers, all scopes | Read and verify | Read and verify |
| Chat content, personal | Never | Every message written after the join, in old chats and new |
| Chat content, PHI | Never, as for any device outside the phi group | Only if it joins the phi group |
| `mesh` scope bodies | Read (R1) | Read |
| `key.content` bodies of earlier epochs | Never | Not applicable |
| `key.credential` boxes sealed to it | As today | As today |

A chat the joiner cannot read shows as closed to this device. It never shows as empty.

### 5.3 The device screen

"History access" on each device's screen shows what the roster says.

| Roster facts | Shown |
|---|---|
| Full grants of every personal epoch | All history |
| Header-only grants before a join epoch | From the join date |
| A later owner grant of more history | Updated to match |

### 5.4 When a device blocks activation

The mesh status names each member that has not announced the capability, with its label and kind.
The pairing refusal names the same devices (section 8). The owner can update a device or remove it.

## 6. Wire and roster changes

These are at the plan's level. Phase 1 fixes the exact forms in revised contracts under
`specs/107-epoch-key-split/contracts/` with `reference/` and `vectors/`. A document, its vectors and
its reference module change together.

### 6.1 Derivation

Both keys come from the epoch secret with HKDF-SHA256, the epoch id as salt and a 32-byte output.

| Info text | Result |
|---|---|
| `astral.epoch.header/v1` | Epoch header key |
| `astral.epoch.introduce/v1` | Epoch introduction key |

Existing epochs follow the same rule. Their epoch key becomes the epoch secret, so every current
member derives a header key for every old epoch with no new roster entry.

### 6.2 Envelopes

- A split-format envelope seals its header under the epoch header key. The leading 32 bytes of
  `header_ct` name the header key, and the associated data binds the format.
- A peer that holds the epoch secret can open an old-form header and seal the same signed header
  again in the split form. The event id, signature and chain do not change (R9).
- A split-format personal `key.content` encrypts its body under the epoch introduction key. Its header
  says so with a new key reference form.
- An old `key.content` body stays encrypted under the epoch secret. A header-only holder cannot open
  it.

The new key reference form must keep the group readable to every place that classifies a key by its
`ek:` prefix. Those are 106 `share.md` section 5, 106 `phi-destinations.md` section 8, `KeyRef::parse`
in `crates/astral-core/src/event.rs`, `share_session.rs` and `settle_rotations` in `shares.rs`. Share
sessions are out of scope, so their behaviour must not change. A share regression test proves it.

### 6.3 Grants in the roster

| Pairing choice | Earlier epochs | The epoch at the join |
|---|---|---|
| `all` | Epoch secret | Epoch secret |
| `from_now` | Header-only wrap | A new epoch starts, and the joiner gets its secret |
| `current` (waits on the owner) | Header-only wrap | Epoch secret of the current epoch |

A header-only wrap is `astral.wrap/v1` of the epoch header key with its own `purpose` value in the
associated data. A full wrap keeps purpose `epoch`.

### 6.4 Sync

- A peer serves a header-only holder the split form of each old envelope, sealing the header again
  when its stored copy is old-form.
- A peer withholds a body from a holder that cannot open it by the roster's facts, with reason
  `history`. This joins the `phi`, `secret` and `unknown` reasons of 105 `sync.md` section 5.
- `mesh` scope bodies under the reintroduced mesh content key are sent (R1).
- A header-only holder forwards headers and holds no old bodies.
- When a full wrap of an earlier epoch reaches a header-only holder, it asks for the bodies it lacks.

### 6.5 Left to the Phase 1 contracts

The plan does not name these, so this spec does not either. Each is fixed in its Phase 1 contract.

- The header-only wrap's `purpose` value.
- The split key reference syntax and the split envelope's format binding.
- The carriers of the capability statement and the activation statement.
- The reasons carried by `pairing.history_unavailable`, and how the blocking devices travel with it.
- The code for a pairing request from a device that cannot announce the capability.
- How a pairing request shows the capability.

## 7. Rollout

### 7.1 Step one: read both

A Kernel at this step reads both envelope forms, derives both keys, holds header-only grants, serves
header-only holders and withholds with reason `history`. It keeps writing the old form.

### 7.2 The capability statement

A Kernel announces the capability with a signed statement only when it can also act on activation.
No released Kernel announces before Phase 3 code is in it. This matters because every green push to
Deep `main` publishes a Mac release, and a Kernel pin bump for ruling 4 could otherwise ship a Kernel
that announces a capability it cannot honour.

### 7.3 Step two: activate

- An updated device writes the activation statement once every current personal member has
  announced. The statement cites the roster state and the capability statements it relies on.
- Every peer judges a statement's validity from the facts it cites, never from its own view of who
  has announced. The form and the tie-break between two concurrent statements are fixed in Phase 1.
  After that, a second statement changes nothing.
- From activation, every member writes the split form, and limited history is allowed.
- Before activation, pairing refuses limited history with `pairing.history_unavailable` and names
  the devices that still need an update.
- After activation, a pairing request from a device that cannot announce the capability is refused.
- A device that never updates stops reading new events after activation. It keeps what it had and
  holds new envelopes aside until it updates. Phase 0 proves this on a real 2.1.0 build.

### 7.4 The personal current-key rule, independent of both steps

The rule of 106 `phi-destinations.md` section 8 applies to personal writes. The first personal write
to a chat after a personal epoch change introduces a chat-scoped `key.content` under the current
epoch, with reason `first_write` or `epoch_changed`.

It needs no activation. A 2.1.0 Kernel never starts the rotation itself. It learns a rotated key from
the event, reads what it seals, and adopts it for its own later writes to that chat, picking the last
such key in stored order (section 4). So the removal gap closes for a chat once an updated writer has
rotated it, and a 2.1.0 writer that has folded the rotation follows.

### 7.5 Epochs that began before activation

A grant of an epoch that began before activation opens only the chats whose key was introduced in
that epoch. `from_now` has no such limit, because its join epoch starts after activation. If `current`
ships, it is offered only when the current epoch began after activation.

## 8. Failure and refusal cases

Codes in plain type exist today. Codes marked *Phase 1* are named in their contract.

| Case | Result | Code |
|---|---|---|
| Limited history before activation | Refused before any prompt, naming blocking devices | `pairing.history_unavailable` |
| `current` with more than one personal epoch, before the owner decides | Refused | `pairing.history_unavailable` |
| A request from a device that cannot announce, after activation | Refused | *Phase 1* |
| A 2.1.0 Kernel meets a split-format envelope | Held aside with reason `epoch`, feed intact | `event.epoch_unknown` |
| A reader lacks the header key named by the envelope | Held aside, retried when a grant arrives | `event.epoch_unknown` |
| A header-only wrap opened as a full wrap, or the reverse | Refused | `wrap.decrypt_failed` |
| A header-only holder asks for an old body | Withheld | reason `history` |
| A header-only holder meets an old body some other way | Not opened | `event.key_unknown` |
| A split header edited, or bound to the wrong format | Refused | `event.decrypt_failed` |
| An activation statement whose cited facts do not show every member announced | Rejected by every peer alike | *Phase 1* |
| A second activation statement | No change, by the Phase 1 tie-break | none |
| A personal write when the personal group has no current epoch | Refused | `phi.group_not_ready`, as 106 section 8 row 2 |

## 9. Calls for the owner to check

These are choices this spec makes beyond the plan and the rulings. Each stands unless the owner
says otherwise.

1. **No capability statement before Phase 3.** The plan had Phase 2 kernels announce. This spec
   moves writing it to Phase 3 so a Kernel pin bump for ruling 4, which publishes a Mac release on a
   green `main`, can never ship a Kernel that announces a capability it cannot act on (section 7.2).
2. **No wrap of a personal secret on request (R6).** Today's contracts allow it and the Kernel never
   does it. Limited history needs the ban written down.
3. **Who writes the activation statement.** Any updated device writes it once the facts it cites
   show every member announced. No owner prompt is asked for. The alternative is to ask the owner
   with presence.
4. **The sheet labels.** "All history" and "From now on", with All history the default.

## 10. Out of scope

- Granting one old chat to a limited device. The design allows it later.
- A client control to grant a limited device more history. The Kernel side is in scope, as task T3.7.
- Any change to the phi group, share sessions or caller tickets.
- Shipping `current`. It waits on a later owner decision.
- The Android and Windows clients, which are paused.
- The web shell, which has no pairing surface.

## 11. Acceptance criteria

Each runs in CI through the real Kernel calls unless marked *(live)*.

**Derivation and wire**

- **AC1.** Both derived keys match the Phase 1 vectors for a fixed epoch secret and epoch id.
- **AC2.** An old-form and a split-form envelope of one event open to the same header and event id.
- **AC3.** A header-only wrap does not open as a full wrap, and the reverse, with `wrap.decrypt_failed`.
- **AC4.** A share session over a chat with split-format events serves the same events and wraps as
  before.

**Read both**

- **AC5.** A mixed mesh of old-form and split-form writers converges in the Kernel's sync tests.
- **AC6.** A header-only holder verifies every chain and opens no `key.content` body of an earlier
  epoch.
- **AC7.** A full peer withholds earlier chat bodies from a header-only holder with reason `history`
  and sends `mesh` scope bodies.
- **AC8.** A Kernel built at Phase 2 writes no capability statement.

**Activation**

- **AC9.** Pairing with `from_now` before activation fails with `pairing.history_unavailable`, and
  the refusal lists each member that has not announced.
- **AC10.** An activation statement whose cited facts do not cover every member is rejected by peers
  holding different views of the roster. Two concurrent valid statements resolve to one on every
  peer, and a later one changes nothing.
- **AC11.** After activation, a request from a device that cannot announce is refused.

**Limited history**

- **AC12.** After a `from_now` join, the joiner reads messages written after the join in an old chat
  and in a new chat, and reads none written before it.
- **AC13.** After a `from_now` join, the joiner reads `mesh` scope settings and connections written
  before the join.
- **AC14.** `current` with more than one personal epoch is still refused.
- **AC15.** A restore with the 24 words after a limited join reads full history.
- **AC23.** When a header-only holder receives a full wrap of an earlier epoch, it asks for and opens
  that epoch's bodies, and its history in status changes to match.

**Personal current-key rule**

- **AC16.** After `remove_device`, the first personal write to an old chat writes a chat-scoped
  `key.content` under the new epoch, and the message seals under that key.
- **AC17.** The removed device, given that message's ciphertext, cannot open it.
- **AC18.** A 2.1.0 Kernel (`eb9821f4`) learns the rotated personal key from the event, reads the
  message sealed under it, and the run records which key its own next write to that chat uses
  *(recorded run)*.
- **AC19.** The `chat_personal_*` cases of `phi-destination-v1.json` pass through the runtime write
  path as well as the pure function.

**Clients and release**

- **AC20.** Deep's four backend groups pass on the new pin with a limited joiner in the node tests.
- **AC21.** The Mac and iPhone apps pair a device From now on, and its device screen shows the join
  date *(live)*.
- **AC22.** A recorded run with one 2.1.0 device and two updated devices, before activation, at
  activation and after the old device updates *(live)*.
