# Native actions and Apple evidence checkpoint — 2026-09-12

This is a local implementation and diagnostic checkpoint. It does not complete
088, qualify final authenticated staging, authorize a product push, promote a
draft PR, or publish an app. Earlier implementation and live observations remain
in `native-checkpoint-20260911.md`; their original boundaries are preserved.

## Source and behavior

Projection is pinned to `5074bdd1877c7d2a26bf45eafe4b839472d9ad21`.
Implementation commit `ca3db3567391a5da5040e0dec9b1e04fd4cbe8a5` and that
upstream merge have the identical tree
`a2b62b434173089b724666a2d17495c98ed5d5af`. Merging the already-incorporated
Projection main history changed no source bytes and makes the current default
branch an ancestor for canonical PR coverage. Pre-merge diagnostics are retained.

Android's actual workspace controller now has two defaulted test seams for its
existing REST client and lifecycle-bound activity-result registry. Normal app
construction, authority checks and release runtime dependencies are unchanged.
Device scenarios exercise actual share, GET authorization, native capture,
presentation POST, private WebView export, document saving, cancellation,
partial-file cleanup, lifecycle destruction and stale owner/revision completion.
The already-pinned MockWebServer dependency is isolated to Android tests.

Apple's debug-only UI fixture uses a dual-gated, ephemeral loopback peer and an
in-memory session before constructing the ordinary model; Release keeps normal
construction. Actual toolbar tests cover share/export, duplicate prevention,
denials, retry, stale completion and system sharing. Rich-result, saved-chat,
slash-command, background-send and refinement journeys use the existing UI.

The Refine cancellation journey exposed a sustained iPhone render loop with the
keyboard open. The retained native sample shows repeated SwiftUI lazy-layout
work at sustained full CPU. `RefineSheet` now owns and clears instruction focus
before dismissal and before submission updates the model. The same cancellation
journey passes, preserves the selected pane and composer draft, and rejects empty
or whitespace instructions. No dispatcher, authorization, target or timer bypass
was added.

Plane remains `daedeed4690282da67cd0a1764668b8dc84c801f`, schema `088.001`.
The UI protocol digest remains
`e5f31514c04cde55cdf6567e04ccc1f95e8ee57e06f7f761bff3c8cb75b84707`.
This increment adds no primitive, schema revision, feature flag or third-party
product runtime dependency. Windows redesign remains excluded.

## Observed verification

These are overlapping diagnostic cohorts, not an aggregate release assertion.
The containing Deep SHA, integrated tooling results and subsequent archive/wheel
receipts are recorded outside the candidate tree to avoid self-referential evidence.

| Frozen-source check | Result |
| --- | --- |
| Projection Python | 1,539 passed; changed executable coverage 836/872 (95.87%). |
| Android Core / app JVM / device | 118 / 336 / 73 passed, no skips or failures. Normal lint, build and coverage gates passed. |
| Android native changed coverage | App 1,998/2,199 (90.86%); Core 64/64 (100%). All 227 collector source inputs matched; no class-ID mismatch. |
| Actual iOS Core / app / UI | 213 / 198 / 22 passed, no skips or failures. The final UI run repeats the previously hanging Refine journey. |
| iOS changed Swift | 2,440/2,692 (90.64%); narrower export comparison 985/1,049 (93.90%). Actual iOS observations only. |
| Apple artifact/observation helper | 218 tests passed; 455/456 executable lines covered. Actual retained Core/unit/UI metadata parsed; missing staging refused. |
| Protected policy integration | 90 validator and 82 workflow tests passed, three existing workflow skips; changed validator lines 10/10 covered. |

Maintained-language lint, staged secret scanning, provenance replay and diff
checks passed on their frozen inputs. Projection records 182 transformations,
including 16 removals, and 337 unchanged original imports. Dependency-lock
changes affect AndroidTest configurations only; no coordinate or version was added.

## Apple evidence boundaries

The producer builds with coverage enabled before testing and retains the exact
shipping app, complete Products trees, test-host metadata and committed source
closure. The helper rejects dirty source claims, invalid archive topology,
incorrect app/test-host selection and changed binary/resource bytes. Core runs
on iOS; app unit, UI and mandatory authenticated staging runs use the prepared app.

The separate protected normalizer executes only pinned policy. It independently
checks all four iOS raw lanes, distinct bundle roots and contents, actual expected
targets/suites, successful case counts and absence of failures/skips. Copied UI
results cannot substitute for unit or staging observations. The final validator
requires the same four canonical raw roots; Mac and Watch retain their existing
single-root contracts. Normalization helpers and the composition schema are now
included in the protected policy digest inventory.

These checks do not prove raw-profile-to-executable UUID linkage. The observed
Xcode artifacts do not expose a usable matching profile binary identity; that
limitation remains explicit. Synthetic UI coverage is not authenticated staging.
No protected provider run, signing operation or publication was performed.

## Artifacts and remaining work

The retained normal Android bundle is
`build/088/handoff/astraldeep-088-controller-UNSIGNED.aab` in the main Deep checkout:
35,643,826 bytes, package `com.personalailabs.astraldeep`, version 1.4/code 7,
SHA-256 `9c80f8ca52b96b2a10e1fa964aff8513c3813717a2076498a504754bf2f5c2dd`.
It is unsigned and not upload-ready. Its DEX class definitions contain neither
JaCoCo nor MockWebServer runtime classes, and no JaCoCo probes were found.
Ordinary OkHttp logging labels are not dependency-class evidence.

Unsigned Apple Release archive build receipts are retained separately. Building
an archive does not establish distribution signing, signing-only export or store
submission. The upload key, intended Play listing/version access, Apple profiles,
App Store Connect access and separately protected publication setup remain open.

The signed-in live backend and user clients were preserved on their earlier
recorded candidates. Final exact-candidate authenticated client/staging checks,
Mac app test-runner qualification, Watch interaction and full native publication
lineage remain open. The temporary test-only Android command link and isolated
test emulator were cleaned up; the user's emulator was untouched.

PRs 195/15/8 remain drafts at their earlier remote heads. No bootstrap approval
or protected release decision exists. Once all required work is qualified,
Plane and Projection must be available before Deep's exact component pins are
merged. Full 088 execution, guidance, monitoring/scheduler and framework work
remains tracked in `tasks.md`; this checkpoint does not check those tasks off.
