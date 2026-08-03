# AstralDeep — native Apple clients (features 051 + 053 + 065)

Three SDUI targets on one shared Swift package, with a pinned LiveKit RTC
dependency for conversational media on iOS and macOS:

```text
AstralCore/    # SPM package: protocol, dispositions + drift guard, WS client,
               # PKCE, device-login client, token stores, REST (swift test-able)
AstralApp/     # iOS (twin of android-client) + macOS (twin of windows-client)
               # — ONE multiplatform target; also embeds the watch app
AstralWatch/   # watchOS: QR sign-in (RFC 8628 via backend broker), bounded
               # PCM voice bridge in/out, and server-synthesized speech
```

Specs: `specs/051-apple-native-clients/` (051 — the clients themselves),
`specs/053-apple-app-store/` (053 — packaging, signing, and the release
pipeline), and `specs/065-conversational-voice/` (065 — included voice across
all clients). Feature 065 pins `client-sdk-swift` 2.15.3 and every resolved
transitive revision in the committed `Package.resolved`; watchOS uses the
owned bounded PCM-over-WSS bridge rather than the LiveKit package.

## Shipped identities

| Thing | Value |
| --- | --- |
| iOS + macOS bundle id | `com.personalailabs.astraldeep` |
| watchOS bundle id | `com.personalailabs.astraldeep.watch` |
| URL scheme (PKCE redirect) | `com.personalailabs.astraldeep` |
| OAuth redirect URI | `com.personalailabs.astraldeep:/oauth2redirect` (single slash) |
| Keycloak client — iOS | `astral-mobile` (shared with Android) |
| Keycloak client — macOS | `astral-desktop` (shared with Windows) |
| Keycloak client — watchOS | `astral-watch` (device-authorization grant) |

The client ids are defined once in `AstralCore/Sources/AstralCore/Configuration.swift`;
the redirect scheme is mirrored in the app's `Info.plist` `CFBundleURLSchemes`.

## The Xcode project is committed and canonical

`apple-clients/AstralApp/AstralApp.xcodeproj` is the **single, canonical**
project — open it directly. There is **no project generator**: the old
XcodeGen `project.yml` was deleted. Do not run `xcodegen`, and do not
regenerate the project; edit `project.pbxproj` (or the Xcode UI) in place.

Three schemes resolve from it: **AstralApp** (the multiplatform iOS + macOS
app), **AstralWatch** (the embedded watchOS app), and **AstralCore** (the
package, exercised via `swift test`).

`AstralApp` is one target whose `SUPPORTED_PLATFORMS = iphoneos
iphonesimulator macosx`. The watch app is an **embedded companion** (an
"Embed Watch Content" copy-files phase + a target dependency, both
`platformFilter = ios`): the iOS product carries `Watch/AstralWatch.app`, and
the macOS product carries no watch app at all. `WatchInfo.plist` sets
`WKCompanionAppBundleIdentifier = com.personalailabs.astraldeep` and
`WKRunsIndependentlyOfCompanionApp = true`.

## AstralPrims — the Swift `astralprims` mirror

