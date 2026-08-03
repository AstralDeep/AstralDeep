"""LLM provider settings surface (key ``llm``) — features 027/043/054.

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


async def _resolve_api_key(orch: Any, websocket: Any, user_id: str,
                           fields: Dict[str, str]) -> Tuple[str, bool]:
    """Resolve the API key for an action: submitted value or the saved one.

    The password field is write-only — a blank submission means "keep the
    key already saved" (the form's hint says so).
    """
    submitted = fields.get("api_key", "")
    if submitted:
        return submitted, False
    saved = await _saved_config(orch, user_id)
    if saved is not None and getattr(saved, "api_key", ""):
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

    key_optional = preset is not None and not preset.key_required
    key_label = "API key" + (" (optional for local runtimes)" if key_optional else "")
    if saved is not None and saved.has_key:
        key_placeholder = "Saved — leave blank to keep"
        key_hint = (
            '<p class="text-xs text-astral-muted">An API key is saved for your account. '
            "It is never displayed; leave the field blank to keep using it.</p>"
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

    return (
        f"{intro}"
        f"<div data-ui-form data-llm-endpoints='{_provider_endpoints_json()}' class=\"space-y-4\">"
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
        '<div class="flex flex-wrap gap-2">'
        f'{_button("chrome_llm_models", "Load models")}'
        f'{_button("chrome_llm_test", "Test connection")}'
        f'{_button("chrome_llm_save", "Save", primary=True)}'
        f"{clear_btn}"
        "</div></div>"
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
        key_help = "A key is saved for your account; leave blank to keep it."
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
    out.append(_sdui.form(
        form_fields,
        actions=[
            {"label": "Load models", "action": "chrome_llm_models"},
            {"label": "Test connection", "action": "chrome_llm_test"},
            {"label": "Save", "action": "chrome_llm_save", "variant": "primary"},
        ],
    ))
    if saved is not None:
        out.append(_sdui.button("Clear configuration", "chrome_llm_clear",
                                variant="secondary"))
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
    api_key, _used_saved = await _resolve_api_key(orch, websocket, user_id, fields)
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
    api_key, _used_saved = await _resolve_api_key(orch, websocket, user_id, fields)
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
    api_key, used_saved = await _resolve_api_key(orch, websocket, user_id, fields)
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


HANDLERS = {
    "chrome_llm_models": _handle_models,
    "chrome_llm_test": _handle_test,
    "chrome_llm_save": _handle_save,
    "chrome_llm_clear": _handle_clear,
}
