Written for: the reviewer of the AstralProjection pull request.

# Six web renderers, the a8p console layout, and one font

**Merge order: third**, after AstralPrimitives 0.4.0 and AstralPlane 089.001,
before AstralDeep.

Five commits: `aeaf6e4` (renderers, ROTE ladders, the web-only guard),
`fe345bc` (the a8p console layout), `7d9bd62` and `03284cc` (the astralprims
0.4.0 pin and its recorded digest), `b10ff5c` (what qualification found).

---

## 1. Six renderers, and the guard that keeps them web-only

One `render_<type>` per new type, registered in `PRIMITIVE_RENDERERS`. Every
string passes through `esc()`; every number is coerced and clamped. No field
value reaches `style`, `class`, `on*` or `href`. Charts are inline SVG using
`currentColor` and theme-bound classes.

Also in scope: `render_bar_chart` now renders **all** datasets as grouped
bars, not only the first.

**The guard matters more than the renderers.** ROTE previously substituted an
unsupported type only when the client had negotiated a `supported_types` set.
A client that had not would have been sent a `gauge` it cannot draw.
`_degrade_089_for_non_web` now runs **before** per-component adaptation, for
every non-web profile, negotiated or not, and it recurses — a gauge nested in
a card is just as undrawable as one at the top level. The fallback ladder is
derived by probing the target's legacy vocabulary rather than reading a flag,
because the voice profile drops `progress` and a flag-based rung would have
handed it one.

Every ladder ends at text that still carries the numbers, so a watch shows
"Humidity: 62%" rather than an empty node.

`tests/rote/test_non_web_unchanged_089.py` requires byte-identical adapter
output for `windows`, `android`, `ios`, `macos`, `watch`, `tv` and `voice`
against the 089 base, except that the new types appear only as fallbacks. A
failure there is blocking, not a known divergence.

## 2. The a8p console layout (web only)

The web client takes the shape of the a8p console: a fixed left sidebar with
the brand, the agent directory, recent work and the profile widget, and a main
column whose canvas holds the landing and a feed of turns above a pinned
composer bar.

Every element id and data hook `client.js` and the tests address is carried
over. What changed is where each region sits.

**The canvas is no longer one flat live surface.** Each turn's result gets its
own response card in the feed, and the newest card's body **is** the live
workspace — so `canvas` keeps its name and every render, upsert, stream and
export-flag path works unchanged.

The 088 chrome cluster was one row in a full-width top bar; it now has the
homes the parity contract names: chat controls beside Recent work, canvas page
actions in the response card's header, account controls in the profile widget
with the gear at its right edge.

## 3. One font, and no colour literals

One self-hosted Open Sans latin variable slice serves every weight the
interface uses — body text, headings and code alike — replacing Inter and
JetBrains Mono. `--astral-font` is the token; `--font-sans` and `--font-mono`
alias it, so nothing can reintroduce a second family by using an old name.

Every colour outside the palette block is now a `--astral-*` token or a
`color-mix()` of one, and `tests/test_no_hex_literals_089.py` keeps it that
way. A literal is a colour a user's theme cannot change — it stays put while
everything around it moves, which is the kind of defect nobody notices until
they pick a light theme.

## 4. The chrome dialog

`render_modal_shell` takes the a8p structure: icon badge, title, subtitle,
close, an optional tab strip for surfaces that declare `SECTIONS`, a scrolling
body and a footer action row. A surface that declares nothing renders as
before, so every existing caller is unaffected.

## 5. Responsive

1024–1279 narrows the sidebar; below 1024 it becomes a focus-trapped drawer
with Esc and backdrop close and focus restored to the toggle; below 768 the
composer pins above the safe-area inset with its secondary controls behind one
overflow button.

---

## Verification

**Desktop parity against captures of a8p** (`tests/web_layout_parity/`):

| Viewport | Score | CSP violations |
|---|---|---|
| 1920×1080 | 98.5% | 0 |
| 1440×900 | 98.5% | 0 |
| 1280×800 | 96.5% | 0 |

Target is ≥ 90% at each. Colours are excluded from scoring by the contract,
because they come from `ThemeView`.

**Responsive checklist: 13/13** at 1024×768, 768×1024, 390×844, 320×640 and
1280×800 at 200% text.

**Full suite from a clean checkout: 2827 passed, 4 failures against 5 at the
baseline — zero new**, and one pre-existing failure (the portable export
document) repaired by making the export generator newline-agnostic.

ESLint via `tooling/web-ci` — clean, 0 warnings.

## What qualification found, and what was fixed

Five real defects, each fixed in `b10ff5c`:

1. the chrome cluster overflowed the sidebar at narrow widths, putting the
   settings gear on the page *behind* a closed drawer;
2. the empty live turn's expand chip counted as workspace content, so the
   workspace view appeared before the first answer existed;
3. icon-only controls below 1024 had a 44px height but no stated width;
4. component grids that arrived laid out for a wide canvas did not fold;
5. Plotly wrote a 10px tick font inline, below the legibility floor.

The harness itself was wrong about three things and was corrected **before**
any of the above was believed: an off-canvas drawer is not on screen; a row
scrolled inside a list is one scroll away rather than clipped; and columns are
counted as drawn, because `repeat(2, minmax(0px, 1fr))` is not three columns.
The candidate side of the response fixture is this client's **own** rendering
of the fixture's components — injecting the reference's markup scored a8p's
styling twice and said nothing about this client.

## Owner exceptions in force

- **E1: CI is ignored.** No workflow file is modified in any repository.
- **E2: web-only client changes.** `windows-client/`, `android-client/` and
  `apple-clients/` are untouched. Non-web clients receive the new types only as
  server-side ROTE fallbacks.
- **E3: desktop-only parity target.** The ≥90% score is required at the three
  desktop viewports; tablet and phone are held to the responsive checklist,
  because a8p has no mobile design to match.

## Known divergences (owner-accepted)

- **KD-1 / KD-2**: the native and Windows manifest drift guards assert the
  exact `component_types` set and therefore fail once the six types exist.
  They are deliberately **not edited** — they are client tests, and E2 puts
  client directories out of scope.
- **KD-3**: `tests/test_disposition_matrix_088.py` is updated for the new
  types' dispositions. Native profile rows are unchanged; the disposition data
  is part of the server-side contract, not client code.

## Note for the reviewer

`pyproject.toml`'s single runtime dependency moves to `astralprims==0.4.0`,
and the immutable-manifest digest in `tests/test_protocol.py` is re-recorded
for it. The rule that test protects — exactly one runtime dependency, pinned
exactly — is unchanged; only the version moved.
