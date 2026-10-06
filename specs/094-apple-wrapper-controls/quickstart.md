# Qualification Quickstart

Confirm task branches and task-owned changes in Deep and sibling Projection. Record exact candidate revisions before final qualification. Use Python 3.11 virtualenvs.

```sh
cd ../AstralProjection
.venv/bin/python -m pytest tests/rote/test_adapter_contracts.py -q
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest windows-client/tests -q
swift test --package-path apple-clients/AstralCore
```

Use schemes/destinations from `apple-clients/README.md` for affected iOS/macOS tests/builds and watch shared-code build. Use the booted iPhone 17 Pro where appropriate. Normal owner-completed PKCE only.

```sh
cd android-client
JAVA_HOME='/Applications/Android Studio.app/Contents/jbr/Contents/Home' ./gradlew ktlintCheck :app:lintDebug :core:test :app:testDebugUnitTest :core:koverVerify :app:assembleDebug
```

Run narrow Deep chrome/Drafts/selection tests against candidate Projection source, recording imports explicitly; then affected module/parity suites, changed-line coverage and tracked lint. Source tests do not mean the installed/deployed component has changed.

Compare the same role/content/theme with web. Open every ordinary destination; exercise provider actions with disposable fixtures, all themes/arbitrary colors, filters/prompt loading, Drafts/revisions, Advanced, cancellation and first failure/recovery. Send no query, upload, delete or expand permissions. The owner separately authorized supplied provider credentials in the requested preview form; keep them encrypted in-product and out of fixtures, logs, source and handoff artifacts. Verify Save acknowledges storage before testing and preserves configuration with a distinct warning on external failure. Record outcomes/adaptations at phone/tablet/desktop sizes and increased text size. Unavailable live evidence remains pending.

Android's current debug HTTP endpoint cannot pass the unchanged HTTPS-only custody setup. Do not relax TLS or introduce an auth fallback for qualification. The existing release variant can be built normally and a temporary unsigned APK copy test-signed for diagnostic verification against the supported HTTPS sandbox. This does not qualify the repaired local backend.

Update transformation provenance and run its guard. Keep product changes, adoption status and curated vault separate. Do not push/deploy/merge/release product repositories without authorization.
