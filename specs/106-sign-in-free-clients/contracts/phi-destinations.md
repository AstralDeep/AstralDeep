# PHI destinations `astral.phi-destination/v1`

This contract fixes who holds PHI and where PHI may go (spec section 8.3, C18 and O14). The PHI group is the owner's upgraded devices, and the owner may exclude one. A model endpoint, an outside service or a contact receives PHI only after the owner approves it by name, one destination per entry, with a presence signature. The contract also fixes how a chat starts holding PHI and keeps its PHI key current. The Kernel, the node and the Apple apps implement against this document and its vectors, and phase 6 sharing builds on its contact kind.

Existing roster v2 stores load unchanged. This contract adds no event type, no roster entry type and no roster field. It gives new meaning to the `phi_authority` and `phi_approval` entries that every deployed kernel already folds, and it adds one `settings.set` key that deployed kernels fold as an unread setting.

Reference code: `reference/phi_destinations.py`. Generator: `reference/generate_phi_destination_vectors.py`, which also writes the labels v2 vectors of [labels-v2.md](labels-v2.md). Vectors: `vectors/phi-destination-v1.json`. LETS-CJ/1, hashes, signatures, wraps and the PHI statement check come from the preserved 105 reference.

It supersedes 105 [roster.md](../../105-decentralized-personal-mesh/contracts/roster.md) section 7 and the `phi` group line of 105 [keys.md](../../105-decentralized-personal-mesh/contracts/keys.md) section 2.

## 1. Carrier

The group and every destination ride on the roster entries `phi_authority` and `phi_approval` of [roster-v2.md](roster-v2.md), with their bodies, presence rules and fold unchanged. A deployed kernel at `c2c1cc4` therefore folds the same `phi_members`, phi epochs and wraps as an upgraded one, and accepts every phi epoch and wrap an upgraded kernel writes. A kernel at `c2c1cc4` folds every sequence step of the vectors to the same summary.

The wire keeps the names `phi_authority`, `institution` and `astral.attestation/v1`, against the wording of C18. No institution key remains. Every authority this contract writes is one of the owner's devices.

A carrier the deployed fold cannot read would split the mesh: a phi epoch wrapping a device outside the deployed fold's `phi_members` is refused there (`roster.epoch_wrong_members`), and the two kernels would then disagree on who holds PHI. So no new entry type, roster version or consent kind carries any of this.

## 2. Approvers and approvals

### 2.1 The approver key

Each device of kind `desktop`, `pocket` or `satellite` generates a software P-256 approver key the first time it approves anything. It keeps the key in store meta `phi.approver.v1`, sealed to its own wrap key, and leaves it out of `export_backup`. The key never appears in a `Debug` form, a log line or an FFI record.

The device registers the key with a `phi_authority` add whose body is:

```
{"action": "add", "institution": {"key": <approver public key>, "name": "astral-device:<device id>"}}
```

The entry is authored by that device and carries its presence signature, as every `phi_authority` entry does. A device is **registered** while some current authority's newest live add was authored by it and is named `astral-device:` followed by its own id. A node never registers an approver.

A restored device has a new identity and so a new device id. It generates a new approver key and registers it. The old device's destination approvals stop counting once that device is removed (section 7.2), and the owner approves them again from a current device.

### 2.2 The approval statement

An approval is the 105 PHI statement, unchanged: exactly `kind` (`phi`), `institution` (the approver public key), `subject_kind`, `subject`, `issued_s`, `not_after_s` and `sig`, signed by the approver key under `astral.attestation/v1`.

- `issued_s` is the whole seconds of the clock of the entry that records it.
- `not_after_s` is `issued_s` plus 631152000 seconds (20 years). Approvals stand until revoked (owner decision OD9).
- `subject_kind` is `device` for a member of the group and `endpoint` for every destination, a contact included. Writers never use `node`.

