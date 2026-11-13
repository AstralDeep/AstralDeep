# Request contract, `astral.requests/v1`

A device that cannot do something itself writes a request addressed to one member. The member that receives it claims it, runs it through its own gates, records the turn it ran, or refuses it. This document fixes the four event bodies, the tool call a request may carry with its ticket, and the fold that gives every request one state on every peer.

Reference code: `reference/astral_mesh_ref/requests.py`. Vectors: `requests.json`. The operations are `request_check`, `request_fold` and `request_mint`.

## 1. Events

All four types have the family `request` and the scope of a chat. A request type with scope `mesh` is `event.malformed`. A request is labeled by the writer. The labels are at least the join of the chat's events it was written from, so a request that carries private or PHI content is visible only where that content is.

| Type | Body (exactly these fields) | Written by |
|---|---|---|
| `request.send` | `to`, `text`, `continues`, `call` | The requester |
| `request.claim` | `request` | The target |
| `request.refuse` | `request`, `code` | The target |
| `request.cancel` | `request` | The requester |

- `to` is the device id of the member that should run the request. It is a member of the roster when written. A request to the writer itself is refused by the kernel and ignored by no rule here.
- `text` is 1 to 65536 bytes of UTF-8. It is what the target shows or feeds to its own model.
- `continues` is a turn id of this chat or null. It is the turn the answer continues.
- `call` is null or a tool call (section 2).
- `request` is the event id of a `request.send` in the same chat.
- `code` is a dotted code of 1 to 80 characters: lowercase letters, digits and `_`, starting with a letter, with at least one `.`. It is the reason shown to the requester, such as a gate or ticket error code.

Any other shape is `request.malformed`.

## 2. A request that carries a tool call

When a device's turn proposes a tool that only another member provides, the device runs its own gates, mints a ticket (`ticket.md`) and sends the call with the request. The target verifies the ticket and then runs its own gates. It never trusts the device's gates.

`call` has exactly these fields.

| Field | Value |
|---|---|
| `call_id` | The id of the call in the requester's turn, 1 to 200 characters of `A-Z a-z 0-9 _ . : -` |
| `tool` | The tool name, 1 to 200 characters without control characters |
| `arguments` | The canonical JSON text of the arguments object, at most 65536 bytes |
| `action` | The action object of `ticket.md` section 2 |
| `ticket` | The ticket object of `ticket.md` section 1 |

`arguments` is the exact text whose hash is the action's `args_digest`, so the target hashes what it received and compares. A text that is not an object, not canonical, or too large is `request.malformed`. An action whose `tool` differs from `tool`, or whose `args_digest` differs from the hash of `arguments`, is `request.call_mismatch`. The shape of the ticket is checked here and its contents are checked by the target with the rules of `ticket.md` section 4.

## 3. The fold

The fold takes the events of one chat and the map from feed to device. It is a pure function of the set of events.

1. A request type event whose body is malformed is ignored. A `request.send` from a feed that no device owns is ignored.
2. A claim or a refusal counts only if its author is the request's `to`. A cancel counts only if its author is the device that wrote the request. Any other claim, refusal or cancel, and any naming an unknown request, is ignored.
3. A claim is **void** if some counted cancel of the same request is one of its ancestors (`event.md` section 10.1). A target that wrote the claim after seeing the cancel did not run the request. A claim written concurrently with a cancel, or before it, stands. This is what prevents two executions: once a target has claimed, the requester cannot take the request back.
4. The **answering turn** is the earliest `chat.turn_start` event, in the order `(clock, feed, seq)`, whose body has `request` equal to the request id and whose author is `to`. It counts only if the request has a claim that is not void. A turn the target records for a request has this field (the turn start of `event.md` section 9 gains the optional field `request`).
5. The state is the first that applies.

| State | When |
|---|---|
| `answered` | There is an answering turn |
| `refused` | A counted refusal exists. The code is the earliest refusal's code |
| `claimed` | A claim that is not void exists. The claim is the earliest such claim |
| `cancelled` | A counted cancel exists |
| `pending` | Otherwise |

Other members only list the request. Every device shows a pending request. Only the target acts on it.

The output is `{"requests": [...], "ignored": [...]}`. Each request is `{id, author, to, state, claim, turn, code}` in the order of its `request.send` event. `author` is the writer's device. `claim`, `turn` and `code` are null when they do not apply, and `code` is set only in the state `refused`. `ignored` is the sorted ids of the request type events that were ignored or void.

## 4. What the target does

The target verifies the ticket of a call (`ticket.md` section 4) with its own roster, its own stop counter and the effect kind it derives itself. It runs its own gate stack. It records the turn it ran as ordinary chat events under its own feed with the labels it computed, which may not be lower than the inputs it could see. The turn start carries `request`. The requester reads the result from the `chat.tool_result` event of the turn whose `call` is the request's `call_id`.

## 5. A member that runs a request outside the engine

The always-on node runs the turn of a request in its own loop and records the turn afterwards with the ordinary events. Its tools run in a sandboxed executor that refuses a call without a ticket. A device mints its tickets in its turn engine, so the node needs one place that applies the same gates and mints for it. That place is the **mint**.

The mint takes the id of a request and one call with these fields.

