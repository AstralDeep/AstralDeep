# macOS workbench qualification

## Current state

Implementation is local on `codex/099-macos-agent-workbench` in separate Deep and Projection worktrees. These observations are diagnostic, not a release authorization. No product branch has been pushed, no Projection revision has been adopted, no candidate has been installed, and production has not been changed.

The six-harness research and reviewed design are recorded in `research.md` and local design commit `4e934c273cac0bba72aec5cb03313132ec37a4c8`. Projection implementation is locally committed at `ec8f15c428633ccbffa2120f34d62d84461cda6e`; it has not passed provider CI or been adopted by Deep. Independent native/backend reviews report no remaining concrete Important/Critical blocker in their reviewed scopes. Broader gates and physical acceptance remain required.

## Local checks

| Check | Observation | Evidence |
|---|---|---|
| Clean baseline AstralCore | 325 passed | Initial local baseline; Xcode 27.1 |
| Current AstralCore with coverage | 356 passed | `/tmp/astral099-core-coverage.log`; 93.29% overall executable-line coverage |
| Native host injected-driver tests | 12 passed | `/tmp/astral099-host-security-app-tests.log`; includes fresh HID proof and queued injected Resume denial |
| Full macOS app unit suite with coverage | 450 cases, one existing iOS-only skip, zero failures | `/tmp/astral099-root-mac-final.log` and `/tmp/astral099-root-mac-final.xcresult`; product target line coverage 63.39%, including OS adapters that still require live qualification |
| Optimized DirectDownload build | Succeeded | `/tmp/astral099-host-direct-build.log`; unsigned compile verification, not an installed host |
| Native workbench/canvas tests | 34 passed | `/tmp/astral-workbench-ui-fallback-final.log`; includes actual ROTE refresh and retained chart/composer state |
| iOS Simulator compatibility build | Succeeded after guarding Mac-only filesystem implementation | `/tmp/astral099-ios-build2.log`; final source rerun pending |
| Deep selected backend tests | 992 passed; 95% changed Python line coverage, 514 measured lines, 25 uncovered | `/tmp/astral099-backend-qualified.log` and `.xml`; all seven changed production files at least 92.5% |
| Root's independent final backend check | 182 passed | `/tmp/astral099-root-backend-final.log`; root also verified all 15 evidence-manifest source hashes |
| Independent backend review closure | 58 focused tests passed; standalone in-flight cancellation repro now zero effects | Read-only review of controller/current-task/physical-lock/cancel/host/transport seams |
| Plain full runtime suite | 47 passed | `/tmp/astral099-current-runtime.log`; final ordinary runtime, no test changes |
| Coverage-instrumented registry stress | Stalls on unchanged baseline as well | `/tmp/astral099-baseline-runtime.log`; matching pristine `3627625` source, not counted as a pass |
| Projection Python suite on host | 4,492 passed, 7 skipped, two failures | `/tmp/astral099-projection-python.log`; new build-number guard updated below; fresh-wheel dependency issue reproduced on unchanged main |
| Projection Python suite in isolated Linux CI image | 4,492 passed, 6 skipped; four Git-metadata mount failures | `/tmp/astral099-projection-ci.log`; no test skip or assertion change used to address mount failures |
| Corrected Git mount and fresh-wheel checks | 12 passed | `/tmp/astral099-projection-ci-focused.log`; includes all four failed checks, wheel-only example and all seven identity tests |
| Final complete Projection Python suite | 4,496 passed, 6 existing skips, 33 subtests passed | `/tmp/astral099-projection-ci-final.log`; isolated Linux image, network disabled, correct read-only Git metadata mount |
| Final Projection contract/architecture/identity checks | 36 passed, one existing immutable-source replay skip | `/tmp/astral099-projection-final-contracts.log`; rerun after final provenance/docs/import-order edits |
| Projection changed Python coverage | 8/8 executable lines, 100% | `/tmp/astral099-projection-changed-coverage.json`; exact `cd37c1c5..ec8f15c4` range |
| Projection Python lint | Passed | `/tmp/astral099-projection-ruff.log` |
| Strict Swift format with tracked configuration | Tool could not read configuration | Local Xcode 27.1 requires `orderedImports.shouldGroupImports`; canonical CI remains Xcode 26.6/build 17F113. Temporary compatible-config results must be labeled separately |
| Recursive Swift format with temporary compatible configuration | Passed | `/tmp/astral099-swift-compatible-lint-final2.log`; canonical configuration unchanged; regular/testable import ordering restored in three task test files |