The statement grants nothing alone. It counts only inside a `phi_approval` grant entry authored by a current member with its presence signature. A revoke is `{"action": "revoke", "subject": <subject>}`, also with presence. One entry records one statement, so one approval names one destination.

The binding passes the number of entries in one presence session to the host, so a host that can reuse one authentication signs a batch with one prompt (OD12). Without host support each entry prompts once.

## 3. The fold

The fold is the roster v2 fold, a pure function of the set of entries. This section restates the parts that decide the group and the destinations. The reference mirrors the Kernel's `roster.rs` for every entry type.

### 3.1 Checks on one entry

Each entry is checked against the state of its own active ancestors, so an entry concurrent with the grants it needs is refused. The first failure is the entry's rejection.

| Step | Check | Code |
|---|---|---|
| 1 | Envelope and body shape of roster v2. `phi_authority` and `phi_approval` carry `presence_sig` from a device author | `roster.malformed` |
| 2 | Recovery may write `phi_authority`, `epoch` and `wrap`, never `phi_approval` | `roster.recovery_not_allowed` |
| 3 | A device author is a current member | `roster.author_removed`, `roster.unauthorized_author` |
| 4 | `sig`, then `presence_sig` | `roster.bad_signature`, `roster.bad_presence_signature` |
| 5 | A phi epoch is not written by recovery | `roster.recovery_not_allowed` |
| 6 | `phi_authority` remove names a current authority key | `roster.unknown_authority` |
| 7 | `phi_approval` grant: the statement has its exact fields, a known subject kind, a non-empty subject and `not_after_s` above `issued_s`. Its approver key is a current authority. Its signature verifies. No clock is read | `attestation.malformed`, `attestation.unknown_authority`, `attestation.bad_signature` |
| 8 | A `device` subject is a current member | `roster.unknown_device` |
| 9 | A phi `epoch` wraps only PHI members, then its author is a PHI member | `roster.epoch_wrong_members`, `roster.not_phi_member` |
| 10 | A phi `wrap` names a PHI member, every grant names a phi epoch, then its author is a PHI member | `roster.epoch_wrong_members`, `roster.unknown_epoch`, `roster.not_phi_member` |

Entries written by a removed device after or concurrently with its removal are void, as in every roster fold. Entries whose ancestors are missing are pending and have no effect.

### 3.2 Merge

- **Live.** An add, or a grant, is live when every remove of its key, or every revoke of its subject, is one of its ancestors. A revoke beats a concurrent grant, and a grant written after the revoke stands.
- **Authorities.** A key is a current authority while it has a live add. Its name and author come from its newest live add by `(clock.ms, clock.n, entry id)`.
- **The chosen grant.** For each subject the newest live grant by the same order is chosen. It counts only while its approver key is a current authority. When it does not, the subject counts nowhere, and an older grant is never used instead.
- **`phi_members`** are the current members whose id is a counted subject whose `not_after_s` is at least the folding device's time. `issued_s` is never read here, so clock skew matters only near `not_after_s`, which the 20-year window puts out of reach.
- **Any subject string** may be counted, but only a member's own id puts it in `phi_members`. A contact, `local:` or URL subject never changes the group. A grant with kind `endpoint` or `node` whose subject is a member's id does admit that member, in every fold, so writers never write one.
- **Epochs.** The current phi epoch is the newest head by `(clock.ms, clock.n, entry id)` among active phi epochs. Its **recipients** are the devices its own wraps name plus those of every active `wrap` grant naming it.

**History.** A device has PHI history when any active `phi_approval` entry, grant or revoke, names its id as subject. Rejected, void and pending entries are no history.

## 4. Upgrade markers

An upgraded kernel writes one `settings.set` event with scope `mesh`, labels `{"confidentiality": "private", "integrity": "owner"}` and body:

```
{"key": "phi.capable/<device id>", "value": {"v": 1, "detector": "astral.phi-detector/v1", "labels": "astral.labels/v2"}}
```

