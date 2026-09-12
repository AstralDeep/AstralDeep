# Feature 088 native checkpoint — 2026-09-11

Status: local implementation and diagnostic verification. Full feature completion,
protected staging qualification, store publication and PR readiness remain open.

## Scope and source

The owner requested Android and Apple parity with the implemented 088 web client,
excluded Windows UI redesign, authorized qualified store publication or a manual
bundle, and reserved merging the PRs. The remote baselines remain Deep
`3c4670dce022b1e2415ea2727526bcb4d1dbafcc` and Projection
`416ce6ce97b0af4cf812ffc8c53b9e2718b2aa70`. The composition manifest and gitlinks
identify this checkpoint's exact component source. Its containing Deep SHA and
subsequent runtime receipts are recorded outside the candidate tree to avoid
self-referential identities.

The current composition pins Plane `daedeed4690282da67cd0a1764668b8dc84c801f`,
schema `088.001`, migration digest
`b6eaa819e9bd471350e48e431686c1ed6922e206608f1b673e0544c14014552d`.
The still-running diagnostic backend uses the older `079.001` composition; this
source pin does not imply that its database was migrated. UI protocol SHA-256 remains
`160017483ed97b08b8dc0b33ec4d8a4de02fa41997506570c9bf8723d1f05801`.
No new primitive, runtime dependency or authentication scheme is included.
The Plane revision owns the guarded schema upgrade described below.

## Implemented behavior

Projection owns the native workspace, welcome presentation, shared licensed
Inter/JetBrains fonts and isolated offline Plotly renderer. Android/iOS/macOS
keep one ephemeral owner-bound composer across layouts, retire welcome on real
work, preserve same-owner reconnect, and clear drafts on logout, owner change
and New chat. Background submission uses the normal authenticated message path
and one-shot `async_mode` state. Watch retains its declared capability adaptations.

Deep exposes native Audit and Agents snapshots from the same owner-scoped reads
and permission resolver as web. Audit filtering, pagination, grouped events and
detail return state are preserved. Personal permissions/credentials remain
separate from owner visibility and trust. Existing command handlers retain
authority. Download handling constrains origins, redirects, temporary storage
and stale owner/session completion.

Web New chat clears unsent drafts, staged attachments, autocomplete, background
arming and only its queued chat submissions. Late upload results cannot restore
removed uploads. Responsive message-drawer accessibility reflects visibility.
The renderer preserves validated welcome placement through mobile grid adaptation.

Authenticated native checks exposed additional defects, now repaired:

- Android accepts a foreground snapshot through the request fence opened by its
  own submission. Only server-originated work needs a fresh commit-ready prelude.
  Scope, purpose, revision and replay checks remain enforced. Rejected duplicate
  preludes cannot reset the five-second recovery deadline.
- Android, Apple and Windows reject a new server commit prelude while another
  uncommitted request owns the fence. Exact terminal failure/cancellation,
  canonical retryable terminal and admission refusal retire only that request.
  Successful terminals still await their snapshot; uncertain errors retain the
  fence. Generation reuse remains rejected across New chat on one connection.
  Windows changes are protocol compatibility fixes, not a UI redesign.
- Apple admits validated unscoped welcome components only before workspace work
  or an active conversation request. Malformed, mixed, scoped and late welcome
  frames cannot bypass normal conversation validation.
- Deep New chat retires the old scope only for its own original socket/session,
  after successful creation. Owner, context and connection changes during awaits
  suppress stale writes and acknowledgment. Other sockets and durable tasks are
  preserved. A transport write already issued before backpressure cannot be
  unsent, but a later stale acknowledgment or marker is suppressed.
- Native grid column limits follow the corresponding web viewport width while
  retaining native content capabilities and watch constraints. Windows layout
  remains unchanged.
- iPhone Messages collapse removes the lazy transcript without structural
  animation, avoiding the observed SwiftUI placement loop. A five-round
  collapse/expand, canvas-scroll and composer regression exercises this case.
