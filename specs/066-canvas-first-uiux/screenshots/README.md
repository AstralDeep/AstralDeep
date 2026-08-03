# Screenshot set — 066 final layouts

Captured 2026-08-03 on the Windows dev box against the local stack
(headless Chrome, `--window-size=W,H --screenshot`, mock auth).

| File | Width | Mode | What it shows |
|---|---|---|---|
| `web-split-1440.png` | 1440 | `split` | Canvas LEADS (left); conversation rail trails with the "CONVERSATION" header and the `»` collapse control; composer input on its own full-width row above the controls (paperclip / background-run / mic / Send) |
| `web-collapsed-900.png` | 900 | `collapsed` | Canvas uses the FULL width; the composer floats as a centered rounded bar over the canvas bottom, carrying the transcript toggle (speech-bubble icon) that opens the drawer |
| `web-stacked-500.png` | 500 | `stacked` | Phone width: canvas column, icon-only top bar, docked full-width composer |

All three show the **default** mode for that width (no stored preference), so
they double as evidence for the breakpoint rules: `<700` stacked,
`700–1023` collapsed, `≥1024` split.

## What these do NOT show

The canvas is in its empty state because headless Chrome exits before the
authenticated WebSocket hydration lands — the layout is real, the emptiness is
a capture artifact, not the product. Content-bearing states (dice component +
distribution chart, the 8-widget dashboard, the failed-turn card with Retry,
the transcript drawer open over the canvas) were verified interactively in the
same session; re-capture them on the Mac from a signed-in browser if the
handoff needs them as files.

## Native captures (added 2026-08-03, second pass)

| File | Client | What it shows |
|---|---|---|
| `windows-split-final.png` | Windows | The final Windows layout after the style-parity fix: icon-only top bar (＋ New · 💬 · ✨ · 🕓 · ⚙), canvas LEADING with a rendered dice component, conversation rail trailing with markdown-bold text, and a quiet composer (📎 · 🎙 · input · Send) with no permanent voice-state chip. Captured from a live signed-in run against the local orchestrator, mid-turn. |
| `android-tablet-signin.png` | Android | Sign-in on a 2560×1600 tablet emulator |
| `android-tablet-01.png`, `android-tablet-02.png` | Android | Canvas LEADING with welcome examples, CONVERSATION rail trailing — the `SplitShell` reorder on a live device |

These are the two natives the Mac pass should match the Apple clients against.
The one visible Windows blemish is the "Accepted" status band above the canvas,
which is the open phase-trail gap recorded in ../parity-checklist.md — not a
layout or style defect.
