# Spec 107: split the epoch key

**Date:** 2026-11-18. **Status:** specified. Nothing is built.
**Tracking:** issue 681. **Plan:** [plan.md](plan.md). **Tasks:** [tasks.md](tasks.md).

This spec turns the plan's "Design A" and the owner's five rulings into requirements a test can
check. The plan holds the reasoning. This document holds what must be true when the work is done.

## 1. How to read this spec

Paths under "105" are `specs/105-decentralized-personal-mesh/contracts/`. Paths under "106" are
`specs/106-sign-in-free-clients/contracts/`. Kernel paths are in the AstralKernel repository.

The plan was read at Kernel `eb9821f4`. Kernel `main` is now `dbab48b9`. The **today** claims this
spec rests on were checked at `dbab48b9` on 2026-11-18. Section 4 says which lines were read again,
which were covered only by the diff since `eb9821f4`, and what was not checked. Deep pins Kernel
`f108c850`, one merge before `dbab48b9`.

Three Kernel versions matter for mixed meshes.

| Name here | Kernel | How it is known |
|---|---|---|
| Pre-106 | `c2c1cc4` | Inferred from 106 `phi-destinations.md` section 10, whose rows name `c2c1cc4` and the Mac 97 and iPhone 99 devices |
| 2.1.0 | `eb9821f4` | Inferred from Deep's Kernel gitlink at `350759eb`, the merge that completed spec 106. The release tag was not checked |
| Updated | A Kernel with this spec's work | |

Requirements are numbered `R` and acceptance criteria `AC`. Names follow plan section 3.1.

| Name | What it is |
|---|---|
| Epoch secret | The 32 random bytes that an `epoch` entry introduces. It is the epoch key of 105 |
| Epoch header key | Derived from the epoch secret. Seals event headers |
| Epoch introduction key | Derived from the epoch secret. Encrypts the body of `key.content` events |
| Header-only holding | A device holds an epoch's header key and nothing else of that epoch |
| Limited joiner | A device paired with `from_now` or `current`, so it has header-only holdings |
| Capability statement | A device's signed claim that its Kernel can act on activation |
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

- **R1. A limited joiner reads `mesh` scope history, within the label rules.** The approver
  introduces the existing mesh content key again under the join epoch, with the same `ck:` id and the
  same secret. A fresh key would leave every old `mesh` body closed. The joiner then opens settings,
  connections and agent records.
  - The label table of 105 `sync.md` section 5 still applies. `phi` and `secret` labelled events stay
    withheld from the joiner with those reasons.
  - Chat-key rotations are `mesh` scope `key.content` events. Those under earlier epochs stay closed.
    Those under the join epoch or later open.
  - Only the `mesh` scope key is introduced again. A chat-scoped key never is, because a chat-scoped
    `key.content` under the current epoch counts as a rotation for the 106 rule and for the 2.1.0
    fold (section 7.4).
  - Chat content stays closed. The Phase 0 inventory of `mesh` scope lists exactly what this exposes,
    and the approval sheet states it.
- **R2. A header-only grant lives in the roster.** It is a wrap of the epoch header key with its own
  purpose in the associated data. A full wrap and a header-only wrap can never open as each other.
  Every peer decides what to serve from the roster alone. The grant survives the approver going
  offline. The new roster form is written only after activation.
- **R3. Activation is a signed statement, written once.** No device infers activation from its own
  view of who has announced. Activation never reverts. Writing it needs owner presence (section 9.1).
- **R4. The personal current-key rule ships even if limited history slips.** Its tasks depend on no
  limited-history task and need no activation (section 7.4). It changes no contract rule. It adds a
  note to 106 on what 2.1.0 does (task R4.8).
- **R5. `from_now` ships first.** `all` and `from_now` are the choices offered. `current` stays
  refused with more than one epoch until the owner decides on it after `from_now` is out.

### 3.3 Requirements that follow

