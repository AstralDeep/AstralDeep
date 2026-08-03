# Notes for the Mac agent — environment sync + release obligations

**Written on the Windows box, 2026-08-03, branch `066-canvas-first-uiux`.**
Two tasks the user asked to be written down: **(A)** bring the local `.env` in
line with the production environment variables while keeping development
posture, and **(B)** what has to happen to the Windows, Apple and Android
releases when this branch merges to `main`.

For the UI/UX work itself read [apple-handoff.md](apple-handoff.md) and
[parity-checklist.md](parity-checklist.md) — this file deliberately does not
repeat them.

Everything below was checked against the tree at `1180eb50` and against the
live repo settings. Claims that could **not** be established are marked
`UNVERIFIED` rather than smoothed over. **This file contains variable NAMES
only — never copy a secret value into it, or into any wiki page.**

---

# (A) Syncing `.env` with production, while staying development

## The one-line summary

Copy production's **feature flags and tuning values**. Do **not** copy its
**endpoints, database URL, or host-specific addresses**. Leave
`ASTRAL_ENV=development`. The purpose is to stop the two files silently
diverging on *behavior* flags — not to make the dev box act like production.

## Read this before you start: `.env` cannot make the clients consistent

No client reads `.env`. Client endpoints are **build-time**:

- Apple → [`apple-clients/Config/Base.xcconfig`](../../apple-clients/Config/Base.xcconfig)
  (`ASTRAL_SERVER_BASE_URL = https://sandbox.ai.uky.edu`), overridden by
  `Debug.xcconfig` → `http://localhost:8001`. Note the **production Keycloak
  authority is not overridden in Debug**.
- Android → `AppConfig.kt:17,20`, release → `wss://sandbox.ai.uky.edu/ws`
- Windows → [`windows-client/deployment/release-profile.json`](../../windows-client/deployment/release-profile.json),
  digest-pinned; the dev fallback lives in `deployment.py:385-397`

"No discrepancies between the two files" is achievable for the **server**
config only. If you find yourself hunting for a `.env` knob that retargets a
client, stop — it doesn't exist.

## The three lines that actually cause damage

### 1. `DATABASE_URL` — delete it, do not merely review it

`backend/shared/database.py:137` is:

```
self.database_url = database_url or os.getenv("DATABASE_URL") or _build_database_url()
```

`docker-compose.yml` hard-sets `DB_HOST`/`DB_PORT`, but `_build_database_url()`
is only consulted **if `DATABASE_URL` is unset**. Compose never sets it, and the
local `.env` doesn't carry it. A hosted production `.env` plausibly does. Copy
it wholesale and your dev container connects to the **production database** and
runs `_init_db()` against it — writes, not reads. The only partial guard is
`SchemaRevisionError` (`database.py:307-315`), which fires only if the prod DB's
revision is neither `065.001` nor `064.001`; at matching revisions it connects
**silently**.

### 2. `ASTRAL_ENV` — and the reason "it'll just fail loudly" is wrong

`ASTRAL_ENV` unset **or unrecognized** means production
(`backend/orchestrator/session_store.py:26,35`). The boot gate
`assert_production_posture()` (`session_store.py:45-111`) exits 78 — but **only
if it finds a problem**. Its checks are `USE_MOCK_AUTH`, the three encryption
keys, the Keycloak triple, and a weak `AGENT_API_KEY`. If you copy prod's
secrets — which "copy the production variables" naturally does — **every check
passes and the box boots cleanly in production posture.**

The symptoms are then indirect and confusing: `DELEGATION_REQUIRED` defaults on
(`orchestrator.py:16976`), `mcp_authz.canonical_public_base_url()` raises because
`PUBLIC_BASE_URL=http://localhost:8001` isn't HTTPS (`mcp_authz.py:55-58`),
plaintext `ws://`/`http://` voice URLs are refused
(`backend/voice_agent/config.py:112`), and agent registrations without
`AGENT_API_KEY` are refused. **Exit 78 is the best case, not the expected one.**

### 3. `LIVEKIT_NODE_IP` — must be *your Mac's* LAN address

`docker-compose.yml:28` is
`${LIVEKIT_NODE_IP:?set the Mac host LAN address advertised for WebRTC media}`.
It is the only one of the six `:?`-required vars where a **wrong** value is
worse than a missing one: the voice session connects and there is simply **no
audio**. A missing value fails `docker compose up` immediately; a copied
production value fails silently at runtime.

