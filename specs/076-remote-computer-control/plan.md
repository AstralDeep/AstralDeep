# Implementation Plan: Remote Computer Control (076)

**Branch**: `076-remote-computer-control` in AstralDeep and AstralProjection | **Spec**: [spec.md](spec.md)

## Technical Context

- **Language/Version**: Python 3.11 (orchestrator, production image); Python 3.10+/PySide6 6.11 (Windows client); Kotlin 2.0 + Compose (Android); ES5 JS (web render layer). Swift touched only for the protocol-manifest tables.
- **Primary Dependencies**: existing only — FastAPI, `websockets`, the OpenAI-compatible client via `llm_config/client_factory.py`, `astralprims` (`image`), the 057 tunnel plumbing (`shared/local_transport.py`, `_tunnel_ingress_over_cap`), the 063 proposal repository (`astralplane.repositories.remote_proposals`) and `remote_confirmation.py`, `projection_surfaces`, `chrome_events`. Windows: Qt `QScreen`/`QImage`/`QBuffer` + stdlib `ctypes`/`subprocess`. **Zero new third-party runtime dependencies** (Constitution V, D5).
- **Storage**: none new (D7). Sessions and hosts are in-process; proposals reuse `remote_operation_proposals`; audit rows carry the durable record. `SCHEMA_REVISION` untouched.
- **Testing**: `pytest` (backend, no Postgres — in-memory Plane proposal repository + a `FakeComputerHost` test seam), Windows `pytest` with `QT_QPA_PLATFORM=offscreen`, Android unit tests (`ProtocolManifestTest`, renderer parity), Deep `test_ui_protocol_manifest.py`.
- **Target Platform**: Windows 10/11 host in the interactive user session; controllers = any client (Android first).
- **Constraints**: flag-off byte-identity (`FF_COMPUTER_USE`); every verb through `_run_gate_stack`; no per-client settings code (Constitution II/XII); manifest drift guards green in every client stack.

## Constitution Check

| Principle | How this feature complies |
|---|---|
| II SDUI | Surface = astralprims components built server-side (`astralprojection.chrome.computers` pure builder + `projection_surfaces/my_computers.py` host adapter); live view = `image` primitive; PC-side consent lives in the client's *own* settings (a client-local capability like the voice device picker), not a settings surface. |
| V Dependencies | none added; PR states it. |
| VII Security | full gate stack, 063 confirmation, taint registration, audit on every session transition and verb, no secrets to the host, owner derived from session. |
| IX Migrations | none. |
| XII Cross-client | manifest + every drift table updated; Apple divergence documented in [contracts/transport.md](contracts/transport.md) §6. |

## Project Structure (files this feature adds or edits)

### AstralDeep

```
backend/shared/feature_flags.py                 + "computer_use": FF_COMPUTER_USE (default False)
backend/shared/protocol.py                      + RegisterUI.computer_host (additive; ComputerHostDescriptor validator)
backend/orchestrator/computer_hosts.py          NEW  registry (owner_sub, host_id) → HostRecord; presence pushes; request/response futures; size/rate caps; honest-offline
backend/orchestrator/computer_sessions.py       NEW  ComputerSession lifecycle (start/pause/resume/end, idle + max timers, heartbeat watchdog), audit, computer_session pushes
backend/orchestrator/computer_use_policy.py     NEW  DESTRUCTIVE_CLASSIFICATION, UNATTENDED_ALLOWED, tiers, summaries for the approval card
backend/orchestrator/remote_confirmation.py     EDIT generalize the single MUTATING_AGENT_ID into a per-agent policy table (063 behaviour unchanged, pinned by its tests)
backend/orchestrator/orchestrator.py            EDIT register_ui → registry; ui_event computer_event/computer_response; disconnect teardown; gate hook for computer-use-1; `_images` → multimodal message assembly + text-only fallback; local_agents injection
backend/orchestrator/local_agents.py            EDIT flag-gated `computer_use` dir; FIRST_PARTY_PUBLIC_AGENT_IDS; inject `computer_use_deps`
backend/agents/computer_use/{__init__,computer_use_agent,mcp_server,mcp_tools}.py  NEW agent `computer-use-1`
backend/orchestrator/projection_surfaces/my_computers.py  NEW surface adapter (TITLE, render, components, HANDLERS chrome_computer_*)
backend/orchestrator/projection_surfaces/__init__.py      EDIT register "my_computers"
backend/orchestrator/chrome_events.py / menu wiring        EDIT pass `computer_enabled` to the menu model
backend/orchestrator/taint.py                   EDIT add computer-use-1 to untrusted sources
backend/tests/test_computer_use_*_076.py        NEW  verbs contract, sessions, gate/unattended, flag-off byte-identity, transport correlation, surface render/components, multimodal assembly + fallback, manifest sweep
config/astral-composition.json                  EDIT repin Projection + ui_protocol sha256 (after the Projection commit)
docs/remote-computer-control.md                 NEW  operator/user doc
CLAUDE.md                                       EDIT Recent Changes entry
```

