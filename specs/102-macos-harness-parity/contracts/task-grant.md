# Contract: task grants and same-host approval

Additive to the computer-use-1 approval flow. No new frame type. Changes land in Deep (`remote_confirmation.py`, `hitl_confirmation.py`, `computer_sessions.py`, `agents/computer_use/mcp_tools.py`) and in the Mac host (`MacComputerInput.swift`, `MacComputerHost.swift`).

## Grant

A grant is created only from an approval card decision. It never exists before a human chose it.

```json
{
  "grant_id": "uuid",
  "owner_id": "...", "host_id": "...", "session_id": "...", "chat_id": "...", "task_id": "...",
  "scope": {"kind": "app", "bundle_id": "com.apple.TextEdit"} | {"kind": "window", "hwnd": 1234, "bundle_id": "..."},
  "verb_classes": ["input"] | ["input", "command"],
  "command": {"argv0": "wc", "root": "/Users/sam/Projects/x"},
  "created_at": "RFC3339", "last_used_at": "RFC3339",
  "sliding_ttl_s": 900, "hard_cap_s": 7200
}
```

Rules

- Covered verbs: `click`, `double_click`, `right_click`, `move`, `drag`, `scroll`, `type_text`, `press_keys`, `focus_window`, `open_app` for the scoped application; `run_command`, `read_output`, `interrupt_command` when `command` is set and the proposed program and working directory match.
- Never covered: `type_text` or `press_keys` into a terminal-capable application (existing terminal admission rules apply), `write_file`, `delete_path`, `set_clipboard` with secret-like content, `browser_navigate` to an unapproved origin, consequential browser actions, and any command matching the destructive list in `local-exec.md`.
- Revocation: pause for any reason, Stop, takeover, socket loss, host replacement, owner or server change, task end, chat change, foreground application different from the scope at effect time, or `hwnd` no longer valid. Revocation is logged as `computer_grant.revoked` with the reason.
- Expiry: sliding 15 minutes from last use, hard cap 2 hours from creation. Expired grants are removed, not refreshed.
- One grant per (session, scope). A second approval for the same scope replaces the first.
- Audit: `computer_grant.created`, `computer_grant.used` (per effect, with verb and target summary), `computer_grant.revoked`, `computer_grant.expired`.

## Card

Input-action review cards gain a third decision alongside Approve and Deny:

`Allow for this task in <Application Name>` (or `in window "<title>"` when the action carries `hwnd`).

For `run_command` the label is `Allow <argv0> in <folder name> for this task`.

Pointer-action cards attach a cropped screenshot (at most 512 by 512 pixels, JPEG) centered on the target with a marker, plus the window title and application name. The crop is produced by the host at proposal time from the latest observation and is tagged untrusted.

## Same-host approval tolerance

When `session.controller_host_id == session.host_id`, the host enters an approval window for 10 seconds after a card is delivered to it. During that window, pointer movement and at most two clicks inside Astral's own windows are classified `approval_activity` and do not pause the session. Keyboard input outside Astral's windows, clicks outside them, or a third click end the window and pause as before. The server records `computer_session.approval_activity` instead of `computer_session.paused`.

The plain Approve decision therefore succeeds on the same Mac. Approve and resume remains for the case where the session was already paused.

## Terminal grant interaction

The approval window above also preserves the existing 180-second terminal admission, so an approved `open_app Terminal` followed by reviewed keyboard input is not voided by the approval click.

## Server-visible folders

`list_computers` and `start_session` return `workspace_roots: [{"name": "...", "path": "..."}]` from the host descriptor so the agent can address folders. Paths are the user-visible paths of granted bookmarks; the server stores none of them durably.

## Secure input, takeover, resume

- Secure-input state refuses `type_text` and `press_keys` with `secure_input_active`; all other verbs proceed. The host stays registered.
- Takeover requires a click, key press, scroll, or pointer movement of more than 24 points within 250 milliseconds outside the approval window. Movement alone below that threshold is ignored.
- A session paused by the local Pause control carries `pause_reason: "local"`. `resume_session` from the server or another client is refused with `local_pause_requires_local_resume`; only the local Resume control clears it.

## Results

Every successful input verb result includes `screenshot` (same shape as the `screenshot` verb, bounded to 1280 pixels wide) taken after the effect settles, so the agent can continue without a second round trip.

## Rulings from the builder check (2026-10-25)

- Grant scope binds owner, host, session and chat. `task_id` is recorded for audit only; a grant survives across turns in the same chat until expiry or revocation, because `begin_task` resets per user message.
- Same-host detection uses the existing rule: the approval controller's websocket is the host's websocket (`remote_confirmation.computer_resume_target`). There is no `controller_host_id` field.
- Input inside Astral's own windows is never takeover, at any time: approving a card, pressing Esc, clicking Stop, using the Activity panel, typing the next message. The host classifies an event as takeover only when, 300 ms after the event, the frontmost application is not Astral. Input inside the browser pane is the one exception and pauses the agent's pane work. The fixed 10-second approval window is withdrawn.
- The floating session panel shows an "Approval waiting" state with a button that brings Astral forward; Astral never raises itself on its own.
- Pause reasons keep their existing names: `local_pause` (explicit Pause control) and `local_input` (takeover). Only `local_pause` refuses remote resume; `local_input` can be resumed by the agent's `resume_session` as today.
- Pointer verbs on a macOS host require `hwnd`; the server refuses a pointer verb without it with a message to call `list_windows` first. The server caches the last `list_windows` result and the last screenshot bytes per session (bounded to 3 MB), so the card gets the application name and window title from the cache and a Pillow crop when Pillow is importable, otherwise no crop.
- The grant decision lives in both card paths: `hitl_confirmation.evaluate` for input verbs (`authorize_action`) and `remote_confirmation` for `run_command`, which is classed destructive "always" so it uses the readable `summary_for` card.
- Never grantable as an application scope: terminal-capable applications (existing list), Safari, Chrome, Firefox, Arc, Edge and other browsers, Mail, Messages, FaceTime, System Settings, Keychain Access, 1Password, Bitwarden and other password managers, Finder windows showing hidden or system paths. Actions in those applications stay on exact review.
- Esc and Command-period cancel the current turn only; the computer session and its grants stay. Stop ends the session, revokes grants and cancels the turn. `cancel_task` is changed so it no longer ends the computer session.
- The host descriptor gains an optional `workspace_roots` list; `ComputerHostDescriptor.from_dict` accepts it as optional, old hosts without it still validate.
