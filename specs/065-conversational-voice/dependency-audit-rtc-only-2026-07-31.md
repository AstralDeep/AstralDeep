# Feature 065 RTC-Only Worker Dependency Audit — 2026-07-31 Baseline

- **Snapshot refreshed**: 2026-08-01
- **Architecture**: direct LiveKit RTC media worker; no LiveKit Agents runtime
- **Distribution approved**: No
- **T004**: Open
- **T168**: Open

This is the separate audit for the approved RTC-only architecture. It supersedes the rejected
Agents closure as the description of the intended worker, but it is not a release approval. The
historical Agents findings remain in `dependency-audit-2026-07-31.md` and are not reused as proof
for this closure.

The checked-in `backend/voice_agent/CLOSURE.json` is a canonical, deterministic
`astraldeep.voice-worker-closure.inventory.v1` snapshot. Its SHA-256 is
`sha256:9ef9e195cd73ba3ff536b7c4d3ec4c15f8ab13472c7fcfe8dc10e4749da6b074`.
It deliberately records false approval flags and null final-evidence fields. The ordinary final
closure verifier rejects this inventory schema; only the explicit unapproved-snapshot verifier
accepts its integrity. Candidate-controlled bytes therefore cannot approve distribution.

## Exact runtime closure

The direct worker input contains only these four pins:

| Distribution | Exact version | Purpose |
|---|---:|---|
| `livekit` | `1.1.14` | Direct room/audio RTC primitives |
| `numpy` | `2.4.6` | Exact numeric runtime for VAD |
| `onnxruntime` | `1.28.0` | CPU-only Silero ONNX inference |
| `websockets` | `17.0.1` | Bounded authenticated worker-control WSS client |

The hash-only Python 3.11 lock contains exactly nine distributions:

| Distribution | Exact version |
|---|---:|
| `aiofiles` | `25.1.0` |
| `flatbuffers` | `25.12.19` |
| `livekit` | `1.1.14` |
| `numpy` | `2.4.6` |
| `onnxruntime` | `1.28.0` |
| `packaging` | `26.2` |
| `protobuf` | `7.35.1` |
| `types-protobuf` | `7.34.1.20260518` |
| `websockets` | `17.0.1` |

`backend/voice_agent/requirements.lock.txt` is 19,485 bytes with SHA-256
`sha256:fb86c9318d01ce59afaccba57842ddde1d098444e527c70b272b81af4ebc61b3`.
It is binary-only, requires hashes, and includes the reviewed linux/amd64 and linux/arm64 native
wheel hashes below:

| Distribution | Reviewed target-wheel SHA-256 values |
|---|---|
| `livekit==1.1.14` | `80962c4a22ddbf0e0ebd3563fc090fce42df66b39b90de68b161b7db01970f68`, `299146efefad5f67751cd15b8225bae759be0d7ad2f0b4ae1a22c15860d93cf9` |
| `numpy==2.4.6` | `89cd468399cfd2504718f0ba50e410dca55a170b61a02ad92bb18c8a65186e93`, `0ab0a9c4ffb1a6d95ef519fe4247dba8eb6b18ad93999f76b7f657039acabd47` |
| `onnxruntime==1.28.0` | `a166b78ee04f3a37fa1ef82034b6a3ce96d9684e582d4d30b296de83e9998bb5`, `8d66f9ceb29909c70839e4e4fb3435c7b490050d8f162bd5f3aba4ca01ee517f` |
| `websockets==17.0.1` | `d41e9845514754a42d1d83b2fca9d27fee2ca7b3b0bee6843ba5a9bb2b6e25ac`, `d9aac6081513f02eac3f8caace800dbfc5c608b69e4a7bef69e414eabfc95aa1` |

The worker closure excludes `livekit-agents`, `livekit-api`, `livekit-plugins-*`, PyAV,
BlingFire, local inference, OpenAI/provider clients, LLM/tool packages, database drivers, and
Keycloak administration packages. `livekit-api==1.2.0` remains at the orchestrator boundary; its
reviewed wheel SHA-256 is
`307f8e5cfb0358c3ca091814ab768af55896022151bcd7f951954ccefa036a24`.

The test-only lock is separate from the runtime image. Its exact direct input is
`pytest-asyncio==1.4.0`; `backend/voice_agent/requirements-test.lock.txt` is 1,532 bytes with
SHA-256 `sha256:755d9407a376ea9a64307f65fba53d125fdaa808c80e858be898f004c7336215`.

