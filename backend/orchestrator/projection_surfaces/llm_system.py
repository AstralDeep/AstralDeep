"""Deep-owned host adapter for the system-LLM Projection surface.

Manages the single deployment-wide credential that powers system-context
LLM work: scheduled-job turns, agent codegen (incl. attachment auto-parsers),
knowledge synthesis, conversation compaction, workspace combine/condense,
and job narration. It NEVER serves user chat (FR-019); user records never
serve system work.

Delivery posture: a **declared web-only admin carve-out** (Constitution XII,
spec FR-018) — the menu item lives in the admin group the server already
omits from every native menu channel, exactly like Tool quality / Tutorial
admin. Every handler re-checks the admin role server-side regardless of what
any client rendered (the ``ADMIN_ONLY`` marker drives both the surface gate
and the per-action re-check in ``chrome_events``).

Hygiene identical to the user surface: the key is write-only (never echoed),
Fernet-encrypted at rest, probe-gated on save, audited as
``llm_config_change{scope:"system"}``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from pydantic import ValidationError

from llm_config.audit_events import record_llm_config_change
from llm_config.probe import probe_chat_completion
from llm_config.providers import all_presets, get_preset
from llm_config.ws_handlers import validate_config_submission
from webrender.chrome import esc, notice_block
from orchestrator.projection_surfaces.llm import (
    _INPUT_CLS,
    _LABEL_CLS,
    _LABEL_TEXT_CLS,
    _actor,
    _button,
    _claims,
    _endpoint_block,
    _failure_notice,
    _fields,
    _model_field,
    _provider_endpoints_json,
    _provider_field,
    _provider_key,
    _request_shim,
    _validation_message,
)

logger = logging.getLogger("Orchestrator.Chrome.LLMSystem")

TITLE = "System LLM"

SURFACE_KEY = "llm_system"

ADMIN_ONLY = True

_CONSUMERS_NOTE = (
    "Used ONLY for background work: scheduled jobs, file-parser generation, "
    "knowledge synthesis, conversation compaction, workspace combine/condense, "
    "and job summaries. It never answers user chat — every user connects their "
    "own provider."
)


def _store(orch: Any):
    return getattr(orch, "_llm_store", None)


async def _system_config(orch: Any):
    store = _store(orch)
    if store is None:
        return None
    try:
        return await store.get_system()
    except Exception:
        logger.exception("llm_system surface: system-config read failed")
        return None


async def _resolve_api_key_sys(orch: Any, fields: Dict[str, str]):
    """Submitted key, else the saved system key (write-only semantics)."""
    submitted = fields.get("api_key", "")
    if submitted:
        return submitted, False
    saved = await _system_config(orch)
    if saved is not None and getattr(saved, "api_key", ""):
        return saved.api_key, True
    return "", False


# ---------------------------------------------------------------------------
# Render (web only — declared carve-out; no components() by design)
# ---------------------------------------------------------------------------

async def render(orch: Any, user_id: str, roles: Any, params: Any) -> str:
    """Render the System LLM form body (admin gate enforced by the caller
    AND re-checked per action handler)."""
    _ = roles
    params = params if isinstance(params, dict) else {}
    saved = await _system_config(orch)
    provider = str(params.get("provider")
                   or (getattr(saved, "provider", "") if saved else "")
                   or "openai").lower()
    if get_preset(provider) is None:
        provider = "custom"
    preset = get_preset(provider)
    base_url = str(params.get("base_url")
                   or (getattr(saved, "base_url", "") if saved else "") or "")
    model = str(params.get("model") or (getattr(saved, "model", "") if saved else "") or "")
    models = params.get("models") if isinstance(params.get("models"), list) else None

    endpoint_block = _endpoint_block(provider, base_url)
    if saved is not None:
        badge = ('<span class="text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 '
                 'rounded-full bg-green-500/10 text-green-400 border border-green-500/20">'
                 "configured</span>")
        key_placeholder = "Saved — leave blank to keep"
        clear_btn = _button("chrome_llm_sys_clear", "Clear system credential", collect=False)
    else:
        badge = ('<span class="text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 '
                 'rounded-full bg-white/5 text-astral-muted border border-white/10">'
                 "not configured</span>")
        key_placeholder = (preset.key_prefix_hint if preset else "") or "sk-..."
        clear_btn = ""

    return (
        f'<p class="text-xs text-astral-muted">{esc(_CONSUMERS_NOTE)}</p>'
        f"<div data-ui-form data-llm-endpoints='{_provider_endpoints_json()}' class=\"space-y-4\">"
        '<div class="bg-white/5 border border-white/10 rounded-lg p-4 space-y-3">'
        f'<div class="flex items-center justify-between">'
        f'<span class="{_LABEL_TEXT_CLS}">System credential</span>{badge}</div>'
        f'<label class="{_LABEL_CLS}"><span class="{_LABEL_TEXT_CLS}">Provider</span>'
        f"{_provider_field(provider)}</label>"
        f"{endpoint_block}"
        f'<label class="{_LABEL_CLS}"><span class="{_LABEL_TEXT_CLS}">API key</span>'
        f'<input type="password" name="api_key" value="" placeholder="{esc(key_placeholder)}" '
        f'autocomplete="off" class="{_INPUT_CLS}"></label>'
        f'<label class="{_LABEL_CLS}"><span class="{_LABEL_TEXT_CLS}">Model</span>'
        f"{_model_field(model, models)}</label>"
        "</div>"
        '<div class="flex flex-wrap gap-2">'
        f'{_button("chrome_llm_sys_models", "Load models")}'
        f'{_button("chrome_llm_sys_test", "Test connection")}'
        f'{_button("chrome_llm_sys_save", "Save", primary=True)}'
        f"{clear_btn}"
        "</div></div>"
    )


# ---------------------------------------------------------------------------
# Handlers (each re-checked admin-only via ADMIN_ONLY in chrome_events)
# ---------------------------------------------------------------------------

def _keep(fields: Dict[str, str], provider: str) -> Dict[str, Any]:
    return {"provider": provider,
            "base_url": fields.get("base_url", ""),
            "model": fields.get("model", "")}


async def _handle_models(orch: Any, websocket: Any, user_id: str, roles: Any, payload: Any):
    """``chrome_llm_sys_models {fields}`` — list the endpoint's models."""
    _ = roles
    from llm_config.api import ListModelsRequest, list_models
    from llm_config.providers import resolve_base_url

    fields = _fields(payload)
    provider = _provider_key(fields)
    keep = _keep(fields, provider)
    base_url = resolve_base_url(provider, fields.get("base_url", ""))
    if not base_url:
        return (SURFACE_KEY, keep, notice_block(
            "error", "Enter the endpoint address for your custom provider."))
    api_key, _used = await _resolve_api_key_sys(orch, fields)
    preset = get_preset(provider)
    if not api_key and (preset is None or preset.key_required):
        return (SURFACE_KEY, keep, notice_block(
            "error", "An API key is required to load models."))
    try:
        body = ListModelsRequest(api_key=api_key or "not-needed", base_url=base_url)
    except ValidationError as exc:
        return (SURFACE_KEY, keep, notice_block("error", _validation_message(exc)))
    resp = await list_models(body=body, request=_request_shim(orch),
                             user_id=user_id, user_payload=_claims(orch, websocket))
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
    """``chrome_llm_sys_test {fields}`` — probe the prospective credential."""
    _ = roles
    from llm_config.api import TestConnectionRequest, test_connection
    from llm_config.providers import resolve_base_url

    fields = _fields(payload)
    provider = _provider_key(fields)
    keep = _keep(fields, provider)
    base_url = resolve_base_url(provider, fields.get("base_url", ""))
    model = fields.get("model", "")
    if not base_url or not model:
        return (SURFACE_KEY, keep, notice_block(
            "error", "Endpoint and model are required to test the connection."))
    api_key, _used = await _resolve_api_key_sys(orch, fields)
    preset = get_preset(provider)
    if not api_key and (preset is None or preset.key_required):
        return (SURFACE_KEY, keep, notice_block(
            "error", "An API key is required to test the connection."))
    try:
        body = TestConnectionRequest(api_key=api_key or "not-needed",
                                     base_url=base_url, model=model)
    except ValidationError as exc:
        return (SURFACE_KEY, keep, notice_block("error", _validation_message(exc)))
    resp = await test_connection(body=body, request=_request_shim(orch),
                                 user_id=user_id, user_payload=_claims(orch, websocket))
    if resp.ok:
        latency = f" in {int(resp.latency_ms)} ms" if resp.latency_ms is not None else ""
        return (SURFACE_KEY, keep, notice_block(
            "success", f"Connection OK — model {resp.model} responded{latency}."))
    return (SURFACE_KEY, keep, _failure_notice(
        "Connection test failed", resp.error_class, resp.upstream_message))