It writes it once from its own feed: at open when it is a member and its log holds none, and right after it joins a mesh. Deployed kernels fold the key as a setting nothing reads. A device on the deployed kernel writes none, so it is never granted. It folds the same group, receives PHI headers only and joins once it upgrades (OD15). It runs no detector and refuses PHI only toward models, so granting it could pass a PHI chat to a legacy remote MCP server.

The fold reads every accepted `settings.set` event whose key starts with `phi.capable/`. Events of other types, such as `mem.remember`, and keys such as `phi.capable` are not read. The first failure wins.

| Step | Check | Code |
|---|---|---|
| 1 | Scope `mesh`, the labels above, a body of exactly `key` and `value`, a key of `phi.capable/` and a device id (`sha256:` and 64 lowercase hex digits), and a value that is an object whose `v` is an integer of at least 1. A value with `v` 1 equals the object above exactly | `phi_marker.malformed` |
| 2 | The named device is a current member | `phi_marker.not_member` |
| 3 | The event's feed is one of that member's feeds | `phi_marker.foreign_feed` |

A value with `v` above 1 counts, so a later kernel stays upgraded. A device is **upgraded** while at least one of its markers counts. A later marker that fails changes nothing. The roster and the log share no order, so a marker is judged against the current roster each time.

## 5. The group

The PHI group is `phi_members`. The upgraded kernel keeps it equal to the owner's upgraded devices (O14) by granting each upgraded member that has no history. A personal node joins by default (OD10). A node in the group refuses draft agents, and the view lists each member's kind so the host can say why on the node's row.

### 5.1 States

Each current member has one state, the first that applies.

| State | When |
|---|---|
| `included` | It is in `phi_members` |
| `excluded` | It has history. The owner excluded it, or its approval stopped counting |
| `not_upgraded` | It has no marker |
| `not_approved` | It is upgraded and waits for a grant |

### 5.2 Pending grants

An upgraded device of any kind but `node` lists as **pending** every current member that is upgraded, has no history and is not in `phi_members`, itself included. A device that was ever revoked is never pending again. Re-including it is an explicit owner grant.

A pending grant needs presence, so it waits for the adoption card or the next presence prompt on an owner device. A pairing add never grants, because the new device's marker cannot exist yet.

### 5.3 Including devices

`include_phi_device` and the completion of pending grants follow one plan for a device L and a list of targets.

| Step | Refusal before any prompt | Code |
|---|---|---|
| 1 | L is a node | `phi_group.node_device` |
| 2 | A target is not a current member | `phi_group.unknown_device` |
| 3 | A target is already in `phi_members` | `phi_group.already_included` |
| 4 | A target is not upgraded | `phi_group.not_upgraded` |

Targets are checked in the order given, each through steps 2 to 4. The plan is `{register_approver, grants, epoch, wraps}`, written in that order:

1. When L is not registered, its `phi_authority` add.
2. One grant per target, sorted by id.
3. When L is in the group after the grants, and the mesh has no current phi epoch or the current one has a recipient outside the new group, a new phi epoch authored by L wrapping every PHI member.
4. When L is in the group after the grants, one `wrap` per target listing every phi epoch L is a recipient of and the target is not. A new or re-included member so reads all PHI that L can read. Pairing refuses limited history, so there is no "current epoch only" form.

**Creation.** When the creating device is not a node, `create_mesh` writes in one presence session the genesis, the approver registration, the self grant and the first phi epoch wrapping the creating device. It grants itself before its marker exists, because the kernel that writes the grant is upgraded by definition. It writes the marker once the mesh is confirmed. A node writes no PHI entries at creation.

**Adoption.** `adopt_phi_group` on an existing mesh completes every pending grant at once, the adopting device included, and so writes the first phi epoch after the grants. The grants are then ancestors of the epoch, which the fold needs.

**Pairing.** The approving device writes the add as today. The new device writes its marker after it joins. Every upgraded owner device then lists it pending.

