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
| B1 | Brand block at top: logo image only — no product name, no tagline — bottom divider; clicking returns to landing | 4 | [auto] structure, [review] look |
| B2 | Section header row "Agent Directory" with count badge right-aligned | 3 | [auto] |
| B3 | Search input with leading magnifier icon, filters the agent list live | 4 | [auto] |
| B4 | Scrollable agent list filling remaining height; each item shows name and short description flush to the card's left edge, plus a status dot — no per-agent icon | 5 | [auto] structure, [review] item styling |
| B5 | History section placed above the agent directory (088 capability), collapsible | 2 | [auto] |
| B6 | Account row pinned to sidebar bottom: picture, name, role, then the settings cog; the cog opens the settings dialog | 4 | [auto] |

## C. Landing / empty state (weight 18)

| # | Item | Weight | Check |
|---|---|---|---|
| C1 | Page header row: title + subtitle left (status pill strip removed per owner directive) | 4 | [auto] |
| C2 | No explanatory overview panel: the examples section follows the page header directly | 3 | [auto] |
| C3 | Scenarios section header with title left and horizontal filter tabs (active tab highlighted) | 4 | [auto] |
| C4 | Scenario card grid, `auto-fit` with a 350px minimum track — the same column count as the reference at each viewport (4 at 1920, 2 at 1440 and 1280) — with rounded 12px cards: agent tag + category badge header, title, description, run action; hover lift | 5 | [auto] grid, [review] card |
| C5 | Landing hides once a conversation has a response and reappears on "return to dashboard" | 2 | [auto] |

## D. Conversation and results (weight 24)

| # | Item | Weight | Check |
|---|---|---|---|
| D1 | Chat thread as a vertical feed with 24px gap; user turns right-aligned bubbles; assistant turns full-width | 5 | [auto] |
| D2 | Each assistant result in a response card: 14px radius, bordered, elevated shadow, full canvas width | 5 | [auto] |
| D3 | Response card header row: left agent badge/name + routing meta chips; right actions | 4 | [auto] |
| D4 | Always-visible "Open full screen" action immediately after the Turn label in the response header | 3 | [auto] |
| D5 | Components inside cards use a8p component styling: metric/stat tiles, SVG charts, gauges, steppers, tables with sticky header | 5 | [review] |
| D6 | Progress/thinking state shown inline in the feed at the position of the pending turn | 2 | [auto] |

The D4 placement records the owner's 2026-09-21 refinement: the action is in
the header on desktop and phone, with no hover requirement. Generated and
grounded results omit repeated provenance footers; estimated-value warnings
and server-stamped provenance remain available. UI v2's current implementation
and source-checkpoint scope are documented in
`components/AstralProjection/docs/UI_V2.md`.

## E. Composer bar (weight 10)

| # | Item | Weight | Check |
|---|---|---|---|
| E1 | Composer bar pinned to bottom of main column, top border, translucent blurred background, padding 18px 48px | 3 | [auto] |
| E2 | Single wide text input (10px radius, 14px 18px padding) + gradient primary Send button at right | 4 | [auto] |
| E3 | Attachment and voice controls stay beside the input; More options opens Run in background, Advanced settings, Workspace timeline and Pulse digest; Send remains at the right | 3 | [auto] |

## F. Overlays (weight 12)

| # | Item | Weight | Check |
|---|---|---|---|
| F1 | Full-screen result overlay covering the viewport: header with icon badge, title row with badges, subtitle; "Exit Full Screen" button with ESC hint; scrollable canvas | 6 | [auto] structure, [review] look |
| F2 | Settings dialog: centered modal card with slide-up animation (reduced-motion respected), header icon badge + title + subtitle + close; **left menu rail** (the settings menu) + right options pane; footer action row when the surface declares one | 6 | [auto] structure, [review] look |