- Persistent runner terminal commit and claim renewal share a per-episode lock.
  Successful terminal acknowledgment retires renewal before notification;
  genuine pre-commit claim loss still cancels execution. Test database work now
  runs off the event loop, and memory replacement/replay fixtures use an explicit
  clock without changing production guard behavior.

## Verification

These cohorts overlap and must not be summed into a release assertion. Exact
commands, source manifests, raw reports, initial failures and reruns are retained
in each repository's ignored `build/088/` directories.

| Gate | Result and boundary |
| --- | --- |
| Android `ktlintCheck :app:lintDebug :core:test :app:testDebugUnitTest :core:koverVerify :app:koverVerify :app:assembleDebug :app:bundleRelease` | Passed on final native source; 113 core and 281 app tests, no failures or skips. Four real MockWebServer/virtual-clock deadline regressions changed from three failures to four passes. |
| Apple Core | 189 passed. |
| iOS unit/UI | 154 unit and two UI tests passed before the final terminal/replay-only batch; affected final unit cohort 35 passed. The UI test includes the observed canvas freeze. |
| watchOS | Final 53 passed, one skip for missing live staging inputs. |
| Apple unsigned builds | iPhone, macOS and Watch builds passed from one unchanged 169-file source closure, SHA-256 `b2cb461811c4dc6e45ac6b2adf7b98eba213d2791f1791362f68bb86fc9947e1`. Final macOS XCTest qualification remains open. |
| Full Windows portable Qt suite on macOS | 1,088 passed, ten skips. This is compatibility evidence, not Windows-host live/release qualification. |
| Full Projection Python | 1,331 passed, one skip, two failures initially: five missing transformation records and stale build-venv setuptools. Exact extraction records/counts were repaired; the existing approved build-tool pin was installed. All 58 protocol/resource rerun tests passed with one skip. No gate was weakened. |
| ROTE/native grid | 581 passed, including 39 density cases; five new executable lines covered. |
| Apple/Windows repair coverage | Python 16/16 changed lines; Swift 77/81, 95.1%. Four unhit Watch lines are pending-pre-chat-id cleanup guards. This is repair coverage, not full-PR qualification. |
| Deep New chat | 110 real-PostgreSQL/continuity/background/narrative/connection tests passed; changed executable lines 22/22, branches 10/10. Separate final host scope/stress cohort 26 passed. |
| Deep runner/fixtures | 434 persistent/DSN attachment/cutover/memory tests passed; final supervisor cohort 32 passed. Runner changed executable lines 20/20 covered. |
| Earlier renderer/browser/chart work | 862 renderer/ROTE, 71 browser continuity and nine real Chromium offline-chart tests passed on their recorded prior candidates. Native chart generation drift check passed. |
| Earlier native fixtures | Four Android instrumentation tests each on phone/tablet; iPad UI fixture passed. These precede the newest repairs and do not qualify all final inputs. |

The broad Deep run against prior candidate `db775cc4c318e02033185357565d9217796df468`
passed 8,674 main tests (34 skips and two declared integration deselections).
Seventeen cohorts combined exposed one timing fixture failure, subsequently
repaired. Supplemental DSN tests exposed the runner race and five event-loop
fixture violations described above. Prior tooling and voice coverage passed;
those reports are not relabeled for this later candidate. Fresh final-source
full coverage, strict changed coverage and release parsing remain separately
recorded, required work.

## Authenticated local observations

The user performed institutional sign-ins and in-product provider setup. The
agent entered no credentials and copied no authentication state between clients.
The isolated app uses localhost port 8001 with preserved storage and supporting
containers. Backend source is baked into images; exact candidate refreshes use
an app-only image rebuild, not a restart presented as a source update.

Before these newest repairs, web ordinary and background Send, a real dice tool,
Audit denial/success filters, date validation, detail/back and pagination, and
read-only Agents permissions were exercised. Phone/tablet/desktop resize and
New chat then passed on Deep `db775cc4c318e02033185357565d9217796df468` with
Projection `97ef7ee2a6e00d172854f2f009aec943f2d0ab11`. That installed candidate
passed 39 source/wheel/public checks and production missing-configuration exit78.
An HTML export returned HTTP200 server-side, but no saved download was verified.

