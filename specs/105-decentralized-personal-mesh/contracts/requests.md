# Request contract, `astral.requests/v1`

A device that cannot do something itself writes a request addressed to one member. The member that receives it claims it, runs it through its own gates, records the turn it ran, or refuses it. This document fixes the four event bodies, the tool call a request may carry with its ticket, and the fold that gives every request one state on every peer.

Reference code: `reference/astral_mesh_ref/requests.py`. Vectors: `requests.json`.

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

## 5. Errors

| Code | Raised when |
|---|---|
| `request.malformed` | A body has the wrong shape, size, text or reference |
| `request.call_mismatch` | The action does not match the call's tool or arguments |
| `event.unknown_type` | The type is not one of the four |
