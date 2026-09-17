"""Deep-owned host adapter for the per-user LLM Projection surface.

Feature 054 (bring-your-own-LLM) rebuilt this surface around the PERSISTED
per-user configuration (``user_llm_config``, API key Fernet-encrypted at
rest) and the server-owned provider catalog
(:mod:`llm_config.providers`):

* A **provider dropdown** (OpenAI, Anthropic, Google Gemini, xAI Grok,
  OpenRouter, Groq, Together AI, Mistral, Ollama, LM Studio, Custom). Base
  URLs for catalog presets are SERVER-DERIVED at action time — the editable
  ``base_url`` field renders only for ``custom`` — so web and native SDUI
  cannot diverge (zero client-side prefill/lock logic).
* The API key is write-only: a "saved" placeholder is shown when a record
  exists and the key itself is NEVER echoed into markup or components.
  Key-optional presets (local runtimes and ``custom`` — self-hosted
  OpenAI-compatible endpoints are commonly keyless) may save with an empty
  key; the probe-gated save still refuses a keyless config the endpoint
  actually rejects.
* ``chrome_llm_save`` delegates to
  :func:`llm_config.ws_handlers.handle_llm_config_set` — the same
  probe-gated, persisting path the WS ``llm_config_set`` message uses — and
  runs the first-run-gate unlock fan-out on success. ``chrome_llm_clear``
  deletes the record and immediately RE-GATES all of the user's clients
  (there is no default to revert to).
* This surface doubles as the MANDATORY first-run dialog: ``params
  {"first_run": true}`` switches title/copy; delivery/undismissability are
  handled by :mod:`orchestrator.llm_gate`.
"""
from __future__ import annotations

import asyncio
import html as _htmlmod
import logging
import re
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

from pydantic import ValidationError

from llm_config.providers import (
    CUSTOM_PROVIDER_KEY,
    all_presets,
    get_preset,
    resolve_base_url,
)
from webrender.chrome import esc, notice_block

logger = logging.getLogger("Orchestrator.Chrome.LLM")

TITLE = "LLM settings"
# Feature 089 — the web dialog's chrome. A surface that declares nothing gets
# the plain dialog; these three sections are the three concerns this form
# already had, so the tabs describe the page rather than reorganising it.
SUBTITLE = "Your provider, your keys, and what leaves this machine"
ICON = "\u2699"
SECTIONS = (
    ("provider", "Provider"),
    ("routing", "Smart routing"),
    ("sharing", "Data sharing"),
)


def footer_html() -> str:
    """The dialog's action row. Same actions, same handlers, new placement."""
    return (
        f'{_button("chrome_llm_models", "Load models")}'
        f'{_button("chrome_llm_test", "Test connection")}'
        f'{_button("chrome_llm_save", "Save", primary=True)}'
    )

FIRST_RUN_TITLE = "Set up your AI provider"

SURFACE_KEY = "llm"

# Max characters of the upstream error message surfaced into a notice.
_UPSTREAM_SNIPPET_LEN = 300

_INPUT_CLS = (
    "rounded-lg bg-white/10 border border-white/10 px-3 py-2 text-sm "
    "text-astral-text w-full focus:outline-none focus:border-astral-primary/50"
)
_LABEL_CLS = "flex flex-col gap-1 text-sm"
_LABEL_TEXT_CLS = "text-astral-text font-medium"

_LOCAL_RUNTIME_NOTE = (
    "Local runtimes (Ollama, LM Studio) must be reachable FROM THE SERVER — "
    "a runtime on your own laptop is not reachable by a hosted deployment."
)


# ---------------------------------------------------------------------------
# Internals plumbing
# ---------------------------------------------------------------------------

def _fields(payload: Any) -> Dict[str, str]:
    """Extract the stripped-string field map from a ``ui_event`` payload."""
    raw = payload.get("fields") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str) and v is not None and not isinstance(v, (dict, list)):
            out[k] = str(v).strip()
    return out


def _claims(orch: Any, websocket: Any) -> Dict[str, Any]:
    """Return the JWT claims registered for ``websocket`` (empty dict if none)."""
    try:
        return (getattr(orch, "ui_sessions", None) or {}).get(websocket) or {}
    except Exception:
        return {}


def _actor(orch: Any, websocket: Any, user_id: str) -> Tuple[str, str]:
    """Mirror the orchestrator's ``llm_config_*`` actor attribution."""
    claims = _claims(orch, websocket)
    actor_user_id = claims.get("sub") or user_id or "legacy"
    auth_principal = claims.get("preferred_username") or claims.get("sub") or "unknown"
    return actor_user_id, auth_principal


def _store(orch: Any):
    """The orchestrator's persisted LLM-config store (``_llm_store``)."""
    return getattr(orch, "_llm_store", None)


async def _saved_config(orch: Any, user_id: str):
    """The user's persisted configuration, or ``None`` (feature 054)."""
    store = _store(orch)
    if store is None or not user_id:
        return None
    try:
        return await store.get(user_id)
    except Exception:
        logger.exception("llm surface: persisted-config read failed")
        return None


def _provider_key(fields: Dict[str, str]) -> str:
    """Normalize the submitted provider field (key or display label) to a
    catalog key; unknown values fall back to ``custom``."""
    raw = (fields.get("provider") or "").strip()
    if not raw:
        return CUSTOM_PROVIDER_KEY
    if get_preset(raw) is not None:
        return raw.lower()
    for p in all_presets():
        if raw.lower() == p.label.lower():
            return p.key
    return CUSTOM_PROVIDER_KEY


