# Contract: The Fixed, Closed Verb Set of `computer-use-1`

**Feature**: `076-remote-computer-control` | **Spec**: [../spec.md](../spec.md)

Contract-test target (`backend/tests/test_computer_use_verbs_076.py`): exact verb set, scopes,
tiers, destructive classification, timeouts, retry posture. Adding or reclassifying a verb
cannot pass unnoticed.

## Global rules

- **Addressing.** Every verb except `list_computers` takes `computer` (host id **or** unique
  name, optional when the owner has exactly one online host or exactly one active session).
  Ambiguity → `ambiguous_computer` listing candidates; unknown → `computer_unavailable`.
- **Session requirement.** Tier *input*, *consequential* and `screenshot`/`list_windows`/
  `get_clipboard`/`read_file`/`list_dir` require an **active** session; `paused` → typed
  `paused`; none → `no_session` with the hint to run `start_session`.
- **Unattended.** In a machine-class turn only `list_computers` is permitted; every other verb
  is refused `unattended_refused` before any frame reaches the host.
- **Never retried.** All verbs declare `retryable: false` (an input verb that timed out may
  still have happened). Timeouts are per verb; a timeout yields `timeout`, never a retry.
- **Typed, bounded results.** Text fields are capped; screen content is untrusted (taint).
- **Coordinates** are in the space of the most recent screenshot of the session
  (`width × height` reported there); the host maps to physical pixels via that screenshot's
  `scale`/`screen_index`. Before any screenshot, coordinates are physical primary-screen
  pixels.

## Session verbs — `tools:read` unless noted

| Verb | Args | Returns | Timeout | Gate |
|---|---|---|---|---|
| `list_computers` | — | `computers[]` {host_id, name, platform, online, screens, session{state, controller}} | 5 s | none (allowed unattended) |
| `start_session` | `computer` | session {session_id, host_id, name, screens, images_supported} | 10 s | live human only |
| `end_session` | `computer?` | {ended: true, reason: user_stop} | 5 s | live human only |
| `resume_session` | `computer?` | {state} after a 1.5 s settle; `paused` again ⇒ typed `paused` with a wait hint | 5 s | live human only |
| `confirm_action` | `computer?`, `summary` (≤ 400 chars) | {approved: true} | — | **always** → approval card (063 proposal); the verb body only returns after approval |

## Observe verbs — `tools:read`, no confirmation

| Verb | Args | Returns | Timeout |
|---|---|---|---|
| `screenshot` | `computer?`, `screen_index=0`, `max_width=1280` (320–1920) | `_images:[jpeg]`, `_ui_components:[image]`, `_data` {width, height, scale, screen_index} | 15 s |
| `list_windows` | `computer?` | windows[] {hwnd, title(≤200), process, rect, focused, minimized} (≤ 100) | 10 s |
| `get_clipboard` | `computer?` | {text (≤ 16 KiB), truncated} | 5 s |
| `read_file` | `computer?`, `path` (absolute), `max_bytes=65536` (≤ 262144) | {path, text, truncated, size} | 10 s |
| `list_dir` | `computer?`, `path` | entries[] {name, is_dir, size, modified} (≤ 500) | 10 s |
| `wait` | `computer?`, `seconds` (0.1–10) | {waited, state, pause_reason?} — server-side sleep, allowed while **paused** | 12 s |

## Input verbs — `tools:write`, session-gated, no per-action confirmation

| Verb | Args | Returns | Timeout |
|---|---|---|---|
| `click` | `computer?`, `x`, `y`, `button=left\|right\|middle`, `count=1\|2` | {x, y, button} | 10 s |
| `double_click` | `computer?`, `x`, `y` | — | 10 s |
| `right_click` | `computer?`, `x`, `y` | — | 10 s |
| `move` | `computer?`, `x`, `y` | — | 5 s |
| `drag` | `computer?`, `x1`, `y1`, `x2`, `y2` | — | 10 s |
| `scroll` | `computer?`, `x`, `y`, `dx=0`, `dy=-3` (±20 notches) | — | 5 s |
| `type_text` | `computer?`, `text` (≤ 4000 chars, Unicode); host refuses into a terminal without `terminal_ok` | {chars} | 30 s |
| `press_keys` | `computer?`, `keys` e.g. `"ctrl+shift+s"`, `"enter"`, `"alt+f4"` (fixed key table); same terminal rule | {keys} | 5 s |
| `focus_window` | `computer?`, `hwnd?` or `title?` (substring, case-insensitive) | {hwnd, title} | 5 s |
| `open_app` | `computer?`, `app` (bare name like `notepad`, `excel`, or an existing `.exe`/`.lnk` path), `args?` (argv list) | {pid?, launched} | 15 s |
| `set_clipboard` | `computer?`, `text` (≤ 16 KiB) | — | 5 s |

`open_app` validates `app` against `^[\w .+-]{1,80}$` or an existing path; no shell string is
assembled (`os.startfile` for names, `subprocess.Popen([path, *args])` for paths).

## Consequential verbs — gated on **every** reach (063 proposal card on the phone)

| Verb | Scope | Args | Returns | Timeout | Classification |
|---|---|---|---|---|---|
| `write_file` | `tools:files` | `computer?`, `path`, `content` (≤ 256 KiB), `if_exists=refuse\|overwrite` | {path, bytes} | 10 s | **always** |
| `delete_path` | `tools:files` | `computer?`, `path` | {path, deleted} | 10 s | **always** |
| `open_app` (shell only) | `tools:write` | see input verbs | | | `{"by_shell_app": true}` — `SHELL_APPS` |

**There is no arbitrary-shell verb (D8).** The tool-security analyzer hard-blocks any
`run_command`-shaped tool (Constitution VII), so commands run the way a person runs them:
open a terminal (approval card), then type. The host refuses `type_text`/`press_keys` while a
command interpreter owns the foreground window unless the request carries `terminal_ok`,
which the orchestrator sets for `TERMINAL_GRANT_S` (180 s) after an approved `confirm_action`
or an approved shell `open_app` on that session.

## Destructive classification (single source: `backend/orchestrator/computer_use_policy.py`)

```python
DESTRUCTIVE_CLASSIFICATION = {
    "write_file": "always", "delete_path": "always", "confirm_action": "always",
    "open_app": {"by_shell_app": True},
}
UNATTENDED_ALLOWED = frozenset({"list_computers"})
```

The card is delivered as the call's **result** (canvas + transcript, stable id
`au_approval_<proposal>`, replaced in place on decision), the model reads a stop
instruction, a repeat reach re-uses the pending card, and an approval re-dispatches the verb
then resumes the task with a continuation turn (a server-initiated, detached turn on the
same chat). If the approved verb could not even be attempted because the computer was
paused, the approval is kept for `APPROVAL_RETRY_GRACE_S` (180 s) so ONE retry with
identical arguments passes the gate without a second card.

The agent registry imports these; a contract test asserts the registry equals the policy.
