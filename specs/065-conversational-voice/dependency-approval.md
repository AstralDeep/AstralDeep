# Feature 065 Dependency Approval

**Feature**: `065-conversational-voice`
**Authorized base**: `origin/064-mcp-2026-07-28-decision@7e99f49`
**Decision date**: 2026-07-31
**Decision**: The repository owner/lead developer approved the **RTC-only replacement** for local
implementation on 2026-07-31 and authorized the implementation decisions needed in this session.
The prior LiveKit Agents closure is rejected and MUST NOT be installed, distributed, uploaded, or
published. The approved replacement boundary is direct worker RTC (`livekit==1.1.14`), exact
Silero v6.0 ONNX inference (`numpy==2.4.6`, `onnxruntime==1.28.0`), bounded worker-control WSS
(`websockets==17.0.1`), and orchestrator-only `livekit-api==1.2.0`. Distribution remains
fail-closed until the regenerated exact closures,
minimal runtime image, notices, multi-architecture tests/scans, and closure fingerprint below are
complete and receive matching PR review.
**Authority**: The owner first replied `approve both` to the exact dependency-review gates and then
explicitly stated, `i approve of any decision you make in this session, i need conversation in this
system`, after the RTC-only replacement and its AstralDeep-owned state-machine consequences were
presented.

This record authorizes only the media-only voice architecture in the feature artifacts. It does
not authorize a worker LLM/tool path, an environment-backed AstralDeep LLM, a user-editable speech
provider, raw-audio retention, a LiveKit server fork, or a release. The implementation PR must
review this record and the generated locks again before merge.

The CI worker lane is disabled unless the repository owner sets the external
`VOICE_WORKER_CLOSURE_APPROVED=true` repository variable after this record, the exact
replacement closure, and its matching review are complete. Runtime image export and artifact
upload are physically absent from the lane while this decision is reopened, and the protected
readiness caller independently requires the same external signal. The variable is intentionally
unset; candidate code alone is not an approval signal. Before export is restored, protected policy
must also bind approval to the exact closure fingerprint rather than trusting this persistent
boolean for future lock or image changes.

## Rejected Agents audit and approved RTC-only replacement

The complete artifact audit invalidated the original assumption that the Agents closure was a
routine Apache-2.0 stack. Neither the current lock nor the initially considered 1.6.0 fallback may
be treated as approved:

- `livekit-agents==1.6.7` unconditionally installs `livekit-local-inference==0.2.6`. Importing the
  Agents/Silero path eagerly loads that separately licensed native model even when AstralDeep asks
  for the independent Silero ONNX plugin. The owner authorized local installation for this session,
  but has not authorized redistributing the model in a CI artifact or container registry.