## Keep local (do not inherit from production)

`DATABASE_URL` (delete) · `DB_HOST` · `DB_PORT` · `DB_NAME` · `DB_USER` ·
`DB_PASSWORD` · `PUBLIC_BASE_URL` · `BACKEND_PUBLIC_URL` · `FORWARDED_ALLOW_IPS`
· `LIVEKIT_NODE_IP` · `LIVEKIT_PUBLIC_URL` · `VOICE_WATCH_BRIDGE_PUBLIC_URL` ·
`VOICE_WATCH_BRIDGE_PORT` · `SPEACHES_URL` · `CORS_ORIGINS` · `DEBUG`

`DEBUG` earns its place: `orchestrator.py:339-342` sets the **root** log level
from it (`INFO` when true, else `WARNING`). Copying production's `false`
silences the very startup lines you would use to verify the sync worked.

Don't bother copying `ASTRAL_VOICE_CONTROL_URL` or `LIVEKIT_INTERNAL_URL` —
`docker-compose.yml:118` and `:61` hard-set both locally, so a `.env` value is
dead weight. Do keep `OPENAI_BASE_URL` and `OPENAI_API_KEY` present even though
[docs/production-deployment.md](../../docs/production-deployment.md) calls them
inert for feature 054 — `docker-compose.yml:110-111` uses `:?` on both to build
the voice worker's speech config, so removing them breaks `docker compose up`.

## Leave the Keycloak block alone — it is *supposed* to match production

The dev box already points at the production IdP by design
(`KEYCLOAK_AUTHORITY` → `https://iam.ai.uky.edu/realms/Astral`,
`KEYCLOAK_ALLOWED_AZP` → `astral-desktop,astral-mobile,astral-watch`,
`KEYCLOAK_DEVICE_CLIENTS` → `astral-watch`). Keycloak is the one category where
prod and dev are correctly identical.

**Do not blindly force `USE_MOCK_AUTH=true`.** Once `ASTRAL_ENV=development`,
`session_store.py:55-56` returns before `USE_MOCK_AUTH` is ever consumed by the
gate, so `false` is a **supported** dev configuration and several suites rely on
it. It matters specifically for you: per [CLAUDE.md](../../CLAUDE.md), **Apple
sims have no dev-token path — sign-in is real PKCE.** With `USE_MOCK_AUTH=true`,
`auth.py:148-160` base64-decodes any presented JWT **without verifying its
signature** and trusts the claims; with it `false`, the local server validates
against `KEYCLOAK_AUTHORITY` and needs `KEYCLOAK_ALLOWED_AZP` to list the native
clients — which it already does. For Apple work, `false` is the honest setting.

## The encryption keys: the risk is rotation, not posture

If the sync **changes** a local value:

- `WEB_SESSION_ENC_KEY` → every `web_session` row is invalidated (forced re-login)
- `CREDENTIAL_ENCRYPTION_KEY` → every `user_llm_config` API key is orphaned (the
  mandatory non-dismissible LLM setup dialog returns), plus every feature-063
  remote-machine credential. **Not re-derivable.**
- `AUDIT_HMAC_SECRET` → breaks chain verification and PII pseudonym stability.
  `backend/audit/pii.py:46` supports `AUDIT_HMAC_SECRET_<KEYID>` — that is the
  correct mechanism if a rotation is genuinely intended.

## Feature flags — the real point of this task

**80 distinct `FF_*` variables** are read by product code. Only **34** live in
the `shared/feature_flags.py` registry; the other **46** are read at their own
module (`orchestrator/ui_designer.py`, `personalization/*`, `dreaming/*`,
`webrender/*`, `rote/*`). Reading only the registry under-counts by more than
half.

Good news: the local `.env` currently declares **exactly those 80 names, with
zero missing and zero extra**. There is no divergence to repair today — the risk
is *introducing* one during the copy. Prove it after you edit:

```bash
grep -rhoE 'FF_[A-Z0-9_]+' --include=*.py backend/ | sort -u > /tmp/code_ff.txt
grep -oE '^FF_[A-Z0-9_]+' .env | sort -u > /tmp/env_ff.txt
comm -3 /tmp/code_ff.txt /tmp/env_ff.txt
```

Empty output means every product flag is declared. Run the same extraction
against the production file and diff the two **value** sets — that diff is the
actual deliverable of task (A).