class SavedKeyEndpointChanged(ValueError):
    """Safe validation refusal when a saved secret's destination changes."""

    MESSAGE = (
        "The endpoint changed; enter the API key again. "
        "For a keyless endpoint, clear the saved configuration first."
    )

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


def _require_saved_key_destination(saved: Any, fields: Dict[str, str]) -> None:
    """Bind implicit key reuse to the same server-resolved API endpoint."""
    destination = _effective_base_url(_provider_key(fields), fields)
    saved_destination = str(getattr(saved, "base_url", "")).strip().rstrip("/")
    if not destination or destination.strip().rstrip("/") != saved_destination:
        raise SavedKeyEndpointChanged()


async def _resolve_api_key(orch: Any, websocket: Any, user_id: str,
                           fields: Dict[str, str]) -> Tuple[str, bool]:
    """Resolve the API key for an action: submitted value or the saved one.

    A blank password keeps the saved key only at the same API endpoint.
    Changing its destination requires explicit key entry before any probe,
    model listing or save. Preset endpoints are resolved by the server.
    """
    submitted = fields.get("api_key", "")
    if submitted:
        return submitted, False
    saved = await _saved_config(orch, user_id)
    if saved is not None and getattr(saved, "api_key", ""):
        _require_saved_key_destination(saved, fields)
        return saved.api_key, True
    return "", False


