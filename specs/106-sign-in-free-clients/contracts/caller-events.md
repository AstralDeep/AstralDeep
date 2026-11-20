# Caller events and consent visibility

This contract fixes what a principal can observe about a personal node. It commits the refusal codes a caller receives, the request lifecycle of the `/mcp` and `/a2a` doors, and who may see a pending consent. Format version `astral.caller-events/v2`.

Reference code: `reference/caller_events.py`. Generator: `reference/generate_caller_event_vectors.py`. Vectors: `vectors/caller-events-v2.json`. The ticket and proof formats stay in [caller-ticket.md](caller-ticket.md).

## 1. Scope rule

A caller observes its own requests and its own refusals. It observes nothing else.

A caller ticket is not an owner presence. It can read the node descriptor and the tool list its ticket covers. It cannot read a turn it did not cause, another caller, a contact, mesh state or a pending consent.

## 2. Principal classes

| Class | Who | Door |
|---|---|---|
| `owner-device` | A device on the mesh roster | The mesh |
| `caller-ticket` | A script or outside agent holding an owner-minted ticket | `/mcp`, `/a2a` |
| `outside-service` | A service the node calls through a connection | None |

A door serves one class. The node's activation manifest names each door's class, and [assembly.md](assembly.md) refuses a manifest that breaks section 5.

## 3. Refusal codes

Every refusal carries a stable `code` and a `reason`. The code is the interface. The reason is text for a person, it may change, and a client never matches on it.

| Class | Status | Codes |
|---|---|---|
| Unauthorized | 401 | `node.bearer_refused`, `caller.malformed`, `caller.proof_malformed`, `caller.bad_signature`, `caller.proof_bad_signature` |
| Forbidden | 403 | `caller.mesh_mismatch`, `caller.wrong_node`, `caller.device_removed`, `caller.wrong_minter`, `caller.stopped`, `caller.expired`, `caller.not_yet_valid`, `caller.revoked`, `caller.proof_mismatch`, `caller.proof_stale`, `caller.replayed`, `caller.tool_not_covered`, `caller.tool_changed`, `caller.tool_unavailable` |
| Unavailable | 503 | `caller.replay_full`, `mesh.not_enrolled`, `node.development_mesh`, `node.mesh_unavailable`, `node.owner_unbound` |
| Gate | 503 | Any code that starts with `gate.` |
| Opaque | 500 | Any other code |

Rules for the node:

- Adding a code is compatible. Removing or renaming one raises the format version.
- A `gate.` code tells the caller only that a safety gate refused. The reason never names the gate's input.
- No refusal names a pending consent, an approver or another principal.

Rules for a client:

- Branch on the code and show the reason.
- Treat a `gate.` code as "the owner must review the node's approvals".
- Treat an unknown code as an opaque failure. Fail closed and show the reason.

## 4. Request lifecycle

The `/mcp` door answers a caller with exactly one result or one error per request.

There is no progress, no partial result and no message the node starts. `subscriptions/listen` answers `"notificationTypes": []`. A client must not invent a notification, a plan event or a turn event.

A notification type added later raises the format version and appears in `notificationTypes`. Until then the tool list is the only information a caller has before it calls.

The `/a2a` door answers a caller's message with one A2A task that ends completed or failed. A caller ticket lists tools through it and cannot call one. The task's status updates are the A2A protocol's own, they concern only that task, and the node adds no notification type to them.

## 5. Consent visibility

The Kernel engine pauses before the effect. The owner device reviews the exact tool and arguments. Its presence signature binds the action digest, and the executor verifies the approval in the ticket.

This section fixes who sees it.

| Class | Pending consents | Consent signals |
|---|---|---|
| `owner-device` | Full details for requests this device can decide, through the Kernel's `pending_consents()` | Local turn progress or a change to the signed mesh log |
| `caller-ticket` on a door that starts no turn | None | None |
| `caller-ticket` on a door that starts turns | None | `consent.blocked` and `consent.resumed`, for its own turn only |
| `outside-service` | None | None |

For a local turn, the initiating owner device decides its own pending actions.
For a text request sent to a node, only the active owner device that sent the
request may answer the node's challenge. Other roster members do not acquire
approval authority by receiving chat history. The requester refreshes its
pending list when chat records sync, even if it has no local turn running.
The node verifies that the request, turn, proposed call, exact action and
executing node still match. A remote approval binds these fields and the
challenge in the presence-signed v2 consent proof. Existing local approvals
use v1. An expired or interrupted turn cannot resume through a late decision.
When PHI is involved both devices must remain eligible for that content.
The node tracks the pending action locally and can stop the turn. Approval requires the requesting owner's presence signature. An independent executor verifies the presented approval without receiving a review prompt. The recipient check in the reference assumes the Kernel has already checked current roster membership and content visibility.

### Relayed action proof

The ticket carries a consent with exactly `v` (2), `kind` (`action`), `digest`, `device`, `clock`, `relay` and `sig`. The presence key on the requesting owner device signs the canonical fields excluding `sig` in the existing `astral.consent/v1` signing context. The top-level `device` identifies that signer. The action names the executing node.

| Field in `relay` | Binding |
|---|---|
| `challenge` | ID of the node’s signed consent challenge |
| `request` | ID of the text request from the reviewing device |
| `turn` | ID of the turn answering that request |
| `call` | Identifier of the proposed tool call within the turn |
| `node` | Device ID of the node handling the turn |

The executor checks the signed action and its matching chat records before claiming the effect. A verifier missing otherwise-valid signed evidence may return `ticket.consent_sync_pending` and check again after sync. A refusal, expiry or missing proof at the end of that wait leaves the effect unclaimed. The local v1 proof follows the original ticket contract.

The visibility class of a door is `full`, `own-redacted` or `none`, in the order of the rows above. Rows two and four are both `none`.

A caller never sees another principal's consent. No door, flag or manifest entry changes that.

The redacted pair carries two fields and no others: `signal`, and `request`, the caller's own request id. It never carries the action digest, the tool name, the tool arguments, the approver or the outcome. A caller learns that its turn waits for the owner and later that it stopped waiting.

No door starts a turn for a caller today. The pair is reserved, it has no producer, and `notificationTypes` stays empty until a door that starts turns is specified.

## 6. Turn events

None are committed. A caller door that starts turns needs its own contract revision, which adds the event names here and the notification types in section 4.

Every client door runs turns on the Kernel engine, so a turn emits the same events for every client. That is the condition for committing names later.

The node has no other turn engine. A scheduled chat turn is refused with `turn_engine_retired` until a contract gives jobs a door on the Kernel engine.

## 7. Codes of this contract

The reference answers a visibility question with `true` or `false`. It refuses bad input with one code.

| Code | Meaning |
|---|---|
| `events.malformed` | An unknown principal class, an unknown signal, a `turns` value that is not a boolean, or an event to redact that is not one of the caller's two signals with a request id |

## 8. Versioning

Version 2 restricts a delegated node consent to the requesting owner device and adds a log-change signal for that device. The caller door behavior remains compatible with version 1. Sections 3 and 4 describe shipped behavior. Section 5 is in force for the owner and caller rows that have producers, and it reserves the redacted pair.

A change to a code's class, to the lifecycle or to a visibility row raises the format version. The document, the vectors and the reference change together.
