# Quickstart: Runtime Reliability and Release Readiness (060)

This is the implementation and release-candidate verification runbook for feature 060. Run commands
from the AstralDeep repository root unless a section changes directory. The 060-specific files named
below (the migration/performance tests, packaged-release test, deployment validator, documentation
link checker, evidence validator, and release-readiness workflow) are implementation deliverables;
their absence, an empty pytest selection, or missing platform evidence is a failure rather than a
reason to skip the gate.

Security behavior is not changed by this feature. Use existing development or release-owner-provided
Keycloak/provider configuration, never print tokens or API keys, and enter personal LLM credentials
only through the product UI.

## 1. Prerequisites and candidate identity

- Checkout `060-runtime-reliability-hardening` and verify the candidate commit before collecting
  evidence. Evidence from different commits or rebuilt artifacts cannot be combined.
- Docker Desktop and Compose must be available. The repository `.env` must already contain a valid
  development posture; do not source or print it.
- Host Python 3.11 and Ruff are required for source checks. Node.js 24 plus Corepack-resolved,
  integrity-pinned npm 11.16.0 are required only for the
  isolated lock-pinned ESLint package; they are never installed into the product image. Browser
  automation runs in the exact digest from `tooling/web-ci/playwright-image.txt`, which must match
  the Playwright lock and pins Chromium plus Linux dependencies; a host browser is not a substitute.
- Android requires Android Studio's JBR, the SDK recorded in
  `android-client/local.properties`, and one connected emulator/device.
- Apple requires macOS and Xcode 26.6 (build 17F113 for this pickup) with the supported iOS/watchOS
  26.5 simulator runtimes. Apple first-login evidence must run on macOS and iOS and is non-waivable
  for this rejection-remediation release.
- Windows artifact proof must run on an actual clean Windows 10+ runner with Python 3.11. Source
  tests on macOS/Linux do not substitute for the frozen EXE checks.
- The release owner must supply the reviewed, non-secret Windows profile at
  `windows-client/deployment/release-profile.json`. It must use the approved authenticated UI-tunnel
  disposition and must not contain a shared agent credential.
- Candidate-staging proof requires the sanitized representative 057.001 database fixture and the
  real configured Keycloak/background/scheduler posture described below on a shared, TLS-reachable
  staging host. An empty database, mock auth, runner-local deployment, or source-only process is not
  an acceptable substitute.

```bash
git status --short --branch
test -z "$(git status --porcelain)"
git branch --show-current
git rev-parse HEAD
docker version
docker compose version
mkdir -p build/060/coverage build/060/release-evidence
```

The clean-tree assertion is mandatory for candidate evidence (ordinary implementation testing may
run before it). Record `git rev-parse HEAD` as `candidate_sha` in every evidence file. Ordinary
implementation verification must not create a tag, sign, upload, or replace official release assets.
The final T128 integration exercise may create/sign/upload only three assets under a temporary
same-repository tag/draft, must keep the draft non-public, and must delete exactly its state-owned
draft/tag during cleanup; that narrow exercise is not an authorization to publish a release. It
remains inactive while ruleset `19078549` prevents the required tag deletion.

## 2. Backend boot, focused checks, and full CI-parity suite

Build and start the checked-out source, then prove both liveness and readiness:

```bash
make up
docker compose ps
curl -fsS http://localhost:8001/healthz
curl -fsS http://localhost:8001/readyz
```

Ordinary source edits are not bind-mounted into the application image. Sync them before each live
pass:

```bash
make sync
curl -fsS http://localhost:8001/readyz
```

If `.env` or another boot-time value changes, `make restart` is insufficient because it reuses the
old container environment. Feature 060 adds the recreate-and-verify target below; its verification
must report only non-sensitive effective values:

```bash
make apply-config
curl -fsS http://localhost:8001/readyz
```

Run the affected existing suites plus the 060 migration suite first:

```bash
docker exec astraldeep bash -c "cd /app/backend && python -m pytest tests/test_async_tasks.py tests/test_register_ui_pipeline.py tests/test_byo_tunnel.py tests/test_byo_lifecycle.py tests/test_byo_authoring_flow.py tests/test_progress_system.py tests/test_schema_revision_guard.py scheduler/tests -q"
```

Then mirror the backend CI's separate discovery roots. The first command alone does not collect the
module-local suites because `backend/pytest.ini` has `testpaths = tests`.

```bash
docker exec astraldeep bash -c "cd /app/backend && python -m pytest -q -m 'not integration'"
docker exec astraldeep bash -c "cd /app/backend && python -m pytest audit/tests llm_config/tests orchestrator/tests onboarding/tests personalization/tests scheduler/tests dreaming/tests verification/tests -q"
docker exec astraldeep bash -c "cd /app/backend && python -m pytest tests/perf/concurrent_surfaces.py -q"
docker exec astraldeep bash -c "cd /app/backend && pip install pytest-cov 'coverage[toml]' && python -m pytest -q -m 'not integration' --cov=. --cov-report= && python -m pytest audit/tests llm_config/tests orchestrator/tests onboarding/tests personalization/tests scheduler/tests dreaming/tests verification/tests -q --cov=. --cov-append --cov-report= && python -m pytest tests/perf/concurrent_surfaces.py -q --cov=. --cov-append --cov-report=xml:coverage-060.xml"
cp backend/coverage-060.xml build/060/coverage/backend.xml
python3.11 -m venv build/060/tooling-venv
build/060/tooling-venv/bin/python -m pip install pytest 'coverage[toml]'
build/060/tooling-venv/bin/python -m coverage run --source=scripts -m pytest backend/tests/test_changed_coverage_060.py backend/tests/test_release_evidence_validator.py backend/tests/test_staging_fixtures_060.py backend/tests/test_documentation_060.py backend/tests/test_android_next_major_canary.py backend/tests/test_release_tooling_coverage_060.py -q
build/060/tooling-venv/bin/python -m coverage xml -o build/060/coverage/tooling-python.xml
ruff check .
test "$(cd components/AstralProjection/tooling/web-ci && corepack npm --version)" = "11.16.0"
(cd components/AstralProjection/tooling/web-ci && corepack npm ci --ignore-scripts)
(cd components/AstralProjection/tooling/web-ci && corepack npm run check:package-manager)
(cd components/AstralProjection/tooling/web-ci && corepack npm run check:product-isolation)
(cd components/AstralProjection/tooling/web-ci && corepack npm run lint)
(cd components/AstralProjection/tooling/web-ci && corepack npm run test:coverage-conversion)
(cd components/AstralProjection/tooling/web-ci && corepack npm run test:coverage-conversion:node)
NODE_V8_DIR="$PWD/build/060/coverage/node-v8"
mkdir -p "$NODE_V8_DIR"
(cd components/AstralProjection/tooling/web-ci && NODE_V8_COVERAGE="$NODE_V8_DIR" corepack npm run check:product-isolation)
(cd components/AstralProjection/tooling/web-ci && NODE_V8_COVERAGE="$NODE_V8_DIR" corepack npm run lint)
(cd components/AstralProjection/tooling/web-ci && NODE_V8_COVERAGE="$NODE_V8_DIR" corepack npm run test:coverage-conversion)
(cd components/AstralProjection/tooling/web-ci && NODE_V8_COVERAGE="$NODE_V8_DIR" corepack npm run test:coverage-conversion:node)
(cd components/AstralProjection/tooling/web-ci && NODE_V8_COVERAGE="$NODE_V8_DIR" corepack npm run test:coverage-union)
(cd components/AstralProjection/tooling/web-ci && NODE_V8_COVERAGE="$NODE_V8_DIR" corepack npm run coverage:node -- --node-v8-directory "$NODE_V8_DIR" --repo-root ../.. --output "$NODE_V8_DIR/interim.json")
(cd components/AstralProjection/tooling/web-ci && corepack npm run coverage:node -- --node-v8-directory "$NODE_V8_DIR" --repo-root ../.. --output "$NODE_V8_DIR/tooling-javascript.json")
PLAYWRIGHT_IMAGE="$(tr -d '\n' < components/AstralProjection/tooling/web-ci/playwright-image.txt)"
test "${PLAYWRIGHT_IMAGE#*@sha256:}" != "$PLAYWRIGHT_IMAGE"
docker pull "$PLAYWRIGHT_IMAGE"
docker image inspect "$PLAYWRIGHT_IMAGE" --format '{{json .RepoDigests}}'
docker run --rm -v "$PWD:/work" -w /work/components/AstralProjection/tooling/web-ci "$PLAYWRIGHT_IMAGE" sh -lc 'test "$(corepack npm --version)" = "11.16.0" && corepack npm ci --ignore-scripts && corepack npm run check:package-manager && corepack npm exec playwright -- --version && corepack npm run test:coverage-conversion:browser'
```

