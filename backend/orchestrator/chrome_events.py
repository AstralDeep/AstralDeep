"""Dispatches settings-menu, surface, and creation ui_event actions from web/native
clients, rendering per device via ROTE; called from Orchestrator.handle_ui_message,
shared by projection_surfaces modules.
"""

import asyncio
import json
import contextvars
import logging
import re
from datetime import UTC, datetime
from typing import Any, Optional

from shared.perf import perf_span
from shared.protocol import OperationStatus

logger = logging.getLogger("Orchestrator.Chrome")

current_surface_socket: contextvars.ContextVar = contextvars.ContextVar(
    "chrome_surface_socket", default=None)

_HANDLERS = None


def canonical_operation_status(
    *,
    operation_id: str,
    action: str,
    surface: str,
    chat_id: Optional[str],
    connection_generation: str,
    request_generation: str,
    sequence: int,
    state: str,
    phase: str,
    label: str,
    terminal: bool,
    retryable: bool,
    error: Optional[dict[str, Any]],
    retry_after_ms: Optional[int],
    updated_at: Optional[datetime] = None,
) -> OperationStatus:
    timestamp = updated_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    frame = OperationStatus(
        operation_id=operation_id,
        action=action,
        surface=surface,
        chat_id=chat_id,
        connection_generation=connection_generation,
        request_generation=request_generation,
        sequence=sequence,
        state=state,
        phase=phase,
        label=label,
        terminal=terminal,
        retryable=retryable,
        error=error,
        retry_after_ms=retry_after_ms,
        updated_at=timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    )
    frame.validate()
    return frame


async def emit_operation_status(orch: Any, websocket: Any, **projection: Any) -> bool:
    frame = canonical_operation_status(**projection)
    return bool(await orch._safe_send(websocket, frame.to_json()))


def _handlers():
    global _HANDLERS
    if _HANDLERS is None:
        from orchestrator.projection_surfaces import collect_handlers
        _HANDLERS = collect_handlers()
        try:
            from orchestrator import agentic_creation
            for action, fn in agentic_creation.HANDLERS.items():
                _HANDLERS[action] = ("drafts", fn)
        except Exception:
            logger.exception("chrome: agentic_creation handlers unavailable")
    return _HANDLERS


def _is_chrome_action(action: str) -> bool:
    return bool(action) and (
        action.startswith("chrome_")
        or action in ("draft_approve", "draft_refine", "draft_discard",
                      "revision_apply", "revision_discard")
    )


def _roles(orch, websocket) -> list:
    claims = orch.ui_sessions.get(websocket) or {}
    roles = list((claims.get("realm_access") or {}).get("roles") or [])
    for client in (claims.get("resource_access") or {}).values():
        roles.extend(client.get("roles") or [])
    return roles


async def _push_modal(orch, websocket, html: str):
    from shared.protocol import ChromeRender
    await _verify_human_delivery(orch, websocket)
    await orch._safe_send(websocket, ChromeRender(region="modal", html=html).to_json())


async def _verify_human_delivery(orch, websocket):
    from orchestrator.human_request_authority import current_human_caller
    from persistent_agents.models import AssignmentError
    caller = current_human_caller(expected_orchestrator=orch)
    if caller is not None:
        pending = caller._binding.socket_request
        if pending is None or pending.websocket is not websocket:
            raise AssignmentError("human_authentication_required", 401)
        await caller.verify_delivery()
        caller._assert_local(orch)


_TAG_RE = re.compile(r"<[^>]+>")


def _device_type(orch, websocket) -> str:
    try:
        prof = orch.rote.get_profile(websocket)
        return getattr(prof.device_type, "value", str(prof.device_type))
    except Exception:
        return "browser"


def _strip_html(html: str) -> str:
    import html as _htmlmod
    return " ".join(_htmlmod.unescape(_TAG_RE.sub(" ", html or "")).split())


def _notice_components(notice_html: str) -> list:
    text = _strip_html(notice_html)
    if not text:
        return []
    low = (notice_html or "").lower()
    variant = "error" if "red-" in low else "success" if "green-" in low else "info"
    return [{"type": "alert", "variant": variant, "message": text}]


