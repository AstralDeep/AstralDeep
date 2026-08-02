# Contract: Apple Release Pipeline (`.github/workflows/apple-release.yml`)

Tag-triggered, signed **App Store** release workflow that produces **two archives** —
iOS (carrying the **embedded watch app**) and macOS (**Mac App Store**) — and uploads
both into the **single Universal Purchase App Store Connect record** they share (bundle
id `com.personalailabs.astraldeep`). The pipeline stops at a **validated upload** and
reports the next step; the final **Submit for Review is operator-performed** (Apple's API
refuses an incomplete listing). A direct analogue of
[`.github/workflows/release-windows.yml`](../../../.github/workflows/release-windows.yml)
(tag-triggered, version-guarded, secret-injected, additive) on the Apple toolchain.
Realizes research decisions **D14** (workflow shape), **D19** (one-record topology),
**D5** (build-number automation), **D6** (App-Store upload flow — **no notarytool**),
**D1** (manual distribution signing in a temp keychain — **three** App Store profiles across
the two bundle ids: iOS + macOS for `com.personalailabs.astraldeep`, watchOS for `…​.watch`),
and **D7** (Mac App Store entitlements). Zero new third-party dependencies
(Constitution V) — everything is `xcrun` / `xcodebuild` / `security` from the
Apple toolchain (no fastlane, no `match`; the build number is a `xcodebuild`
build-setting override, not `agvtool`).

## Trigger

```yaml
on:
  push:
    tags: ["apple-v*"]     # Apple-scoped namespace — see below
  workflow_dispatch:
```

- **`apple-v*` deliberately — and specifically NOT `v-apple-*`**: the Windows
  release (`release-windows.yml`) fires on `tags: ["v*"]`. In GitHub Actions tag
  filters `*` matches any character except `/`, so a `v-apple-1.0.0` tag **would
  still be matched by `v*`** and would double-fire the Windows release (whose
  tag-vs-`__version__` guard then fails the run). Only a namespace that does not
  begin with `v` — `apple-v*` — is provably disjoint from `v*`. Do not "fix" this
  back to a `v`-prefixed Apple tag. (D14)
- `workflow_dispatch` allows a manual run from the `main`/default ref only;
  like the Windows precedent, the tag-vs-version guard runs **only** on `startsWith(github.ref,
  'refs/tags/')` and is **skipped** on `workflow_dispatch` (it has no tag ref).

## Runner & job shape

- `runs-on: macos-15` (matches `apple-ci.yml`; Xcode 16+ for the objectVersion-77 project).
- `timeout-minutes:` a bounded budget covering the **two** archives + export + upload;
  both signed archives must validate and upload within it (plan → Performance Goals).
- A single job (or a per-target matrix over `AstralApp` iOS / `AstralApp` macOS)
  running the ordered step contract below. The **watch app ships embedded in the
  iOS archive** (`WKCompanionAppBundleIdentifier` + "Embed Watch Content" phase, D12),
  so the iOS archive is the unit that carries watchOS; macOS archives separately for the
  Mac App Store. Both archives feed the **one** Universal Purchase record (D19).

## Permissions

```yaml
permissions:
  contents: read      # checkout only; no GitHub Release is created (App Store is the channel)
```

- **No `id-token: write`** (unlike Windows sigstore keyless signing) — Apple signing
  uses imported distribution certs + an App Store Connect API key, not GitHub OIDC.
- Least privilege: this workflow does not write to the repo, publish packages, or
  mint OIDC tokens.

## Ordered step contract

Each step is a named contract obligation, in this exact order:

1. **`actions/checkout@v5`** — source only; no submodules of signing material.
2. **Select Xcode** — pin the stable Xcode (same idiom as `apple-ci.yml`:
   `sudo xcode-select -s "$(ls -d /Applications/Xcode_*.app | sort -V | tail -1)"`).
3. **Tag-vs-`MARKETING_VERSION` guard** (D5) — on tag pushes only, assert the pushed
   tag equals `apple-v<MARKETING_VERSION>` (read from the project / xcconfig). On
   mismatch, **exit non-zero before any signing step** with an actionable message
   ("bump MARKETING_VERSION before tagging"), mirroring Windows'
   `tag == v<__version__>` guard. Named distinctly as a *release-configuration*
   failure, not a verification-gate failure (see Invariants → Constitution XI).
