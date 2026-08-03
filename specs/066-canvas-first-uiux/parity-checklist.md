# Cross-client parity checklist — canvas-first UX (066)

Web is the reference implementation. Each row: the behavior, how the web
realizes it, and each client's status (`pass`, `divergence` + justification,
or `pending`).

| # | Behavior (web reference) | Web | Windows | Android | Apple (pending Mac) |
|---|---|---|---|---|---|
| P1 | **Canvas leads, conversation trails.** Canvas is the primary surface and takes the leading edge; the conversation rail sits on the trailing edge | pass — split mode: canvas left, rail right (screenshot) | pass — QSplitter reordered to `[canvas, rail]`, sizes 900/380, canvas stretch=1 and non-collapsible (066); **suite green with the change: 701 passed / 7 skipped** | pass — `SplitShell` reordered to canvas-then-rail (066); **248 unit tests green with the change** | verify |
| P2 | **Canvas dominance at rest** ≥70% of width when the conversation is collapsed/closed | pass — collapsed mode = full width, floating composer | pass — rail collapsible to the edge, canvas absorbs it | pass — StackedShell canvas is `weight(1f)` above the Messages panel | verify |
| P3 | **Conversation on demand.** Hidden/collapsed by default at medium widths; reachable in one action; unread indicator when new assistant text arrives while hidden | pass — collapsed drawer + unread badge + peek | divergence (accepted): the desktop rail is persistent-but-collapsible; no unread badge, because a collapsed rail is a deliberate user action on a large screen and the transcript is one drag away | pass — `MessagesPanel` collapsible with count (the original model this feature generalized) | verify |
| P4 | **Composer never crushed** — input ≥20 visible characters at every width | pass — verified 320→2560 | pass — QSplitter min widths + native line edit | pass — InputBar is full-width in both shells | verify |
| P5 | **Voice affordance always present** with honest state/reason, even with no server frame | pass — pre-rendered default control + re-render on teardown | divergence (accepted): the native composer renders the server's voice model directly and shows a disabled mic when unavailable; there is no "empty host" failure mode to defend against (the web bug was CSS `:empty` hiding) | divergence (accepted): same as Windows | verify |
| P6 | **No silent sends.** Sends while disconnected queue visibly or refuse loudly | pass — bounded queue + connection pill | pending — native reconnect/queue behavior not audited in this pass | pending — same | verify |
| P7 | **Failed turn keeps the user message + retry; canvas never blanked** | pass — inline error card with exact-text retry | pending — audit in a later pass | pending — same | verify |
| P8 | **Welcome examples on a new chat**, not only first load | pass — server pushes welcome on `new_chat` before `chat_created` | pass — server-driven, no client code (verified by construction: the frame is a normal `ui_render`) | pass — same server frame | verify |
| P9 | **Calm component chrome** — provenance/actions revealed on intent, not stamped at rest | pass — hover/focus/tap reveal | divergence (accepted): native chrome is already a right-click/long-press context menu — the "always-on row" problem is web-only | divergence (accepted): same (`ArtifactChrome` uses long-press) | verify |
| P11 | **Shared control style.** Every top-bar control except "＋ New", and every composer control except Send, is an ICON with its name in the tooltip/accessible name; the composer is quiet at rest (no permanent voice-state chip) | pass — reference (`.astral-voice-control`, `chrome/topbar.py::_ICON_SVG`, feedback line `hidden` while off-with-no-message) | pass — fixed in this pass; Windows shipped text labels ("💬 Recent chats", "Pulse digest", "⚙ Settings", "Start voice conversation", "Voice: off") and now renders the same icons + a quiet composer | pass — already icon-only (`RootScaffold`, `InputBar`) | verify |
| P10 | **Live capability envelope** re-reported on material change | pass — rides the EXISTING `update_device` ui_event action (no new wire vocabulary) on resize settle / permission / connection / reduced-motion; server diffs the profile and re-adapts the persisted canvas under `FF_LIVE_VIEWPORT` | n/a — a desktop window reports at registration; resize re-reporting is a follow-up if the native canvas ever needs re-adaptation | n/a — Compose owns its own reflow; ROTE substitution is content-only on natives (documented in `rote/capabilities.py`) | verify |

## Live click-through record — Windows (2026-08-03)

Driven interactively against the LOCAL orchestrator (`type=windows` registration
confirmed in the server log), not merely inspected:

| Check | Result |
|---|---|
| P1 canvas leads / rail trails | **PASS** — "Canvas" left, "Conversation" right |
| P2 canvas dominance | **PASS** — canvas ≈85% of a maximized window |
| P4 composer not crushed | **PASS** — full-width input across the window |
| P5 voice affordance | **PASS** — "Start voice conversation" + "Voice: off" (local worker admitted); read "Voice: unavailable" against prod, which is the honest degraded state |
| P8 welcome on new chat | **PASS** — ＋New renders the six examples (server-driven, zero client code) |
| Rich canvas render | **PASS** — dice card + KPI tiles + table; weather dashboard with KPI tiles, bar chart, 7-day table and a second chart |
| Markdown in the rail | **PASS** — renders as real bold natively; Windows was never affected by the web words-only-snapshot defect |
| **Phase / step trail** | **FAIL — open gap.** The status band stays on "Accepted" for the whole turn and does not clear at the end. Three principled fixes were applied (see below) and none changed the observed behavior, so the true owner of that line is still unidentified. |

