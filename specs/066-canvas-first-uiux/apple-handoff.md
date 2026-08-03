# Apple handoff — canvas-first UX (066)

**Written on the Windows box, 2026-08-03, for the iOS/macOS/watchOS pass on the Mac.**
Everything below is either verified live on this machine or explicitly marked
unverified. Read [spec.md](spec.md) for the requirements and
[parity-checklist.md](parity-checklist.md) for the row-by-row status.

> **Read [mac-agent-notes.md](mac-agent-notes.md) before you merge anything.** It
> covers the two operational tasks this document does not: syncing `.env` with
> the production variables while staying in development posture, and what the
> merge does to each of the three client releases. The single item worth reading
> first: **merging a branch that touches `apple-clients/**` auto-triggers an App
> Store upload** — so a version bump has to land *in* that merge, not after it.

## What changed, in one paragraph

The web client became canvas-first: three layout modes replace the old
fixed 380px rail, the composer gained an always-present voice control plus
connection/queue honesty, failed turns keep the user's message and offer
retry, per-component chrome hides at rest, and the client now re-reports its
capability envelope live so server-side adaptation never goes stale. Two
blocking product bugs were fixed on the way (every first message of a new
chat failed at a publication fence; keyless custom LLM endpoints were
unusable). Windows and Android received the one structural parity fix they
needed — canvas leads, conversation trails.

## The layout contract Apple should match

| Mode | Trigger (web) | Arrangement |
|---|---|---|
| `stacked` | < 700 CSS px | Canvas column on top (flex 1), collapsible "Messages (N)" panel, docked composer full width |
| `collapsed` | 700–1023 px by default; any width by user choice | Canvas uses the FULL width; composer floats as a centered bar (max 760px) over the canvas bottom; transcript opens as a drawer inside that bar with an unread badge |
| `split` | ≥ 1024 px by default | Canvas leads (left, stretching), conversation rail trails (right, ~320–420px) with a header + collapse control |

Apple equivalents to decide on the Mac: iPhone → `stacked`; iPad
portrait/compact-width multitasking → `stacked` or `collapsed`; iPad
landscape + macOS windows → `split` with the canvas LEADING. The
non-negotiable invariants are: canvas takes the leading edge, the
conversation never permanently occupies more than ~1/3 of a wide window, and
the composer input never falls below ~20 visible characters.

## Capability envelope (the ROTE contract)

Registration and every material change now carry:
`viewport_width/height`, `screen_width/height`, `pixel_ratio`, `has_touch`,
`has_geolocation`, `has_microphone`, `microphone_permission`,
`has_audio_output`, `connection_type`, `user_agent`, plus the two fields
added by this feature: **`reduced_motion` (bool)** and
**`pointer_type` ("fine" | "coarse")**.

Web re-reports via a `ui_event` with **`action: "update_device"`** and
`payload.device` = the same dict as `register_ui.device`; the server refreshes
the socket's ROTE profile and re-pushes `rote_config`. **No new frame type AND
no new action** — `update_device` is already in the sanctioned action list
([ui_protocol.json:181](../../backend/shared/ui_protocol.json#L181)), so
Constitution XII is untouched in both directions.

> An earlier draft of this document named a `capability_update` action. That
> action was drafted and then **removed**: the existing `update_device` already
> did the same job and did it better, so shipping a second one would have been
> pure divergence. `update_device` is what the client sends
> ([client.js:411](../../backend/webrender/static/client.js#L411)). Do not
> implement `capability_update` on any Apple client — it does not exist.

Apple clients own their own reflow (like Android), so re-reporting is
optional there; if the Apple canvas ever depends on server-side density
adaptation, use the same action. The additive fields are safe to send today —
older servers ignore unknown keys, and the server defaults them.

## Voice composer

The server's `composer_state` frame is unchanged. The web fix was
web-specific: an empty control host was being hidden by CSS, so a failed
server projection erased the voice affordance entirely. Web now pre-renders a
disabled voice-start control and re-renders it on socket teardown. **Apple
clients that render the server's control model directly do not need this**;
what they DO need is to keep showing a disabled mic with the reason when
`available=false`, never nothing.

Real SVG icons now back the `data-icon` values: `microphone`,
`device-transfer`, `stop`, `speaker-stop`, `speaker-muted`, `chat`,
`speaker-consent`. Match SF Symbols to those semantics.

## Voice service status (read before testing voice on the Mac)

The speech endpoint (`https://api-llm-factory.ai.uky.edu/v1`) was verified on
2026-08-03 to serve BOTH speech models and their routes:
ASR `Systran/faster-whisper-large-v3` (POST `/audio/transcriptions` → 200) and
TTS `speaches-ai/Kokoro-82M-v1.0-ONNX` voice `af_heart`
(POST `/audio/speech` → 200, 34,860-byte WAV). The 2026-08-02 wiki note that
the inventory omitted them is stale.

The production sandbox still returns 503 on `POST /api/voice/sessions`
(no admitted worker). Diagnosis + runbook:
[voice-prod-diagnosis.md](voice-prod-diagnosis.md). The top suspect is a
container that was `restart`ed rather than recreated after the secret
rotation, so it presents a stale `VOICE_CONTROL_SECRET` — locally this
reproduces as `"WebSocket /api/voice/worker-control" 401` in the orchestrator
log with an EMPTY worker log.

## Screenshots

`screenshots/` in this directory (headless Chrome against the local stack):

- `web-split-1440.png` — split mode: canvas leads, rail trails with header + `»` collapse
- `web-collapsed-900.png` — collapsed mode: full-width canvas, centered floating composer with the transcript toggle
- `web-stacked-500.png` — stacked mode (phone width): canvas column + docked composer

Each is the DEFAULT mode for that width, so the set doubles as breakpoint
evidence. Their canvases are empty because headless exits before the
authenticated WebSocket hydration lands — the layout is real, the emptiness is
a capture artifact.

The native captures were taken in a second pass and are the ones to match:

- `windows-split-final.png` — the final Windows layout from a live signed-in
  run, mid-turn: icon-only top bar, canvas leading with a rendered component,
  conversation trailing with markdown-bold text, quiet composer
- `android-tablet-01.png`, `android-tablet-02.png` — canvas leading /
  conversation trailing on a 2560×1600 emulator; `android-tablet-signin.png`
  is the sign-in screen

See `screenshots/README.md` for the content states verified interactively but
not written to disk.

## Open items for the Apple pass

1. Verify P1–P9 in [parity-checklist.md](parity-checklist.md) on iOS
   (iPhone + iPad), macOS, and watchOS; record divergences with reasons.
2. Decide the iPad multitasking mapping (compact width → stacked vs collapsed).
3. Confirm the disabled-with-reason voice state renders on every Apple client
   when `available=false` (watch included).
4. If voice is exercised, run the runbook first — a 503 there is the
   deployment issue above, not an Apple-client bug.
5. Not audited anywhere yet (flagged in the checklist, not claimed): native
   send-queue behavior while disconnected (P6) and native failed-turn retry
   (P7). These are web-only implementations today.
