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
avoids rework. It changes no contract rule: 106 `phi-destinations.md` sections 8 and 10 already
specify it, and its vectors already carry the personal cases (spec section 4). R4.8 adds a note on
2.1.0 to the 106 text.

| ID | Task | Repo | Files or area | Proof | Depends on | Tags |
|---|---|---|---|---|---|---|
| R4.1 | Route every personal chat write through `chat_write_key`, rotating with `first_write` or `epoch_changed` before the seal. Refactor `prepare_phi_chat` so personal writes share it, with no change in PHI behaviour | AstralKernel | `crates/astral-kernel/src/mesh_core.rs` (`prepare_seal`, `sealing_body_key`), `phi_chat_key.rs`, `chats.rs`, `shares.rs` | New `crates/astral-kernel/tests/personal_chat_key.rs`: after `remove_device`, the first write to an old chat writes a chat-scoped `key.content` under the new epoch and seals under it (AC27). The removed device cannot open that body (AC28). Each fails without the change. The PHI suites pass with their files unchanged (AC31) | none | ruling 4 |
| R4.2 | Make the fold pick the current personal key by the section 8 rule: current epoch only, latest by `(clock.ms, clock.n, feed, seq)`, and the `chat.create` key only under the current epoch | AstralKernel | `fold_content_keys` in `mesh_core.rs`, `chat_keys` cache | Two devices rotate one chat under different epochs and both seal with the same key. A late-sorting key under an older epoch is never chosen | R4.1 | ruling 4 |
| R4.3 | Drive the `chat_personal_*` vectors through the runtime write path | AstralKernel | `crates/astral-kernel/src/phi_vector_tests.rs` or a new test beside it | Every `chat_personal_*` case of `tests/fixtures/qualified/deep/vectors/phi-destination-v1.json` gives the same decision through a real write (AC30) | R4.1 | ruling 4 |
| R4.4 | Prove shares still rotate and settle | AstralKernel | `crates/astral-kernel/tests/share_revocation.rs` | Revocation after an `epoch_changed` rotation still writes a newer key, and `settle_rotations` treats both reasons alike | R4.1, R4.2 | ruling 4 |
| R4.5 | Recorded run against a 2.1.0 Kernel | AstralKernel, result in Deep | A Kernel built at `eb9821f4` beside one with R4.1 and R4.2. Result in `specs/107-epoch-key-split/phase-0/` | Both cases of AC29: the 2.1.0 Kernel seals its next write with the rotated key, and with a later-sorting older-epoch key present it seals with that one *(live)* | R4.1, R4.2 | ruling 4 |
| R4.6 | Kernel pin in Deep and Vessel | AstralDeep | `config/astral-composition.json`, `components/AstralKernel`, `scripts/verify_composition.py` | Composition check, Deep's four backend groups including `backend/tests/test_phi_node_gates.py`, the Vessel gate | R4.1 to R4.5 merged in the Kernel, a Kernel release on the owner's word | ruling 4 |
| R4.7 | Apple apps on the new Kernel | AstralProjection | Where the Apple build takes its Kernel | `swift test` for `AstralCore`, the unsigned `xcodebuild` schemes. Remove a device on the Mac and write to an old chat *(live)* | R4.6 | ruling 4 |
| R4.8 | Add to 106 `phi-destinations.md` the last paragraph of section 8 and the section 10 row "Chats after a personal epoch change": their "deployed kernel" is the pre-106 `c2c1cc4`, and 2.1.0 adopts a rotated key with no current-epoch filter | AstralDeep | `specs/106-sign-in-free-clients/contracts/phi-destinations.md` | No rule or vector changes. `scripts/check_doc_links.py` and the 106 reference tests pass | none | ruling 4 |

## Phase 0: evidence

Each task ends with a written result in `specs/107-epoch-key-split/phase-0/` in AstralDeep. These
are findings, not CI gates.

