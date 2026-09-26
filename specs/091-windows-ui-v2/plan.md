# Implementation Plan: Windows UI v2 parity

**Branch**: `codex/091-windows-ui-v2` | **Date**: 2026-09-25 | **Spec**: [spec.md](spec.md)

## Summary

Consume shared console/v2 and ROTE checkpoint Deep 4e2eae70 / Projection 1d4a864 in Windows. Keep server-owned catalog/actions and one live result canvas. Implement native shell, responsive settings, truthful device aggregation, six existing primitive renderers, and coverage/live evidence.

## Technical Context

- Python 3.11-compatible source, PySide6, existing FastAPI/ROTE backend and Windows requirements; pytest/coverage/ruff tooling; bundled shared Open Sans asset. No web wrapper.
- Existing server conversations/settings; no migration or new credential storage.
- Windows offscreen suite, shared ROTE/chrome, affected Deep chrome/surface/protocol, browser regression and relevant CI gates; changed-line coverage >=90%.
- Logical widths 320–1440 and 100/125/150/200% scaling. Bounded parsing, one renderer across presentation transitions, debounced viewport updates and no UI-thread network calls.
- Windows owns its client; 090 Apple/watchOS/Android sources/artifacts remain unchanged. Shared edits limited to Windows qualification plus regression tests and required additive manifest inventory.

## Constitution Check

Pre/post design passes: server definitions and ROTE retained; existing primitives only; Keycloak/owner/permission/confirmation paths reused; Python 3.11 and source-header convention; no migrations. Theme uses shared roles and font asset. Registration advertises implemented types and actual capabilities. Coverage, lint, relevant complete suites, image/boot/secret checks and real authenticated backend/worker walkthrough remain completion requirements. Local evidence is diagnostic. The owner's later instruction authorizes task commits and branch pushes; no merge, release, signing or bootstrap exception is authorized. Missing hardware/sign-in/service evidence remains incomplete, never waived.

## Project Structure

- `components/AstralProjection/windows-client/astral_client/app.py`: native shell/feed integration and authenticated actions.
- `astral_client/console.py`: bounded console/presentation decoding; native widgets consume server data.
- `astral_client/protocol.py`, `protocol_manifest.py`: actual capability aggregation and frame handling.
- `astral_client/renderer.py`, `composites.py`: six native primitives and export/state behavior.
- `astral_client/theme.py`, `AstralDeep.spec`: shared typography and packaging.
- `windows-client/tests/`: behavior, malformed/denial, resize/focus/state/export regressions.
- Deep `backend/orchestrator/native_console.py`, Projection `backend/rote/capabilities.py`: Windows qualification only with negotiation and exact supported types.
- `specs/091-windows-ui-v2/verification.md`: exact checks/gaps; vault screenshots and curated checkpoints.

## Execution

1. Ingest exact 090 checkpoint, validate models and establish baseline tests.
2. Parallel bounded work: shell integration; native primitives; capability/decoder/shared qualification, with exclusive app.py ownership.
3. Integrate settings, selection and state-preserving full-screen behavior and security regressions.
4. Run coverage/relevant CI; inspect live web and Windows with real dependencies; retain privacy-safe evidence.
5. Review and commit task changes, push the separate Windows branches under the owner's later authorization, and commit/push curated vault updates with explicit gaps.

## Complexity Tracking

No constitution exception. Published 090 is partial; inherited failures are baselined rather than called passing Windows qualification.