async def _handle_save(orch: Any, websocket: Any, user_id: str, roles: Any, payload: Any):
    """``chrome_llm_sys_save {fields}`` — probe-gated persist of the system
    credential (Fernet at rest; audited ``scope:"system"``)."""
    _ = roles
    fields = _fields(payload)
    provider = _provider_key(fields)
    keep = _keep(fields, provider)
    api_key, used_saved = await _resolve_api_key_sys(orch, fields)
    store = _store(orch)
    if store is None:
        return (SURFACE_KEY, keep, notice_block(
            "error", "Configuration store unavailable — try reloading."))

    submission = {"provider": provider, "api_key": api_key,
                  "base_url": fields.get("base_url", ""),
                  "model": fields.get("model", "")}
    norm, errors = validate_config_submission(submission)
    if errors:
        return (SURFACE_KEY, keep, notice_block(
            "error", "; ".join(f"{k}: {v}" for k, v in errors.items())))

    actor_user_id, auth_principal = _actor(orch, websocket, user_id)
    ok, error_class, upstream = await probe_chat_completion(
        api_key=norm["api_key"], base_url=norm["base_url"], model=norm["model"])
    try:
        await record_llm_config_change(
            orch.audit_recorder, actor_user_id=actor_user_id,
            auth_principal=auth_principal, action="tested",
            base_url=norm["base_url"], model=norm["model"], transport="ws",
            result="success" if ok else "failure",
            error_class=error_class if not ok else None, scope="system")
    except Exception:  # pragma: no cover — audit is best-effort
        logger.warning("system llm tested audit failed", exc_info=True)
    if not ok:
        return (SURFACE_KEY, keep, _failure_notice(
            "Connection test failed — nothing saved", error_class, upstream))

    prior = await _system_config(orch)
    await store.set_system(
        provider=norm["provider"], base_url=norm["base_url"],
        model=norm["model"], api_key=norm["api_key"], updated_by=actor_user_id)
    try:
        await record_llm_config_change(
            orch.audit_recorder, actor_user_id=actor_user_id,
            auth_principal=auth_principal,
            action="updated" if prior is not None else "created",
            base_url=norm["base_url"], model=norm["model"], transport="ws",
            scope="system")
    except Exception:  # pragma: no cover
        logger.warning("system llm save audit failed", exc_info=True)
    suffix = " (kept the previously saved API key)" if used_saved else ""
    return (SURFACE_KEY, keep, notice_block(
        "success", f"System LLM credential saved{suffix}. Background features "
                   "will use it on their next run."))


