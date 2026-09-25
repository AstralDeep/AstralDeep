# Tasks — Windows UI v2

## Setup and foundation

- [x] T001 Refresh/clean repositories with owner permission, inventory remote spec trees and reserve separate Windows ownership in `specs/091-windows-ui-v2/spec.md` (FR-011).
- [x] T002 Integrate published 090 checkpoint without modifying its artifacts; record exact contract in `specs/091-windows-ui-v2/contracts/windows-console.md` (FR-008).
- [x] T003 Establish Python 3.11 test environment and baseline Windows/affected shared tests; record commands in `specs/091-windows-ui-v2/verification.md` (FR-012).
- [x] T004 [P] Test/implement bounded console and presentation decoding in `components/AstralProjection/windows-client/astral_client/console.py` and `tests/test_console.py` (FR-007, FR-008).
- [x] T005 Test/implement actual capability aggregation and opt-in in `astral_client/protocol.py`, `protocol_manifest.py`, Deep `backend/orchestrator/native_console.py` and Projection `backend/rote/capabilities.py` (FR-007, FR-008).

## US1 — Navigate/start work

Independent test: dashboard/search/history/scenarios at all seven logical dimensions; Run dispatches once, Load only edits draft.

- [x] T006 [US1] Test shell navigation/scenario behavior in `windows-client/tests/test_console_shell.py`, including malformed/empty catalogs and keyboard/drawer dismissal (FR-001, FR-002).
- [x] T007 [US1] Implement shared-model sidebar/drawer, History, search/account controls, landing categories/scenarios in `astral_client/console_widgets.py` and `app.py` (FR-001, FR-002).

## US2 — Converse/results

Independent test: real synthetic request; attachment/selection/voice, collapse/expand/fullscreen and export/share retain authority/state.

- [x] T008 [US2] Test conversation/result/selection transitions and owner resets in `windows-client/tests/test_console_shell.py` (FR-003, FR-004, FR-009).
- [x] T009 [US2] Implement bottom composer, More/Advanced, correlated selection and ordered feed with one live result canvas in `astral_client/app.py` (FR-003, FR-004, FR-009).
- [ ] T010 [US2] Verify voice controls/errors and absence of passive web banner in `windows-client/tests/`, then real worker capture/playback/recovery in `verification.md` (FR-010).

## US3 — Presentation/settings/primitives

Independent test: six primitives/actions including invalid data; settings/themes at reference sizes and 100/125/150/200% scaling.

- [x] T011 [P] [US3] Test and implement six existing primitive renderers in `astral_client/renderer.py`, `composites.py` and `tests/test_console_primitives.py`, including export, actions, zero/non-finite data and accessible feedback (FR-006).
- [x] T012 [US3] Implement responsive shared settings/surfaces, theme/typography and packaging in `astral_client/app.py`, `theme.py`, `AstralDeep.spec` with tests (FR-005).
- [ ] T013 [US3] Test logical resizing, keyboard/focus, input aggregation and DPI scaling; record source-bound screenshots in vault `assets/astral-native-ui-v2/windows/` (FR-005, FR-007, FR-012).

## Qualification and checkpoints

- [ ] T014 Run full Windows offscreen suite, affected Deep/ROTE/web suites, >=90% changed-line coverage and relevant CI gates; resolve failures without weakening tests; record exact commands in `verification.md` (FR-012).
- [ ] T015 Inspect live authenticated web success/fullscreen and test Windows with matching backend/worker across acceptance matrix; record gaps in `verification.md` (FR-001–FR-010).
- [ ] T016 Review security/state/capability seams; commit/push authorized Windows branches and update/push curated vault pages, screenshot manifest, index and log (FR-009, FR-011, FR-012).

## Dependencies and strategy

September 25 qualification checkpoint: implementation checkmarks describe source plus scoped regression coverage only. T009 includes production-styled narrow active-voice geometry, wrapped feedback, minimum targets and preserved focus. The complete current Windows suite passes1537tests with10skips and2252/2298changed lines (98.00%). The rebuilt executable passes six packaged checks, actual offline GUI/retry and unsigned-helper refusal; Linux affected backend152passes. T010 and T013–T016 retain real worker/display, shared failing/incomplete CI and final publication/acceptance obligations. Exact current results,32offscreen scaling captures and remaining gaps are in `verification.md`. Shared090 corrections beyond published Deep4e2eae70/Projection1d4a864 remain unavailable; Mac has requested the isolated Windows-owned identity fix through the vault.

T001–T003 establish baseline. T004 and T011 run independently; T005 follows decoder/renderers before qualification. T006–T009 share app.py ownership and proceed sequentially. Settings can follow shell. Final gates depend on integration. US1 is the first usable increment; all three stories remain required. Parallel example: primitive renderer work versus console decoding versus shell preparation, with separate files. The owner now authorizes task commits and branch pushes; no090artifact edits, merge or release is authorized.