## Procedure

1. **Back up outside the repo**: `cp .env ~/astraldeep-env-backup-$(date +%Y%m%d)`.
   Do **not** write `.env.bak.*` at the repo root — `.gitignore:90` matches only
   the exact name `.env`, so a `.bak` file is untracked, secret-bearing, and
   commit-able.
2. Copy from production **only**: `FF_*` flags, and non-endpoint tuning
   (`UI_DESIGNER_MAX_ROUNDS`, `UI_DESIGNER_TIMEOUT_SECONDS`, `DB_POOL_MIN`,
   `DB_POOL_MAX`, `JWKS_REFRESH_SECONDS`, `LOG_LEVEL`, `DRAFT_ARCHIVE_MAX`,
   `DRAFT_ARCHIVE_SKIP_SCORE`, `DEVICE_LOGIN_START_RATE`,
   `KNOWLEDGE_SYNTHESIS_INTERVAL`, `KNOWLEDGE_MIN_INTERACTIONS`,
   `DESKTOP_RELEASE_REPO`, `DESKTOP_RELEASE_TTL_SECONDS`, `MODEL_TIERS`,
   `POLICY_RULES`, `ROTE_HOST_CONFIG`).
3. Force `ASTRAL_ENV=development`. Delete any `DATABASE_URL`. Restore every
   "keep local" name above. Set `LIVEKIT_NODE_IP` to the Mac's LAN address.
4. `grep -n '^VITE_' .env` and delete any hits — `backend/shared/__init__.py:16-25`
   still copies `VITE_USE_MOCK_AUTH` / `VITE_KEYCLOAK_AUTHORITY` /
   `VITE_KEYCLOAK_CLIENT_ID` into the modern names, so a stale production line
   can flip posture invisibly.
5. Apply with a **recreate, not a restart**: `make apply-config` (`Makefile:58`).
   `shared/feature_flags.py:186` builds the registry at import and
   `orchestrator/auth.py:28` branches on `USE_MOCK_AUTH` at import — a plain
   `docker compose restart` picks up neither. (This is the same class of bug as
   the sandbox voice 503 in [voice-prod-diagnosis.md](voice-prod-diagnosis.md).)
6. Verify posture directly rather than by log-reading:
   `docker exec astraldeep printenv ASTRAL_ENV` **must** print `development`.
   Absence of a "REFUSING TO START" block proves nothing — in dev posture
   `assert_production_posture()` returns at `session_store.py:55-56` before it
   can collect a single problem, so that check is a tautology. Then `GET /readyz`
   → 200.
7. While you are here, fix the committed template drift: `.env.example` is
   missing `FF_REMOTE_COMPUTE` and `SPEACHES_URL`, and carries
   `WEB_SESSION_SECRET` and `AUDIT_RETENTION_DAYS` that the working `.env` does
   not. `.env.example` **is** source-controlled — commit that separately.
8. Never commit `.env`.

**Direction is one-way: production → local, never local → production.**
`FORWARDED_ALLOW_IPS` is `*` locally, which is harmless on a dev box and
catastrophic in the other direction.

---

# (B) Releases, on merge of this branch into `main`

## Do this in order — it is a real constraint, not advice

All three **release** builds point at `https://sandbox.ai.uky.edu`
(Android `AppConfig.kt:17,20`; Apple `Config/Base.xcconfig:11`; Windows
`deployment/release-profile.json`). Merging to `main` publishes a GHCR image
(`ci.yml` `publish` job → `sha-<commit>` + `latest`) but **deploys nothing**.

> **merge → CI publishes the image → a human deploys sandbox.ai.uky.edu →
> *then* cut client releases**

Shipping an Apple or Android build before the backend is deployed puts a
canvas-first client against a pre-066 server.

## What you do NOT have to do (checked, so you don't go hunting)

- **`backend/shared/ui_protocol.json` is untouched by 066.** This is the manifest
  three client suites pin simultaneously — `windows-client/tests/test_protocol_manifest.py`,
  Android `VocabularyParityTest.kt`, and
  `apple-clients/AstralCore/Tests/AstralCoreTests/ManifestDriftTests.swift`.
  Adding a frame type, `ui_event` action, component type, or `admission_refusal`
  code turns **all three client CI lanes red at once**. 066 adds none: the new
  `DeviceCapabilities` fields (`rote/capabilities.py:158-161`) are additive
  envelope fields, and the client re-reports via the **already-sanctioned**
  `update_device` action.
