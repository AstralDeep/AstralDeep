# Contract: New Primitives, Web Rendering, ROTE Adaptation

AstralPrimitives `0.3.0 → 0.4.0`. These are additive wire types; existing types are unchanged. Conventions follow `src/astralprims/primitives.py`:

- `type` is a `Literal` snake_case name;
- nested primitive lists are named `content` or `children`;
- list-of-dict items are documented in the docstring;
- `variant` strings carry semantic roles (default, success, warning, error, info);
- **no hex color fields**, because colors resolve from `ThemeView` roles.

## Definitions

### `action_group` — `ActionGroup`
| Field | Type | Default | Notes |
|---|---|---|---|
| `buttons` | `List[Button]` | `[]` | Nested primitives serialize recursively |
| `align` | `str` | `"start"` | start, center, end, between |
| `label` | `Optional[str]` | `None` | Accessible group label |

### `stat_group` — `StatGroup`
| Field | Type | Default | Notes |
|---|---|---|---|
| `title` | `Optional[str]` | `None` | |
| `items` | `List[Dict]` | `[]` | `{label: str, value: str, delta?: str, trend?: "up"\|"down"\|"flat", hint?: str, variant?: role}` |
| `columns` | `int` | `4` | Clamped 1–6 by renderer and ROTE |

### `gauge` — `Gauge`
| Field | Type | Default | Notes |
|---|---|---|---|
| `label` | `str` | `""` | |
| `value` | `float` | `0.0` | **0–1**, consistent with `progress` |
| `display_value` | `Optional[str]` | `None` | e.g. `"72 °F"`; falls back to a percentage |
| `thresholds` | `List[Dict]` | `[]` | `{at: float 0–1, variant: role}`, ascending |
| `subtitle` | `Optional[str]` | `None` | |

### `pipeline_stepper` — `PipelineStepper`
| Field | Type | Default | Notes |
|---|---|---|---|
| `title` | `Optional[str]` | `None` | |
| `steps` | `List[Dict]` | `[]` | `{label: str, status: "done"\|"active"\|"pending"\|"error", detail?: str}` |
| `orientation` | `str` | `"horizontal"` | horizontal, vertical |

### `donut_chart` — `DonutChart`
| Field | Type | Default | Notes |
|---|---|---|---|
| `title` | `str` | `""` | |
| `labels` | `List[str]` | `[]` | |
| `data` | `List[float]` | `[]` | Same length as `labels` |
| `center_label` | `Optional[str]` | `None` | |
| `center_value` | `Optional[str]` | `None` | |

### `radar_chart` — `RadarChart`
| Field | Type | Default | Notes |
|---|---|---|---|
| `title` | `str` | `""` | |
| `axes` | `List[str]` | `[]` | 3–12 axes |
| `datasets` | `List[Dict]` | `[]` | `{label: str, data: List[float]}`, same shape as the bar/line datasets |
| `max_value` | `Optional[float]` | `None` | Defaults to the data max |

Each class is exported from `astralprims/__init__.py`, registered for `Primitive.from_dict`, documented in `README.md` (Constitution VI) and covered in `tests/test_primitives.py` (round-trip, defaults, validation). The contract digest (`contract_sha256`) is regenerated.

## Web rendering (`P:backend/webrender/renderer.py`)

- One `render_<type>` function per type, registered in `PRIMITIVE_RENDERERS`, which also updates `allowed_primitive_types()`.
- **All** text passes through `esc()`. Numeric values are coerced with `float()` and clamped. No field value is emitted into `style`, `class`, `on*` or `href`. Attributes go through the existing allowlist.
- Charts are inline SVG using `currentColor` and CSS classes bound to theme variables (`--astral-primary`, `--astral-accent`, state roles). Series colors cycle through a fixed CSS class list (`.astral-series-1…6`) defined in `astral.css` from theme roles.
- `action_group` buttons render through the existing `render_button`, so actions dispatch through the existing delegated handler in `client.js` and no new event path is added.
- `pipeline_stepper` and `stat_group` use semantic lists (`<ol>`, `<dl>`) with `aria-current="step"` on the active step.
- `gauge` and `donut_chart` expose `role="img"` with an `aria-label` summarizing their values. `radar_chart` also emits a visually hidden data table.
- **Also in scope**: `render_bar_chart` renders all datasets (grouped bars), not only the first.

