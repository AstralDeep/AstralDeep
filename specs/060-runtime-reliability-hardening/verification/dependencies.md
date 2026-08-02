# Spec 060 Dependency and Workflow Audit

**Feature**: 060 Runtime Reliability and Release Readiness
**Branch**: `060-runtime-reliability-hardening`
**Recorded**: 2026-07-16 (America/New_York)
**Status**: incomplete; T126 remains open

This is a read-only audit of the current local candidate tree. It records the
dependency isolation that is already proved and the immutable-supply-chain work
that still blocks Constitution Principle V and the protected release path. It
is not lead-developer approval, a protected-workflow attestation, or release
authorization.

## Decision

T126 cannot close yet. Every third-party `uses:` occurrence in the workflow
tree — including the T107/T119 additions audited below — is an immutable
commit-SHA reference with an adjacent version comment. The local evidence
wrapper (`scripts/prepare_release_evidence.py`), protected native-CI readiness
workflow set, and hardened build-once Windows bridge/publisher now exist and
their action pins are recorded below; what still blocks closure is the
lead-developer approval disposition and binding this audit to the final
candidate SHA. The 2026-07-16 owner
decision retains bounded exception/debt behavior in native protected CI but
removes the proposed repository-scoped GitHub Apps, installation tokens, and
custom token broker; the implemented workflows use only the built-in
short-lived job token behind protected environments.

The previously ad-hoc Python CI/tooling installs and the legacy Windows release
install are resolved in the candidate tree: every CI-only Python tool now comes
from one complete SHA-256 lock, the Docker test job mounts that lock read-only,
and both Windows release paths consume complete hash locks. Android now carries
Gradle dependency locking (persistent lockfiles; strict per-artifact checksum
verification is deferred to a Linux-hosted change — see the Android section).
Remaining findings are:

- Principle V lead-developer approval and disposition must be recorded in the
  feature PR after the final dependency set is frozen and bound to the
  candidate SHA and native protected-CI identities.

## Workflow action ledger

The following refs accounted for all 43 formerly unpinned occurrences. On
2026-07-16, each advertised tag was resolved directly against its official
upstream with `git ls-remote`; annotated tags were peeled to their commit
objects. The workflow files now reference these commits directly with adjacent
version comments.

| Former mutable ref | Occurrences | Pinned version | Pinned commit |
|---|---:|---|---|
| `actions/checkout@v4` | 3 | `v4.3.1` | `34e114876b0b11c390a56381ad16ebd13914f8d5` |
| `actions/checkout@v5` | 12 | `v5.0.1` | `93cb6efe18208431cddfb8368fd83d5badbf9bfd` |
| `actions/setup-python@v6` | 5 | `v6.3.0` | `ece7cb06caefa5fff74198d8649806c4678c61a1` |
| `actions/setup-node@v6` | 1 | `v6.5.0` | `249970729cb0ef3589644e2896645e5dc5ba9c38` |
| `actions/upload-artifact@v4` | 6 | `v4.6.2` | `ea165f8d65b6e75b540449e92b4886f43607fa02` |
| `actions/download-artifact@v4` | 5 | `v4.3.0` | `d3f86a106a0bac45b974a628896c90dbdf5c8093` |
| `actions/setup-java@v4` | 3 | `v4.8.0` | `c1e323688fd81a25caa38c78aa6df2d33d3e20d9` |
| `docker/setup-buildx-action@v3` | 1 | `v3.12.0` | `8d2750c68a42422c14e847fe6c8ac0403b4cbd6f` |
| `docker/build-push-action@v6` | 1 | `v6.19.2` | `10e90e3645eae34f1e60eeb005ba3a3d33f178e8` |
| `gitleaks/gitleaks-action@v2` | 1 | `v2.3.9` | `ff98106e4c7b2bc287b24eaf42907196329070c7` |
| `docker/login-action@v3` | 1 | `v3.7.0` | `c94ce9fb468520275223c153574b00df6fe4bcc9` |
| `gradle/actions/setup-gradle@v4` | 2 | `v4.4.3` | `ed408507eac070d1f99cc633dbcf757c94c7933a` |
| `reactivecircus/android-emulator-runner@v2` | 1 | `v2.38.0` | `a421e43855164a8197daf9d8d40fe71c6996bb0d` |
| `softprops/action-gh-release@v2` | 1 | `v2.6.2` | `3bb12739c298aeb8a4eeaf626c5b8d85266b0e65` |

