Written for: the reviewer of the AstralPrimitives pull request.

# Six composite readout primitives (astralprims 0.4.0)

**Merge order: this PR is first.** Plane, Projection and Deep all pin
`astralprims==0.4.0`, so nothing else in feature 089 can merge before it.

## What this adds

Six component types, additive, in `src/astralprims/primitives.py`:

| Type | What it is for |
|---|---|
| `action_group` | A labelled row of related buttons |
| `stat_group` | A grid of small KPI readouts |
| `gauge` | One bounded 0–1 reading as a dial |
| `pipeline_stepper` | An ordered sequence with one current stage |
| `donut_chart` | A single-series ring with a centre readout |
| `radar_chart` | A multi-axis comparison |

Each is exported from `__init__.py`, registered for `Primitive.from_dict`
through the existing `__pydantic_init_subclass__` auto-registration,
documented in `README.md` (Constitution VI) and covered in
`tests/test_primitives.py` for round-trip, defaults and validation.

The package version moves 0.3.0 → **0.4.0** and the contract digest is
regenerated.

## What this does not do

**No type carries a colour.** Not a field, not a default, not a hint. Colour
comes from `ThemeView` at render time, so an agent cannot pick one and a theme
change restyles every one of these without touching a definition.

No existing type changes. A client that has never heard of these six sees
exactly what it saw before — the server substitutes a fallback before the
component leaves (see the AstralProjection PR).

## Verification

- `uv run --group ci python -m pytest -q` — 69 passed
- `ruff check` — clean

## Owner exceptions in force

- **E1: CI is ignored.** Every CI-equivalent check was run locally and recorded
  in `specs/089-typesafe-a8p-integration/verification.md`. No workflow file is
  modified in any repository (enforced by `scripts/verification/check_089_scope.py`).
- **E5: dependency pin without an approval gate.** Not exercised here; this
  package adds no dependency.

## Known divergences

None in this repository. The manifest drift guards that fail once these six
types exist live in AstralProjection and are described in that PR.
