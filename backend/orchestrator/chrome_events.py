"""Feature 027 — chrome ui_event dispatcher.

Routes the settings-menu / surface / creation actions that arrive as
``{type:"ui_event", action, payload}`` from the web shell. Hooked from
``Orchestrator.handle_ui_message`` AFTER the legacy if/elif chain so the
026 actions are untouched; returns ``True`` when the action was handled
(including handled-with-error) and ``False`` for actions outside the
chrome/creation namespace.

Contract (contracts/chrome-ws-protocol.md): every failure renders an
in-modal error notice and structured-logs the exception — never a silent
drop. Admin-only surfaces/actions re-check the role server-side here
regardless of what the menu rendered (FR-014).
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

#: Feature 076: the UI socket a settings surface is being rendered FOR, set by
#: ``_render_surface`` around the builder call (``None`` outside a render).
current_surface_socket: contextvars.ContextVar = contextvars.ContextVar(
    "chrome_surface_socket", default=None)

# Lazily aggregated {action: (surface_key, handler)} — surfaces register via
# their module-level HANDLERS dicts; agentic_creation contributes the
# draft/revision decision actions through the same mechanism.
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
    """Build and validate one canonical operation-status projection."""

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
    """Send one validated status frame over the orchestrator's safe-send seam."""

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
    """Roles from the validated register_ui JWT claims (mock auth ⇒ admin)."""
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


# --- Feature 043: device-target-aware surface delivery -----------------------
# Web (browser) → ChromeRender HTML modal (feature 027, unchanged). Native SDUI
# (windows/android) → ChromeSurface: a ROTE-adapted astralprims component list
# the client renders through its EXISTING renderer (contracts/chrome-surface.md).

_TAG_RE = re.compile(r"<[^>]+>")


def _device_type(orch, websocket) -> str:
    """The connecting client's ROTE device type ('browser'|'windows'|'android')."""
    try:
        prof = orch.rote.get_profile(websocket)
        return getattr(prof.device_type, "value", str(prof.device_type))
    except Exception:
        return "browser"


def _strip_html(html: str) -> str:
    """Best-effort plain text from a chrome notice's HTML (native has no HTML).

    Tags become SPACES (then whitespace collapses) so adjacent blocks don't
    fuse into one word — the theme notice used to read
    "Daylight theme saved.Theme applied" on native clients."""
    import html as _htmlmod
    return " ".join(_htmlmod.unescape(_TAG_RE.sub(" ", html or "")).split())


def _notice_components(notice_html: str) -> list:
    """Map a handler's re-render notice (HTML) to a leading Alert component."""
    text = _strip_html(notice_html)
    if not text:
        return []
    low = (notice_html or "").lower()  # infer kind from the notice_block color class
    variant = "error" if "red-" in low else "success" if "green-" in low else "info"
    return [{"type": "alert", "variant": variant, "message": text}]


async def _push_surface(orch, websocket, surface_key, title, admin_only, components):
    from shared.protocol import ChromeSurface
    await _verify_human_delivery(orch, websocket)
    await orch._safe_send(websocket, ChromeSurface(
        region="modal", surface_key=surface_key, title=title,
        admin_only=bool(admin_only), components=list(components or []),
    ).to_json())


# Feature 051: iOS/macOS join Windows/Android as chrome-model SDUI natives
# (the watch is deliberately chrome-free — no surfaces on the wrist).
_NATIVE_SDUI_DEVICE_TYPES = ("windows", "android", "ios", "macos")


async def _push_error_notice(orch, websocket, title: str, message: str,
                             surface_key: str = ""):
    """Device-aware error notice (feature 044, FR-002/FR-017).

    Web keeps the feature-027 HTML modal; native SDUI clients get a
    ``chrome_surface`` carrying an error Alert — an HTML frame would be
    invisible to them (the pre-044 gap)."""
    if _device_type(orch, websocket) in _NATIVE_SDUI_DEVICE_TYPES:
        from webrender.chrome.surfaces import _sdui
        await _push_surface(orch, websocket, surface_key or "error", title, False,
                            [_sdui.alert(message, "error")])
    else:
        from webrender.chrome import chrome_error_block, render_modal_shell
        await _push_modal(orch, websocket, render_modal_shell(
            title, chrome_error_block(message, surface_key or None)))


