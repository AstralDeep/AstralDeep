# Feature 088 native checkpoint — 2026-09-11

Status: local implementation and diagnostic verification. No full feature,
authenticated staging, store release, or ready-for-review claim.

## Scope and source

The owner requested Android and Apple parity with the implemented 088 web
experience, excluded Windows redesign, authorized qualified store publication or
a manual-upload bundle, and reserved merging the existing PRs. The prior remote
baseline is Deep `3c4670dce022b1e2415ea2727526bcb4d1dbafcc` and Projection
`416ce6ce97b0af4cf812ffc8c53b9e2718b2aa70`. Plane remains pinned at
`8924bd4ba154a00184218c5b50c3de3bab9d13c4`, schema `079.001`; no new schema,
primitive, external authentication scheme, or runtime dependency is introduced.

Projection owns the native workspace, server-welcome fixture and presentation
contract, shared licensed Inter/JetBrains fonts, and isolated offline Plotly
renderer. Android/iOS/macOS keep one ephemeral owner-bound composer across
responsive layouts, retire welcome on real work, preserve same-owner reconnect,
and clear drafts on logout, owner change, and New chat. Background submission
uses the ordinary authenticated message path and one-shot `async_mode` state.
Native actions still use existing server chrome and effect gates. Watch preserves
its declared hardware/capability adaptations and explicit handoffs.

Deep's `projection_surfaces/audit.py` and `agents.py` now provide native snapshots
from the same owner-scoped reads and permission resolver as their web surfaces.
Audit date filters, keyset navigation, grouped events, and detail return state
are preserved. Agent personal permissions/credentials are distinct from
owner-only visibility and owner/admin trust controls. Existing command handlers
retain authority. Download fixes constrain credential origins, redirects,
temporary storage, and stale owner/session completion.

## Verification completed locally

These counts describe separate, sometimes overlapping cohorts; do not sum them
into a full-release assertion. Exact logs are retained in ignored `build/088/`
directories and platform result bundles, not published canonical evidence.

| Command/cohort | Result |
| --- | --- |
| Projection `PYTHONPATH=src:backend <Deep-venv>/bin/python -m pytest tests/test_native_chart_template.py tests/chrome tests/webrender tests/rote -q` with branch coverage | 972 passed. Changed executable lines in existing shared adapter/admin/agents modules: 71/71 covered. New template generator: 36/37 lines, 97.3%; branches 90%. |
| Projection `node tooling/web-ci/node_modules/@playwright/test/cli.js test tooling/web-ci/tests/native-chart-088.spec.js --workers=2 --reporter=list` | 9 passed in real Chromium: canonical datasets/categories, mixed traces, hover, zoom, narrow/wide bounds, empty/error states, inert malicious HTML and blocked remote images. |
| Projection `python3 scripts/build_native_chart.py --check` | Passed; exact web options, Plotly script hashes, and approved font bytes match generated offline template. |
| Projection `PYTHONPATH=src:backend <Deep-venv>/bin/python -m pytest tests/test_resources.py -q` | 38 passed, including wheel build/install and installed resources. Initial attempt lacked the declared `build` tool; installing the existing hash-locked build-tool subset resolved that environment failure. |
| Deep focused existing/native Agents and Audit suites | 147 passed. Native Audit changed executable coverage 49/49; Agents 63/63. |
| Deep surrounding chrome/runtime/protocol cohort with exact pinned Plane on `PYTHONPATH` | 668 passed, 15 PostgreSQL skips. Dedicated database rerun against isolated PostgreSQL: 16 passed, no skips. Initial 81 setup errors were stale installed Plane, resolved by using the exact pinned source. |
| Deep composition/schema/migration/ownership/primitive tests | 303 passed, no deselections. This run verified the prior baseline pins; final-pin verification is recorded separately. |
| Deep documentation links | 25 files passed. |
| Full Projection `ASTRALDEEP_SOURCE_REPO=<Deep> PYTHONPATH=src:backend <Deep-venv>/bin/python -m pytest tests -q` | 1,267 passed, including immutable extraction replay and updated transformation ledger. |
| Full Projection and Deep Ruff; new browser-test ESLint | Passed. |
| Changed Python coverage across shared adapter/admin/agents/template generator | 107/108 executable lines covered, 99%; diagnostic cohort, not canonical all-repository coverage. |
| Diff whitespace | Passed excluding the verbatim upstream JetBrains Mono OFL notice, which retains one official trailing space. |

## Native build checkpoint

