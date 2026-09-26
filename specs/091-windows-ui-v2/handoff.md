# Windows UI v2 stopped-work handoff — 2026-09-26

The owner explicitly stopped implementation and requested immediate publication for another agent tomorrow. **Incomplete, unqualified work in progress. Do not claim visual parity or merge readiness.** Continue `codex/091-windows-ui-v2` in Deep and Projection; PR20 and PR200 now target main. No new feature number is needed.

## Published source

Projection `cdec4a63a8fba1cb0279dccca9384377ee09d5f8` contains the current Windows changes. Before publication, both repositories fast-forwarded the remote Windows branches, preserving the other machine's merged 090 work (Projection e0b09a8 / Deep ad0f9fd5). Windows-authored edits do not touch Apple, Android or 090 artifacts. Deep pins cdec4a6; LETS, Plane and Primitives retain their exact existing pins and schema/contract digests.

The active viewport consumer is now implemented in `viewport.py`, integrated through `protocol.py`, `app.py` and semantic renderer tags. It negotiates the published contract, pairs scoped acknowledgments with exact snapshots, defers busy work, retires stale requests and restores controls by semantic identity. The old ignored recovery prototype is superseded; use active source. Its focused tests pass, but full combined-source and authenticated live resize acceptance remain open.

Visual work changes sidebar/logo/cards/account controls, landing categories and Run/Load controls, composer sizing, conversation bubbles, result preview/full-screen structure, primitive rendering and native Settings. `surface_widgets.py` adapts recognized server-owned agent actions and falls back for unrecognized structures. These changes are unfinished, not accepted parity.

## Evidence and immediate gaps

- Viewport cohort: 54 passed; viewport.py 366/373 executable lines (98%). Settings/dialog cohort: 32 passed; surface_widgets.py 97%. Earlier primitive cohort: 102 passed before its final FlowLayout adjustment. These are narrow source-specific results, not a pass for the final combined commit.
- The current console/composer run was interrupted at the owner's stop request with failures present. No aggregate pass or final changed-line coverage exists. The last full Windows/supervision run belongs to older source only. Reproduce current failures before broader qualification; do not relax assertions/deadlines.
- Inspected offscreen landing captures cover the seven required logical sizes; desktop sidebar/content/first-card geometry improved. Settings captures cover desktop and phone. They are diagnostics from an evolving working tree, not authenticated screenshots or final-commit evidence. Side-by-side overlays/diffs and the complete state/theme matrix are unfinished. Archived diagnostic images/metadata are in kos-wiki `assets/astral-native-ui-v2/windows/work-in-progress-2026-09-26/`.
- Typography still needs measured Qt hinting/line-height work. Bundled TTF and web WOFF2 outline/metric/axis tables match; web title tracking was added. Scenario category weight and wrapped description baselines need review. Result toolbar/full-screen changes need final capture and regression review. Settings still has extra narrow-name wrapping and lacks background blur.
- Only the user performed native and browser Keycloak sign-in. Native Computer Use repeatedly failed with `foreground window did not report a process id`, including after fresh selection/recovery and the user's unlocked-desktop confirmation. Do not bypass the skill or authentication. Current native session was launched at 1280×800 client / 1280×831 frame, DPR1,96DPI on1470×923 screen (1470×875 available). Its loaded source precedes final edits; later captures/physical scaling are unverified.
- The matching live web client signed in at http://localhost:8001;127.0.0.1 is not an allowed frontend callback. A real six-dice Run was submitted, but final result/full-screen inspection was not completed before stop. No current native attachments/Advanced selection/voice/microphone/playback acceptance is claimed.
- Packaged profile isolation remains unimplemented: three packaged GUI tests, two release-evidence paths and the candidate workflow delete HKCU\\Software\\AstralDeep\\WindowsClient. Do not run those against the user's active profile. No new settings factory was written. The executable was not rebuilt for this source.

## Local runtime and recoverable files

Only local `astraldeep` was replaced with diagnostic image `astraldeep:windows091-live-3aa18e0a`, SHA256 `ab800558d0ed4433a283f5feffc352e98dc9f938994970be328d658696cc9100`, based on Deep3aa18e0a / Projectione0419dd. It contains the shared hydration contract and is healthy. Configuration/mount/port/network digests match; user PostgreSQL, LiveKit and worker container IDs are unchanged. This is not a clean final-source image or production deployment.

Narrow backend evidence:140 unique passes,5 current admission passes,310 ROTE/chrome passes,18 Chromium passes. Initial setup failures/skips remain recorded. All test containers/private network/anonymous test volume were removed. Retained: active derivative image, ordinary BuildKit cache, ignored `build/windows-091/runtime-20260926/live.compose.yml`, and WSL `/home/sam/astral091-live-l4p07kjs/context`.

Ignored `build/windows-091/` retains logs, coverage and diagnostic scripts. Copies of the three diagnostic scripts and concise runtime evidence are under `diagnostics/`; restore scripts to their original build/windows-091 location before using their relative paths. `font_probe.py` was prepared but not run. No merge, release, signing, installer publication or production deployment was performed or authorized.