def is_native_sdui(orch, websocket) -> bool:
    """Does this socket render surfaces as native SDUI components?

    Native surfaces are full screens with no modal ✕ (web) and, on Apple, no
    system Back (Android) — so a handler that leaves one on screen strands it.
    """
    return _device_type(orch, websocket) in _NATIVE_SDUI_DEVICE_TYPES


def open_surface_for(orch, websocket) -> str:
    """The surface key this socket last rendered and has not closed ("" when
    none). Feature 077: background work (the quick-create pipeline) re-renders
    a surface only while the person is still looking at it — never reopening
    a modal they closed or replacing one they navigated to."""
    return (getattr(orch, "_open_chrome_surface", None) or {}).get(id(websocket), "")


def _note_open_surface(orch, websocket, surface_key: str) -> None:
    table = getattr(orch, "_open_chrome_surface", None)
    if table is None:
        try:
            table = orch._open_chrome_surface = {}
        except Exception:  # noqa: BLE001 — a frozen test double
            return
    if surface_key:
        table[id(websocket)] = surface_key
    else:
        table.pop(id(websocket), None)


async def push_close(orch, websocket):
    """Device-aware modal close: web clears the HTML modal region; native SDUI
    clients receive the documented empty-components ``chrome_surface`` form."""
    _note_open_surface(orch, websocket, "")
    if is_native_sdui(orch, websocket):
        await _push_surface(orch, websocket, "", "", False, [])
    else:
        await _push_modal(orch, websocket, "")


async def _render_surface(orch, websocket, user_id, roles, surface_key: str,
                          params: dict, notice_html: str = ""):
    """Render a surface into the modal, adapting to the connecting client.

    Web → ChromeRender HTML (unchanged). Native SDUI (windows/android) →
    ChromeSurface (ROTE-adapted astralprims components). Admin-gated and
    gracefully-degrading on either path (Constitution X/XII, FR-014).
    """
    if surface_key == "guidance":
        from persistent_agents.models import AssignmentError
        raise AssignmentError("explicit_note_navigation_unavailable", 503)
    # Feature 076: a surface may need to know WHICH of the owner's sockets is
    # asking (the My computers surface offers the consent switch only to the
    # desktop that can host). Exposed as a context variable for the duration of
    # the render — never through ``params``, which surfaces may serialize.
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
    """The dialog's heading for this render.

    A surface whose heading depends on what it is showing (an agent's own
    dialog is headed by that agent's name) declares an async
    ``title(orch, user_id, params)``; the rest keep their fixed ``TITLE``.
    It is async because a heading that names a record has to answer the same
    "may this account see it?" question the body does, and that question
    reaches the database.

    A title() that raises falls back to ``TITLE`` rather than failing the
    render — a heading is never worth losing a dialog over, and the fallback
    reveals nothing.
    """
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
    """The surface's tab strip: a tuple of ``(key, label)`` string pairs.

    Anything else is dropped. A module that binds ``SECTIONS`` to something
    other than a tab strip — the User guide imported its own content sections
    under that name for a while — used to have the dispatcher print a repr of
    whatever it found across the top of the dialog. A tab strip that cannot be
    read as one is no tab strip.
    """
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
    """Who the settings dialog belongs to, for its account block.

    Derived from the SAME validated ``register_ui`` claims the roles come
    from, through the one shared derivation in ``web_auth``, so the dialog
    and the shell cannot disagree about who is signed in. Display only.
    """
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
    """The settings dialog's left rail for this session, or "" if unavailable.

    The gear opens the dialog instead of a dropdown, so the menu has to travel
    with every surface the dialog shows. It is built from the same
    ``build_menu_model`` the shell's gear and the native ``chrome_menu`` frame
    use, with the same host availability inputs, so what the rail offers can
    never drift from what the menu offers.

    A rail is navigation, not authority: a failure here leaves the dialog
    without one rather than taking the surface down, and every entry it draws
    still goes through the ordinary ``chrome_open`` checks.
    """
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
    """Web path — server-rendered HTML modal (feature 027; behavior unchanged)."""
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
    # Feature 089: a surface may declare a subtitle, an icon, section tabs and
    # a footer action row; the ones that declare nothing render exactly as
    # before. Declaring is opt-in per surface module, so a native surface is
    # unaffected either way (this is the web path only).
    # The rail is the settings menu. A surface that is not one of its entries
    # (an agent's own dialog, opened from the directory) declares NO_NAV and
    # renders as a plain dialog, so opening an agent does not look like
    # opening settings.
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
    """Native SDUI path (feature 043) — a ROTE-adapted ChromeSurface frame."""
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
        # Not yet converted to SDUI → a single labeled placeholder (FR-014),
        # never the retired text placeholder and never a blank screen.
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
    # ROTE-adapt for this device. Use ComponentAdapter directly (not
    # orch.rote.adapt) so surface components don't clobber the canvas
    # re-adaptation cache (orch.rote._last_components).
    try:
        from rote.adapter import ComponentAdapter
        payload = ComponentAdapter.adapt(payload, orch.rote.get_profile(websocket))
    except Exception:
        logger.debug("chrome: ROTE adapt failed; sending unadapted components", exc_info=True)
    await _push_surface(orch, websocket, surface_key, title,
                        getattr(mod, "ADMIN_ONLY", False), payload)