def _request_shim(orch: Any) -> Any:
    """Minimal stand-in for the FastAPI ``Request`` the probe endpoints take."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(orchestrator=orch)))


def _validation_message(exc: ValidationError) -> str:
    """First pydantic validation error as a short ``field: message`` string."""
    try:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in (first.get("loc") or ())) or "input"
        return f"{loc}: {first.get('msg', 'invalid value')}"
    except Exception:
        return "Invalid input."


_MARKUP_RE = re.compile(r"<[^>]+>")

#: error_class → what the user should actually DO about it.
_FAILURE_HINTS = {
    "auth_failed": "the endpoint rejected the API key. Re-check the key "
                   "(it is never displayed after saving — re-enter it to replace it).",
    "model_not_found": "the endpoint doesn't offer that model id. "
                       "Use “Load models” to pick from the models it actually serves.",
    "transport_error": "the endpoint couldn't be reached. Check the provider/endpoint, "
                       "your network, and any VPN/proxy.",
    "contract_violation": "that address answered, but not like an OpenAI-compatible "
                          "API. Point the endpoint at an API root "
                          "(usually ending in /v1, e.g. https://api.openai.com/v1), "
                          "not a website.",
}
_FAILURE_HINT_DEFAULT = ("the endpoint's reply wasn't usable. Double-check the "
                         "provider, the model id, and the API key.")


def _clean_upstream(raw: str) -> str:
    """A human-safe upstream snippet: markup stripped, whitespace collapsed,
    bounded; a whole HTML page yields ``""``."""
    raw = (raw or "").strip()
    if raw.lower().startswith(("<!doctype", "<html")) or "<html" in raw[:200].lower():
        return ""  # an HTML page, not a message
    text = " ".join(_htmlmod.unescape(_MARKUP_RE.sub(" ", raw)).split())
    return text[:_UPSTREAM_SNIPPET_LEN]


def _failure_notice(prefix: str, error_class: Optional[str], upstream_message: Optional[str]) -> str:
    """User-actionable error notice: taxonomy class + a WHAT-TO-DO hint."""
    hint = _FAILURE_HINTS.get(error_class or "", _FAILURE_HINT_DEFAULT)
    msg = f"{prefix} ({error_class or 'unknown'}) — {hint}"
    detail = _clean_upstream(upstream_message or "")
    if detail:
        msg = f"{msg} Upstream said: “{detail}”"
    return notice_block("error", msg)


def _effective_base_url(provider: str, fields: Dict[str, str]) -> Optional[str]:
    """Server-derived endpoint for the submitted provider (catalog presets
    ignore any submitted base_url; only ``custom`` honors it)."""
    return resolve_base_url(provider, fields.get("base_url", ""))


# ---------------------------------------------------------------------------
# Render (web HTML)
# ---------------------------------------------------------------------------

def _model_field(model: str, models: Optional[list]) -> str:
    """The model input — a ``<select>`` once models are loaded, else text."""
    if models:
        listed = list(dict.fromkeys(str(m) for m in models if str(m).strip()))
        if model and model not in listed:
            listed.insert(0, model)
        opts = []
        for m in listed:
            sel = " selected" if m == model else ""
            opts.append(f'<option value="{esc(m)}"{sel}>{esc(m)}</option>')
        return (f'<select name="model" class="{_INPUT_CLS} astral-field">'
                f'{"".join(opts)}</select>')
    return (
        f'<input type="text" name="model" value="{esc(model)}" '
        f'placeholder="e.g. gpt-4o-mini" autocomplete="off" class="{_INPUT_CLS} astral-field">'
    )


def _provider_field(provider: str) -> str:
    """The provider ``<select>`` from the server-owned catalog.

    Carries ``astral-field`` (dark option styling + color-scheme) and
    ``astral-llm-provider`` (the client-side change hook in client.js that
    toggles the endpoint field between the preset caption and the custom
    input without a server round-trip).
    """
    opts = []
    for p in all_presets():
        sel = " selected" if p.key == provider else ""
        opts.append(f'<option value="{esc(p.key)}"{sel}>{esc(p.label)}</option>')
    return (f'<select name="provider" class="{_INPUT_CLS} astral-field astral-llm-provider">'
            f'{"".join(opts)}</select>')


def _endpoint_block(provider: str, base_url: str) -> str:
    """Render BOTH the preset-endpoint caption and the custom base-URL input,
    showing the one that matches ``provider`` and hiding the other.

    The chrome modal is static HTML with no reactive re-render, so the
    client-side ``astral-llm-provider`` change handler flips which half is
    visible when the dropdown changes. Both halves are always in the DOM:
    for a preset the (hidden) ``base_url`` input submits empty and the
    server derives the URL; for custom the input is the source of truth.
    """
    is_custom = provider == CUSTOM_PROVIDER_KEY
    preset = get_preset(provider)
    preset_url = "" if is_custom else (getattr(preset, "base_url", "") or "")
    caption_style = ' style="display:none"' if is_custom else ""
    input_style = "" if is_custom else ' style="display:none"'
    return (
        '<div class="astral-llm-endpoint">'
        f'<p class="astral-llm-endpoint-preset text-xs text-astral-muted"{caption_style}>'
        'Endpoint: <span class="astral-llm-endpoint-url font-mono">'
        f'{esc(preset_url)}</span> (set automatically for this provider)</p>'
        f'<label class="astral-llm-endpoint-custom {_LABEL_CLS}"{input_style}>'
        f'<span class="{_LABEL_TEXT_CLS}">Endpoint (Base URL)</span>'
        f'<input type="text" name="base_url" value="{esc(base_url if is_custom else "")}" '
        f'placeholder="https://api.openai.com/v1" autocomplete="off" '
        f'class="{_INPUT_CLS} astral-field"></label>'
        "</div>"
    )


def _provider_endpoints_json() -> str:
    """JSON map ``{provider_key: base_url}`` embedded on the form for the
    client-side endpoint toggle (custom maps to "")."""
    import json as _json
    return esc(_json.dumps({p.key: (p.base_url or "") for p in all_presets()}))


def _button(action: str, label: str, primary: bool = False, collect: bool = True) -> str:
    """A chrome action button (``data-ui-action`` + optional field collection)."""
    if primary:
        cls = ("bg-astral-primary/20 text-astral-primary border border-astral-primary/30 "
               "hover:bg-astral-primary/30")
    else:
        cls = "bg-white/5 text-astral-text border border-white/10 hover:bg-white/10"
    collect_attr = ' data-ui-collect="true"' if collect else ""
    return (
        f'<button type="button" class="px-3 py-2 rounded-lg text-sm font-medium {cls}" '
        f'data-ui-action="{esc(action)}"{collect_attr}>{esc(label)}</button>'
    )



# ---------------------------------------------------------------------------
# Feature 089 — TypeSafe key and the data-sharing acknowledgment
# ---------------------------------------------------------------------------


def _typesafe_store(orch: Any):
    """The TypeSafe credential store, or ``None`` when it is not wired."""
    return getattr(orch, "_typesafe_store", None)


def _data_sharing_store(orch: Any):
    return getattr(orch, "_data_sharing_store", None)


async def _typesafe_status(orch: Any, user_id: str):
    """The renderable status. A missing store reads as "not set"."""
    from llm_config.typesafe_store import TypeSafeKeyStatus

    store = _typesafe_store(orch)
    if store is None:
        return TypeSafeKeyStatus()
    try:
        return await store.status(user_id)
    except Exception:
        logger.debug("TypeSafe status read failed (non-fatal)", exc_info=True)
        return TypeSafeKeyStatus()


async def _acknowledgment_state(orch: Any, user_id: str):
    from llm_config.data_sharing import AcknowledgmentState

    store = _data_sharing_store(orch)
    if store is None:
        return AcknowledgmentState()
    try:
        return await store.state(user_id)
    except Exception:
        logger.debug("data-sharing state read failed (non-fatal)", exc_info=True)
        return AcknowledgmentState()


def _typesafe_status_line(status: Any) -> tuple[str, str]:
    """Return ``(text, badge variant)`` for a status. Never shows key material."""
    when = getattr(status, "at", None)
    stamp = when.date().isoformat() if when is not None else "an earlier date"
    name = getattr(status, "name", "not_set")
    if name == "active":
        return "Active", "success"
    if name == "rejected":
        return f"Key rejected on {stamp} — update or remove it", "error"
    if name == "unavailable":
        return "Temporarily unavailable — using standard routing", "warning"
    return "Not set — standard routing", "neutral"


_TYPESAFE_HEADING = "TypeSafe routing (optional)"
_TYPESAFE_HELP = (
    "Uses your own TypeSafe API key for faster agent routing, result layout and "
    "an extra safety check. Without a key, Astral uses standard routing."
)


def _typesafe_block(status: Any) -> str:
    """The web TypeSafe section. Returns "" when it should be hidden."""
    from llm_config.data_sharing import FIELD_NAME  # noqa: F401 - documents the pairing

    line, variant = _typesafe_status_line(status)
    badge_styles = {
        "success": "bg-green-500/10 text-green-400 border-green-500/20",
        "error": "bg-red-500/10 text-red-400 border-red-500/20",
        "warning": "bg-amber-500/10 text-amber-300 border-amber-500/20",
        "neutral": "bg-white/5 text-astral-muted border-white/10",
    }
    badge = (
        f'<span class="text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 '
        f'rounded-full border {badge_styles[variant]}">{esc(line)}</span>'
    )
    placeholder = "Saved key hidden" if getattr(status, "is_set", False) else ""
    remove = (
        _button("chrome_typesafe_clear", "Remove", collect=False)
        if getattr(status, "is_set", False)
        else ""
    )
    return (
        '<div class="bg-white/5 border border-white/10 rounded-lg p-4 space-y-3">'
        f'<div class="flex items-center justify-between">'
        f'<span class="{_LABEL_TEXT_CLS}">{esc(_TYPESAFE_HEADING)}</span>{badge}</div>'
        f'<p class="text-xs text-astral-muted">{esc(_TYPESAFE_HELP)}</p>'
        f'<label class="{_LABEL_CLS}">'
        f'<span class="{_LABEL_TEXT_CLS}">TypeSafe API key</span>'
        f'<input type="password" name="typesafe_api_key" value="" '
        f'placeholder="{esc(placeholder)}" autocomplete="off" class="{_INPUT_CLS}">'
        "</label>"
        '<div class="flex flex-wrap gap-2">'
        f'{_button("chrome_typesafe_save", "Save TypeSafe key")}'
        f"{remove}"
        "</div></div>"
    )


def _data_sharing_block(state: Any, error: Optional[str] = None) -> str:
    """The web data-sharing warning and checkbox.

    Placed below every credential input and above the actions, because that is
    the moment the user is actually deciding to hand over a key.
    """
    from llm_config import data_sharing as ds

    acknowledged = bool(getattr(state, "acknowledged", False))
    when = getattr(state, "acknowledged_at", None)
    note = (
        f'<p class="astral-data-sharing-note">Acknowledged on '
        f"{esc(when.date().isoformat())}.</p>"
        if acknowledged and when is not None
        else ""
    )
    error_block = (
        f'<p class="astral-data-sharing-error" role="alert">{esc(error)}</p>'
        if error
        else ""
    )
    # Colors come from the ThemeView warning role, not from a palette class:
    # the same block has to look right under every theme a user can pick.
    return (
        f'<div class="astral-data-sharing-warning" role="note" '
        f'id="{ds.WARNING_ELEMENT_ID}">'
        f'<p class="astral-data-sharing-title">⚠ {esc(ds.NOTICE_TITLE)}</p>'
        f'<p class="astral-data-sharing-body">{esc(ds.NOTICE_BODY)}</p>'
        "</div>"
        '<label class="astral-data-sharing-ack">'
        f'<input type="checkbox" name="{ds.FIELD_NAME}" id="{ds.CHECKBOX_ELEMENT_ID}" '
        f'aria-describedby="{ds.WARNING_ELEMENT_ID}"'
        f'{" checked" if acknowledged else ""}>'
        f"<span>{esc(ds.CHECKBOX_LABEL)}</span></label>"
        f"{note}{error_block}"
    )


async def render(orch: Any, user_id: str, roles: Any, params: Any) -> str:
    """Render the provider-setup / LLM settings form body.

    ``params`` may carry re-render state from handlers — ``provider`` /
    ``base_url`` / ``model`` (submitted values to preserve), ``models``
    (list → model ``<select>``), and ``first_run`` (mandatory-dialog copy).
    NEVER carries an API key.
    """
    _ = roles
    params = params if isinstance(params, dict) else {}
    first_run = bool(params.get("first_run"))
    saved = await _saved_config(orch, user_id)
    provider = str(params.get("provider")
                   or (getattr(saved, "provider", "") if saved else "")
                   or "openai").lower()
    if get_preset(provider) is None:
        provider = CUSTOM_PROVIDER_KEY
    preset = get_preset(provider)
    base_url = str(params.get("base_url")
                   or (getattr(saved, "base_url", "") if saved else "") or "")
    model = str(params.get("model") or (getattr(saved, "model", "") if saved else "") or "")
    models = params.get("models") if isinstance(params.get("models"), list) else None

    endpoint_block = _endpoint_block(provider, base_url)

    # Feature 089. The TypeSafe section is hidden during first run: the user is
    # being asked for the one credential the product cannot start without, and
    # an optional second one next to it reads as a second requirement.
    typesafe_status = await _typesafe_status(orch, user_id)
    acknowledgment = await _acknowledgment_state(orch, user_id)
    typesafe_block = "" if first_run else _typesafe_block(typesafe_status)
    data_sharing_block = _data_sharing_block(
        acknowledgment, error=params.get("data_sharing_error")
    )

    key_optional = preset is not None and not preset.key_required
    key_label = "API key" + (" (optional for local runtimes)" if key_optional else "")
    if saved is not None and saved.has_key:
        key_placeholder = "Saved — leave blank to keep at this endpoint"
        key_hint = (
            '<p class="text-xs text-astral-muted">An API key is saved for your account. '
            "Leave blank to keep it at this endpoint. Enter it again if the endpoint changes.</p>"
        )
    else:
        key_placeholder = (preset.key_prefix_hint if preset else "") or "sk-..."
        key_hint = ""
    if saved is not None:
        saved_badge = (
            '<span class="text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 '
            'rounded-full bg-green-500/10 text-green-400 border border-green-500/20">'
            "configured</span>"
        )
        clear_btn = _button("chrome_llm_clear", "Clear configuration", collect=False)
    else:
        saved_badge = (
            '<span class="text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 '
            'rounded-full bg-white/5 text-astral-muted border border-white/10">'
            "not configured</span>"
        )
        clear_btn = ""

    if first_run:
        # Name the signed-in account: the configuration is per-user and
        # server-side, so someone signed in under a second identity must be able
        # to see WHY they are being asked again (their other account's config is
        # intact — this one has none).
        who = str(params.get("principal") or "").strip()
        identity = (
            f'<p class="text-xs text-astral-muted">Signed in as '
            f'<span class="text-astral-text font-medium">{esc(who)}</span>. This is '
            "saved to your account and applies to all your devices.</p>"
        ) if who else ""
        intro = (
            '<p class="text-sm text-astral-text">AstralDeep runs on the AI provider '
            "YOU connect — nothing is built in. Pick a provider, paste your API key, "
            "choose a model, and test the connection to get started.</p>"
            f"{identity}"
            f'<p class="text-xs text-astral-muted">{esc(_LOCAL_RUNTIME_NOTE)}</p>'
        )
    else:
        intro = (
            '<p class="text-xs text-astral-muted">Your provider configuration is stored '
            "for your account (API key encrypted at rest) and applies to all of your "
            f"devices. {esc(_LOCAL_RUNTIME_NOTE)}</p>"
        )

    # Feature 089: the three concerns become the dialog's three sections. The
    # form wrapper still spans all of them, so Save collects every field
    # regardless of which section is on screen — switching tabs must never
    # silently drop what someone typed on another one.
    return (
        f"{intro}"
        f"<div data-ui-form data-llm-endpoints='{_provider_endpoints_json()}' class=\"space-y-4\">"
        '<div data-section="provider">'
        '<div class="bg-white/5 border border-white/10 rounded-lg p-4 space-y-3">'
        f'<div class="flex items-center justify-between">'
        f'<span class="{_LABEL_TEXT_CLS}">AI provider</span>{saved_badge}</div>'
        f'<label class="{_LABEL_CLS}"><span class="{_LABEL_TEXT_CLS}">Provider</span>'
        f"{_provider_field(provider)}</label>"
        f"{endpoint_block}"
        f'<label class="{_LABEL_CLS}"><span class="{_LABEL_TEXT_CLS}">{esc(key_label)}</span>'
        f'<input type="password" name="api_key" value="" placeholder="{esc(key_placeholder)}" '
        f'autocomplete="off" class="{_INPUT_CLS}">'
        f"</label>{key_hint}"
        f'<label class="{_LABEL_CLS}"><span class="{_LABEL_TEXT_CLS}">Model</span>'
        f"{_model_field(model, models)}</label>"
        "</div>"
        f'<div class="flex flex-wrap gap-2 mt-3">{clear_btn}</div>'
        "</div>"
        f'<div data-section="routing">{typesafe_block}</div>'
        f'<div data-section="sharing">{data_sharing_block}</div>'
        "</div>"
    )


async def components(orch: Any, user_id: str, roles: Any, params: Any):
    """Feature 043 — the surface as native SDUI components.

    Native forms can't re-render when the provider dropdown changes (no
    client round-trip), so the ``base_url`` field is ALWAYS present and
    editable, prefilled with the selected provider's endpoint. For a preset
    the server derives the URL and ignores the submitted value; for
    ``custom`` the field is the source of truth — so picking "Custom" always
    yields a usable endpoint input. All actions submit to the same
    ``chrome_llm_*`` handlers.
    """
    from webrender.chrome.surfaces import _sdui
    params = params if isinstance(params, dict) else {}
    first_run = bool(params.get("first_run"))
    saved = await _saved_config(orch, user_id)
    provider = str(params.get("provider")
                   or (getattr(saved, "provider", "") if saved else "")
                   or "openai").lower()
    if get_preset(provider) is None:
        provider = CUSTOM_PROVIDER_KEY
    preset = get_preset(provider)
    base_url = str(params.get("base_url")
                   or (getattr(saved, "base_url", "") if saved else "") or "")
    model = str(params.get("model") or (getattr(saved, "model", "") if saved else "") or "")
    models = params.get("models") if isinstance(params.get("models"), list) else None

    provider_field = _sdui.field(
        "provider", "Provider", "select", default=provider,
        options=[p.key for p in all_presets()],
        help="; ".join(f"{p.key} = {p.label}" for p in all_presets()
                       if p.key != provider)[:200] or None,
    )
    # base_url: prefill custom with the saved/submitted URL; presets prefill
    # with the catalog endpoint (server ignores it for presets).
    endpoint_default = base_url if provider == CUSTOM_PROVIDER_KEY else (
        getattr(preset, "base_url", "") or "")
    form_fields = [
        provider_field,
        _sdui.field("base_url", "Endpoint (Base URL)", "text",
                    default=endpoint_default,
                    help="Auto-set for hosted providers; required for Custom "
                         "(e.g. https://api.openai.com/v1)."),
    ]
    key_optional = preset is not None and not preset.key_required
    if saved is not None and saved.has_key:
        key_help = "A key is saved; leave blank at this endpoint. Enter it again if the endpoint changes."
    elif key_optional:
        key_help = "Optional for local runtimes."
    else:
        key_help = "Stored encrypted for your account."
    form_fields.append(_sdui.field("api_key", "API key", "password", help=key_help))
    if models:
        listed = list(dict.fromkeys(str(m) for m in models if str(m).strip()))
        if model and model not in listed:
            listed.insert(0, model)
        form_fields.append(_sdui.field("model", "Model", "select",
                                       default=model, options=listed))
    else:
        form_fields.append(_sdui.field("model", "Model", "text", default=model,
                                       help="e.g. gpt-4o-mini"))

    # Feature 089: the TypeSafe field reaches every native client through the
    # existing SDUI field vocabulary, so no client changes.
    typesafe_status = await _typesafe_status(orch, user_id)
    if not first_run:
        status_line, _variant = _typesafe_status_line(typesafe_status)
        form_fields.append(
            _sdui.field(
                "typesafe_api_key",
                "TypeSafe API key (optional)",
                "password",
                help=f"{_TYPESAFE_HELP} Status: {status_line}.",
            )
        )

    # The acknowledgment sits below every credential input and above the
    # actions, in first-run mode too.
    from llm_config import data_sharing as _ds

    acknowledgment = await _acknowledgment_state(orch, user_id)
    form_fields.append(
        _sdui.field(
            _ds.FIELD_NAME,
            _ds.CHECKBOX_LABEL,
            "boolean",
            default=bool(getattr(acknowledgment, "acknowledged", False)),
            help=_ds.NOTICE_BODY,
        )
    )

    intro = ("AstralDeep runs on the AI provider YOU connect — nothing is "
             "built in. Pick a provider, add your API key, choose a model, "
             "and save to get started." if first_run else
             "Your provider configuration is stored for your account (API key "
             "encrypted at rest) and applies to all of your devices.")
    # Same identity context as the web dialog: on a second device under a
    # different account, "set up your AI provider" with no name reads as a
    # sync failure rather than a different sign-in.
    who = str(params.get("principal") or "").strip()
    if first_run and who:
        intro += (f" Signed in as {who}. This is saved to your account and "
                  "applies to all your devices.")
    out = [
        _sdui.text(intro, "caption"),
        _sdui.badge("configured" if saved is not None else "not configured",
                    "success" if saved is not None else "default"),
    ]
    if preset is not None and preset.base_url:
        out.append(_sdui.text(f"Endpoint: {preset.base_url} (set automatically)",
                              "caption"))
    if provider in ("ollama", "lmstudio") or first_run:
        out.append(_sdui.text(_LOCAL_RUNTIME_NOTE, "caption"))
    out.append(_sdui.alert(_ds.NOTICE_BODY, "warning", _ds.NOTICE_TITLE))
    acknowledged_at = getattr(acknowledgment, "acknowledged_at", None)
    if getattr(acknowledgment, "acknowledged", False) and acknowledged_at is not None:
        out.append(
            _sdui.text(
                f"Acknowledged on {acknowledged_at.date().isoformat()}.", "caption"
            )
        )
    actions = [
        {"label": "Load models", "action": "chrome_llm_models"},
        {"label": "Test connection", "action": "chrome_llm_test"},
        {"label": "Save", "action": "chrome_llm_save", "variant": "primary"},
    ]
    if not first_run:
        actions.append(
            {"label": "Save TypeSafe key", "action": "chrome_typesafe_save"}
        )
    out.append(_sdui.form(form_fields, actions=actions))
    if saved is not None:
        out.append(_sdui.button("Clear configuration", "chrome_llm_clear",
                                variant="secondary"))
    if not first_run and getattr(typesafe_status, "is_set", False):
        out.append(
            _sdui.button("Remove TypeSafe key", "chrome_typesafe_clear",
                         variant="danger")
        )
    return out


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _keep_params(fields: Dict[str, str], provider: str) -> Dict[str, Any]:
    return {
        "provider": provider,
        "base_url": fields.get("base_url", ""),
        "model": fields.get("model", ""),
    }



def _submitted_acknowledgment(payload: Any) -> Optional[bool]:
    """Read the checkbox as a tri-state.

    Present and truthy is a yes, present and falsey is an explicit no, and
    absent is "the client did not send the field" -- which falls back to what
    is stored, so a client that has not been updated keeps working.
    """
    fields = _fields(payload)
    from llm_config.data_sharing import FIELD_NAME

    if FIELD_NAME not in fields:
        raw_payload = payload if isinstance(payload, dict) else {}
        if FIELD_NAME not in raw_payload:
            return None
        raw = raw_payload.get(FIELD_NAME)
    else:
        raw = fields.get(FIELD_NAME)
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return None
    normalized = str(raw).strip().lower()
    if normalized in ("1", "true", "yes", "on", "checked"):
        return True
    if normalized in ("0", "false", "no", "off", "", "unchecked"):
        return False
    return None


async def _require_acknowledgment(
    orch: Any, websocket: Any, user_id: str, payload: Any, *, target: str
):
    """Run the acknowledgment gate. Returns ``None`` when the save may proceed.

    This is the **first** step of every credential save: it happens before any
    validation and before any provider request, so an unacknowledged save
    cannot leak the user's request content to a provider on its way to being
    rejected.
    """
    from llm_config import data_sharing as ds

    store = _data_sharing_store(orch)
    if store is None:
        # Without the store there is nothing to enforce against. Failing the
        # save here would lock every user out of settings over a wiring
        # problem, so the gate is skipped and the absence is logged.
        logger.warning("data-sharing store unavailable; acknowledgment not enforced")
        return None

    submitted = _submitted_acknowledgment(payload)
    result = await asyncio.to_thread(
        ds.require_acknowledgment, store, user_id, submitted
    )
    if result.allowed:
        if result.newly_acknowledged:
            actor_user_id, auth_principal = _actor(orch, websocket, user_id)
            await ds.record_acknowledged(
                getattr(orch, "audit_recorder", None),
                actor_user_id=actor_user_id,
                auth_principal=auth_principal,
            )
        return None

    actor_user_id, auth_principal = _actor(orch, websocket, user_id)
    await ds.record_save_blocked(
        getattr(orch, "audit_recorder", None),
        actor_user_id=actor_user_id,
        auth_principal=auth_principal,
        target=target,
    )
    return result


async def _handle_models(orch: Any, websocket: Any, user_id: str, roles: Any, payload: Any):
    """``chrome_llm_models {fields}`` — list the endpoint's advertised models."""
    _ = roles
    from llm_config.api import ListModelsRequest, list_models

    fields = _fields(payload)
    provider = _provider_key(fields)
    keep = _keep_params(fields, provider)
    base_url = _effective_base_url(provider, fields)
    if not base_url:
        return (SURFACE_KEY, keep, notice_block(
            "error", "Enter the endpoint address for your custom provider."))
    try:
        api_key, _used_saved = await _resolve_api_key(orch, websocket, user_id, fields)
    except SavedKeyEndpointChanged as exc:
        return (SURFACE_KEY, keep, notice_block("error", str(exc)))
    preset = get_preset(provider)
    if not api_key and (preset is None or preset.key_required):
        return (SURFACE_KEY, keep, notice_block(
            "error",
            "An API key is required to load models "
            "(it may be left blank only when one is already saved).",
        ))
    try:
        body = ListModelsRequest(api_key=api_key or "not-needed", base_url=base_url)
    except ValidationError as exc:
        return (SURFACE_KEY, keep, notice_block("error", _validation_message(exc)))

    resp = await list_models(
        body=body,
        request=_request_shim(orch),
        user_id=user_id,
        user_payload=_claims(orch, websocket),
    )
    if not resp.ok:
        return (SURFACE_KEY, keep, _failure_notice(
            "Couldn't load models", resp.error_class, resp.upstream_message))
    if not resp.models:
        return (SURFACE_KEY, keep, notice_block(
            "info", "The endpoint advertises no models — enter a model id manually."))
    keep["models"] = list(resp.models)
    return (SURFACE_KEY, keep, notice_block(
        "success", f"Loaded {len(resp.models)} models from {body.base_url}."))


