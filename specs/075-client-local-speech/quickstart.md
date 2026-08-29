# Implementation and Verification Quickstart

**Feature**: `075-client-local-speech`  
**Purpose**: Dependency-ordered implementation, local verification, candidate qualification, and
rollback runbook. Run commands from the named repository and do not push product branches until all
locally executable gates are complete.

## 1. Ownership and repository preflight

The authorized feature directory is `specs/075-client-local-speech` on
`codex/075-client-local-speech`. Before every repository's first edit:

```bash
git fetch --prune origin
git status --short --branch
git log -5 --oneline --decorate
git submodule status
```

Preserve unrelated work. The repositories start from these refreshed `main` anchors recorded during
planning: Deep `444b240`, Plane `04ed5fb`, Primitives `4056df9`, Projection `0dcf169`, LETS
`252d0b2`, and kos-wiki `9af695b`. Re-query before a final push because these are planning anchors,
not permanent authority.

Feature 075 changes Plane, Projection, and Deep. It does not change Primitives or LETS. Plane owns
schema/repositories; Projection owns web/native clients and the UI protocol; Deep owns backend
selection, session/dispatch policy, worker reliability, composition pins, and integration tests.

The implementation preflight on 2026-08-28 re-fetched every origin and found no ownership or
Feature-075 collision. The local, unpushed implementation branches are:

| Repository | Branch | Exact refreshed `origin/main` baseline |
| --- | --- | --- |
| AstralDeep | `codex/075-client-local-speech` | `444b240219f9962866522df4e37dbc6f446ad81c` |
| AstralPlane | `codex/075-client-local-speech` | `04ed5fb67977ab87c7c0e43252ae956338b1bc04` |
| AstralProjection | `codex/075-client-local-speech` | `0dcf1699951671f111d4c1a5689c435e3cf50496` |

Primitives remained clean at `4056df95acd992a9f84d883e572760f6da24c88e`; LETS remained
clean at `252d0b2bdf7eccdb7d972fc2fcc6427c462e21fb`. The separately pushed vault feature
branch was at `425941318e2f1c21c9d4462e901aafecaebb7e6b` after the analysis checkpoint.

### Recoverable obsolete-client inventory

Before cleanup, the ignored Deep-root sources are exactly `android-client/` (22 files, 220 KiB by
`du -sk`) and `apple-clients/` (2,507 files, 191,688 KiB). A sorted manifest containing each
directory/symlink path and each file's SHA-256 has aggregate digest
`c88dd173dbe1106782b251aa8b2604f24e934824527a5593b64d5cbd86c076b5`. The Android SDK pointer is
`android-client/local.properties`, SHA-256
`c0aed71de8b4a2792ad6e0be875e9784657504d8d4ab9a522d3b0d2a35707390`; its contents are not
recorded. The reserved recoverable destination is
`/Users/sam/.Trash/AstralDeep-obsolete-clients-075-20260828-01/`, which did not exist at inventory
time and will contain the two source directory names unchanged. Root `.gitignore` lines for
`/android-client/local.properties`, `/android-client/keystore.properties`,
`/android-client/.gradle/`, and `/android-client/core/bin/`, plus the exact
`.git/info/exclude` entry `/apple-clients/`, are the only cleanup candidates. Recompute the same
manifest and abort on any mismatch before moving either directory.

Cleanup executed recoverably on 2026-08-29 after both independent manifests reproduced exactly:
the canonical T004 digest remained
`c88dd173dbe1106782b251aa8b2604f24e934824527a5593b64d5cbd86c076b5`, and manifest-v1 remained
`b1904d1f738ca890499e14107297fa80c7c5432cd62abbe94f004708ae8ebc48` for 2,529 regular files,
190,499,553 logical bytes, 1,072 directories, and the one recorded symlink. The SDK pointer was
copied byte-for-byte to the authoritative standalone Projection checkout at
`/Users/sam/Desktop/Work/AstralProjection/android-client/local.properties`, set to mode `0600`, and
remains ignored. The two obsolete roots were same-filesystem renamed into
`/Users/sam/.Trash/AstralDeep-obsolete-clients-075-20260828-01/`; the directory is mode `0700`,
contains exactly `android-client/` and `apple-clients/`, and reproduces manifest-v1 after the move.
Only the obsolete Deep Android ignore block and local `/apple-clients/` exclude were removed.
Nothing was permanently deleted. Restore requires both original paths to remain absent and both
Trash children to match manifest-v1 before explicitly renaming them back without overwrite or
merge.

## 2. Configuration and safe comparison

Add only the server-owned selector:

```text
VOICE_SPEECH_BACKEND=llm_factory
VOICE_SPEECH_BACKEND=client_local
```

Missing means legacy `llm_factory`. An explicit empty/unknown value makes only voice unavailable.
Do not print `.env`, speech endpoints, keys, LiveKit secrets, tokens, or expanded Compose config.
No client setting, query argument, frame, or header may override this selector.

For a remote comparison, use the existing exact preflight and a bounded synthesized/retranscribed
fixture. Record only phase durations and allowlisted outcomes, never response bodies/audio/text in
ordinary logs. A model-list success is insufficient: exercise real ASR and TTS inference.

## 3. Implement in dependency order

### 3.1 AstralPlane

1. Create guarded migration `075.001` from exact `074.004`.
2. Add/backfill/finalize `voice_session.speech_backend`; add `client_local` transport; make only
   remote fields conditionally nullable; install the named exhaustive constraint.
3. Update immutable repository models/create validation and preserve legacy remote reads.
4. Add empty-database, representative pre-075 upgrade, repeat-run, wrong-predecessor, invalid mixed
   row, local lifecycle, and recovery tests.
5. Bump `SCHEMA_REVISION` and the guarded migration digest. Do not repin Deep until this exact Plane
   candidate passes locally.

Qualified local Plane checkpoint (not pushed):

- Base: `04ed5fb67977ab87c7c0e43252ae956338b1bc04`
- Candidate: `4a1d990387428436041dd70d9c417e9e86000b6c`
- Revision: `075.001`
- Migration digest: `755faecd45a7d8ca9956f25a239bed476802b885efdce29a36dc3b66981f94df`
- Current structural digest: `0b623484495b64cb2557473f6e9d9c1d9f41a6798090641f2ffe65f8c7076b15`
- Evidence: 252 focused tests passed on Python 3.11/PostgreSQL 17 with no skips; Ruff,
  dependency direction, lock, and diff checks passed; changed executable-line coverage was 96%.
- Independent review: exact-range specification and code-quality review clean, with historical
  migration statement bytes unchanged and no dependency/lockfile changes.

### 3.2 AstralProjection shared contract and clients

1. Add v2 capability/session/frame vocabulary, fixtures, ROTE dispositions, and drift guards first.
2. Implement one half-duplex local speech controller contract on web, Windows, Android, iOS,
   macOS, and watchOS. Each adapter proves local-only ASR and local TTS or returns a stable typed-only
   reason; no automatic remote fallback exists.
3. Stop capture/synthesis synchronously on lifecycle loss, serialize announcements, enforce the
   500 ms echo fence, and submit finals only after server turn binding.
4. Add packaging/privacy declarations: Windows helper and Qt speech plugin, Android API-33 runtime/
   gates, Apple Speech usage/privacy declarations. Add no third-party product dependency. Pin the
   owner-approved development-only Microsoft test/coverage packages only in the helper test
   project's separate manifest/lock; an automated guard must prove the product project and publish
   assets contain none of them. Emit Cobertura consumed by the shared `windows_csharp` changed-line
   gate.
5. Fix existing Windows/Android remote grant recovery while retaining v1 bytes.

Qualified local Projection shared-foundation checkpoint (not pushed):

- Base: `0dcf1699951671f111d4c1a5689c435e3cf50496`
- Candidate: `44a3d03e0294612ca95bd64f428316ff5816e9ef` (implementation `9452f3b` plus
  review repair `44a3d03`)
- Remote-v1 fixture SHA-256 retained:
  `bc98077594fa8d51dd664fadefaa48cf596a94e7fb2a961a972dbabca4f02143`
- Local-v2 fixture SHA-256:
  `59b77d9acbfd4c40ec9c2eaa50c30e30090c8da7ff7026786d1f77d8c5984b04`
- Evidence: the unfiltered owning contract/resource/workflow suite passed 107 tests with one
  environment-dependent immutable-source replay skip; the full ROTE suite passed 497 tests; Ruff,
  JSON, all 82 provenance digests, dependency guards, and diff checks passed.
- Independent review: three Important gaps were found in the first round (REST reason/optional
  vocabulary, dependency-authority coverage, and one pre-existing Apple provenance record), all
  were repaired test-first, and a fresh final specification/code-quality review was clean.
- Scope: T013–T017 and the Projection half of T003 only. No web/native runtime adapter was begun.

### 3.3 AstralDeep