On authenticated native clients, macOS returned `macOS native check passed`;
iPhone returned `iPhone native check passed` and rendered six real dice totaling
18. That iPhone run exposed the grid density and Messages-collapse defects.
Android initially discarded a successful dice snapshot; after the initial fix
it hydrated the six-dice table totaling24 and preserved the canvas through both
`Android foreground commit passed` and `Android background result passed`.
Audit showed actual tool start/end records and working filters/details. A later
Android recovery/deadline batch was rebuilt and still requires final live checks.
Fresh Apple replacement builds likewise require authenticated checks against
the new backend candidate. Prior observations are not final-candidate claims.

## Subsequent authenticated repairs and T012

Projection `2ec13a3806b31abc7eecc5e13de0c8d47cf5ebca` admits the validated
start welcome while New chat is awaiting its acknowledgment; real work still
retires welcome. Projection `40431505178ee697e28ab1cdb8f7edcd70b9f42d` reports
Android viewport dimensions in logical pixels while retaining physical display
metrics. Regression tests demonstrated each prior failure before passing.

The Android APK with SHA-256
`bed0841c175be044932b1bb4ef155d76c6f6d557eafa4e58b864c22d68aee1bf`
retained user authentication and hydrated the real six-dice result totaling30.
Stats now use one column at the phone's 411 logical pixels. New chat, More,
Messages collapse and an actual saved HTML export passed. A subsequent background
request preserved the dice canvas and committed the synthetic Alpha2/Beta5 result,
but its native chart displayed a rendering error. The subsequent shared chart
repair and actual saved-result verification below close that specific defect.

The exact installed Deep `e8d6a30763b5feba79d5c46cc70178543ecb9fee` image passed
42 source/package/public checks. Its full diagnostic coverage run totaled11,169
passed, five failed and four setup errors; changed executable coverage passed
1006/1007 (99.90%). A test changed only unused base64 padding bits rather than the
MAC, and verification harness database calls ran on the event loop. These fixtures
and harness calls are repaired in Deep `4a77331`; the actual guarded verification
suite then passed63 and identity module passed10, with changed driver coverage
51/54 (94.44%). These overlay results do not relabel the original failed candidate
reports. Fresh final-candidate qualification remains required.

Unsigned iPhone builds lacked simulated application/keychain entitlements and
could not persist login. A local ad-hoc simulator build with the existing observed
team/application identities fixes the build setup without authentication source
changes. Its Watch companion retained authentication through installation and
process restart; conversation interaction remains unverified. The user completed the replacement
iPhone institutional sign-in and macOS Keychain prompt. Android, iPhone, macOS
and Watch retained their sign-in through process restart. iPhone and macOS New
chat/More checks passed; Watch conversation interaction is still unverified. No credentials
were entered or copied by the agent.

Projection `8a2ef8bfcc75643d1d1af5497285773ddb804611` implements T012's public-only
offline document, manifest and worker. Seven exact public assets are anonymously
fetched, size/hash/MIME checked, and checked again when read from cache. Only root
navigation can fall back to the offline document; personalized shell/API/auth,
queries, private headers and non-GET requests are excluded. Failed updates do not
activate. Cleanup is limited to this worker's cache prefix. Deep explicitly serves
the two bundled WOFF2 fonts as font/woff2 because minimal Linux images otherwise
label them application/octet-stream, preventing the strict worker from installing.

T012 verification:40 worker unit tests, four real Chromium tests,122 Projection
protocol/resource/CI tests (one skip), and51 Linux Deep static/cache/shell security
tests passed. Changed coverage is112/112 worker lines,8/8 registration lines,
33/33 generator lines and7/7 Deep lines. Browser fixtures used an isolated synthetic
origin. Deep `e0a22dcd985c245761a53bbe7a8d5126153f1de5` subsequently passed
53 source/package/public checks and six installed-worker assertions in isolated
anonymous Chromium: exact public cache membership, offline root fallback, API
and auth exclusion, narrow layout and online recovery. Authenticated shell
registration was independently observed; this is not personalized offline data.

