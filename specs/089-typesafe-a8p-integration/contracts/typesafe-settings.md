# Contract: TypeSafe Key and Data-Sharing Acknowledgment in LLM Settings

**Surface**: `backend/orchestrator/projection_surfaces/llm.py` (server-owned; web `render()` and native `components()`). Native clients receive the new fields through existing SDUI with no client code change.

## Section layout

The section is placed after the existing provider, model and key fields. It is hidden when `params.first_run == true`.

| Element | Web `render()` | SDUI `components()` (`_sdui.field`) |
|---|---|---|
| Heading | "TypeSafe routing (optional)" | `Text(variant="h3")` |
| Help text | "Uses your own TypeSafe API key for faster agent routing, result layout and an extra safety check. Without a key, Astral uses standard routing." | `Text` |
| Status line | One of: "Not set — standard routing", "Active", "Key rejected on {date} — update or remove it", "Temporarily unavailable — using standard routing" | `Badge` with variant `neutral`/`success`/`error`/`warning` + `Text` |
| Key input | `<input type="password" name="typesafe_api_key" value="" autocomplete="off">`; placeholder "Saved key hidden" when a record exists | `field("typesafe_api_key", "TypeSafe API key", kind="password", default="")` |
| Save | button `data-action="chrome_typesafe_save"` | `Button(action="chrome_typesafe_save")` |
| Remove | button `data-action="chrome_typesafe_clear"`, shown only when a record exists; asks for confirmation using the existing confirm pattern | `Button(action="chrome_typesafe_clear", variant="danger")` |

## Data-sharing warning and checkbox (all modes, including `first_run`)

Placement: immediately **below all credential inputs** (after the LLM provider, model and key fields, and after the TypeSafe key field when shown) and **above** the action buttons.

| Element | Web `render()` | SDUI `components()` |
|---|---|---|
| Warning | `<div class="astral-data-sharing-warning" role="note" id="data-sharing-warning">` with a warning icon, the title **"Your data may be shared"**, and body text "If you use a third-party model or a TypeSafe model, the content of your requests — which can include your messages and conversation context — is sent to that provider and may be shared with them." | `Alert(variant="warning", title="Your data may be shared", message=<same body>)` |
| Checkbox | `<input type="checkbox" name="data_sharing_acknowledged" id="data-sharing-ack" aria-describedby="data-sharing-warning">` + `<label for="data-sharing-ack">I understand that my data may be shared with third-party model providers and TypeSafe through requests.</label>`; `checked` when the current version is acknowledged | `field("data_sharing_acknowledged", "<same label>", kind="boolean", default=<acknowledged>)` |
| Acknowledged note | "Acknowledged on {date}" under the checkbox when the current version is acknowledged | `Text(variant="caption")` |
| Error | Inline under the checkbox: "Check this box to confirm you understand how your data is shared." | Field error through the existing form-error mechanism |

`NOTICE_VERSION` and both strings live in `backend/llm_config/data_sharing.py`. A test fails if the text changes without a version bump.

### Enforcement: `require_acknowledgment(user_id, submitted: bool | None) -> AckResult`

1. If `submitted is True`, upsert the acknowledgment for `NOTICE_VERSION` (audit it the first time this version is acknowledged) and allow the save.
2. Else, if the stored `notice_version == NOTICE_VERSION` **and** `submitted` is not an explicit `False`, allow the save. An explicit `False` means the user unchecked the box, so the save is rejected.
3. Otherwise reject: return the field error, audit `llm_data_sharing.save_blocked`, and make **no** probe or provider request.

| Action | Requires acknowledgment |
|---|---|
| `chrome_llm_save`, `chrome_typesafe_save` | Yes, as the first step |
| legacy WS `llm_config_set` | Yes. It reads optional `data_sharing_acknowledged`; if the check fails, the error message is "Confirm the data-sharing notice in Settings → LLM settings, then save again." |
| Test connection, Load models, `chrome_llm_clear`, `chrome_typesafe_clear` | No |
| Chat, routing, first-run gate predicate | Never consulted |

## Actions

### `chrome_typesafe_save` — payload `{typesafe_api_key: str}`

1. Authenticate the socket owner; the key is bound to the verified subject.
2. `require_acknowledgment(user_id, payload.fields.data_sharing_acknowledged)`. On failure, stop before any probe.
3. Validate: trim the key and require 1 ≤ len ≤ 512 with no whitespace. On failure, the field error reads "Enter a TypeSafe API key".
4. `_check_probe_rate(user_id)`. When limited, the message reads "Too many attempts — try again in a minute".
5. Probe: `AsyncTypeSafeClient(api_key=key, base_url=TYPESAFE_API_BASE, model=TYPESAFE_MODEL, retry=RetryPolicy(max_retries=0), timeout=5.0).models.list()`.
   - `TypeSafeAuthenticationError` / `TypeSafePermissionDeniedError` → "TypeSafe rejected this key." Nothing is stored.
   - Timeout, connection or 5xx → "Couldn't reach TypeSafe to verify the key. Try again." Nothing is stored.
6. On success, the durable credential operation (same mechanism as `_LLM_CREDENTIAL_SAVE_ACTIONS`) encrypts and upserts the record with outcome `valid`. It then resets the user's routing circuit, audits `typesafe_credential.saved` and re-renders the surface with status "Active".
7. **No** `llm_gate.unlock_after_save` call is made.

### `chrome_typesafe_clear` — payload `{}`

1. Authenticate the owner and delete the record. Clearing is idempotent: a missing record is still a success.
2. Reset the circuit, audit `typesafe_credential.cleared` and re-render with status "Not set".
3. **No** `llm_gate.regate_after_clear` call is made.

## Gate isolation (tested)

- `llm_configured_for(user_id)` is unchanged and ignores `user_typesafe_credential`.
- Neither action is in the gated-state allowed actions (`chrome_events.py` ~L378). They are reachable only after the LLM gate is satisfied.
- A user with a TypeSafe key and no LLM config remains gated.

## Hygiene (tested)

- The key never appears in rendered HTML, SDUI components, WS frames other than the inbound save payload, logs, audit, exceptions or durable-operation records.
- The durable operation record for `chrome_typesafe_save` stores the fingerprint and outcome only.
- `redact_llm_config` redacts the `typesafe_api_key` dict key, and `_assert_no_api_key` rejects it.
