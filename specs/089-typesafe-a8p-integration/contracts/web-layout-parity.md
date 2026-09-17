# Contract: Web Layout Parity with a8p (Desktop) and Responsive Checklist

**Reference**: a8p console at `bcdc014` (`a8p:astral/app.py`), served locally via `uvicorn astral.app:app --port 8001`.

**Reference captures**: taken with Playwright at 1920×1080, 1440×900 and 1280×800 in five states:

1. Landing (no conversation)
2. Conversation with a multi-component response
3. Response card hovered
4. Full-screen result view
5. Settings dialog open

Captures are stored under `specs/089-typesafe-a8p-integration/reference/` (PNG plus the DOM bounding-box JSON of each region).

**Scoring**
- Scoring applies only to **desktop viewports ≥1280 CSS px**.
- Each item is Pass (full weight), Partial (half weight: the position or structure matches but a dimension differs by more than the tolerance) or Fail (0).
- Score = Σ earned / Σ weights. The target is **≥ 90% at each of the three viewports**.
- Dimension tolerance is ±8% or ±8px, whichever is larger.
- Colors are **excluded** from scoring, because they come from `ThemeView` (owner directive).
- Content differences are also excluded: real agents instead of a8p's 11 demo agents, and welcome examples instead of a8p's 12 hard-coded scenarios.

**Verification**: `P:tests/web_layout_parity/` holds a Playwright script that computes bounding boxes and structural checks for items marked **[auto]**. Items marked **[review]** are scored by a person against the captures, and both results go in `verification.md`.

## A. Global frame (weight 14)

| # | Item | Weight | Check |
|---|---|---|---|
| A1 | Two-column app frame: fixed left sidebar + flexible main column, full viewport height, no page scroll (main column scrolls internally) | 4 | [auto] |
| A2 | Sidebar width 350px, right border divider, padding 22px, vertical gap 16px | 3 | [auto] |
| A3 | Main column background is a radial gradient anchored top-center (theme-derived) | 2 | [review] |
| A4 | Canvas area padding 32px 48px, thin 6px custom scrollbar | 2 | [auto] |
| A5 | Open Sans on every text element, including headings, code and inputs; heading weights 700–800, body 400–600 | 3 | [auto] (computed font family must be 100% — also SC-010) |

## B. Sidebar (weight 22)

| # | Item | Weight | Check |
|---|---|---|---|
| B1 | Brand block at top: logo image + stacked product name/subtitle, bottom divider; clicking returns to landing | 4 | [auto] structure, [review] look |
| B2 | Section header row "Agent Directory" with count badge right-aligned | 3 | [auto] |
| B3 | Search input with leading magnifier icon, filters the agent list live | 4 | [auto] |
| B4 | Scrollable agent list filling remaining height; each item shows icon, name, short description, status dot | 5 | [auto] structure, [review] item styling |
| B5 | Recent work section placed below the agent list (088 capability), collapsible | 2 | [auto] |
| B6 | User profile widget pinned to sidebar bottom: avatar with online dot, name, role, settings cog button at right | 4 | [auto] |

## C. Landing / empty state (weight 18)

| # | Item | Weight | Check |
|---|---|---|---|
| C1 | Page header row: title + subtitle left; status pill strip right (system live, agents ready, audit status, resume chat when applicable) | 4 | [auto] |
| C2 | Overview panel: header row + 3-column grid of numbered steps | 3 | [auto] |
| C3 | Scenarios section header with title left and horizontal filter tabs (active tab highlighted) | 4 | [auto] |
| C4 | Scenario card grid, `auto-fit` with a 350px minimum track — the same column count as the reference at each viewport (4 at 1920, 2 at 1440 and 1280) — with rounded 12px cards: agent tag + category badge header, title, description, run action; hover lift | 5 | [auto] grid, [review] card |
| C5 | Landing hides once a conversation has a response and reappears on "return to dashboard" | 2 | [auto] |

## D. Conversation and results (weight 24)