| ID | Task | Repo | Files or area | Proof | Depends on | Tags |
|---|---|---|---|---|---|---|
| T0.1 | What a 2.1.0 Kernel does with an envelope whose leading 32 bytes are no epoch it holds, and with one whose leading 32 bytes are an epoch id it holds but whose header is sealed under another key | AstralKernel | Kernel at `eb9821f4`, `Envelope::epoch_id` in `crates/astral-core/src/event.rs`, the hold path in `crates/astral-log/src/log.rs` | The first is held aside with reason `epoch`, feed intact, and opened after an update. The second is rejected, which is why the header key needs its own name (spec section 6.2). Checked in a test and on a real 2.1.0 build, the recorded part of AC6 *(live)* | none | |
| T0.2 | What a 2.1.0 Kernel does with a roster entry it rejects during sync | AstralKernel | Kernel at `eb9821f4`, `crates/astral-mesh/src/roster.rs` | The entry lands in `rejected`, the store and every other entry stay intact | none | |
| T0.3 | Inventory of `mesh` scope: every event kind, what its body holds, its labels, and whether a limited joiner needs it | AstralKernel, result in Deep | `crates/astral-core/src/event.rs` event types, writers of `Scope::Mesh` | A table of every kind. It feeds R1 and the approval sheet's text | none | |
| T0.4 | Cost of sealing headers again at serve time on a log of realistic size | AstralKernel | `crates/astral-log` serve path | A recorded measurement. No wall-clock bound enters CI | none | |
| T0.5 | Find any code that treats `header_ct` bytes as identity | AstralKernel, AstralDeep | Kernel `astral-log` stores, `astral-mailbox`, `astral-net`, `crates/astral-core/src/share_session.rs`. Deep backend relays and node code | A list of every use with a verdict. Fixes become T2.6 | none | |

## Phase 1: contracts

Contracts live in AstralDeep under `specs/107-epoch-key-split/contracts/`, each with its `reference/`
module and `vectors/` in the same change. Each is finished when its reference tests pass on its new
vectors. Every task here is a contract change. Tasks in Phases 2 and 3 wait on the ones they name.

| ID | Task | Repo | Files or area | Proof | Depends on |
|---|---|---|---|---|---|
| T1.1 | Keys: derivation and both info texts, the header-only wrap and its purpose, who writes each grant with owner presence, a header-only wrap never shadowing a full grant, the backup note, recovery unchanged. Revise 105 `keys.md` section 2 so no member wraps a personal secret on request (R6) | AstralDeep | `contracts/keys.md` from 105 `keys.md` sections 2 to 6 | Reference tests on new `keys` vectors, including both wraps failing as each other and a header-only wrap after a full grant changing nothing | none |
| T1.2 | Events: the split envelope, its format binding, a header key name that never equals an epoch id, the split key reference form, error codes | AstralDeep | `contracts/event.md` from 105 `event.md` sections 2, 6, 7, 11 | Both forms of one event open to the same header and event id in the reference. No vector's header key name equals an epoch id | T1.1, T0.1, T0.5 |
| T1.3 | Roster grants: the three history choices, with `current` designed in shape only and not enabled, the header-only grant entry, `pairing.history_unavailable` and its reasons, the code for a request that cannot announce | AstralDeep | `contracts/roster.md` from 105 `roster.md` section 8 and 106 `roster-v2.md` | Roster vectors fold a header-only grant and refuse `current` with more than one epoch | T1.1, T0.2 |
| T1.4 | Capability and activation statements. Evaluate the `phi.capable/<device id>` pattern of 106 `phi-destinations.md` section 4 as the capability carrier, and say what it lacks: the `add` binding and the removal cutoff. Fix the bindings of R10, how a statement travels with a pairing request, owner presence on activation, coverage of preceding members (R11), seating after activation for member and recovery `add` entries (R12), the causal order for "precedes", the R13 position rule, and the tie-break | AstralDeep | `contracts/roster.md`, `contracts/sync.md` if a statement is an event | Vectors: a statement missing a preceding member does not activate under different roster views. A concurrent `add` with and without a capability statement. A recovery `add` of a device that cannot announce is not seated. A statement from a removed feed is dropped. A re-added device counts only after it announces again. Two concurrent activations resolve to one. An old-form write after activation in its author's history is refused | T1.3 |
| T1.5 | Sync: serving header-only holders, reason `history`, `mesh` scope bodies by the label table, chat-key rotations by epoch. Revise 105 `sync.md` section 4 step 1 (R6) | AstralDeep | `contracts/sync.md` from 105 `sync.md` sections 4 and 5 | Sync vectors plan `send` and `withheld` for a header-only holder, including `phi` and `secret` labelled `mesh` events and rotations on both sides of the join epoch | T1.2, T1.3, T0.3, T0.4 |
| T1.6 | PHI: revise 106 `phi-destinations.md` line 140, and any `ek:` text the new reference form touches | AstralDeep | `contracts/phi-destinations.md` | `phi-destination` vectors still pass, with new cases for split key references | T1.2 |
| T1.7 | Share: classify split key references by group with no change in behaviour | AstralDeep | `contracts/share.md` from 106 `share.md` section 5 | `share` vectors unchanged, plus cases with split references | T1.2 |
| T1.8 | New vectors for both envelope forms in the remaining suites the plan names: 105 `mailbox`, 106 `contacts` and `roster-v2` | AstralDeep | `contracts/vectors/`, `contracts/reference/` | Reference tests pass on both forms, and the old-form outputs equal today's | T1.2 |
| T1.9 | Vendor the new vectors into the Kernel and wire their tests | AstralKernel | `tests/fixtures/qualified/deep/vectors/`, vector test files | Kernel vector tests read the new files and pass on the reference outputs | T1.1 to T1.8 |