On 2026-08-02 the organization-licensed `gitleaks-action` row above became
historical: `ci.yml` replaced it with the official Gitleaks `v8.30.1` Linux x64
release archive, pinned by SHA-256
`551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb`.
CI verifies the archive before extraction and executes the CLI over `--all`
history without exposing `GITLEAKS_LICENSE` or another repository secret to a
candidate workflow. `.gitleaksignore` contains only 12 exact
commit/path/rule/line fingerprints for reviewed historical false positives; it
does not add a path, rule, regex, or future-finding wildcard.

Added by the T107/T119 protected-trust implementation on 2026-07-16 (both
resolved live against their official upstreams before pinning):

| Action | Occurrences | Pinned version | Pinned commit | Purpose |
|---|---:|---|---|---|
| `actions/attest-build-provenance` | 2 | `v4.1.1` | `0f67c3f4856b2e3261c31976d6725780e5e4c373` | Trusted-builder + exception-registrar manifest attestation |
| `sigstore/gh-action-sigstore-python` | 1 | `v3.4.0` | `5b79a39c381910c090341a2c9b0bf022c8b387e1` | Bridge keyless detached signing + in-action self-verify |

The six workflows named by the 2026-07-16 owner decision now exist
(`release-readiness.yml`, `release-trusted-builder.yml`,
`release-evidence-exception.yml`, the rewritten `release-windows.yml` bridge,
`release-windows-publisher-controller.yml`, `release-windows-publisher.yml`);
every third-party `uses:` in them is commit-SHA pinned with a version comment
and `backend/tests/test_release_workflows_060.py` enforces the pin format plus
the absence of any app-token action. The bridge contains no package install;
the publisher installs only the complete hashed Windows release lock to run the
shipped v0.3.0 verifier.

Already immutable before this remediation:

- Apple CI uses five `actions/checkout` references pinned to
  `11bd71901bbe5b1630ceea73d27597364c9af683` (v4.2.2) and four
  `actions/upload-artifact` references pinned to
  `ea165f8d65b6e75b540449e92b4886f43607fa02` (v4.6.2).
- The Windows candidate workflow pins one `actions/checkout`, one
  `actions/setup-python`, and two `actions/upload-artifact` references to
  commits recorded in the table above.

## Manifest delta and isolation evidence

### Browser CI tooling

`tooling/web-ci/package.json` declares six exact development dependencies. The
lockfile contains 81 package-path entries (six direct and 75 transitive) with
SHA-512 integrity metadata. npm is pinned through `packageManager`; install uses
`npm ci --ignore-scripts`. The package is private and CI-only, `node_modules`
is ignored, and product-isolation contract tests prove that this graph is not
copied into the backend image or a shipping client.

- `tooling/web-ci/package-lock.json` SHA-256:
  `229de79c244eb8cbd66f8936b24b22dbbe373d37c2bfbaa622eecceb8f8dae86`
- Playwright image declaration SHA-256:
  `f618914b6c43f617bc1dbda7c620094eb06458729df6581d6e3cd0b4bc63e48d`
- Declared image: official Playwright 1.61.1 Noble image pinned to repository
  digest
  `sha256:5b8f294aff9041b7191c34a4bab3ac270157a28774d4b0660e9743297b697e48`.

### Python CI tooling