## Exact model and build inputs

| Input | Bytes | SHA-256 |
|---|---:|---|
| `backend/voice_agent/requirements.in` | 519 | `aa59e2e6b8bae7fb23b758d2ce2a31fd995fbf15fd3e566c09d7b5d6c04ab6a1` |
| `backend/voice_agent/requirements.lock.txt` | 19,485 | `fb86c9318d01ce59afaccba57842ddde1d098444e527c70b272b81af4ebc61b3` |
| `backend/shared/voice_transcript.py` | 12,074 | `ef281afbe6ecf17739b464d24cd02ab6ee8a435fab3d9dc727fc23bbfc067249` |
| `Dockerfile.voice` | 10,937 | `86c246452f4c2d720f70a49b26d57f1ddd47e53d506173f87e316a6f7a83e943` |
| `Dockerfile.voice.dockerignore` | 920 | `dd045c071183b84e1b7612646efc5c271f2fa80dcabb104088ec71265da17b37` |
| Silero v6.0 ONNX | 2,327,524 | `sha256:597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004` |
| Vendored Silero MIT license | 1,076 | `51c19c8be941a3fb00ccf58f0bf9053de9f7237a0b37327896eabad32dffe873` |
| Silero provenance JSON | 529 | `144e92f17546c15e8c71956947cb0d53acf98f08ba4bfe156a721b30685ebe0c` |

The model is the exact upstream `v6.0` artifact from commit
`fba061dc5559f696e62171e9a0741782b0fdc23c`. The provenance binds upstream path
`src/silero_vad/data/silero_vad.onnx`, upstream license SHA-256
`2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b`, and the
vendored model/license bytes. No build or runtime model download is allowed.

## Base and image status

The reproducible local-development fallback remains:

- OCI reference: `python:3.11.15-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba`
- linux/amd64 manifest: `sha256:28255a3ace7eb4c48bc1b57b90af29e1bc82b4fd6c60614a8e3dce61b87ff941`
- linux/arm64 manifest: `sha256:3df1d95e3529533d0b646640edb63a0fde8a68597c0e7c62d34c4176678bb7d1`

That fallback is explicitly distribution-unapproved. The final DHI reference/index/platform
manifests are null in `CLOSURE.json`, as are both final worker platform digests. A local Docker
image content ID is not substituted for an OCI manifest digest.

### Local fallback diagnostics captured 2026-08-01

Both supported architectures were rebuilt from the exact inputs above with the fallback base. The
runtime images executed as UID/GID `10001:10001`, contained the exact nine-distribution closure,
had no pip/setuptools/wheel runtime installer, constructed a LiveKit 16-kHz audio frame, and ran
real inference through the vendored Silero model. The hash-locked packaged worker suite passed
`256` tests on each architecture.

| Target | Architecture | Local OCI index/content ID | Platform manifest | Config |
|---|---|---|---|---|
| runtime | linux/arm64 | `sha256:d341b83242d496a69de1c257677e311a1485b663ed20103eb1d0e9073928bf27` | `sha256:fb63d79ff95cc655c28feb8da9bf9a95ddbfeef2c60cf65451457d858fd4fec4` | `sha256:b0ce5f39205c92ceffc6e1515b2d5c8b0b971fd5bcdf9206ab8b2b99e4792a5e` |
| runtime | linux/amd64 | `sha256:e1a31b071f274e0d93ac19afe2b7d5b9d2cfecc3b2c9bb66a4309358e3210dc9` | `sha256:ec3bf71b6cacc343e9da596c8b16982ec3ba58b4e2e731dd34555b1307bac840` | `sha256:a4627e86d50576a0629b87703ace224c9dee44e9c597f902ca214e67b2bfca7d` |
| test | linux/arm64 | `sha256:6630d851fef0a18bcc2e5b92813495551f21bb554aa2a400a21e119f35bd2339` | `sha256:4f1045009f68e17d9d5648b1c9960f12069c56b922fd6ef9e34b812a7e7b0e39` | `sha256:841ba2da7fd17c6b4ecb18bddf90881c427d4d4ae46f3c7a7438bd28e9847e4d` |
| test | linux/amd64 | `sha256:af7cee75bf300b7690b7412a9157945efad966110d8e9f0f6f95b0d4bbea8b77` | `sha256:4fe422dd0d167f62628f277c550d4ed055a75ad990fc5b1bc8650051e42077be` | `sha256:28e282e17c9ffd949ddf251eca25e399d9e3fba586f1fa994959fd6a4fccc6ae` |