- **The voice fixture is untouched.** `backend/tests/fixtures/voice_065/client_conformance.json`'s
  SHA-256 is pinned in **five** places across two clients
  (`apple-clients/AstralCore/Package.swift:21`, `VoiceContract065Tests.swift:24`,
  `android-client/app/build.gradle.kts:87`, `VoiceFixtureBundle065Test.kt:30`,
  `VoiceConversation065InstrumentedTest.kt:57`). Touching it is an atomic
  five-file change plus an Apple manifest-time abort if you get it wrong. You
  will be editing Apple sources next to this — leave it alone.
- **No schema bump.** `SCHEMA_REVISION = '065.001'` (`shared/database.py:37-38`),
  enforced by `test_schema_revision_guard.py:24`. 066 does not touch
  `shared/database.py`. The guard's failure message is alarming; if you trip it,
  it is for an unrelated reason.
- **No static-asset version bump**, despite ~570 new lines in `client.js` —
  `orchestrator.py:750-809` derives per-file versions from content hashes.
- **No client-version handshake exists.** I grepped: `client_version` appears only
  for BYO desktop agent host registration (`user_agents.py:865-870`). There is no
  minimum-client-version refusal, so the three clients **do not** have to ship
  simultaneously. They only have to not diverge in the manifest — and they don't.

## Windows — **cannot be re-released on this merge.** Say so plainly.

Do not attempt a Windows release and do not hand anyone a procedure for one.
Four independent blockers, all verified:

1. **The chain is PR-based, not merge-based.** `release-readiness-protected.yml:30`
   requires `github.event.workflow_run.event == 'pull_request'`. A push to `main`
   after a merge **never** produces a release decision. The release candidate is
   the PR head SHA, never the post-merge commit.
2. **Every gating repository variable is empty.** `release-readiness-protected.yml:26-28`
   requires `RELEASE_READINESS_ACTIVE`, `RELEASE_EPHEMERAL_CREDENTIALS_READY` and
   `VOICE_WORKER_CLOSURE_APPROVED` to each equal `'true'`. Live check:
   `gh api repos/AstralDeep/AstralDeep/actions/variables` → `total_count: 0`.
   `RELEASE_TRUSTED_BUILDER_SHA` is unset for the same reason.
3. **The bridge signer is self-blocking in tracked files.**
   `release-windows.yml:43` requires `github.ref == 'refs/heads/main'`, while
   `:59-63` asserts `GITHUB_REF == refs/tags/$TAG`. Its only trigger is
   `workflow_dispatch`, and the publisher dispatches it with `-f ref=$TAG`
   (`release-windows-publisher.yml:621`). Dispatched at the tag the job is
   **skipped**; dispatched at main the step **exits 1**. Both conditions are
   pinned by `backend/tests/test_release_workflows_060.py` (`:1322-1340` and
   `:803`), so neither can be relaxed without changing a test.
4. **The `release-readiness-staging` environment does not exist** — only
   `release-evidence-exception` and `release-publisher` do — and the
   `release-publisher` environment has `prevent_self_review: false` with a single
   reviewer, while the publisher hard-refuses a reviewer equal to the requester
   (`release-windows-publisher.yml:519→521`). *This one is conditional*: a
   different requester would satisfy it, and it lives in live repo settings, not
   in the checkout.

A hand-made `gh release create` is not a workaround: the shipped updater verifies
a detached sigstore bundle against a hard-pinned Fulcio identity
(`windows-client/astral_client/integrity.py:43-52`), so an unsigned release is
refused by the product — and the tag would collide with the publisher's own
collision check for a later proper release.

**Version bump blast radius, for when it is unblocked**: `astral_client/__init__.py:3`
(`0.4.0`) is the read-source, but the value is *also* asserted against a hard-coded
literal at `AstralDeep.spec:61`, and carried independently in
`deployment/release-profile.json:5` and `deployment/runtime-manifest.json:4` —
plus `deployment.py:385-386` hard-codes `generic-developer-0.4.0` in the dev
profile. Four sites, one read-source. If `requirements-release.lock.txt` moves,
`backend/orchestrator/agent_generator.py:51` `BYO_RUNTIME_LOCK_SHA256` must move
in the **same commit**.

## Apple — the merge itself can trigger a release. Decide before you merge.