async def _handle_test(orch: Any, websocket: Any, user_id: str, roles: Any, payload: Any):
    """``chrome_llm_test {fields}`` — probe the configuration, render verdict."""
    _ = roles
    from llm_config.api import TestConnectionRequest, test_connection

    fields = _fields(payload)
    provider = _provider_key(fields)
    keep = _keep_params(fields, provider)
    base_url = _effective_base_url(provider, fields)
    model = fields.get("model", "")
    if not base_url:
        return (SURFACE_KEY, keep, notice_block(
            "error", "Enter the endpoint address for your custom provider."))
    if not model:
        return (SURFACE_KEY, keep, notice_block("error", "Model is required."))
    try:
        api_key, _used_saved = await _resolve_api_key(orch, websocket, user_id, fields)
    except SavedKeyEndpointChanged as exc:
        return (SURFACE_KEY, keep, notice_block("error", str(exc)))
    preset = get_preset(provider)
    if not api_key and (preset is None or preset.key_required):
        return (SURFACE_KEY, keep, notice_block(
            "error",
            "An API key is required to test the connection "
            "(it may be left blank only when one is already saved).",
        ))
    try:
        body = TestConnectionRequest(api_key=api_key or "not-needed",
                                     base_url=base_url, model=model)
    except ValidationError as exc:
        return (SURFACE_KEY, keep, notice_block("error", _validation_message(exc)))

    resp = await test_connection(
        body=body,
        request=_request_shim(orch),
        user_id=user_id,
        user_payload=_claims(orch, websocket),
    )
    if resp.ok:
        latency = f" in {int(resp.latency_ms)} ms" if resp.latency_ms is not None else ""
        return (SURFACE_KEY, keep, notice_block(
            "success", f"Connection OK — model {resp.model} responded{latency}."))
    return (SURFACE_KEY, keep, _failure_notice(
        "Connection test failed", resp.error_class, resp.upstream_message))