CI reports the JavaScript command separately from Ruff and runs it on pull requests and main pushes.
The package cache is keyed to `components/AstralProjection/tooling/web-ci/package-lock.json`; CI records both the Playwright
version, container digest, and pinned Chromium revision, and never falls back to a system browser.
Each platform emits its native coverage format and the final merge gate maps an immutable event-aware
base-to-candidate diff to repository-owned Python XML, Projection's lock-pinned v2 Node/browser V8
union converted and executable-syntax-filtered to canonical Istanbul statement JSON,
counter-validated Android app/core Kover XML, and line-complete Apple app/core/Watch coverage. The
JavaScript input must carry the exact `astralprojection-node-browser-union` producer and
`node-browser-union` lane identity; a browser-only or legacy producer envelope fails closed.
The protected collector rejects raw or unfiltered V8 ranges and forces text hunks for maintained
paths, so comments and candidate `.gitattributes` cannot inflate or hide coverage.
Every changed maintained language and the combined executable lines must each be at least 90%; a
missing applicable report fails rather than becoming zero selected lines.

## 3. Guarded migration and representative-data proof

Feature 074 extracted schema evolution into AstralPlane. Its guarded lineage now
owns `SCHEMA_REVISION`, the PostgreSQL advisory-lock owner, post-lock state
rechecks, idempotent DDL, current-structure verification, and recovery. Deep
retains the product policy revision and invokes Plane's atomic reconciliation
repository before admitting traffic.

Create a pre-migration backup before the first boot of 060 against representative data. This command
uses the database container's existing environment and does not echo credentials:

```bash
mkdir -p build/060
docker exec astraldeep-postgres sh -lc 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > build/060/pre-060.dump
docker exec -i astraldeep-postgres pg_restore --list < build/060/pre-060.dump > /dev/null
```

Run the migration-focused tests, which must cover an existing-data fixture, two concurrent starters,
an updater crash, repeat execution, and a policy-only revision change while the schema marker is
already current:

```bash
docker exec astraldeep bash -c "cd /app/backend && python -m pytest tests/test_schema_revision_guard.py ../components/AstralPlane/tests/test_schema_migrations.py ../components/AstralPlane/tests/integration/test_empty_database_startup.py -q"
docker compose restart astraldeep
curl -fsS http://localhost:8001/readyz
docker compose restart astraldeep
curl -fsS http://localhost:8001/readyz
```

Inspect markers interactively without printing any row payloads:

```bash
make psql
```

```sql
SELECT key, value
FROM schema_meta
WHERE key IN ('revision', 'user_agent_policy_revision')
ORDER BY key;
```

Both restarts must converge on the same current values, and the second boot must take the guarded
fast path. A failed migration must leave the prior marker and data intact.

## 4. Reliability, fault, and load probes

The release-scale probe lives at `backend/tests/perf/test_runtime_reliability_060.py` and uses real
PostgreSQL coordination. It must run with `LOOP_GUARD_ENFORCE=1` and include the specification's
counts rather than a reduced smoke-only parameter set:

```bash
docker exec -e LOOP_GUARD_ENFORCE=1 astraldeep bash -c "cd /app/backend && python -m pytest tests/perf/test_runtime_reliability_060.py -q -m perf"
```

The probe is green only when all of these assertions hold:

| Probe | Required result |
|---|---|
| 1,000 connection frames | Active work never exceeds the configured limit; each accepted operation has one terminal; no connection-owned task remains after five seconds. |
| 10,000 scheduler interleavings | Repeated polls and crash recovery produce at most one visible effect. Saturated scheduled capacity holds work queued for more than 30 seconds: the 15-second claim renews at least every five seconds from post-claim queueing through execution, a second instance cannot reclaim it, and owner death permits the same occurrence/new attempt only after database expiry. |
| 100 BYO crash/hang/host trials | Exit or host loss settles within two seconds; hang settles within seven; no stale generation result is accepted. |
| 100 revision-promotion faults | The last-known-good revision remains callable at every failed promotion boundary. |
| 100 authoring/delete races | No lost update, shared draft storage, duplicate publication, or deleted-agent resurrection. |
| 50 two-starter migration trials | One updater owns each revision and both starters converge; policy-only changes always revalidate. |
| 10,000 registry overlaps | No mutation exception, partial snapshot, or stale current-state result. |
| 100 backend process-supervision trials | The backend-local supervisor bounds output and leaves no descendant, reader, or pipe five seconds after stop. |
| 100 frozen-Windows supervision trials | The independently packaged Windows-local supervisor passes the same vectors from the actual frozen host without importing backend code. |
| Maintenance fault matrix | Successful units alone complete; failed units retain identity, error, and retry state; publication is atomic. |

Capture the concurrent maintenance/process latency spans and verify interactive p95 is at most two
seconds and no observation exceeds five seconds:

```bash
docker compose logs --no-color astraldeep | python3 backend/scripts/perf_report.py
```

Do not accept a probe that silently deselects tests, lowers the trial count, disables fault
injection, or checks dispatch count without checking the visible-effect ledger.

## 5. Real-browser continuity and progress

Once the protected staging credential issuer is implemented, activated, and live-verified, the
automated release lane launches a fresh real-browser profile, completes sign-in through the
Keycloak UI using a per-producer, request-scoped identity minted just in time by that issuer, never
injects a token or persists browser auth state, and writes a schema-valid report plus digested raw
evidence. Until then the protected readiness caller remains fail-closed:

```bash
PLAYWRIGHT_IMAGE="$(tr -d '\n' < components/AstralProjection/tooling/web-ci/playwright-image.txt)"
mkdir -p build/060/coverage build/060/release-evidence
docker run --rm -v "$PWD:/work" -w /work/components/AstralProjection/tooling/web-ci \
  -e ASTRAL_PLAYWRIGHT_IMAGE="$PLAYWRIGHT_IMAGE" \
  -e STAGING_URL -e SHA \
  -e ASTRAL_RELEASE_USERNAME -e ASTRAL_RELEASE_PASSWORD \
  -e ASTRAL_RELEASE_ID -e ASTRAL_RELEASE_VERSION \
  -e ASTRAL_RELEASE_STAGING_FILE \
  -e ASTRAL_RELEASE_LIFECYCLE_AGENT_ID -e ASTRAL_RELEASE_LIFECYCLE_STATES \
  -e ASTRAL_RUNNER_ENVIRONMENT \
  -e GITHUB_WORKFLOW -e GITHUB_RUN_ID -e GITHUB_RUN_ATTEMPT -e GITHUB_JOB \
  -e RUNNER_OS -e RUNNER_ARCH -e RUNNER_NAME \
  "$PLAYWRIGHT_IMAGE" sh -lc '
    set -eu
    NODE_V8_DIRECTORY=/tmp/astral-node-v8
    NODE_INTERIM=/tmp/astral-node-interim.json
    NODE_COVERAGE=/tmp/astral-node.json
    BROWSER_COVERAGE=/tmp/astral-browser-istanbul.json
    test ! -e "$NODE_V8_DIRECTORY"
    mkdir "$NODE_V8_DIRECTORY"
    test "$(corepack npm --version)" = "11.16.0"
    corepack npm ci --ignore-scripts
    corepack npm run check:package-manager
    export NODE_V8_COVERAGE="$NODE_V8_DIRECTORY"
    corepack npm run check:product-isolation
    corepack npm run lint
    corepack npm run test:coverage-conversion
    corepack npm run test:coverage-conversion:node
    corepack npm run test:coverage-union
    corepack npm run test:coverage-conversion:browser
    corepack npm run browser:release -- --base-url "$STAGING_URL" --candidate-sha "$SHA" --output /work/build/060/release-evidence/web.json --coverage-output /work/build/060/coverage/web-v8.json --coverage-istanbul-output "$BROWSER_COVERAGE"
    corepack npm run coverage:node -- --node-v8-directory "$NODE_V8_DIRECTORY" --repo-root ../.. --output "$NODE_INTERIM"
    unset NODE_V8_COVERAGE
    corepack npm run coverage:node -- --node-v8-directory "$NODE_V8_DIRECTORY" --repo-root ../.. --output "$NODE_COVERAGE"
    corepack npm run coverage:union -- --node "$NODE_COVERAGE" --browser "$BROWSER_COVERAGE" --repo-root ../.. --output /work/build/060/coverage/web-istanbul.json
  '
```

After that activation, the protected producer supplies those identities and the request-scoped
staging-output path; the ephemeral username/password exist only for that producer lease and are
never written to an output, artifact, log, or report. The release runner
rejects host execution, an unpinned/different image, missing workflow/runner identity, or a staging
endpoint that differs from the staged output. The synthetic continuity reducer suite is available
separately as `npm run browser:contract` and is never release evidence.

Open the backend-served web client in a real Chrome process:

```bash
open -a "Google Chrome" "http://localhost:8001/"
```

Sign in through the existing Keycloak flow and perform this sequence:

1. Start a new chat and run the curated request, **“Roll exactly six six-sided dice and show the
   normalized results.”** Confirm the request, tool trace, quantities, sides, notation, and narrative
   agree.
2. Record the chat identity, transcript, committed canvas, and final render revision. Reload Chrome;
   the same coherent conversation must replace the old view atomically within five seconds, with no
   welcome flash.
3. While the chat is open, restart only the service and wait for reconnect:

   ```bash
   docker compose restart astraldeep
   curl -fsS http://localhost:8001/readyz
   ```

   The old committed view stays visible until the complete replacement is ready. Delayed output
   from the prior connection/request must not alter it.
   Each controlled logical commit must yield one complete `snapshot_purpose=commit`
   `conversation_snapshot` at revision `R+1`. On reconnect at unchanged revision R, the first complete
   `snapshot_purpose=hydration` snapshot with a fresh ID for the generation explicitly opened for
   hydration must atomically replace state (including any new ROTE adaptation);
   its same-ID replay is a no-op, a later different-ID equal-revision snapshot in that generation is
   a conflict, an equal snapshot for a normal new-turn/commit generation is rejected, and lower/old-
   generation snapshots are stale. Reordered transient `ui_*` frames may
   affect only their request overlay and cannot partially mutate committed transcript/canvas.
   A client load/turn must use its own fresh UUID4 request generation. Then exercise a scheduled
   turn, a persisted stream terminal, a detached/REST component mutation, and a long-running-job
   completion. Each server-originated update must use a fresh server generation announced only by
   this exact six-field prelude, with no missing or extra keys:

   ```json
   {
     "type": "conversation_commit_ready",
     "schema_version": 1,
     "chat_id": "<active-chat-uuid4>",
     "connection_generation": "<current-connection-uuid4>",
     "request_generation": "<fresh-server-uuid4>",
     "render_revision": 2
   }
   ```

   The prelude must immediately precede exactly one matching
   `snapshot_purpose=commit` snapshot. Missing/extra/malformed, foreign-chat, old-connection,
   duplicate, and stale/equal-revision preludes are no-ops. A valid prelude received while a
   client-created commit is unfinished must not steal its fence; reconnect hydration must still
   reveal the already durable server update. For a scheduled job without an explicit target chat,
   verify the hydrated `chat_id` equals the job's UUID4 on the first attempt and every retry.
4. Exercise an operation longer than two seconds. A labeled status must appear by two seconds and
   end exactly once as completed, failed, cancelled, or retryable.
5. Observe a personal agent through `starting → online → updating → failed/offline`; the page must
   update within two seconds without a full reload.
6. Explicit **New chat** must clear the resume locator and show welcome. A transient disconnect or
   page reload must not. Repeat once with sign-out and once with confirmed chat deletion.

The release-readiness browser job repeats sign-in, rendered chat, reconnect/resume, generation
reordering, lifecycle, and progress against the candidate SHA; a manual browser pass is required in
addition to that automation.

## 6. Windows source and packaged-release checks

Run the full source suite on the development host first:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r windows-client/requirements.txt pytest pytest-cov
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest windows-client/tests -q --cov=windows-client --cov-report=xml:build/060/coverage/windows.xml
QT_QPA_PLATFORM=offscreen .venv/bin/python windows-client/tests/e2e_live.py --prompt "roll exactly 6 six-sided dice"
```

The shipping proof runs on Windows PowerShell from `windows-client/`. Use a clean release venv so
test-only packages cannot leak into the frozen runtime:

```powershell
Set-Location windows-client
py -3.11 -m venv .venv-test
& .\.venv-test\Scripts\python -m pip install -r requirements.txt pytest pytest-cov
$env:QT_QPA_PLATFORM = 'offscreen'
& .\.venv-test\Scripts\python -m pytest tests -q --cov=. --cov-report=xml:build\coverage\windows.xml