async def _push_surface(orch, websocket, surface_key, title, admin_only, components):
    from shared.protocol import ChromeSurface
    await _verify_human_delivery(orch, websocket)
    await orch._safe_send(websocket, ChromeSurface(
        region="modal", surface_key=surface_key, title=title,
        admin_only=bool(admin_only), components=list(components or []),
    ).to_json())


_NATIVE_SDUI_DEVICE_TYPES = ("windows", "android", "ios", "macos")


async def _push_error_notice(orch, websocket, title: str, message: str,
                             surface_key: str = ""):
    if _device_type(orch, websocket) in _NATIVE_SDUI_DEVICE_TYPES:
        from webrender.chrome.surfaces import _sdui
        await _push_surface(orch, websocket, surface_key or "error", title, False,
                            [_sdui.alert(message, "error")])
    else:
        from webrender.chrome import chrome_error_block, render_modal_shell
        await _push_modal(orch, websocket, render_modal_shell(
            title, chrome_error_block(message, surface_key or None)))


def is_native_sdui(orch, websocket) -> bool:
    return _device_type(orch, websocket) in _NATIVE_SDUI_DEVICE_TYPES


def open_surface_for(orch, websocket) -> str:
    return (getattr(orch, "_open_chrome_surface", None) or {}).get(id(websocket), "")


def _note_open_surface(orch, websocket, surface_key: str) -> None:
    table = getattr(orch, "_open_chrome_surface", None)
    if table is None:
        try:
            table = orch._open_chrome_surface = {}
        except Exception:  # noqa: BLE001
            return
    if surface_key:
        table[id(websocket)] = surface_key
    else:
        table.pop(id(websocket), None)


async def push_close(orch, websocket):
    _note_open_surface(orch, websocket, "")
    if is_native_sdui(orch, websocket):
        await _push_surface(orch, websocket, "", "", False, [])
    else:
        await _push_modal(orch, websocket, "")


async def _render_surface(orch, websocket, user_id, roles, surface_key: str,
                          params: dict, notice_html: str = ""):
    if surface_key == "guidance":
        from persistent_agents.models import AssignmentError
        raise AssignmentError("explicit_note_navigation_unavailable", 503)
    token = current_surface_socket.set(websocket)
    _note_open_surface(orch, websocket, surface_key)
    try:
        with perf_span("surface.render." + surface_key, surface=surface_key):
            if _device_type(orch, websocket) in _NATIVE_SDUI_DEVICE_TYPES:
                await _render_surface_sdui(orch, websocket, user_id, roles,
                                           surface_key, params, notice_html)
            else:
                await _render_surface_html(orch, websocket, user_id, roles,
                                           surface_key, params, notice_html)
    finally:
        current_surface_socket.reset(token)


async def _surface_title(mod, surface_key: str, orch, user_id, params) -> str:
    maker = getattr(mod, "title", None)
    if callable(maker):
        try:
            made = await maker(orch, user_id, params or {})
            if made:
                return str(made)
        except Exception:
            logger.debug("chrome: surface %s title() failed", surface_key, exc_info=True)
    return getattr(mod, "TITLE", surface_key)


def _surface_sections(mod) -> tuple:
    raw = getattr(mod, "SECTIONS", ()) or ()
    if isinstance(raw, (str, bytes, dict)):
        return ()
    out = []
    for entry in raw:
        if (isinstance(entry, (tuple, list)) and len(entry) == 2
                and all(isinstance(part, str) for part in entry)):
            out.append((entry[0], entry[1]))
        elif isinstance(entry, str):
            out.append((entry, entry))
    return tuple(out)


def _session_identity(orch, websocket, roles) -> dict:
    try:
        from orchestrator.web_auth import (
            MOCK_IDENTITY,
            identity_from_claims,
            _is_mock,
        )

        if _is_mock():
            return dict(MOCK_IDENTITY)
        return identity_from_claims(orch.ui_sessions.get(websocket) or {}, roles)
    except Exception:
        logger.debug("chrome: session identity unavailable", exc_info=True)
        return {}