async def _handle_save(orch: Any, websocket: Any, user_id: str, roles: Any, payload: Any):
    """``chrome_llm_save {fields}`` — probe-gated persist to the user store.

    Delegates to :func:`llm_config.ws_handlers.handle_llm_config_set` — the
    EXACT function the WS ``llm_config_set`` branch calls — so validation,
    the server-side probe, persistence, audit, and the ``llm_config_ack``
    reply are identical on every path. On success the first-run gate (if
    active) unlocks across all of the user's sockets.
    """
    _ = roles
    from llm_config.ws_handlers import handle_llm_config_set

    fields = _fields(payload)
    provider = _provider_key(fields)
    keep = _keep_params(fields, provider)
    # Feature 089 US7: acknowledgment first, before validation and before any
    # provider request.
    blocked = await _require_acknowledgment(
        orch, websocket, user_id, payload, target="the LLM provider"
    )
    if blocked is not None:
        keep["data_sharing_error"] = blocked.error
        return (SURFACE_KEY, keep, notice_block("error", blocked.error))
    try:
        api_key, used_saved = await _resolve_api_key(orch, websocket, user_id, fields)
    except SavedKeyEndpointChanged as exc:
        return (SURFACE_KEY, keep, notice_block("error", str(exc)))
    store = _store(orch)
    if store is None:
        return (SURFACE_KEY, keep, notice_block(
            "error", "Configuration store unavailable — try reloading."))

    actor_user_id, auth_principal = _actor(orch, websocket, user_id)
    saved = await handle_llm_config_set(
        safe_send=orch._safe_send,
        websocket=websocket,
        config={
            "provider": provider,
            "api_key": api_key,
            "base_url": fields.get("base_url", ""),
            "model": fields.get("model", ""),
        },
        actor_user_id=actor_user_id,
        auth_principal=auth_principal,
        store=store,
        recorder=orch.audit_recorder,
    )
    if not saved:
        return (SURFACE_KEY, keep, notice_block(
            "error",
            "Save rejected — check the provider, endpoint, model, and API key, "
            "then test the connection.",
        ))
    # First-run gate: a successful save unblocks ALL of the user's sockets
    # (closes the mandatory dialog + renders the welcome canvas). For the
    # gated socket the unlock replaces the modal, so skip the re-render.
    try:
        from orchestrator import llm_gate
        unlocked = await llm_gate.unlock_after_save(orch, actor_user_id)
    except Exception:
        logger.exception("llm gate unlock failed (non-fatal)")
        unlocked = False
    if unlocked:
        return None
    # Already-configured (settings-path) save: nothing was gated, so no unlock
    # closed the surface. Web can answer with a success notice because its
    # modal shell carries a ✕, and Android has system Back — but an Apple
    # surface is a full screen with neither, so a notice re-render would strand
    # it on screen with the save already done (the reported macOS symptom).
    # Close it instead; a REJECTED save still returns its error notice above,
    # so the surface only stays open when it still needs the user.
    from orchestrator.chrome_events import is_native_sdui, push_close
    if is_native_sdui(orch, websocket):
        await push_close(orch, websocket)
        return None
    suffix = " (kept the previously saved API key)" if used_saved else ""
    return (SURFACE_KEY, keep, notice_block(
        "success", f"AI provider saved for your account{suffix}."))