### Windows status-line changes made during this pass

All three are safe-by-construction — they can only let MORE frames through, or
stop a generic label from overwriting a more specific one — and the suite is
green with them. None is *confirmed* to change the rendered result:

1. `chat_step` scoped by chat id (the 060 fence rejected 100% of step frames,
   since the frame carries no generation) — matches web/Android/Apple.
2. `chat_status` accepted when unscoped. Verified against the server:
   `_send_chat_status` emits `{type, status, message}` with **no** chat_id and
   **no** generations, so the bounded-legacy branch of
   `_scoped_status_matches` refused every frame once a chat had an in-flight
   request. Web accepts unscoped frames for exactly this reason.
3. `_generic_phase_would_clobber` — a chat turn's own phase is not overwritten
   by the generic `operation_status` label while the turn owns the line.

**Next step for whoever picks this up**: instrument `_on_message` to log every
`chat_status` / `chat_step` frame the Windows client actually receives. If the
frames never arrive, the cause is upstream of these three fixes; if they arrive
and the band still reads "Accepted", a fourth writer owns the topbar.

## Live click-through record — Android (2026-08-03)

Driven on a signed-in tablet-width emulator (`type=android` registered at
2560×1600 in the server log):

| Check | Result |
|---|---|
| P1 canvas leads / rail trails | **PASS** — canvas LEFT, CONVERSATION rail RIGHT, confirming the `SplitShell` reorder |
| P8 welcome on new chat | **PASS** — welcome examples render in the canvas |
| P5 voice affordance | **PASS** — "Voice is available." |
| Rich canvas render + rail markdown | **PASS** — a dice turn renders its card in the canvas and real bold in the rail |
| P11 control style | **PASS** — icon-only top bar; composer is mic · input · paperclip · send |

## Windows style-parity pass (2026-08-03, P11)

The Windows bar and composer were the last surfaces reading as a different
application beside web and Android. Fixed and re-verified live (icon-only bar
rendered, `roll 2 dice` turn round-tripped to a canvas card + bold rail text):

| Divergence | Before | After |
|---|---|---|
| Top-bar controls | text buttons — "💬 Recent chats", "Pulse digest", "⚙ Settings ⌄" | 💬 ✨ 🕓 ⚙ icons, name in tooltip + accessible name; order and model unchanged |
| Pulse digest icon | **unmapped** — `_ACTION_ICONS` keyed on `pulse`/`activity`/`clock`, names the server never sends, so `sparkle` fell through to the label | server icon names (`sparkle`/`history`/`gear`) mapped directly; a test now pins that every model icon resolves |
| Voice controls | server label rendered as button TEXT ("Start voice conversation") | glyph per the server's `icon`, mirroring web's `VOICE_ICONS`; an unmapped icon still falls back to text rather than a blank button |
| Voice state | permanent "Voice: off" chip in the composer row | hidden when off with nothing to report, exactly like web's `hidden = state === "off" && !message`; accessible description always set |
| Paperclip / icon buttons | full-size text buttons with a menu chevron | 38px square ghost buttons matching `.astral-voice-control`, chevron suppressed |

"＋ New" keeps its word on every client (it is the primary action), and Send
stays a filled text button because web's is too.

**Release-hygiene catch during this pass**: the tracked production
`windows-client/deployment/release-profile.json` + `runtime-manifest.json` had
been overwritten with a LOCAL dev profile (`astraldeep-local-devbox`,
`ws://127.0.0.1:8001/ws`, `development_defaults_allowed: true`) to launch the
client against the local orchestrator. Reverted to the production identity
(`astraldeep-production-uky-0.4.0`, profile digest `771bd01c…` per
`specs/060-runtime-reliability-hardening/verification/us4-windows-release.md`)
before any commit. Local dev launches must pass a profile on the command line
instead of editing the tracked one.

## Status summary (2026-08-03)

Both native changes were made and then re-verified by re-running each client's
suite with the change in place — not merely edited.


- **Web**: 8 of 8 applicable rows pass.
- **Windows**: P1 fixed in this feature; P3/P5/P9 are accepted divergences with
  reasons above; P6/P7 not audited (flagged, not claimed).
- **Android**: P1 fixed in this feature; P3 was already the reference model;
  P5/P9 accepted divergences; P6/P7 not audited.
- **Apple**: every row `verify` — the Mac pass uses this table plus
  [apple-handoff.md](apple-handoff.md).

## How to re-verify

Web: follow [quickstart.md](quickstart.md) §1–6.
Windows: `QT_QPA_PLATFORM=offscreen python -m pytest windows-client/tests -q`
then launch the client and confirm P1/P2 visually.
Android: `./gradlew :app:testDebugUnitTest` then run the app on a tablet-width
emulator and confirm P1/P2.
