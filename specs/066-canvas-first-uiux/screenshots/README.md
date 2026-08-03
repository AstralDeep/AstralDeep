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

## Not captured in this pass

- **Windows client**: needs an interactive signed-in run (the offscreen test
  harness renders no window). The 066 change is structural — `QSplitter`
  reordered to `[canvas, rail]` with the canvas stretching and
  non-collapsible, in `windows-client/astral_client/app.py`. Suite is green
  WITH the change: **701 passed / 7 skipped**.
- **Android client**: no emulator was launched on this box. The 066 change is
  the `SplitShell` row reorder in `AdaptiveShell.kt` (canvas then rail).
  Unit suite green WITH the change: **248 tests**.

Both gaps are recorded as open items in ../apple-handoff.md.