Trivy `0.72.0` scanned both local runtime targets with database version 2 updated
`2026-07-31T19:19:43.338098622Z`. The database SHA-256 was
`7ad4c48ca179b5a67c44ccbeffa3b09c2181244ba2b4f48ea08bd169ab243c9a` and its
metadata SHA-256 was
`84696791e1de06192c5168c2d6403b75458a3c04ed2249403818ea3715f7543a`.

| Architecture | Raw local report SHA-256 | Python findings | OS HIGH | OS CRITICAL |
|---|---|---:|---:|---:|
| linux/arm64 | `650ec4e7ddafba29b7416493ef24510fb54488f708b0c68cff0a32cf01bdcf9c` | 0 | 18 | 6 |
| linux/amd64 | `cfd67fbcd349b05788dd046c363c72503ec3c271ad7b73413b0c361bafb87624` | 0 | 18 | 6 |

The raw reports are local temporary diagnostics and are not committed or candidate-bound release
evidence. The identical blocking OS findings confirm that the fallback cannot satisfy the final
zero-HIGH/CRITICAL gate. These local fallback identities therefore remain absent from the final
image fields in `CLOSURE.json` and do not satisfy DHI, signature, SBOM, VEX, or protected-policy
requirements.

## Open release gates

| Gate | Recorded status | Approved | Evidence digest |
|---|---|---:|---|
| DHI base verification | `not_verified` | No | `null` |
| Multi-architecture final images | `not_verified` | No | `null` |
| Vulnerability scan | `not_run_against_final_images` | No | `null` |
| Signature verification | `not_produced` | No | `null` |
| SBOM verification | `not_produced` | No | `null` |
| VEX verification | `not_produced` | No | `null` |
| Protected approval | `not_approved` | No | `null` |

T004 cannot close until a login-backed final DHI identity is verified, linux/amd64 and linux/arm64
runtime/test images are produced from the same closure, both execute the locked tests and real
Silero inference, both pass native/license and zero-HIGH/CRITICAL scans, signature/SBOM/VEX
evidence is independently verified, and protected policy binds an owner-reviewed exact final
`CLOSURE.json` digest. T168 additionally requires the locked direct-RTC worker integration run
against the strict fake speech service and digest-pinned LiveKit, with exact config/model/image
digests recorded in candidate-bound verification evidence.

The 2026-08-01 local preflight now exercises that intended integration path without weakening the
candidate requirement. A fresh Linux/arm64 `voice-worker-test` image
(`sha256:aebe3588b4fd3d3c720cde29cf1aa2f505386901d9e5d8c957a6464a5cc6c4a8`) passed all 261 default
networkless tests with the opt-in case deselected. Its isolated lane then passed 1/1 against
LiveKit `v1.13.5@sha256:3497163e15c48fef6e7830c78716f9e9d5edc28abf7aa90b61c86e93bbc306b1`.
The lane pre-pulls/builds before minting 90-second grants, proves nonzero microphone PCM through
real RTC/Opus into the production ASR WAV, and returns correlated reliable transcript/result data
plus nonzero 24-kHz TTS PCM. It removes its disposable containers and internal network. Integration
Compose/config SHA-256 values are
`d83c7c17fb936354bc8bafb9f4c9215da28cdbcbb09196abf72653b54d475cd2` and
`b935d38ad1f39cbb57cdfdf883e02c5a474783f325924f78fad39b1d7f052d85`; closure/lock/Silero values
remain `9ef9e195cd73ba3ff536b7c4d3ec4c15f8ab13472c7fcfe8dc10e4749da6b074`,
`fb86c9318d01ce59afaccba57842ddde1d098444e527c70b272b81af4ebc61b3`, and
`597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004`. This was an uncommitted
local diagnostic, so it is readiness evidence for the lane rather than T168 completion.

No DHI identity, multi-architecture final image digest, signature, SBOM, VEX, clean scan, protected
approval, T004 completion, or T168 completion is asserted by this document.
