# Tasks for spec 107

**Date:** 2026-11-18. **Status:** not started.
**Spec:** [spec.md](spec.md). **Plan:** [plan.md](plan.md). **Tracking:** issue 681.

## How to read this list

Tasks run in the order listed unless "Depends on" says otherwise. Each names its repository, the
files or area, the test that proves it, and what must land first.

| Tag | Meaning |
|---|---|
| **contract first** | Needs its Phase 1 contract, vectors and reference merged before code starts |
| **ruling 4** | Part of the personal current-key rule. Ships without limited history and without activation |
| **waits on owner** | Blocked until the owner decides on `current` after `from_now` is out |
| *(live)* | A recorded demonstration, not a CI gate |

Kernel paths are in AstralKernel. Projection paths are under `apple-clients/AstralApp/AstralApp/Mesh/`
in AstralProjection. Deep paths are in this repository. Kernel topic branches may be pushed once
their tests pass. Kernel releases, Deep merges and app releases need the owner's word, and every
green push to Deep `main` publishes a Mac release.

## Track R4: the personal current-key rule

This track lands first. It shares `mesh_core.rs` and `chats.rs` with Phase 3, so landing it early
avoids rework. No contract changes: 106 `phi-destinations.md` sections 8 and 10 already specify it,
and its vectors already carry the personal cases (spec section 4).

| ID | Task | Repo | Files or area | Proof | Depends on | Tags |
|---|---|---|---|---|---|---|
| R4.1 | Route every personal chat write through `chat_write_key`, rotating with `first_write` or `epoch_changed` before the seal | AstralKernel | `crates/astral-kernel/src/mesh_core.rs` (`prepare_seal`, `sealing_body_key`), `phi_chat_key.rs` (generalise `prepare_phi_chat` to a group), `chats.rs`, `shares.rs` | New `crates/astral-kernel/tests/personal_chat_key.rs`: after `remove_device`, the first write to an old chat writes a chat-scoped `key.content` under the new epoch and seals under it (AC16). The removed device cannot open that body (AC17). Each fails without the change | none | ruling 4 |
| R4.2 | Make the fold pick the current personal key by the section 8 rule: current epoch only, latest by `(clock.ms, clock.n, feed, seq)` | AstralKernel | `fold_content_keys` in `mesh_core.rs`, `chat_keys` cache | Two devices rotate one chat under different epochs and both seal with the same key. A late-sorting key under an older epoch is never chosen | R4.1 | ruling 4 |
| R4.3 | Drive the `chat_personal_*` vectors through the runtime write path | AstralKernel | `crates/astral-kernel/src/phi_vector_tests.rs` or a new test beside it | Every `chat_personal_*` case of `tests/fixtures/qualified/deep/vectors/phi-destination-v1.json` gives the same decision through a real write (AC19) | R4.1 | ruling 4 |
| R4.4 | Prove shares still rotate and settle | AstralKernel | `crates/astral-kernel/tests/share_revocation.rs` | Revocation after an `epoch_changed` rotation still writes a newer key, and `settle_rotations` treats both reasons alike | R4.1, R4.2 | ruling 4 |
| R4.5 | Recorded run against a 2.1.0 Kernel | AstralKernel, result in Deep | A Kernel built at `eb9821f4` beside one with R4.1. Result in `specs/107-epoch-key-split/phase-0/` | The 2.1.0 Kernel learns the rotated key from the event and reads the message (AC18) *(live)* | R4.1, R4.2 | ruling 4 |
| R4.6 | Kernel pin in Deep and Vessel | AstralDeep | `config/astral-composition.json`, `components/AstralKernel`, `scripts/verify_composition.py` | Composition check, Deep's four backend groups, the Vessel gate | R4.1 to R4.5 merged in the Kernel, a Kernel release on the owner's word | ruling 4 |
| R4.7 | Apple apps on the new Kernel | AstralProjection | Where the Apple build takes its Kernel | `swift test` for `AstralCore`, the unsigned `xcodebuild` schemes. Remove a device on the Mac and write to an old chat *(live)* | R4.6 | ruling 4 |

## Phase 0: evidence

Each task ends with a written result in `specs/107-epoch-key-split/phase-0/` in AstralDeep. These
are findings, not CI gates.

