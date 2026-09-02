# Remote computer control (feature 076)

Drive your own Windows PC — the one running the AstralDeep desktop client — from your
phone or any other signed-in AstralDeep client, the way Claude and ChatGPT computer use
works: you type a task, the assistant looks at the screen, clicks and types, and you watch
the screen update. Commands and file changes always ask you to approve on the device you
are holding, and whoever is sitting at the PC can pause or stop at any time.

Spec: [`specs/076-remote-computer-control/`](../specs/076-remote-computer-control/spec.md)
(contracts: [transport](../specs/076-remote-computer-control/contracts/transport.md),
[verbs](../specs/076-remote-computer-control/contracts/verbs.md)).

## Enabling it (operator)

| Setting | Where | Default | Notes |
|---|---|---|---|
| `FF_COMPUTER_USE` | orchestrator `.env` | **off** | Read once at import — recreate the container to toggle. Off ⇒ no agent, no menu item, no surface, host announcements ignored (byte-identical). |
| `COMPUTER_USE_IDLE_TIMEOUT_S` | orchestrator | 1200 | A session ends after this much inactivity. |
| `COMPUTER_USE_MAX_DURATION_S` | orchestrator | 7200 | Hard cap per session. |
| `COMPUTER_USE_HEARTBEAT_SILENCE_S` | orchestrator | 90 | A host that stops heartbeating ends the session (`host_silent`). |
| `COMPUTER_USE_ACK_TIMEOUT_S` | orchestrator | 5 | How long `start_session` waits for the desktop to acknowledge. |
| `COMPUTER_USE_MAX_IMAGES` | orchestrator | 3 | Screenshots kept as real images in the model's context; older ones become a placeholder. |
| `COMPUTER_HOST_MAX_FRAME_BYTES` | orchestrator | 4 MiB | Host replies above this are dropped. |
| `BYO_TUNNEL_MAX_FRAMES_PER_S` | orchestrator | 50 | Per-owner ingress cap, shared with the 058 tunnel. |

The agent `computer-use-1` is safe-seeded like the other bundled agents when the flag is
on, so the observe verbs work out of the box; `write_file`, `delete_path`, `confirm_action`
and a shell `open_app` are gated on **every** reach by the durable proposal card
(`orchestrator/remote_confirmation.py`) regardless of the safe baseline. Tune the budget of
a look-then-act turn with `COMPUTER_USE_MAX_TURNS` (default 24).

## Using it

1. **On the PC** (Windows desktop client ≥ 0.5.0): Settings → **My computers** →
   *This computer* → **Allow remote control**. The client announces itself as a computer
   host on its existing authenticated connection; nothing listens on a port.
2. **On the phone / another client**: Settings → **My computers** → the PC shows as
   online → **Control this computer**. (Or just ask in chat: "take a screenshot of my PC".)
3. **Ask in chat**: "open Notepad and type a note", "what's on my screen?", "switch to
   Excel and read the totals". The assistant calls `screenshot`, decides, acts, and
   screenshots again; the latest screenshot updates in place on the canvas.
4. **Approvals**: opening a terminal, writing/deleting a file, or anything the assistant
   judges consequential (buying, sending, signing in, running a command) shows a card with
   **Approve** / **Decline** on your device. Nothing runs until you approve; a card expires
   after 15 minutes and is single-use; after Approve the task resumes by itself. There is no
   "run a command" tool on purpose (Constitution VII) — the assistant types into a terminal
   the way you would, and that typing is what your approval unlocks (for 3 minutes). A
   terminal is any console-hosted window (cmd, PowerShell, Windows Terminal, a Python/Node
   REPL …) or a known terminal emulator. If you approve something while someone is using
   the PC, the approval is kept for 3 minutes so the assistant can retry the same step once
   the PC is free, without asking again.
5. **At the PC**: a banner "*Your Android phone* is controlling this computer — Pause · Stop"
   stays on screen. Touching the mouse or keyboard pauses the session (the assistant waits
   and resumes when you stop; its own clicks and keystrokes never count as you); Stop ends it.
   Switching **Allow remote control** off ends any session and removes the PC from every
   other client within seconds.

## What the model can do (closed verb set)

| Tier | Verbs | Gate |
|---|---|---|
| session | `list_computers`, `start_session`, `end_session`, `resume_session`, `confirm_action` | live human; `confirm_action` ⇒ card |
| observe | `screenshot`, `list_windows`, `get_clipboard`, `read_file`, `list_dir`, `wait` | active session |
| input | `click`, `double_click`, `right_click`, `move`, `drag`, `scroll`, `type_text`, `press_keys`, `focus_window`, `open_app`, `set_clipboard` | active session |
| consequential | `write_file`, `delete_path`, `open_app` of a shell, keystrokes into a terminal | active session **+ approval card** |

Machine-class turns (scheduled jobs, MCP) may run only `list_computers`.

## Vision and text-only models

Screenshots reach the model as image parts (OpenAI-compatible `image_url` data URIs) in
a user message that follows the tool messages. If the provider rejects images, the call is
retried text-only once and the (endpoint, model) is remembered as non-vision for the
process; the assistant then works from `list_windows`, `type_text`, `press_keys` and
`read_file`. Screenshots are still shown to *you* on the phone. Proven live 2026-09-02 with
GLM-5.2 on LLM Factory from both the web client and the Android emulator (it described the
desktop accurately and read a `Get-Date` result off a PowerShell screenshot).

## Security posture

- Owner derived from the socket's verified session, never from a frame; registry keyed
  `(owner, host_id)`; a response is accepted only from the socket that holds the host.
- Every verb goes through the full dispatch gate stack; no delegation token or per-user
  secret ever reaches the host; host output is an untrusted taint source and screenshots
  are captioned as untrusted content.
- Sessions are in-process; the audit trail (`agent_lifecycle` · `computer_session.*` and
  `remote_op.*`, plus per-verb `agent_tool_call` rows) is the durable record.
- Limitations: injected input cannot reach UAC-elevated windows; the client must run in the
  interactive session of the signed-in user; multi-monitor coordinates assume one DPI scale
  per screen; the banner is visible in screenshots.

## Verifying locally

```bash
docker compose up -d                       # with FF_COMPUTER_USE=1 in .env
cd backend && python -m pytest -q tests/test_computer_use_076.py
# Windows client (offscreen): QT_QPA_PLATFORM=offscreen PYTHONPATH=. python -m pytest -q tests/test_remote_control_076.py
```
