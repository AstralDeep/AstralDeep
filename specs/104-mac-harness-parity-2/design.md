# Design: macOS Harness Parity 2 — semantic control, agent overlay, browser parity, artifacts

**Date**: 2026-11-01. **Scope**: macOS DirectDownload client + sandbox server only. CI and other clients are out of scope; other clients must keep working unchanged through capability gating.

**Owner decisions (2026-11-01)**: all four gaps are in scope (semantic app control, agent cursor overlay, browser pane parity, fix today's flow first); the deployed product uses only `zai-org/GLM-5.3-Flash` from LLM Factory (no Anthropic/OpenAI models in the product; the Codex CLI goal handoff stays off); artifacts are native composition plus sandboxed HTML; the unattended test runs with persisted bypass and `private/` hidden from the agent. Fable orchestrates this work with Opus/Sonnet subagents.

## Intent

Make the Mac client behave like the Claude desktop app's computer use and built-in browser and the Codex app's computer use, and make each response read as one designed artifact, like Claude's artifacts. Neither vendor's implementation is open source (verified 2026-11-01: Codex's computer use lives in a closed helper app `com.openai.sky.CUAService`; Claude's browser/artifact panes live in the closed Electron app). We reproduce their tool contracts and visible behavior, which this session knows first-hand, and reuse the open pieces: Codex's sandboxed HTML viewer design (Apache-2.0), Anthropic's computer-use best practices (MIT), the artifact skills' design rules.

Test of success: from the installed Mac app, signed in to `sandbox.ai.uky.edu`, the prompt *"View the project at /Users/sam/Desktop/Work/Pi-Agent/ and set up a self improving loop based on the feedback on PRs that you received over the 80 issues you worked on in the AstralDeep repo"* runs unattended to a useful result, using semantic control, the browser pane and commands without stalls or false denials, and the response renders as an artifact.

## What exists (2.0.12 build 85, Deep `401c8233`)

- Host verbs (closed protocol v1): `screenshot list_windows get_clipboard read_file list_dir wait click double_click right_click move drag scroll type_text press_keys focus_window open_app set_clipboard write_file delete_path run_command read_output interrupt_command browser_snapshot browser_get_text browser_screenshot browser_wait browser_navigate browser_click browser_fill browser_press browser_scroll browser_dialog`.
- Swift: `apple-clients/AstralCore/Sources/AstralCore/Computer/ComputerHostProtocol.swift` (verb set, arg whitelist, deadline ceilings), `AstralApp/AstralApp/Computer/*` (host, driver, input, observation, commands, files, browser pane/driver), `Resources/browser_snapshot.js` (refs `e12`, snapshot ids `s3`).
- Python: `backend/shared/protocol.py` (`COMPUTER_HOST_VERBS`, descriptor roots), `backend/orchestrator/computer_use_policy.py` (tiers), `backend/agents/computer_use/mcp_tools.py` (`TOOL_REGISTRY`), `browser_tools.py`, `computer_sessions.py` (in-memory bypass), `ui_designer.py` (layout tree), `ComponentView.swift` (36 native types), `contracts/ui_protocol.json`.
- Pointer actions already return a fresh screenshot. Bypass is per host connection, in memory, not persisted. No cursor overlay; only an `NSPanel` with Pause/Resume/Stop. AX is used only for window geometry/raise.

## Part A — Semantic app control (new host verbs)

Model: Claude's `app_ax_find` / `app_menu` / `app_click(ref)` and Codex's `get_app_state` (tree + indices). Refs are `a<N>` (distinct from browser `e<N>`), scoped to an AX snapshot id `x<N>`; a ref from an older snapshot that no longer resolves returns `stale_ref`, never a misdirected action.

| Verb | Args (whitelist) | Result |
|---|---|---|
| `list_apps` | — | `{apps:[{pid,name,bundle_id,active,hidden,window_count}]}` |
| `ax_tree` | `hwnd?`, `pid?`, `depth` 1..8 (6), `max_nodes` 1..800 (300), `mode` interactive\|all | `{snapshot_id, app, window:{hwnd,title}, lines:[...], truncated}`; line = `[a12] role "title" value="…" (x,y,w,h) states…` in screen coordinates matching `screenshot` |
| `ax_find` | `query` ≤200, `role?` ≤40, `hwnd?`, `pid?`, `max` 1..50 (20) | `{snapshot_id, matches:[{ref,role,title,value,frame:{x,y,w,h},enabled,focused,actions:[...]}]}` case-insensitive substring over role/title/value/description/identifier |
| `ax_action` | `ref` (a-ref), `snapshot_id`, `action` press\|focus\|raise\|show_menu\|confirm\|cancel\|increment\|decrement\|set_value\|pick, `value?` ≤4000 | performs the AX action; `pick` presses then selects a menu item by `value`; secure fields refuse `set_value`; returns fresh screenshot |
| `menu` | `pid?`, `path` 1..6 items ≤100 each, `list?` bool | invokes a menu-bar item through AX (`AXMenuBar → AXMenu…`), or lists children when `list` or the path is incomplete: `{items:[{title,enabled,checked,shortcut,has_children}]}` |
| `zoom` | `x,y,w,h` (screen coords), `scale` 1..4 (2), `hwnd?` | region screenshot like `screenshot` (`image`, `width`, `height`, `origin`) |
| `click`/`double_click`/`right_click`/`move`/`drag` | existing args **or** `ref` + `snapshot_id` (resolved to the element's frame center; `drag` takes `ref` as start and `x2,y2` or `ref2`) | unchanged |

Policy tiers: `list_apps ax_tree ax_find zoom` observe; `ax_action menu` input (an `ax_action` whose element's title/role means buy, pay, send, delete, or whose app is a terminal, is consequential through the existing destructive classifier); ref-addressed clicks keep the click tier. Deadline ceilings: `ax_tree 8000`, others default.

Host rules: the driver cannot operate Astral's own process (existing rule holds for refs). AX refs are cached per snapshot id in the driver (`[snapshotId: [ref: AXUIElement]]`, last 4 snapshots), invalidated on app termination; element identity re-checked (`kAXRole`, frame non-empty, on-screen) before acting. Everything goes through the existing input queue, takeover detection and task grants.

## Part B — Agent cursor overlay

A transparent, click-through, non-activating overlay (`NSPanel`, `.screenSaver` level, `ignoresMouseEvents`, `collectionBehavior` can-join-all-spaces + stationary, one per screen) that shows:
- an agent pointer glyph that glides to the target over ~150 ms before a click, a click ripple/halo at the point, a drag path, a scroll indicator, a key chord toast (`⌘S`), and a label naming the target (`Save — TextEdit`) for 1.2 s;
- a top banner "Astral is controlling TextEdit" with Pause/Resume/Stop (the existing session `NSPanel` controls move into this banner; keyboard: Esc pauses when the banner is key-less, so Stop is a click);
- states: driving (purple accent), paused by you (gray), awaiting approval (amber).

The overlay windows are excluded from `screenshot`/`zoom` captures (`SCContentFilter(display:excludingWindows:)` already takes a list) so the model never sees its own halo. The overlay never receives input and never becomes key. Tests: a presenter protocol records show/hide/animate calls; capture plan tests assert overlay window ids are excluded.

## Part C — Browser pane parity

Model: Claude's built-in browser tools (`navigate`, `find`, `read_page`, `get_page_text`, `form_input`, `computer` screenshot/click/type/scroll/zoom, `read_console_messages`, `read_network_requests`, `javascript_tool`, `resize_window`, tabs, `browser_batch`).

New/extended host verbs:

| Verb | Args | Notes |
|---|---|---|
| `browser_find` | `query` ≤200, `max` 1..20 | returns `{snapshot_id, matches:[{ref,line}]}` from a fresh interactive snapshot (same ref scheme) |
| `browser_snapshot` | + `ref` (subtree), `depth` 1..30 | `mode: all` is the read_page equivalent |
| `browser_hover` | `ref`, `snapshot_id` | dispatches mouseover/mousemove on the element |
| `browser_zoom` | `x0,y0,x1,y1` (viewport px), `scale` 1..4 | region screenshot |
| `browser_console` | `level` all\|error, `limit` 1..200, `pattern?` ≤200 | console hook injected at document start (page world) posting to a handler; plus navigation errors; ring of 500 |
| `browser_network` | `limit` 1..100, `url_pattern?` ≤200 | main-frame navigations (status from the navigation response) + resource timing entries (name, type, duration, transfer size); ring of 500; auth-like query values redacted |
| `browser_evaluate` | `script` ≤8000 | runs in the page world, JSON result ≤50 KB; **consequential** tier (card unless bypass) |
| `browser_resize` | `preset` mobile\|tablet\|desktop or `width` 320..3840,`height` 320..2160, `color_scheme?` light\|dark | emulates viewport size inside the pane (letterboxed) and `prefers-color-scheme` via `overrideUserInterfaceStyle` |
| `browser_tabs` | `action` list\|create\|close\|select, `tab_id?` ≤32, `url?` | up to 8 tabs; each tab is its own `WKWebView` sharing the non-persistent store; the pane shows a tab strip; `create` with `url` applies origin approval as `browser_navigate` does |

`browser_batch` is a server-side composite tool (sequential, ≤20 steps, stops at first error, each step still policy-gated); no host verb. `browser_navigate` keeps `action: back|forward|reload`. Pane UI gains: tab strip, viewport preset menu, a console drawer toggle, and keeps the "Astral is driving" chip and Take over. Deadline ceilings 5000 ms (console/network/find), 15000 (`browser_evaluate`), 15000 (`browser_tabs` create).

## Part D — Artifacts (native composition + sandboxed HTML)

Two new component types, emitted by the server as plain dicts (astralprims classes follow later), listed in `contracts/ui_protocol.json` with dispositions, and **capability-gated**: a client that advertises `artifact_v1` in `registerUI` capabilities receives them; any other client receives the flattened children (`artifact`) or a `download_card` of the HTML (`html_artifact`). The Mac advertises `artifact_v1`.

`artifact` (native composition):
```json
{"type":"artifact","id":"art_…","title":"Loop Design","kind":"report|dashboard|document|app|analysis",
 "subtitle":"optional ≤200","summary":"optional ≤400","version":1,
 "sections":[{"id":"s1","heading":"≤80","layout":"stack|grid-2|grid-3","children":[<components or {"type":"ref","id":…}>]}],
 "actions":[{"label":"≤40","action":"copy|export|share|open"}]}
```
Bounds enforced by code: ≤12 sections, depth ≤3, ≤60 descendants, title ≤120. Produced in `ui_designer.design_round`: the designer prompt now returns `{"title","kind","sections":[…]}` over the same ref tree; a deterministic fallback (title from hero/heading/first text, one section `stack`) applies when the LLM output fails validation or the designer is off. Env flag `FF_ARTIFACT_FRAME` (default on in sandbox) wraps rounds of ≥2 components; single-component rounds stay bare.

`html_artifact` (Claude-style page):
```json
{"type":"html_artifact","id":"art_…","title":"≤120","kind":"page|app|visualization","version":1,
 "html":"<!doctype html>… ≤2 MiB","created_at":"RFC3339"}
```
Orchestrator tools `publish_artifact {title, html, kind?}` and `update_artifact {artifact_id, html}` (version+1) are available to the model; the system prompt carries the design rules (title 2–4 words; colors as tokens on `:root` with dark mode under `prefers-color-scheme` guarded by `:root:not([data-theme="light"])` and `:root[data-theme="dark"]`; explicit `body` background; 16 px gutters, no horizontal scroll; scripts only from cdnjs/jsdelivr/unpkg/esm.sh pinned to exact versions; fonts only from Google Fonts; everything else inline). Code validates: size cap; strips `<base>`, `<meta http-equiv=refresh>`, `<object>`, `<embed>`, nested `<iframe>`, `<form>` with external `action`; every `src`/`href` on script/link/img/font must be inline, `data:`/`blob:`, or on the allowlist; injects a CSP meta (`default-src 'none'; script-src 'unsafe-inline' <allowlist>; style-src 'unsafe-inline' <allowlist>; img-src data: blob: <allowlist>; font-src data: <allowlist>; connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'`). Rejections return a precise error to the model.

Mac rendering: `ArtifactView` (header: title, kind badge, version chip, actions; sections with headings; `grid-2/3` via adaptive grid) and `HtmlArtifactView` (a `WKWebView` with non-persistent store, JavaScript on, `javaScriptCanOpenWindowsAutomatically` off, a content rule list allowing only the allowlist hosts, a navigation delegate that denies every navigation after the initial load, the HTML loaded as `srcdoc` of an `<iframe sandbox="allow-scripts">` inside a host page, like Codex's viewer). Canvas: the newest artifact fills the canvas; earlier artifacts stay reachable through the existing history pill; Open in Browser, Copy and Export (HTML file) actions on `html_artifact`.

Primitive polish (ComponentView.swift, Graphite theme): a 4-step type scale, consistent 12/16/24 spacing, card surfaces with 1 px borders and 10 pt radius, tables with sticky header and zebra rows, code blocks with copy and language label, metric tiles with trend arrows, hero with eyebrow/title/summary, lists with leading glyphs. No new copy strings.

## Part E — Unattended posture

- **Bypass persistence**: when the owner confirms bypass on this Mac, the client stores `computer.bypass.<account-server hash>`. On a fresh authenticated launch or reconnect, after computer access has rearmed and `bypassAllowed()` holds, the client re-enables bypass and sends the frame (so a server restart does not lose it). Turning bypass off clears the key. The footer warning stays. Stop/Esc still do not turn bypass off (owner decision 2026-10-27).
- **Workspace exclusions**: each selected root carries `excluded` patterns, default `private/`, `.env*`, `*.pem`, `*.key`, `*.p8`, `*.p12`, `credentials*.json`, `realm-export.json`, `id_rsa*`, `id_ed25519*`, `*.keychain*`. `read_file`/`list_dir`/`write_file`/`delete_path` refuse with `excluded_path`; `list_dir` omits excluded names; `run_command` refuses a `cwd` inside an excluded directory and an argv/command text that names an excluded path; the root descriptor reports `excluded` so the agent is told (`protocol.py` accepts the optional key; the tool descriptions mention it). Computer controls show the list under each folder.
- **Product model**: sandbox `MODEL_TIERS` → every tier `zai-org/GLM-5.3-Flash`; goal harness stays off (no Codex handoff); the owner's in-app LLM config is verified to point at LLM Factory with that model.

## Part F — Reliability pass

Before merging the new lanes: run the current 2.0.12 flow live (TextEdit task, `run_command` in a workspace folder, browser pane on a fixture page, Esc cancel) and record stalls/false denials. After integration: rerun the same plus the Pi-Agent prompt; fix what breaks; redeploy. Known trap list from the vault: approval-click takeover, AX focus miss during rearm, cancel during approval continuation, App Nap socket stalls, HITL "dispatch_completed" refusals.

## Non-goals

Windows/Android/web parity; downloads/uploads/popups in the pane; per-app persistent allowlists (bypass and task grants cover it); astralprims package changes (follow-up); App Store lane; CI green.

## Rollout

1. Scaffold the verb contract in both repos (one commit each) so lanes don't collide.
2. Lanes (worktrees): Projection `mac-ax-overlay`, `mac-browser-parity`, `mac-artifacts`, `mac-bypass-exclusions`; Deep `backend-host-tools`, `artifact-server`.
3. Merge to local mains, focused tests, Debug build, DirectDownload signed build, install (previous app kept as a hidden backup in /Applications).
4. Sandbox: prune the rehearsal containers and dangling images (disk at 97%), paired backup, build `astraldeep:sha-<rev>`, cutover, health; set `MODEL_TIERS`.
5. Live test; iterate; publish `2.0.13` with `scripts/release_macos.py`; update kos-wiki and memory.