| ID | Task | Repo | Files or area | Proof | Depends on | Tags |
|---|---|---|---|---|---|---|
| T0.1 | What a 2.1.0 Kernel does with an envelope whose leading 32 bytes it does not know | AstralKernel | Kernel at `eb9821f4`, `crates/astral-log/src/log.rs` hold path | Held aside with reason `epoch`, feed intact, opened after an update. Checked in a test and on a real 2.1.0 build *(live)* | none | |
| T0.2 | What a 2.1.0 Kernel does with a roster entry it rejects during sync | AstralKernel | Kernel at `eb9821f4`, `crates/astral-mesh/src/roster.rs` | The entry lands in `rejected`, the store and every other entry stay intact | none | |
| T0.3 | Inventory of `mesh` scope: every event kind, what its body holds, and whether a limited joiner needs it | AstralKernel, result in Deep | `crates/astral-core/src/event.rs` event types, writers of `Scope::Mesh` | A table of every kind. It feeds R1 and the approval sheet's text | none | |
| T0.4 | Cost of sealing headers again at serve time on a log of realistic size | AstralKernel | `crates/astral-log` serve path | A recorded measurement. No wall-clock bound enters CI | none | |
| T0.5 | Find any code that treats `header_ct` bytes as identity | AstralKernel, AstralDeep | Kernel `astral-log` stores, `astral-mailbox`, `astral-net`, `crates/astral-core/src/share_session.rs`. Deep backend relays and node code | A list of every use with a verdict. Fixes become T2.6 | none | |

## Phase 1: contracts

Contracts live in AstralDeep under `specs/107-epoch-key-split/contracts/`, each with its `reference/` module and
`vectors/` in the same change. Each is finished when its reference tests pass on its new vectors.
Every task here is a contract change. Tasks in Phases 2 and 3 wait on the ones they name.

| ID | Task | Repo | Files or area | Proof | Depends on |
|---|---|---|---|---|---|
| T1.1 | Keys: derivation and both info texts, the header-only wrap and its purpose, the backup note, recovery unchanged. Revise 105 `keys.md` section 2 so no member wraps a personal secret on request (R6) | AstralDeep | `contracts/keys.md` from 105 `keys.md` sections 2 to 6 | Reference tests on new `keys` vectors, including both wraps failing as each other | none |
| T1.2 | Events: the split envelope, its format binding, the split key reference form, error codes | AstralDeep | `contracts/event.md` from 105 `event.md` sections 2, 6, 7, 11 | Both forms of one event open to the same header and event id in the reference | T1.1, T0.1, T0.5 |
| T1.3 | Roster: the three history choices, the header-only grant entry, the capability and activation carriers if they are roster entries, `pairing.history_unavailable` and its reasons, the code for a request that cannot announce | AstralDeep | `contracts/roster.md` from 105 `roster.md` section 8 and 106 `roster-v2.md` | Roster vectors fold a header-only grant, reject an activation written too early, and ignore a second one | T1.1, T0.2 |
| T1.4 | Sync: serving header-only holders, reason `history`, `mesh` scope bodies sent, the statements if they are events. Revise 105 `sync.md` section 4 step 1 (R6) | AstralDeep | `contracts/sync.md` from 105 `sync.md` sections 4 and 5 | Sync vectors plan `send` and `withheld` for a header-only holder | T1.2, T1.3, T0.3, T0.4 |
| T1.5 | PHI: revise 106 `phi-destinations.md` line 140, and any `ek:` text the new reference form touches | AstralDeep | `contracts/phi-destinations.md` | `phi-destination` vectors still pass, with new cases for split key references | T1.2 |
| T1.6 | Share: classify split key references by group with no change in behaviour | AstralDeep | `contracts/share.md` from 106 `share.md` section 5 | `share` vectors unchanged, plus cases with split references | T1.2 |
| T1.7 | Vendor the new vectors into the Kernel and wire their tests | AstralKernel | `tests/fixtures/qualified/deep/vectors/`, vector test files | Kernel vector tests read the new files and pass on the reference outputs | T1.1 to T1.6 |

## Phase 2: Kernel reads both

Writers keep the old form. No capability statement is written (spec section 7.2).

| ID | Task | Repo | Files or area | Proof | Depends on | Tags |
|---|---|---|---|---|---|---|
| T2.1 | Derived keys, both envelope forms, the header-only wrap | AstralKernel | `crates/astral-core` (`KeyRing`, `SealRequest`, `seal_event_with`, `open_header`, `wrap_epoch_key`) | AC1, AC2, AC3 on the vendored vectors | T1.1, T1.2, T1.7 | contract first |
| T2.2 | Header-only holdings and folding header-only grants | AstralKernel | `crates/astral-mesh` (`EpochKeys`, roster fold, `join_from_bundle`) | Scenario tests fold a header-only grant into a holding that opens headers only | T2.1, T1.3 | contract first |
| T2.3 | Header key lookup, sealing again at serve time, reason `history`, `mesh` scope bodies sent | AstralKernel | `crates/astral-log` (`KeyProvider`, `envelopes` and `held` rows, sync planning) | AC6, AC7 | T2.1, T2.2, T1.4 | contract first |
| T2.4 | Mixed old-form and split-form writers converge | AstralKernel | `crates/astral-kernel/tests/` sync tests, split writer in the testkit only | AC5 | T2.3 | contract first |
| T2.5 | Read and fold other devices' capability statements | AstralKernel | `crates/astral-mesh` or `astral-log`, per the T1.3 or T1.4 carrier | A Phase 2 build folds a statement and writes none (AC8) | T2.2, T1.3, T1.4 | contract first |
| T2.6 | Fix each `header_ct` identity use that T0.5 found | AstralKernel | Per T0.5 | One test per fix that fails without it | T0.5, T2.1 | contract first |