## Phase 2: Kernel reads both

Writers keep the old form. No capability statement is written (spec section 7.2).

| ID | Task | Repo | Files or area | Proof | Depends on | Tags |
|---|---|---|---|---|---|---|
| T2.1 | Derived keys, both envelope forms with the distinct header key name, the header-only wrap | AstralKernel | `crates/astral-core` (`KeyRing`, `SealRequest`, `seal_event_with`, `open_header`, `epoch_id`, `wrap_epoch_key`) | AC1, AC2, AC3 and the CI part of AC6 on the vendored vectors | T1.1, T1.2, T1.9 | contract first |
| T2.2 | Header-only holdings and folding header-only grants | AstralKernel | `crates/astral-mesh` (`EpochKeys`, roster fold, `join_from_bundle`) | Scenario tests fold a header-only grant into a holding that opens headers only | T2.1, T1.3 | contract first |
| T2.3 | Header key lookup, sealing again at serve time, reason `history`, `mesh` scope bodies by label, rotations by epoch | AstralKernel | `crates/astral-log` (`KeyProvider`, `envelopes` and `held` rows, sync planning) | AC5, AC8, AC9, AC10, AC12 | T2.1, T2.2, T1.5 | contract first |
| T2.4 | Mixed old-form and split-form writers converge, and share sessions are unchanged | AstralKernel | `crates/astral-kernel/tests/` sync tests and `share_revocation.rs`, split writer in the testkit only | AC4, AC7 | T2.3, T1.7 | contract first |
| T2.5 | Read and fold other devices' capability statements | AstralKernel | `crates/astral-mesh` or `astral-log`, per the T1.4 carrier | A Phase 2 build folds a statement and writes none, and adds no code path that writes one (AC11) | T2.2, T1.4 | contract first |
| T2.6 | Fix each `header_ct` identity use that T0.5 found | AstralKernel | Per T0.5 | One test per fix that fails without it | T0.5, T2.1 | contract first |

## Phase 3: activation and limited history

Phase 3 turns on the split form, the statements and `from_now`. Every task needs its Phase 1
contract, and those that touch chat keys also need R4.1 and R4.2.

| ID | Task | Repo | Files or area | Proof | Depends on | Tags |
|---|---|---|---|---|---|---|
| T3.1 | Write the capability statement. Write the activation statement once with owner presence, and fold its validity from the facts it cites and the roster | AstralKernel | `crates/astral-mesh` or `astral-log` per the carrier, `crates/astral-kernel` | AC14, AC15 | T2.5, R4.1 | contract first |
| T3.2 | Write the split form after activation, with personal `key.content` under the introduction key. Refuse old-form personal writes after activation in their author's history | AstralKernel | `crates/astral-core`, `crates/astral-kernel/src/chats.rs`, `mesh_core.rs`, `crates/astral-log` accept path | AC18. After activation every new envelope is split-form, and a header-only holder never opens a new `key.content` of an epoch it lacks | T3.1 | contract first |
| T3.3 | `from_now` grants: header-only wraps of earlier epochs, a new epoch, and the existing mesh content key introduced again under it with the same `ck:` id and secret (R1). Never a chat-scoped key | AstralKernel | `crates/astral-mesh/src/pairing.rs` (`approve_request`), `writer.rs` (`grant_epochs`). A new path beside `publish_content_key` in `crates/astral-kernel/src/chats.rs`, which always generates a fresh key. Read `ContentKeys::accept` and `introduced_under` in `crates/astral-mesh/src/epochs.rs` (around lines 317 to 345), the skip in `fold_content_keys` (`mesh_core.rs` around 399) and `sealing_body_key` (around 789) | AC19, AC20. A test of a second introduction of an already known key: holders keep the same key and group, and the joiner opens old `mesh` bodies with it | T3.2, R4.1, R4.2 | contract first |
| T3.4 | Lift the refusal behind activation for `from_now` only, naming blocking devices. Keep `current` refused with more than one epoch | AstralKernel | `crates/astral-kernel/src/pairing.rs` (`require_readable_history`, `check_history`), `crates/astral-kernel/tests/limited_history_sync.rs` | AC13, AC22 | T3.1, T3.3 | contract first |
| T3.5 | Refuse a pairing request that cannot announce, after activation, and do not seat such a device in the fold for member and recovery `add` entries | AstralKernel | `crates/astral-kernel/src/pairing.rs`, the roster fold in `crates/astral-mesh`, `crates/astral-mesh/src/recovery.rs` | AC16, AC17 | T3.1 | contract first |
| T3.6 | Recovery and backup after a limited join | AstralKernel | `crates/astral-mesh/src/recovery.rs`, `crates/astral-kernel/src/recovery.rs` | AC23, AC24. A backup from a header-only holder restores old metadata only | T3.3 | contract first |
| T3.7 | Grant a header-only holder an earlier epoch in full, with owner presence, and have it ask for the bodies it lacks | AstralKernel | `crates/astral-mesh/src/writer.rs`, `crates/astral-log` sync planning | AC25, AC26 | T3.3 | contract first |
| T3.8 | Activation state, blocking devices, each device's history and closed chats in status | AstralKernel | `crates/astral-kernel-ffi/src/records.rs`, the Python binding | Binding tests read all four for a limited joiner, and the history after T3.7. AC21 | T3.1, T3.3, T3.7 | contract first |

