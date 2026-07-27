# Contract: Remote Machines Inventory & Credential Surface

**Feature**: `063-remote-compute-agents` | **Spec**: [../spec.md](../spec.md)

A new chrome settings surface `remote_machines` where a user registers, probes, re-trusts, and
deletes their own machines and credentials. It renders on **web and native** (FR-012,
Constitution XII) — the full `render()` + `components()` trio, following `llm.py` (the BYO-LLM
credential form) as the template, **not** the web-only `agents.py` credential UI.

## Module contract (`backend/webrender/chrome/surfaces/remote_machines.py`)

Per `surfaces/__init__.py:3-19`, the module exports:
- `TITLE = "Remote machines"`
- `async render(orch, user_id, roles, params) -> str` — web HTML.
- `components(orch, user_id, roles, params) -> list[dict]` — native astralprims (via `_sdui`),
  bound to the **same** `chrome_*` handler keys as web (`_sdui.py:6-9`).
- `HANDLERS = {…}` — the actions below.
- Registered in `SURFACE_MODULES` (`surfaces/__init__.py:28`); flag-gated (see plan) so with the
  feature off the surface, its menu item, and its handlers do not exist (FR-005).

The menu item is added to the shared server-owned chrome definition so every client shows it in
the same place (Constitution XII).

## Registration form (FR-008, FR-011)

Fields: `label`, `address`, `port` (default 22), `username`, `os_family`
(select linux/windows/macos), `role` (select cluster/plain), `cred_type` (select
ssh_key/password), and the credential:
- **ssh_key** → a **multi-line** private-key field + an optional passphrase field.
- **password** → a single password field.

The private key MUST use a multi-line control that survives a pasted PEM intact (FR-011,
US1-2): a `<textarea>` on the web `render()` and `kind="textarea"` on native
`_sdui.field(...)` (`_sdui.py:88-95`). The current `agents.py` credential input is single-line
`<input type="password">` (`agents.py:513-514`) and is explicitly not reused.

### Conditional credential visibility — `visible_when` (063.1, additive)

The web form already shows only the credential group matching `cred_type` (the
`astral-cred-type` change handler in `client.js`). Native parity rides an ADDITIVE field
attribute emitted by `components()`:

```json
{"name": "password", "kind": "password",
 "visible_when": {"field": "cred_type", "equals": "password", "default": "ssh_key"}}
```

A capable client hides the field unless the named controller field's current value
(typed-value-or-`default` — the marker embeds the controller's default so clients resolve
untouched pickers without cross-field lookup) equals `equals`, re-evaluating on every
controller change. Hidden fields still submit whatever they hold; `chrome_machine_add`
keeps reading only the inputs matching `cred_type`, so a stale hidden value stays inert.
Clients that predate the attribute ignore it and render every field (the original 063
shape — the labels alone must therefore still disambiguate). Implemented on all three
native clients: Apple (`ComponentView.swift` `fieldIsVisible`), Windows (`renderer.py`
`_r_param_picker` conditional-visibility block), Android (`Input.kt` `fieldIsVisible`
+ `ParamVisibleWhenTest`).

Native param text fields and textareas MUST NOT autocapitalize or autocorrect
(usernames/hosts/PEMs are identifiers; iOS defaults corrupted them — 063.1, live-found).

The inventory starts **empty** for every user; the empty state is an explicit invitation to add
a machine, with **no** example, default, or pre-seeded host of any kind (FR-009, US1-1).

## Save → immediate real probe (FR-013, US1-3)

On save, the surface stores the machine + Fernet-encrypted credential, then **immediately**
calls `probe_machine`, which opens a real connection and (on first contact) records the host
key. The verdict is displayed from the enumerated vocabulary
([result-vocabulary.md](result-vocabulary.md)) — `ok`(authenticated) / `unreachable` /
`auth_failed` / `host_key_mismatch` / `mfa_required` / … — never a generic error. For a Windows
target without OpenSSH Server, the verdict is `unreachable` **plus the documented prerequisite**
(FR-017, US5-4), not a hang.

## Actions (`HANDLERS`, all owner-scoped)

| Action | Effect |
|---|---|
| `chrome_machine_add` | validate + insert `remote_machine`; store `machine_credential` (Fernet); probe; return verdict |
| `chrome_machine_probe` | re-run `probe_machine` for one machine; update `last_verdict`/`last_checked_at` |
| `chrome_credential_set` | rotate/replace the credential for a machine (blank field = leave unchanged, per `agents.py:866-871` semantics) |
| `chrome_credential_delete` | destroy the stored secret (`DELETE FROM machine_credential`); machine remains, verbs return `credential_not_configured` |
| `chrome_machine_retrust` | deliberately overwrite the recorded host key after a `host_key_mismatch` — the only path that changes a recorded identity (FR-020) |
| `chrome_machine_delete` | destroy machine + credential (FK cascade); mark any in-flight `tracked_job` for it `orphaned` (US1-6, FR-046); audit |

Every handler verifies `owner_user_id == user_id` so one user can never see, name, address, or
act on another user's machine (FR-010, SC-012). Deletions and credential changes are audited
(FR-047); secret values never appear in any response, log, or audit row (FR-049).

## Isolation & parity checks

- A second user's inventory listing returns only their own rows (US1-5, SC-012).
- The surface is exercised on web + at least one native client for the PEM round-trip (US1
  Independent Test); watch degrades to "manage on phone/desktop" from the shared definition.