`tooling/python-ci/requirements.in` declares six exact direct CI-only packages.
`requirements.lock.txt` contains 15 exact distribution blocks (six direct and
nine transitive or platform-conditional), and every block carries one or more
SHA-256 artifact hashes. The lint, release-tooling, Windows-source-test, backend
coverage, and changed-coverage jobs install this lock with
`python -m pip install --require-hashes`; no workflow names a test/tool package
directly. The backend coverage container receives only a read-only mount of the
tooling directory, so the manifest is not added to the image.

The Windows source suite now runs on `windows-latest`, where it installs the
Windows-targeted product/build graph from `requirements-release.lock.txt` with
`--require-hashes` before adding the separate CI-tool lock. This is intentional:
the release lock's wheel hashes are platform-specific and a clean Linux install
fails closed on the first non-Windows wheel instead of silently selecting an
unreviewed artifact. No unhashed Python install remains in `ci.yml`.

The Windows candidate is built from the independently compared release-lock
environment before the CI-only lock is installed for packaged/source testing.
The legacy Windows release workflow no longer upgrades pip, installs the
developer convenience manifest, or admits a Sigstore version range; it consumes
the complete Windows release lock with `--require-hashes`.

- `tooling/python-ci/requirements.in` SHA-256:
  `d4b2d288fe8cb11b805ed1f37375cdc583ac7e4c5cf23c4d17e284f459285064`
- `tooling/python-ci/requirements.lock.txt` SHA-256:
  `53a13f8fdc29757212ffe792ce361a8e17ef4e4acd52c3f723b726c84f62d15f`
- Product-isolation contracts prove the CI-only manifest is absent from the
  Docker build inputs, Windows PyInstaller manifest, Android/Apple manifests,
  and product runtime requirement manifests.

### Windows packaging

The shipping input manifest has eight exact direct packages. The release lock
contains 62 hash-locked wheel rows (eight direct and 54 transitive), and the
runtime manifest binds both input and lock digests before packaging. The
PyInstaller specification bundles the input manifest, hash lock, deployment
profile, and runtime manifest; Node/Playwright tooling is absent.

- `windows-client/requirements.in` SHA-256:
  `8c14423afc61a216ec6ea49f891f1f45e60095f0616835aa377a9037a8f6a155`
- `windows-client/requirements-release.lock.txt` SHA-256:
  `6041036906881c59868b9e53e16d1e22d8371b68af2f36701022a5a239dd43ba`

### Android

The feature updates AndroidX Test Ext 1.2.1 to 1.3.0, Espresso 3.6.1 to 3.7.0,
and Kover 0.8.3 to 0.9.8, and declares Kotlin test/JUnit 2.2.10. These serve
instrumentation, coverage, and unit-test compatibility. On 2026-07-16 the
Android client gained Gradle **dependency locking** (persistent lockfiles),
which pins the exact resolved module coordinates and fails the build
fail-closed on any version drift. This is the reproducibility control the
audit gap required and it is OS-independent (lockfiles record
`group:name:version`, not per-platform artifacts).

