# Screenshot set — 066 final layouts

Captured 2026-08-03 on the Windows dev box (Chrome, local stack, mock auth).

| File | What it shows |
|---|---|
| web-split-1264.png | Split mode (>=1024px): canvas leads, conversation rail trails with header + collapse control |
| web-collapsed-1264.png | Collapsed mode: full-width canvas, centered floating composer |
| web-collapsed-open-1264.png | Collapsed mode with the transcript drawer open over the canvas |
| web-stacked-504.png | Stacked mode (phone width): canvas column + "Messages (N)" bar + docked composer |
| web-turn-dice.png | Completed turn: canvas component + rail narrative, calm chrome at rest |
| web-failed-turn.png | Failure state: user message kept, inline status near the composer, canvas not blanked |

## Not captured in this pass

- **Windows client**: needs an interactive signed-in run (the offscreen test
  harness renders no window). The 066 change there is structural
  (`QSplitter` order/stretch in `astral_client/app.py`) — screenshot it on the
  next signed-in launch.
- **Android client**: no emulator was launched on this box. The 066 change is
  the `SplitShell` reorder in `AdaptiveShell.kt`; capture on a tablet-width
  emulator (canvas left, rail right).

Both gaps are recorded as open items in ../apple-handoff.md.
