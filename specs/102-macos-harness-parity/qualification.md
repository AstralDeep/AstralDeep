# macOS harness parity qualification

## State

Implementation lanes are complete on `claude/102-macos-harness-parity` in Deep and Projection and on `claude/102-mac-appcast` in the website repository. Nothing is deployed, installed or published yet. This file records only what has actually run.

## Local checks

| Check | Observation | Evidence |
|---|---|---|
| Deep `tests` group on the host venv (pinned component wheels) | 15,759 passed, 72 failed, 2,621 skipped, 4 collection errors | 71 of 72 failures and all 4 collection errors reproduce identically on untouched `main` in this host environment (Apple coverage tooling, tokenizer, PDF and DOCX parsing dependencies, BYO authoring, sandbox memory limit, host bash `set -e` behavior); the remaining failure is the native intro fixture comparison that stays red until the Projection pin moves |
| Deep Ruff over `backend` and `scripts` | passed | |
| Projection Python suite on the branch | 4,898 passed, 2 failed, 7 skipped | the interop wheel test fails on untouched `main` too; the provenance ledger entry awaits the workbench lane's final commit |
| Projection theme parity (web, Windows, Android, Apple) | 21 passed | Graphite in every table with the same seven values |
| macOS app unit suite in one pass (`xcodebuild test`, scheme AstralApp, macOS) | 688 passed, 1 failed, with `AppModelChromeSurfaceTests` and the UI tests skipped | the failure is the new Activity accessibility assertion, which like the four pre-existing accessibility-tree cases needs an accessibility-trusted runner; `AppModelChromeSurfaceTests` blocks on the macOS Desktop privacy prompt for a new test host and needs an interactive pass |
| AstralCore package tests | 404 passed | reported by the host lane after its last commit |
| iOS Simulator build | succeeded after the local `AstralSparkle` wrapper package | Sparkle is absent from the iOS product and embedded and signed in the macOS product |
| Deep `tests` group after the Projection pin move | the intro fixture comparison now passes; the Apple dependency graph expectation was updated for Sparkle; one attachments materialization test failed under load and passes alone | commit `88274b70` |
| Release pipeline (`scripts/release_macos.py`, no publish) | 1.9 (70) arm64 archived, Developer ID signed, notarized (Accepted), stapled, Gatekeeper accepted, EdDSA signed, appcast and signed policy staged | archive SHA-256 `cfb568170737ae0a1c9f1f62b0a8d5e9b612a6ce5ec7056b13df4919e2475196`, 22,930,610 bytes; first attempt failed its own identity check on a universal archive, fixed by pinning `ARCHS=arm64`; the `astral-notary` keychain profile had to be re-stored with an explicit `--keychain` path |
| Installed client | `/Applications/AstralDeep.app` is 1.9 (70), `codesign --verify --deep --strict` and `spctl` accepted, Team `593LGH35GS`, feed URL present; build 69 retained at `~/Library/Application Support/AstralDeep-rollback/AstralDeep-1.8-69.zip` | not yet launched against the new server |
| Website | `npm test` 136 passed, `npm run check`, `npm run build:preview` | appcast cross-check added |

## Pending

- Full macOS app suite in one pass after the workbench lane commits.
- Projection pin move in Deep (`components/AstralProjection`, `config/astral-composition.json`).
- Server image build and sandbox rollout with paired backup and rollback.
- Signed, notarized 1.9 (70) build installed over 69.
- Live verification of each story's independent test against the sandbox.
- Publication of 1.9 (70) through the release command on the owner's word.