async def _handle_clear(orch: Any, websocket: Any, user_id: str, roles: Any, payload: Any):
    """``chrome_llm_clear`` — delete the persisted record and RE-GATE.

    With no operator default to revert to (feature 054), clearing makes the
    user unconfigured: the mandatory setup dialog is pushed to all of their
    connected clients immediately.
    """
    _ = roles
    _ = payload
    from llm_config.ws_handlers import handle_llm_config_clear

    store = _store(orch)
    if store is None:
        return (SURFACE_KEY, {}, notice_block(
            "error", "Configuration store unavailable — try reloading."))
    actor_user_id, auth_principal = _actor(orch, websocket, user_id)
    removed = await handle_llm_config_clear(
        safe_send=orch._safe_send,
        websocket=websocket,
        actor_user_id=actor_user_id,
        auth_principal=auth_principal,
        store=store,
        recorder=orch.audit_recorder,
    )
    if removed:
        try:
            from orchestrator import llm_gate
            await llm_gate.regate_after_clear(orch, actor_user_id)
            # The mandatory dialog replaced the modal on every socket.
            return None
        except Exception:
            logger.exception("llm gate re-gate failed (non-fatal)")
    return (SURFACE_KEY, {}, notice_block(
        "info", "No stored AI provider configuration."))



