# Contract: copy cuts and layout fixes

Classes: CUT removes the string at its source. KEEP leaves it. STATE keeps it because it reports a condition the user acts on. LAYOUT names the structural change that carries the meaning after a cut. Accessibility labels, hints and tooltips are never cut.

## Mac Swift (Projection `apple-clients/AstralApp/AstralApp`)

| Location | String | Action |
|---|---|---|
| `Views/ComponentView.swift:1379,1496,1583,1598,1627` | form `description` and field `help` drawn as captions and as placeholders | CUT the caption rendering; keep a short example placeholder only when the server marks it `example` |
| `Views/ChatView.swift:820` | composer placeholder | CUT; LAYOUT: visible field outline, send control always drawn |
| `Views/ChatView.swift:706` | history-lock sentence | CUT; LAYOUT: inline chip "Viewing history" with "Back to live" |
| `Views/ChatView.swift:399-401` | generated-interface tagline and helper | CUT |
| `Views/ChatView.swift:418-481` | history label, helper, component count | CUT label, helper and count; KEEP snapshot date as STATE |
| `Views/ChatView.swift:243` | "CONVERSATION" rail label | CUT |
| `Views/MacWorkbenchView.swift:308` | "AstralDeep" beside sidebar logo | CUT; logo only |
| `Views/MacWorkbenchView.swift:344,571-591` | "No chats", "No activity" | CUT; empty list stays empty |
| `Views/MacWorkbenchView.swift:454` | canvas header "{agent} Interface" | CUT; LAYOUT: result title or agent name |
| `Views/ConsoleShell.swift:477,489,528` | ornament glyph, subtitle, role | CUT; title and close only |
| `Views/ComponentChrome.swift:47` | row words refine, history, csv, share | CUT; glyph only, tooltip keeps the word |
| `Views/ComponentChrome.swift:95,126` | refine and history helper sentences | CUT |
| `Views/ComponentView.swift:867,602` | attach hint, "No conversations yet." | CUT |
| `Views/RootView.swift:148` | "This Mac" strip label | CUT; keep the button |
| `Views/RootView.swift:426` | second sentence of connection error | CUT |
| `Views/Screens.swift:226-232` | second sentence of load error | CUT |
| `Computer/MacComputerControls.swift:54,60,73` | diagnostics line, not-available paragraph, "Saved for…" | CUT; LAYOUT: disabled controls plus status for not-available |
| `Computer/MacComputerControls.swift:56,84` | consent paragraphs | condense to one line each under "Permissions" and "Workspaces" headings |
| `Computer/MacComputerHost.swift:231` | "macOS checks: …" | CUT |
| `Computer/MacComputerHost.swift` recovery sentences in `lastError` | STATE; trim to the first sentence |
| `AppModel.swift:2024-2039` | first two background-task banners | CUT; keep "Background task finished" |
| `AppModel.swift:2055,3661` | "Saved <title>", attachment sentence | CUT |
| `Voice.swift:752-772` (AstralCore) | voice guidance sentence | CUT |

## Server-owned (Deep `backend/orchestrator`)

| Location | String | Action |
|---|---|---|
| `welcome.py:24-51` | seven scenario captions | CUT (send empty `description`) |
| `welcome.py:84,92` | "How can I help?", "More examples" | CUT |
| `projection_surfaces/agent_intro.py:15,170-195` | subtitle, repeated example prompts | CUT; keep "Tools it can reach (n)" |
| `projection_surfaces/*.py` captions, info alerts, field helps (agents, llm, theme, personalization, workspace_timeline, my_computers, pulse and the rest) | CUT captions and helps; KEEP labels, headings, error and success alerts as STATE |
| `chrome_events.py:490-522,750-801`, `projection_controllers.py:421-447` | failure notices | STATE |
| `llm_config/data_sharing.py:21-30` | data-sharing consent | KEEP |

## Server-owned (Projection `src/astralprojection` and `backend/webrender`)