4. **Import distribution signing material into a temporary keychain** (D1) — decode
   `APPLE_DISTRIBUTION_CERT_P12_BASE64` and `APPLE_PROVISION_PROFILE_BASE64` from
   base64 secrets to files; `security create-keychain` → `security import` the `.p12`
   with `APPLE_CERT_PASSWORD` → `security set-key-partition-list` → install **every**
   provisioning profile into `~/Library/MobileDevice/Provisioning Profiles/`. A single
   `.mobileprovision` is per bundle-id **and** platform, so **three** App Store profiles
   are needed: **iOS** and **macOS** for `com.personalailabs.astraldeep` (the shared app
   bundle id still needs one profile per platform) **and** **watchOS** for
   `com.personalailabs.astraldeep.watch` (US1 scenario 3: the embedded watch app carries
   its own App Store profile). `APPLE_PROVISION_PROFILE_BASE64` MUST therefore carry **all
   three** `.mobileprovision` files (one base64 archive/`tar`; all installed at import).
   The keychain is **ephemeral** (created this run, deleted on cleanup); `DEVELOPMENT_TEAM`
   comes from `APPLE_TEAM_ID` via env/xcconfig, never committed.
   - **Missing-material fast-fail (FR-015 edge)**: if any required signing secret is
     absent/empty, fail *here* with a clear "missing signing material" message naming
     the missing secret **by key, never by value**, and **do not proceed to archive** —
     no unsigned artifact is ever produced and no secret bytes reach the logs.
5. **Set build number from the run** (D5) — derive `CURRENT_PROJECT_VERSION` from
   `GITHUB_RUN_NUMBER` and pass it to `xcodebuild` at archive time
   (`CURRENT_PROJECT_VERSION=$GITHUB_RUN_NUMBER` as a build-setting override, **not**
   `agvtool`), so successive archives carry distinct, monotonically increasing build
   numbers with no manual source edit (FR-008; Build-number-collision edge case).
6. **`xcodebuild archive` per target** — archive iOS (`-scheme AstralApp`, iOS
   destination, carrying the embedded watch app) and macOS (`-scheme AstralApp`, macOS
   destination) with `CODE_SIGN_STYLE=Manual`, the imported Apple Distribution
   identity, `CURRENT_PROJECT_VERSION=$GITHUB_RUN_NUMBER` (step 5), and the **Release**
   configuration (App Sandbox + Hardened Runtime for macOS per D7; ATS-clean per D3).
   Output `.xcarchive`s to the runner workspace. The workflow **asserts** the iOS archive
   contains `Watch/AstralWatch.app` and the macOS archive contains **no** watch app
   (the iOS-only embed platform filter, D20/FR-011b) — a wrong result fails the run.
7. **`xcodebuild -exportArchive` with per-platform `ExportOptions` (`method = app-store-connect`)**
   (D6) — export each archive to a signed `.ipa` (iOS+watch) / `.pkg` (macOS) using a
   **per-platform** options plist: `apple-clients/ExportOptions-ios.plist` for the iOS
   archive and `apple-clients/ExportOptions-macos.plist` for the macOS archive. Both are
   version-controlled **templates** rendered at job time by the stdlib
   `apple-clients/Scripts/render_export_options.py` (team id / profile mapping filled from
   secrets; the script exits non-zero if any placeholder has no value). The two are split
   because the iOS and macOS App-Store exports differ (product form `.ipa` vs `.pkg`, and
   the iOS plist's per-bundle-id profile mapping must name the embedded watch profile as
   well). Each sets `method` = **`app-store-connect`** (the old `app-store` spelling is
   deprecated), `teamID` = `APPLE_TEAM_ID`, `signingStyle` = `manual`, and
   **`manageAppVersionAndBuildNumber = false`** so App Store Connect does not auto-rewrite
   the run-number build we set in step 5. **This is the App-Store export path** — no
   Developer-ID export is produced.
8. **`xcrun altool --validate-app`** — validate each exported build against App Store
   Connect using the App Store Connect API key (`--apiKey $ASC_KEY_ID
   --apiIssuer $ASC_ISSUER_ID`, `.p8` materialized from `ASC_KEY_P8_BASE64`). A
   validation failure here is a **release-configuration failure**, surfaced distinctly
   from a compile/verification failure.
9. **`xcrun altool --upload-app`** — upload **both** validated builds (iOS-with-embedded-
   watch and macOS) to App Store Connect (same API-key auth). Because they share the
   bundle id, they land as the two platform versions of the **single Universal Purchase
   record** (D19), not two records. This is the "notarize/upload" of FR-015 for the App
   Store path: Apple processes and signs each build **server-side** — there is deliberately
   **NO `xcrun notarytool` step** (D6). `notarytool` is the Developer-ID *outside-store*
   path, which this feature does not produce; adding it would be incorrect for App
   Store (incl. Mac App Store) distribution.
10. **Report the next step — the operator submits** — the pipeline does **NOT** call the
    App Store Connect submission API. Pressing **Submit for Review** requires a **complete
    store listing** — screenshots for iPhone 6.9", iPad 13", Mac, and Apple Watch;
    description; privacy-policy URL; age rating — that only the operator can author, and
    Apple's API refuses an incomplete listing (FR-015a). So after a successful upload the
    workflow prints the remaining operator action (complete the listing, then Submit for
    Review **once** for the single Universal Purchase record) and stops. The pipeline's
    Definition of Done is the **validated upload of both builds**; the one-time submission
    is performed by the operator from App Store Connect.
