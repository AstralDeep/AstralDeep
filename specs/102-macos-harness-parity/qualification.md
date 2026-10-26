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

## Sandbox rollout (2026-10-26)

| Step | Observation |
|---|---|
| Host inventory | `/opt/AstralDeep` clean at `da5a6c66` with submodules at the pins; root disk at 98 percent with 9.5 GB free; passwordless sudo and docker group available to the operator account |
| Build phase | Docker build cache and unused images reclaimed to 22 GB free; Deep commit `88274b70` and Projection `26651aee` fetched from git bundles (no push); image `astraldeep:sha-88274b70e6e926f22759a6e5e4ca135317fb1067` built on the host, id `sha256:977bcb02…`, 2,688,174,487 bytes |
| Paired backup | `/opt/astraldeep-backups/deploy-88274b70-20261026T133724Z` (root only): PostgreSQL custom dump 3,131,458 bytes (`PGDMP` header, TOC lists through `workspace_snapshot_chat_id_fkey`), durable tar of `backend/data`, `backend/knowledge`, `backend/tmp`, `backend/agents` 10,806,948 bytes, previous override, previous image `sha256:cdd5291a914b…` and previous commit `da5a6c66`, `SHA256SUMS` |
| Cutover | checkout `88274b70` with Projection `26651aee`; override image line rewritten to the new tag with `pull_policy: never` and `FF_COMPUTER_USE=true` preserved; `docker compose up -d --no-deps --force-recreate astraldeep`; readiness 200 within the deadline; container healthy |
| External smoke | `/healthz` 200, `/readyz` 200 with db ok, 15 agents, LETS enforce/healthy; `/api/chats` and `/api/chrome/menu` 401; `/` redirects to sign-in |
| Logs | startup complete; one "ASGI callable returned without completing handshake" line from an unauthenticated socket attempt during startup |
| Rollback | `rollback.sh <backup dir>` restores the previous override, checkout and container; not exercised |

## Live verification (2026-10-26)

| Step | Observation |
|---|---|
| Launch and sign-in | 1.9 (70) restored the Keychain session and connected to the deployed sandbox (`/ws` accepted); the workbench showed the cut copy: no wordmark beside the logo, a bare outlined composer, the canvas header carrying the result title; the stored Ocean preset was honored |
| Crash | opening Updates… trapped in SwiftUI: the sheet was attached outside the window's `.environment(model.themeStore)` chain and `UpdateSettingsSheet` read `ThemeStore` non-optionally (crash report `AstralDeep-2026-10-26-094204.ips`, `EXC_BREAKPOINT` in `EnvironmentValues.subscript.getter`) |
| Fix | the sheet content now receives the theme and tint environment at its presentation site, and the three update views fall back to the Graphite palette when no `ThemeStore` is present; regression test `UpdateViewsEnvironmentTests` hosts them with no environment; Projection `7539629`; 1.9 (70) rebuilt from it |

| First live steps (14:55 to 15:04 local, build 70 second install) | step 1 and the TextEdit request produced one approval card for `start_session`; the approval was consumed and the session started (`computer_session.started` 13:55:33Z), then the server's continuation task raised `skill_lookup_unavailable` (503 from the foreground guidance capture inside a detached task) so no desktop action ran; at 13:56:42Z the host withdrew access (`computer_session.ended consent_revoked`) because its permission refresh on activation found a permission missing for the rebuilt binary while the owner was in System Settings, and macOS had registered two AstralDeep rows in Device Control and Data Access; the host stayed offline until re-enabled locally, so step 3 (`start_session` "no computer called RyzenRoll Mobile") and step 4 (agent fell back to `fetch_page`) could not run; the owner then chose a theme preset (14:04 `chrome_theme_preset`) and found Graphite too dark |
| Follow-ups | continuation guidance binding fix and withdrawn-host offline reason on the server (lane S1); Graphite backgrounds lightened to `#121214`, `#1B1B1F`, `#232328` in every table (Projection `960f9cf`, Deep `3cb8c7ee`); Deep pin `bc4f37b2`; 1.9 (70) rebuilt again from the lighter theme |

| Second cutover (2026-10-26) | image `astraldeep:sha-f1c963541a03484dacfb6e98cb69b95f882695de` (id `sha256:e5bc0626…`) built from Deep `f1c96354` with Projection `960f9cf8` via bundles; paired backup under `/opt/astraldeep-backups/deploy-f1c96354-*`; readiness 200 with db ok, 15 agents, LETS enforce/healthy; `/api/chats` 401; no startup errors |
| Mac install | 1.9 (70) rebuilt from Projection `960f9cf8` (archive sha256 `cce860597514f346…`), notarized and stapled, installed over the previous 70 and relaunched |

## Pending

- Full macOS app suite in one pass after the workbench lane commits.
- Projection pin move in Deep (`components/AstralProjection`, `config/astral-composition.json`).
- Live verification of each story's independent test against the sandbox.
- Publication of 1.9 (70) through the release command on the owner's word.