Task 4's server-side `client_local` runtime was independently accepted at
`982e8741a54a2622a9c3d1f083dc347c53b5cbde` over exact base
`2532e6a910b5c648242e4c05a784f940a6144d6a`. The final focused lifecycle lane passed 263/263;
aggregate/backend passed 729 with 40 isolated-PostgreSQL environment skips; worker passed 359 with
5 matching skips; tooling passed 87; and strict changed executable-line coverage was
2,306/2,536 (90.93%). The independent full-range reviewer reported PASS with no actionable
findings. The immutable 658,720-byte patch SHA-256 is
`05a0a1b5314d1a5b06dc512d0118d30d281e0f69198d9078aacdf8bfcb8465c2`. Real PostgreSQL and live
client evidence remain mandatory final gates; this checkpoint does not claim them. No product push,
PR, or hosted CI occurred at acceptance.

1. Parse `VOICE_SPEECH_BACKEND` once and expose strict authenticated v2 capability/status.
2. Extend the Plane composition pin only after the Plane candidate is qualified.
3. Create local sessions without worker/room/grant construction; preserve owner, takeover, lease,
   context, mute, stop, end, and cleanup semantics.
4. Implement strict local WebSocket validation/binding, call-stack-only attestation, and the exact
   ordinary transcript-admission/`handle_chat_message` dispatcher seam.
5. Emit only policy-authorized local announcements and consume content-free playout observations.
6. Change remote ASR/TTS retry loops to one total deadline, synchronously warm greeting/earliest
   acknowledgement before worker registration, and add redacted phase metrics.
7. Remove obsolete root Apple/Android client residue only after relocating the local Android SDK
   pointer to Projection. Move recoverable build/IDE residue to a dated Trash directory; remove
   only the now-obsolete root ignore entries.

Qualified local Deep contract/composition checkpoint (not pushed):

- Base: `6d9931dbc43c6c9ff2f0435000c91dd1106e9409`
- Candidate: `5c1fd7cc1fa53fe8b7e335c07282c0bc7bcc9b05` (composition `eec1cfa` plus
  review repair `5c1fd7c`)
- Plane gitlink/manifest: `4a1d990387428436041dd70d9c417e9e86000b6c`, revision `075.001`,
  migration digest `755faecd45a7d8ca9956f25a239bed476802b885efdce29a36dc3b66981f94df`.
- Projection gitlink/manifest remained `0dcf1699951671f111d4c1a5689c435e3cf50496`; its protocol
  digest remained `cf30e7a25087cef4dc9bcff4d272d501eef6b128fa48582d2bdb753a68caf904`.
- Evidence: the required isolated unfiltered owning lane passed 162 tests with zero skips/warnings;
  contract-validator tooling passed 26 tests; Ruff, composition, JSON, dependency, gitlink/export/
  source-origin, and diff checks passed.
- Independent review: four Important proof-strength gaps were found (dependency authority policy,
  complete REST goldens, mandatory validator versions, embedded Plane import origin), repaired
  test-first, and a fresh final specification/code-quality review was clean.
- Scope: T018–T021 and the Deep half of T003. No selector/session/dispatcher runtime was added.

### 3.4 Qualified local web/heartbeat/Stop checkpoint (not pushed)

- Projection foundation: `44a3d03e0294612ca95bd64f428316ff5816e9ef` over exact
  `0dcf1699951671f111d4c1a5689c435e3cf50496`.
- Projection web commits: `f04a28c3fcf3b5be5b00527673fb352d9f1f238d` and
  `928732587878508bf2026aa9ddb26ad9a525259c`.
- Deep accepted runtime: `982e8741a54a2622a9c3d1f083dc347c53b5cbde`; heartbeat and
  post-Stop readiness repairs: `0a339563b9e5f15d4b0b104dea008c9bf9d344c6` and
  `ee729415f2825eaa966fbfefc999ced96a2767f4`.
- Web behavior: strict unprefixed, `processLocally=true` recognition; exact-locale local-service
  synthesis; explicit install gesture; typed fallback; no local-mode RTC/media grant; bounded and
  bound local finals; serialized announcements; echo fence; and Stop-to-empty-PATCH-to-fresh-ready
  recovery with announcement sequence reset only after ready delivery.
- Projection evidence: 96/96 Chromium journeys; changed executable `client.js` statements
  1,463/1,612 (90.76%); 18/18 renderer/composer tests; 25/25 coverage-tool tests; Ruff, ESLint,
  package-manager pin, and diff checks passed.
- Deep evidence: 342/342 focused lifecycle/admission/API tests passed against the sibling
  AstralPlane source; Ruff and diff checks passed. Two independent race-review rounds found the
  initial delivery/authority gaps; all reproduced interleavings were repaired and the final review
  was clean.
