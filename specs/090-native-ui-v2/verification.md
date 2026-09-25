# Native UI v2 verification

## Source and toolchain

Baseline Deep `341f58c5625d7dda530720e4cac6b3d35ae47492`, embedded Projection `7cb7e25483e023df68f3c6a762ef87b2e36417fb`. Both feature branches are local `codex/090-native-ui-v2`, unpushed. Owner confirmed updates ready on September 24, 2026. Docker 29.8.0; Xcode 27.0 (27A266a), Swift 6.4; available iOS/watchOS 26.5 simulators. Local Xcode differs from hosted CI's pin.

## Current backend and browser

- `make composition-preflight PYTHON=python3`: passed, composition digest `0c060a74e6e8c9d761a1e193d0923c84e4c83227bb835a01884cb36338232a55`.
- Fresh ARM image build initially failed for existing aicspylibczi due to missing OpenSSL development libraries. Added `libssl-dev` to Dockerfile build prerequisites. `docker build -t astraldeep:ui-v2-reference .`: passed. Image `sha256:e5223ebf731f68d4f4b06c284e92385dc575b27396a62a7e7a7a2f9868625048` contains baseline UI source plus this build-prerequisite change.
- `docker compose -f docker-compose.yml -f /tmp/astral-native-ui-v2-compose.yml up -d --no-build astraldeep`: passed. Temporary override selects the reference image. Backend, PostgreSQL and LiveKit running; backend/PostgreSQL healthy.
- `/healthz`: OK; `/readyz`: OK, database/publication OK, ten agents, local LETS disabled by existing configuration.
- `docker exec astraldeep python scripts/install_local_components.py verify --root /app --lock /opt/astral-component-wheels/astral-component-wheels.lock.json`: four components verified.
- Served/source SHA256 equality: client.js `0b1931672e4e38f6552ea58396cb3734f65a4e2d566127a0b2bc6c4c33ece706`; astral.css `089ccbe370dcaeed91fd9caf104015e603667bb21968a335f185199164c44615`.
- Owner completed normal web Keycloak sign-in. Landing/settings captures inspected at seven required sizes; phone drawer, composer More and desktop agent intro/error also captured. Existing History collapsed for screenshots. Assets and hashes live in kos-wiki `assets/astral-native-ui-v2/web-reference/`.
- Ordinary dice scenario dispatch reached the configured upstream model, which returned HTTP 404 `model_not_found`. Error reference is real; successful result/fullscreen references remain pending owner model configuration. No credentials were read or changed.
- Voice worker was not yet running during initial captures. The owner explicitly excludes the bottom web voice warning from native UI; native worker connection/recording/playback/recovery remain required.

## Unchanged Apple baseline

- `swift test --package-path components/AstralProjection/apple-clients/AstralCore --enable-code-coverage`: 254 tests, five failed assertions. ManifestDriftTests expects 35 components/136 actions versus current 41/138; PrimitivesTests lacks six v2 mirror types; WatchPresentation088Tests expects a weather emoji while current fixture uses `WX`. These are baseline failures to resolve, not accepted waivers.
- `xcodebuild -project components/AstralProjection/apple-clients/AstralApp/AstralApp.xcodeproj -scheme AstralApp -destination 'platform=macOS' -derivedDataPath /tmp/astral-native-ui-v2-macos -configuration Debug CODE_SIGNING_ALLOWED=NO build`: BUILD SUCCEEDED before native changes.

## Remaining qualification

Shared contract, native implementation, changed coverage, CI-equivalent suites, native signed-in visual comparison and live voice are pending. No passing hosted CI, final parity, release or deployment claim.


## September 25 complete references and published collaboration checkpoint

- All54 planned screenshots are visually inspected, their PNG dimensions/SHA256 verified, and the complete7-state ×7-size matrix plus drawer/error references pushed to kos-wiki `92e4ed0c8254df10f0fc196c0aa8a11fb432ecae`. Remote main was independently queried and matches. Native UI edits paused for this prerequisite and resumed only after the verified push.
- Main and system models persist as `zai-org/GLM-5.3-Flash` after reopening normal product settings. Real dice dispatch completed with model200, delegated authorization, roll_dice success and final model200; six rolls5,3,3,2,2,1 total16 supply the result references. Ordinary reload refreshed an expired IAM session that had produced skill_lookup_unavailable; no bypass or provider secret handling.
- Voice worker build/start and exact ASR/TTS preflight pass; authenticated voice status reports ready. Native recording/playback/recovery are not yet verified.
- `xcodebuild -project components/AstralProjection/apple-clients/AstralApp/AstralApp.xcodeproj -scheme AstralApp -destination 'platform=macOS' -derivedDataPath /tmp/astral-native-ui-v2-macos -configuration Debug CODE_SIGNING_ALLOWED=NO build`: BUILD SUCCEEDED with partial console shell/composer/result implementation; log `/tmp/astral-native-ui-v2-console-build.log`.
- Changed Python source/tests: Ruff check passed. Changed Swift files:38 formatted and strict-linted with the temporary configuration merging Xcode27 required defaults; the repository's hosted-toolchain formatting gate still requires final qualification.
- Earlier verification:52 real-browser tests passed; Core278 passed before watch-preparation changes;29 macOS primitive/export/work-renderer tests passed, export changed-line coverage96.43%. Final palette/sizing changes and current watch-preparation/core changes require reruns. Root console shell/state tests and final changed-line coverage remain open.
- Shared targeted watch/presentation/menu tests264 passed; watch/guidance/console-label tests87 passed. Six earlier Deep session-authority fixture failures pass in isolation; latest combined real-PostgreSQL suite is being rerun. Do not treat prior combined failure as waived.
- `handoff-windows.md` records the owner's new branch-publishing request. Product publication is a work-in-progress collaboration checkpoint, not final CI, parity, merge, deployment or release. Windows source remains unchanged; watch client negotiation remains disabled until its implementation is ready.

- Publication-freeze follow-up: full AstralCore `swift test --package-path apple-clients/AstralCore --scratch-path /tmp/astral-core-console-v2 --enable-code-coverage` passed278/278 after truthful default-network fixture corrections, log `/tmp/astral-core-watch-freeze.log`. Changed browser helper/spec files pass ESLint with zero warnings.
- Combined Deep shared-surface suite completed152/152 against disposable PostgreSQL17, log `/tmp/astral-watch-surfaces-tests.log`; changed executable lines in native_console/guidance/agent_intro/ChromeSurface are41/41 covered. Two newest watch PostgreSQL cases were added after that collection and remain to run. The previous six failures are resolved by passing rerun without weakening authorization.
