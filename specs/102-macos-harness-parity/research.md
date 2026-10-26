# Research and Decisions

Eight read-only investigations ran on 2026-10-25 against Deep `c86d118f` and `da5a6c66`, Projection `4b7a12b` and `88f68807`, the website at `314a07dd`, and the public sources of the five harnesses and Sparkle 2.10.1. Line references are to those trees.

## Harness autoupdate patterns

| Harness | Trigger and source | Install mode | Notable |
|---|---|---|---|
| Claude Code | startup and periodic; hosted `manifest.json` plus `.sig` per version | automatic background install, "Update installed" footer | channels latest and stable; `DISABLE_AUTOUPDATER`; managed required minimum and maximum versions refuse to start; failed verification keeps the old version |
| Codex | startup; `releases/latest`; 20-hour cache | prompt with Update now, Skip, Skip until next version | daemon restarts only when idle; staged atomic swap; lock file |
| Pi | startup; `pi.dev/api/latest-version` | notify plus `pi update` | server-supplied release note shown in the notice |
| Hermes CLI and Desktop | 24-hour check; Squirrel.Mac on desktop | toast "Update now / Maybe later", 24-hour snooze | pinned SHA-256 and Team ID checks around Squirrel; trust taken from the signed running app |
| SPACE | none | n/a | host enforces a guest version range; checksum pinned in the shipped catalog |

Decision: Sparkle 2.10.1 for delivery and swap, an appcast on the Downloads site whose enclosures point at GitHub Release assets, EdDSA signing, verification before extraction, relaunch postponed while a task runs, 24-hour checks, Install, Later and Skip, a preview channel, and an app-side signed minimum-version gate because Sparkle cannot force an upgrade. Hand-rolling the swap was rejected: Sparkle already handles admin-owned Applications, translocation, atomic swap, rollback and relaunch.

## Distribution lane facts

- Installed app: `AstralDeep.app` 1.8 (69), arm64, Developer ID `593LGH35GS`, notarized, hardened runtime, not sandboxed, entitlements exactly audio input, minimum macOS 14.0. The Deep packager `scripts/package_macos_candidate.py` asserts that exact entitlement set and `codesign --verify --deep --strict`.
- Release `macos-v1.8-69`: immutable prerelease with `AstralDeep-1.8-69-arm64.zip`, `SHA256SUMS` and `release-provenance.json`. `/releases/latest` is unusable because `v*`, `apple-v*` and `macos-v*` tags share the repository.
- Website feed: `https://astraldeep.github.io/data/downloads.json`, closed schema (`scripts/downloads.mjs:10-26`), one current record, cache 600 seconds; `verifyPublishedDownload` checks release immutability, asset digests and `SHA256SUMS` bytes at build time.
- Version identity: `MARKETING_VERSION` and `CURRENT_PROJECT_VERSION` in the pbxproj; `tests/release/test_client_identity_continuity.py:113-154` pins the literal numbers in 21 configurations.
- The client sends no version to the server except `client_version` "1.8.0" inside the computer host descriptor (`MacComputerHost.swift:264-271`); the device user agent is the constant `AstralDeep-macOS/0.1`.
- Sparkle 2.10.1 (2026-10-25): SPM binary target, Embed and Sign, no library-validation entitlement needed with the same Team ID, XPC services inert when not sandboxed, `SUVerifyUpdateBeforeExtraction` requires EdDSA, `SURequireSignedFeed` requires inline release notes, `generate_appcast` needs the archives locally and rewrites every enclosure URL for archives present, so only the newest archive is staged.

## Codex 099 review findings

Live state: production is Deep `b386d556` (image `81268a10`) with `FF_COMPUTER_USE=true`; installed build 69 is Projection `4b7a12b`. Only permissions, consent and chat are proven live. No physical effect has run.