def _settings_nav_html(roles, surface_key: str, identity=None) -> str:
    try:
        from webrender.chrome import render_settings_nav
        from webrender.chrome.menu_model import build_menu_model
        from orchestrator.chrome_availability import projection_chrome_availability

        model = build_menu_model(roles, **projection_chrome_availability())
        return render_settings_nav(model, surface_key, identity=identity)
    except Exception:
        logger.debug("chrome: settings rail unavailable", exc_info=True)
        return ""


async def _render_surface_html(orch, websocket, user_id, roles, surface_key: str,
                               params: dict, notice_html: str = ""):
    from webrender.chrome import chrome_error_block, render_modal_shell
    from orchestrator.projection_surfaces import get_surface

    mod = get_surface(surface_key)
    if mod is None:
        logger.warning("chrome: unknown surface %r requested", surface_key)
        await _push_modal(orch, websocket, render_modal_shell(
            "Not available", chrome_error_block(f"Unknown settings surface: {surface_key}")))
        return
    if getattr(mod, "ADMIN_ONLY", False) and "admin" not in roles:
        logger.warning("chrome: non-admin %s denied surface %s", user_id, surface_key)
        await _audit_admin_rejection(orch, websocket, user_id, surface_key)
        await _push_modal(orch, websocket, render_modal_shell(
            "Not authorized", chrome_error_block("This area requires the admin role.")))
        return
    try:
        body = await mod.render(orch, user_id, roles, params or {})
    except Exception:
        logger.exception("chrome: surface %s render failed", surface_key)
        await _push_modal(orch, websocket, render_modal_shell(
            getattr(mod, "TITLE", surface_key),
            chrome_error_block("This surface failed to load. Please retry.", surface_key)))
        return
    nav_html = "" if getattr(mod, "NO_NAV", False) else _settings_nav_html(
        roles, surface_key, _session_identity(orch, websocket, roles))
    await _push_modal(orch, websocket, render_modal_shell(
        await _surface_title(mod, surface_key, orch, user_id, params), (notice_html or "") + body, surface_key,
        subtitle=getattr(mod, "SUBTITLE", ""),
        icon=getattr(mod, "ICON", ""),
        sections=_surface_sections(mod),
        footer_html=getattr(mod, "footer_html", lambda: "")()
        if callable(getattr(mod, "footer_html", None)) else "",
        nav_html=nav_html))


async def _render_surface_sdui(orch, websocket, user_id, roles, surface_key: str,
                               params: dict, notice_html: str = ""):
    from orchestrator.projection_surfaces import get_surface
    from webrender.chrome.surfaces import _sdui

    mod = get_surface(surface_key)
    if mod is None:
        logger.warning("chrome: unknown surface %r requested (native)", surface_key)
        await _push_surface(orch, websocket, surface_key, "Not available", False,
                            [_sdui.alert(f"Unknown settings surface: {surface_key}", "error")])
        return
    title = await _surface_title(mod, surface_key, orch, user_id, params)
    if getattr(mod, "ADMIN_ONLY", False) and "admin" not in roles:
        logger.warning("chrome: non-admin %s denied surface %s (native)", user_id, surface_key)
        await _audit_admin_rejection(orch, websocket, user_id, surface_key)
        await _push_surface(orch, websocket, surface_key, "Not authorized", True,
                            [_sdui.alert("This area requires the admin role.", "error")])
        return
    builder = getattr(mod, "components", None)
    if builder is None:
        await _push_surface(orch, websocket, surface_key, title, False, [_sdui.placeholder(title)])
        return
    try:
        comps = list(await builder(orch, user_id, roles, params or {}) or [])
    except Exception:
        logger.exception("chrome: surface %s components() failed", surface_key)
        await _push_surface(orch, websocket, surface_key, title, False,
                            [_sdui.alert("This surface failed to load. Please retry.", "error")])
        return
    payload = _notice_components(notice_html) + comps
    # Not orch.rote.adapt — would clobber the canvas cache
    try:
        from rote.adapter import ComponentAdapter
        payload = ComponentAdapter.adapt(payload, orch.rote.get_profile(websocket))
    except Exception:
        logger.debug("chrome: ROTE adapt failed; sending unadapted components", exc_info=True)
    await _push_surface(orch, websocket, surface_key, title,
                        getattr(mod, "ADMIN_ONLY", False), payload)