> **Owner directive, 2026-09-18 — five rows superseded.** The owner
> directed five deliberate departures from the a8p reference after reviewing
> the built console. Parity with a8p is the contract's purpose, so where the
> owner has since decided against a8p's choice, the row states the decision
> and the harness scores that instead. The reference captures are unchanged
> and still hold for every other row.
>
> - **B1** — the sidebar's wordmark and "Multi-agent workspace" tagline are
>   gone. The logo alone identifies the product; the console's own title and
>   subtitle already sit at the top of the page, so the sidebar was saying it
>   twice.
> - **B4** — the per-agent initials badge is gone and the name/description
>   move to the card's left edge. Ten two-letter badges down the sidebar were
>   noise, and they distinguished nothing the name did not.
> - **B6** — the profile widget is the settings cog alone. The name, role and
>   avatar head the settings dialog the cog opens, so the identity is one
>   click away instead of permanently occupying the sidebar's bottom. The
>   model's action controls (Pulse, Recent work, Workspace timeline) moved
>   into that dialog's rail with it. **Native clients are unaffected**: the
>   chrome model still carries them in `topbar`, and only the web renderer
>   changed.
> - **C2** — the "How a turn runs" panel (Route / Gate / Render) is gone. It
>   explained the architecture to someone who had not asked; the examples
>   below it demonstrate the same thing by being run.
> - **F2** — the gear opens the settings dialog directly and the menu is the
>   dialog's **left rail**, not a dropdown and not a tab strip. Every settings
>   surface therefore shows the menu, so moving between them is one click
>   rather than close-reopen-pick. a8p's tab strip remains available to any
>   surface that declares `SECTIONS`; the rail is the level above it.
>
> Two rows that read on the same regions are deliberately unchanged: **B2**
> (the directory's header and count badge) and **B5** (Recent work).

> **Owner directive, 2026-09-19 — B6 reversed, B5 amended.** A second
> walkthrough of the built console changed two of the decisions above. The
> reference captures are still unchanged.
>
> - **B6** — the account row carries the signed-in person again: a default
>   drawn avatar, their name and their role, then the cog at the row's right
>   edge. A console showing no sign of whose account it is reads as signed
>   out, and one click away turned out to be one click too many for something
>   a person checks at a glance. The settings dialog keeps its own account
>   block, from the same `web_auth.identity_from_claims`, so the two cannot
>   disagree. The model's action controls stay in the dialog's rail, and
>   native clients remain unaffected either way.
> - **B5** — the section is titled **History**, and its header carries one
>   icon-only New-chat button. It had carried two adjacent buttons, "+ New
>   chat" and a Recent-chats button that opened the list directly beneath it.
>   The collapse control was also inert: `client.js` re-homes the toggle into
>   that header, which broke the adjacent-sibling rule that had hidden the
>   list, so the chevron turned and nothing else happened. The section now
>   carries the collapsed state itself.
> - **F2 addition** — where a surface declares `SECTIONS`, its tab strip
>   renders inside the pane beside the rail rather than across the whole
>   dialog. Run full width it sat above the rail too, reading as navigation
>   for the rail as well as for the pane.

> **Owner directive, 2026-09-21 — B5 moved above agent directory; C1 header buttons removed; welcome section removed.** History and
> the agent directory switched places in the sidebar: History sits on top
> (below the brand), with the agent directory and search input below. The
> status pill buttons in the dashboard page header are removed. The legacy
> "How can I help?" welcome section and example pills on the dashboard page are
> also removed in favor of the primary scenario card grid.

> **C4 correction (2026-09-17).** The row first read “3 columns at ≥1440, 2 at 1280”. The reference at `bcdc014` uses `grid-template-columns: repeat(auto-fit, minmax(350px, 1fr))`, which yields 4 columns at 1920, 2 at 1440 and 2 at 1280. The contract’s purpose is parity with the reference, so the row now states the reference’s own rule and the scorer compares against the captured column count rather than a written constant.

**Total weight: 100.**

**Mapping rule**: Where a region's a8p content has no Astral equivalent (for example the "AU-9 Sealed" demo badge), the equivalent 088 status or capability is placed in that slot. The item is scored on structure and position, not on literal text.

**Owner-directive rule**: a row the owner has since directed away from a8p is scored against the directive, not the capture, and the directive is recorded in this file with its date. The reference captures are never re-shot to match: they are the record of what was compared, and a row that no longer compares to them says so in its own text.

**No emoji**: nothing this console renders carries an emoji — not example titles, not agent glyphs, not status marks. Where a8p or an earlier Astral build used one, the replacement is a word, a drawn SVG icon, or nothing. This is a standing project rule, not a row of this contract, and it applies to every client.

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