Plane T024 is committed locally in the separate review worktree at
`d39053ce202f99216d6a8f0d6f9b398751c7e65c` and is not included in this Deep pin.
Wait/wake receipts, versioned controls, terminal recovery/reconciliation and
conservative unknown-record retention passed 2,403 Linux/Python 3.11 PostgreSQL
tests with nine Windows-only skips. Changed coverage is 169/173 versus the prior
local increment and 654/684 versus main. The mixed-profile cleanup adapter
requires explicit Deep adoption before pinning. T025 is now qualified locally at `f0c2109feb73bafa4dce8dcfc8535395a83e10d2`: 2,467 Linux/PostgreSQL tests passed with nine Windows-only skips, 89.68% aggregate and 211/214 changed-line coverage versus d39053. Package/source equality and Python 3.11/3.14 imports passed. Deep must still supply keyed request/result attestations, source reconstruction and current authority. T026–T030
and the dependent UI/guidance work remain substantive implementation tasks.

Projection `d178a848ff999f7ccfa3eb72b4f920cd33ac45f0` repairs native Plotly
handling of ordinary sanitized objects while keeping prototype keys inert and
external image sources blocked. The exact previously failing saved Alpha2/Beta5
chart rendered successfully on authenticated Android; seven Android, fifteen
Chromium and four Apple WKWebView regression tests passed. The following Apple live checkpoint includes this chart repair.

Projection `a30f9fc5ba9cc9e84e78f5da2332612e40dc3fd3` adds the reviewed native
style batch. Android restores canonical metric titles, web card/chart tokens,
Inter weights, adaptive welcome placement and right-aligned composer controls.
Idle voice stays quiet while explicit errors and terminal notices remain visible.
Android and Apple Markdown links preserve labels but reject unsupported schemes;
root-relative URLs resolve against the configured backend without credentials.
Apple cards, metrics, charts, canvas spacing and New chat use measured web tokens.

Final Android gates passed: ktlint, lint, 113 core and 287 app unit tests, both
coverage gates, debug/test APKs and release bundle. Fourteen device tests passed,
including light/dark pixel checks, adapted welcome layout, multiline/large-font
composer behavior, read-only drafts and retained terminal notices. APK SHA-256:
`e4658a265d04093532765fb42502931076876542565b0d4391d92199d6d70673`.
Apple Core193, four style raster tests, four WebKit chart tests and three workspace
UI tests passed. Clean iPhone/macOS/Watch ad-hoc builds passed. Projection's
protocol/resources/chart regression cohort passed71 and chart generation did not
drift. These cohorts overlap earlier reports and are not summed.

Updated iPhone and macOS retained authentication; macOS required another user-
completed Keychain prompt for the rebuilt binary. Both loaded saved metrics and
passed Messages collapse plus New chat/More. Mac rendered Alpha2/Beta5 bars;
its white WebKit page background is a newly confirmed parity defect under repair.
iPhone chart heading loaded, but live scroll gestures did not move its viewport,
so full live chart visibility is still unverified. Watch was updated, with its
new live conversation journey pending. Automated synthetic checks are separate.

The partial Work read facade adds owner-only list/detail/immediate poll through
fresh auth and role validation, no-store responses, opaque future metadata and
bounded off-loop reads. Review removed an inherited synchronous profile write
from read authentication. Seventy-eight tests passed against an exact Plane d39053
archive and real isolated PostgreSQL, with147/148 new lines covered. This is an
explicit qualification overlay: Deep's Plane pin and installed runtime remain079.
Admission, controls, joined activity/results, SSE, and immediate bearer-session
revocation are not implemented by this slice; T027/T030 remain unchecked.

The e0a22 backend cohorts totaled 11,200 passed, one timing fixture failure,
15 skips and two declared integration deselections. All sixteen explicit module
cohorts passed. Strict changed coverage was 1,064/1,068 (99.63%); tooling passed
835 and voice passed372. The fixture repair in Deep `63c0caf` passed twenty
repeated runs plus144 surrounding tests without changing production behavior.
Those overlay results do not relabel the e0a22 report; final-source qualification
and protected release evidence remain required.

## Artifacts and release boundaries