### AstralProjection

```
contracts/ui_protocol.json                      EDIT +3 push types, +7 accept actions, +1 additive field
src/astralprojection/chrome/computers.py        NEW  pure builder for the "My computers" surface (components); web HTML via the shared SDUI-to-HTML path
backend/webrender/chrome/menu_model.py          EDIT `_MY_COMPUTERS_ITEM`, `computer_enabled` kwarg
tests/test_protocol.py, tests/chrome/…          EDIT manifest expectations + builder tests
windows-client/astral_client/protocol_manifest.py   EDIT classify computer_request/computer_session/computer_host
windows-client/astral_client/protocol.py            EDIT register_ui gains computer_host when consent is on; send_computer_response/send_computer_event helpers
windows-client/astral_client/remote_control.py      NEW  consent setting (QSettings), host descriptor, session state machine, banner (always-on-top frameless QWidget), presence detector (GetLastInputInfo), heartbeat
windows-client/astral_client/app.py                 EDIT settings entry "Remote control", frame dispatch for computer_request/computer_session/computer_host
windows-client/win_agent/computer_use.py            NEW  executor: QScreen capture → JPEG data, ctypes SendInput/SetCursorPos, key table, EnumWindows, clipboard, files, run_command
windows-client/tests/test_remote_control_076.py, test_computer_use_executor_076.py  NEW
android-client/core/…/protocol/ProtocolManifest.kt  EDIT pushes/actions
android-client/app/…/render/renderers/Media.kt      EDIT data: URI decode for `image` (Base64 → Bitmap)
android-client/app/…/AppViewModel.kt                EDIT refresh open my_computers surface on computer_session/computer_host
android-client tests                                EDIT manifest test expectations
apple-clients/AstralCore/…/Dispositions.swift, ManifestDriftTests.swift  EDIT counts + `.ignored` rows (not compiled here — Mac follow-up)
```

## Phases

0. **Spec + contracts** (this document) → commit.
1. **Protocol** — manifest deltas in Projection + Deep `protocol.py` + all drift tables. Tests green in the Python stacks; Kotlin/Swift edited by inspection.
2. **Backend core** — registry, sessions, policy, agent, gate generalization, multimodal assembly, surface, menu, flag-off test. Unit tests without Postgres.
3. **Windows host** — executor + consent + banner + presence + wiring; offscreen tests for the state machine, key table, coordinate mapping, JPEG encoding; live smoke on ryzenroll.
4. **Android** — data: image support + surface refresh + manifest; emulator smoke.
5. **Live rig** — Docker rebuild with the branch, Windows client from source, emulator; run the demo task; capture evidence; fix; repin composition; PRs.
6. **Docs + wiki** — `docs/remote-computer-control.md`, CLAUDE.md entry, kos-wiki page `astral-remote-computer-control` + index + log.

## Research decisions (condensed)

- **R1 Transport shape**: a *device capability* on the UI socket (registry keyed by host) rather than registering the PC as a tunnel agent. Reason: per-machine addressing, one stable agent id for prompts/permissions/safe-seeding, no `user_agent` row needed, and the 063 surface/gate patterns apply directly.
- **R2 Vision**: OpenAI-style `image_url` data URIs in a `user` message following the tool message (tool messages cannot carry images in the OpenAI schema); keep the last 3 screenshots as images, older become text placeholders; one text-only retry on provider rejection, then the session is marked `images_unsupported`.
- **R3 Capture**: `QGuiApplication.screens()[i].grabWindow(0)` on the GUI thread (marshalled with a queued signal), `QImage.scaledToWidth`, JPEG q=70 via `QBuffer`; typical 1280-px frame 80–200 KiB. Qt's bundled `qjpeg` plugin is already packaged by the PySide6 PyInstaller hook.
- **R4 Input**: `user32.SetCursorPos` + `SendInput` (MOUSEEVENTF_*), keyboard via `SendInput` `KEYEVENTF_UNICODE` for text and a fixed VK table for chords; DPI: the client is per-monitor-DPI-aware (Qt 6 default), so physical pixels are consistent with `grabWindow`.
- **R5 Presence**: `GetLastInputInfo` compared with the executor's own last-injection tick (+250 ms guard) — newer input not ours ⇒ human ⇒ pause.
- **R6 Banner**: frameless, `WindowStaysOnTopHint | Tool`, top-centre of the primary screen, ignores focus; excluded from screenshots by hiding it for the capture instant.