## Phase 4: consumers

| ID | Task | Repo | Files or area | Proof | Depends on | Tags |
|---|---|---|---|---|---|---|
| T4.1 | Kernel release | AstralKernel | Release tag | Kernel CI green on the tagged commit | Phase 3, the owner's word | |
| T4.2 | Kernel pin, and node tests with a limited joiner | AstralDeep | `config/astral-composition.json`, `components/AstralKernel`, `backend/devtools/dev_mesh.py`, `backend/tests/test_node_mesh_kernel_integration.py`, `vessel/tests/` | AC32, the composition check, the Vessel gate | T4.1 | |
| T4.3 | The history choice at approval, unavailable before activation with blocking devices named | AstralProjection | `RealMeshKernel.swift`, `MeshKernelAPI.swift`, `InMemoryMeshKernel.swift`, `MeshPairingViews.swift`, `MeshRuntime.swift` | `swift test` for `AstralCore`, the unsigned `xcodebuild` schemes, UI tests for both states | T3.8, T4.1 | |
| T4.4 | The device screen shows each device's history from the roster | AstralProjection | `MeshDeviceDetailView.swift` | UI tests for all history and from the join date | T3.8, T4.1 | |
| T4.5 | A chat a limited device cannot read shows as closed in the chat list and the chat view | AstralProjection | The chat list and chat views, found at the start of the task | UI tests for a closed chat beside a readable one | T3.8, T4.1 | |
| T4.6 | Pair a device From now on on the Mac and the iPhone | AstralProjection, AstralDeep | Live run against the seeded test mesh | AC33 *(live)* | T4.2 to T4.5 | |

## Phase 5: mixed versions and release

| ID | Task | Repo | Proof | Depends on |
|---|---|---|---|---|
| T5.1 | One 2.1.0 device and two updated devices, before activation, at activation and after the old device updates | AstralProjection, AstralDeep | AC34 *(live)*, result in `specs/107-epoch-key-split/` | T4.6 |
| T5.2 | Release in order: Kernel, then the Deep pin, then the apps | All three | Each release's own gates | T5.1, the owner's word |

## `current`: waits on the owner

Nothing here starts before `from_now` is released and the owner decides on `current`.

| ID | Task | Repo | Files or area | Proof | Depends on | Tags |
|---|---|---|---|---|---|---|
| TC.1 | The owner decides whether `current` ships | none | Recorded in this folder | A written ruling | T5.2 | waits on owner |
| TC.2 | Contract text: `current` only when the current epoch began after activation | AstralDeep | `contracts/roster.md`, vectors | Reference tests | TC.1 | waits on owner |
| TC.3 | Allow `current` under that rule | AstralKernel | `crates/astral-kernel/src/pairing.rs`, `crates/astral-mesh/src/pairing.rs` | A joiner reads the current epoch and is refused when that epoch began before activation | TC.2 | waits on owner, contract first |
| TC.4 | Offer `current` on the approval sheet | AstralProjection | `MeshPairingViews.swift` | UI tests, then a live pairing *(live)* | TC.3 | waits on owner |