`AstralCore/Sources/AstralCore/Primitives/` mirrors the first-party
[`astralprims`](https://github.com/AstralDeep/Astral-Primitives) Python
package (currently v0.3.0): the same 32 primitives (`AstralPrims.Text`,
`.Card`, `.Table`, `.Hero`, …) with the same serialization semantics —
`toDict()` ≙ `to_dict()`, `createUIResponse` ≙ `create_ui_response`,
`attributes` merged last (and able to override), `class_name` → `"class"`,
empty `css` omitted, non-Optional defaults emitted. Types are namespaced
under `AstralPrims` so nothing collides with SwiftUI. This is the AUTHORING
layer only — the consuming/render model stays `AstralComponent`
(Constitution II: astralprims defines → orchestrator renders → ROTE adapts).

```swift
let canvas = AstralPrims.createUIResponse([
    AstralPrims.Hero(title: "Q3 Sales", variant: "gradient"),
    AstralPrims.Grid(columns: 2).add(
        AstralPrims.MetricCard(title: "Revenue", value: "$1.2M", subtitle: "+12%"),
        AstralPrims.MetricCard(title: "New users", value: "3,401", variant: "success")),
])
```

Fidelity is pinned by known-answer fixtures generated FROM the live Python
package: every Swift construction in `PrimitivesTests` must byte-match its
Python `to_dict()`. When the pip package version bumps, regenerate:

```bash
docker cp apple-clients/AstralCore/Tests/AstralCoreTests/Fixtures/generate_fixtures.py \
  astraldeep:/tmp/gen_fixtures.py
docker exec astraldeep python3 /tmp/gen_fixtures.py
docker cp astraldeep:/tmp/fixtures_out.json \
  apple-clients/AstralCore/Tests/AstralCoreTests/Fixtures/astralprims-fixtures.json
swift test --package-path apple-clients/AstralCore   # fails on any drift
```

## How the endpoints are configured

The server and Keycloak URLs are **build-time configuration**, not source.
`apple-clients/Config/{Base,Debug,Release}.xcconfig` define
`ASTRAL_SERVER_BASE_URL` + `ASTRAL_KEYCLOAK_AUTHORITY`, wired as the project's
Debug/Release `baseConfigurationReference`. They surface into BOTH Info.plists
as `ASTRALServerBaseURL` / `ASTRALKeycloakAuthority` and are read at launch by
`AstralCore.AstralConfig`.

| Configuration | `ASTRAL_SERVER_BASE_URL` | `ASTRAL_KEYCLOAK_AUTHORITY` |
| --- | --- | --- |
| Debug | `http://localhost:8001` | `https://iam.ai.uky.edu/realms/Astral` |
| Release | `https://sandbox.ai.uky.edu` | `https://iam.ai.uky.edu/realms/Astral` |

Repoint a build with **no code edit** — override the key in the xcconfig, or on
the command line (`xcodebuild … ASTRAL_SERVER_BASE_URL=…`).

At launch `AstralConfig` resolves the endpoint by the ladder **runtime override
→ Info.plist → compiled-in fallback** (FR-011). A value is only usable if it is
an absolute `http(s)` URL with a host, which also rejects an unsubstituted
`$(ASTRAL_SERVER_BASE_URL)` literal. The watch can adopt the phone's override
opportunistically over WatchConnectivity (no App Group), and otherwise falls
back to its build-time endpoint.

> xcconfig quirk: a bare `//` starts a comment, so the URLs are written
> `https:/$()/host` — the empty `$()` splits the slashes past the comment
> scanner and still expands to `https://host`. Keep that form when editing.

## App icons

Generated by `apple-clients/Scripts/generate_app_icons.py` (stdlib + the
`sips` tool, **zero dependencies**). Regenerate every slot from the master:

```bash
python3 apple-clients/Scripts/generate_app_icons.py            # regenerate
python3 apple-clients/Scripts/generate_app_icons.py --check    # verify only (CI gate)
```

The iOS/watchOS 1024 icons are fully opaque (ITMS-90717); the ten macOS slots
keep their transparent gutter. `--check` is run by `apple-ci.yml` so an icon
regression fails in CI rather than at App Store upload.

## Build & test — all three schemes

```bash
# AstralCore package (any Mac, no Xcode project needed):
swift test --package-path apple-clients/AstralCore    # includes the ui_protocol.json drift guard

# iOS app (+ embedded watch), unsigned:
xcodebuild -project apple-clients/AstralApp/AstralApp.xcodeproj \
  -scheme AstralApp -destination 'generic/platform=iOS Simulator' \
  -configuration Debug CODE_SIGNING_ALLOWED=NO build

# macOS app, unsigned:
xcodebuild -project apple-clients/AstralApp/AstralApp.xcodeproj \
  -scheme AstralApp -destination 'platform=macOS' \
  -configuration Debug CODE_SIGNING_ALLOWED=NO build

# watchOS app, unsigned:
xcodebuild -project apple-clients/AstralApp/AstralApp.xcodeproj \
  -scheme AstralWatch -destination 'generic/platform=watchOS Simulator' \
  -configuration Debug CODE_SIGNING_ALLOWED=NO build
```

`DEVELOPMENT_TEAM = $(ASTRAL_DEVELOPMENT_TEAM)` is empty by default, so
unsigned CI builds and clean clones build without a signing identity.

### One-time macOS network-cache migration

Feature 065 routes authenticated HTTP and WebSocket traffic through an ephemeral,
no-store session. At every iOS, macOS, and watchOS launch, the app also fails closed
unless it can remove the bundle-scoped `Cache.db`, journal/WAL/SHM files, and
`fsCachedData` directory from its current sandbox.

A sandboxed macOS build cannot reach the global cache directory created by an older
unsandboxed development build. Before validating an upgrade from such a build, quit
the old app and use Finder to move only these legacy artifacts from
`~/Library/Caches/com.personalailabs.astraldeep/` to Trash:

- `Cache.db`, `Cache.db-shm`, `Cache.db-wal`, and `Cache.db-journal`
- `fsCachedData`

Then launch the signed, sandboxed build and verify both the global and container
cache locations are clear. Moving the artifacts is recoverable and does not erase
Trash; an operator must treat Trash as retained sensitive data until they explicitly
empty it. Do not add a temporary sandbox exception or re-enable persistent URL
caching to automate this development-only migration.

## Running against the dev backend

1. Backend up (`docker compose up -d`) with `.env`:
   `FF_DEVICE_LOGIN=true`,
   `KEYCLOAK_ALLOWED_AZP=…,astral-mobile,astral-desktop,astral-watch`,
   `KEYCLOAK_DEVICE_CLIENTS=astral-watch`.
2. Keycloak realm: create the three public clients per
   `docs/keycloak-realm-settings.md` §051 (device grant ON for `astral-watch`;
   `com.personalailabs.astraldeep:/oauth2redirect` in the Valid redirect URIs of
   `astral-mobile` and `astral-desktop`).
3. iOS/macOS app: a **Debug** build already points at `http://localhost:8001`
   (from `Config/Debug.xcconfig`); the sign-in screen can still override the
   server + realm URL at runtime (the FR-011 override wins over the plist).
4. Watch app: launches straight into the QR screen against its build-time
   endpoint (Debug → `http://localhost:8001`; simulators reach the host
   directly). Scan with a phone camera or type the short code at the realm's
   `/device` page.

## App Store compliance (053)

- **ATS**: neither Info.plist sets `NSAllowsArbitraryLoads`. Both carry only
  `NSAllowsLocalNetworking = true`, which relaxes ATS for loopback/`.local`
  and never permits an insecure load to a public host — so Release
  (HTTPS to `sandbox.ai.uky.edu`) is fully ATS-compliant.
- **Sandbox / entitlements**: the only entitlements file is
  `AstralApp/AstralApp-macOS.entitlements` (`app-sandbox`, `network.client`,
  the `network.server` socket-bind capability required by LiveKit/WebRTC ICE,
  and audio input), wired via `CODE_SIGN_ENTITLEMENTS[sdk=macosx*]` on the
  **Release** config alongside `ENABLE_APP_SANDBOX[sdk=macosx*] = YES` and
  `ENABLE_HARDENED_RUNTIME[sdk=macosx*] = YES`. There is no iOS or watch
  entitlements file and no `keychain-access-groups` — tokens live under the
  default per-app keychain access group (least privilege).
- **Privacy manifests**: `AstralApp/AstralApp/PrivacyInfo.xcprivacy` and
  `AstralWatch/PrivacyInfo.xcprivacy` (they must live inside the
  file-system-synchronized folders to be bundled) declare
  `NSPrivacyTracking = false` and the `UserDefaults` required-reason `CA92.1`.
- **Encryption**: `ITSAppUsesNonExemptEncryption = false` in both Info.plists.
- **Microphone disclosure**: both app Info.plists declare a narrowly worded
  `NSMicrophoneUsageDescription`. Capture starts only after the user activates
  conversation mode and stops on end/background/ownership loss. ASR and TTS
  run on the server; the apps do not use Apple's Speech framework or retain
  microphone/generated audio.
- **BYO client-side agents (features 057/058) are AUTHOR-ONLY on these builds.**
  The App Sandbox forbids spawning arbitrary child processes / executing the
  packaged interpreter, so a Mac App Store build cannot *host* a user-authored
  agent — it can only author and manage one (execution binds to a separate
  Windows or direct-download-macOS desktop host). A non-sandboxed
  **direct-download (Developer ID)** macOS build *may* host BYO agents with the
  same child-process model as Windows. Host-gating rationale + the full BYO
  security/enablement posture: [docs/byo-client-agents.md](../docs/byo-client-agents.md).
  The watch is excluded from BYO authoring entirely.

## Signing & release runbook (053)

Release is driven by **`.github/workflows/apple-release.yml`** (trigger:
push a tag `apple-v*`, or `workflow_dispatch` from the `main`/default ref;
runner `macos-15`). It
automates **archive → sign → export → validate → upload** and nothing more.

### The tag namespace is `apple-v*`, not `v-apple-*`

`release-windows.yml` triggers on `v*`, and a GitHub Actions `*` matches any
character except `/`, so a `v-apple-1.0.0` tag would ALSO match `v*` and
double-fire the Windows release. Only a namespace that does not begin with `v`
is provably disjoint. Push `apple-vX.Y.Z` where `X.Y.Z` equals the project's
`MARKETING_VERSION` — a guard step fails the run if they disagree. The build
number is `CURRENT_PROJECT_VERSION = $GITHUB_RUN_NUMBER`, passed straight to
`xcodebuild` (not agvtool).

### Required repository secrets

Seven secrets are checked by a fail-fast gate (names only ever reach the log —
never values); no archive is produced if any is missing:

| Secret | What it is |
| --- | --- |
| `APPLE_TEAM_ID` | Apple Developer Team ID (also injected as `ASTRAL_DEVELOPMENT_TEAM`) |
| `APPLE_DISTRIBUTION_CERT_P12_BASE64` | base64 of the Apple Distribution `.p12` |
| `APPLE_CERT_PASSWORD` | password for that `.p12` |
| `APPLE_PROVISION_PROFILE_BASE64` | base64 of a **tar of all three** App Store profiles |
| `ASC_KEY_ID` | App Store Connect API key id |
| `ASC_ISSUER_ID` | App Store Connect API issuer id |
| `ASC_KEY_P8_BASE64` | base64 of the ASC API `.p8` private key |

`ExportOptions` rendering additionally reads three profile-name secrets —
`APPLE_PROFILE_IOS`, `APPLE_PROFILE_MACOS`, `APPLE_PROFILE_WATCH` — mapped to
each bundle id/platform.

### The three provisioning profiles

A `.mobileprovision` is per bundle-id **and** platform, so you need three App
Store profiles: iOS (`com.personalailabs.astraldeep` on iOS), macOS
(`com.personalailabs.astraldeep` on macOS), and watchOS
(`com.personalailabs.astraldeep.watch`). Tar all three and base64 them into
`APPLE_PROVISION_PROFILE_BASE64`; the workflow installs them into both the
MobileDevice and Xcode UserData profile directories and asserts it found ≥3.

### What the pipeline does (and does not do)

- Archives iOS (asserts `Watch/AstralWatch.app` **is** embedded) and macOS
  (asserts a watch app is **not** embedded — the iOS platform filter must hold).
- `-exportArchive` both with `method = app-store-connect` export options.
- `altool --validate-app` then `altool --upload-app` both into the ONE
  Universal Purchase record they share.
- **No notarytool.** Notarization is the Developer-ID / outside-the-store path;
  App Store builds (including the Mac App Store) are signed-checked by Apple
  after upload.
- **Submission is operator-performed.** The pipeline stops at "uploaded &
  validated". Pressing **Submit for Review** needs a complete store listing —
  screenshots for iPhone 6.9", iPad 13", Mac and Apple Watch; description;
  privacy-policy URL; age rating — that only the operator can author, and
  Apple's submission API refuses an incomplete listing.

## Parity + CI

- Per-frame/per-component dispositions live in
  `AstralCore/Sources/AstralCore/Protocol/Dispositions.swift` — the
  machine-checked seed of the 044 parity matrix rows for ios/macos/watch.
- CI: `.github/workflows/apple-ci.yml` runs the icon `--check` gate and
  `swift test` on a macOS runner, then unsigned `xcodebuild` of all three app
  targets (iOS, macOS, watchOS) against the committed project.
- Known gaps: `KNOWN-ISSUES.md`.