py -3.11 -m venv .venv-release
& .\.venv-release\Scripts\python -m pip install --require-hashes -r requirements-release.lock.txt
& .\.venv-release\Scripts\python -m pip check
& .\.venv-release\Scripts\pyinstaller --noconfirm --clean AstralDeep.spec
```

Validate the frozen artifact before any signing step on an ephemeral Windows runner or disposable
Windows user. Changing `APPDATA` alone is not sufficient because Qt stores this application's
`QSettings` under HKCU. The automated release job MUST start with a fresh user hive. For a local
diagnostic run, isolate the exact application key and restore it even when validation fails:

```powershell
$clean = Join-Path $PWD 'build\clean-profile'
New-Item -ItemType Directory -Force "$clean\Roaming", "$clean\Local", "$PWD\build" | Out-Null
$settingsKey = 'HKCU\Software\AstralDeep\WindowsClient'
$settingsBackup = Join-Path $clean 'WindowsClient-QSettings.reg'
reg.exe query $settingsKey *> $null
$hadSettings = ($LASTEXITCODE -eq 0)
if ($hadSettings) {
    reg.exe export $settingsKey $settingsBackup /y | Out-Null
}
$env:APPDATA = "$clean\Roaming"
$env:LOCALAPPDATA = "$clean\Local"
try {
    reg.exe delete $settingsKey /f *> $null
    & .\dist\AstralDeep.exe --validate-deployment --report .\build\deployment-validation.json
    if ($LASTEXITCODE -ne 0) { throw "deployment validation failed: $LASTEXITCODE" }
    $env:ASTRAL_WINDOWS_EXE = (Resolve-Path .\dist\AstralDeep.exe).Path
    & .\.venv-test\Scripts\python -m pytest tests\test_packaged_release.py -q
    if ($LASTEXITCODE -ne 0) { throw "packaged release tests failed: $LASTEXITCODE" }
    Get-FileHash .\dist\AstralDeep.exe -Algorithm SHA256
}
finally {
    reg.exe delete $settingsKey /f *> $null
    if ($hadSettings) {
        reg.exe import $settingsBackup | Out-Null
    }
}
```

`test_packaged_release.py` must run the actual EXE's worker branch through a benign stdio round trip,
exercise bounded high output and descendant cleanup, validate the bundled runtime manifest/lock
digest, and prove clean termination. Launch the same EXE with the clean profile and verify the main
window appears without **Configure AstralDeep**, all transports and the BYO host report the same
immutable profile digest, a normal chat completes, and a benign personal-agent call returns. An
offline/failed connection must retain that profile and offer retry; it must not fall back to local
defaults.

> **Inactive publication procedure:** The build and diagnostic checks below remain valid, but do not
> execute the tag, bridge, draft, cleanup, or public-transition steps until T120's reviewer, tag-policy,
> trusted-tag-creation, and durable-cleanup blockers are resolved and live-proven.

Two fresh Windows/Python-3.11 release environments must resolve identical installed-package and lock
digests. The client and file metadata must both report `0.4.0`; `v0.3.0` and its assets remain
immutable. The order is build → clean-profile validation → frozen-worker round trip → no-dialog GUI
smoke → local evidence preparation → independent CI validation → protected environment approval →
exact tag → legacy-compatible detached sign → draft upload → re-download/verify → public transition.
The reusable Windows candidate job archives that one unsigned EXE with its run/artifact ID, SHA-256,
source SHA, profile/lock digests, and coverage. The readiness matrix downloads and tests those bytes.
Tracked candidate/build/evidence workflows have read-only permissions. This is not yet an enforced
repository-wide publication boundary because unrestricted fresh-`v*` creation can dispatch changed
tag-ref bridge bytes under the legacy updater identity; T120 must close that route. The intended
protected, environment-approved publisher uses only the built-in short-lived `GITHUB_TOKEN` with
job-scoped contents/actions write plus attestations/deployments read; actions write is used only to dispatch the bridge after the
create-only tag operation, because a `GITHUB_TOKEN`-authored tag does not recursively trigger a push
workflow. No repository-scoped GitHub App or custom broker is involved. It consumes the exact
independently validated candidate/artifact decision and refuses any existing tag/release/asset. To
preserve the verifier shipped in v0.3.0, its exact-byte-pinned signer has only `contents: read`,
`actions: read`, `attestations: read`, and `id-token: write`. The publisher dispatches it at the exact
new `refs/tags/<tag>` ref with the protected readiness run and decision artifact IDs; the bridge
rejects any differing ref/run/artifact and rechecks a bounded decision lifetime before signing. It
retrieves T068's EXE by exact originating
run/attempt/artifact ID, re-hashes it, and emits a detached bundle with SAN exactly
`https://github.com/AstralDeep/AstralDeep/.github/workflows/release-windows.yml@refs/tags/v0.4.0`.
The signer cannot mutate releases. The publisher verifies the bundle with the actual v0.3.0 policy,
creates `SHA256SUMS`, uploads exactly `AstralDeep.exe`, `SHA256SUMS`, and `cosign.bundle` create-only to
a new draft, resolves/re-downloads all three distinct numeric asset IDs, and re-hashes/verifies the
checksum, legacy identity/issuer, draft state/count, tag, target SHA, CI decision, publisher
workflow identity, and environment approval. It validates `windows_draft_verification_provenance` while the
release is still a draft, including `release_name == tag == v0.4.0` and
`latest_disposition == make_latest_on_publish`; only then may it make the draft public as latest.
Official mode must re-query the API-shaped `/releases/latest` response and run the shipped v0.3.0
updater parser before declaring success. Failure is designed to remove only the newly created
tag/draft before publication, but active ruleset `19078549` currently blocks that tag deletion.
T128 disposable mode uses a temporary tag/draft in the same repository, must verify complete cleanup,
and never creates an official tag or public release; it remains inactive until rollback is possible.

## 7. Android checks and process recreation

Use the committed wrapper via `sh` (the file is intentionally not executable in this checkout):

```bash
export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
export ANDROID_HOME="$(sed -n 's/^sdk.dir=//p' android-client/local.properties)"
"$ANDROID_HOME/platform-tools/adb" devices -l
cd android-client
sh ./gradlew ktlintCheck :app:lintDebug :core:test :app:testDebugUnitTest :core:koverVerify :app:koverXmlReport :core:koverXmlReport :app:assembleDebug --no-daemon --stacktrace
sh ./gradlew :app:connectedDebugAndroidTest --no-daemon --stacktrace
cd ..
cp android-client/app/build/reports/kover/report.xml build/060/coverage/android-app.xml
cp android-client/core/build/reports/kover/report.xml build/060/coverage/android-core.xml
python3 scripts/run_android_next_major_canary.py android-client/gradle/next-major-canary.properties
```

AGP 10 and Gradle 10 do not both have stable public releases as of 2026-07-16, so the tracked
declaration is explicitly `unreleased` and the command above MUST exit 69 rather than count a
prerelease, guessed, or shipping toolchain as evidence. CI may verify that this declaration remains
current with:

```bash
python3 scripts/run_android_next_major_canary.py \
  android-client/gradle/next-major-canary.properties \
  --allow-unreleased --verify-official-availability \
  --output build/060/android-next-major-readiness.json
```

That readiness diagnostic succeeds until both official metadata feeds contain stable major-10
releases. Alpha, beta, RC, milestone, nightly, and snapshot versions are ignored. Spec 060 does not
replace the sentinels or upgrade the shipping toolchain; a separately authorized future change owns
stable pins, the official wrapper URL/SHA-256, and the true isolated canary. Install the current
candidate and exercise real process recreation:

```bash
"$ANDROID_HOME/platform-tools/adb" install -r android-client/app/build/outputs/apk/debug/app-debug.apk
"$ANDROID_HOME/platform-tools/adb" shell am force-stop com.personalailabs.astraldeep
"$ANDROID_HOME/platform-tools/adb" shell am start -n com.personalailabs.astraldeep/com.personalailabs.astraldeep.app.MainActivity
```

After a completed rendered turn, force-stop and relaunch twenty times. Each time, the account-scoped
locator must select the same chat before registration; transcript and canvas return coherently in
five seconds; structured/empty/error transcript forms remain visible; no welcome appears. Confirm
stale generation frames and invalid `conversation_commit_ready` preludes are ignored, one valid
fresh server prelude opens only its matching commit snapshot, every lifecycle state updates, and
changed switches/statuses have stable TalkBack name, role, state, and focus behavior.

## 8. Apple tests, first-login gate, and live continuity

Confirm the booted destinations, run the shared drift guard, then test the app target on iOS and
macOS. The scheme must include the 060 unit/UI targets.

