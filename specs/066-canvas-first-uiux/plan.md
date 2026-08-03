# Implementation Plan: Canvas-First Adaptive UI/UX

**Branch**: `066-canvas-first-uiux` | **Date**: 2026-08-03 | **Spec**: [spec.md](spec.md)

## Summary

Rework the web shell/chrome so the canvas is the primary surface in three layout
modes (expanded / collapsed / stacked), make the composer honest (default voice
affordance, connection state, queued sends), make the turn lifecycle honest
(phases, failure-with-retry, markdown, no boilerplate bubbles, real titles), calm
the canvas chrome, feed ROTE a live capability envelope, and finish the two
groundwork bug fixes (new-chat operation binding; keyless custom endpoints) with
tests. Voice-session reliability gains a readiness surface, bounded preflight
re-checks, and composer-visible refusal reasons. Android/Windows get a
checklist-driven consistency pass; an Apple handoff doc closes the feature.

## Technical Context

- **Backend**: Python 3.11 (production image; local `.venv` 3.13). Web render
  layer is ES5 vanilla JS + CSS, server-rendered shell (`backend/webrender/`,
  no build step). **Zero new third-party runtime dependencies** (Constitution V).
- **Key existing seams (verified live 2026-08-03)**:
  - Shell: [shell.html](../../backend/webrender/templates/shell.html) — two-panel
    flex; `body[data-astral-layout]` stamped by
    [client.js](../../backend/webrender/static/client.js) `applyLayoutClass()`
    (600px stacked threshold today); styling in
    [astral.css](../../backend/webrender/static/astral.css) (~line 1030 layout block).
  - Capability report: `detectDeviceCapabilities()` (client.js ~370) one-shot at
    `register_ui`; server model `rote/capabilities.py` `DeviceCapabilities` /
    `DeviceProfile.from_dict`; per-socket profile stored at registration.
    `FF_LIVE_VIEWPORT` re-adaptation exists server-side but is never fed.
  - Voice composer: `#astral-voice-controls` empty host + `composer_state`
    frames (`webrender/chrome/composer_model.py`); CSS text-stub icons
    (`astral.css` ~665-730); `publish_voice_composer_state`
    (orchestrator.py ~17870) returns None on any projection error → empty host.
  - Turn pipeline: ui_event `chat_message` branch (orchestrator.py ~7841),
    `_serialized_chat` → `handle_chat_message` → `_begin_conversation_publication`
    with the 060 operation fences (orchestrator.py ~9500, history.py stage_commit,
    task_state.py); `bind_chat` added in work_admission (groundwork commit).
  - Keyless LLM: `llm_config/client_factory.py::openai_auth_kwargs` + shared
    keyless httpx client (groundwork commit); key-optional `custom` preset.
  - Welcome: `orchestrator/welcome.py` (wel_ identities); pushed only at
    register_ui today. Provenance footers: `FF_PROVENANCE_SURFACING` stamping.
  - Voice worker: `voice_agent/` — control-channel reconnect w/ 0.5–5s backoff
    (control.py ~906); speech preflight strict inventory + live probes
    (speech_adapters.py ~130/340). Speech endpoint = the UK LLM factory
    (`OPENAI_*` → `VOICE_SPEECH_*` via compose), verified serving GLM + Kokoro
    TTS + faster-whisper ASR routes with the operator key.
- **Storage**: No schema change anticipated. Layout-mode persistence is
  client-local (localStorage). `SCHEMA_REVISION` untouched.
- **Wire**: No new frame types (Constitution XII — `ui_protocol.json` untouched).
  New C→S `ui_event` **action** `capability_update` (actions are not
  frame-type vocabulary); additive optional fields inside existing frames only.
- **Testing**: backend pytest in the `astraldeep` container (CI flag posture per
  the three-invocation layout in ci.yml); browser verification via scripted
  Chrome sweeps; Windows/Android suites for the consistency pass.

## Constitution Check

- II (SDUI: astralprims defines → orchestrator renders → ROTE adapts): PASS —
  component vocabulary untouched; only shell/chrome and adaptation inputs change.