`apple-release.yml`'s gate job (`:64`) runs
`git diff --name-only HEAD^ HEAD | grep -q '^apple-clients/'` on a push to
`main`. If it matches, it **auto-releases**: archive → sign → export →
`altool --validate-app` → `altool --upload-app` to **App Store Connect**, with
the build stamped `MARKETING_VERSION` from the project and
`CURRENT_PROJECT_VERSION` overridden to `$GITHUB_RUN_NUMBER`.

- **This branch touches zero `apple-clients/` files**, so merging *it* will not
  fire an Apple release. **Your** merge, after the Apple UX pass, will.
- `HEAD^` is the **first parent** — the diff spans the whole branch only on a
  merge or squash commit. On a rebase-and-merge, or a direct multi-commit push,
  it sees only the final commit and can miss Apple changes entirely.
- **Land the version bump in the same merge**, not after it, or the auto-built
  upload carries the old version.

`MARKETING_VERSION` currently reads `1.1` at **ten sites** in
`apple-clients/AstralApp/AstralApp.xcodeproj/project.pbxproj` (four shipping:
lines 529, 562, 587, 613; six test targets). Do **not** touch
`CURRENT_PROJECT_VERSION` — CI overrides it (`apple-release.yml:274,288`). The
tag guard (`:111-124`) requires the tag to be exactly `apple-v<MARKETING_VERSION>`;
`apple-v1.0` is already taken, so at `1.1` the tag is `apple-v1.1`. **Never use a
`v`-prefixed tag for Apple.** Stale docs still claim `1.0`:
`specs/053-apple-production-release/operator-prerequisites.md:124` and
`tasks.md:70`.

Two traps:

- **Nothing forces `apple-ci` green before a release.** `apple-release.yml` has no
  `needs:`, no `workflow_run:`, and no repo-variable gate. Check it yourself.
- **The two lanes use different toolchains.** `apple-ci.yml:22-23` pins Xcode
  `26.6` build `17F113` on `macos-26`; `apple-release.yml:76,82` runs `macos-15`
  and picks Xcode with `ls -d /Applications/Xcode_*.app | sort -V | tail -1`. A
  green local build proves nothing about the release lane's toolchain, which is
  unpinned and has not been exercised since 2026-07-09.

The `apple-v1.0` release **did** succeed — run `29036053155`,
`VERIFY SUCCEEDED: 2 / UPLOAD SUCCEEDED: 2 / FAILED: 0`, recorded at
`specs/053-apple-production-release/verification/implementation-evidence.md:182-192`.
So the path works; it is the *toolchain drift* since then that is unproven.

Manual, Mac-only, not automatable: exporting the `.p12` identities from Keychain
Access (a bare `.cer` imports but signs nothing — `apple-release.yml:174-178`
fails on exactly that), creating the three provisioning profiles, the App Store
Connect listing, and pressing **Submit for Review** (the workflow deliberately
stops at a validated upload and prints a notice, `:369-373`).

**Screenshots — read before touching them.** `066` reshaped the client UX, so the
committed store screenshots may now misrepresent the app. But
`apple-clients/Scripts/prepare_screenshots.py` **cannot** be used the obvious way:
it exits 2 with no `--source`; its `CLASSES` hardcodes `iphone-6.5`
(1284×2778) and cannot emit a 6.9" set; and its `MANIFEST` maps captures by
hardcoded `HH.MM.SS` stamps from the original capture session, so a fresh capture
does not match. The 13 committed PNGs do pass `--check`. Treat re-capture as its
own task with a script change, not a one-liner. `UNVERIFIED`: whether Apple still
accepts a 6.5" set where the repo's own doc says 6.9" is required.

## Android — there is no automated release path, by design

`android-ci.yml` lints, unit-tests, coverage-gates, assembles a **debug** APK and
runs instrumented tests. The only uploaded binary is `app-debug.apk`. Repo-wide,
`bundleRelease`/`assembleRelease` appear **only** in the runbook
[`android-client/docs/play-store-release.md`](../../android-client/docs/play-store-release.md),
never in a workflow. Even `release-readiness.yml`'s android producer bundles
`app-debug.apk` as evidence.

This branch **does** touch `android-client/`, so `android-ci` will fire on merge —
producing nothing shippable. Note also that three of the six trigger paths are
*backend* paths (`ui_protocol.json`, the voice fixture, the canary test), and a
nightly `cron` builds regardless of any merge.

The release is a 100% manual local build:

1. `versionCode = 4`, `versionName = "1.2"` at `app/build.gradle.kts:119-120`.
   Bump `versionCode` above the true Play Console high-water mark — `UNVERIFIED`:
   the repo value is only what was last committed and is **not** authoritative
   for what was uploaded.
2. **`./gradlew` is not executable** — `git ls-files -s android-client/gradlew`
   returns mode `100644`, and there is no `100755` blob anywhere under
   `android-client/`. On a fresh Mac clone it dies with `permission denied`. Use
   `sh ./gradlew` or `chmod +x`.
3. JDK 17 (temurin, matching `android-ci.yml:40-43`), Android SDK platform 35 for
   AGP 9.2.1, `android-client/local.properties` with `sdk.dir=…`, **and
   `sdkmanager --licenses`** — AGP fails configuration without accepted licenses.
4. `keystore.properties` with `storeFile`/`storePassword`/`keyAlias`/`keyPassword`.
   **If it is missing, `signingConfigs.findByName("release")` returns null
   (`app/build.gradle.kts:152`) and the build silently emits an UNSIGNED bundle**
   that Play rejects. This is the single most likely silent failure.
   `UNVERIFIED` and needs resolving first: the runbook documents
   `%USERPROFILE%\.android-keys\astral-upload.jks` alias `astral-upload`, but the
   only keystore found on the Windows box is at
   `Desktop/Containers/Android_Keystores/upload-keystore.jks` alias `upload`, and
   the documented directory does not exist. Confirm with `keytool -list -v`
   against the certificate registered in Play Console → App signing **before
   building anything**.
5. `sh ./gradlew :app:bundleRelease` → `app/build/outputs/bundle/release/app-release.aab`.
   **Expect a first-run dependency-verification failure and do not panic**: this
   is the first `bundleRelease` since dependency verification was reintroduced on
   2026-08-02, and `releaseRuntimeClasspath` is a 149-module strict subset of
   debug's 156 — so it is *not* proven identical to what CI resolves.
6. If it fails on a host-classifier artifact, add that one artifact's sha256 under
   the existing `<component>` in `gradle/verification-metadata.xml`, mirroring
   `:1510-1512`. **Do not** run
   `./gradlew --write-verification-metadata sha256 …` — it regenerates the file
   wholesale and would drop the Windows `aapt2` entry that was just added.
   **Do not** pass `--no-verify` or delete the file; deleting it is what commit
   `341d6d7a` did and it had to be reverted. The macOS `aapt2` classifier is
   already pinned at `:1507-1509`, so aapt2 itself should resolve.
7. Verify it is actually signed:
   `keytool -printcert -jarfile app-release.aab`, check Owner CN against the
   registered upload certificate.
8. Smoke-test on a device (`adb uninstall com.personalailabs.astraldeep` first —
   debug, locally-signed release, and Play-re-signed artifacts carry three
   different signatures), then upload by hand at Play Console → Internal testing.
   There is no API upload, no fastlane, and no service-account key in this repo.

`UNVERIFIED`: whether AGP 9.2.1 resolves a separate R8/D8 artifact for a release
bundle — there is no `com.android.tools:r8` component anywhere in
`verification-metadata.xml` or any lockfile, and `isMinifyEnabled = false`.

---

## Summary table

| Platform | Merge auto-triggers? | Can it be released now? | Version site |
|---|---|---|---|
| **Windows** | No (chain is PR-only) | **No — four blockers**, see above | `astral_client/__init__.py:3` + 3 mirrors |
| **Apple** | **Yes**, if the merge diff touches `apple-clients/**` | Yes, if the 12 secrets are set | `project.pbxproj` ×10, currently `1.1` |
| **Android** | Builds debug only | Manual local build → Play Console | `app/build.gradle.kts:119-120`, `4` / `1.2` |

## Provenance

Established by a 13-agent workflow on 2026-08-03: four parallel readers (one per
area), two independent adversarial verifiers per area with distinct lenses
(evidence-checking and procedure-walking), and a completeness critic. Every
verifier returned `PARTIALLY_REFUTED` or `REFUTED`; their corrections are
incorporated above rather than the researchers' original claims. The
load-bearing facts — the readiness variable gate, the bridge self-block, the
Android version and `gradlew` file mode, the `DATABASE_URL` precedence, and the
Apple `MARKETING_VERSION` — were then re-checked by hand against the tree.