- Scope: T039-T043 plus narrow heartbeat/readiness repairs required by the web journey. Native
  adapters, rollback qualification, full local CI, live-device/staging evidence, component repins,
  product pushes, PRs, and hosted CI remain open.

### 3.5 Qualified selector/rollback checkpoint (not pushed)

- Deep implementation: `5b6419feeb43b9647222a19aac4869d5756a218d` over the accepted web
  checkpoint. The Plane pin remains `4a1d990387428436041dd70d9c417e9e86000b6c`, schema revision
  `075.001`; this phase adds no migration or conversation rewrite.
- Selection behavior: one strict process-lifetime authority chooses `llm_factory` or
  `client_local`; malformed explicit values close voice only; service/runtime drift, partial
  authority, durable-row/backend mismatch, and in-flight backend mutation fail closed. Terminal
  end/cleanup remains available across a restart boundary without invoking the wrong media
  strategy. Remote-only v1 routes return the typed local-client upgrade response in local mode.
- Operations/security: both backends honor the independent voice kill switch. Local/staging
  Compose explicitly blank worker-native speech endpoint/key values inherited from environment
  files, and a rendered-Compose sentinel test proves they do not enter the orchestrator. The
  tracked runbook covers drain, force-recreate, dirty-shutdown rows, rollback, typed fallback, and
  a real post-rollback remote smoke without publishing endpoint or credential material.
- Rollback evidence: one isolated PostgreSQL runtime completed local → remote → local
  reconstruction with fresh selectors/repositories, real durable create/end paths, deterministic
  credential-free fake media, byte-equivalent conversation/message snapshots, terminal rows bound
  to their original backends, and unchanged Plane schema revision/migration digest. The real
  Factory smoke and six-client live matrix remain T109/T110 gates.
- Verification: 443/443 focused selector/API/session/bootstrap/lifecycle/topology/rollback tests
  passed against sibling AstralPlane source; 24/24 documentation/quickstart tests passed; Ruff and
  diff checks passed. Changed executable-line coverage was 345/365 (94%, required at least 90%).
  Three independent reviews found credential inheritance, missing executable typed-fallback proof,
  terminal cancellation fencing, partial/distinct selection authority, and missing kill-gate
  fail-open defects; each was reproduced and repaired, and the final 94-test core re-review was
  clean.
- Scope: T044-T048. Native adapters, remote deadline/warm-up optimization, cleanup, full local CI,
  live-device/staging evidence, final component repins, product pushes, PRs, and hosted CI remain
  open.

## 4. Narrow local tests while implementing

Start with a failing test for each behavior and run the smallest owning suite after each slice.
Coverage for changed lines must be at least 90% in every changed language/lane. Required groups
include:

- Plane migration/repository/unit/integration/recovery tests against an isolated PostgreSQL DB;
- Deep selector, v2 API, socket schema, ownership/replay/authorization/PHI/retention, local
  no-worker/no-egress, ordinary dispatcher parity, remote deadline/cache, and composition tests;
- Projection schema/fixture/drift/ROTE tests plus web, Windows, Android, and Apple controller tests;
- first-party Windows helper warning-as-error build, `dotnet format`, unit tests, Cobertura, and
  frozen-package/hash/plugin probes;
- malformed/oversized/extra-key/stale/wrong-owner/wrong-device/wrong-chat/duplicate/out-of-order
  denials and typed fallback;
- blocked-network local journeys proving zero speech egress and no audio/text retention.

Before full qualification, freeze exact clean local candidates. In each repository create a final
content commit whose message contains `[skip ci]`, then create one empty child without the skip
instruction. Deep must pin the exact Plane and Projection child SHAs. Record both parent and child
SHAs; any subsequent tree or SHA change invalidates the affected evidence and restarts this freeze.

## 5. Merge-level local gates

Mirror the exact current workflow invocations rather than assuming root pytest discovers nested
suites. At minimum:

### AstralPlane

```bash
uv lock --check
uv sync --frozen --group ci
uv run --frozen --group ci ruff check .
uv run --frozen --group ci python tests/architecture/test_dependency_direction.py
uv run --frozen --group ci pytest -q -p no:cacheprovider --cov=astralplane --cov-branch --cov-report=xml --cov-fail-under=88.75
uv run --frozen --group ci diff-cover coverage.xml --compare-branch origin/main --fail-under=90
uv build --build-constraints tooling/python-ci/build-requirements.lock.txt --require-hashes
```

The coverage run must use an isolated `ASTRALPLANE_TEST_POSTGRES_DSN`; skipped migration tests are
not success.

### AstralProjection