11. **Cleanup (`if: always()`)** — delete the temporary keychain and the decoded
    cert / profile / `.p8` files even on failure, so no signing material lingers on the
    runner.

> **No notarytool anywhere.** For App Store (incl. Mac App Store) distribution the flow
> is archive → export(app-store-connect) → validate → upload → **operator submit**.
> Notarization is a distinct Developer-ID flow this feature explicitly does not use
> (D6; plan Complexity note).

## Required GitHub secrets (runtime-injected, never committed)

All seven are **CI secrets supplied at job runtime** and MUST NOT appear in tracked
files or be baked into any image (FR-017; enforced by the existing gitleaks
secret-scan gate in [`ci.yml`](../../../.github/workflows/ci.yml)):

| Secret | Purpose |
| --- | --- |
| `APPLE_TEAM_ID` | `DEVELOPMENT_TEAM` / `ExportOptions.teamID` for manual signing (D1). |
| `APPLE_DISTRIBUTION_CERT_P12_BASE64` | Base64 Apple Distribution cert (`.p12`) imported into the temp keychain. |
| `APPLE_CERT_PASSWORD` | Passphrase for the `.p12` import. |
| `APPLE_PROVISION_PROFILE_BASE64` | Base64 App Store provisioning profiles. MUST cover **three** pairs: iOS + macOS (both `com.personalailabs.astraldeep`) and watchOS (`…​.watch`) — a `.mobileprovision` is per bundle-id **and platform**, so the shared bundle id still needs one profile per platform. Carry all three `.mobileprovision` files in one base64 archive (all installed at import). |
| `ASC_KEY_ID` | App Store Connect API key id (`--apiKey`). |
| `ASC_ISSUER_ID` | App Store Connect API issuer id (`--apiIssuer`). |
| `ASC_KEY_P8_BASE64` | Base64 App Store Connect API private key (`.p8`), materialized at runtime for `altool` / submit. |

Secrets are referenced only as `${{ secrets.* }}`, decoded to files under the runner
workspace, and deleted in the cleanup step. Logs name secrets **by key, never by value**.

## Invariants

- **Existing gates untouched (FR-016, Constitution XI, SC-006)**: this workflow is
  purely **additive**. The six backend CI gates in `ci.yml` (lint / build / test /
  coverage-gate / smoke / secret-scan, plus the main-only publish) and the unsigned
  Apple compile matrix in `apple-ci.yml` are **not modified or weakened**, and both
  remain green. `apple-release.yml` runs only on `apple-v*` tags / manual dispatch —
  it never gates a PR.
- **Fail-closed on missing signing material (FR-015 edge)**: absent/invalid cert,
  profile, or ASC key ⇒ a **clear failure at the import/validate step** with no
  unsigned or partial artifact emitted and **no secret bytes leaked** to logs.
- **Distinguishable failure classes (Constitution XI)**: a *release-configuration*
  failure (tag-vs-version mismatch, missing secret, signing / validation / upload
  error) is surfaced by a distinctly-named step and message, separable from a
  *verification* failure (a compile break — `apple-ci.yml`'s job — or a drift-guard
  break in `swift test`). The release workflow does not re-run or subsume the
  verification gates.
- **No new dependency (FR-026, Constitution V)**: only `xcodebuild`, `xcrun altool`, and
  `security` from the Apple toolchain — build number via `xcodebuild`'s
  `CURRENT_PROJECT_VERSION` override (not `agvtool`); no fastlane, no `match`.
- **Build-number monotonicity (FR-008)**: `GITHUB_RUN_NUMBER`-derived build numbers are
  monotonic and collision-free across runs; App Store Connect never sees a duplicate.

## Requirement → step map

| Requirement / criterion | Where satisfied |
| --- | --- |
| **FR-015** archive → export → upload → submit the **two** builds (iOS+embedded watch, macOS) into the **one** Universal Purchase record; fail-clear on missing material | Steps 6–10; two archives (step 6), one record (step 9), single submit (step 10); missing-material fast-fail in step 4 |
| **FR-015a** complete store listing — incl. screenshots for every required device class — before submission | Step 10 gate (operator prerequisite) |
| **FR-016** existing backend gates + unsigned Apple matrix untouched | `apple-v*`-only trigger; additive workflow (Invariants) |
| **FR-017** signing material runtime-injected, never committed | Secrets table; decode-then-cleanup; gitleaks gate |
| **SC-001** both archives pass App Store Connect upload validation with zero signing/compliance errors | Steps 7–8 (export + `altool --validate-app` per archive) |
| **SC-001a** both builds uploaded to the single record; version submitted for review once, complete listing | Steps 9–10 (upload both → submit once, listing-gated) |
| **SC-006** tag → archive → (validated) upload with no manual step; gates stay green; ≥90% changed-line coverage | Ordered steps 1–11 automated; additive posture keeps `ci.yml` coverage-gate green |