- **R6. Who wraps a personal epoch key.** No member wraps a personal epoch secret to a device except
  through pairing with `all`, the join epoch of a limited pairing, recovery, or a later grant of more
  history.
  - A header-only wrap and a later full grant are written only with owner presence. Section 6.3 says
    which device writes each.
  - A header-only wrap never replaces or shadows a full grant for the same device and epoch.
  - A held-aside envelope never triggers a wrap of a secret to a header-only holder.
  - Today's contract text allows any member to wrap on request (105 `keys.md` section 2, `sync.md`
    section 4 step 1). The Kernel never does. Both sentences are revised in Phase 1.
- **R7.** The recovery key still receives every personal epoch secret, including the join epoch of a
  limited pairing. Phi epochs are still never wrapped to the recovery key (105 `keys.md` section 5).
- **R8.** The phi key path keeps its behaviour. Task R4.1 refactors `prepare_phi_chat` so personal
  writes share it, and the PHI suites pass unchanged. A limited joiner sees PHI headers as any device
  outside the phi group does today, and never receives their bodies.
- **R9.** Nothing already stored is rewritten. Stored bytes, event ids, signatures and chains of old
  events stay the same in both envelope forms.
- **R10. Capability statements are bound.** A statement binds the mesh id, the device id, the
  device's `add` entry and a capability version. A statement from a removed feed is dropped under the
  existing removal cutoff. A device that announced, went back to an old build and was added again
  does not count until it announces under its new `add`.
- **R11. Activation covers the roster it follows.** The statement cites the roster state and the
  capability statements it relies on. That state covers every member whose `add` precedes the
  statement. A statement that misses one, or cites a capability statement that does not hold, does
  not activate. Section 7.3 says how concurrent `add` entries are handled.
- **R12. Seating after activation is a fold rule.** An `add` concurrent with or after the activation
  statement, of a device with no valid capability statement, is not seated. This holds for an `add`
  by any member and for one by the recovery key. The local pairing refusal stays as a second guard.
- **R13. Old-form writes after activation are refused.** An updated peer refuses an old-form
  personal write when the activation statement precedes it in its author's causal history. Phase 1
  fixes the exact position rule.

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
`chat.create` key. No Kernel test writes to a chat after a personal epoch change.

So the rule is specified and the Kernel does not follow it. Ruling 4 is Kernel conformance work and
changes no contract rule. Plan section 3.6 and its section 4 row are corrected on this branch.

**What 106 says about older Kernels.** The last paragraph of 106 section 8 and its section 10 row
"Chats after a personal epoch change" say a deployed Kernel keeps sealing its own writes with the
`chat.create` key.

- That is true of the pre-106 Kernel. At `c2c1cc4` the fold learns keys and caches no rotation.
- It is out of date for 2.1.0. At `eb9821f4`, as at `dbab48b9`, `fold_content_keys` adopts a rotated
  key. For each chat it caches the last personal chat-scoped key it can open, read through the query
  ordered by `(clock.ms, clock.n, feed, seq)` in `crates/astral-log/src/views.rs`.

That is the same order key as the 106 rule. The differences are two. 2.1.0 applies no current-epoch
filter. Its fallback to the `chat.create` key has no epoch condition. So a mixed mesh disagrees when a
later-sorting chat key sits under an older epoch, for example one written by a device that had not yet
folded a removal. Updated devices ignore that key and 2.1.0 seals with it. 106 says nothing of 2.1.0,
so task R4.8 adds it.

**What changed since `eb9821f4`.** Ten files changed. The work folds content keys at seal time
through a gate and a key-event counter (`mesh_core.rs`, `chats.rs`, `phi_chat_key.rs`, `recovery.rs`,
`astral-log`). None of it touches `astral-core`, `astral-mesh`, `pairing.rs` or `shares.rs`. The plan's
other Kernel symbols in its section 4 table are covered by that diff, without reading each again.

**Also found.**

- The mesh content key is introduced once. `publish_content_key` in `crates/astral-kernel/src/chats.rs`
  always generates a fresh key, and `publish_content_key("mesh")` runs only at recovery confirmation,
  which creates the mesh. R1 needs a second path that introduces an existing key.
