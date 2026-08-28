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

### 3.2 AstralProjection shared contract and clients

1. Add v2 capability/session/frame vocabulary, fixtures, ROTE dispositions, and drift guards first.
2. Implement one half-duplex local speech controller contract on web, Windows, Android, iOS,
   macOS, and watchOS. Each adapter proves local-only ASR and local TTS or returns a stable typed-only
   reason; no automatic remote fallback exists.
3. Stop capture/synthesis synchronously on lifecycle loss, serialize announcements, enforce the
   500 ms echo fence, and submit finals only after server turn binding.
4. Add packaging/privacy declarations: Windows helper and Qt speech plugin, Android API-33 runtime/
   gates, Apple Speech usage/privacy declarations. Add no third-party product dependency.
5. Fix existing Windows/Android remote grant recovery while retaining v1 bytes.

### 3.3 AstralDeep

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

## 4. Narrow local tests while implementing

Start with a failing test for each behavior and run the smallest owning suite after each slice.
Coverage for changed lines must be at least 90% in every changed language/lane. Required groups
include:

- Plane migration/repository/unit/integration/recovery tests against an isolated PostgreSQL DB;
- Deep selector, v2 API, socket schema, ownership/replay/authorization/PHI/retention, local
  no-worker/no-egress, ordinary dispatcher parity, remote deadline/cache, and composition tests;
- Projection schema/fixture/drift/ROTE tests plus web, Windows, Android, and Apple controller tests;
- malformed/oversized/extra-key/stale/wrong-owner/wrong-device/wrong-chat/duplicate/out-of-order
  denials and typed fallback;
- blocked-network local journeys proving zero speech egress and no audio/text retention.

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
offscreen pytest plus packaged-helper probe, Android Gradle lint/unit/Kover/assemble/connected tests,
and recursive Swift formatting, AstralCore tests, unsigned iOS/macOS/watchOS xcodebuild coverage,
and xccov union. Use the exact current commands in Projection's workflows/README.

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

## 6. Candidate-bound live qualification

Only after the local gates are green, create exact candidate commits and locally regenerate/parse
canonical release evidence. The local parser is diagnostic; protected CI remains authoritative.
Then make the first intentional product push and open draft PRs in dependency order: Plane,
Projection, Deep. Do not manually dispatch GitHub Actions.

Against the same candidate SHAs/artifacts, qualify:

1. `llm_factory`: real inference readiness, greeting/ack warm path, total-deadline failure, media
   reconnect/takeover, stale grant/proof rejection, and recovered-service latency.
2. `client_local`: supported browser, Windows host, Android physical device, iOS, macOS, and watchOS;
   remote speech endpoints blocked; two-turn conversation; interruption/mute/stop/takeover; sensitive
   result; typed fallback; zero microphone egress.
3. Persistent staging: real PostgreSQL/Keycloak/ordinary agent dispatcher and representative
   migration dataset. Record exact SHAs, manifests, report digests, environment identity, and times.

Do not claim audibility from TTS callbacks alone. Physical/acoustic evidence is required. Missing
native/staging evidence blocks merge/release readiness and is not waived by local unit tests.

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