### 5.4 Excluding devices

`exclude_phi_device` on a device L for a target T:

| Step | Refusal before any prompt | Code |
|---|---|---|
| 1 | L is a node | `phi_group.node_device` |
| 2 | T is not a current member | `phi_group.unknown_device` |
| 3 | T is not in `phi_members` | `phi_group.not_included` |

L writes a revoke of T with presence. When the group left after the revoke is empty, nothing more is written. Otherwise, when L is still in it (`rotate`), L writes a new phi epoch wrapping the remaining members, as `remove_device` does. When L excluded itself (`rekey_needed`), the keeper writes it (section 5.5). Data already on T stays there, and the card says so (OD14). Removing a device from the roster rotates the same way.

### 5.5 The keeper

The **keeper** is the lowest device id among upgraded PHI members. Its duties complete what an interrupted or a deployed writer left.

- `epoch`: the group is not empty and the mesh has no current phi epoch, or the current one has a recipient outside the group. This covers self-exclusion, a removal written by a device outside the group such as one on the deployed kernel, and an adoption interrupted after its grants.
- `wraps`: for every other PHI member, the phi epochs the keeper is a recipient of and the member is not.

A node can be the keeper, since epochs and wraps need no presence. When no upgraded PHI member remains, no device rotates, and the next grant by an owner device makes one.

Each device's duties are `{"grants", "epoch", "wraps"}`. `grants` is its pending list. `epoch` and `wraps` are set on the keeper only. A device with no marker has none.

### 5.6 The local state

A device can seal a `phi` event only while it is in the group and is a recipient of the current phi epoch. Otherwise it refuses the append and the text stays in the composer.

| Local state | Code |
|---|---|
| In the group and a recipient of the current phi epoch | none |
| Not in the group, with history | `phi.device_excluded` |
| Anything else, such as no phi epoch yet or a grant or wrap still on its way | `phi.group_not_ready` |

## 6. Destinations

### 6.1 Kinds, targets and subjects

| Kind | Target | Subject of its approval |
|---|---|---|
| `model` | A canonical endpoint (section 6.2) | The target. A loopback target is on the device and is never approved |
| `service` | A canonical endpoint, or for loopback the bound form `local:<device id>:<canonical loopback endpoint>` | The target |
| `contact` | A contact's mesh id, `sha256:` and 64 lowercase hex digits | `contact:<mesh id>` |
| `device` | A device id | The group itself (section 5) |

`model` and `service` share subjects. One approval covers every `model.provider.*` item, model connection, connection or legacy remote MCP server whose target equals it, because the approval binds the place the data goes and not the provider or connection that named it (OD8). Editing a base URL so that its canonical form changes voids the approval. Every subject uses `subject_kind` `endpoint`, which deployed folds accept as any non-empty string. A subject of another form is listed `unsupported` and approves nothing.

### 6.2 Canonical endpoint

One function turns a provider's `base_url`, a connection's `endpoint` or a legacy server's endpoint into a target, and the gate compares targets exactly. The checks run in this order.