```bash
xcodebuild -version
xcrun simctl list devices booted
xcrun swift-format lint --strict --recursive --configuration apple-clients/.swift-format apple-clients/AstralCore apple-clients/AstralApp apple-clients/AstralWatch
python3 apple-clients/Scripts/generate_app_icons.py --check
swift test --package-path apple-clients/AstralCore
xcodebuild -project apple-clients/AstralApp/AstralApp.xcodeproj -scheme AstralApp -destination 'platform=iOS Simulator,name=iPhone 17 Pro,OS=26.5' -configuration Debug CODE_SIGNING_ALLOWED=NO -enableCodeCoverage YES -resultBundlePath build/060/coverage/AstralApp-iOS.xcresult test
xcodebuild -project apple-clients/AstralApp/AstralApp.xcodeproj -scheme AstralApp -destination 'platform=macOS' -configuration Debug CODE_SIGNING_ALLOWED=NO -enableCodeCoverage YES -resultBundlePath build/060/coverage/AstralApp-macOS.xcresult test
xcodebuild -project apple-clients/AstralApp/AstralApp.xcodeproj -scheme AstralWatch -destination 'platform=watchOS Simulator,name=Apple Watch Series 11 (46mm),OS=26.5' -configuration Debug CODE_SIGNING_ALLOWED=NO -enableCodeCoverage YES -resultBundlePath build/060/coverage/AstralWatch.xcresult -only-testing:AstralWatchTests test
rm -f build/060/coverage/apple-ios-xccov.json build/060/coverage/apple-macos-xccov.json build/060/coverage/apple-watchos-xccov.json
python3 scripts/export_xccov_line_coverage.py --repo "$PWD" --xcresult build/060/coverage/AstralApp-iOS.xcresult --platform ios --output build/060/coverage/apple-ios-xccov.json
python3 scripts/export_xccov_line_coverage.py --repo "$PWD" --xcresult build/060/coverage/AstralApp-macOS.xcresult --platform macos --output build/060/coverage/apple-macos-xccov.json
python3 scripts/export_xccov_line_coverage.py --repo "$PWD" --xcresult build/060/coverage/AstralWatch.xcresult --platform watchos --output build/060/coverage/apple-watchos-xccov.json
```

The bounded exporter first lists the archive's files, then invokes
`xcrun xccov view --archive --file <path> --json` separately for every tracked maintained Swift
source owned by that platform. It validates and normalizes contiguous archive line observations,
filters cross-built targets (iOS/macOS own AstralApp plus AstralCore; watchOS owns AstralWatch plus
AstralCore), and refuses duplicate, malformed, oversized, symlinked, or pre-existing output. The
aggregate `--report --json` form contains only file/function totals and is not valid changed-line
evidence. Protected release jobs execute the exporter from the owner-pinned protected-policy
checkout, never from candidate-controlled script bytes; this local command remains diagnostic.

The selected developer directory must report Xcode 26.6; the destination OS remains the supported
26.5 simulator runtime.

Run the deterministic first-login UI suite separately on both affected platforms so its timing
result is visible in evidence:

```bash
xcodebuild -project apple-clients/AstralApp/AstralApp.xcodeproj -scheme AstralApp -destination 'platform=iOS Simulator,name=iPhone 17 Pro,OS=26.5' -configuration Debug CODE_SIGNING_ALLOWED=NO -only-testing:AstralAppUITests/LLMFirstLoginUITests test
xcodebuild -project apple-clients/AstralApp/AstralApp.xcodeproj -scheme AstralApp -destination 'platform=macOS' -configuration Debug CODE_SIGNING_ALLOWED=NO -only-testing:AstralAppUITests/LLMFirstLoginUITests test
```

Across 30 valid, invalid, slow, and unavailable-provider trials on each platform, verify:

- Save acknowledges and all scripted focus/navigation/window/scene interactions respond within
  250 ms;
- duplicate Save is single-flight while the fields remain editable after failure;
- work still active at one second shows an accessible phase label;
- at least 95% of valid active-connection attempts advance within five seconds;
- every invalid or unavailable attempt terminates by ten seconds with corrective or retryable state,
  and no late success replaces that terminal;
- app termination/relaunch after a completed chat restores one coherent transcript+canvas snapshot
  without welcome or stale output; and
- malformed/stale `conversation_commit_ready` values are no-ops while one exact fresh server
  prelude opens only its matching commit snapshot without stealing an unfinished client turn; and
- `starting`, `online`, `updating`, `failed`, and `offline` render consistently with the other
  clients.

On the booted watch simulator, sign in through the existing broker flow, complete a supported chat
turn, interrupt and reconnect the paired transport, and verify the same conversation plus current
agent lifecycle state returns. `AstralWatchTests` must directly exercise the Watch-owned account-
scoped locator, purpose-aware reducer, lifecycle state, accessibility, and paired reconnect, and its
coverage report must map changed `AstralWatch/*.swift` lines. Record a `watchos` artifact report; a
Watch build or iOS/macOS surrogate test without this target/runtime smoke is not release evidence.

Repeat the macOS release-candidate flow on the reported 14-inch MacBook Pro/macOS 26.5.2 profile
when available and on each supported iOS review target. Another client or a source-only Swift test
cannot waive this gate.

Feature 060 never implements feature 059's macOS personal-agent host. Read applicability only from
the exact candidate's authenticated `/api/dashboard` and `system_config.config` value at
`capabilities.personal_agent_host.macos`. `{supported:false,
runtime_contract_versions:[],source_feature:null}` permits only `macos_personal_agent_host` to be
`not_applicable`; continuity, authoring, lifecycle display, accessibility, and Apple first-login
remain required. `{supported:true,runtime_contract_versions:[2],source_feature:"059"}` requires the
direct-download macOS artifact to send the structured host registration, receive
`agent_host_registered`, and pass compatibility, supervision, lifecycle, and benign-call evidence.
Missing/malformed capability data or an advertised but refused/unacknowledged host blocks the gate;
the 059 spec directory, branch name, and connected-client count prove nothing.

## 9. Qualifying same-candidate staging

First validate the tracked synthetic/sanitized fixture set and fingerprints:

```bash
SHA="$(git rev-parse HEAD)"
FIXTURE_MANIFEST="backend/tests/fixtures/runtime_reliability_060/staging/fixture-manifest.json"
python3 -m pytest backend/tests/test_staging_fixtures_060.py -q
python3 scripts/run_candidate_staging.py validate-fixtures --manifest "$FIXTURE_MANIFEST"
```

The qualifying workflow reuses the exact image already built by `ci.yml`, pushes that artifact under
an immutable digest-qualified reference without rebuilding, and invokes the following shape on the
configured staging host. Once the activation blocker below is cleared, inputs are limited to
protected non-credential configuration and per-run issuer leases; they are never evidence-controlled,
hand-authored local substitutes, durable provider keys, or shared runtime secrets:

```bash
python3 scripts/run_candidate_staging.py deploy --candidate-sha "$SHA" --candidate-image "$CANDIDATE_IMAGE" --fixture-manifest "$FIXTURE_MANIFEST" --environment-id "$STAGING_ENVIRONMENT_ID" --outputs build/060/staging-outputs.json --trusted-manifest build/060/trusted-stage-deploy.json --leave-running
```

The driver creates unique Compose project/volumes, restores `representative-057.sql`, and imports
the non-secret PKCE realm. Once the separately protected request-scoped issuer is implemented and
activated, runtime users are permitted only after exact workflow/run/attempt/producer
authentication. The driver boots normal `_init_db()` to
060.004, starts real Keycloak and product background/scheduler paths, and emits a non-loopback HTTPS
endpoint plus request, deployment-run, image/service, dataset/realm/manifest, schema, worker, and
candidate-capability identities. The optional `--trusted-manifest` flag additionally emits a
schema-valid `trusted_stage_deploy` manifest (`contracts/release-trust.schema.json`) derived from the
same deploy outputs plus the job's `GITHUB_*` identity; it refuses any job whose `GITHUB_JOB` is not
`stage-deploy`, and its self-declared artifact/builder fields are never a trust root — the protected
trusted builder reconstructs run/job/artifact identity from the GitHub API before attesting.
`stage-deploy` exits while the namespace remains alive. Every
backend/browser/Windows/Android/Apple job declares `needs: stage-deploy`, consumes those exact
trusted outputs, proves that endpoint's reachability itself, and repeats the normalized identity in
its report. Repository rules require `.github/workflows/release-trusted-builder.yml` at a protected
signer digest independent of the candidate. The stage job and every platform producer send a separate
manifest through that builder under a unique artifact ID. The protected verifier reconstructs exact
IDs from its own run/API, verifies every attestation with the protected signer digest/certificate
identity plus repository and candidate SHA, then validates the pinned
`contracts/release-trust.schema.json`. Each report's
run/attempt/job/runner must match exactly one verified producer manifest. It compares evidence to the
verified stage bytes before probing and never contacts an evidence-supplied URL. Same-named files in
the evidence bundle and manifest-declared signer values are ignored as trust roots.
Aggregation waits for all platform jobs; only then an `if: always()` job on the same staging host runs:

```bash
python3 scripts/run_candidate_staging.py cleanup --environment-id "$STAGING_ENVIRONMENT_ID"
```

The staging gate fails for a dirty/different source tree, rebuilt mismatched image, mock auth, empty
database, fixture/realm fingerprint drift, skipped normal migration, disabled background or scheduler
worker, localhost/non-HTTPS endpoint, unreachable cross-runner endpoint, or source-process endpoint.
Missing staging host/registry/TLS secrets blocks the workflow. A local sequential Compose smoke is
useful diagnostics but cannot emit qualifying release evidence.

Activation is additionally blocked until the issuer's separate TLS route and protected runtime have
passed live mint, expiry, per-job revoke, and cleanup-time `revoke-all` tests. Durable repository
probe/access tokens and shared login credentials are forbidden. The default-branch caller inherits
no repository secrets and requires both `RELEASE_READINESS_ACTIVE=true` and
`RELEASE_EPHEMERAL_CREDENTIALS_READY=true`; the latter remains unset/false until this blocker is
cleared. Real Keycloak Authorization Code + PKCE evidence remains mandatory for browser and Apple
sign-in—a mock, injected bearer, plaintext artifact, or workflow-output credential cannot qualify.

## 10. Local pre-push evidence and protected CI validation

Validate tracked documentation, including files explicitly unignored beneath `docs/`:

```bash
python3 scripts/check_doc_links.py
git ls-files docs/byo-client-agents.md
python3 -m pytest backend/tests/test_release_contract_schemas.py backend/tests/test_release_evidence_validator.py -q
```

The validator must schema-check the active tracked schemas, reject unsupported keywords/remote
references, enforce UUID/date-time/URI formats plus `oneOf`/`contains`/conditionals, and reject
missing/unavailable platform reports, under-threshold quantitative measurements, illegal N/A
outcomes, bundled/GitHub evidence whose exact bytes cannot be re-hashed, and backend/web OCI claims
that do not exactly match the digest-qualified image in the separately attested stage deployment.

The normative local pre-push wrapper is `make prepare-release-evidence` (T107):

```bash
BASE_SHA="$(git rev-parse origin/main)" make prepare-release-evidence
# equivalent direct invocation
python3 scripts/prepare_release_evidence.py --base-sha "$(git rev-parse origin/main)" --candidate-sha "$(git rev-parse HEAD)" \
  --backend-python build/060/coverage/backend.xml \
  --voice-worker-python build/065/coverage/voice-worker.xml \
  --tooling-python build/060/coverage/tooling-python.xml \
  --repository-profile deep \
  --coverage-mode strict
```

The wrapper collects, normalizes, and parses canonical evidence and digests locally: it inventories
`build/060/release-evidence`, canonicalizes and SHA-256-digests every recognized document, assembles
one deterministic `release_evidence_set` (content-derived UUIDv5 identity) when the directory holds
only platform reports, and delegates schema plus same-candidate policy validation to
`scripts/validate_release_evidence.py`. Under the repository-owner topology, Deep strict mode
requires exactly three independently named reports: backend, voice worker, and tooling. Projection
strict mode runs separately in the AstralProjection repository with exactly eight reports:
Projection Python, Windows, JavaScript, Android app, Android core, iOS, macOS, and watchOS. The
legacy composed-monorepo profile is an exact eleven-slot compatibility diagnostic, not Deep's
normative owner lane. `--coverage-mode partial` exists only for focused programmatic diagnostics;
the Make wrapper pins strict mode after any report-path override, so an empty or incomplete override
fails closed. Before invoking the changed-code collector, the wrapper binds each report's raw and
canonical semantic identities, rejects path/inode/content aliases globally, and then requires the
collector decision to name the exact same identities. This detects report replacement between the
binding and policy phases. Semantic identity is derived only from the native parser's normalized
files plus observed/executable/covered source-line sets, so whitespace, timestamps, generator labels,
and other irrelevant XML/JSON metadata cannot disguise a copied report. A second target-independent
native-observation identity closes copies that target path filtering would otherwise make look
different. Strict mode parses every report in the selected exact owner profile even when its
evaluator target is unchanged, requires a nonempty executable contribution from a file that exists
in the exact candidate tree, and enforces producer source scopes: non-voice backend; isolated voice
worker; Projection package/ROTE/web-renderer Python; `client.js`; separate Android
app/core; iOS App plus AstralCore with Watch rejected; macOS App with no Watch; and watchOS with a required tracked Watch
witness, allowed AstralCore output, and no App output. The voice worker's
runtime-renamed `streaming_egress.py`, `voice_transcript.py`, and `watch_ticket.py` observations are
attributed back to their exact `backend/shared/` candidate sources before identity and changed-line
policy. The distinct tracked `backend/voice_agent/voice_transcript.py` and `watch_ticket.py` host-test
shims remain backend-producer owned and are excluded from worker ownership because the image replaces
those runtime paths. Every applicable producer must map its own scoped changed files. A changed
AstralCore file and all of its changed physical lines must map completely through at least one of iOS
or macOS because a real iOS UI archive can omit Core; Watch archive Core observations do not substitute
for that group. Every other applicable Apple report must observe every changed physical line. Each
Cobertura-backed Python producer must observe every candidate-executable changed line in its owned
files. That candidate-bound witness normalizes recursive `co_lines()` metadata through Python's
tokenized logical-statement map, ignores nullable line entries, and applies the exact default
exclusions and docstring rules from locked Coverage 7.15.2. It is checked in both directions against
all tracked maintained Deep Python sources under Python 3.11 and 3.14; exclusion matches are mapped
with one incremental linear scan rather than rescanning the candidate prefix per match. Comments, blanks, docstrings,
declaration continuations, exact `TYPE_CHECKING`/ellipsis-only declarations, and exact no-cover
clauses are not invented as executable work. Every changed maintained Python path must yield a bounded,
NUL-free regular-blob witness; missing, non-regular, oversized, or binary sources fail explicitly.
Overlapping Python producers must also agree on executable changed lines. No producer is required to cover a line merely
to prove its native contribution—a valid zero-hit macOS archive remains valid input to the ordinary
threshold decision. The protected collector
invocation independently pins the same strict mode and complete matrix. Local shape rules cannot prove
that an iOS- versus macOS-shaped archive came from a particular job; producer origin comes from
protected, attempt-scoped artifact and job identities rather than self-asserted xccov metadata. It writes
`build/060/release-evidence/local-diagnostic.json`, exits `2` with
`release evidence rejected: ...` on any rejection, and stays diagnostic. A green local
result remains diagnostic and must state `protected_release_authorization: false`; the wrapper has
no decision-output mode and cannot emit a trusted decision. Protected CI then
downloads the exact run/artifact identities, independently schema-validates and re-hashes the
canonical evidence and executes pinned policy/coverage. An affected platform must pass unless the
existing bounded exception policy is independently satisfied by protected CI; local output cannot
approve an exception.