Current Android bundle:
`/Users/sam/Desktop/Work/AstralProjection/android-client/app/build/outputs/bundle/release/app-release.aab`

It is **unsigned**, versionCode **7**, versionName **1.4**, **35,236,402 bytes**,
SHA-256 `17cfe98f82858fe172bdc26b0d9739627773418c6246204e04156e086134d6d8`.
The Gradle task name does not establish signing; no signature entries exist.
It is not uploadable until signed with the approved upload key and checked
against Play's highest version code. The documented upload key is on Windows,
not this Mac; Play service credentials are absent. Apple local distribution
provisioning/installer/API inputs and current store build maxima are incomplete.
Unsigned simulator and macOS builds are diagnostic artifacts, not store releases.

No current canonical eight-target reports, qualifying HTTPS staging inputs or
protected publisher decision exist. Protected staging runner/environment/activation
configuration is incomplete. Android/Apple publisher jobs do not consume the
protected final decision. Constitution X therefore leaves ordinary candidate
push, draft-to-ready transition, signing/distribution and publication gated.
Missing infrastructure is not bootstrap approval. No policy, workflow, ruleset,
signing secret or IAM registration has been weakened to bypass this boundary.

Second-owner isolation, revocation, microphone flow, remaining native downloads,
representative migrated data and all-target authenticated staging remain open.
Watch explicitly hands off forms, effect approval, chrome editing, chart
interaction and downloads. Android public HTTPS downloads use the system browser
without app credentials. Exact requested behavior on every form factor is not
established by the existing local evidence.

The source inventory still includes 35 capability, 99 route and 12 retained
capability entries pending. Runtime, guidance, monitoring and framework
stories remain. Offline T012 is locally implemented and verified. Plane PR8
contains remote T021–T023 groundwork but is unpinned; qualified local T024 has
not been pushed, and T025 transient-input reconstruction is locally qualified but unpinned. A separate continuation worktree preserves the
existing remote foundation; this native checkpoint does not install that work.

Projection PR15, Deep PR195 and Plane PR8 remain draft. When fully qualified,
the current native increment depends on Projection before Deep. A future Plane
pin requires its own schema/runtime qualification before dependent Deep changes.
The owner retains the merge decision. Primitives and LETS are unchanged.


## Matched Plane and execution checkpoint

Plane `daedeed4690282da67cd0a1764668b8dc84c801f` includes the T024/T025
foundation and a shared assignment/admission transaction guard. It locks owner
retirement, the selected grant, assignment and admission in the reviewed order,
then samples current database time and validates authority. Deep's persistent
action start and finish use that same bounded transaction. Exact approved-action
identity is retained; fresh remote checks run before success publication. An
authentic stale permit still settles accounting while result content remains
unavailable. A revoked grant refuses a hold and leaves the existing recovery
path responsible; no fabricated successful completion is recorded.

Owner cleanup now retires both operation profiles and commits that retirement
before returning a hold for retained assignments or unresolved actions. An older
Plane without the mandatory facade fails closed. Deep's static composition
verifier reads only the reviewed assignment/operation data-only literal modules,
without executing candidate Python or accepting arbitrary imports.

Qualification: Plane's final Linux/Python3.11 PostgreSQL suite passed2,503 with
nine Windows-only skips, with39/39 changed guard lines covered. The Deep caller
cohort passed426 without skips against the exact Plane bytes and event-loop
guard;39/39 changed executable lines are covered. Owner cleanup passed680 in
its original broader run with eight old-manifest setup errors; the matched
composition rerun passed those eight and one adjacent case (nine total). The
composition/install verifier cohort passed169, with15/15 changed verifier lines
covered. Exact commands, source hashes, first failures and reruns are retained in
`build/088/dual-fence-qualification`, `owner-retirement-qualification`, and
`operation-schema-coverage`. These are local increment diagnostics, not the
final candidate's full coverage or protected release decision.

T025 is qualified. T026 still requires transient-input reconstruction and
one-shot execution; T027–T030 submission, controls, joined events/results and
streaming remain incomplete. This checkpoint does not enable those features,
migrate the running app, publish product branches, submit stores or promote PRs.