Run locked Python install/lint/pytest/diff-cover, web lint/unit/coverage/Playwright, Windows
offscreen pytest plus packaged-helper probe, `dotnet format`, helper unit/Cobertura coverage, Android
Gradle lint/unit/Kover/assemble/connected tests, and recursive Swift formatting, AstralCore tests,
unsigned iOS/macOS/watchOS xcodebuild coverage, and xccov union. Use the exact current commands in
Projection's workflows/README.

### AstralDeep

```bash
make sync
ruff check .
make test-backend
docker exec astraldeep bash -c "cd /app/backend && python -m pytest -q"
python scripts/check_doc_links.py
```

Also run every module-local suite and release/composition/changed-coverage command named by the
current Deep workflows. A container restart alone does not copy changed source; rebuild/sync first.

Run Plane's own candidate-aware diff-cover independently. Run Deep's changed-coverage policy once
with the `deep` profile and once against the standalone Projection candidate with the `projection`
profile, including `windows_csharp`; do not claim that this collector owns Plane coverage. Require
at least 90% in every changed lane and retain all report identities under `build/075/coverage/`.

## 6. Candidate-bound live qualification

Run all gates and locally regenerate/parse canonical release evidence against the already-frozen
trigger-child commits. Invoke Deep's parser separately for the exact Deep and Projection bases/
candidates with `deep` and `projection` strict profiles, writing distinct `build/075` results; bind
Plane's native coverage/diff-cover/migration report digests separately. The local parser is
diagnostic; protected CI remains authoritative.

Against the same candidate SHAs/artifacts, qualify:

1. `llm_factory`: real inference readiness, greeting/ack warm path, total-deadline failure, media
   reconnect/takeover, stale grant/proof rejection, and recovered-service latency.
2. `client_local`: supported browser, Windows host, Android physical device, iOS, macOS, and watchOS;
   remote speech endpoints blocked; two-turn conversation; interruption/mute/stop/takeover; sensitive
   result; typed fallback; zero microphone egress.
3. Persistent staging: real PostgreSQL/Keycloak/ordinary agent dispatcher and representative
   migration dataset. Record exact SHAs, manifests, report digests, environment identity, and times.
4. Performance: for every exact supported client posture, record 20 consecutive non-discarded
   trials (one cold, 19 warm) for SC-001/002/003/011 and require at least 19 within each bound. Use
   monotonic timestamps and physical or loopback audio onset where audibility is claimed; retain no
   audio, transcript, identity, endpoint, credential, or engine-path content.

Before those trials, freeze a privacy-safe matrix bound to the exact Plane, Projection, and Deep
candidate SHAs. It must use opaque posture IDs, contain an explicit web/Windows/Android/iOS/macOS/
watchOS slot, and record only client/OS/hardware-class/browser/locale/asset-state plus supported or
typed-only disposition. The parser rejects missing, duplicate, unknown, or SHA-mismatched slots;
changing the matrix invalidates all dependent evidence.

Do not claim audibility from TTS callbacks alone. Physical/acoustic evidence is required. Missing
native/staging evidence blocks merge/release readiness and is not waived by local unit tests.

After the implementation/candidate wiki checkpoint is pushed, re-fetch remote state. Explicitly
push only each repository's `[skip ci]` parent SHA and open all three draft PRs in Plane →
Projection → Deep order. Confirm no workflow uses `pull_request_target` and no `push` or
`pull_request` Actions were triggered by those parent SHAs; unrelated scheduled runs can still
exist. Only
after all three PRs are visible, fast-forward each remote branch to its already-qualified trigger
child so ordinary automatic `push`/`pull_request` CI starts. Never manually dispatch a workflow.
The skipped checks may remain pending until that child arrives; the PRs remain draft and blocked.

## 7. Rollback and recovery

- To restore remote speech, drain/end active local sessions, set
  `VOICE_SPEECH_BACKEND=llm_factory`, restart, verify v2 discovery points to voice-rest/v1, and run a
  real remote smoke. No conversation data rewrite is needed.
- A malformed selector remains voice-off/typed-on; fix configuration and restart.
- Migration rollback is restore of the verified pre-075 backup with the old app, or a new guarded
  forward Plane migration. Do not hand-edit schema or pretend the selector reverses DDL.
- A local TTS/ASR failure after turn acceptance never rolls back the message/task. End/suspend speech
  and keep the visible text result.
- Root client-residue cleanup remains recoverable from the recorded dated Trash directory until the
  candidate is accepted.

Update curated kos-wiki pages, `index.md`, and `log.md` at plan, tasks, implementation, PR, merge,
and release-state checkpoints. Commit/push wiki changes separately from product repositories.
