# Contract: Graphite preset

## Server roles (all seven preset tables)

| Role | Hex | Purpose |
|---|---|---|
| bg | `#121214` | near-black workspace (lightened from `#0B0B0D` after the owner saw it live) |
| surface | `#1B1B1F` | panels, cards, sidebar |
| primary | `#7C3AED` | purple controls, selection, links; white text on it measures about 5.7:1 |
| secondary | `#6E6E78` | gray accent, secondary gradient stop |
| text | `#ECECEF` | body text |
| muted | `#8C8C95` | secondary text, icons |
| accent | `#A78BFA` | lighter purple highlights |

Background luminance is below the 0.5 threshold, so the Mac keeps the dark color scheme.

## Apple-only extras (`NativeAppearance/Theme.swift`)

| Role | Hex |
|---|---|
| surface2 | `#232328` |
| border | white at 8 percent |
| success | `#5EC28F` |
| warning | `#D4A94A` |
| error | `#E06C6C` |
| info | `#9A8CF2` |

## Where it lands

1. Deep `backend/orchestrator/projection_surfaces/theme.py`: `PRESETS["graphite"]` first in order; `_DEFAULT_PRESET = "graphite"`; summary "default (Graphite)".
2. Deep `backend/agents/general/mcp_tools.py` preset copy.
3. Projection `src/astralprojection/chrome/personalization.py` canonical copy, Graphite first.
4. Projection `backend/webrender/static/client.js` preset copy and `backend/webrender/static/astral.css` `:root` tokens switched to Graphite.
5. Projection `windows-client/astral_client/theme.py` preset copy and `PALETTE` default.
6. Projection `android-client/.../ui/theme/Theme.kt` preset copy and `DEFAULT_PALETTE`.
7. Projection `apple-clients/NativeAppearance/Theme.swift`: `AstralPalette()` initializers become Graphite; `static let graphite = AstralPalette()`; `midnight` becomes an explicit case with its former values; `apply(preset:)` default falls back to `.graphite`; every preset case sets `surface2`.
8. `ThemeView` default name "graphite" in Projection `models.py` and Deep `projection_controllers.py`.

Only the Mac client ships in this feature; the Windows, Android and web edits are table and default changes without behavior logic and are covered by the parity test.

## Delivery to clients without a stored theme

In the `register_ui` handler, when no stored theme exists, the server sends `{"type": "user_preferences", "preferences": {"theme": {"preset": "graphite", "colors": {...seven roles...}}}}`. Clients that know the name apply the preset; clients that do not know it ignore the name and apply the colors. Stored themes are sent as today and win.

## Mac sweep

- `ComponentView.swift:519-520` code text color becomes `palette.success` on `palette.surface2`; `MarkdownBlockView.swift:39` uses `palette.surface2`.
- `.foregroundStyle(.white)` on primary fills stays; `#7C3AED` keeps white text above 4.5:1 (the lighter `#8B5CF6` measured 4.24:1 and was rejected).
- `RootView.swift:508` and `MacComputerControls.swift:52,98` use `palette.error`.
- `BannerBar` gains an SF Symbol per variant.
- `OfflineChartView.swift` injects `primary`, `secondary`, `accent` for series so charts follow the palette.

## Tests

Update the pinned values and names in Deep `tests/chrome/test_surface_theme.py`, `test_surface_components.py`, `test_projection_controllers.py`; Projection `tests/test_theme_preset_parity.py`, `tests/chrome/test_personalization_views.py`, `tests/test_no_hex_literals_089.py`; Windows `tests/test_theme_live.py`; Android `ThemeTest.kt`; Mac `SettingsControlTests.swift`, `WebPrimitiveStyleTests.swift`, `OfflineChartTests.swift`, `CanvasCapture088Tests.swift`, `WorkspaceExportPipeline088Tests.swift`; watch `ConsoleWatchTests.swift`. Add a Deep test that `register_ui` without a stored theme sends the Graphite colors and that a stored Midnight is sent unchanged.

## Rulings from the builder check (2026-10-25)

- Stored custom themes carry only the seven roles and no preset name. On the Mac, applying a colors-only theme derives the Apple extras from the stored roles: `surface2` is `surface` lightened by 4 percent when the background is dark and darkened by 4 percent when light, `border` is `text` at 8 percent, and the four status colors stay the Graphite set. A stored `{"preset": "midnight"}` applies the full Midnight case including its original `surface2`.
- `Theme.swift` is shared with iOS and watchOS; their defaults become Graphite too. The spec's "no client code changes" for other clients means no behavior logic changes; theme table data on Windows and Android and the shared Apple palette default do change.