async def _audit_admin_rejection(orch, websocket, user_id: str, what: str):
    """US4 scenario 3 — audit a server-side admin rejection (best-effort)."""
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


# Feature 054: setup actions remain available while unconfigured. Feature 079
# also preserves owner inspection and authority-reducing assignment controls;
# continued work, consent and approvals retain the setup gate.
_LLM_GATE_ALLOWED_ACTIONS = frozenset({
    "chrome_llm_models", "chrome_llm_test", "chrome_llm_save", "chrome_llm_clear",
})


def _assignment_control_without_llm(orch, websocket, action, payload, user_id):
    """Let a verified owner inspect or quiesce work without granting new work."""
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
        # Reuse the same feature, live human socket and owner checks as the
        # final handler. Every resource read/control still enforces its owner.
        _assignment_access(orch, websocket, user_id)
    except (AssignmentError, TypeError, AttributeError):
        return False
    return True


async def _llm_gate_refusal(orch, websocket, action: str, user_id: str, *, payload=None) -> bool:
    """Server-authoritative first-run gate (feature 054, FR-014).

    Returns True when the action was refused: the refusal is audited
    (``llm_unconfigured``) and the mandatory setup dialog is (re)pushed so
    the client lands back on the only actionable surface."""
    try:
        claims = orch.ui_sessions.get(websocket) or {}
        uid = claims.get("sub") or user_id or ""
        if not uid or await orch.llm_configured_for(uid):
            return False
    except Exception:
        # Predicate failure: fail open here — the chat pre-flight and the
        # per-call resolver still fail closed on actual LLM use.
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
    """Dispatch one chrome/creation ui_event. Returns True if handled."""
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
    # New work and approvals keep the normal setup gate. Owner Schedule reads
    # and pause/stop/revoke remain operable when personal LLM setup is absent.
    if await _llm_gate_refusal(orch, websocket, action, user_id, payload=payload):
        return True
    from orchestrator.human_request_authority import current_human_caller
    human_caller = current_human_caller(expected_orchestrator=orch)
    if human_caller is None:
        roles = _roles(orch, websocket)
    else:
        from orchestrator.auth import _extract_roles
        roles = _extract_roles(human_caller.claims)
    # Resolved before the handler runs so an exception's error notice carries
    # the acting surface key (feature 044 — native key-matched reducers).
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
                # Feature 044: native clients don't inject chat_id client-side
                # (web's client.js does) — default to the socket's active chat
                # so per-chat surfaces (workspace_timeline) work everywhere.
                # Same fallback the timeline's _live handler already uses.
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
        # Admin re-check for actions owned by admin-only surfaces (FR-014).
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