Release publication and protected exception/debt transitions remain environment-approved GitHub
Actions jobs using the built-in short-lived `GITHUB_TOKEN` with job-scoped permissions. Do not
create a repository-scoped GitHub App, installation token, publisher App, or custom token broker.

Candidate jobs initialize the pinned component gitlinks recursively and verify the exact local
composition before consuming component-owned sources. AstralProjection and AstralPlane are private
sibling repositories, and separately authorized cross-repository read access is not currently
available to these jobs. Do not add a token to the workflows or activate the readiness variables to
work around that boundary. Until a lead approves and installs least-privilege source-read access,
the recursive checkout must fail closed and release readiness remains unavailable.

The readiness matrix is called only by the protected default-branch
`release-readiness-protected.yml` workflow after both
`RELEASE_READINESS_ACTIVE` and `RELEASE_EPHEMERAL_CREDENTIALS_READY` are true (the required check is
`release-readiness / protected-decision`). The ephemeral-credentials variable is a separate
fail-closed activation checkpoint and MUST remain false until the external issuer contract above is
live-verified. A candidate rerun is dispatched with an
explicit verified base, and the unique request id rides the run-name
(`release-readiness <request_id>`):

```bash
DEFAULT_BRANCH="$(gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name')"
gh workflow run release-readiness.yml --ref "$DEFAULT_BRANCH" -f candidate_sha="$SHA" -f base_sha="$BASE_SHA" -f request_id="$REQUEST_ID"
```

The run's `protected-decision` job uploads the canonical `release-evidence` bundle and attests the
exact `trusted-release-decision` bytes as the protected reusable `release-readiness.yml` workflow at
the installed `RELEASE_TRUSTED_BUILDER_SHA`. The bridge, controller, and publisher each verify that
exact signer workflow and signer digest before accepting the decision. A rerun still requires the
shared-run backend and tooling coverage artifacts, so the coverage gate fails honestly when they are
absent.

<details>
<summary>Superseded App/broker bootstrap (historical only — do not execute)</summary>

The remainder of this section records the rejected pre-2026-07-16 design so its security analysis is
not lost. It is not an implementation or administrator runbook.

Bootstrap is deliberately ordered. A first protected landing installs the verifier, pinned policy
and all three schemas, bridge template, publisher/controller, exception registrar, token broker, and
protected environments/tag rules while leaving the automatic PR/main caller and required check
disabled. After the candidate rebases onto and verifies that immutable root, a second checkpoint
enables the caller and `release-readiness / protected-decision`. A run that is also installing the
trust root it claims to use is non-qualifying.

`ci.yml` calls the reusable readiness workflow automatically on every pull request and main push;
its named aggregate is a required check and main image publication depends on it. PRs use the event's
immutable base SHA, main pushes use `github.event.before`, and any zero/non-ancestor/unexpected-empty
code comparison fails. Manual dispatch remains available for candidate reruns, but requires an
explicit verified base. The workflow's
backend/staging, browser, Windows, Android, macOS, iOS, and watchOS jobs must all test the same SHA,
staging image, and shipping artifacts and attach their digests. A unique request ID is part of the
workflow `run-name`; selecting merely the
latest branch run is forbidden because it can attach evidence from a concurrent dispatch:

```bash
SHA="$(git rev-parse HEAD)"
DEFAULT_BRANCH="$(gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name')"
git fetch --no-tags origin "$DEFAULT_BRANCH"
BASE_SHA="$(git merge-base "$SHA" "origin/$DEFAULT_BRANCH")"
git merge-base --is-ancestor "$BASE_SHA" "$SHA"
test "$BASE_SHA" != "$SHA"
REQUEST_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
gh workflow run release-readiness.yml --ref "$DEFAULT_BRANCH" -f candidate_sha="$SHA" -f base_sha="$BASE_SHA" -f request_id="$REQUEST_ID"
RUN_ID=""
for _ in {1..30}; do
  RUN_ID="$(gh run list --workflow release-readiness.yml --event workflow_dispatch --branch "$DEFAULT_BRANCH" --limit 100 --json databaseId,displayTitle,headBranch --jq ".[] | select(.headBranch == \"$DEFAULT_BRANCH\" and .displayTitle == \"release-readiness $REQUEST_ID\") | .databaseId" | head -n 1)"
  test -n "$RUN_ID" && break
  sleep 2
done
test -n "$RUN_ID"
gh run watch "$RUN_ID" --exit-status
mkdir -p build/060/release-evidence
gh run download "$RUN_ID" --name release-evidence --dir build/060/release-evidence
```

The passing run already performed the qualifying validation below inside the independently pinned
protected verifier job. This block is the required workflow shape, not a local/name-based substitute:
the verifier reconstructs producer jobs and unique artifact IDs from its current run and GitHub API
before these commands begin, runs protected policy code, and emits the attested required decision.

```bash
# This executes in the protected reusable verifier, never in candidate-controlled policy code.
# A name-based/manual download is diagnostic only and cannot qualify a release.
mkdir -p build/060/trust build/060/attestation-verification build/060/protected-decision
test "${GITHUB_ACTIONS:-}" = "true"
test "$GITHUB_JOB" = "protected-decision"
test -n "$TRUSTED_BUILDER_SHA"
test -n "$TRUSTED_BUILDER_CERT_IDENTITY"
test -n "$PROTECTED_POLICY_ROOT"
PROTECTED_POLICY_SOURCE_ROOT="$PROTECTED_POLICY_ROOT"
test "$(git -C "$PROTECTED_POLICY_SOURCE_ROOT" rev-parse HEAD)" = "$TRUSTED_BUILDER_SHA"
test -z "$(git -C "$PROTECTED_POLICY_SOURCE_ROOT" status --porcelain --untracked-files=no)"
rm -rf build/060/protected-policy build/060/protected-policy.tar
mkdir -p build/060/protected-policy
git -C "$PROTECTED_POLICY_SOURCE_ROOT" archive "$TRUSTED_BUILDER_SHA" scripts/validate_release_evidence.py scripts/check_changed_coverage.py specs/060-runtime-reliability-hardening/contracts/windows-deployment-profile.schema.json specs/060-runtime-reliability-hardening/contracts/release-evidence.schema.json specs/060-runtime-reliability-hardening/contracts/release-trust.schema.json > build/060/protected-policy.tar
PROTECTED_POLICY_SHA256="$(sha256sum build/060/protected-policy.tar | cut -d ' ' -f 1)"
test "${#PROTECTED_POLICY_SHA256}" -eq 64
tar -xf build/060/protected-policy.tar -C build/060/protected-policy
test -z "$(find build/060/protected-policy -type l -print -quit)"
PROTECTED_POLICY_ROOT="$PWD/build/060/protected-policy"
shopt -s nullglob
producer_manifests=(build/060/trust/producers/*.json)
stage_manifests=(build/060/trust/stage/*.json)
approval_manifests=(build/060/trust/approvals/*.json)
resolution_manifests=(build/060/trust/resolutions/*.json)
test "${#producer_manifests[@]}" -gt 0
test "${#stage_manifests[@]}" -eq 1
EXCEPTION_LEDGER_SHA="$(gh api "repos/$GITHUB_REPOSITORY/git/ref/heads/release-evidence-debt" --jq .object.sha)"
test "${#EXCEPTION_LEDGER_SHA}" -eq 40
manifests=("${producer_manifests[@]}" "${stage_manifests[@]}" "${approval_manifests[@]}" "${resolution_manifests[@]}")
for manifest in "${manifests[@]}"; do
  relative="${manifest#build/060/trust/}"
  verification="build/060/attestation-verification/${relative//\//_}.json"
  gh attestation verify "$manifest" --repo "$GITHUB_REPOSITORY" --source-digest "$SHA" --signer-digest "$TRUSTED_BUILDER_SHA" --cert-identity "$TRUSTED_BUILDER_CERT_IDENTITY" --format json > "$verification"
done
python3 "$PROTECTED_POLICY_ROOT/scripts/validate_release_evidence.py" --schema "$PROTECTED_POLICY_ROOT/specs/060-runtime-reliability-hardening/contracts/release-evidence.schema.json" --trust-schema "$PROTECTED_POLICY_ROOT/specs/060-runtime-reliability-hardening/contracts/release-trust.schema.json" --deployment-profile-schema "$PROTECTED_POLICY_ROOT/specs/060-runtime-reliability-hardening/contracts/windows-deployment-profile.schema.json" --evidence-dir build/060/release-evidence --raw-apple-evidence-dir build/060/raw-apple-evidence --windows-product-evidence-dir build/060/coverage-inputs/windows --base-sha "$BASE_SHA" --candidate-sha "$SHA" --trusted-provenance-dir build/060/trust/producers --trusted-stage-deploy "${stage_manifests[0]}" --trusted-approvals-dir build/060/trust/approvals --trusted-debt-resolutions-dir build/060/trust/resolutions --attestation-verification-dir build/060/attestation-verification --protected-builder-sha "$TRUSTED_BUILDER_SHA" --protected-builder-identity "$TRUSTED_BUILDER_CERT_IDENTITY" --protected-policy-sha "$PROTECTED_POLICY_SHA256" --exception-ledger-repository "$GITHUB_REPOSITORY" --exception-ledger-ref refs/heads/release-evidence-debt --exception-ledger-commit "$EXCEPTION_LEDGER_SHA" --decision-output build/060/protected-decision/trusted-release-decision.json
test "$(gh api "repos/$GITHUB_REPOSITORY/git/ref/heads/release-evidence-debt" --jq .object.sha)" = "$EXCEPTION_LEDGER_SHA"
```