- `ContentKeys::accept` in `crates/astral-mesh/src/epochs.rs` needs only that the introducing epoch is
  in the roster with the right group. `fold_content_keys` skips a key it already holds. So holders
  ignore a second introduction and keep the group they recorded. A joiner records it under the join
  epoch.
- `sealing_body_key` in `mesh_core.rs` picks the first `mesh` scope key by id. A second mesh key with
  a new id would not reliably become the sealing key. Reusing the id avoids that.
- `Envelope::epoch_id` in `crates/astral-core/src/event.rs` reads `header_ct[..32]` as the epoch id.
  The log holds an envelope whose epoch it lacks with `event.epoch_unknown`
  (`crates/astral-log/src/log.rs`). Both are the same at `eb9821f4`. Section 6.2 says what follows.

**Apple and Deep.** The Mac and iPhone app approves with `.all` in `RealMeshKernel.swift` and shows a
fixed "History access" of "All chats" in `MeshDeviceDetailView.swift` (Projection `d3a296b3`). Deep and
Vessel pass `HistoryGrant.ALL` in `backend/devtools/dev_mesh.py` and tests.

**Not verified.** The two 2.1.0 behaviours of Phase 0 items 1 and 2. The cost of sealing again at
serve time. Whether any path compares `header_ct` bytes. The full `mesh` scope inventory. Whether the
watch app shows device history. Where the Apple build takes its Kernel. All of these are Phase 0 or
Phase 4 work.

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
| Chat content, personal | Never | Every message sealed by a writer that has folded the join epoch, in old chats and new |
| Chat content, PHI | Never, as for any device outside the phi group | Only if it joins the phi group |
| `mesh` scope bodies | Read, except `phi` and `secret` labelled events (R1) | Read, by the same labels |
| Chat-key rotations (`mesh` scope `key.content`) | Never | Read when under the join epoch or later |
| `key.credential` boxes sealed to it | As today | As today |

A writer that has not folded the join epoch still seals with the chat's earlier key. 106 section 8
says no rule closes that window. Messages written in it stay closed to the joiner.

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

Existing personal epochs follow the same rule. Their epoch key becomes the epoch secret, so every
current member derives a header key for every old personal epoch with no new roster entry. Phi epochs
keep their form.

### 6.2 Envelopes

- A split-format envelope seals its header under the epoch header key. The associated data binds the
  format.
- The leading 32 bytes of `header_ct` name the header key, and that name never equals any epoch id.
  A 2.1.0 Kernel reads those bytes as an epoch id. With a distinct name it holds the envelope with
  `event.epoch_unknown`. If the bytes were the epoch id, a 2.1.0 holder of the epoch secret would try
  that secret and reject the envelope instead of holding it.
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

| Grant | Who writes it |
|---|---|
| Wraps at pairing, full and header-only | The approving member device, with owner presence |
| A later full grant of an earlier epoch | A member device, with owner presence |
| Recovery wraps | As today (R7) |

A device that holds a full grant and a header-only wrap of the same epoch holds the full grant. A
header-only wrap written after a full grant changes nothing.

### 6.4 Sync

- A peer serves a header-only holder the split form of each old envelope, sealing the header again
  when its stored copy is old-form. The stored copy stays as it is (R9).
- A peer withholds a body from a holder that cannot open it by the roster's facts, with reason
  `history`. This joins the `phi`, `secret` and `unknown` reasons of 105 `sync.md` section 5.
- `mesh` scope bodies under the mesh content key are sent, by the label table of 105 `sync.md`
  section 5 (R1).
- Chat-key rotations under the join epoch or later are sent. Earlier ones are withheld with reason
  `history`.
- A header-only holder forwards headers and holds no old bodies.
- When a full wrap of an earlier epoch reaches a header-only holder, it asks for the bodies it lacks.

### 6.5 Capability and activation statements

- A capability statement carries the bindings of R10. Phase 1 evaluates the `phi.capable/<device id>`
  `settings.set` pattern of 106 `phi-destinations.md` section 4 as its carrier, since pre-106 and
  2.1.0 Kernels fold that key as a setting nothing reads. That pattern judges a marker against the
  current roster and binds no `add` entry. The evaluation must say how the `add` binding and the
  removal cutoff are added.