The Linux test image is `sha256:9ac423f7df9788ad324ee56dc23a05ccf028ae514f7f462961fb038a4fd4e00f`, with Python 3.11.17 and the declared pytest 9.1.1, pytest-cov 7.0.0, astralprims 0.4.0 and ruff 0.15.21. It ran with network disabled. Required CI remains authoritative.

The existing release-identity guard now includes all seven DirectDownload versioned configurations while retaining the exact original/Watch configuration inventory, registered bundle identity and monotonic version assertions. An additional check verifies Mac-only targeting, Developer ID signing, hardened runtime, no sandbox, minimal entitlements and the explicit scheme configuration. This repairs the expected inventory without weakening identity checks.

The host fresh-wheel test fails on unchanged Projection main too: its nested `--system-site-packages` environment cannot see astralprims installed only in the parent virtualenv. The isolated Linux global-dependency environment passes that test. No product or test change was made for this host setup difference.

Backend coverage XML SHA-256 is `426659c9ddf4474f770ffbec3b39f43b28075dfb27f022dfdf0788df4364e380`. Exact commands, private-environment setup references, source hashes and declared unchanged component pins are retained outside the candidate in `/tmp/astral099-backend-evidence.json`. Credentials are never retained in feature evidence.

## Required remaining acceptance

- Freeze, personally review and commit exact component/backend bytes; refresh provenance and rerun affected checks with retained coverage reports.
- Complete required Projection CI before Deep adopts its exact commit and UI-protocol digest. Product push/PR permission must follow the repository's standing external-mutation rule.
- Build and verify the local signed DirectDownload candidate, preserve the existing app recoverably, install it, and have the owner complete macOS TCC and any required Keycloak/provider sign-in. Never bypass these controls.
- Run an isolated commit-derived server candidate with real configured IAM, PostgreSQL, affected workers and LETS, representative migrated data, and actual Mac/web controller flows. Empty-database or mock-only tests do not qualify staging.
- Through ordinary authenticated dispatch, verify Chrome navigation/form typing/select/scroll/click against the synthetic fixture's independent state oracle; a native app edit; selected-root file round trip; and a reviewed visible-Terminal command with a physical postcondition.
- Verify missing approval/TCC, outside-root/symlink, secure-field/Terminal, stale target, task/chat/owner/controller replacement, Pause/Stop, lock/disconnect and no-replay denials. Unknown delivered outcomes must not be called failures with zero effects or retried blindly.
- Exercise wide/compact Mac keyboard navigation, local controls, generated canvas interaction and approval/Stop behavior. Verify every affected client surface; preserve undeclared legacy layouts.
- Promote only the qualified immutable server/client pair, then repeat production health and real workflow checks and update the curated vault checkpoint.

## Production baseline and rollback

Read-only inspection found `/opt/AstralDeep` clean on main at `ca822e68d5477fb60fa9076107a4dab4138eeda6`. The healthy running server uses `ghcr.io/astraldeep/astraldeep:sha-ca822e68d5477fb60fa9076107a4dab4138eeda6@sha256:802244a5eb1a715dff4fa587a0e7d447b9eb425f1b674fc48cf0e685ced18aa2`. Computer use is currently off. Keep this exact image and effective configuration available for rollback.

The candidate adds no database migration or new primitive. It changes optional negotiated Mac geometry, native host dispositions, existing-v1 optional Mac input arguments and server authorization/continuation behavior. Enabling `FF_COMPUTER_USE` requires recreating the service with the verified candidate image and effective environment; restart alone does not apply source or Compose environment changes. Roll back the image and computer-use flag if readiness, ordinary chat, authorization, Stop or any required physical acceptance fails. Preserve durable data and unrelated services.

The installed baseline is the sandboxed Mac App Store app 1.7/build 66. Retain its complete bundle outside `/Applications` before replacing it; do not overlay old resources or remove Keychain/container data. A locally signed candidate is distinct from notarized distribution or a public/store release.