- `livekit-agents==1.6.0` is the newest 1.6.x release without local inference, but it hard-pins
  `json-repair==0.59.10`, affected by
  [GHSA-xf7x-x43h-rpqh](https://github.com/mangiucugna/json_repair/security/advisories/GHSA-xf7x-x43h-rpqh)
  (High, CVSS 7.5). The fixed `0.60.1` cannot satisfy that wheel's metadata. It also predates
  shutdown, transcript-close, recovery, endpointing, idle-timer, and RTC teardown fixes relevant to
  this feature.
- Both Agents selections install PyAV 18.0.0. Its Linux wheels bundle FFmpeg, x264, and x265 while
  reporting LGPLv3. PyAV's exact vendor patch moves x264/x265 out of FFmpeg's GPL list, but
  [FFmpeg's own 8.1.2 license](https://github.com/FFmpeg/FFmpeg/blob/n8.1.2/LICENSE.md) says those
  combinations require GPL. No commercial exception or complete bundled-library license/source
  evidence is present in the wheel or its incomplete auditwheel SBOM. Notices alone do not resolve
  that conflict.
- Both Agents selections install `livekit-blingfire==1.1.0`, whose native wheels statically embed
  Microsoft BlingFire and two models but omit the Microsoft MIT notice and do not attest the exact
  unpinned upstream commit used by the wrapper build.
- The exact official Python slim base currently reports 24 Debian HIGH/CRITICAL findings without
  vendor fixes. The two fixed Python-tooling findings originally exposed in the runtime
  (`CVE-2026-23949` and `CVE-2026-24049`) have been removed; the remaining OS findings still require
  a minimal-runtime or documented reachability/remediation decision before approval.

The **APPROVED ARCHITECTURE** is an RTC-only media worker. The initial combined resolution measured
21 distributions with direct `livekit==1.1.14`, `livekit-api==1.2.0`, `numpy==2.4.6`, and
`onnxruntime==1.28.0`, plus the exact Silero v6.0 ONNX/MIT artifacts below. The final boundary moves
`livekit-api` into the orchestrator. A boundary audit then proved the worker needs one generic WSS
client for its authenticated pool-control channel because the RTC SDK provides none; exact
`websockets==17.0.1` adds no transitives and replaces a rejected home-grown RFC 6455 transport.
The worker lock therefore contains nine distributions from RTC, NumPy, ONNX Runtime, and this
control client. This removes Agents, PyAV/FFmpeg, BlingFire, local
inference, OpenAI, sounddevice, gRPC, Pydantic, and OpenTelemetry while retaining direct LiveKit
room/audio primitives. The 2026-07-31 OSV/PyPI snapshot returned zero findings for the initially
resolved replacement set. AstralDeep owns endpointing, capture gating, interruption, reconnect
fencing, playout, and cleanup instead of delegating them to `AgentSession`. The plan and tasks now
encode that decision; regenerated locks, image audit, and Spec Kit Analyze remain required before
the replacement closure can be distributed.

## Artifact inventory and current status

| Surface | Artifact / status | Immutable identity / reviewed impact |
|---|---|---|
| LiveKit server | `livekit/livekit-server:v1.13.5` | OCI index `sha256:3497163e15c48fef6e7830c78716f9e9d5edc28abf7aa90b61c86e93bbc306b1`; amd64 manifest `sha256:d0d1cfdbe95617647bbe91630454526c2cdd88cec83f41114b3495b444918b9a`; arm64 manifest `sha256:804b0d2cfffb5b8f95a9cc5aa47a7b715605d1a527095f45dcc8e94b9cf9920e`; separate media service, never the authority plane |
| RTC-only worker direct pins (approved; final image gate open) | `livekit==1.1.14`, `numpy==2.4.6`, `onnxruntime==1.28.0`, `websockets==17.0.1` | Nine-distribution media/VAD/control runtime. Lock SHA-256 `fb86c9318d01ce59afaccba57842ddde1d098444e527c70b272b81af4ebc61b3`; exact amd64/arm64 native wheels are included. No Agents, API, provider, LLM, tool, or general HTTP package may enter this image. |
| Worker control WSS | `websockets==17.0.1` | Python >=3.11, zero runtime transitives, BSD-3-Clause, PyPI/OSV zero findings on 2026-07-31; amd64 wheel SHA-256 `d41e9845514754a42d1d83b2fca9d27fee2ca7b3b0bee6843ba5a9bb2b6e25ac`, arm64 `d9aac6081513f02eac3f8caace800dbfc5c608b69e4a7bef69e414eabfc95aa1`; only the fixed-origin bounded control connector may import it. |
| Orchestrator LiveKit API (approved placement) | `livekit-api==1.2.0` | Wheel SHA-256 `307f8e5cfb0358c3ca091814ab768af55896022151bcd7f951954ccefa036a24`; orchestrator-side scoped token/room operations only; prohibited from the worker lock. |
| Worker VAD (approved) | Silero v6.0 ONNX + `onnxruntime==1.28.0` | Exact upstream model/MIT artifacts in the fixed-profile table; no `livekit-plugins-silero` or local-inference package. |
| Rejected historical worker | `livekit-agents==1.6.7`, worker RTC `livekit==1.1.13`, `livekit-plugins-silero==1.6.7` | Historical audit evidence only. This closure and its PyAV/BlingFire/local-inference payloads are prohibited from build, CI artifact upload, registry, and release. |
| Web | `livekit-client@2.21.0` | npm tarball SHA-256 `205a3d49070d350702dc44b6c20045c0cc2aae22f19117efa8ebea7375caf097`; bundled `dist/livekit-client.umd.js` SHA-256 `a77a2f4c363e93099d7c135721c9ec81d6c5bacc691796dad799222e33cbfb31`; reviewed third-party notice SHA-256 `53c4b66c4a3a2c2c595fdce7c8d4a4d6389bec04843e80bce2ab6b21797e823e`; vendor locally, never from a CDN |
| Windows | `livekit==1.1.14` | `win_amd64` wheel SHA-256 `b8f8d38f131956297923e520bc4375bc9ebfa255cab7f125cb7755bfca71df24`; regenerate the existing release lock and frozen-runtime manifest |
| Android | `io.livekit:livekit-android:2.27.0` | Maven Central AAR SHA-256 `d3a85158392a0bf0ed0d835d4d5932ef3f166bbad2c80bb9a9b6bd08c42ac0a7`; WebRTC `144.7559.09` AAR SHA-256 `d2542864ce012f188d0b2d5da21f5cc48bacc6d46523d25f7515809d424780c6`; AudioSwitch commit `039a35…` AAR SHA-256 `c8240221daa9a96d4ea01a4dc6f6f6b10b4903d2a71f9b57f838bdfeb6c3fcbc`; override vulnerable published `protobuf-javalite 3.22.0` to a compatible exact `>=3.25.5` lock |
| iOS/macOS | LiveKit Swift `2.15.3` | SwiftPM package is restricted to AstralApp iOS/macOS targets; WebRTC XCFramework `144.7559.11` checksum `07c5caf718058af3c528dcabd257298c40e5a8527e4fb9f47c48336ba5899853`; UniFFI `0.0.6` checksum `0d3f2ce159a224c728f8b131068d53bbf9b13d968cda0edc68a6a2290f2651ed`; lock ranged SwiftProtobuf exactly; no watchOS slice or AstralCore dependency |
| Contract validator | `jsonschema==4.25.1` | Wheel SHA-256 `3fba0169e345c7175110351d456342c364814cfcf3b964ba4587f22915230a63`; isolated test image only |
| OpenAPI validator | `openapi-spec-validator==0.7.2` | Wheel SHA-256 `4bbdc0894ec85f1d1bea1d6d9c8b2c3c8d7ccaa13577ef40da9c006c9fd0eb60`; isolated test image only |

The shortened hashes above are descriptive only. Every install/build consumes the full hashes from
the committed lock, vendored checksum, Maven verification metadata, SwiftPM resolved state, or OCI
digest. A shortened value is never accepted by tooling.

## Fixed speech and model artifacts

| Function | Required identity | Artifact rule |
|---|---|---|
| ASR | `Systran/faster-whisper-large-v3` | Exact ready inventory from the operator speech service; no model substitution or local download |
| TTS | `speaches-ai/Kokoro-82M-v1.0-ONNX` | Exact ready inventory from the operator speech service; no model substitution or local download |
| Voice | `af_heart`, English (US), 24 kHz | Exact readiness probe and candidate-bound inventory/profile digest |
| VAD | Silero VAD v6.0 | 2,327,524-byte ONNX payload, SHA-256 `597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004`; exact upstream tag commit `fba061dc5559f696e62171e9a0741782b0fdc23c`, MIT license SHA-256 `2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b`; image must retain that MIT notice and provenance |

The speech-service inventory may not expose underlying model-file hashes. In that case the
candidate evidence binds the exact advertised IDs, voice metadata, sample rate, readiness response
digest, configured endpoint identity, and worker image/config digest; it must not invent a file
hash the service did not attest.

## Approved replacement closure gate

Before distribution is enabled, the replacement implementation must prove all of the following:

- The worker input contains exactly the approved direct pins `livekit==1.1.14`, `numpy==2.4.6`,
  `onnxruntime==1.28.0`, and `websockets==17.0.1`; the complete nine-distribution Python 3.11
  amd64/arm64 lock is hash-only and contains no
  `livekit-agents`, `livekit-api`, `livekit-plugins-*`, PyAV, BlingFire, local-inference, OpenAI,
  provider, LLM, tool, database, or Keycloak package.
- `livekit-api==1.2.0` is installed only with the orchestrator and the worker receives only a
  short-lived room-scoped join grant. Neither the LiveKit API secret nor the speech credential is
  sent to a client or stored in a durable worker artifact.
- The worker image contains the exact Silero ONNX bytes, upstream MIT license, provenance record,
  and expected size/hash; no model is fetched during build or runtime.
- Both amd64 and arm64 images import the direct RTC and ONNX runtimes, execute real Silero inference,
  pass the worker tests, run non-root with no package installer, and pass native-object/license/SBOM
  and current high/critical vulnerability scans against an immutable minimal-base digest.
- A canonical closure manifest binds the worker input/lock, Dockerfile, base OCI index and platform
  manifests, Silero model/license/provenance, produced platform image digests, and scan database/time.
  Protected policy compares its exact digest to an owner-reviewed value; the persistent boolean is
  never sufficient by itself and any byte/version/advisory change reopens approval.

The immutable public Python 3.11 slim fallback builds and passes both architecture tests but has
24 unfixed Debian HIGH/CRITICAL findings and remains labeled distribution-unapproved. The preferred
final base is Docker Hardened Images Python 3.11 Debian 13, whose catalog currently reports zero
HIGH/CRITICAL findings; registry pull, signature/SBOM/VEX verification, and the final dual-arch
scan require an operator `docker login dhi.io`. Local feature implementation may continue on the
fallback, but the final distribution-approved closure fingerprint, image export, and T004
completion remain blocked until that login-backed verification succeeds. A checked-in canonical
inventory may record the exact local inputs and explicit false/null gate state, but it is not that
final fingerprint and cannot authorize distribution.

## Historical rejected closure evidence

The following facts explain the rejected Agents decision. They do not describe the approved
replacement build and MUST NOT be used to re-enable distribution.

- Worker resolution is Python 3.11, binary-only, for both Linux x86_64 and aarch64. The reviewed
  closure contains 75 distributions, downloads approximately 138.70 MiB/132.01 MiB respectively,
  and includes native RTC, BlingFire, PyAV, ONNX Runtime,
  `pydantic-core`, NumPy, gRPC, and other ABI-specific wheels. `livekit-agents` also resolves the
  OpenAI client and OpenTelemetry packages; these are package-level transitives only. The worker
  import/runtime guard must deny LLM, AstralDeep tool, database, Keycloak-admin, and user-authority
  use despite their presence.
- `livekit-local-inference==0.2.6` is an unconditional worker transitive under
  `Apache-2.0 AND LicenseRef-LiveKit-Model`. Its model terms allow the embedded model only with
  LiveKit Agents, prohibit standalone/other-framework use and use to improve unrelated models, and
  require the notice on redistribution. The x86_64/aarch64 wheels are respectively
  `d6379f9d5ee4753919d10a2cedd2e16e1cbf634e496ad21fb54564513b731e69` and
  `c684ee2d2f22c0a24ff471cdd5d873ee010135a9469d791c2cd2d3f83b219b51`; `MODEL_LICENSE` is
  `dfd8e206e8d2f207c6e4ca174600610287afc649005127dabe68be74ccf60348`. Because this is a
  restricted model license rather than an ordinary permissive dependency, local installation was
  permitted only for this development session. CI upload, registry push, or release remains blocked
  until the owner explicitly accepts the terms and redistribution, or the dependency is removed.
- The Windows release lock resolves the existing client/build chain plus `livekit==1.1.14` for
  Python 3.11/win_amd64 and must hash every one of its 66 target distributions. The tracked
  cross-host lock also retains the existing macOS-only `macholib` resolution helper, for 67 pins
  total. PyInstaller must collect only the reviewed RTC native payload alongside the existing
  QtMultimedia closure.
- The isolated validator closure contains 19 Python 3.11 distributions and never enters the
  backend, voice-worker, or client runtime images; its compressed download is approximately
  2.06 MiB.
- Android locks the 2,569,227-byte LiveKit AAR, the 22,600,113-byte WebRTC native AAR, and
  95,318-byte AudioSwitch AAR only from narrowly content-filtered JitPack. The published
  `protobuf-javalite 3.22.0` is prohibited by CVE-2024-7254/GHSA-735f-pc8j-v9w8; dependency
  resolution must select and test an exact compatible `3.25.5` or newer patched release. Apple
  locks the 66,602,249-byte WebRTC XCFramework, 5,544,487-byte UniFFI binary target, and exact
  SwiftProtobuf source resolution. Web vendors the 561,757-byte minified UMD bundle under the
  repository's chosen filename with a generated notice for its bundled Apache-2.0, BSD-3-Clause,
  MIT, and 0BSD dependencies.
- The web package's exact v2.21.0 source lock resolves eleven runtime-manifest packages. Nine
  contribute runtime code to the UMD: `@livekit/mutex`, `@livekit/protocol`,
  `@bufbuild/protobuf`, `events`, `jose`, `loglevel`, `sdp-transform`, `webrtc-adapter`, and its
  `sdp` transitive. `tslib` is a runtime-manifest dependency with no separately mapped source in
  this UMD, while `typed-emitter` is used only for types; their licenses are retained
  conservatively in the notice. The source map also proves that the bundled LiveKit source retains
  MIT-licensed code derived from `ts-debounce`. The adjacent reviewed notice contains exact
  versions, package-artifact hashes, copyrights, and complete BSD-3-Clause/MIT/0BSD terms; the
  adjacent LiveKit license provides the complete Apache-2.0 terms.
- The LiveKit package metadata is predominantly Apache-2.0, but that label is not a substitute for
  auditing bundled native code. The stock PyAV wheel contains x264/x265 and an unresolved
  GPL/LGPL classification conflict; BlingFire provenance/notices are incomplete; and the Silero
  plugin omits the model's MIT notice. These findings have reopened the worker approval. Any future
  closure must map every native/model payload to exact provenance, license/notice bytes, and source
  obligations before distribution.
- The 2026-07-31 OSV/PyPI snapshot returned zero findings for the exact 1.6.7 Python distributions,
  but that does not cover bundled native code or the base filesystem. The 1.6.0 fallback has one
  High Python advisory. Trivy 0.72.0 found no vulnerability in the exact pinned LiveKit server
  image, but found the 24 unfixed Debian findings described above in the worker base. The server
  module inventory also maps `golang.org/x/crypto v0.54.0` to GO-2026-5932 in deprecated
  `openpgp` packages. This server finding is resolved for the exact v1.13.5 image: source scans and
  extracted amd64/arm64 binary scans with `govulncheck v1.6.0` found zero symbol and zero imported-
  package vulnerabilities, `go list -deps` contained no `openpgp` package, and both binaries
  contained no `openpgp` symbol. It remains one module-only advisory because other `x/crypto`
  packages are used. The exact evidence is recorded in `dependency-audit-2026-07-31.md`; an image,
  build, or advisory-database change requires a rerun. The prebuilt WebRTC/native artifacts still
  require image/binary/SBOM scanning; registry metadata alone is not a clean bill of health. Any
  known unmitigated high/critical advisory, artifact drift, or resolver change reopens this approval.
- The current native worker wheels require glibc 2.28 or newer. `sounddevice` is pure Python but requires
  PortAudio if imported; production code must avoid LiveKit's legacy CLI path or explicitly add and
  approve `libportaudio2` rather than failing at runtime.

## Reproducibility and change control

The authoritative exact closures are the generated files below, not a floating resolver result:

- `backend/voice_agent/requirements.lock.txt`
- `specs/065-conversational-voice/dependency-audit-2026-07-31.md` (dated diagnostic evidence;
  never an approval by itself)
- `windows-client/requirements-release.lock.txt`
- `tooling/contract-ci/requirements.lock.txt`
- `backend/webrender/static/vendor/livekit-client.sha256`
- `backend/webrender/static/vendor/THIRD_PARTY_NOTICES.livekit-client`
- `backend/webrender/static/vendor/THIRD_PARTY_NOTICES.livekit-client.sha256`
- Android dependency verification/lock metadata and content-filtered repositories
- `apple-clients/AstralApp/AstralApp.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved`
  plus checked binary-target checksums
- digest-pinned LiveKit and worker images plus candidate-bound normalized config/model-profile hashes

Direct pin changes, new distributions, different native artifacts, changed licenses, yanked files,
or new high/critical advisories require a new explicit owner decision. Matching implementation-PR
review remains a merge gate even though dependency work is authorized to begin.

## Historical packaging evidence from the rejected closure

The following artifacts were selected while implementing the locked image boundary after the
original direct-pin decision. The owner authorized local installation for this development
session, but that does **not** authorize publishing or redistributing the resulting worker image.
They remain part of the open T002/T004 review together with `LicenseRef-LiveKit-Model`:

- Runtime base: official `python:3.11.15-slim-bookworm` OCI index
  `sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba`
  (amd64 manifest `sha256:28255a3ace7eb4c48bc1b57b90af29e1bc82b4fd6c60614a8e3dce61b87ff941`,
  arm64 manifest `sha256:3df1d95e3529533d0b646640edb63a0fde8a68597c0e7c62d34c4176678bb7d1`).
- The initially copied `libgomp.so.1` and full-Python source stage were removed after an ELF
  dependency audit found no `DT_NEEDED` consumer on either supported architecture and real Silero
  session construction succeeded without it. A fail-closed image audit now rejects a future
  unreviewed `DT_NEEDED` dependency on `libgomp`; the strict build allowlist prevents the removed
  payload from being copied back through the retired full-Python/native-library stage.
- Test-only async plugin: `pytest-asyncio==1.4.0`, Apache-2.0, universal wheel SHA-256
  `933ca923a23075a87fb7070c0ec272a6848489824d887c85c812670932835aa1`.
  It is declared in a separate test manifest and is mechanically absent from the runtime target.

The locally built 1.6.7 images include the exact `MODEL_LICENSE` notice and now add the exact Silero
v6.0 MIT text plus commit/model provenance omitted by the plugin wheel. They deliberately carry a
`org.astraldeep.voice.distribution-approved=false` label instead of an incomplete image-wide SPDX
claim. PyAV/FFmpeg classification, BlingFire provenance/notices, the restricted local-inference
model, and the 24 unfixed base-image findings remain blockers. Uploading the image tar, pushing it
to a registry, or allowing CI artifact upload remains prohibited. Approval of the restricted model
alone would not resolve the other closure blockers.