- The activation statement is a signed statement written once, as the owner ruled, with owner
  presence. It cites the roster state and the capability statements of R11.

### 6.6 Left to the Phase 1 contracts

The plan does not name these, so this spec does not either. Each is fixed in its Phase 1 contract.

- The header-only wrap's `purpose` value.
- The split key reference syntax, the split envelope's format binding and the header key's name.
- The carriers of the capability statement and the activation statement.
- How a capability statement travels with a pairing request so the `add` can carry it, since a
  statement bound to an `add` entry cannot be signed before that entry exists.
- The causal order that decides whether an `add` precedes the activation statement.
- The exact position rule of R13.
- What happens to wraps already written for an `add` the fold declines to seat, since that device
  would hold current epoch secrets without being a member.
- The reasons carried by `pairing.history_unavailable`, and how the blocking devices travel with it.
- The code for a pairing request from a device that cannot announce the capability.

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

- An updated device writes the activation statement, with owner presence, once every current
  personal member has a valid capability statement (R10). The statement cites the roster state and
  the capability statements it relies on.
- Whether a statement activates is a fold over the facts it cites and the roster, with the same
  result on every peer.
- Coverage counts every member whose `add` precedes the statement. An `add` concurrent with the
  statement or after it is seated only with a valid capability statement (R12). So a concurrent `add`
  that arrives late never voids an activation that peers have acted on.
- A statement that misses a preceding member does not activate (R11). Two concurrent valid
  statements resolve by a Phase 1 tie-break. After that, a later statement changes nothing.
- From activation, every member writes the split form, and limited history is allowed. An updated
  peer refuses an old-form personal write that follows the statement in its author's causal history
  (R13).
- Before activation, pairing refuses limited history with `pairing.history_unavailable` and names
  the devices that still need an update.
- After activation, a pairing request from a device that cannot announce the capability is refused,
  and the fold does not seat such a device either (R12).
- A member that goes back to an old build after activation stops reading new events. It keeps what
  it had and holds new envelopes aside with `event.epoch_unknown` until it updates (section 6.2).
  Phase 0 proves this on a real 2.1.0 build.

### 7.4 The personal current-key rule, independent of both steps

The rule of 106 `phi-destinations.md` section 8 applies to personal writes. The first personal write
to a chat after a personal epoch change introduces a chat-scoped `key.content` under the current
epoch, with reason `first_write` or `epoch_changed`. It needs no activation.

| Kernel | Starts a rotation | After another device rotates a chat |
|---|---|---|
| Pre-106 | Never | Reads the rotated key's messages, keeps sealing with the `chat.create` key |
| 2.1.0 | Never | Seals with the last chat key it can open by `(clock.ms, clock.n, feed, seq)`, under any epoch |
| Updated | At the first write after an epoch change | Seals with the current key by the 106 rule |

So the removal gap closes for a chat once an updated writer has rotated it. It closes for updated
writers and for a 2.1.0 writer that has folded the rotation. It stays open for messages from a pre-106
writer until it updates. It also stays open for a 2.1.0 writer while a later-sorting key under an
older epoch exists (section 4).

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
| An `add` after activation of a device with no valid capability statement, by a member or the recovery key | Not seated | *Phase 1* |
| A capability statement from a removed feed, or bound to an earlier `add` of the device | Dropped | *Phase 1* |
| A 2.1.0 Kernel meets a split-format envelope, whose header key name is no epoch id (section 6.2) | Held aside with reason `epoch`, feed intact | `event.epoch_unknown` |
| A reader lacks the header key named by the envelope | Held aside, retried when a grant arrives | `event.epoch_unknown` |
| A header-only wrap opened as a full wrap, or the reverse | Refused | `wrap.decrypt_failed` |
| A header-only wrap or a later full grant without owner presence | Rejected | *Phase 1* |
| A header-only wrap for a device and epoch it holds in full | No change | none |
| A header-only holder asks for an old body | Withheld | reason `history` |
| A header-only holder meets an old body some other way | Not opened | `event.key_unknown` |
| A split header edited, or bound to the wrong format | Refused | `event.decrypt_failed` |
| An activation statement that misses a preceding member, or lacks owner presence | Does not activate, on every peer alike | *Phase 1* |
| A second activation statement | No change, by the Phase 1 tie-break | none |
| An old-form personal write that follows activation in its author's causal history | Refused by updated peers | *Phase 1* |
| A personal write when the personal group has no current epoch | Refused | `phi.group_not_ready`, as 106 section 8 row 2 |

