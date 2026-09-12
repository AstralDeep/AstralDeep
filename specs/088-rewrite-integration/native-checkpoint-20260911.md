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

Plane remains pinned at `8924bd4ba154a00184218c5b50c3de3bab9d13c4`, schema
`079.001`. UI protocol SHA-256 remains
`160017483ed97b08b8dc0b33ec4d8a4de02fa41997506570c9bf8723d1f05801`.
No new primitive, runtime dependency, authentication scheme or schema is included.

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
but its native chart displayed a rendering error. **That live chart defect remains
open**; standalone chart fixtures are not evidence that this case works.

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
process restart; conversation interaction remains unverified. The replacement
iPhone still requires the user's normal institutional sign-in, and the final
macOS candidate is waiting for the user's Keychain Allow prompt. No credentials
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
origin; actual installed offline fallback must still be checked after rebuilding
Deep with this Projection pin.

Plane T024 is progressing in its separate review worktree and is not included in
this Deep pin. Wait/wake receipts, profile-aware owner reads and finite terminal
handling have focused PostgreSQL coverage; versioned decision/delete/reconcile,
recovery terminal semantics and future-version purge handling remain. T025–T030
and the dependent UI/guidance work are still substantive implementation tasks.

## Artifacts and release boundaries

Current Android bundle:
`/Users/sam/Desktop/Work/AstralProjection/android-client/app/build/outputs/bundle/release/app-release.aab`

It is **unsigned**, versionCode **7**, versionName **1.4**, **35,226,685 bytes**,
SHA-256 `6a417647644fbb8e2953f1c34c3a81d13076f30fa73f0532375a14cbd4fcc530`.
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
capability entries pending. Runtime, guidance, monitoring, framework and offline
stories remain. Plane PR8 contains remote T021–T023 groundwork but is unpinned;
T024/T025 are real missing work, including event waits, versioned controls and
transient-input reconstruction. A separate continuation worktree preserves the
existing remote foundation; this native checkpoint does not install that work.

Projection PR15, Deep PR195 and Plane PR8 remain draft. When fully qualified,
the current native increment depends on Projection before Deep. A future Plane
pin requires its own schema/runtime qualification before dependent Deep changes.
The owner retains the merge decision. Primitives and LETS are unchanged.