| Field | Value |
|---|---|
| `call_id` | The id of the call in the node's turn, as in section 2 |
| `tool` | The tool name |
| `arguments` | The canonical JSON text of the arguments object, at most 65536 bytes |
| `tool_def` | The definition hash the node pins for the tool |
| `effect` | `mesh_read`, `mesh_write`, `outward_read` or `outside_effect` |
| `running` | Optional. The labels the node's turn has reached so far. They never lower the labels of the request |
| `influence` | Optional. Ids of chat events that influenced the call. The id of the request is always added |
| `untrusted_arguments` | Optional. True when an argument value came from untrusted content |
| `consent` | Optional. A consent object of `ticket.md` section 5 |

The action it builds is the one of `ticket.md` section 2. Its `device` is the node and its `agent` is `kernel`. Its `turn` is the id of the `request.send` event, because the turn start event does not exist until the node records the turn.

The mint checks these in order and the first failure is the error. Step 1 is gate step 1 of `labels.md` section 5. The pure checks of steps 2 to 6 are the vectors `request_mint`. Step 7 reads the chat and step 8 is the gate stack.

1. The mesh is not halted and this device is on the roster (`gate.mesh_stopped`, `gate.device_not_on_roster`).
2. The call is well formed. The ids, the canonical arguments and the hash have the shapes above (`mint.malformed`). The effect is one of the four (`labels.invalid`).
3. The request is addressed to this device (`request.not_target`), is claimed (`request.not_claimed` while pending, `request.answered`, `request.refused` or `request.cancelled` otherwise) and carries no call (`mint.call_request`). A request that carries a call already has its ticket, and the node answers it as a target (section 6).
4. This device has not minted 64 tickets for the request (`mint.limit`). The count lives in the process, and the bound of 64 steps in a recorded turn still applies after a restart.
5. The tool is in this device's own latest catalog (`mint.unknown_tool`), the hash equals the catalog's (`gate.tool_def_changed`) and the effect equals the catalog's (`mint.effect_mismatch`). The catalog is the definition the owner granted, and the executor derives the effect from it.
6. The running label is the join of the request's labels and `running`. It is never `secret` (`mint.secret_label`). It is `phi` only when this device is in the PHI group (`mint.phi_not_approved`).
7. Every influence id is an event of the chat (`mint.unknown_influence`).
8. The gate stack of `labels.md` section 5 runs for the action. Permission needs a consented grant whose tool is exactly `<name>@<first 8 hex digits of tool_def>` and whose scope is the effect or `*`. A grant for the bare name or for `*` does not cover the tool, so a changed definition needs a new grant (`gate.permission_denied`). A hard flag, the policy rules and the flow rules apply as for any effect. The allowance is charged last. A refusal there is `allowance.share_spent` or the code the allowance names.

When every step passes the mint charges the allowance for `mesh_write` and `outside_effect` and holds a real LETS receipt. It writes the receipt as an `auth.receipt` event in the chat, so the node can name it in the recorded step. A read carries no receipt and no charge, as in `ticket.md` section 6. Asking again for the same action finds the receipt in the chat and does not charge a second time. The ticket is the one of `ticket.md` section 1. Its stop counter is the roster's at that moment and its lifetime is the node's ticket lifetime.

**Consent.** The gate can ask for consent because of a policy rule, an untrusted influence, the rule of two, an untrusted argument or the solo allowance. A node has no presence key. The mint therefore never signs a consent and never returns a ticket for such an action unless `consent` is given. Without it the mint refuses with `consent.required` and the message names the reasons and the action digest. The preview of the same call returns the action, its digest, the outcome and the reasons without charging anything. The node's host shows the action to the owner. A device of the owner draws the prompt from the action and signs a consent with its presence key (`ticket.md` section 5). The node gives that consent to the mint, and the gate accepts it only when it verifies for the exact action digest and is fresh. A consent for other arguments, another tool or another request fails, because the digest differs.

The node must not mint for a turn it does not run, for another member's request, for a request that is not claimed or has ended, or for a call outside its own catalog. Any ticket it mints verifies with the verification of `ticket.md` section 4 on every member, and the tampered, stopped, revoked and spent cases refuse there with the codes of that section.

## 6. A target that holds the tool

A device that holds a tool answers a request that carries a call for it. It does these steps and the first failure refuses the request with that code, so the requester reads it as in section 4.

1. The request is addressed to this device and is pending. A claimed request that was not answered may have run and is never run again.
2. The tool is in the device's own tool runner, the runner's definition hash equals the action's `tool_def` (`gate.tool_def_changed`), and the hash of `arguments` equals the action's `args_digest` (`request.call_mismatch`).
3. The ticket verifies with the effect kind the device derived itself (`ticket.md` section 4).
4. The device's own gates run for the action: gate steps 1 to 6 with the exact grant of section 5 step 8. The allowance is not charged again because the verified receipt carries it. A consent in the ticket counts as consent.
5. The device claims the request, runs the tool with the ticket, and records one turn with the ordinary events. The turn start carries `request`. The result event carries the labels of the request joined with the labels of the output. An output whose label is `secret` is never run, because it cannot be recorded.

## 7. Errors

| Code | Raised when |
|---|---|
| `request.malformed` | A body has the wrong shape, size, text or reference |
| `request.call_mismatch` | The action does not match the call's tool or arguments |
| `event.unknown_type` | The type is not one of the four |
| `mint.malformed`, `mint.call_request`, `mint.limit`, `mint.unknown_tool`, `mint.effect_mismatch`, `mint.secret_label`, `mint.phi_not_approved`, `mint.unknown_influence` | A mint check of section 5 failed |
| `consent.required` | The gate asks for consent and the call carries none |