## Protocol manifest (`P:contracts/ui_protocol.json`)

- `component_types` adds `action_group`, `donut_chart`, `gauge`, `pipeline_stepper`, `radar_chart`, `stat_group` in sorted order.
- The server-side disposition data gains rows for the six types:
  - `web` → `supported`;
  - `windows`, `android`, `ios`, `macos`, `watch` → `omitted_by_server`, with `fallback` naming the ladder head.
- `D:backend/tests/test_ui_protocol_manifest.py` and `P:tests/test_disposition_matrix_088.py` are updated. The manifest's sha256 in `config/astral-composition.json` → `compatibility.ui_protocol.sha256` is updated on repin.
- **Not modified** (web-only directive): `android-client/**/VocabularyParityTest.kt`, `apple-clients/**/ManifestDriftTests.swift`, `apple-clients/**/Dispositions.swift`, `windows-client/tests/test_renderer.py`. Their local failures against the new list are recorded in `verification.md` as the accepted known divergence.

## ROTE (`P:backend/rote/`)

### Fallback ladder (`fallback.py`)
| Type | Ladder |
|---|---|
| `action_group` | `container`, `text` |
| `stat_group` | `grid`, `keyvalue`, `table`, `text` |
| `gauge` | `progress`, `metric`, `text` |
| `pipeline_stepper` | `timeline`, `list`, `text` |
| `donut_chart` | `pie_chart`, `table`, `list`, `text` |
| `radar_chart` | `table`, `list`, `text` |

Each substitution maps fields explicitly so no data is dropped silently:

- `gauge→progress`: value and label;
- `stat_group→grid`: each item becomes a `metric`;
- `pipeline_stepper→timeline`: `status→variant` (done→success, active→info, pending→default, error→error);
- `donut_chart→pie_chart`: identical data;
- `radar_chart→table`: headers are `["", …axes]` with one row per dataset.

### Web-profile adaptation (`adapter.py`, `capabilities.py`)
These rules apply only when `profile.device_type ∈ {BROWSER, TABLET, MOBILE}`:

| Type | Rule |
|---|---|
| `stat_group` | `columns = min(columns, max_grid_columns)` |
| `gauge` | `viewport_width < 480` → compact variant attribute |
| `donut_chart`, `radar_chart` | `0 < viewport_width < 700` → table form (same rule as `_adapt_chart`) |
| `pipeline_stepper` | `viewport_width < 768` → `orientation="vertical"` |
| `action_group` | `MOBILE` → wrap; >3 buttons → first 2 + overflow menu button |

**Guardrail tests**: For a fixture corpus that includes all 41 component types, adapter output for `WINDOWS`, `ANDROID`, `IOS`, `MACOS`, `WATCH`, `TV` and `VOICE` profiles is byte-identical before and after 089, except that the six new types appear only as their ladder fallbacks.

## Style-driven layout (`D:backend/orchestrator/typesafe_routing/layout.py`)

`compose(style, components) -> dict | None` returns a layout in the existing `ui_designer` layout format (ref nodes over delivered component IDs). It is validated by the designer's existing validator and lint before sending, and returns `None` for `as_delivered`, for fewer than 2 components, or for any unmapped result.

| Class | Types |
|---|---|
| headline | `hero`, `alert` |
| kpi | `metric`, `stat_group`, `gauge`, `badge`, `rating` |
| chart | `bar_chart`, `line_chart`, `pie_chart`, `donut_chart`, `radar_chart`, `plotly_chart` |
| record | `table`, `keyvalue`, `list`, `timeline`, `pipeline_stepper` |
| prose | `text`, `card`, `code`, `collapsible`, `tabs`, everything else |

| Style | Order and arrangement |
|---|---|
| dashboard | headline → kpi (grid, columns = min(4, n)) → chart (grid 2) → record (full) → prose |
| detailed_table | headline → record (full) → kpi (grid) → chart (collapsible "Charts") → prose |
| alert_focused | alert/badge first → headline → everything else inside collapsible "Details" |
| conversational | prose → headline → others stacked full width |

Within a class, the delivered order is preserved. Every delivered component appears exactly once, and no component is created or altered (FR-029).