async def _handle_typesafe_save(orch: Any, websocket: Any, user_id: str, roles: Any, payload: Any):
    """``chrome_typesafe_save {fields}`` — probe-gated persist of the user's key.

    The order is the point: acknowledge, validate, probe, then persist. A key
    that fails any step never reaches the store, so a rejected save leaves a
    working stored key exactly as it was (FR-003).
    """
    _ = roles
    from llm_config.typesafe_handlers import TypeSafeSaveError, save_key

    blocked = await _require_acknowledgment(
        orch, websocket, user_id, payload, target="TypeSafe"
    )
    if blocked is not None:
        return (SURFACE_KEY, {"data_sharing_error": blocked.error},
                notice_block("error", blocked.error))

    store = _typesafe_store(orch)
    if store is None:
        return (SURFACE_KEY, {}, notice_block(
            "error", "TypeSafe settings are unavailable — try reloading."))

    fields = _fields(payload)
    actor_user_id, auth_principal = _actor(orch, websocket, user_id)
    try:
        await save_key(
            store,
            user_id,
            fields.get("typesafe_api_key"),
            recorder=getattr(orch, "audit_recorder", None),
            actor_user_id=actor_user_id,
            auth_principal=auth_principal,
        )
    except TypeSafeSaveError as exc:
        return (SURFACE_KEY, {}, notice_block("error", str(exc)))
    except Exception:
        logger.exception("TypeSafe key save failed")
        return (SURFACE_KEY, {}, notice_block(
            "error", "Couldn't save the TypeSafe key — try again."))

    # Deliberately no unlock_after_save: the TypeSafe key is not the LLM gate,
    # and a user with a TypeSafe key and no LLM configuration stays gated.
    return (SURFACE_KEY, {}, notice_block(
        "success", "TypeSafe key saved. Routing will use it from your next message."))