## Native history and test isolation checkpoint

Projection `61ead0d14daaceb167dbb9bf18cbe472b26646a5` renders canonical
history previews, glyphs, timestamps, saved markers and titles in shared native
rows. Strict unscoped history delivery updates chrome without replacing active
canvas or turns. Watch preserves its server-adapted rows and same-owner HTTP
read across socket reconnect; the established socket requests fresh history.
The reconnect repair follows an actual empty-list observation on the signed-in
Watch. macOS charts composite against the actual trusted host backdrop, including
translucent transcript bubbles. All96 Apple app unit-test model constructions
now explicitly inject memory storage. Production still uses its existing
Keychain; code inspection found a potential test-interference path but did not
prove that a real session was deleted.

Android passed117 core and293 app tests, both coverage gates, ktlint/lint, debug
and release bundle builds, and seven checks on a separate test emulator. Apple
passed196 Core tests,178 Mac app tests,17 final Watch tests and prior27 iOS
unit/UI tests plus the final history UI rerun. Twelve focused Mac chart/style
checks include actual WebKit pixel comparisons. Projection protocol/resources
passed66 with one source-history skip; chart generation and provenance checks
passed. Provenance retains519 original source entries:174 transformations
(including16 removals) and345 unchanged imports. These overlapping cohorts are
local diagnostics; full exact-candidate qualification remains required.

The Android instrumentation runner initially removed the app from the user's
emulator during test cleanup. The final APK was restored without copying auth
state; the user signed in again. Its real nine-row history and saved-result
selection then passed. Tests now use a separate emulator. Updated iPhone/macOS
history and saved metrics were observed; the saved macOS bar chart renders with
the correct dark background. The final rebuilt Apple artifact checks, actual
iPhone chart scrolling, final Watch interaction and remaining topbar Export/Share
parity are separate pending work.

Current AAB SHA-256 is
`c1e91ac4b85b0855275bc1282d18d52221a964ae2e70acbc2b0a22c05e13fafa`,
35,243,718 bytes, versionCode7/versionName1.4. It is unsigned, not upload-ready.
Valid Apple signing identities exist; installed distribution provisioning
profiles and protected publisher integration remain missing. Android's existing
upload-keystore location is still needed. No product push, store action, PR
promotion or running backend update occurred at this checkpoint.

## Work controls and complete baseline qualification follow-up

The owner-authenticated Work API now supports pause, cancel and settled terminal
deletion. Pause/cancel use canonical command receipts and the observed state
revision; retries cannot repeat the transition. Cancellation preserves issued
effect liabilities. Deletion refuses active, retained or unresolved work and
returns 404 for absent, foreign or repeated deletion. Cookie writes require the
exact public origin and JSON; bearer writes retain fresh institutional IAM and
role verification. Query credentials are refused for writes. Resume, submission,
the one-shot runner, joined results/events and SSE are still incomplete, so
T027/T030 remain open.

The five-file controls batch passed 86 isolated PostgreSQL/auth/API tests with
the development mock initially enabled, then explicitly disabled inside the
real credential tests. Changed production coverage is 113/113; all three Work
modules cover 258/258 statements. Ruff and diff checks pass, and the integrated
source hashes match `build/088/work-controls-qualification/handoff.json`.

An immutable image of Deep `eaf8994bc17a1f9c43e2b180d8ed4cb0e946eef4`,
with its exact four component pins, completed all 17 backend CI cohorts:
11,260 passed, seven failed, 15 skipped and two deselected. Tooling passed 835
tests (three skipped, four CI deselected); voice passed 372 (two skipped).
Strict changed-Python coverage was 1,278/1,283 (99.61%). The seven failures were
five current-schema assertions still expecting 079 and two auth fixtures
setting `MOCK_AUTH` instead of the actual `USE_MOCK_AUTH` variable. Four schema
test files now bind current 088 metadata while preserving historical 079
lineage, with 15 focused tests passing. The controls batch includes the minimal
auth fixture correction and retains real 200/403/401 credential behavior.
Original failed candidate reports remain unchanged; these focused follow-ups
are not a passing rerun of a new final candidate.

