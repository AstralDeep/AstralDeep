# Feature 063 — client-interface parity: status + MacBook handoff

Notes for finishing the remote-compute feature's **client interfaces**. Written on
the Windows dev box; everything runnable here is done, Apple is queued for the
MacBook. Last updated: 2026-07-27.

## What the feature adds to the client interfaces

1. **"Remote machines" settings surface** (`webrender/chrome/surfaces/remote_machines.py`)
   — register a machine + credential (SSH key or password), see per-machine
   reachability, Probe / Delete. This is the only surface that needed new code.
2. **Job / confirmation / output cards** — `run_job`, the tracked-job card, the
   destructive-op confirmation card (`remote_op_decision`), and `read_job_output`
   are ordinary astralprims components. They render on **every** client through the
   same generic canvas renderer — **no per-client work** (confirmed for all three).

## The key architectural finding (why this is small)

**Every native client is fully generic** — one component renderer draws any surface;
there is no per-surface code anywhere. So the *only* code the whole feature needed
on the client side is the server-side `components()` for the remote_machines surface.
Adding it makes the surface render on Windows, Android, and Apple with **zero**
Swift/Kotlin/PySide6 changes.

- Server dispatch: `chrome_events._render_surface_sdui` calls a surface's
  `components()` for native device types and pushes a `chrome_surface` frame.
- Menu item propagates via the `FF_REMOTE_COMPUTE` flag fallback
  (`menu_model._resolve_remote_compute(None)`), so native menu channels already
  deliver "Remote machines" when the flag is on — no `remote_enabled` plumbing needed.
- Handler parity: web `data-ui-collect` and native `ParamPicker` action-submit both
  post `payload.fields`, which `_h_machine_add._fields()` already parses.
- **Native has no reactive show/hide**, so `components()` emits BOTH credential
  inputs (private-key textarea + password) always; `_h_machine_add` picks by
  `cred_type` and ignores the other (same pattern as llm.py's always-present base_url).

## DONE + verified on this Windows box

- **Server `components()` (T025)** added to `remote_machines.py` (form with all
  fields incl. `private_key` textarea; per-machine Probe/Delete buttons; same
  `chrome_machine_*` handlers).
- **Backend suite green**: `docker exec astraldeep … pytest -q` → **4618 passed, 0 failed**.
  - New: `backend/tests/chrome/test_surface_remote_machines.py` — registry, web
    render() textarea, native components() fields + submit_action, and the **T020
    multi-line PEM round-trip** through `chrome_machine_add` into the credential store.
- **Windows client** (`windows-client`, PySide6, headless offscreen):
  `.\.venv\Scripts\python.exe -m pytest tests/test_renderer.py tests/test_remote_machines_surface.py`
  → **33 passed**. New `test_remote_machines_surface.py` locks the flagged risk: the
  multi-line PEM survives the `QPlainTextEdit` textarea and the form action-submit
  posts the exact `chrome_machine_add {fields}` payload; Probe/Delete emit with
  `{machine_id}`. **No Windows client code changed** — generic renderer already
  supports every field kind.
- **Android** (`android-client`, gradlew + SDK + JDK-17 present here): ran
  `.\gradlew.bat :core:test :app:testDebugUnitTest` (JVM unit tests, no emulator) →
  **BUILD SUCCESSFUL in 42s**. No Android code changed (generic); this confirms the
  toolchain works on Windows and nothing regressed. Instrumented Compose UI tests
  (actual on-device rendering) need an emulator → CI nightly / MacBook (see TODOs).

## The one thing to VERIFY per client (not a code gap)

The `textarea` field kind (the pasted PEM) is honored by every renderer but was
**never exercised in production** before this (no other shipping surface uses a
textarea). Verified on Windows via the new test. On Android/Apple it's verified by
the live run (below) and, optionally, by an instrumented/UI test (see TODOs).

## MacBook handoff — Apple clients (iOS / macOS; watchOS is chrome-free by design)

**No Swift changes are required** — the Apple renderer (`ComponentView.swift`),
menu (`RootView.swift` → `openSurface`), and ParamPicker action-submit
(`AppModel.submitParamPicker`) already handle everything. This is **build + verify**:

1. `git pull` the `063-remote-compute-agents` branch.
2. Regression: `swift test --package-path apple-clients/AstralCore`.
3. Build all three schemes unsigned (same as `apple-ci.yml`):
   - `xcodebuild -project apple-clients/AstralApp/AstralApp.xcodeproj -scheme AstralApp -destination 'generic/platform=iOS Simulator' -configuration Debug CODE_SIGNING_ALLOWED=NO build`
   - repeat with `-destination 'platform=macOS'`
   - `-scheme AstralWatch -destination 'generic/platform=watchOS Simulator'`
4. **Live acceptance** (the real test, since no Swift changed): set
   `FF_REMOTE_COMPUTE` on in the backend `.env`, `docker compose up -d`, run Debug
   AstralApp (Debug points at `http://localhost:8001`). Sign in (real PKCE — a human
   signs in once; sims have no dev-token path). Settings gear → confirm **Remote
   machines** under Account → open it → confirm the intro text, empty-inventory
   notice, and the **Add a machine** form: Label / Address / Port(number) /
   Username / OS(select) / Role(select) / Credential type(select) / **Private key
   (textarea)** / Passphrase(password) / Password(password) + an **Add & probe**
   button. Add a machine → confirm the row Card (KeyValue facts + Probe/Delete) and
   a probe-verdict notice. Use the `ios-simulator-skill` / `mcp__xcode__*` tools to
   drive/screenshot the simulator; drive the macOS app directly (not a simulator).
5. **Expected outcome:** builds cleanly with **zero** Swift edits and the surface
   renders + round-trips. **If the surface does NOT render, the bug is server-side**
   (`components()` / the `FF_REMOTE_COMPUTE` menu flag), not in the Apple client.

## Optional follow-up tests (emulator / CI / Mac only — not required to function)

- **Android instrumented**: add a `remote_machines` fixture case to
  `app/src/androidTest/.../SurfacesTest.kt` (renders the ParamPicker + textarea +
  Probe/Delete). Runs on an emulator (CI nightly / MacBook), not the Windows loop.
- **Apple**: add a `chrome_surface{surface_key:"remote_machines"}` case to
  `AstralAppTests/AppModelChromeSurfaceTests.swift` (mirrors the existing llm/theme
  cases) asserting the ParamPicker + buttons render and Add posts `chrome_machine_add`.

## Enable / operate

`FF_REMOTE_COMPUTE=1` in `.env` (read once at import → **container recreate**, not
just restart, to toggle). With it off, the agent, menu item, and surface are all
absent (byte-identical to pre-063).