- V (no new third-party runtime deps): PASS — vanilla JS/CSS + stdlib; markdown
  rendering in bubbles is a small safe-subset renderer, not a library.
- IX (idempotent migrations): PASS — no schema delta.
- XI (CI gates): PASS — changed-line coverage ≥90% target; ruff from repo root.
- XII (frozen wire manifest): PASS — no new frame types; `capability_update` is
  a ui_event action; composer default state is client-local.

## Project Structure (files touched)

```text
backend/webrender/templates/shell.html      # composer rework, default voice control, connection pill,
                                            # canvas toolbar, collapse toggle, drawer hosts
backend/webrender/static/astral.css         # 3 layout modes, component-chrome reveal, SVG icon masks,
                                            # queued-bubble/error styles, breakpoints 700/1024
backend/webrender/static/client.js          # layout modes + persistence, capability_update sender,
                                            # send queue + connection state, failure UX + retry,
                                            # markdown bubbles, status phases, welcome-on-new-chat,
                                            # component-chrome touch affordance, voice default state
backend/rote/capabilities.py                # + reduced_motion, pointer_type (additive, default-safe)
backend/orchestrator/orchestrator.py        # capability_update handler → profile refresh;
                                            # welcome push on explicit new chat; title fallback;
                                            # boilerplate→metadata; voice status surface wiring
backend/orchestrator/voice_bootstrap.py     # readiness snapshot (workers/refusal/preflight verdicts)
backend/orchestrator/voice_api.py           # GET /api/voice/status (authed, operator-readable)
backend/voice_agent/control.py|main.py      # bounded preflight re-check loop; startup log lines
backend/orchestrator/welcome.py             # wel_ components excluded from provenance stamping
backend/orchestrator/history.py             # (only if preview strip needs server side)
backend/*/tests/…_066.py                    # regression pins: bind_chat fences, keyless auth kwargs,
                                            # key-optional custom, capability refresh, welcome-on-new,
                                            # title fallback, voice status surface
windows-client/  android-client/            # consistency pass per parity checklist (US8)
specs/066-canvas-first-uiux/apple-handoff.md  + screenshots/   # US8 deliverable
```

## Phasing

- **P0 groundwork (committed)**: bind_chat + fences; keyless auth kwargs;
  key-optional custom. Tests land in this feature.
- **P1 web core**: layout modes + composer + connection/queue + voice default +
  capability envelope + turn failure UX. Verify in browser sweep.
- **P2 lifecycle & chrome**: phases, markdown, boilerplate metadata, titles,
  component-chrome reveal, canvas toolbar, welcome-on-new-chat.
- **P3 voice reliability**: readiness surface, preflight re-check, composer
  refusal reasons (incl. permission-shaped timeout reason).
- **P4 native consistency + handoff**: Windows/Android checklist pass; Apple
  handoff doc + final screenshots.

## Decisions (defaults chosen; revisit only on evidence)

- Breakpoints: `<700px` stacked; `700–1023px` collapsed-by-default (canvas
  full-width, chat as overlay drawer); `≥1024px` expanded rail by default with
  a persisted user toggle. Kills the 600–1024 crush zone.
- Collapsed-mode composer: floating bottom bar over the canvas (max-width
  ~760px, centered); transcript = right-side overlay drawer (stacked keeps the
  bottom Messages sheet, dimmed canvas behind, 60vh max).
- Auto-reveal: badge + peek (300ms, suppressed under reduced-motion) on new
  assistant text while hidden; never auto-expand permanently.
- Voice icons: inline SVG masks keyed by the existing `data-icon` contract —
  no client-JS changes to the frame consumer, CSS-only swap + shell default.
- Send queue: bounded (5), per-tab, flushed on registration ack; refusal after
  45s with per-message retry affordance. Queue is memory-only by design.
- Markdown: safe-subset renderer (escape-first; bold/italic/inline-code/links/
  lists/headings-as-bold) shared by bubbles; previews strip via the same code.
- `capability_update`: debounced 400ms after resize settle; fires on
  orientation/permission/connection/reduced-motion changes; server refreshes
  the socket's ROTE profile and stamps `data-rote-device` via existing
  `rote_config` push.
```