| Location | String | Action |
|---|---|---|
| `backend/webrender/chrome/console_model.py:13-15,23,26-27` | console taglines and empty-state sentences | CUT; contract change below |
| `backend/webrender/chrome/console_model.py:28` | composer placeholder | CUT |
| `backend/webrender/chrome/console_model.py:46-47` | "{agent} Interface" | CUT; result title |
| `backend/webrender/chrome/web_landing.py:15-24` | category labels | CUT |
| `src/astralprojection/chrome/*.py` captions, info and empty alerts, helps | CUT; KEEP labels and state alerts |
| `src/astralprojection/chrome/terminal.py:92,96,263,349,269,276,290,147,158,358` | helper and placeholder sentences | CUT |
| `backend/webrender/chrome/component_model.py:13-16` | row words | CUT; tooltip keeps the word |

## Contract change

`AstralCore/Chrome/ConsoleModel.swift:154,195` requires `title`, `subtitle`, `start_here`, `example`, `empty_*` and `message_placeholder` to be non-empty. These become optional and may be empty. `contracts/ui_protocol.json` console labels, `contracts/fixtures/console/chrome-console.json`, `watch-menu.json`, Windows `test_console_shell.py`, Android `ConsoleShellTest.kt` and Deep `test_native_console_surfaces.py` are updated in the same change. Web, Windows and Android renderers already tolerate empty strings; the Mac and watch parsers are the ones that change.

## Layout fixes

A. Composer: no placeholder; the field has a visible outline; the send control is always drawn; the locked state is an inline chip "Viewing history" with a "Back to live" button.

B. Canvas header: shows the first component title, else the agent name; icon buttons keep their tooltips.

C. Settings forms and Computer controls: labels stand alone; examples live only in the placeholder; Computer controls group rows under "Permissions" and "Workspaces" headings with one-line consent text each.

## Tests to update

Swift: `MacWorkbenchViewTests.swift` forbidden-copy list (extend with every CUT string above), `ConsoleShellPresentationTests.swift`, `SettingsControlTests.swift`, `AppModelBackgroundContinuityTests.swift`, `MacComputerHostTests.swift`, `ConsoleModelTests.swift`, `ComponentChromeTests.swift`, `ConsoleWatchTests.swift`, `WorkspacePresentationUITests`, `ConversationContinuityUITests`, `AppModelChromeSurfaceTests`, `FirstLoginUITestFixture.swift:412`.

Deep: `test_welcome.py`, `test_welcome_identity.py`, `test_wiring_030.py`, `test_first_turn_contract.py`, `test_curated_examples_060.py`, `tests/chrome/test_surface_*.py`, `test_native_console_surfaces.py`, `test_pulse.py`, `test_search_surface.py`, `test_typesafe_settings.py`.

Projection: `tests/chrome/test_console_model.py`, `tests/chrome/test_personalization_views.py`, `tests/chrome/test_agent_views.py`, Windows `test_console_shell.py`, `test_detail_surface_geometry.py`, `test_message_routing.py`, Android `ConsoleShellTest.kt`.

## Rulings from the builder check (2026-10-25)

- Windows `console.py:113` and Android `ConsoleModel.kt:75-91` reject blank console labels, and `message_placeholder` is their accessible name. So the server keeps sending every label other clients require: `title`, `subtitle`, `start_here`, `example`, `empty_*`, `message_placeholder` and `result_*` stay non-empty on the server. The Mac stops drawing them visually and keeps `message_placeholder` as the composer's accessibility label. T007 still makes the Mac and watch parsers tolerate absent labels so a later server cut cannot break them.
- Cut at source applies to strings no client requires to decode: welcome scenario captions, "How can I help?" and "More examples", the agent intro subtitle and repeated prompts, surface captions, info alerts and field helps, web landing categories, terminal helper sentences and component row words. These are optional fields on every client.
- `subtitle` on the server changes from the tagline to the short functional label "Workspace" so Windows and Android keep a valid non-empty label without a tagline.
