# Feature 065 Dependency Audit Evidence — 2026-07-31

This is immutable historical diagnostic evidence for the **rejected LiveKit Agents worker
closure**. It does **not** describe or approve the RTC-only replacement and must not be used to
enable CI installation, artifact upload, registry push, merge, or release. The current decision and
replacement gates remain in `dependency-approval.md`; the replacement receives a separate audit
record so these captured hashes/results are not rewritten.

## Audited inputs

| Input | SHA-256 / identity |
|---|---|
| `Dockerfile.voice` | `343d3d527d351398071e611662e6ec70e23dbfc4360bea8a0b570c3ebac65b4e` |
| `Dockerfile.voice.dockerignore` | `b298920b320e3fb1d5cb98d05a974e26f5fa9d7ac105a1ed81385df206af31ab` |
| Worker lock | `4a480c063389574f5b14854946cf63b7f46878150f92c310860a4c58df189f7c` |
| ELF audit | `771e50923ee563e7fedb46dc8914d47df1adca59066cb67f4f23a974cc2b8d8c` |
| Vendored Silero MIT notice | `51c19c8be941a3fb00ccf58f0bf9053de9f7237a0b37327896eabad32dffe873` |
| Silero provenance record | `144e92f17546c15e8c71956947cb0d53acf98f08ba4bfe156a721b30685ebe0c` |
| Worker base OCI index | `python:3.11.15-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba` |
| Worker base amd64 manifest | `sha256:28255a3ace7eb4c48bc1b57b90af29e1bc82b4fd6c60614a8e3dce61b87ff941` |
| Worker base arm64 manifest | `sha256:3df1d95e3529533d0b646640edb63a0fde8a68597c0e7c62d34c4176678bb7d1` |

The Silero model was independently fetched from upstream tag `v6.0`, commit
`fba061dc5559f696e62171e9a0741782b0fdc23c`, path
`src/silero_vad/data/silero_vad.onnx`. It was 2,327,524 bytes with SHA-256
`597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004`, exactly
matching the hash-locked plugin payload. Upstream `LICENSE` had SHA-256
`2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b`; the
vendored notice differs only by one terminating newline and is hashed separately above.

## Local worker image verification

The final setup Dockerfile was built independently for both target architectures. These are local
content IDs, not published image references:

| Architecture | Runtime image content ID | Packaged tests | ELF audit |
|---|---|---:|---|
| linux/arm64 | `sha256:1916d6243e92537240987b86a63e1b91dfc4242d8fa411e34d8293aa55f58a0b` | 8 passed | 924 objects; no `DT_NEEDED libgomp` |
| linux/amd64 | `sha256:376b9e82aa794a242b91008a998b44394e3355cdc0aaaa0dfac843bce24eb982` | 8 passed under emulation | 927 objects; no `DT_NEEDED libgomp` |

Both images retained the local-inference `MODEL_LICENSE`, the complete Silero MIT terms and
provenance record, omitted an incomplete image-wide SPDX label, and carried
`org.astraldeep.voice.distribution-approved=false`. The malformed-ELF, empty-root, and synthetic
`libgomp.so.1` negative tests also passed.

Representative commands:

```text
docker build --platform linux/arm64 --file Dockerfile.voice --target runtime ...
docker build --platform linux/amd64 --file Dockerfile.voice --target runtime ...
docker run --rm --platform <arch> --entrypoint /opt/voice-test-venv/bin/python <test-image> \
  /opt/voice-audit/verify_no_libgomp.py --root /opt/voice-venv --root /usr
docker run --rm --platform <arch> --entrypoint /bin/sh <test-image> -ec \
  '/opt/voice-test-venv/bin/python -m pytest voice_agent/tests -q -p no:cacheprovider'
```

## Worker vulnerability snapshot

Tool: Trivy `0.72.0`. Database metadata was version 2, updated
`2026-07-31T19:19:43.338098622Z`, with `trivy.db` SHA-256
`7ad4c48ca179b5a67c44ccbeffa3b09c2181244ba2b4f48ea08bd169ab243c9a` and
`metadata.json` SHA-256
`84696791e1de06192c5168c2d6403b75458a3c04ed2249403818ea3715f7543a`.

The command below returned the same result for both runtime architectures: 24 Debian
HIGH/CRITICAL findings, all without a fixed version; zero Python findings; and zero findings for
the previously removed Python-tooling CVEs `CVE-2026-23949` and `CVE-2026-24049`.

```text
trivy image --skip-db-update --scanners vuln --severity HIGH,CRITICAL \
  --format json <local-runtime-image>
```