**Strict per-artifact checksum verification deferred.** A first attempt also
committed `gradle/verification-metadata.xml` (sha256 per artifact), but Gradle
verification metadata generated on one OS cannot cover another OS's
classifier-specific artifacts: Android's build tools ship per-platform
binaries (`com.android.tools.build:aapt2:…-linux.jar` vs `…-windows.jar`, and
Android Lint's native components), so a Windows-host generation is structurally
incomplete for the Linux CI runner (CI failed verification on
`aapt2-9.3.0-15703166-linux.jar`). Complete cross-OS metadata requires
resolving on each target OS; that is deferred to a Linux-hosted release-infra
change. The lockfiles alone satisfy the reconstructable-graph/reproducibility
requirement; the metadata file was removed to keep the gate honest rather than
carry a Windows-only, CI-broken checksum set.

**Dependency locking** — `dependencyLocking { lockAllConfigurations() }` for
all modules plus buildscript-classpath locking in
`android-client/build.gradle.kts` (settings classpath activation in
`settings.gradle.kts`). Lock state, 509 distinct module coordinates total:

- `android-client/app/gradle.lockfile`: 436 module entries across 39
  dependency-bearing configurations plus 31 locked-empty configurations.
- `android-client/core/gradle.lockfile`: 77 module entries across 19
  dependency-bearing configurations plus 13 locked-empty configurations.
- `android-client/buildscript-gradle.lockfile`: 129 module entries in the root
  plugin `classpath` configuration.
- `android-client/settings-gradle.lockfile`: records only the empty
  `incomingCatalogForLibs0`. Gradle resolves the settings `plugins {}` request
  outside the lockable settings classpath, so the foojay resolver cannot
  receive lock state; it is exactly version-pinned in `settings.gradle.kts`.

The lockfiles were generated over the full android-ci build-test task graph
plus the instrumented job's androidTest compile/packaging resolution
(`sh ./gradlew --write-locks ktlintCheck :app:lintDebug :core:test
:app:testDebugUnitTest :core:koverVerify :app:koverXmlReport
:core:koverXmlReport :app:assembleDebug :app:compileDebugAndroidTestKotlin
:app:assembleDebugAndroidTest --no-daemon --no-configuration-cache`) and
proved to build fail-closed on version drift from a fresh Gradle user home. No
repository was added or re-trusted (google(), mavenCentral(),
gradlePluginPortal() for plugin management only — unchanged).

- `android-client/app/gradle.lockfile` SHA-256:
  `5bb036a5a90dc953b7dd1bdeaec762e02625c08f6c186888b1e9d9a8bcca65d0`
- `android-client/core/gradle.lockfile` SHA-256:
  `aee1fb50d70d15c9c7be9def38101135e607b42ba440477c6a8e3043333cfc49`
- `android-client/buildscript-gradle.lockfile` SHA-256:
  `16a25d21d1173cac794ae3e68bc577df2f7814fe4ae77881b0cf195645c69878`
- `android-client/settings-gradle.lockfile` SHA-256:
  `5e2d075903b5cd264613e7538c7c51b1484fe2ed489d4ead3e6b4ba0cf3911c4`
- `android-client/gradle/libs.versions.toml` SHA-256:
  `6635125f17b5c8126dcdf043f35b4994efa9ef27a13135f6a4517dbb75ffbe6b`
- The AGP/Gradle major-10 canary remains explicitly `UNRELEASED`; no guessed
  version, URL, or checksum is treated as evidence.

### Apple and backend runtime

AstralCore still has no third-party package dependency. The backend runtime
requirements and Dockerfile dependency layer have no Spec 060 product-runtime
dependency addition. The new Python and JavaScript tooling is exercised only
through isolated test/CI surfaces.

## Local validation

On 2026-07-16, the Python CI lock installed with `--require-hashes` in a clean
official `python:3.11-slim` Linux container; `pip check` reported no broken
requirements and the six direct tool versions matched the input manifest. The
same locked install plus `pip check` also passed in the existing local
`astraldeep:latest` dependency environment as a compatibility smoke (not
candidate-bound release evidence). Ruby's YAML parser accepted all three
changed workflows. The exact release-tooling selection then ran from the hash
lock under clean Python 3.11/Linux: 226 tests passed and every maintained
`scripts/*.py` executable was measured, producing 93% aggregate coverage
(individual files ranged from 90% to 100%). Ruff and `git diff --check` passed
for the changed files.

## Required re-audit before T126 can close

The former item 2 (Gradle dependency locking) was resolved on 2026-07-16 — the
lockfile facts and digests are recorded in the Android section above; strict
per-artifact checksum verification is deferred to a Linux-hosted change (its
metadata cannot be generated completely from a Windows host). Still open:

1. The T107/T119/T120 action pins are recorded above. Re-confirm no new
   third-party action entered the local-evidence/readiness/native-publisher
   paths after the final rebase.
2. Re-run product-artifact isolation checks against the reviewed protected-CI
   workflow bytes and exact candidate artifacts; prove no custom App/broker
   credential path exists.
3. Record the lead-developer approval disposition in the feature PR, then bind
   this audit to the candidate SHA and native protected-CI identities.
