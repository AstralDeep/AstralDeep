# Contract: browser pane verbs

Ten verbs on the computer-use-1 host channel, advertised only when the Mac host is enabled and the browser pane is turned on in Computer controls. Verbs run inside the existing `computer_request` and `computer_response` envelope. No new frame type.

## Verbs

| Verb | Request | Class | Review |
|---|---|---|---|
| `browser_snapshot` | `{"scope_ref"?: "e12", "mode": "interactive" \| "all", "max_chars": 60000}` | observe | none |
| `browser_get_text` | `{"ref"?: "e12", "max_chars": 60000}` | observe | none |
| `browser_screenshot` | `{"max_width": 1280, "ref"?: "e12"}` | observe | none |
| `browser_wait` | `{"for": "load" \| "text" \| "ref", "text"?: "...", "ref"?: "e12", "seconds": 10}` | observe | none |
| `browser_navigate` | `{"url": "https://..."} \| {"action": "back" \| "forward" \| "reload"}` | input | first visit to an origin per session |
| `browser_click` | `{"ref": "e12", "button": "left" \| "right", "count": 1, "snapshot_id": "s9"}` | input | consequential target |
| `browser_fill` | `{"fields": [{"ref": "e12", "value": "..."}], "snapshot_id": "s9"}` | input | consequential target |
| `browser_press` | `{"keys": "cmd+a" \| "Enter", "ref"?: "e12"}` | input | none |
| `browser_scroll` | `{"ref"?: "e12", "dy": 600}` | input | none |
| `browser_dialog` | `{"action": "accept" \| "dismiss", "text"?: "..."}` | input | none |

Deferred to a later version: `browser_find`, `browser_console`, `browser_network`, `browser_tabs`, `browser_upload`, `browser_download`.

## Snapshot shape

```json
{
  "snapshot_id": "s9", "url": "https://example.org/form", "title": "Form",
  "frames": [{"id": "f0", "url": "..."}],
  "lines": ["[e1] heading \"Order\"", "[e2] textbox \"Name\" value=\"\"", "[e3] button \"Submit\""],
  "truncated": false, "pending_dialog": null
}
```

- References are `e<n>` and are stable for the life of a snapshot. Actions carry `snapshot_id`; a reference that no longer resolves, or a snapshot older than the current document, returns `stale_ref`.
- Values of password, card and one-time-code fields appear as `value=<redacted>`.
- Lines are bounded by `max_chars`; interactive mode lists only controls, headings, links and landmarks.

## Host behavior

- One `WKWebView` in the pane with a `.nonPersistent()` data store, `javaScriptCanOpenWindowsAutomatically = false`, `createWebViewWith` returning nil, and a compiled content rule list allowing only http and https.
- The snapshot and action script is injected as a `WKUserScript` at document start in every frame and runs in a named `WKContentWorld`; replies travel through `WKScriptMessageHandlerWithReply`.
- Navigation policy: allow http and https only; refuse `file`, `javascript`, `data` and custom schemes; refuse downloads and uploads; refuse origins not approved in this session.
- Dialogs: `WKUIDelegate` queues alerts, confirms and prompts; a queued dialog is reported in `pending_dialog` and answered by `browser_dialog`.
- Consequential classification at action time: a submit control inside a form containing password, card or one-time-code fields; a control whose accessible name or text matches buy, pay, purchase, order, checkout, delete, remove, send, transfer, confirm payment; a control with `autocomplete` starting with `cc-`. Such actions return `confirmation_required` with the element line; the orchestrator raises the source-bound card and, once approved, issues a one-shot permit bound to the reference, origin and snapshot id.
- Secret fields are refused outright for `browser_fill` and `browser_get_text` with `secret_field`.
- Synthetic events: the script dispatches focus, input, change and click; when a control ignores untrusted events, the host falls back to an `NSEvent` click at the element's center inside the pane. Fallback events are marked so they do not trigger takeover.
- User input in the pane pauses the session as a takeover. The pane shows an "Astral is driving" chip and a Take over control.
- Screenshots use `takeSnapshot` of the viewport, bounded to `max_width`, JPEG, at most 2.9 MB.
- Timeouts: 5 seconds for observations, 15 seconds for navigation and wait.

## Server behavior

- `shared/protocol.py` adds the verbs to `COMPUTER_HOST_VERBS`; `computer_use_policy.py` classifies them; `computer_sessions.py` keeps `approved_origins` per session; `summary_for` renders the origin card and the consequential card.
- Tool results: lines and text are wrapped as untrusted content through the existing datamarking; browser results never carry a model digest. Screenshots attach with the caption "page content".
- `taint.py` adds `browser_click`, `browser_fill`, `browser_press` and `run_command` as sinks so tainted text cannot flow into them without review.
- Tool descriptions say: use `browser_*` for the pane; pixel verbs cannot operate Astral's own windows; sites that refuse embedded views need the user's own browser through the desktop path.

## Limits stated to the user

No shared sign-in with Safari or Chrome. Google and many single sign-on providers refuse embedded views. No console or network inspection in this version. Downloads, uploads and popups are blocked in this version.

## Rulings from the builder check (2026-10-25)

- Origin approval is a grant: approving `browser_navigate` to an origin creates a grant with scope `{"kind": "origin", "origin": "https://example.org"}` covering `browser_navigate` within that origin and `browser_click`, `browser_fill`, `browser_press`, `browser_scroll` and `browser_dialog` on pages of that origin. Observation verbs need no review. Consequential targets still return `confirmation_required` and take exact review. The grant follows the same expiry and revocation rules as other grants.
- Deadline ceilings: `browser_navigate` and `browser_wait` 15 s, other browser verbs 5 s. Browser verbs run on the host's non-physical executor.
- The pane is one of Astral's own windows for takeover purposes with the stated exception: typing or clicking inside the page content pauses the agent's browser work; using the pane's own URL bar, Take over and navigation buttons does not.

## Consequential handshake fields (landed by Lane S2, 2026-10-25)

- `browser_click` and `browser_fill` accept an optional `confirm_element` string: the element line the host reported in a `confirmation_required` result. The model repeats the call with it; the server then treats the call as destructive, so no grant covers it and the exact source-bound review runs.
- After approval the server adds `permit: {"ref" or "refs", "origin", "snapshot_id"}` to the host arguments. The host proceeds only when the permit's ref, origin and snapshot id match the live target; otherwise it returns `confirmation_required` again. A permit is single use.
- A `confirm_element` the host never reported is refused by the server with `confirm_element_mismatch` before any request reaches the host.
- URL canonicalization: `canonical_browser_url` in `computer_use_policy.py` (ASCII only; no whitespace, backslash, control or bidi characters; http or https; no userinfo; lowercase scheme and host; default ports removed; host labels limited to `[a-z0-9._-]`; IPv6 literals kept in brackets; at most 2048 characters). Rejected URLs return `url_rejected` before any card. The host applies the same rule independently.
- Host refusals (`stale_ref`, `secret_field`, `confirmation_required`, `origin_not_approved`, `pending_dialog`, `unsupported`) reach the model as ordinary results with `status: "refused"` and a `next_action`, because MCP error text is replaced by a generic message.