## Phase 3: activation and limited history


| ID | Task | Repo | Files or area | Proof | Depends on | Tags |
|---|---|---|---|---|---|---|
| T3.1 | Write the capability statement, and write and validate the activation statement once | AstralKernel | `crates/astral-mesh` or `astral-log` per the carrier, `crates/astral-kernel` | AC10 | T2.5, R4.1 | contract first |
| T3.2 | Write the split form after activation, with personal `key.content` under the introduction key | AstralKernel | `crates/astral-core`, `crates/astral-kernel/src/chats.rs`, `mesh_core.rs` | After activation every new envelope is split-form, and a header-only holder never opens a new `key.content` of an epoch it lacks | T3.1 | contract first |
| T3.3 | `from_now` grants: header-only wraps of earlier epochs, a new epoch, and the mesh content key introduced again under it (R1) | AstralKernel | `crates/astral-mesh/src/pairing.rs` (`approve_request`), `writer.rs` (`grant_epochs`) | AC12, AC13 | T3.2, R4.1, R4.2 | contract first |
| T3.4 | Lift the refusal behind activation for `from_now` only, naming blocking devices. Keep `current` refused with more than one epoch | AstralKernel | `crates/astral-kernel/src/pairing.rs` (`require_readable_history`, `check_history`), `crates/astral-kernel/tests/limited_history_sync.rs` | AC9, AC14 | T3.1 | contract first |
| T3.5 | Refuse a pairing request that cannot announce, after activation | AstralKernel | `crates/astral-kernel/src/pairing.rs` | AC11 | T3.1 | contract first |
| T3.6 | Recovery and backup after a limited join | AstralKernel | `crates/astral-mesh/src/recovery.rs`, `crates/astral-kernel/src/recovery.rs` | AC15. A backup from a header-only holder restores old metadata only | T3.3 | contract first |
| T3.7 | Activation state, blocking devices and each device's history in status | AstralKernel | `crates/astral-kernel-ffi/src/records.rs`, the Python binding | Binding tests read all three for a limited joiner | T3.1, T3.3 | contract first |

## Phase 4: consumers

| ID | Task | Repo | Files or area | Proof | Depends on | Tags |
|---|---|---|---|---|---|---|
| T4.1 | Kernel release | AstralKernel | Release tag | Kernel CI green on the tagged commit | Phase 3, the owner's word | |
| T4.2 | Kernel pin, and node tests with a limited joiner | AstralDeep | `config/astral-composition.json`, `components/AstralKernel`, `backend/devtools/dev_mesh.py`, `backend/tests/test_node_mesh_kernel_integration.py`, `vessel/tests/` | AC20, the composition check, the Vessel gate | T4.1 | |
| T4.3 | The history choice at approval, unavailable before activation with blocking devices named | AstralProjection | `RealMeshKernel.swift`, `MeshKernelAPI.swift`, `InMemoryMeshKernel.swift`, `MeshPairingViews.swift`, `MeshRuntime.swift` | `swift test` for `AstralCore`, the unsigned `xcodebuild` schemes, UI tests for both states | T3.7, T4.1 | |
| T4.4 | The device screen shows each device's history from the roster | AstralProjection | `MeshDeviceDetailView.swift` | UI tests for all history and from the join date | T3.7, T4.1 | |
| T4.5 | Pair a device From now on on the Mac and the iPhone | AstralProjection, AstralDeep | Live run against the seeded test mesh | AC21 *(live)* | T4.2, T4.3, T4.4 | |

## Phase 5: mixed versions and release

| ID | Task | Repo | Proof | Depends on |
|---|---|---|---|---|
| T5.1 | One 2.1.0 device and two updated devices, before activation, at activation and after the old device updates | AstralProjection, AstralDeep | AC22 *(live)*, result in `specs/107-epoch-key-split/` | T4.5 |
| T5.2 | Release in order: Kernel, then the Deep pin, then the apps | All three | Each release's own gates | T5.1, the owner's word |

## `current`: waits on the owner

Nothing here starts before `from_now` is released and the owner decides on `current`.

| ID | Task | Repo | Files or area | Proof | Depends on | Tags |
|---|---|---|---|---|---|---|
| TC.1 | The owner decides whether `current` ships | none | Recorded in this folder | A written ruling | T5.2 | waits on owner |
| TC.2 | Contract text: `current` only when the current epoch began after activation | AstralDeep | `contracts/roster.md`, vectors | Reference tests | TC.1 | waits on owner |
| TC.3 | Allow `current` under that rule | AstralKernel | `crates/astral-kernel/src/pairing.rs`, `crates/astral-mesh/src/pairing.rs` | A joiner reads the current epoch and is refused when that epoch began before activation | TC.2 | waits on owner, contract first |
| TC.4 | Offer `current` on the approval sheet | AstralProjection | `MeshPairingViews.swift` | UI tests, then a live pairing *(live)* | TC.3 | waits on owner |