async def _handle_typesafe_clear(orch: Any, websocket: Any, user_id: str, roles: Any, payload: Any):
    """``chrome_typesafe_clear`` — remove the key. Never re-gates the user."""
    _ = roles
    _ = payload
    from llm_config.typesafe_handlers import clear_key

    store = _typesafe_store(orch)
    if store is None:
        return (SURFACE_KEY, {}, notice_block(
            "error", "TypeSafe settings are unavailable — try reloading."))

    actor_user_id, auth_principal = _actor(orch, websocket, user_id)
    try:
        removed = await clear_key(
            store,
            user_id,
            recorder=getattr(orch, "audit_recorder", None),
            actor_user_id=actor_user_id,
            auth_principal=auth_principal,
        )
    except Exception:
        logger.exception("TypeSafe key clear failed")
        return (SURFACE_KEY, {}, notice_block(
            "error", "Couldn't remove the TypeSafe key — try again."))

    # No regate_after_clear: removing this key returns the user to standard
    # routing, which is a working state, not a gated one.
    if removed:
        return (SURFACE_KEY, {}, notice_block(
            "success", "TypeSafe key removed. Astral will use standard routing."))
    return (SURFACE_KEY, {}, notice_block("info", "No TypeSafe key was stored."))


HANDLERS = {
    "chrome_llm_models": _handle_models,
    "chrome_llm_test": _handle_test,
    "chrome_llm_save": _handle_save,
    "chrome_llm_clear": _handle_clear,
    "chrome_typesafe_save": _handle_typesafe_save,
    "chrome_typesafe_clear": _handle_typesafe_clear,
}