| Step | Check | Code |
|---|---|---|
| 1 | A text of 1 to 2048 characters, each from `%x21-7E`. A list, a number, null, a space between two URLs and non-ASCII text fail here | `phi_destination.malformed` |
| 2 | The text before the first `://`, compared without case, is `https` or `http` | `phi_destination.malformed` |
| 3 | No `*` anywhere, and no `?` in the host part, the text after `://` up to the first `/` or the end | `phi_destination.wildcard` |
| 4 | No `@` (userinfo) and no `#` in the host part | `phi_destination.malformed` |
| 5 | When the host part starts with `[`, a `]` follows, and the text between them is one or more hex digits, `:` and `.` | `phi_destination.wildcard` |
| 6 | That literal is exactly `[::1]`, followed by nothing or a port | `phi_destination.malformed` |
| 7 | No other `[` or `]` in the host part | `phi_destination.wildcard` |
| 8 | The host, lowercased, is not empty | `phi_destination.malformed` |
| 9 | No empty label, so no leading, trailing or doubled dot | `phi_destination.wildcard` |
| 10 | A host of digit labels only is a loopback address `127.a.b.c` with four labels of 0 to 255 and no leading zero | `phi_destination.malformed` |
| 11 | Otherwise at most 253 characters, every label matching `[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?`, a last label that is neither all digits nor `0x` and hex digits, and at least two labels unless the host is `localhost` | `phi_destination.malformed` |
| 12 | A port after the last `:` (after `]` for `[::1]`) is 1 to 65535 in digits with no leading zero | `phi_destination.malformed` |
| 13 | `http` only for a loopback host | `phi_destination.malformed` |
| 14 | The path has no `?` or `#`. After one trailing `/` is removed it is empty or one or more `/segment` parts. A segment is one or more RFC 3986 `pchar`, each escape `%` and two uppercase hex digits that do not encode `A-Z`, `a-z`, `0-9`, `-`, `.`, `_` or `~`, and never `.` or `..` | `phi_destination.malformed` |

The target is the scheme in lowercase, `://`, the lowercase host, the port unless it is the scheme's default (443 for `https`, 80 for `http`) and the path. Its host is **loopback** when it is `localhost`, ends in `.localhost`, is a `127.0.0.0/8` address or is `[::1]`. A target is canonical exactly when the function returns it unchanged.

Every refused form is refused, never rewritten, so each place has one spelling. An internationalized name must already be punycode. An IP literal other than loopback is refused, because a destination off the device is named by a DNS name. A `?` before the first `/` reads as a wildcard, so `https://api.example.com?x=1` is `phi_destination.wildcard`. The Kernel's model egress check accepts some spellings this function refuses, such as `localhost.`, `127.1`, `[0:0:0:0:0:0:0:1]` and public IP literals. Those endpoints still work for content that is not PHI.

### 6.3 Loopback

- **Model.** A loopback model endpoint is on the device, as an on-device model is. It needs no approval and no approval may name it (`phi_destination.on_device`). A PHI member may send PHI to it. The limit is stated on the card: a loopback port that forwards elsewhere is the owner's own setup.
- **Service.** A loopback service is never on the device by its endpoint, since a local server may bridge to the internet. Its target binds the device that runs it, so an approval of a server on one device approves nothing on another. For a local connection that device is the record's `device`. For a legacy remote MCP server it is the device that holds its configuration, because those items are sealed to the device that wrote them. A bare loopback URL as a subject approves nothing and is listed `invalid` with `phi_destination.loopback_unbound`. A bound subject counts only while its device is a current member.
- **Confined service.** A connection whose process the Kernel launches under an exec policy with no hosts is on the device. No kind of `astral.connection/v1` is launched, so this version has no record for it. A local `mcp-http` record always lists its own endpoint in `egress` and so always needs approval.

### 6.4 Sources and proposals

`propose_phi_destination(source)` takes one source and returns `{kind, target, subject, name, needs_approval, facts}`. Errors come before any prompt.