The protected reusable `release-readiness.yml` workflow attests `trusted-release-decision.json` at
the installed `RELEASE_TRUSTED_BUILDER_SHA` and owns the repository-required
`release-readiness / protected-decision` check. Every consumer verifies that exact signer workflow
and signer digest. The caller aggregate may download and independently verify that exact decision
artifact but cannot replace its verdict. Schema fields and embedded hashes
are never accepted as proof of trust; the protected verifier compares the
same-repository protected ledger head both before and after evaluation and attests its exact commit,
tree, immutable reference, and canonical `debts/` plus `resolutions/` path-to-byte-digest snapshot even when no current exception
is used. A stale or concurrently moved ledger head blocks.
independently verified attestation subject, protected builder digest/certificate, repository, source
SHA, run, attempt, job, runner, artifact/member, and OCI/release-asset identities to current workflow
context. Protected values and policy code come from environment/ruleset/default-branch configuration
that candidate code cannot modify.

The validator must reject a missing client, mismatched SHA or artifact digest, duplicate/unknown
platform, duplicate `checks[].id` even when the duplicate objects differ, cross-report staging
identity mismatch, unreachable endpoint, invalid 059 capability truth, incomplete required flow,
expired exception, unresolved historical debt, untrusted/mutable/path-traversing artifact reference,
or caller-supplied hash that differs from resolved bytes. A passed report requires every mandatory
check to pass; only the Watch report's `personal_agent` authoring check and capability-map-authorized
`macos_personal_agent_host` may be N/A.
An `evidence_exception_request` current-run artifact names its requester, candidate, temporarily
unavailable shipping-client platform/checks, rationale, and seven-day maximum. It deliberately has
no reviewer, approval time, expiry, or approval-state field. Its unavailable report still carries the exact client artifact,
verified qualifying-stage identity, control-producer identity, and immutable re-hashed attempted-
target observation. Failed behavior, backend/docs, qualifying staging, provenance/trust, policy-
integrity, and Apple first-login gaps cannot be exceptions. Before review, the protected
`release-evidence-exception` deployment payload exposes the exact exception artifact ID/reference/
digest. An allowlisted release owner other than the requester approves that payload; the protected
verifier re-queries the immutable GitHub deployment/payload and re-hashes the same request bytes.
After approval, a separately pinned registrar appends one canonical debt entry create-only to the
protected non-force-push `refs/heads/release-evidence-debt` ref. The independently attested
`trusted_exception_approval` must bind reviewer/time/expiry, the request artifact, parent and new
ledger commits, unique path, canonical bytes/digest, and immutable commit/path reference before the
final decision can pass. Candidate commits never carry current debt. A self-authored string/manifest,
same-run trust bootstrap, force push, duplicate path, or mutation before/after approval is invalid.
Each prior `blocks_next_release` platform/check debt from the exact protected ledger commit must be
passed by the next candidate. When it passes, the protected registrar appends a separate canonical
`resolutions/<resolution_id>.json` entry plus attested `trusted_debt_resolution` receipt bound to the
later evidence/provenance; it never rewrites the debt. A resolution satisfies only its named debt
once, so a later outage for the same check requires a new exception/debt. Apple macOS+iOS first-
login evidence is never exception-eligible for this release.

Release readiness requires all of the following for every affected shipping client: real sign-in,
one ordinary rendered chat, reconnect/resume, lifecycle transitions, status terminality, and
authoring/hosting where supported, except only exact current evidence covered by the complete
request→protected approval→registered debt path above. Passing source tests or one client is not
equivalent to a valid release evidence set.

The protected verifier runs the fail-closed changed-code gate from the same pinned policy revision.
The candidate-tree command below is diagnostic only; reproduce it locally with the same immutable
base/candidate and downloaded artifacts:

```bash
python3 scripts/check_changed_coverage.py --base-sha "$BASE_SHA" --candidate-sha "$SHA" --backend-python build/060/coverage/backend.xml --voice-worker-python build/065/coverage/voice-worker.xml --tooling-python build/060/coverage/tooling-python.xml --repository-profile deep --coverage-mode strict --fail-under 90 --output build/060/coverage/changed-code.json
```

</details>

## 11. Rollback and recovery

Do not use ad-hoc deployed SQL. The 060 migration is additive and transactional; a failed attempt
must roll back and leave its revision marker unchanged. For a release-candidate failure:

1. Stop new traffic and the application, but preserve Postgres and all bind-mounted data:

   ```bash
   docker compose stop astraldeep
   ```

2. Preserve a post-failure dump for diagnosis. Restore `build/060/pre-060.dump` into a fresh
   recovery database/volume and verify it before directing traffic or starting the previously
   verified application image. Do not boot an older binary against the migrated production database
   unless that exact pairing has passed the compatibility matrix.
3. If an interrupted candidate revision or maintenance output exists, leave the durable active/
   last-known-good pointer untouched. On restart, recovery may remove unreferenced staging data, but
   it must not delete the prior active bundle or mark failed maintenance inputs complete.
4. For an operator-setting regression, restore the prior `.env` value and recreate the service:

   ```bash
   make apply-config
   curl -fsS http://localhost:8001/readyz
   ```

   `FF_SCHEDULER_EXECUTION=false` remains the fail-closed scheduler posture; disabling it prevents
   new unattended claims but does not erase occurrence/effect history.
5. Do not clear conversation locators merely because a network/service rollback occurred. They are
   cleared only by explicit New chat, definitive sign-out/account switch, or confirmed deletion.
6. Never overwrite `v0.3.0`, `v0.4.0`, or any signed asset. If a published Windows candidate must be
   superseded, publish a newer semantic version after the full matrix passes. A failed Apple
   first-login gate blocks resubmission; it cannot be waived or replaced by Windows/Android evidence.

After recovery, repeat the migration test, focused runtime suites, one continuity cycle on every
affected client, and the same-SHA evidence validator before reopening release eligibility.
