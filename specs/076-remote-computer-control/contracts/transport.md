# Contract: Host Transport — Frames Between the Orchestrator and a Computer Host

**Feature**: `076-remote-computer-control` | **Spec**: [../spec.md](../spec.md)

Every frame rides the host's **existing authenticated UI WebSocket**. The owner is always the
socket's verified session `sub` (never a frame field), exactly as the 057 tunnel does
(`orchestrator.py:1914-1915`). No new listener, port, or credential exists.

## 1. Announcing a host — additive `register_ui.computer_host`

Sent by a client whose "Allow remote control" setting is on. Absent = not a host.

```json
{"type": "register_ui", "...": "...",
 "computer_host": {
   "host_id": "b9c7…",                  // uuid4 hex, stable per install (%LOCALAPPDATA%)
   "name": "RYZENROLL",                 // human label (hostname by default, user-editable)
   "platform": "windows",               // windows | macos | linux (v1 executes on windows only)
   "client_version": "0.5.0",
   "screens": [{"index": 0, "width": 2560, "height": 1440, "scale": 1.0, "primary": true}],
   "verbs": ["screenshot", "click", "…"], // exact list the host will execute (FR-014)
   "protocol": 1
 }}
```

Server behaviour: with `FF_COMPUTER_USE` off the field is ignored (FR-004). Otherwise
`ComputerHostRegistry.register(owner_sub, ws, descriptor)` keyed `(owner_sub, host_id)`; a
newer socket supersedes an older one for the same key (the old session, if any, ends
`host_superseded`). A `computer_host` presence push goes to the owner's *other* sockets.

## 2. Host → server events — `ui_event action=computer_event`

```json
{"type": "ui_event", "action": "computer_event",
 "payload": {"host_id": "…", "event": "announce|withdraw|paused|resumed|stopped|heartbeat",
             "session_id": "cs_…",            // for session events
             "host": { …same object as above… } // for announce
            }}
```

- `announce` / `withdraw`: the toggle changed while connected (withdraw ends the session
  `consent_revoked`).
- `paused` (`reason: local_input`), `resumed`, `stopped` (`reason: local_stop`): the banner or
  the presence detector acted. The server mirrors the state and pushes `computer_session`.
- `heartbeat` every 30 s while a session is active; the server ends a session after 90 s of
  silence (`host_silent`); the host ends its banner after 90 s without a server frame.

## 3. Server → host requests — push `computer_request`

```json
{"type": "computer_request", "request_id": "creq_…", "session_id": "cs_…",
 "verb": "click", "args": {"x": 640, "y": 402, "button": "left"}, "deadline_ms": 15000}
```

The host MUST refuse (typed error `no_session`) any request whose `session_id` is not its
current active session, and `paused` while paused. Correlation: the orchestrator keeps
`request_id → Future` (same pattern as `pending_requests`), resolved by §4 or by the deadline
(`timeout` result — never an indefinite wait).

## 4. Host → server responses — `ui_event action=computer_response`

```json
{"type": "ui_event", "action": "computer_response",
 "payload": {"request_id": "creq_…", "ok": true, "result": {…verb-specific…}}}
{"type": "ui_event", "action": "computer_response",
 "payload": {"request_id": "creq_…", "ok": false,
             "error": {"code": "no_session|paused|screen_locked|unsupported|out_of_range|window_not_found|timeout|failed",
                       "message": "human text"}}}
```

Payloads are size-capped (`COMPUTER_HOST_MAX_FRAME_BYTES`, default 4 MiB) and rate-capped per
owner by the tunnel window (`_tunnel_ingress_over_cap`); over-cap frames are dropped and
counted (FR-022). Results are **untrusted** (FR-021).

## 5. Server → owner sockets — push `computer_session` and `computer_host`

```json
{"type": "computer_session", "session_id": "cs_…", "host_id": "…", "host_name": "RYZENROLL",
 "state": "active|paused|ended", "reason": null,
 "controller_device_id": "…", "controller_label": "Android phone", "chat_id": "…",
 "started_at": 1756800000, "last_activity_at": 1756800123}
{"type": "computer_host", "host_id": "…", "name": "RYZENROLL", "platform": "windows",
 "state": "online|offline"}
```

`computer_session` reaches **every** socket of the owner (the host uses it to show/hide the
banner; phones refresh the surface). Reasons: `user_stop`, `local_stop`, `consent_revoked`,
`host_offline`, `host_superseded`, `host_silent`, `idle_timeout`, `max_duration`,
`controller_offline`, `flag_off`.

## 6. Manifest deltas (AstralProjection `contracts/ui_protocol.json`)

- `push_types` += `computer_request` (category `host`), `computer_session`, `computer_host`
  (category `presence`) → **72**.
- `accept_actions` += `computer_event`, `computer_response`, `chrome_computer_session_start`,
  `chrome_computer_session_stop`, `chrome_computer_session_pause`,
  `chrome_computer_session_resume`, `chrome_computer_forget` → **109**.
- `additive_fields` += `register_ui.computer_host` → **12**.
- `component_types` unchanged (**35**).

Per-client classification: Windows `HANDLED` for all three pushes; Android handles
`computer_session`/`computer_host` (refresh an open `my_computers` surface) and ignores
`computer_request`; Apple `.ignored` for all three in this feature (documented divergence: the
Apple client is authored on a Mac in the follow-up — it still renders the surface, the live
image and the approval card through the generic SDUI path, so the *capability* is present).

## 7. What never crosses this transport

Delegation tokens, per-user secrets, LLM credentials, other users' data. The host is an
untrusted executor of the owner's own intent: the orchestrator authorizes every verb through
the full gate stack before a `computer_request` is built (FR-020), mirroring
`_untrusted_tunnel_agent`.
