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
| P10 | **Live capability envelope** re-reported on material change | pass — `capability_update` on resize/permission/connection/reduced-motion | n/a — a desktop window reports at registration; resize re-reporting is a follow-up if the native canvas ever needs re-adaptation | n/a — Compose owns its own reflow; ROTE substitution is content-only on natives (documented in `rote/capabilities.py`) | verify |

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