Projection is committed locally as
`159588a6a8b407faa0dad64c7a2ea85c3f54ddae`. Deep's gitlink and composition pin
resolve that exact source; the UI protocol digest is
`160017483ed97b08b8dc0b33ec4d8a4de02fa41997506570c9bf8723d1f05801`.
`scripts/verify_composition.py --json` passes with no diagnostics and manifest
digest `33742e49d5447eb733c8ec309cfd6ae508555818cbdabbdcf9318b7a2d5aa1bc`.
The final-pin composition/schema/ownership/provenance/primitive cohort passed
302 tests and exposed one expected stale exact-pin assertion. Updating that
assertion to the verified new commit and protocol digest passed its targeted
rerun (one test). Full Deep Ruff and the active feature's 13 Markdown link
checks also pass after that correction.

| Final native check | Result |
| --- | --- |
| Android `ktlintCheck :app:lintDebug :core:test :app:testDebugUnitTest :core:koverVerify :app:koverVerify :app:assembleDebug :app:assembleDebugAndroidTest :app:bundleRelease` | Passed; 113 core and 268 app unit tests. Source lint covers app Kotlin. |
| Android phone and tablet instrumentation | Four tests passed on each form factor. Final screenshot synchronization edit also passed source lint and test APK build. Actual offline charts, table overflow, and start/work geometry were visually inspected. |
| Apple `swift test --package-path apple-clients/AstralCore` | 186 passed. |
| iPhone Xcode unit/UI suites | 152 unit and one UI test passed. |
| iPad Pro 13-inch Xcode UI suite | One UI test passed. |
| watchOS Xcode suite | 48 passed; one test skipped for absent live staging inputs. |
| macOS final unsigned build | Passed. Final XCTest attempts stalled before tests started while awaiting the IDE session. An earlier 154-test unit pass predates later changes and does not qualify final source. |
| macOS manual fixture verification | Start, More, multiline submission, split work layout, draft-preserving rail restoration, and New chat reset passed. |

Exact platform logs, result bundle paths, screenshots, and limitations are in
Projection's ignored `build/088/android/README.md`,
`build/088/android/android-release-metadata.json`, and
`build/088/apple/apple-088-local-evidence.json`. Evidence uses synthetic fixtures
in the real native apps/emulator/simulators, not authenticated staging. Android
fixture screenshots omit the surrounding product chrome. Pixel equivalence of
every platform widget and real authenticated dispatch remain unverified.
Watch still explicitly hands off forms, effect approval, chrome editing, chart
interaction, and downloads to phone/desktop. Android external public HTTPS
downloads use the system browser without app credentials. These are remaining
limits on the owner's requested exact cross-client behavior.

The fresh Android artifact is
`components/AstralProjection/android-client/app/build/outputs/bundle/release/app-release.aab`
in a build of the pinned component; the actual local build is in the sibling
Projection checkout's `android-client/app/build/outputs/bundle/release/`.
It is **unsigned**, versionCode **7**, versionName **1.4**, **35,225,475 bytes**,
SHA-256 `97b9ce68d2ff713879096499a3e1f148eb60f8ff7320780ed0714acaed642189`.
It cannot be manually uploaded until signed with the approved upload key and
checked against the highest Play version code. An evidence copy and the older
pre-existing bundle are retained separately under `build/088/android/`.

Deep's containing commit identity and subsequent installed-runtime and release
parser receipts are retained outside the candidate tree in its ignored
`build/088/local-runtime/` and `build/088/release-evidence/` directories and in
the curated knowledge-vault checkpoint. This avoids embedding a self-referential
candidate SHA. This document alone does not assert those subsequent checks pass.

## Runtime and release boundaries

The isolated Mac app at `http://localhost:8001` was rebuilt from Deep
`14bc027952c51886e8b5157350c80726859065ea` and its exact component pins. The
installed runtime passed 38 source, wheel, supporting-container and public
checks, including 21 endpoint, asset, authentication-redirect and denial checks.
Its image identity is
`sha256:e3ef104ebacdde2a0887ba3cc301fc48402964ec49d495685bc5d0e00042e5e5`.
App and PostgreSQL are healthy; the voice worker authenticates control. Original
containers and volumes are preserved. The user completed institutional login and
in-product provider setup; the agent entered no credentials and copied no
authentication state between clients. A normal settings connection test then
reported a successful model response in 431 ms.

Authenticated local web observations against that candidate:

| Flow | Actual result |
| --- | --- |
| Ordinary multiline Send | Start changed to work, input cleared, and a two-row synthetic Label/Count table rendered with Alpha = 2 and Beta = 5. The model titled the card "Document", not the requested "088 UI check". |
| Background Send | Running-task feedback appeared without a foreground skeleton; the composer remained usable and the one-shot background choice reset. History and page reload restored the exact completed synthetic response. |
| Server-owned dice example | The real dispatcher returned six legal dice values, 2/4/2/3/1/5, totaling 17. Audit showed matching `tool.roll_dice.start` and successful `tool.roll_dice.end` events. |
| Audit | Failure filtering exposed real earlier provider-not-configured denials; detail/back preserved the filter. Reversed dates were rejected, Reset cleared the filters, and Next retrieved a second page. |
| Agents | The owned-agent list and Dice Roller detail exposed personal permissions separately from owner visibility/trust. No permission, trust or visibility setting was changed. |
| Canvas export | Server audit recorded a successful HTML export and HTTP 200. The browser download event timed out and no saved file was verified; this is not a completed download claim. |
| Responsive layout | Desktop 1280×900, tablet 900×900 and phone 390×844 were visually inspected. Canvas/composer fit the viewport, and multiline draft text survived rail restoration and resizing. The temporary browser viewport override was reset. |

The live review found two web defects: New chat retained an unsent draft and
could replay its queued message into the next conversation; breakpoint changes
could leave the hidden message drawer labeled expanded. Projection follow-up
`a1fe35825f6c5799c9b1f5ff47ea3c9086969b1c` fixes both. New chat clears draft,
staged attachments, autocomplete and one-shot background arming, cancels unsent
chat queue entries and their expiry timers, and ignores removed-upload outcomes.
Other queued actions, same-owner reconnect and accepted background work remain.
Responsive controls now report the actual drawer visibility.

The final follow-up passed 69 continuity browser tests, three targeted voice
navigation tests, full configured ESLint, 44 protocol/resource/chart tests (one
wheel-install test deselected), native chart generation `--check` and whitespace
checks. A CSS-backed resize regression failed on the old source and passed after
the fix. Independent review found no further queue/upload or initialization
defect. Deep's updated pin passes composition verification (manifest digest
`d5746fae315f4deb40747250ca0e567ddf88e8d905efefca801420582048473b`), the
exact-pin regression and targeted Ruff. Native sources/assets and the AAB above
are unchanged by this follow-up. Exact installed-candidate rerun receipts are
retained outside the candidate tree under `build/088/local-runtime/`; the prior
candidate's live observations above do not themselves establish a pass of the
fixes.

Native apps remain at their normal institutional sign-in screens until the user
completes each client login/device approval. No authenticated native journey,
second-owner isolation, revocation, microphone flow, representative migrated data
or qualifying HTTPS staging run is established by these local web checks.

The release audit found no canonical current eight-target reports or qualifying
HTTPS staging inputs. Fresh diagnostic tooling and voice-worker coverage was
collected against Deep `14bc027952c51886e8b5157350c80726859065ea`; its command,
source identities, report digests and unresolved backend failures are retained
outside the candidate tree in `build/088/coverage/collection-status.json`. Those
reports do not qualify a later candidate. The release parser still requires
Windows compatibility
evidence even though Windows redesign is excluded. The source inventory still
contains 35 capability, 99 route, and 12 retained-capability entries pending;
this native increment does not complete the remaining runtime, guidance,
monitoring, or framework stories.

Protected readiness configuration is incomplete. Android/Apple publisher jobs
do not consume the protected final decision, and the protected staging runner,
environment, and activation inputs are absent. Missing infrastructure is not a
bootstrap approval. Constitution X therefore prevents ordinary candidate push,
draft-to-ready transition, signing, distribution, and publication at this stage.
No policy, workflow, environment, ruleset, signing secret, or IAM registration was
changed to bypass those gates.

The previous local AAB was unsigned and predates 088; it is preserved separately.
The documented Android upload keystore is on Windows, not this Mac. Play upload
credentials are absent, and current store version/build maxima remain unverified.
Apple distribution identity exists, but local store provisioning/installer/API
credentials are incomplete. Historical successful store workflows are not
evidence for this candidate.

The existing PRs remain draft: Projection #15, Deep #195, and unrelated unfinished
Plane #8. Once the feature and required gates are complete, the current native
dependency order is Projection then Deep. Plane #8 is not a prerequisite for this
unchanged schema pin and must not be merged as if its unfinished foundation were
qualified. Primitives and LETS are unchanged by this increment.