| Vulnerability | Package | Installed version | Fixed version | Severity | Scanner status |
|---|---|---|---|---|---|
| CVE-2023-45853 | zlib1g | 1:1.2.13.dfsg-1 | none | CRITICAL | will_not_fix |
| CVE-2025-69720 | libncursesw6 | 6.4-4 | none | HIGH | affected |
| CVE-2025-69720 | libtinfo6 | 6.4-4 | none | HIGH | affected |
| CVE-2025-69720 | ncurses-base | 6.4-4 | none | HIGH | affected |
| CVE-2025-69720 | ncurses-bin | 6.4-4 | none | HIGH | affected |
| CVE-2025-7458 | libsqlite3-0 | 3.40.1-2+deb12u2 | none | CRITICAL | affected |
| CVE-2026-13221 | perl-base | 5.36.0-7+deb12u3 | none | CRITICAL | affected |
| CVE-2026-41992 | gzip | 1.12-1 | none | HIGH | fix_deferred |
| CVE-2026-42496 | perl-base | 5.36.0-7+deb12u3 | none | CRITICAL | fix_deferred |
| CVE-2026-42497 | perl-base | 5.36.0-7+deb12u3 | none | HIGH | fix_deferred |
| CVE-2026-48962 | perl-base | 5.36.0-7+deb12u3 | none | HIGH | affected |
| CVE-2026-53615 | bsdutils | 1:2.38.1-5+deb12u3 | none | HIGH | affected |
| CVE-2026-53615 | libblkid1 | 2.38.1-5+deb12u3 | none | HIGH | affected |
| CVE-2026-53615 | libmount1 | 2.38.1-5+deb12u3 | none | HIGH | affected |
| CVE-2026-53615 | libsmartcols1 | 2.38.1-5+deb12u3 | none | HIGH | affected |
| CVE-2026-53615 | libuuid1 | 2.38.1-5+deb12u3 | none | HIGH | affected |
| CVE-2026-53615 | mount | 2.38.1-5+deb12u3 | none | HIGH | affected |
| CVE-2026-53615 | util-linux | 2.38.1-5+deb12u3 | none | HIGH | affected |
| CVE-2026-53615 | util-linux-extra | 2.38.1-5+deb12u3 | none | HIGH | affected |
| CVE-2026-54369 | libacl1 | 2.3.1-3 | none | HIGH | fix_deferred |
| CVE-2026-57432 | perl-base | 5.36.0-7+deb12u3 | none | HIGH | affected |
| CVE-2026-57433 | perl-base | 5.36.0-7+deb12u3 | none | CRITICAL | affected |
| CVE-2026-8376 | perl-base | 5.36.0-7+deb12u3 | none | CRITICAL | affected |
| CVE-2026-9538 | perl-base | 5.36.0-7+deb12u3 | none | HIGH | fix_deferred |

This snapshot is not a waiver. A minimal-runtime replacement or an explicit, reviewed
reachability/remediation decision is still required before T002/T004 can close.

## LiveKit server GO-2026-5932 reachability

The exact server tag `v1.13.5` resolves to commit
`3b9f118327b257301083a7c4aa46076c8012918a`. The pinned OCI index is
`sha256:3497163e15c48fef6e7830c78716f9e9d5edc28abf7aa90b61c86e93bbc306b1`,
with exact amd64 manifest
`sha256:d0d1cfdbe95617647bbe91630454526c2cdd88cec83f41114b3495b444918b9a`
and arm64 manifest
`sha256:804b0d2cfffb5b8f95a9cc5aa47a7b715605d1a527095f45dcc8e94b9cf9920e`.
OCI provenance binds both builds to that commit, `./cmd/server`, and Go 1.26.5.

Extracted production binaries:

| Architecture | Binary SHA-256 |
|---|---|
| linux/arm64 | `6bc048a87d23f08441d3fef14f00a9ecbb4d011c6a4afb053daea4962bf016d3` |
| linux/amd64 | `e288e265268d0f3ebc0e15900d58838e17ec2f3bc82725c3ccdf2f9d9b9ec438` |

`govulncheck v1.6.0`, with vulnerability database updated
`2026-07-27T20:14:16Z`, was run against official source for both `GOARCH` values and against both
extracted binaries. Every scan reported zero symbol vulnerabilities and zero imported-package
vulnerabilities. GO-2026-5932 remained one module-only finding because the build uses other
`golang.org/x/crypto v0.54.0` packages. `go mod why golang.org/x/crypto/openpgp` reported that the
main module does not need the package, `go list -deps ./cmd/server` listed no `openpgp` package, and
`go tool nm` found no `openpgp` symbol in either binary.

Representative exact invocations (repeated with `GOARCH=arm64` and `GOARCH=amd64`):

```text
docker run --rm --platform linux/<arch> -e CGO_ENABLED=0 -e GOOS=linux \
  -e GOARCH=<arch> -v <v1.13.5-source>:/src:ro -w /src golang:1.26-alpine \
  sh -c 'go install golang.org/x/vuln/cmd/govulncheck@v1.6.0 && \
    /go/bin/govulncheck -version && \
    /go/bin/govulncheck -show verbose ./cmd/server'
docker run --rm -v <audit-dir>:/audit:ro golang:1.26-alpine \
  sh -c 'go install golang.org/x/vuln/cmd/govulncheck@v1.6.0 && \
    /go/bin/govulncheck -mode=binary -show verbose /audit/livekit-server-<arch>'
```

Primary evidence links:

- [GO-2026-5932 advisory](https://vuln.go.dev/ID/GO-2026-5932.json)
- [LiveKit v1.13.5 source tag](https://github.com/livekit/livekit/tree/v1.13.5)
- [Exact source commit](https://github.com/livekit/livekit/commit/3b9f118327b257301083a7c4aa46076c8012918a)
- [OCI builder run](https://github.com/livekit/livekit/actions/runs/30611336320/attempts/1)

The server pin does not need replacement for this module-only advisory. This reachability result
must be rerun if the server image, source/build provenance, Go version, or vulnerability database
changes.