async def _audit_admin_rejection(orch, websocket, user_id: str, what: str):
    try:
        from datetime import datetime, timezone

        from audit.recorder import get_recorder, make_correlation_id
        from audit.schemas import AuditEventCreate
        rec = get_recorder()
        if rec is None:
            return
        await rec.record(AuditEventCreate(
            actor_user_id=user_id or "unknown",
            auth_principal=user_id or "unknown",
            event_class="settings",
            action_type="settings.admin_denied",
            description=f"Non-admin attempted admin surface/action: {what}",
            correlation_id=make_correlation_id(),
            outcome="failure",
            started_at=datetime.now(timezone.utc),
        ))
    except Exception:
        logger.debug("chrome: admin-rejection audit failed", exc_info=True)


_LLM_GATE_ALLOWED_ACTIONS = frozenset({
    "chrome_llm_models", "chrome_llm_test", "chrome_llm_save", "chrome_llm_clear",
})


def _assignment_control_without_llm(orch, websocket, action, payload, user_id):
    if action not in {"chrome_assignment_pause", "chrome_assignment_stop", "chrome_assignment_revoke"}:
        if action != "chrome_open" or not isinstance(payload, dict):
            return False
        params = payload.get("params")
        if (payload.get("surface") != "personalization" or not isinstance(params, dict)
                or params.get("tab") != "schedule"
                or params.get("assignment_mode") not in (None, "list", "detail")):
            return False
    from persistent_agents.models import AssignmentError

    from orchestrator.projection_surfaces.personalization import _assignment_access
    try:
        _assignment_access(orch, websocket, user_id)
    except (AssignmentError, TypeError, AttributeError):
        return False
    return True


async def _llm_gate_refusal(orch, websocket, action: str, user_id: str, *, payload=None) -> bool:
    try:
        claims = orch.ui_sessions.get(websocket) or {}
        uid = claims.get("sub") or user_id or ""
        if not uid or await orch.llm_configured_for(uid):
            return False
    except Exception:
        logger.exception("chrome: llm gate predicate failed (failing open)")
        return False
    if action in _LLM_GATE_ALLOWED_ACTIONS:
        return False
    if _assignment_control_without_llm(orch, websocket, action, payload, user_id):
        return False
    try:
        actor_user_id, auth_principal = orch._llm_audit_principals(websocket)
        await orch._record_llm_unconfigured(
            orch.audit_recorder,
            actor_user_id=actor_user_id,
            auth_principal=auth_principal,
            feature=f"chrome:{action}",
        )
    except Exception:
        logger.debug("chrome: llm gate refusal audit failed", exc_info=True)
    try:
        from orchestrator import llm_gate
        await llm_gate.push_setup_dialog(orch, websocket, user_id)
    except Exception:
        logger.debug("chrome: llm gate re-push failed", exc_info=True)
    return True


async def handle_chrome_event(orch, websocket, action: str, payload: dict,
                              user_id: str, *, request_generation=None, work_read=None,
                              guidance_navigation=None) -> bool:
    from orchestrator.human_request_authority import _socket_method, bind_human_caller
    from persistent_agents.models import AssignmentError
    method = _socket_method({"type": "ui_event", "action": action, "payload": payload})
    if method is None:
        if isinstance(action, str) and action.startswith(("chrome_user_skill_", "chrome_declarative_", "chrome_note_")):
            raise AssignmentError("human_authentication_required", 401)
        return await _handle_chrome_event(orch, websocket, action, payload, user_id,
            request_generation=request_generation, work_read=work_read, guidance_navigation=guidance_navigation)
    import sys
    context_var = getattr(sys.modules.get(type(orch).__module__), "_CONNECTION_OPERATION_CONTEXT", None)
    if context_var is None:
        from orchestrator.orchestrator import _CONNECTION_OPERATION_CONTEXT as context_var
    pending = (context_var.get() or {}).get("human_request")
    if (pending is None or pending.websocket is not websocket or pending.boundary.orchestrator is not orch
            or pending.purpose != "metadata" or pending.method != method or pending.message.get("action") != action
            or (pending.message.get("payload") or {}) != payload):
        raise AssignmentError("human_authentication_required", 401)
    try:
        async with asyncio.timeout_at(pending.deadline):
            caller = await pending.authenticate()
            with bind_human_caller(caller):
                return await _handle_chrome_event(orch, websocket, action, payload, caller.owner_id,
                    request_generation=request_generation, work_read=work_read, guidance_navigation=guidance_navigation)
    except TimeoutError:
        raise AssignmentError("human_request_timeout", 408) from None