## 9. Decisions and calls

### 9.1 Decided on 2026-11-18

The owner delegated open questions to the coordinating session, which decided these after review of
the first draft. They are delegated decisions and stand until the owner rules otherwise. Each is a requirement above.

1. **Activation needs owner presence (R3).** It never reverts and changes every device's write
   format, so a member key alone must not be able to write it.
2. **Seating after activation is a fold rule (R12).** A local pairing refusal does not stop another
   member or the recovery key from adding an old build.
3. **The cited roster covers its members (R11).** The decision was that the cited roster must cover
   every member that precedes or is concurrent with the statement. A statement citing a stale roster
   could activate while a member that cannot read the split form is seated. This spec narrows it,
   pending the coordinating session's confirmation: coverage counts preceding members, and
   concurrent `add` entries go through R12. A statement cannot cite an `add` it has not seen, and a
   late concurrent arrival must never undo an activation, which never reverts.
4. **Capability statements are bound (R10).** A device that announced and then went back to an old
   build must not count.
5. **"After activation" has a boundary (R13).** Without one, writers that fold the statement at
   different times disagree on which writes are allowed.
6. **The `phi.capable/` pattern is evaluated as the capability carrier (section 6.5).** Older Kernels
   fold it as an inert setting. The activation stays one signed statement, as ruled.
7. **Grant authority (R6, section 6.3).** A grant changes what a device can read, so it needs owner
   presence. A header-only wrap must never lower a full grant.

### 9.2 Calls for the owner to check

These are choices this spec makes beyond the plan and the rulings. Each stands unless the owner
says otherwise.

1. **No capability statement before Phase 3.** The plan had Phase 2 kernels announce. This spec
   moves writing it to Phase 3 so a Kernel pin bump for ruling 4, which publishes a Mac release on a
   green `main`, can never ship a Kernel that announces a capability it cannot act on (section 7.2).
2. **No wrap of a personal secret on request (R6).** Today's contracts allow it and the Kernel never
   does it. Limited history needs the ban written down.
3. **The sheet labels.** "All history" and "From now on", with All history the default.

## 10. Out of scope

- Granting one old chat to a limited device. The design allows it later.
- A client control to grant a limited device more history. The Kernel side is in scope, as task T3.7.
- Any change to the phi group, share sessions or caller tickets.
- Shipping `current`. It waits on a later owner decision.
- Editing 106 text in this change. The note on 2.1.0 is task R4.8.
- The Android and Windows clients, which are paused.
- The web shell, which has no pairing surface.

## 11. Acceptance criteria

Each runs in CI through the real Kernel calls unless marked *(live)*, a recorded demonstration.

**Derivation and wire**

- **AC1.** Both derived keys match the Phase 1 vectors for a fixed epoch secret and epoch id.
- **AC2.** An old-form and a split-form envelope of one event open to the same header and event id.
- **AC3.** A header-only wrap does not open as a full wrap, and the reverse, with `wrap.decrypt_failed`.
- **AC4.** A share session over a chat with split-format events serves the same events and wraps as
  before.
- **AC5.** After a peer serves the split form of old envelopes, and after activation, its stored
  envelope bytes, event ids, signatures and chains are unchanged (R9).
- **AC6.** No header key name in the vectors equals an epoch id. The log holds an envelope whose
  leading 32 bytes name no known epoch with `event.epoch_unknown`, with the feed unchanged. A real
  2.1.0 build holds split-form envelopes aside and rejects none *(live)*.

**Read both**

- **AC7.** A mixed mesh of old-form and split-form writers converges in the Kernel's sync tests.
- **AC8.** A header-only holder verifies every chain and opens no `key.content` body of an earlier
  epoch.