- Every input verb raises its own review card (`orchestrator.py` near 17674, `source_bound.py:28-54`, `hitl_confirmation.py:380-470`).
- Approving on the same Mac pauses the session first, so the plain Approve is refused; Approve and resume exists only for input carrying `hwnd`, which the tool description calls optional (`MacComputerInput.swift:208-223`, `remote_confirmation.py:172-200`, `hitl_confirmation.py:519-525`, `mcp_tools.py:684-686`).
- The approval pause clears the 180-second Terminal grant (`computer_sessions.py:194`).
- Secure Event Input withdraws the whole host until physical re-enable (`MacComputerObservation.swift:17`, `MacComputerHost.swift:340-343,630-633`).
- Takeover triggers on any pointer movement (`MacComputerInput.swift:208-211`); remote Resume overrides a local Pause (`MacComputerHost.swift:576-579`, `computer_sessions.py:202-211`).
- No browser driver: screenshot plus pixel clicks; `open_app` rejects arguments (`computer_use_policy.py:71-79`).
- Commands: Terminal keystrokes, output read from a screenshot; no process host in Swift.
- Nothing sends `cancel_task`; Activity is a blocking sheet of strings; tool descriptions are written for Windows; actions return only `{"attempted": true}`.
- Smaller defects: `Host.current()` on the main thread per request, no punctuation keys, UTF-8 split on truncated reads, overwrite resets mode and drops attributes, the session panel blocks clicks beneath it, Stop shows without a session, dead `wait` verb.

## Governed command execution

The constitution has no clause banning local command execution; the "arbitrary command" block lives in `tool_security.py:64-84`, spec 076 and 099 FR-005. The merged `terminal-shell-1` agent already runs "arbitrary command execution" on isolated SSH runtimes behind `FF_TERMINAL_SHELL` and is cleared by `terminal_host_review.py`. Reusable: host registry and request envelope (`computer_hosts.py`), closed verb sets in `shared/protocol.py`, Swift `ComputerHostProtocol.swift` and Windows `computer_use.py`, policy tiers in `computer_use_policy.py`, sessions and grants in `computer_sessions.py`, source-bound review with `tools:execute` mapped to privileged effect (`source_bound.py:28-34`), delegation from `is_tool_allowed`, `TerminalAuthority` per-effect recheck, the ring buffer in `terminal_sessions.py:55-120`, and the Windows `run_command` design (`win_agent/tools.py:304-396`).

Decision: new verbs on the existing computer-use channel and agent, not a new agent id and not a Mac PTY behind the terminal manager.

## In-app browser pane

Pixel verbs cannot drive the pane: `ComputerHostTargetPolicy.externalProcess` refuses Astral's own process by design. `OfflineChartView.swift` already hosts a locked-down WKWebView with a compiled content rule list, deny-all navigation, generation tokens and a timeout-and-cancel JavaScript bridge. Limits: no cookie sharing with Safari or Chrome, Google OAuth refuses embedded views, no CDP, `isTrusted=false` on synthetic events. Decision: ten verbs in v1, isolated content world, non-persistent store, origin approval, secret-field refusal, consequential handshake on the terminal pattern.

## Theme

Midnight is baked into each client (`Theme.swift:8-20,159`, `astral.css:17-23`, Windows `theme.py:34`, Android `Theme.kt:135`) and the server sends `user_preferences` only when a row is stored (`orchestrator.py:10086-10099`). Preset tables exist in seven places. Mac traps: unknown preset resets to Midnight (`Theme.swift:237-238`); `surface2` is not set by three presets; color scheme is derived from background luminance. Decision: add Graphite everywhere, make it the server and Mac default, and send its colors to users without a stored theme so other clients need no code change.

## Copy

About 255 Swift strings and about 260 server-owned helper strings were classified. The largest single cut is `ComponentView.swift` drawing every form description and field help as a caption. `ConsoleModel.swift:154,195` requires several labels to be non-empty, so the contract must make them optional before the server stops sending them.

## Owner rulings

Commands as Claude Code runs them, under secure delegation. In-app browser pane. Graphite default for all clients, presets kept. Cut copy at source. Owner-created notarization key as `astral-notary`. Constitution may bend where it helps the ecosystem. Mac App Store lane retired for macOS.