async def _handle_chrome_event(orch, websocket, action: str, payload: dict,
                              user_id: str, *, request_generation=None, work_read=None,
                              guidance_navigation=None) -> bool:
    if not _is_chrome_action(action):
        return False
    payload = payload or {}
    if (action.startswith("chrome_note_") or action == "chrome_turn_selection_set"
            or (action == "chrome_open"
                and isinstance(payload, dict) and payload.get("surface") == "guidance")):
        from orchestrator.projection_surfaces import get_surface
        return await get_surface("guidance").deliver(orch, websocket, user_id, action,
            payload, request_generation, guidance_navigation=guidance_navigation)
    if action in {"chrome_open", "chrome_close"}:
        from orchestrator.work_surface_authority import invalidate
        if work_read is None:
            invalidate(orch, websocket)
        if action == "chrome_open" and isinstance(payload, dict) and payload.get("surface") == "work":
            from orchestrator.projection_surfaces import get_surface
            delivered = await get_surface("work").deliver(orch, websocket, user_id,
                payload.get("params", {}), request_generation, work_read=work_read)
            if work_read is not None and not delivered:
                from persistent_agents.models import AssignmentError
                raise AssignmentError("work_read_unavailable", 503)
            return True
    if await _llm_gate_refusal(orch, websocket, action, user_id, payload=payload):
        return True
    from orchestrator.human_request_authority import current_human_caller
    human_caller = current_human_caller(expected_orchestrator=orch)
    if human_caller is None:
        roles = _roles(orch, websocket)
    else:
        from orchestrator.auth import _extract_roles
        roles = _extract_roles(human_caller.claims)
    err_surface = ""

    try:
        if action == "chrome_close":
            await push_close(orch, websocket)
            return True

        if action == "chrome_open":
            surface = str(payload.get("surface") or "")
            params = payload.get("params") or {}
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except json.JSONDecodeError:
                    params = {}
            if isinstance(params, dict) and not params.get("chat_id"):
                chat_id = getattr(orch, "_ws_active_chat", {}).get(id(websocket), "")
                if chat_id:
                    params["chat_id"] = chat_id
            await _render_surface(orch, websocket, user_id, roles, surface, params)
            return True

        entry = _handlers().get(action)
        if entry is None:
            logger.warning("chrome: unknown chrome action %r", action)
            await _push_error_notice(orch, websocket, "Not available",
                                     f"Unknown action: {action}")
            return True

        surface_key, fn = entry
        err_surface = surface_key
        from orchestrator.projection_surfaces import get_surface
        owner = get_surface(surface_key)
        if owner is not None and getattr(owner, "ADMIN_ONLY", False) and "admin" not in roles:
            logger.warning("chrome: non-admin %s denied action %s", user_id, action)
            await _audit_admin_rejection(orch, websocket, user_id, action)
            await _push_error_notice(orch, websocket, "Not authorized",
                                     "This action requires the admin role.",
                                     surface_key)
            return True

        result = await fn(orch, websocket, user_id, roles, payload)
        if result is not None:
            re_surface, re_params, notice_html = result
            await _render_surface(orch, websocket, user_id, roles, re_surface,
                                  re_params or {}, notice_html or "")
        return True

    except Exception:
        from orchestrator.human_request_authority import current_human_caller
        if current_human_caller(expected_orchestrator=orch) is not None:
            raise
        logger.exception("chrome: action %s failed", action)
        try:
            await _push_error_notice(orch, websocket, "Something went wrong",
                                     "The action failed. Please retry.",
                                     err_surface)
        except Exception:
            logger.debug("chrome: error-notice push failed", exc_info=True)
        return True