| # | Item | Weight | Check |
|---|---|---|---|
| D1 | Chat thread as a vertical feed with 24px gap; user turns right-aligned bubbles; assistant turns full-width | 5 | [auto] |
| D2 | Each assistant result in a response card: 14px radius, bordered, elevated shadow, full canvas width | 5 | [auto] |
| D3 | Response card header row: left agent badge/name + routing meta chips; right actions | 4 | [auto] |
| D4 | Hover overlay with "expand" chip that opens full-screen view | 3 | [auto] |
| D5 | Components inside cards use a8p component styling: metric/stat tiles, SVG charts, gauges, steppers, tables with sticky header | 5 | [review] |
| D6 | Progress/thinking state shown inline in the feed at the position of the pending turn | 2 | [auto] |

## E. Composer bar (weight 10)

| # | Item | Weight | Check |
|---|---|---|---|
| E1 | Composer bar pinned to bottom of main column, top border, translucent blurred background, padding 18px 48px | 3 | [auto] |
| E2 | Single wide text input (10px radius, 14px 18px padding) + gradient primary Send button at right | 4 | [auto] |
| E3 | 088 composer controls (attach, background, Advanced, voice) placed inside the bar as compact icon buttons left of Send, without adding a second row at ≥1280px | 3 | [auto] |

## F. Overlays (weight 12)

| # | Item | Weight | Check |
|---|---|---|---|
| F1 | Full-screen result overlay covering the viewport: header with icon badge, title row with badges, subtitle; "Exit Full Screen" button with ESC hint; scrollable canvas | 6 | [auto] structure, [review] look |
| F2 | Settings dialog: centered modal card with slide-up animation (reduced-motion respected), header icon badge + title + subtitle + close; tab strip; tab content; footer action row | 6 | [auto] structure, [review] look |

> **C4 correction (2026-09-17).** The row first read “3 columns at ≥1440, 2 at 1280”. The reference at `bcdc014` uses `grid-template-columns: repeat(auto-fit, minmax(350px, 1fr))`, which yields 4 columns at 1920, 2 at 1440 and 2 at 1280. The contract’s purpose is parity with the reference, so the row now states the reference’s own rule and the scorer compares against the captured column count rather than a written constant.

**Total weight: 100.**

**Mapping rule**: Where a region's a8p content has no Astral equivalent (for example the "AU-9 Sealed" demo badge), the equivalent 088 status or capability is placed in that slot. The item is scored on structure and position, not on literal text.

## Responsive checklist (viewports <1280px; pass/fail, 100% required — SC-012)

Viewports: 1024×768, 768×1024, 390×844, 320×640, plus 200% text zoom at 1280×800.

| # | Item |
|---|---|
| R1 | No horizontal page scroll at any tested viewport |
| R2 | 1024–1279: sidebar ≥ 280px and ≤ 300px; scenario grid 2 columns; canvas padding 24px |
| R3 | <1024: sidebar becomes an off-canvas drawer; toggle button visible in a top bar; focus is trapped in the open drawer; Esc and backdrop close it; focus returns to the toggle |
| R4 | <768: composer pinned to bottom above the safe-area inset; secondary composer controls in an overflow menu; Send always visible |
| R5 | Response cards single column; component grids respect ROTE `max_grid_columns` for the web profile |
| R6 | Full-screen overlay and settings dialog become full-viewport sheets <768; settings tabs scroll horizontally |
| R7 | Landing overview and scenario grid collapse to one column <768; filter tabs become a horizontally scrollable chip row |
| R8 | Tables: no overflow of the card; horizontal scroll inside the table container only; ROTE column caps applied on `tablet`/`mobile` |
| R9 | Charts, gauge, donut and radar legible: ≥ 11px text; donut and radar use the table form <700px |
| R10 | All controls ≥ 44×44 CSS px touch targets <1024 |
| R11 | No overlapping or clipped controls; every 088 capability reachable in ≤ 2 interactions from the main view |
| R12 | Orientation change re-adapts (web client re-registers viewport; ROTE re-renders) without losing the draft or scroll position |
| R13 | Keyboard, focus-visible, screen-reader names, reduced motion and 200% text behave per 088 FR-028 at each viewport |