| Source | Fields | Result |
|---|---|---|
| `provider` | `name`, `base_url` | Kind `model` |
| `connection` | `name`, `use` (`model` or `service`), `endpoint` (text or null), `device` (the record's device id or null), `confined` (boolean) | Kind `use`. A confined service is on the device. A null endpoint otherwise is `phi_destination.no_target`. A loopback service with no `device` is `phi_destination.malformed` |
| `legacy_server` | `name`, `endpoint` | Kind `service`, bound to the proposing device when loopback |
| `contact` | `name`, `mesh_id` | Kind `contact` |

Any other source, a source that is not an object or a `name` that is not text is `phi_destination.malformed`. When the destination is on the device, `needs_approval` is false, `subject` is null and `facts` is empty. Otherwise `facts` is `["not_owner_run", "leaves_devices", "unseen_handling"]`: the destination is not run by the owner, anything marked PHI will leave the owner's devices, and Astral cannot see what the destination does with it. The card draws the warning from those facts and names the one destination the owner chose. It offers no wildcard.

### 6.5 Approval and revocation

`approve_phi_destination(proposal)` refuses, in this order: any proposal error, a node device (`phi_group.node_device`), a destination that needs no approval (`phi_destination.on_device`) and a contact (`phi_destination.contact_unavailable`, section 6.6). Then it registers the approver when needed and records a grant with `subject_kind` `endpoint` and the proposal's subject. The Kernel draws the presence prompt from the proposal, never from model output.

`revoke_phi_destination(subject)` records a revoke with presence. It needs no approver key. A revoke beats a concurrent grant, and a grant written after it stands (section 3.2).

### 6.6 Contacts

The contact kind, its subject, its fold case and its query are fixed now. This phase has no contact book, so approval of a contact is refused with `phi_destination.contact_unavailable`. Phase 6 lifts that refusal for a verified contact and owns the rule that each share is confirmed.

`phi_destination_allowed("contact", mesh id)` also needs a current verified contact record for that mesh id. Removing a contact records a revoke of its subject, so a contact added again later with the same mesh id is not a destination until approved again.

## 7. The view

`phi_destinations()` is a pure function of the fold, the accepted marker events, the device's time and the set of current verified contacts.

### 7.1 Group rows

One row per current member: `{device, kind, state, approved_by}`. `state` follows section 5.1. `approved_by` is the device whose approver chose the counting grant, or null when that approver is not a device approver (section 7.2).

### 7.2 Destination rows

One row per subject with history that is not a roster device id: `{subject, form, target, status, code, approved_by}`. `form` is `endpoint`, `local`, `contact`, `invalid` or `unsupported`. The status is the first that applies.

| Status | When |
|---|---|
| `unsupported` | The subject is not a URL, a `local:` form or a `contact:` form |
| `invalid` | The subject has a known form but fails it. `code` is `phi_destination.wildcard`, `phi_destination.malformed`, `phi_destination.not_canonical` (it canonicalizes to another text) or `phi_destination.loopback_unbound` |
| `revoked` | No grant of the subject is live |
| `authority_removed` | The chosen grant's approver key is no longer a current authority |
| `expired` | The chosen grant's `not_after_s` is below the device's time |
| `legacy_authority` | The chosen grant's authority is not a **device approver**: its newest live add is not authored by the device that `institution.name` names |
| `approver_removed` | The approver device is no longer a current member |
| `device_removed` | A `local:` subject whose bound device is no longer a current member |
| `contact_missing` | A contact with no current verified contact record |
| `approved` | None of the above |

`approved_by` is the approver device for the last six statuses when its authority is a device approver, else null. A legacy authority's device approvals still count in the fold, as they do on deployed kernels, while its destination approvals approve nothing. A store written through the FFI with an institution authority keeps its devices and lists its destinations this way.

### 7.3 One answer

`phi_destination_allowed(kind, target)` answers over the same view.

1. The local device is in `phi_members`. Otherwise false, for every kind.
2. `device`: the target is in `phi_members`.
3. `contact`: the target is a mesh id and the row of `contact:<target>` is `approved`.
4. `service` with a `local:` target: the target is canonical in that form and its row is `approved`.
5. `model` or `service` with any other target: the target goes through section 6.2, and a refusal is false. A loopback target is true for `model` and false for `service`. Otherwise the row of the canonical target is `approved`.
6. Any other kind is false.

The Kernel computes the labels v2 input `endpoint_phi_approved` from this answer, or from the on-device case of section 6.3. It is never supplied by a host or a request.

## 8. Chat PHI key

A chat has at most one current writing key per key group. A `phi` event is sealed with the chat's current phi key, and any other event with its current personal key. Only a PHI member can introduce a phi key, which is why `phi.group_not_ready` and `phi.device_excluded` exist.

**The current key.** Among the `key.content` events the device can open whose body `scope` is the chat id and whose header key is `ek:<group>:<epoch>`, the current key of that group is the latest by `(clock.ms, clock.n, feed, seq)`, the order key of the 105 folds. With none, it is the key that `chat.create` names when that key was introduced under the same group. Otherwise the chat has no key of that group.

**Before each write** of an event of group G to a chat, the first matching row decides.

| Step | Condition | Result |
|---|---|---|
| 1 | G is `phi` and the local state (section 5.6) has a code | Refuse with that code |
| 2 | G has no current epoch | Refuse with `phi.group_not_ready` |
| 3 | The chat has no key of G | Rotate, reason `first_write` |
| 4 | The current key was introduced under an epoch other than G's current epoch | Rotate, reason `epoch_changed` |
| 5 | Otherwise | Seal with the current key |

**Rotation.** One primitive, `rotate_chat_key(chat, group, reason)`, serves every trigger. The reasons are a closed set:

- `first_write`: the chat has no key of the group, as at its first PHI write.
- `epoch_changed`: the first write after the group's epoch changed, by an exclusion, a removal or a keeper rotation (owner ruling).
- `revoked`: a share of the chat was revoked. Phase 6 calls it for every group the writer can write. A writer outside the PHI group rotates the personal key only.

It writes a `key.content` event with event scope `mesh`, body `{"id": "<ck:...>", "key": <32 random bytes>, "scope": <chat id>}` and header key `ek:<group>:<current epoch of the group>`. The reason is not written. The new key becomes the chat's current key of the group, and the event that triggered the rotation is sealed with it. A chat-scoped `key.content` under a phi epoch is the only way a phi key enters a chat.

Phi epochs carry no recovery wrap, so a phi key is never readable with the recovery key alone, as 105 `keys.md` section 5 says. Because the next phi write after an exclusion rotates, an excluded device keeps the keys it held and can open nothing sealed after the rotation, even if it receives the ciphertext.

Earlier events keep their keys. Marking a whole chat PHI at its first PHI event would gain nothing, since earlier events stay readable to every member. Deployed kernels keep sealing with the `chat.create` key and read any key they hold, so mixed meshes keep writing.

## 9. Codes

| Code | Where |
|---|---|
| `phi_destination.malformed`, `phi_destination.wildcard` | Canonical endpoint, subjects, proposals, approvals |
| `phi_destination.not_canonical`, `phi_destination.loopback_unbound` | The `invalid` status of a destination row |
| `phi_destination.no_target`, `phi_destination.on_device`, `phi_destination.contact_unavailable` | Proposals and approvals |
| `phi_group.node_device`, `phi_group.unknown_device`, `phi_group.already_included`, `phi_group.not_upgraded`, `phi_group.not_included` | Include and exclude plans, approvals on a node |
| `phi_marker.malformed`, `phi_marker.not_member`, `phi_marker.foreign_feed` | Markers |
| `phi.group_not_ready`, `phi.device_excluded` | The local state, appends and chat keys |
| `roster.*`, `attestation.*` | The fold, section 3.1 |

View statuses: `unsupported`, `invalid`, `revoked`, `authority_removed`, `expired`, `legacy_authority`, `approver_removed`, `device_removed`, `contact_missing`, `approved`. Group states: `included`, `excluded`, `not_upgraded`, `not_approved`.

## 10. Existing data and mixed versions

| Change | Existing data | Handling |
|---|---|---|
| Approver registration, grants, phi epochs and wraps | Roster v2 stores with no PHI entries on Mac 97, iPhone 99 and nodes | Entries are added and nothing is rewritten. A kernel at `c2c1cc4` folds the same group and epochs |
| Stores holding `phi_authority` or `phi_approval` entries written through the FFI | None known, since no client calls that API | Load and fold as today. Their device approvals count. Their destination approvals are `legacy_authority` |
| Meta `phi.approver.v1` | v2 stores | Additive. Absent means not registered. Deleting it and reopening changes no roster, feed or other meta byte. The device registers a new key when it next approves |
| `settings.set` key `phi.capable/<device id>` | Feeds read by deployed kernels | Folded there as a setting nothing reads, with no gap in any feed |
| Loopback model providers on PHI members | Provider items | Still on the device. Remote endpoints stay refused until approved by name |
| Chats keyed under the personal group | Every existing chat | Old events keep their keys. The first `phi` event introduces the chat's phi key |
| Devices on the deployed kernel | Mac 97 and iPhone 99 in a mixed mesh | Never granted, since they write no marker. They fold the same group and receive PHI headers only. After an upgrade the marker arrives and an owner device grants them |

## 11. Vectors

`vectors/phi-destination-v1.json` uses the vector format of the roster v2 contract. `constants` holds the lifetime, the approver prefix, the marker key, value and labels, the facts, the statuses, the group states, the rotation reasons, the folding time and `contact_mesh`, a fixed test mesh id. `test_devices` publishes the roster v2 test devices `alice` (desktop), `bob` (pocket, with a second feed), `carol` (satellite), `node` (node) and `dave` (desktop, which stays on the deployed kernel), plus `bob_restored` (pocket) and the stranger `mallory`, with ids, feeds, presence and approver scalars and approver public keys. They are public test values. No case reads a clock or the network.

Case kinds:

- `phi_endpoint` has `value` and expects `{target, loopback}` or a code.
- `phi_subject` has `subject` and expects `{form, code, target, device}`.
- `phi_contact` has `mesh_id` and expects `{subject}` or a code.
- `phi_propose` and `phi_approve` have a `source`, the device and, for approvals, its kind.
- `phi_statement` has an approver and a subject and expects the signed statement and the authority body as LETS-CJ/1 text.
- `phi_sequence` has `mesh`, `now_s` and steps. Each step adds `entries`, each the LETS-CJ/1 text of one signed roster entry, with `entry_names`, adds marker `events` with `event_names`, and sets `contacts`, `queries` and `plans`. Each expected step holds the roster summary (members, removed, `phi_members`, authorities, epochs, current epochs, recipients, rejected, void and pending), the markers, the group and destination rows, every member's local state and duties, each query's answer and each plan's result, all over every entry and event so far.
- `phi_chat_key` has the chat, the group, the `chat.create` key, the openable keys, the current epochs and the local state.

Groups of cases:

| Group | Cases |
|---|---|
| Endpoints | `endpoint_*`: case, default and other ports, trailing slash, punycode, each wildcard form, userinfo, query, fragment, a list, every loopback form and its near misses, the size limits at and one past their value, and `endpoint_order_*` for adjacent checks |
| Subjects and contacts | `subject_*`, `contact_*` |
| Proposals and approvals | `propose_*`, `approve_*`, `statement_*` |
| Sequences | `seq_create_and_pair`, `seq_adopt_group` (adoption with a node and a deployed device, pairing, exclusion, a device not granted again, self-exclusion, the keeper's rotation and an include with two epochs), `seq_removal_by_deployed_device`, `seq_removal_by_phi_member`, `seq_markers`, `seq_concurrent_revoke_and_grant`, `seq_destinations`, `seq_approver_removed`, `seq_legacy_authority` |
| Fold rules | `fold_*`: each code of section 3.1, the void rule, a pending entry, an expired approval and an endpoint subject that names a device |
| Chat keys | `chat_*`, including two concurrent introductions decided by each part of the order key |

Regenerate with `python specs/106-sign-in-free-clients/contracts/reference/generate_phi_destination_vectors.py` and verify with `--check`, which writes nothing.

## 12. Versioning

A change to a rule, a field or a code raises the format version. This document, the vectors and the reference change together, and the generator's `--check` and the reference tests pass before a change merges.