The full diagnostic handoff is under
`build/088/coverage/eaf8994bc17a1f9c43e2b180d8ed4cb0e946eef4/`.
All 1,149 baked backend Python source hashes were checked. The live app and
database container identities, start times, networks and mounts remained
unchanged. Canonical release collection refused missing provider/staging inputs;
none of these results authorizes release or PR promotion.

The final normal macOS 61ead0d build has since passed user-approved Keychain
sign-in, nine-row history, exact saved-result selection and the dark Alpha/Beta
chart check. The final Watch home shows recent chats; its saved-row interaction
still awaits a manual tap because simulator automation cannot deliver that tap.
The new shared Export/Share foundation and native consumers are being qualified
separately and are not part of these committed native artifact claims.


## Server-owned native workspace controls checkpoint — 2026-09-12

Projection `5a06d37a915982d2d6dca3ad9ceb64ca3c242681` adds the exact
server-defined Export/Share toolbar descriptors to Android and iOS/macOS.
The actions wrap in canonical order below 700 logical pixels, preserve the
visible canvas while work is in progress, and disappear with that canvas.
Share sends one authenticated request, validates its returned same-origin link,
and preserves the closed PHI denial. Export verifies the render revision and
keeps temporary files and native save callbacks tied to the original account,
conversation and presentation. Cancellation cannot clear or save a newer action.
Watch retains the explicit 055 chrome-free/file-I/O omission. Windows consumes
its existing compatible controls without a redesign.

The matching UI protocol canonical digest is
`84e4e70b3cfd10e3964e3da3b2f26fe78316b33fbbaccca7ec1f543f0183baf3`
(file SHA-256 `994b5bea27c68e837bebe58acfbc3003b46b9c0326e5181096b613865b7d6bc7`).
Deep pins that exact component and resolves export/share availability independently;
default export-on/share-off behavior is retained. No schema or dependency changed.

Android passed all wrapper gates: 118 core plus 314 app unit tests, no failures
or skips, and core Kover 1,869/2,006 lines (93.17%). Eleven instrumented toolbar
and existing chrome tests passed on isolated emulator 5584, including explicit
48 dp targets and 320 dp wrapping. That emulator was stopped; the user's signed-in
5554 app was not changed. Apple passed 205 Core tests, 187 Mac app tests, nine
focused final Mac tests after test-only formatting, and a final iPhone rerun of
nine app tests plus one toolbar UI test. Strict formatting passed 99 Swift files.
The earlier iPhone cohort passed 13 tests before save-lifetime hardening.

The shared foundation passed 177 Projection chrome tests, 55 Deep host tests,
26 Windows compatibility tests and five actual-renderer browser tests, with
100% changed executable Python coverage (21 Projection lines and eight Deep lines).
The final combined protocol/resources/charts/chrome/Watch run passed 254 tests,
including immutable extraction replay. The integrated Deep host/chrome follow-up
passed 75 tests, the composition suite passed 121, and the local composition
verifier reported no diagnostics after staging the matching gitlink. Provenance
now contains 178 transformations
(including 16 removals) and 341 unchanged entries out of the original 519.
Source-fenced Android, Apple and shared receipts are in each repository's
`build/088/` tree. These overlapping local diagnostics are not release evidence.

Current native actions still receive the existing static server HTML; exact
web document appearance requires the portable export implementation now isolated
on `codex/088-native-export` worktrees. It must capture visible native state and
loaded pixels and use the shared web finalizer. Authenticated live checks of the
new actions remain open. No current-action build was installed into user sessions,
no product branch pushed, no draft promoted and no store upload performed.

A read-only store-access check found the signed-in Play Console account exposes
only a different package, `edu.uky.ai.astral`; the intended
`com.personalailabs.astraldeep` listing and its highest versionCode were unavailable.
App Store Connect requires sign-in. This does not change application identities
or establish upload access. The preserved 61ead0d AAB remains unsigned and not
upload-ready; the upload keystore and protected Apple/store publisher setup are
still missing. See `build/088/handoff/store-access-20260912.json`.
