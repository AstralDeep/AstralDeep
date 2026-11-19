# Caller turn door `astral.caller-turn/v0` — DRAFT

> [!note] Status
> Draft for maintainer co-design. No node code implements this door. The assembly manifest's
> `doors` section already has the shape to declare it (`principal: caller-ticket`, `turns: true`,
> `consent: own-redacted`, `notification_types: []`), and `astral.caller-events/v1` already
> defines the consent classes it would emit. This draft supplies the missing lifecycle and
> vocabulary so the implementation can be reviewed before it exists.

A caller turn door lets a caller-ticket principal start a model turn on a personal node and follow
it to completion. Today a caller gets tool calls (`/mcp`) and an echo on `/a2a`; the node's model
turns run in the Kernel turn engine for owner devices through mesh requests. This door reuses that
engine path: the node runs the turn through the same bridge machinery (`node_mesh_chat.MeshChatBridge`
and its runner), with the caller as the requesting principal and every existing gate applied.

## 1. Door declaration

The assembly manifest declares the door; a node without the declaration refuses the route.

```toml
[[doors]]
principal = "caller-ticket"
turns = true
consent = "own-redated"
notification_types = []
```

`boot_posture` refuses to start when the declaration names a principal the route does not serve, or
when `turns` is true without the controls the turn path requires (`consent-gate`,
`ticket-verification`, `egress-allowlist`, `phi-gate` as applicable to the node's posture).

## 2. Lifecycle

One POST runs one turn. There is no streaming and no server-side history for callers; the caller
owns its transcript.

```
POST /a2a  (caller auth, method SendMessage, text present, ticket covers a2a.message)
  -> 202 { "turn": "<turn id>", "chat": "<chat id>" }        # admitted, running
  -> 200 { result ... }                                       # completed within the request window
  -> refusal per caller-events/v1                             # before admission
```

- Admission runs the existing gates in order: caller verification (mesh binding, ticket coverage of
  `a2a.message`), the label floor the caller's ticket carries, admission control
  (`MAX_CONCURRENT_TURNS` per principal class, `TURN_SECONDS` deadline), and the PHI posture.
- The turn runs through the same runner the mesh bridge uses, so consent gates, tool tickets and
  audit provenance are the mesh path's, not a parallel implementation.
- The turn's events are recorded on the node's side exactly as mesh-request turns are; the caller's
  transcript stays app-owned. The node keeps only what the audit and consent machinery already keep.

## 3. Events and consent

Per `astral.caller-events/v1`:

- `notification_types` stays empty: the door is request/response. A caller that wants progress
  polls `GET /a2a/{turn}` (same auth, same ticket), which returns one of: `running`,
  `consent.blocked`, `consent.resumed`, `completed`, or the refusal that ended it.
- `consent.blocked` carries only `signal` and `request` (the redacted fields). When a caller's turn
  pauses at the consent gate, the node records the pending consent exactly as it does for owner
  turns; the owner device answers it through the existing `resolve_consent` path. The caller polls.
- `consent.resumed` (or a final result) ends the pause. A consent that is refused ends the turn with
  the gate's code as an ordinary refusal.

## 4. Labels and floors

The turn's label floor is the join of: the caller ticket's labels, the chat floor of the local chat
the turn records into, and the node's posture floor. The bridge's `join_labels` composes them; a
request whose content would raise the floor above what the ticket carries is refused before the
model runs. PHI posture applies exactly as the mesh path applies it.

## 5. Refusals and limits

All refusals use `astral.caller-events/v1` codes plus the existing gate codes. New codes this door
may emit: `caller.turn_limit` (the principal class is at its concurrent-turn cap),
`caller.turn_deadline` (the turn exceeded `TURN_SECONDS`). Limits: at most
`MAX_CONCURRENT_TURNS` in flight per principal class; one text per turn, `1..65536` bytes as in
`requests.md`; a turn id is opaque to the caller.

## 6. Open decisions for the maintainer

1. Route placement: extend `/a2a` method handling or a dedicated `/api/node/caller/turn`. Proposed:
   extend `/a2a` (the ticket already covers `a2a.message`, so coverage semantics stay in one place).
2. Polling endpoint shape and the turn id lifetime (proposed: turn ids live for
   `TURN_SECONDS` + grace, then 410 like expired tickets).
3. Whether caller turns share the mesh bridge's `MAX_CONCURRENT_TURNS` pool or get their own
   (proposed: their own, so a caller cannot starve owner-device turns).
4. Whether `chat` scoping gives each caller one synthetic chat per ticket, or per conversation id
   supplied by the caller (proposed: per ticket, one synthetic chat, so label floors stay stable).