- **AC9.** A full peer withholds earlier chat bodies and earlier chat-key rotations from a
  header-only holder with reason `history`. It sends `mesh` scope bodies and rotations under the join
  epoch.
- **AC10.** `phi` and `secret` labelled `mesh` scope events stay withheld from a limited joiner with
  those reasons.
- **AC11.** A Kernel built at Phase 2 writes no capability statement in any test, and the Phase 2
  changes add no code path that writes one.
- **AC12.** A header-only holder holding envelopes aside causes no peer to wrap a secret to it. No
  wrap of a personal secret is written outside the paths of R6.

**Activation**

- **AC13.** Pairing with `from_now` before activation fails with `pairing.history_unavailable`, and
  the refusal lists each member that has not announced.
- **AC14.** An activation statement that misses a member whose `add` precedes it does not activate on
  peers holding different views of the roster. One without owner presence does not activate. Two
  concurrent valid statements resolve to one on every peer, and a later one changes nothing.
- **AC15.** A capability statement from a removed feed is dropped. A device that announced, ran an
  old build and was added again does not count until it announces under its new `add`.
- **AC16.** After activation, a pairing request from a device that cannot announce is refused.
- **AC17.** After activation, an `add` by a member and an `add` by the recovery key, each of a device
  with no valid capability statement, are not seated on any peer. A concurrent `add` of a device with
  a valid statement is seated, and the activation stands.
- **AC18.** After activation, an updated peer refuses an old-form personal write that follows the
  statement in its author's causal history, and accepts one concurrent with it.

**Limited history**

- **AC19.** After a `from_now` join, the joiner reads messages written after the join by a writer
  that has folded the join epoch, in an old chat and in a new chat. It reads none written before the
  join.
- **AC20.** After a `from_now` join, the joiner reads `mesh` scope settings and connections written
  before the join. The second introduction carries the mesh key's `ck:` id and secret, and existing
  holders keep the same key and recorded group.
- **AC21.** The Kernel reports a chat the joiner cannot read as closed, never as an empty chat.
- **AC22.** `current` with more than one personal epoch is still refused.
- **AC23.** A restore with the 24 words after a limited join reads full history.
- **AC24.** After a `from_now` join, the recovery key holds every personal epoch secret, including
  the join epoch, and no phi epoch (R7).
- **AC25.** When a header-only holder receives a full wrap of an earlier epoch, it asks for and opens
  that epoch's bodies, and its history in status changes to match.
- **AC26.** A header-only wrap or a later full grant without owner presence is rejected. A
  header-only wrap for a device and epoch it holds in full changes nothing.

**Personal current-key rule**

- **AC27.** After `remove_device`, the first personal write to an old chat writes a chat-scoped
  `key.content` under the new epoch, and the message seals under that key.
- **AC28.** The removed device, given that message's ciphertext, cannot open it.
- **AC29.** A 2.1.0 Kernel learns the rotated personal key from the event and reads the message
  sealed under it. Its own next write to that chat seals with the rotated key's `ck:` id. In a second
  case a later-sorting chat key under the older epoch also exists. Updated devices seal with the
  rotated key and the 2.1.0 device seals with the older one, recorded as known 2.1.0 behaviour
  *(live)*.
- **AC30.** The `chat_personal_*` cases of `phi-destination-v1.json` pass through the runtime write
  path as well as the pure function.
- **AC31.** The PHI suites pass with their files unchanged: Kernel
  `crates/astral-kernel/tests/phi_labeling.rs` and the other phi tests, and Deep
  `backend/tests/test_phi_node_gates.py` (R8).

**Clients and release**

- **AC32.** Deep's four backend groups pass on the new pin with a limited joiner in the node tests.
- **AC33.** The Mac and iPhone apps pair a device From now on, its device screen shows the join date,
  and a chat it cannot read shows as closed *(live)*.
- **AC34.** A recorded run with one 2.1.0 device and two updated devices: while the old device
  blocks activation and is named, after it updates and announces, and after activation. Then one
  device goes back to the 2.1.0 build and holds new envelopes aside *(live)*.