async def _handle_clear(orch: Any, websocket: Any, user_id: str, roles: Any, payload: Any):
    """``chrome_llm_sys_clear`` — delete the system credential. Background
    features degrade honestly (log + skip + failed runs) until reconfigured."""
    _ = roles
    _ = payload
    store = _store(orch)
    if store is None:
        return (SURFACE_KEY, {}, notice_block(
            "error", "Configuration store unavailable — try reloading."))
    removed = await store.clear_system()
    if removed:
        actor_user_id, auth_principal = _actor(orch, websocket, user_id)
        try:
            await record_llm_config_change(
                orch.audit_recorder, actor_user_id=actor_user_id,
                auth_principal=auth_principal, action="cleared",
                base_url=None, model=None, transport="ws", scope="system")
        except Exception:  # pragma: no cover
            logger.warning("system llm clear audit failed", exc_info=True)
        return (SURFACE_KEY, {}, notice_block(
            "success", "System credential cleared — background AI features "
                       "will skip honestly until a new one is saved."))
    return (SURFACE_KEY, {}, notice_block("info", "No system credential was saved."))


HANDLERS = {
    "chrome_llm_sys_models": _handle_models,
    "chrome_llm_sys_test": _handle_test,
    "chrome_llm_sys_save": _handle_save,
    "chrome_llm_sys_clear": _handle_clear,
}


# Expose the catalog for tests asserting the two surfaces share one source.
PROVIDER_KEYS = tuple(p.key for p in all_presets())
